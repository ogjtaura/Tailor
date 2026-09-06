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

from pydantic import BaseModel, ConfigDict, Field


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
    NO_PROGRESS = "NO_PROGRESS"
    STAGNATION_UNRESOLVED = "STAGNATION_UNRESOLVED"
    PLANNING_FAILED = "PLANNING_FAILED"
    REVIEWER_ERROR = "REVIEWER_ERROR"
    REVIEW_COVERAGE_GAP = "REVIEW_COVERAGE_GAP"
    PROTECTED_PATH_MODIFIED = "PROTECTED_PATH_MODIFIED"
    DIRTY_WORKTREE = "DIRTY_WORKTREE"
    UNEXPECTED_WORKTREE_STATE = "UNEXPECTED_WORKTREE_STATE"
    EXPECTED_HEAD_MOVED = "EXPECTED_HEAD_MOVED"
    DETACHED_HEAD = "DETACHED_HEAD"
    CONFIG_INVALID = "CONFIG_INVALID"
    UNSAFE_RESUME_STATE = "UNSAFE_RESUME_STATE"
    STATE_CORRUPT = "STATE_CORRUPT"
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

# Nodes that a persisted ``next_node`` is allowed to name. Any other value on
# resume is treated as an unsafe state.
RESUMABLE_NODES: frozenset[str] = frozenset(
    {"plan", "implement", "verify", "review", "decide", "repair", "escalation_review", "checkpoint", "end"}
)

CheckpointStatus = Literal["none", "intended", "committed"]


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
    model_config = ConfigDict(extra="forbid")

    objective: str
    run_id: str
    started_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)

    # task tracking
    current_task: Optional[str] = None
    remaining_tasks: list[str] = Field(default_factory=list)
    completed_tasks: list[str] = Field(default_factory=list)

    # loop counters
    iteration: int = 0                    # ++ when an implement OR repair worker starts
    repair_attempts: int = 0             # per current task; reset on new task / checkpoint
    no_progress_count: int = 0           # writer iterations that produced no attributable change

    # verification
    check_results: list[CheckResult] = Field(default_factory=list)
    checks_passed: Optional[bool] = None

    # review — schema-valid + semantically-valid verdict only
    review_status: Optional[Literal["pending", "pass", "fail"]] = None
    review_severity: Optional[Literal["none", "minor", "major", "critical"]] = None
    review_findings: list[Finding] = Field(default_factory=list)
    reviewed_paths: list[str] = Field(default_factory=list)
    # review — infrastructure failure (never routed to Claude repair)
    review_error: Optional[str] = None
    review_error_retries: int = 0

    # stagnation
    last_diff_hash: Optional[str] = None
    stagnant_iterations: int = 0
    last_failure_fingerprint: Optional[str] = None
    repeated_failure_count: int = 0
    stagnation_escalated: bool = False

    # worktree provenance / git ownership
    baseline_head: Optional[str] = None   # HEAD at fresh bootstrap (immutable)
    expected_head: Optional[str] = None   # moves forward with each harness commit
    owned_paths: list[str] = Field(default_factory=list)  # this task's attributable changes
    pre_writer_paths: list[str] = Field(default_factory=list)
    pre_writer_hashes: dict[str, Optional[str]] = Field(default_factory=dict)

    # checkpoint intent (crash-safe, idempotent commit)
    checkpoint_status: CheckpointStatus = "none"
    checkpoint_task: Optional[str] = None
    checkpoint_paths: list[str] = Field(default_factory=list)
    checkpoint_pre_head: Optional[str] = None
    checkpoint_result_head: Optional[str] = None

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

    def add_owned(self, paths) -> None:
        self.owned_paths = sorted(set(self.owned_paths) | set(paths))

    def failing_checks(self) -> list[CheckResult]:
        return [c for c in self.check_results if not c.passed]

    def is_write_worker_in_flight(self) -> bool:
        return self.worker_in_flight in WRITE_CAPABLE_WORKERS


_WS_RE = re.compile(r"\s+")


def _norm_lines(text: str, n: int = 5) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(_WS_RE.sub(" ", ln) for ln in lines[:n])


def failure_fingerprint(state: HarnessState) -> str:
    """Stable hash of *what* is currently failing (failing check commands +
    first normalised lines of their output + sorted review finding ids)."""
    parts: list[str] = []
    for chk in sorted(state.failing_checks(), key=lambda c: c.command):
        parts.append("CMD " + chk.command)
        parts.append(_norm_lines(chk.stderr_tail or chk.stdout_tail))
    for fid in sorted(f.id for f in state.review_findings):
        parts.append("FINDING " + fid)
    blob = "\n--\n".join(parts).encode("utf-8", "replace")
    return hashlib.sha256(blob).hexdigest()


def update_stagnation(state: HarnessState, *, new_diff_hash: Optional[str]) -> None:
    """Recompute stagnation counters after a verify pass. Call BEFORE writing
    ``new_diff_hash`` onto the state elsewhere."""
    fp = failure_fingerprint(state)
    still_failing = bool(state.failing_checks()) or state.review_status == "fail"

    if not still_failing:
        state.repeated_failure_count = 0
        state.stagnant_iterations = 0
        state.stagnation_escalated = False
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
        state.stagnation_escalated = False  # progress was made; reset the escalation latch

    state.last_failure_fingerprint = fp
    state.last_diff_hash = new_diff_hash


def is_stagnant(state: HarnessState, *, max_stagnant: int) -> bool:
    return state.stagnant_iterations >= max_stagnant
