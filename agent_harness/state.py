"""Typed, serialisable graph state for the harness.

Everything the LangGraph loop reads or writes lives on :class:`HarnessState`.
It round-trips through JSON (``model_dump(mode="json")`` / ``model_validate``)
so a crash can be recovered from ``.agent/state.json``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Status(str, Enum):
    BOOTSTRAP = "BOOTSTRAP"
    PLANNING = "PLANNING"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    DECIDING = "DECIDING"
    ESCALATING = "ESCALATING"
    CHECKPOINTING = "CHECKPOINTING"
    DONE = "DONE"
    STOPPED = "STOPPED"


class StopReason(str, Enum):
    SUCCESS = "SUCCESS"
    ITERATION_LIMIT = "ITERATION_LIMIT"
    REPAIR_LIMIT = "REPAIR_LIMIT"
    STAGNATION_UNRESOLVED = "STAGNATION_UNRESOLVED"
    PLANNING_FAILED = "PLANNING_FAILED"
    REVIEWER_ERROR = "REVIEWER_ERROR"
    PROTECTED_PATH_MODIFIED = "PROTECTED_PATH_MODIFIED"
    DIRTY_WORKTREE = "DIRTY_WORKTREE"
    UNEXPECTED_WORKTREE_STATE = "UNEXPECTED_WORKTREE_STATE"
    CLI_FATAL = "CLI_FATAL"
    USAGE_LIMIT = "USAGE_LIMIT"
    USER_ABORT = "USER_ABORT"


WorkerInFlight = Literal[
    "claude_plan",
    "claude_implement",
    "claude_repair",
    "codex_review",
    "codex_escalate",
]

WRITE_CAPABLE_WORKERS: frozenset[str] = frozenset({"claude_implement", "claude_repair"})


class CheckResult(BaseModel):
    command: str
    exit_code: int
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_s: float = 0.0

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


class Finding(BaseModel):
    id: str
    description: str = ""
    evidence: str = ""
    suggested_fix: str = ""


class HarnessState(BaseModel):
    objective: str
    run_id: str
    started_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)

    # task tracking
    current_task: Optional[str] = None
    remaining_tasks: list[str] = Field(default_factory=list)
    completed_tasks: list[str] = Field(default_factory=list)

    # loop counters
    iteration: int = 0                     # ++ when an implement OR repair worker starts
    repair_attempts: int = 0              # per current task; reset on new task / checkpoint

    # verification
    check_results: list[CheckResult] = Field(default_factory=list)
    checks_passed: Optional[bool] = None

    # review — schema-valid verdict only
    review_status: Optional[Literal["pending", "pass", "fail"]] = None
    review_severity: Optional[Literal["none", "minor", "major", "critical"]] = None
    review_findings: list[Finding] = Field(default_factory=list)
    # review — infrastructure failure (never routed to Claude repair)
    review_error: Optional[str] = None
    review_error_retries: int = 0

    # stagnation
    last_diff_hash: Optional[str] = None
    stagnant_iterations: int = 0
    last_failure_fingerprint: Optional[str] = None
    repeated_failure_count: int = 0

    # worktree provenance
    baseline_head: Optional[str] = None
    harness_touched_files: list[str] = Field(default_factory=list)

    # escalation
    root_cause: Optional[str] = None

    # crash-resume bookkeeping
    last_completed_node: Optional[str] = None
    next_node: Optional[str] = None
    worker_in_flight: Optional[WorkerInFlight] = None

    # lifecycle
    status: Status = Status.BOOTSTRAP
    stop_reason: Optional[StopReason] = None
    commits: list[str] = Field(default_factory=list)

    # ---- helpers -------------------------------------------------------------

    def touch(self) -> None:
        self.updated_at = _utcnow()

    def record_touched(self, paths: list[str]) -> None:
        merged = set(self.harness_touched_files) | set(paths)
        self.harness_touched_files = sorted(merged)

    def failing_checks(self) -> list[CheckResult]:
        return [c for c in self.check_results if not c.passed]

    def is_write_worker_in_flight(self) -> bool:
        return self.worker_in_flight in WRITE_CAPABLE_WORKERS


_WS_RE = re.compile(r"\s+")


def _norm_lines(text: str, n: int = 5) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(_WS_RE.sub(" ", ln) for ln in lines[:n])


def failure_fingerprint(state: HarnessState) -> str:
    """Stable hash of *what* is currently failing.

    Combines the sorted set of failing check commands, the first few normalised
    lines of each failing stream, and the sorted review finding ids. Used to
    detect stagnation (same failure surviving repeated repair attempts).
    """
    parts: list[str] = []
    for chk in sorted(state.failing_checks(), key=lambda c: c.command):
        parts.append("CMD " + chk.command)
        parts.append(_norm_lines(chk.stderr_tail or chk.stdout_tail))
    for fid in sorted(f.id for f in state.review_findings):
        parts.append("FINDING " + fid)
    blob = "\n--\n".join(parts).encode("utf-8", "replace")
    return hashlib.sha256(blob).hexdigest()


def update_stagnation(state: HarnessState, *, new_diff_hash: Optional[str]) -> None:
    """Recompute stagnation counters after a verify pass.

    Call this from the ``verify`` node with the freshly computed working-tree
    diff hash, BEFORE writing it onto the state. Stagnation accrues only while
    something is still failing, the failure fingerprint is unchanged, and the
    diff has not moved - i.e. repair attempts are not making a difference.
    """
    fp = failure_fingerprint(state)
    still_failing = bool(state.failing_checks()) or state.review_status == "fail"

    if not still_failing:
        state.repeated_failure_count = 0
        state.stagnant_iterations = 0
        state.last_failure_fingerprint = fp
        state.last_diff_hash = new_diff_hash
        return

    same_failure = fp == state.last_failure_fingerprint
    same_diff = new_diff_hash == state.last_diff_hash
    if same_failure and same_diff:
        state.repeated_failure_count += 1
        state.stagnant_iterations += 1
    else:
        state.repeated_failure_count = 1
        state.stagnant_iterations = 0

    state.last_failure_fingerprint = fp
    state.last_diff_hash = new_diff_hash


def is_stagnant(state: HarnessState, *, max_stagnant: int) -> bool:
    return state.stagnant_iterations >= max_stagnant
