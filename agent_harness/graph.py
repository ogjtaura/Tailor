"""The LangGraph orchestration graph.

The loop is expressed entirely in graph edges - nodes never recurse into
themselves. ``decide`` is the single router; every routing decision (and any
stop reason it implies) is made in the ``decide`` NODE, because LangGraph does
not persist mutations made inside conditional-edge functions.

    START -> bootstrap -> plan -> implement -> verify -> decide -> ...
             repair / escalation_review / review / checkpoint hang off decide
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from langgraph.graph import END, START, StateGraph

from agent_harness.config import Config
from agent_harness import git_tools
from agent_harness.git_tools import GitError, GitTools, WorktreeSnapshot
from agent_harness import persistence
from agent_harness.persistence import Persistence
from agent_harness import verifier
from agent_harness.state import (
    RESUMABLE_NODES,
    Finding,
    HarnessState,
    Status,
    StopReason,
    clear_candidate_evidence,
    is_stagnant,
    update_stagnation,
)
from agent_harness.agents import AgentRouter
from agent_harness.workers.claude import ClaudeWorker, PlanningError, WorkerUsageLimitError
from agent_harness.workers.codex import CodexWorker

NODE_LABELS = {
    "bootstrap": "BOOTSTRAP",
    "plan": "PLAN",
    "implement": "IMPLEMENT",
    "freeze": "FREEZE",
    "verify": "VERIFY",
    "review": "REVIEW",
    "decide": "DECIDE",
    "repair": "REPAIR",
    "escalation_review": "ESCALATION",
    "checkpoint": "CHECKPOINT",
}


def recursion_budget(config: Config) -> int:
    """A LangGraph recursion limit that can never undercut the harness's own
    logical limits. Derived from the configured maxima, then generously padded -
    over-provisioning is harmless (our stop reasons fire first); under-
    provisioning raises GraphRecursionError, which must never happen.

    Per writer iteration, worst case:
      writer + freeze + verify + decide                 = 4
      (review + decide) x (review_retries + 1)          = 2*(r+1)
      escalation_review + repair + freeze + verify + decide = 5  (stagnation cycle)
    Plus plan + checkpoint per task, and tasks <= max_iterations.
    """
    a = config.agent
    r = max(0, config.codex.review_retries)
    per_iteration = 4 + 2 * (r + 1) + 5
    return 100 + a.max_iterations * (per_iteration + 4)


class Printer:
    """Human-readable transition log (rich if available, else plain)."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        try:  # pragma: no cover - cosmetic
            from rich.console import Console

            self._console = Console(soft_wrap=True, highlight=False, markup=False)
        except Exception:  # pragma: no cover
            self._console = None

    def _emit(self, line: str) -> None:
        if not self.enabled:
            return
        if self._console is not None:  # pragma: no cover
            self._console.print(line)
        else:
            print(line, flush=True)

    def transition(self, node: str, state: HarnessState, detail: str = "") -> None:
        label = NODE_LABELS.get(node, node.upper())
        self._emit(f"[iter {state.iteration:>2}] {label:<11} {detail}".rstrip())

    def info(self, msg: str) -> None:
        self._emit(msg)


class Engine:
    """Builds and runs the harness graph with injectable collaborators."""

    def __init__(
        self,
        config: Config,
        *,
        claude: Optional[ClaudeWorker] = None,
        codex: Optional[CodexWorker] = None,
        agents: Optional[AgentRouter] = None,
        git: Optional[GitTools] = None,
        store: Optional[Persistence] = None,
        printer: Optional[Printer] = None,
        resume_target: Optional[str] = None,
    ) -> None:
        self.config = config
        self.git = git or GitTools(config.repo_root)
        self.store = store or Persistence(config.repo_root)
        # ROLE -> BACKEND -> MODEL is resolved by the router; the graph never
        # picks a backend or builds provider argv. `claude=`/`codex=` stay as
        # injection points for the backend adapters (tests, dry-run).
        self.agents = agents or AgentRouter(config, claude=claude, codex=codex)
        self.printer = printer or Printer()
        self.resume_target = resume_target

    # -- graph construction ------------------------------------------------

    def build(self):
        g = StateGraph(HarnessState)
        for name in ("bootstrap", "plan", "implement", "freeze", "verify", "review",
                     "decide", "repair", "escalation_review", "checkpoint"):
            g.add_node(name, getattr(self, name))

        g.add_edge(START, "bootstrap")
        # Single source of truth: every node a resume may target is in
        # state.RESUMABLE_NODES, and each maps to itself here ("end" -> END).
        resume_map = {n: (END if n == "end" else n) for n in RESUMABLE_NODES}
        g.add_conditional_edges("bootstrap", self._route_after_bootstrap, resume_map)
        g.add_conditional_edges("plan", self._gate("implement"), {"implement": "implement", "end": END})
        g.add_conditional_edges("implement", self._gate("freeze"), {"freeze": "freeze", "end": END})
        g.add_conditional_edges("repair", self._gate("freeze"), {"freeze": "freeze", "end": END})
        g.add_conditional_edges(
            "freeze", self._route_after_freeze, {"verify": "verify", "decide": "decide", "end": END}
        )
        g.add_conditional_edges("verify", self._gate("decide"), {"decide": "decide", "end": END})
        g.add_conditional_edges("review", self._gate("decide"), {"decide": "decide", "end": END})
        g.add_conditional_edges(
            "escalation_review", self._gate("repair"), {"repair": "repair", "end": END}
        )
        g.add_conditional_edges(
            "decide",
            self._route_after_decide,
            {
                "repair": "repair",
                "escalate": "escalation_review",
                "review": "review",
                "checkpoint": "checkpoint",
                "end": END,
            },
        )
        g.add_conditional_edges(
            "checkpoint", self._route_after_checkpoint, {"plan": "plan", "end": END}
        )
        return g.compile()

    def run(self, state: HarnessState) -> HarnessState:
        app = self.build()
        limit = recursion_budget(self.config)
        try:
            result = app.invoke(state, {"recursion_limit": limit})
            final = result if isinstance(result, HarnessState) else HarnessState.model_validate(result)
        except Exception as exc:  # includes GraphRecursionError - must not leak
            state.stop_reason = state.stop_reason or StopReason.CLI_FATAL
            state.status = Status.STOPPED
            self.printer.info(f"graph aborted: {type(exc).__name__}: {exc}")
            self.store.save(state, note=f"graph aborted: {type(exc).__name__}")
            return state
        self.store.save(final, note="run finished")
        return final

    # -- routers (read-only; decisions are made in nodes) ------------------

    def _gate(self, default: str) -> Callable[[HarnessState], str]:
        def _router(state: HarnessState) -> str:
            return "end" if state.stop_reason is not None else default

        return _router

    def _route_after_bootstrap(self, state: HarnessState) -> str:
        if state.stop_reason is not None:
            return "end"
        return self.resume_target or "plan"

    def _route_after_freeze(self, state: HarnessState) -> str:
        if state.stop_reason is not None:
            return "end"
        # No candidate object was produced (no-op writer) -> let decide handle
        # the no-progress bookkeeping without a verify/review of "nothing".
        return "verify" if state.candidate_commit_oid else "decide"

    def _route_after_decide(self, state: HarnessState) -> str:
        if state.stop_reason is not None:
            return "end"
        return state.next_node or "end"

    def _route_after_checkpoint(self, state: HarnessState) -> str:
        if state.stop_reason is not None:
            return "end"
        return state.next_node or "end"

    def _decide_route(self, state: HarnessState) -> str:
        """Decision logic, called from the ``decide`` NODE so any stop_reason it
        sets is persisted. Returns an edge key."""
        a = self.config.agent
        if state.stop_reason is not None:
            return "end"

        # Reviewer INFRASTRUCTURE failure -> retry the reviewer, never repair.
        if state.review_error is not None:
            return "review"

        # No-op writer: freeze produced no immutable candidate. A no-op can
        # never be a successful task completion.
        if state.candidate_commit_oid is None and state.checkpoint_phase == "none":
            if state.no_progress_count >= a.max_repairs_per_task:
                state.stop_reason = StopReason.NO_PROGRESS
                return "end"
            if state.iteration >= a.max_iterations:
                state.stop_reason = StopReason.ITERATION_LIMIT
                return "end"
            return "repair"

        if state.checks_passed is False:
            if state.repair_attempts >= a.max_repairs_per_task:
                state.stop_reason = StopReason.REPAIR_LIMIT
                return "end"
            if state.iteration >= a.max_iterations:
                state.stop_reason = StopReason.ITERATION_LIMIT
                return "end"
            if is_stagnant(state, max_stagnant=a.max_stagnant_iterations):
                if state.stagnation_escalated:
                    state.stop_reason = StopReason.STAGNATION_UNRESOLVED
                    return "end"
                state.stagnation_escalated = True
                return "escalate"
            return "repair"

        # Deterministic checks passed against the immutable candidate.
        if state.review_status in (None, "pending"):
            return "review"
        if state.review_status == "fail":
            if state.repair_attempts >= a.max_repairs_per_task:
                state.stop_reason = StopReason.REPAIR_LIMIT
                return "end"
            if state.iteration >= a.max_iterations:
                state.stop_reason = StopReason.ITERATION_LIMIT
                return "end"
            return "repair"
        return "checkpoint"  # review_status == "pass"

    # -- nodes ---------------------------------------------------------------

    def bootstrap(self, state: HarnessState) -> HarnessState:
        state.status = Status.BOOTSTRAP
        try:
            self.config.require_runnable()
        except Exception as exc:
            state.stop_reason = StopReason.CONFIG_INVALID
            self.printer.info(f"config rejected: {exc}")
            return state
        try:
            self.git.ensure_repo()
        except GitError as exc:
            state.stop_reason = StopReason.CLI_FATAL
            self.printer.info(f"bootstrap failed: {exc}")
            return state

        if self.resume_target is None:
            # Branch / clean-tree guards apply to a FRESH run only.
            try:
                current_ref = self.git.current_branch_ref()
            except GitError as exc:
                state.stop_reason = StopReason.DETACHED_HEAD
                self.printer.info(f"bootstrap failed: {exc}")
                self.store.init_run(state)
                return state
            # THE run's write authorization: the checked-out feature ref, once,
            # now. Validated by the ONE canonical target validator (syntax,
            # refs/heads namespace, main/master exclusion, non-symbolic).
            try:
                self.git.validate_checkpoint_target_ref(current_ref)
            except GitError as exc:
                state.stop_reason = (
                    StopReason.PROTECTED_BRANCH
                    if ("protected" in str(exc).lower() or "symbolic" in str(exc).lower())
                    else StopReason.CLI_FATAL
                )
                self.printer.info(f"refusing to start: {exc}")
                self.store.init_run(state)
                return state
            try:
                if self.config.git.branch_required:
                    self.git.require_non_default_branch()
            except GitError as exc:
                state.stop_reason = (
                    StopReason.DETACHED_HEAD if "detached" in str(exc).lower()
                    else StopReason.CLI_FATAL
                )
                self.printer.info(f"bootstrap failed: {exc}")
                self.store.init_run(state)
                return state
            if self.config.git.require_clean_worktree and not self.git.is_clean():
                state.stop_reason = StopReason.DIRTY_WORKTREE
                self.printer.info(
                    "refusing to start: git worktree is not clean. Commit or stash first."
                )
                self.store.init_run(state)
                return state
            tip = self.git.resolve_ref(current_ref)
            state.run_target_ref = current_ref
            state.baseline_head = tip
            state.expected_head = tip
            self.store.init_run(state)  # first persisted state already carries run_target_ref
            # Create the write-once authorization manifest. This is FRESH-RUN
            # ONLY - there is no resume path that (re)creates it.
            try:
                self.store.create_run_manifest(
                    state, authorized_target_ref=current_ref, initial_target_oid=tip
                )
            except Exception as exc:
                state.stop_reason = StopReason.CLI_FATAL
                self.printer.info(f"could not create run manifest: {exc}")
                self.store.save(state, note="run manifest creation failed")
                return state
        else:
            # RESUME: authorization comes ONLY from the existing write-once
            # manifest. Missing/invalid/mismatched -> fail closed, no heal.
            reason = persistence.verify_run_authorization(state, self.store, self.git)
            if reason is not None:
                state.stop_reason = reason
                self.printer.info(f"resume authorization refused: {reason.value}")
                self.store.save(state, note="resume authorization refused")
                return state

        log_dir = self.store.run_log_dir(state)
        self.agents.bind_run(state.run_id, Path(log_dir))
        state.last_completed_node = "bootstrap"
        self.printer.transition("bootstrap", state, f"branch={self.git.current_branch()}")
        self.store.save(state, note="bootstrap ok")
        return state

    def plan(self, state: HarnessState) -> HarnessState:
        state.status = Status.PLANNING
        if not state.remaining_tasks and state.current_task is None:
            state.worker_in_flight = "claude_plan"
            self.store.save(state, note="planner in flight")
            try:
                plan = self.agents.plan(
                    objective=state.objective,
                    repo_context=self._repo_context(),
                    iteration=state.iteration,
                )
            except WorkerUsageLimitError:
                state.worker_in_flight = None
                state.stop_reason = StopReason.USAGE_LIMIT
                self.store.save(state, note="planner usage limit")
                return state
            except PlanningError as exc:
                state.worker_in_flight = None
                state.stop_reason = StopReason.PLANNING_FAILED
                self.printer.info(f"planning failed: {exc}")
                self.store.save(state, note="planning failed")
                return state
            state.worker_in_flight = None
            state.remaining_tasks = [t.title for t in plan.tasks]

        if state.current_task is None and state.remaining_tasks:
            state.current_task = state.remaining_tasks.pop(0)
            self._reset_task_scoped(state)

        if state.current_task is None:
            state.stop_reason = StopReason.SUCCESS
            state.status = Status.DONE
        elif state.iteration >= self.config.agent.max_iterations:
            state.stop_reason = StopReason.ITERATION_LIMIT

        state.last_completed_node = "plan"
        self.printer.transition("plan", state, f"task={state.current_task!r}")
        self.store.save(state, note="plan")
        return state

    def implement(self, state: HarnessState) -> HarnessState:
        return self._write_worker(state, role="implement", worker_key="claude_implement")

    def repair(self, state: HarnessState) -> HarnessState:
        state.repair_attempts += 1
        return self._write_worker(state, role="repair", worker_key="claude_repair")

    def _write_worker(self, state: HarnessState, *, role: str, worker_key: str) -> HarnessState:
        state.status = Status.IMPLEMENTING

        # HEAD must be where we left it before we hand control to a writer.
        try:
            if self.git.head() != state.expected_head:
                state.stop_reason = StopReason.EXPECTED_HEAD_MOVED
                self.printer.info("HEAD moved unexpectedly before a writer invocation")
                self.store.save(state, note="expected head moved")
                return state
            before = self.git.snapshot()
        except GitError as exc:
            state.stop_reason = StopReason.CLI_FATAL
            self.printer.info(f"git error before writer: {exc}")
            return state

        # Pre-existing changes that are neither harness-owned nor in a
        # harness-managed protected path (e.g. .agent/) are unexplained.
        external = sorted(
            p for p in set(before.changed) - set(state.owned_paths)
            if not self.git.protected_hits([p], self.config.safety.protected_paths)
        )
        if external:
            state.stop_reason = StopReason.UNEXPECTED_WORKTREE_STATE
            self.printer.info(f"unrelated pre-existing changes present: {external}")
            self.store.save(state, note="unrelated changes before writer")
            return state

        state.iteration += 1
        state.worker_in_flight = worker_key
        state.pre_writer_paths = list(before.changed)
        state.pre_writer_hashes = dict(before.hashes)
        self.store.save(state, note=f"{role} in flight")

        if role == "implement":
            inv = self.agents.implement(
                objective=state.objective, task=state.current_task or "",
                repo_context=self._repo_context(), iteration=state.iteration,
            )
        else:
            inv = self.agents.repair(
                objective=state.objective, task=state.current_task or "",
                failures=self._failure_brief(state), root_cause=state.root_cause or "",
                iteration=state.iteration,
            )
            state.root_cause = None

        state.worker_in_flight = None

        try:
            after = self.git.snapshot()
        except GitError:
            after = WorktreeSnapshot()
        attributable = self.git.attributable_changes(before, after)

        hits = self.git.protected_hits(attributable, self.config.safety.protected_paths)
        if hits:
            state.stop_reason = StopReason.PROTECTED_PATH_MODIFIED
            self.printer.info(f"worker modified protected paths: {hits}")
            self.store.save(state, note="protected path modified")
            return state

        # Record attributable changes FIRST - a failed invocation's partial file
        # edits are preserved on disk, never auto-discarded. (no_progress is
        # decided in `freeze` from the candidate tree, the authoritative signal.)
        state.add_owned(attributable)

        # A write-capable invocation is a success only if the subprocess is
        # clean AND the structured response parsed AND is_error is false AND no
        # usage-limit signal is present (classification or subtype).
        if inv.is_usage_limit:
            state.stop_reason = StopReason.USAGE_LIMIT
        elif not inv.ok:
            state.stop_reason = StopReason.CLI_FATAL

        state.last_completed_node = role
        self.printer.transition(
            role, state,
            f"claude exit={inv.result.exit_code} is_error={inv.is_error} "
            f"parsed={inv.parsed} attributable={len(attributable)}",
        )
        self.store.save(state, note=f"{role} done")
        return state

    # -- freeze the immutable candidate ------------------------------------

    def freeze(self, state: HarnessState) -> HarnessState:
        """Freeze the harness-owned working-tree changes into an immutable Git
        tree + (unreachable) commit object, BEFORE verification and review. The
        feature-branch ref is NOT touched here."""
        state.status = Status.IMPLEMENTING

        # -- crash recovery: candidate TREE was frozen, the commit was not yet
        #    made. Rebuild the commit FROM THE PERSISTED TREE OID + the
        #    already-persisted deterministic spec - never from the mutable
        #    working tree, which may have drifted since the freeze.
        if (
            state.checkpoint_phase == "candidate_frozen"
            and state.candidate_tree_oid
            and not state.candidate_commit_oid
        ):
            return self._resume_commit_from_frozen_tree(state)

        # a brand-new candidate invalidates ALL prior verify/review evidence
        clear_candidate_evidence(state)

        # THE target is the authorized run target, never the currently checked-out
        # branch. It must be a direct, non-symbolic, non-main/master refs/heads ref.
        target_ref = state.run_target_ref
        try:
            self.git.validate_checkpoint_target_ref(target_ref)
        except GitError as exc:
            state.stop_reason = (
                StopReason.PROTECTED_BRANCH
                if ("protected" in str(exc).lower() or "symbolic" in str(exc).lower())
                else StopReason.INVALID_CHECKPOINT_STATE
            )
            self.printer.info(f"freeze refused: {exc}")
            self.store.save(state, note="freeze target ref invalid")
            return state

        # Any prior notion of the checkpoint target must equal the run target.
        if state.checkpoint_target_ref and state.checkpoint_target_ref != target_ref:
            state.stop_reason = StopReason.INVALID_CHECKPOINT_STATE
            self.printer.info(
                f"freeze: checkpoint_target_ref {state.checkpoint_target_ref} != "
                f"authorized run target {target_ref}"
            )
            self.store.save(state, note="freeze target mismatch")
            return state

        # If the user has switched the checkout away from the run target, the
        # working tree no longer represents the feature branch - stop conservatively
        # rather than freeze confusing content.
        try:
            checked_out = self.git.current_branch_ref()
        except GitError:
            checked_out = None
        if checked_out != target_ref:
            state.stop_reason = StopReason.EXPECTED_HEAD_MOVED
            self.printer.info(
                f"freeze: checked-out branch {checked_out} != run target {target_ref}"
            )
            self.store.save(state, note="freeze checkout switched away from run target")
            return state

        parent = self.git.resolve_ref(target_ref)
        if parent is None or parent != state.expected_head:
            state.stop_reason = StopReason.EXPECTED_HEAD_MOVED
            self.printer.info(f"freeze: {target_ref} moved ({parent} != {state.expected_head})")
            self.store.save(state, note="freeze expected head moved")
            return state

        if not state.owned_paths:
            state.candidate_tree_oid = state.candidate_commit_oid = None
            state.checkpoint_phase = "none"
            state.no_progress_count += 1
            state.last_completed_node = "freeze"
            self.printer.transition("freeze", state, "no owned paths -> no candidate")
            self.store.save(state, note="freeze no-op (empty owned)")
            return state

        try:
            tree_oid = self.git.freeze_candidate_tree(
                parent=parent, owned_paths=state.owned_paths,
                protected=self.config.safety.protected_paths,
            )
        except GitError as exc:
            state.stop_reason = StopReason.CANDIDATE_INVALID
            self.printer.info(f"freeze failed: {exc}")
            self.store.save(state, note="freeze failed")
            return state

        if tree_oid == self.git.commit_tree_of(parent):
            # owned paths produce no net change vs the branch tip -> a no-op
            state.candidate_tree_oid = state.candidate_commit_oid = None
            state.checkpoint_phase = "none"
            state.no_progress_count += 1
            state.last_completed_node = "freeze"
            self.printer.transition("freeze", state, "candidate tree == parent tree -> no-op")
            self.store.save(state, note="freeze no-op (tree == parent)")
            return state

        state.no_progress_count = 0
        now = f"{int(time.time())} +0000"
        state.checkpoint_target_ref = target_ref
        state.checkpoint_expected_parent = parent
        state.checkpoint_task = state.current_task
        state.checkpoint_paths = sorted(state.owned_paths)
        state.checkpoint_message = (
            f"harness: {state.current_task} [iter {state.iteration}] [run {state.run_id}]"
        )
        state.checkpoint_author = [git_tools.HARNESS_IDENTITY[0], git_tools.HARNESS_IDENTITY[1], now]
        state.checkpoint_committer = list(state.checkpoint_author)
        state.candidate_tree_oid = tree_oid
        state.candidate_commit_oid = None
        state.checkpoint_phase = "candidate_frozen"
        self.store.save(state, note="candidate tree frozen")  # <-- crash boundary A

        try:
            commit_oid = self.git.commit_tree(
                tree_oid=tree_oid, parent=parent, message=state.checkpoint_message,
                author=state.checkpoint_author, committer=state.checkpoint_committer,
            )
        except GitError as exc:
            state.stop_reason = StopReason.CANDIDATE_INVALID
            self.printer.info(f"commit-tree failed: {exc}")
            self.store.save(state, note="commit-tree failed")
            return state
        state.candidate_commit_oid = commit_oid
        state.last_completed_node = "freeze"
        self.printer.transition("freeze", state, f"candidate {commit_oid[:9]} (tree {tree_oid[:9]})")
        self.store.save(state, note="candidate commit created")  # <-- crash boundary B
        return state

    def _resume_commit_from_frozen_tree(self, state: HarnessState) -> HarnessState:
        """Tree->commit crash window recovery. ``candidate_tree_oid`` is the
        immutable source of truth; the commit is (re)created from it and the
        already-persisted deterministic spec. The working tree is never consulted.
        Fails closed if the persisted spec is incomplete or inconsistent."""
        tree = state.candidate_tree_oid
        ref = state.checkpoint_target_ref
        parent = state.checkpoint_expected_parent
        msg = state.checkpoint_message
        author = state.checkpoint_author
        committer = state.checkpoint_committer

        def _stop(reason: StopReason, why: str) -> HarnessState:
            state.stop_reason = reason
            self.printer.info(f"freeze (tree->commit recovery): {why}")
            self.store.save(state, note=f"tree->commit recovery stop: {reason.value}")
            return state

        if not (ref and parent and msg and author and committer):
            return _stop(StopReason.INVALID_CHECKPOINT_STATE,
                         "persisted candidate commit spec is incomplete")
        if not (isinstance(author, (list, tuple)) and len(author) == 3
                and isinstance(committer, (list, tuple)) and len(committer) == 3):
            return _stop(StopReason.INVALID_CHECKPOINT_STATE,
                         "persisted author/committer spec is malformed")
        if state.run_target_ref and ref != state.run_target_ref:
            return _stop(StopReason.INVALID_CHECKPOINT_STATE,
                         f"persisted target {ref!r} != authorized run target {state.run_target_ref!r}")
        try:
            self.git.validate_checkpoint_target_ref(ref)
        except GitError as exc:
            return _stop(
                StopReason.PROTECTED_BRANCH
                if ("protected" in str(exc).lower() or "symbolic" in str(exc).lower())
                else StopReason.INVALID_CHECKPOINT_STATE,
                str(exc),
            )
        try:
            if self.git.object_type(tree) != "tree":
                return _stop(StopReason.INVALID_CHECKPOINT_STATE,
                             f"persisted candidate_tree_oid {tree} is not a tree object")
            if not self.git.object_exists(parent):
                return _stop(StopReason.INVALID_CHECKPOINT_STATE,
                             f"persisted expected parent {parent} is gone")
            current = self.git.resolve_ref(ref)
        except GitError as exc:
            return _stop(StopReason.INVALID_CHECKPOINT_STATE, str(exc))

        if current != parent or state.expected_head not in (None, parent):
            return _stop(StopReason.EXPECTED_HEAD_MOVED,
                         f"{ref} is at {current}, expected parent {parent}")

        try:
            commit_oid = self.git.commit_tree(
                tree_oid=tree, parent=parent, message=msg,
                author=list(author), committer=list(committer),
            )
            if self.git.commit_tree_of(commit_oid) != tree:
                return _stop(StopReason.CANDIDATE_INVALID,
                             "rebuilt commit tree != persisted candidate tree")
            if self.git.commit_parents(commit_oid) != [parent]:
                return _stop(StopReason.CANDIDATE_INVALID,
                             "rebuilt commit parent != persisted expected parent")
        except GitError as exc:
            return _stop(StopReason.CANDIDATE_INVALID, f"commit-tree failed: {exc}")

        state.candidate_commit_oid = commit_oid
        state.checkpoint_phase = "candidate_frozen"
        state.last_completed_node = "freeze"
        self.printer.transition(
            "freeze", state, f"recovered candidate {commit_oid[:9]} from frozen tree {tree[:9]}"
        )
        self.store.save(state, note="candidate commit rebuilt from frozen tree")
        return state

    def verify(self, state: HarnessState) -> HarnessState:
        """Run the configured deterministic checks against the IMMUTABLE
        candidate commit (a throwaway detached worktree), never the mutable
        original working tree."""
        state.status = Status.VERIFYING
        state.review_status = state.review_severity = None
        state.review_findings = []
        state.review_error = None
        # a fresh verification run supersedes any prior review evidence
        state.reviewed_tree_oid = None
        state.reviewed_commit_oid = None

        if state.checkpoint_phase not in ("candidate_frozen", "verified") or not state.candidate_commit_oid:
            state.stop_reason = StopReason.CANDIDATE_INVALID
            self.printer.info("verify reached without a frozen candidate")
            self.store.save(state, note="verify without candidate")
            return state

        # Prove the candidate's Git identity (object types, tree(C)==T,
        # parents(C)==[P]) BEFORE spending a deterministic-check run on it.
        try:
            self.git.validate_candidate_identity(
                state.candidate_commit_oid, state.candidate_tree_oid,
                state.checkpoint_expected_parent,
            )
        except GitError as exc:
            state.stop_reason = StopReason.INVALID_CHECKPOINT_STATE
            self.printer.info(f"verify: candidate identity invalid: {exc}")
            self.store.save(state, note="verify candidate identity invalid")
            return state

        # the checks must run against the EXACT candidate commit, in isolation
        try:
            with self.git.materialize(state.candidate_commit_oid) as wt:
                results = verifier.run_checks(self.config.checks.commands, cwd=wt)
        except GitError as exc:
            state.stop_reason = StopReason.CLI_FATAL
            self.printer.info(f"could not materialize candidate for verification: {exc}")
            self.store.save(state, note="materialize failed")
            return state

        state.check_results = results
        state.checks_passed = verifier.checks_passed(results)
        if state.checks_passed:
            state.verified_tree_oid = state.candidate_tree_oid
            state.verified_commit_oid = state.candidate_commit_oid
            state.checkpoint_phase = "verified"
        else:
            state.verified_tree_oid = None
            state.verified_commit_oid = None
            state.checkpoint_phase = "candidate_frozen"

        update_stagnation(state, candidate_tree_oid=state.candidate_tree_oid)
        state.last_completed_node = "verify"
        self.printer.transition("verify", state, verifier.summarise(results))
        self.store.save(state, note="verify")
        return state

    def review(self, state: HarnessState) -> HarnessState:
        """Independent review of the SAME immutable candidate: the payload is an
        object-level diff ``expected_parent -> candidate_commit`` (no working
        tree / index). Fails closed if the payload cannot be built."""
        state.status = Status.REVIEWING

        tree = state.candidate_tree_oid
        commit = state.candidate_commit_oid
        if (
            state.checkpoint_phase not in ("verified",)
            or not commit
            or state.verified_tree_oid != tree
            or state.verified_commit_oid != commit
        ):
            state.stop_reason = StopReason.CANDIDATE_INVALID
            self.printer.info("review reached without a verified candidate")
            self.store.save(state, note="review without verified candidate")
            return state

        # Re-prove candidate identity BEFORE building a payload for the PAID
        # reviewer - an invalid candidate must never reach Codex.
        try:
            self.git.validate_candidate_identity(
                commit, tree, state.checkpoint_expected_parent
            )
        except GitError as exc:
            state.stop_reason = StopReason.INVALID_CHECKPOINT_STATE
            self.printer.info(f"review: candidate identity invalid: {exc}")
            self.store.save(state, note="review candidate identity invalid")
            return state

        try:
            review_input = self.git.diff_tree(
                state.checkpoint_expected_parent, state.candidate_commit_oid
            )
        except GitError as exc:
            state.stop_reason = StopReason.REVIEW_PAYLOAD_ERROR
            self.printer.info(f"could not build review payload: {exc}")
            self.store.save(state, note="review payload error")
            return state
        if not review_input.strip():
            state.stop_reason = StopReason.REVIEW_PAYLOAD_ERROR
            self.printer.info("review payload empty despite a frozen candidate; failing closed")
            self.store.save(state, note="review payload empty")
            return state

        state.worker_in_flight = "codex_review"
        self.store.save(state, note="review in flight")
        outcome = self.agents.routine_review(
            diff=review_input,
            check_results=verifier.summarise(state.check_results),
            iteration=state.iteration,
        )
        state.worker_in_flight = None

        if outcome.kind == "verdict" and outcome.verdict is not None:
            state.review_status = outcome.verdict.verdict
            state.review_severity = outcome.verdict.severity
            state.review_findings = [Finding(**f.model_dump()) for f in outcome.verdict.findings]
            if outcome.verdict.verdict == "pass":
                state.reviewed_tree_oid = tree
                state.reviewed_commit_oid = commit
                state.checkpoint_phase = "reviewed"
            else:
                state.reviewed_tree_oid = None
                state.reviewed_commit_oid = None
            state.review_error = None
            detail = f"verdict={state.review_status} severity={state.review_severity}"
        else:
            state.review_error = outcome.reason
            state.review_error_retries += 1
            if outcome.classification == "usage_limit":
                state.stop_reason = StopReason.USAGE_LIMIT
            elif state.review_error_retries > self.config.codex.review_retries:
                state.stop_reason = StopReason.REVIEWER_ERROR
            detail = f"infra_error: {outcome.reason} (retry {state.review_error_retries})"

        state.last_completed_node = "review"
        self.printer.transition("review", state, detail)
        self.store.save(state, note="review")
        return state

    def decide(self, state: HarnessState) -> HarnessState:
        state.status = Status.DECIDING
        route = self._decide_route(state)
        state.next_node = route
        state.last_completed_node = "decide"
        self.printer.transition(
            "decide", state,
            f"checks={state.checks_passed} review={state.review_status} "
            f"repairs={state.repair_attempts} stagnant={state.stagnant_iterations} -> {route}",
        )
        self.store.save(state, note=f"decide -> {route}")
        return state

    def escalation_review(self, state: HarnessState) -> HarnessState:
        state.status = Status.ESCALATING
        state.worker_in_flight = "codex_escalate"
        self.store.save(state, note="escalation in flight")
        diff = ""
        try:
            if state.candidate_commit_oid and state.checkpoint_expected_parent:
                diff = self.git.diff_tree(
                    state.checkpoint_expected_parent, state.candidate_commit_oid
                )
        except GitError:
            diff = ""
        outcome = self.agents.escalation_review(
            diff=diff, failures=self._failure_brief(state), iteration=state.iteration
        )
        state.worker_in_flight = None
        state.root_cause = outcome.root_cause
        if outcome.classification == "usage_limit":
            state.stop_reason = StopReason.USAGE_LIMIT
        state.last_completed_node = "escalation_review"
        self.printer.transition(
            "escalation_review", state,
            "root-cause obtained" if outcome.root_cause else f"no root-cause ({outcome.classification})",
        )
        self.store.save(state, note="escalation")
        return state

    def checkpoint(self, state: HarnessState) -> HarnessState:
        """Accept the reviewed immutable candidate by atomically advancing the
        PERSISTED feature-branch ref (compare-and-swap). Never `git add`/`git
        commit`; never touch whatever branch happens to be checked out; never
        reconstruct content from the working tree; never adopt a foreign commit.
        """
        state.status = Status.CHECKPOINTING
        phase = state.checkpoint_phase
        # THE target is the authorized run target - never checkpoint_target_ref
        # on its own, never the checked-out branch.
        ref = state.run_target_ref
        parent = state.checkpoint_expected_parent
        candidate = state.candidate_commit_oid
        tree = state.candidate_tree_oid

        # -- identity present -----------------------------------------------
        if not (ref and parent and candidate and tree):
            return self._checkpoint_stop(state, StopReason.CANDIDATE_INVALID,
                                         "checkpoint missing candidate/target identity")

        # -- INVARIANT: the run target and the checkpoint target are the same
        #    ref, and it is a direct non-symbolic non-main/master refs/heads ref.
        if state.checkpoint_target_ref and state.checkpoint_target_ref != ref:
            return self._checkpoint_stop(
                state, StopReason.INVALID_CHECKPOINT_STATE,
                f"checkpoint_target_ref {state.checkpoint_target_ref!r} != run target {ref!r}")
        try:
            self.git.validate_checkpoint_target_ref(ref)
        except GitError as exc:
            return self._checkpoint_stop(
                state,
                StopReason.PROTECTED_BRANCH
                if ("protected" in str(exc).lower() or "symbolic" in str(exc).lower())
                else StopReason.INVALID_CHECKPOINT_STATE,
                str(exc),
            )

        # -- checkpoint phase must permit a CAS ----------------------------
        if phase not in ("reviewed", "ref_update_intent", "ref_updated"):
            return self._checkpoint_stop(state, StopReason.CLI_FATAL,
                                         f"checkpoint reached in phase {phase!r}")

        # -- evidence identity: verify AND review bound to the EXACT (tree,
        #    commit) pair. A PASS from a prior candidate cannot authorise this.
        if not (state.verified_tree_oid == state.reviewed_tree_oid == tree):
            return self._checkpoint_stop(state, StopReason.REVIEW_INVALIDATED,
                                         "verified/reviewed tree OID != candidate tree OID")
        if not (state.verified_commit_oid == state.reviewed_commit_oid == candidate):
            return self._checkpoint_stop(state, StopReason.REVIEW_INVALIDATED,
                                         "verified/reviewed commit OID != candidate commit OID")
        if state.checks_passed is not True:
            return self._checkpoint_stop(state, StopReason.REVIEW_INVALIDATED,
                                         "deterministic checks are not currently PASS")
        if state.review_status != "pass":
            return self._checkpoint_stop(state, StopReason.REVIEW_INVALIDATED,
                                         f"review status is {state.review_status!r}, not pass")

        # -- Git object relationships (defence in depth) ----------------
        try:
            self.git.validate_candidate_identity(candidate, tree, parent)
            current = self.git.resolve_ref(ref)
        except GitError as exc:
            return self._checkpoint_stop(state, StopReason.CANDIDATE_INVALID, str(exc))

        # -- phase: ref already advanced -> bookkeeping only -----------
        if phase == "ref_updated" or current == candidate:
            if current != candidate:
                return self._checkpoint_stop(
                    state, StopReason.UNEXPECTED_WORKTREE_STATE,
                    f"phase ref_updated but {ref} ({current}) != candidate {candidate}")
            state.checkpoint_phase = "ref_updated"
            state.expected_head = candidate
            if candidate not in state.commits:
                state.commits.append(candidate)
            self._maybe_sync_index(state, candidate, ref)
            return self._finish_checkpoint(state)

        # -- phase: CAS may have happened (crash right after update-ref)
        if phase == "ref_update_intent" and current == parent:
            pass  # CAS did not land; retry below
        elif phase == "ref_update_intent":
            return self._checkpoint_stop(
                state, StopReason.REF_UPDATE_CONFLICT,
                f"{ref} ({current}) is neither expected parent nor our candidate")
        elif current != parent:
            return self._checkpoint_stop(
                state, StopReason.REF_UPDATE_CONFLICT,
                f"{ref} moved to {current}; expected parent {parent}")

        # -- atomic compare-and-swap of the persisted feature ref -----
        state.checkpoint_phase = "ref_update_intent"
        self.store.save(state, note="ref-update intent recorded")  # <-- crash boundary
        try:
            self.git.cas_update_ref(ref, candidate, parent)
        except GitError as exc:
            return self._checkpoint_stop(state, StopReason.REF_UPDATE_CONFLICT, str(exc))

        state.checkpoint_phase = "ref_updated"
        state.expected_head = candidate
        state.commits.append(candidate)
        self.store.save(state, note="feature ref advanced (CAS)")  # <-- crash boundary
        self._maybe_sync_index(state, candidate, ref)
        return self._finish_checkpoint(state)

    def _checkpoint_stop(self, state, reason, msg):
        state.stop_reason = reason
        self.printer.info(f"checkpoint: {msg}")
        self.store.save(state, note=f"checkpoint stop: {reason.value}")
        return state

    def _maybe_sync_index(self, state, commit_oid, ref):
        """Re-sync the REAL index to the accepted commit for `git status`
        hygiene - only when HEAD is still on the feature branch we advanced, and
        the working tree already matches (no data loss possible). Skipped if the
        user has switched away."""
        try:
            if self.git.current_branch_ref() == ref:
                self.git.sync_index_to(commit_oid)
        except GitError:
            pass

    def _finish_checkpoint(self, state: HarnessState) -> HarnessState:
        task = state.checkpoint_task or state.current_task
        if task and task not in state.completed_tasks:
            state.completed_tasks.append(task)
        if state.current_task == task:
            state.current_task = None
        self._reset_task_scoped(state)
        state.last_completed_node = "checkpoint"

        if not state.remaining_tasks:
            state.stop_reason = StopReason.SUCCESS
            state.status = Status.DONE
            state.next_node = "end"
        else:
            state.next_node = "plan"

        self.printer.transition(
            "checkpoint", state,
            f"ref={state.commits[-1][:9] if state.commits else 'none'} -> {state.next_node}",
        )
        self.store.save(state, note="checkpoint done")
        return state

    # -- helpers -------------------------------------------------------------

    def _reset_task_scoped(self, state: HarnessState) -> None:
        state.repair_attempts = 0
        state.no_progress_count = 0
        state.review_error_retries = 0
        state.stagnant_iterations = 0
        state.repeated_failure_count = 0
        state.stagnation_escalated = False
        state.last_failure_fingerprint = None
        state.last_candidate_tree_oid = None
        state.checks_passed = None
        state.review_status = None
        state.review_severity = None
        state.review_findings = []
        state.review_error = None
        state.owned_paths = []
        state.pre_writer_paths = []
        state.pre_writer_hashes = {}
        state.root_cause = None
        # immutable-object checkpoint fields
        state.checkpoint_phase = "none"
        state.checkpoint_task = None
        state.checkpoint_paths = []
        state.checkpoint_target_ref = None
        state.checkpoint_expected_parent = None
        state.checkpoint_message = None
        state.checkpoint_author = None
        state.checkpoint_committer = None
        state.candidate_tree_oid = None
        state.candidate_commit_oid = None
        state.verified_tree_oid = None
        state.verified_commit_oid = None
        state.reviewed_tree_oid = None
        state.reviewed_commit_oid = None

    def _failure_brief(self, state: HarnessState) -> str:
        lines: list[str] = []
        if state.checks_passed and not state.owned_paths:
            lines.append(
                "- NO PROGRESS: the previous attempt produced no attributable "
                "repository change. Actually implement the current task by editing "
                "the files it requires."
            )
        for chk in state.failing_checks():
            lines.append(f"- CHECK FAILED (exit {chk.exit_code}): {chk.command}")
            tail = (chk.stderr_tail or chk.stdout_tail).strip().splitlines()[-15:]
            lines.extend("    " + ln for ln in tail)
        if state.review_status == "fail":
            for f in state.review_findings:
                lines.append(f"- REVIEW FINDING {f.id}: {f.description}")
                if f.evidence:
                    lines.append(f"    evidence: {f.evidence}")
                if f.suggested_fix:
                    lines.append(f"    suggested fix: {f.suggested_fix}")
        return "\n".join(lines) or "(no specific failure captured)"

    def _repo_context(self) -> str:
        try:
            branch = self.git.current_branch()
        except GitError:
            branch = "?"
        return (
            f"Repository root: {self.config.repo_root}\n"
            f"Branch: {branch}\n"
            f"Deterministic checks: {self.config.checks.commands}\n"
        )
