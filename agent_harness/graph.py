"""The LangGraph orchestration graph.

The loop is expressed entirely in graph edges - nodes never recurse into
themselves. ``decide`` is the single router; every other transition is either
unconditional or a thin "stop if a fatal condition was set, else continue" gate.

    START -> bootstrap -> plan -> implement -> verify -> decide -> ...
             repair / escalation_review / review / checkpoint hang off decide
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from langgraph.graph import END, START, StateGraph

from agent_harness.config import Config
from agent_harness.git_tools import GitError, GitTools
from agent_harness.persistence import Persistence
from agent_harness import verifier
from agent_harness.state import (
    Finding,
    HarnessState,
    Status,
    StopReason,
    failure_fingerprint,
    is_stagnant,
    update_stagnation,
)
from agent_harness.workers.claude import ClaudeWorker, PlanningError
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


@dataclass
class EngineDeps:
    claude: ClaudeWorker
    codex: CodexWorker
    git: GitTools
    store: Persistence
    printer: Printer


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
        g.add_node("bootstrap", self.bootstrap)
        g.add_node("plan", self.plan)
        g.add_node("implement", self.implement)
        g.add_node("verify", self.verify)
        g.add_node("review", self.review)
        g.add_node("decide", self.decide)
        g.add_node("repair", self.repair)
        g.add_node("escalation_review", self.escalation_review)
        g.add_node("checkpoint", self.checkpoint)

        g.add_edge(START, "bootstrap")
        resume_map = {
            "plan": "plan",
            "implement": "implement",
            "verify": "verify",
            "review": "review",
            "repair": "repair",
            "decide": "decide",
            "end": END,
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
        limit = max(50, self.config.agent.max_iterations * 8)
        result = app.invoke(state, {"recursion_limit": limit})
        final = result if isinstance(result, HarnessState) else HarnessState.model_validate(result)
        self.store.save(final, note="run finished")
        return final

    # -- routers ---------------------------------------------------------

    # NOTE: LangGraph does not persist mutations made inside conditional-edge
    # functions - only node return values are written back. Every routing
    # decision (and any stop_reason it implies) is therefore made in a NODE;
    # these functions only *read* the decision the node already recorded.

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
        """Pure-ish decision logic, called from the ``decide`` NODE so that any
        ``stop_reason`` it sets is persisted. Returns an edge key."""
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
                return "escalate"
            return "repair"

        # Deterministic checks passed.
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
            self.git.ensure_repo()
            if self.config.git.branch_required:
                self.git.require_non_default_branch()
        except GitError as exc:
            state.stop_reason = StopReason.CLI_FATAL
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

        log_dir = self.store.run_log_dir(state)
        self.claude.log_dir = Path(log_dir)
        self.codex.log_dir = Path(log_dir)
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
            state.repair_attempts = 0

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
        state.status = Status.IMPLEMENTING
        state.iteration += 1
        state.worker_in_flight = "claude_implement"
        self.store.save(state, note="implement in flight")

        inv = self.claude.implement(
            objective=state.objective,
            task=state.current_task or "",
            repo_context=self._repo_context(),
            iteration=state.iteration,
        )
        state.worker_in_flight = None
        self._absorb_worker_changes(state, inv.result.classification, inv.result.timed_out, inv.result.exit_code)
        state.last_completed_node = "implement"
        self.printer.transition("implement", state, f"claude exit={inv.result.exit_code}")
        self.store.save(state, note="implement done")
        return state

    def verify(self, state: HarnessState) -> HarnessState:
        state.status = Status.VERIFYING
        # A fresh implementation/repair invalidates any prior review.
        state.review_status = None
        state.review_severity = None
        state.review_findings = []
        state.review_error = None
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
        outcome = self.codex.review(
            diff=self._safe_diff(),
            check_results=verifier.summarise(state.check_results),
            iteration=state.iteration,
        )
        state.worker_in_flight = None

        if outcome.kind == "verdict" and outcome.verdict is not None:
            state.review_status = outcome.verdict.verdict
            state.review_severity = outcome.verdict.severity
            state.review_findings = [Finding(**f.model_dump()) for f in outcome.verdict.findings]
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
        self.printer.transition(
            "decide",
            state,
            f"checks={state.checks_passed} review={state.review_status} "
            f"repairs={state.repair_attempts} stagnant={state.stagnant_iterations} -> {route}",
        )
        self.store.save(state, note=f"decide -> {route}")
        return state

    def repair(self, state: HarnessState) -> HarnessState:
        state.status = Status.IMPLEMENTING
        state.iteration += 1
        state.repair_attempts += 1
        state.worker_in_flight = "claude_repair"
        self.store.save(state, note="repair in flight")

        inv = self.claude.repair(
            objective=state.objective,
            task=state.current_task or "",
            failures=self._failure_brief(state),
            root_cause=state.root_cause or "",
            iteration=state.iteration,
        )
        state.worker_in_flight = None
        state.root_cause = None
        self._absorb_worker_changes(state, inv.result.classification, inv.result.timed_out, inv.result.exit_code)
        state.last_completed_node = "repair"
        self.printer.transition("repair", state, f"attempt={state.repair_attempts}")
        self.store.save(state, note="repair done")
        return state

    def escalation_review(self, state: HarnessState) -> HarnessState:
        state.status = Status.ESCALATING
        state.worker_in_flight = "codex_escalate"
        self.store.save(state, note="escalation in flight")
        root_cause = self.codex.escalate(
            diff=self._safe_diff(),
            failures=self._failure_brief(state),
            iteration=state.iteration,
        )
        state.worker_in_flight = None
        state.root_cause = root_cause
        state.last_completed_node = "escalation_review"
        self.printer.transition(
            "escalation_review", state, "root-cause obtained" if root_cause else "no root-cause"
        )
        self.store.save(state, note="escalation")
        return state

    def checkpoint(self, state: HarnessState) -> HarnessState:
        state.status = Status.CHECKPOINTING
        if not (state.checks_passed and state.review_status == "pass"):
            state.stop_reason = StopReason.CLI_FATAL
            self.printer.info("checkpoint reached without green checks+review; stopping")
            self.store.save(state, note="checkpoint guard tripped")
            return state

        try:
            changed = self.git.changed_paths()
        except GitError as exc:
            state.stop_reason = StopReason.CLI_FATAL
            self.printer.info(f"checkpoint git error: {exc}")
            self.store.save(state, note="checkpoint git error")
            return state

        unknown = sorted(set(changed) - set(state.harness_touched_files))
        if unknown:
            state.stop_reason = StopReason.UNEXPECTED_WORKTREE_STATE
            self.printer.info(f"unexpected changes not attributable to the harness: {unknown}")
            self.store.save(state, note="unexpected worktree state")
            return state

        hits = self.git.protected_hits(changed, self.config.safety.protected_paths)
        if hits:
            state.stop_reason = StopReason.PROTECTED_PATH_MODIFIED
            self.printer.info(f"protected paths modified: {hits}")
            self.store.save(state, note="protected path modified")
            return state

        if self.config.git.auto_commit and changed:
            msg = f"harness: {state.current_task} [iter {state.iteration}]"
            try:
                sha = self.git.checkpoint(
                    msg, pathspec=changed, protected=self.config.safety.protected_paths
                )
                state.commits.append(sha)
            except GitError as exc:
                state.stop_reason = StopReason.CLI_FATAL
                self.printer.info(f"checkpoint commit failed: {exc}")
                self.store.save(state, note="checkpoint commit failed")
                return state

        if state.current_task is not None:
            state.completed_tasks.append(state.current_task)
        state.current_task = None
        state.repair_attempts = 0
        state.review_error_retries = 0
        state.stagnant_iterations = 0
        state.repeated_failure_count = 0
        state.last_failure_fingerprint = None
        state.checks_passed = None
        state.review_status = None
        state.review_severity = None
        state.review_findings = []
        state.harness_touched_files = []
        state.last_completed_node = "checkpoint"

        if not state.remaining_tasks:
            state.stop_reason = StopReason.SUCCESS
            state.status = Status.DONE
            state.next_node = "end"
        else:
            state.next_node = "plan"

        self.printer.transition(
            "checkpoint",
            state,
            f"commit={state.commits[-1][:9] if state.commits else 'none'} -> {state.next_node}",
        )
        self.store.save(state, note="checkpoint")
        return state

    # -- helpers -------------------------------------------------------------

    def _absorb_worker_changes(
        self, state: HarnessState, classification: str, timed_out: bool, exit_code: Optional[int]
    ) -> None:
        try:
            changed = self.git.changed_paths()
        except GitError:
            changed = []
        state.record_touched(changed)

        hits = self.git.protected_hits(changed, self.config.safety.protected_paths)
        if hits:
            state.stop_reason = StopReason.PROTECTED_PATH_MODIFIED
            self.printer.info(f"worker modified protected paths: {hits}")
            return
        if classification == "usage_limit":
            state.stop_reason = StopReason.USAGE_LIMIT
            return
        if timed_out or (exit_code not in (0, None)):
            state.stop_reason = StopReason.CLI_FATAL

    def _failure_brief(self, state: HarnessState) -> str:
        lines: list[str] = []
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

    def _safe_diff(self) -> str:
        try:
            return self.git.diff()
        except GitError:
            return ""

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
