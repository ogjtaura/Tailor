"""The LangGraph orchestration graph.

The loop is expressed entirely in graph edges - nodes never recurse into
themselves. ``decide`` is the single router; every routing decision (and any
stop reason it implies) is made in the ``decide`` NODE, because LangGraph does
not persist mutations made inside conditional-edge functions.

    START -> bootstrap -> plan -> implement -> verify -> decide -> ...
             repair / escalation_review / review / checkpoint hang off decide
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from langgraph.graph import END, START, StateGraph

from agent_harness.config import Config
from agent_harness.git_tools import GitError, GitTools, WorktreeSnapshot
from agent_harness.persistence import Persistence
from agent_harness import verifier
from agent_harness.state import (
    Finding,
    HarnessState,
    Status,
    StopReason,
    is_stagnant,
    update_stagnation,
)
from agent_harness.workers.claude import ClaudeWorker, PlanningError, WorkerUsageLimitError
from agent_harness.workers.codex import CodexWorker

NODE_LABELS = {
    "bootstrap": "BOOTSTRAP",
    "plan": "PLAN",
    "implement": "IMPLEMENT",
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
      writer + verify + decide                          = 3
      (review + decide) x (review_retries + 1)          = 2*(r+1)
      escalation_review + repair + verify + decide      = 4   (stagnation cycle)
    Plus plan + checkpoint per task, and tasks <= max_iterations.
    """
    a = config.agent
    r = max(0, config.codex.review_retries)
    per_iteration = 3 + 2 * (r + 1) + 4
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
        git: Optional[GitTools] = None,
        store: Optional[Persistence] = None,
        printer: Optional[Printer] = None,
        resume_target: Optional[str] = None,
    ) -> None:
        self.config = config
        self.git = git or GitTools(config.repo_root)
        self.store = store or Persistence(config.repo_root)
        self.claude = claude or ClaudeWorker(config)
        self.codex = codex or CodexWorker(config)
        self.printer = printer or Printer()
        self.resume_target = resume_target

    # -- graph construction ------------------------------------------------

    def build(self):
        g = StateGraph(HarnessState)
        for name in ("bootstrap", "plan", "implement", "verify", "review",
                     "decide", "repair", "escalation_review", "checkpoint"):
            g.add_node(name, getattr(self, name))

        g.add_edge(START, "bootstrap")
        resume_map = {
            "plan": "plan", "implement": "implement", "verify": "verify",
            "review": "review", "repair": "repair", "decide": "decide",
            "checkpoint": "checkpoint", "end": END,
        }
        g.add_conditional_edges("bootstrap", self._route_after_bootstrap, resume_map)
        g.add_conditional_edges("plan", self._gate("implement"), {"implement": "implement", "end": END})
        g.add_conditional_edges("implement", self._gate("verify"), {"verify": "verify", "end": END})
        g.add_conditional_edges("verify", self._gate("decide"), {"decide": "decide", "end": END})
        g.add_conditional_edges("review", self._gate("decide"), {"decide": "decide", "end": END})
        g.add_conditional_edges("repair", self._gate("verify"), {"verify": "verify", "end": END})
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

        # Deterministic checks passed.
        if not state.owned_paths:
            # The writer produced nothing to commit - a no-op cannot be success.
            if state.no_progress_count >= a.max_repairs_per_task:
                state.stop_reason = StopReason.NO_PROGRESS
                return "end"
            if state.iteration >= a.max_iterations:
                state.stop_reason = StopReason.ITERATION_LIMIT
                return "end"
            return "repair"

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
            if self.config.git.branch_required:
                self.git.require_non_default_branch()
        except GitError as exc:
            state.stop_reason = (
                StopReason.DETACHED_HEAD if "detached" in str(exc).lower() else StopReason.CLI_FATAL
            )
            self.printer.info(f"bootstrap failed: {exc}")
            return state

        if self.resume_target is None:
            if self.config.git.require_clean_worktree and not self.git.is_clean():
                state.stop_reason = StopReason.DIRTY_WORKTREE
                self.printer.info(
                    "refusing to start: git worktree is not clean. Commit or stash first."
                )
                self.store.init_run(state)
                return state
            self.store.init_run(state)
            state.baseline_head = self.git.head()
            state.expected_head = state.baseline_head

        log_dir = self.store.run_log_dir(state)
        self.claude.log_dir = Path(log_dir)
        self.codex.log_dir = Path(log_dir)
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
                plan = self.claude.plan(
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

        external = sorted(set(before.changed) - set(state.owned_paths))
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
            inv = self.claude.implement(
                objective=state.objective, task=state.current_task or "",
                repo_context=self._repo_context(), iteration=state.iteration,
            )
        else:
            inv = self.claude.repair(
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

        state.add_owned(attributable)
        if attributable:
            state.no_progress_count = 0
        else:
            state.no_progress_count += 1

        cls = inv.result.classification
        if cls == "usage_limit":
            state.stop_reason = StopReason.USAGE_LIMIT
        elif inv.result.timed_out or (inv.result.exit_code not in (0, None)):
            state.stop_reason = StopReason.CLI_FATAL

        state.last_completed_node = role
        self.printer.transition(
            role, state,
            f"claude exit={inv.result.exit_code} attributable={len(attributable)} "
            f"no_progress={state.no_progress_count}",
        )
        self.store.save(state, note=f"{role} done")
        return state

    def verify(self, state: HarnessState) -> HarnessState:
        state.status = Status.VERIFYING
        # a fresh implementation/repair invalidates any prior review
        state.review_status = None
        state.review_severity = None
        state.review_findings = []
        state.review_error = None
        state.reviewed_paths = []

        results = verifier.run_checks(self.config.checks.commands, cwd=self.config.repo_root)
        state.check_results = results
        state.checks_passed = verifier.checks_passed(results)
        try:
            new_hash = self.git.diff_hash()
        except GitError:
            new_hash = None
        update_stagnation(state, new_diff_hash=new_hash)
        state.last_completed_node = "verify"
        self.printer.transition("verify", state, verifier.summarise(results))
        self.store.save(state, note="verify")
        return state

    def review(self, state: HarnessState) -> HarnessState:
        state.status = Status.REVIEWING
        state.worker_in_flight = "codex_review"
        self.store.save(state, note="review in flight")
        try:
            review_input = self.git.review_diff(state.owned_paths)
        except GitError as exc:
            review_input = ""
            self.printer.info(f"could not build review diff: {exc}")
        outcome = self.codex.review(
            diff=review_input or "(no diff available)",
            check_results=verifier.summarise(state.check_results),
            iteration=state.iteration,
        )
        state.worker_in_flight = None

        if outcome.kind == "verdict" and outcome.verdict is not None:
            state.review_status = outcome.verdict.verdict
            state.review_severity = outcome.verdict.severity
            state.review_findings = [Finding(**f.model_dump()) for f in outcome.verdict.findings]
            state.reviewed_paths = list(state.owned_paths)
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
        try:
            diff = self.git.review_diff(state.owned_paths) or self.git.diff()
        except GitError:
            diff = ""
        outcome = self.codex.escalate(
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
        state.status = Status.CHECKPOINTING

        # -- resume: a commit already succeeded, finish bookkeeping only ----
        if state.checkpoint_status == "committed":
            return self._finish_checkpoint(state)

        # -- guards --------------------------------------------------------
        if not (state.checks_passed and state.review_status == "pass"):
            state.stop_reason = StopReason.CLI_FATAL
            self.printer.info("checkpoint reached without green checks+review; stopping")
            self.store.save(state, note="checkpoint guard tripped")
            return state

        if not set(state.owned_paths).issubset(set(state.reviewed_paths)):
            state.stop_reason = StopReason.REVIEW_COVERAGE_GAP
            self.printer.info(
                f"checkpoint would commit unreviewed paths: "
                f"{sorted(set(state.owned_paths) - set(state.reviewed_paths))}"
            )
            self.store.save(state, note="review coverage gap")
            return state

        try:
            if self.git.is_detached():
                state.stop_reason = StopReason.DETACHED_HEAD
                self.store.save(state, note="detached head at checkpoint")
                return state
            self.git.require_non_default_branch()
            head = self.git.head()
        except GitError as exc:
            state.stop_reason = StopReason.CLI_FATAL
            self.printer.info(f"checkpoint git error: {exc}")
            self.store.save(state, note="checkpoint git error")
            return state

        # -- resume: intent recorded, decide whether commit already happened
        if state.checkpoint_status == "intended":
            if head == state.checkpoint_pre_head:
                pass  # safe to (re)try the commit below
            elif self._looks_like_our_checkpoint(head, state):
                state.checkpoint_result_head = head
                state.checkpoint_status = "committed"
                state.expected_head = head
                if head not in state.commits:
                    state.commits.append(head)
                self.store.save(state, note="adopted pre-crash checkpoint commit")
                return self._finish_checkpoint(state)
            else:
                state.stop_reason = StopReason.EXPECTED_HEAD_MOVED
                self.printer.info("HEAD moved and does not match our checkpoint intent")
                self.store.save(state, note="checkpoint head reconciliation failed")
                return state
        elif head != state.expected_head:
            state.stop_reason = StopReason.EXPECTED_HEAD_MOVED
            self.printer.info("HEAD moved unexpectedly before checkpoint")
            self.store.save(state, note="expected head moved at checkpoint")
            return state

        # -- attributable-content assertion -------------------------------
        try:
            changed_now = set(self.git.changed_paths())
        except GitError as exc:
            state.stop_reason = StopReason.CLI_FATAL
            self.printer.info(f"checkpoint git error: {exc}")
            return state
        if not changed_now:
            state.stop_reason = StopReason.NO_PROGRESS
            self.printer.info("checkpoint: nothing changed in the worktree; not a completion")
            self.store.save(state, note="checkpoint no-op")
            return state
        extra = sorted(changed_now - set(state.owned_paths))
        if extra:
            state.stop_reason = StopReason.UNEXPECTED_WORKTREE_STATE
            self.printer.info(f"checkpoint: unowned changes present: {extra}")
            self.store.save(state, note="unowned changes at checkpoint")
            return state
        hits = self.git.protected_hits(state.owned_paths, self.config.safety.protected_paths)
        if hits:
            state.stop_reason = StopReason.PROTECTED_PATH_MODIFIED
            self.store.save(state, note="protected path at checkpoint")
            return state

        # -- record intent, then commit ---------------------------------
        state.checkpoint_status = "intended"
        state.checkpoint_task = state.current_task
        state.checkpoint_paths = sorted(state.owned_paths)
        state.checkpoint_pre_head = head
        state.checkpoint_result_head = None
        self.store.save(state, note="checkpoint intent recorded")  # <-- crash boundary (pre-commit)

        if not self.config.git.auto_commit:
            state.checkpoint_status = "committed"  # completion without a commit
            self.store.save(state, note="checkpoint (auto_commit off)")
            return self._finish_checkpoint(state)

        msg = f"harness: {state.current_task} [iter {state.iteration}] [run {state.run_id}]"
        try:
            sha = self.git.checkpoint(
                msg, pathspec=state.checkpoint_paths, protected=self.config.safety.protected_paths
            )
        except GitError as exc:
            state.stop_reason = StopReason.CLI_FATAL
            self.printer.info(f"checkpoint commit failed: {exc}")
            self.store.save(state, note="checkpoint commit failed")
            return state
        # <-- crash boundary (post-commit, pre-persist)
        state.checkpoint_result_head = sha
        state.checkpoint_status = "committed"
        state.expected_head = sha
        state.commits.append(sha)
        self.store.save(state, note="checkpoint committed")
        return self._finish_checkpoint(state)

    def _finish_checkpoint(self, state: HarnessState) -> HarnessState:
        task = state.checkpoint_task or state.current_task
        if task and task not in state.completed_tasks:
            state.completed_tasks.append(task)
        if state.current_task == task:
            state.current_task = None
        self._reset_task_scoped(state)
        state.checkpoint_status = "none"
        state.checkpoint_task = None
        state.checkpoint_paths = []
        state.checkpoint_pre_head = None
        state.checkpoint_result_head = None
        state.last_completed_node = "checkpoint"

        if not state.remaining_tasks:
            state.stop_reason = StopReason.SUCCESS
            state.status = Status.DONE
            state.next_node = "end"
        else:
            state.next_node = "plan"

        self.printer.transition(
            "checkpoint", state,
            f"commit={state.commits[-1][:9] if state.commits else 'none'} -> {state.next_node}",
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
        state.checks_passed = None
        state.review_status = None
        state.review_severity = None
        state.review_findings = []
        state.review_error = None
        state.reviewed_paths = []
        state.owned_paths = []
        state.pre_writer_paths = []
        state.pre_writer_hashes = {}
        state.root_cause = None

    def _looks_like_our_checkpoint(self, head: str, state: HarnessState) -> bool:
        try:
            parents = self.git.commit_parents(head)
            msg = self.git.commit_message(head)
        except GitError:
            return False
        return (
            state.checkpoint_pre_head in parents
            and f"[run {state.run_id}]" in msg
            and "harness:" in msg
        )

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
