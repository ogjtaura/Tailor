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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_harness.git_tools import is_protected_target_ref


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
    REVIEW_INVALIDATED = "REVIEW_INVALIDATED"
    REVIEW_PAYLOAD_ERROR = "REVIEW_PAYLOAD_ERROR"
    CANDIDATE_INVALID = "CANDIDATE_INVALID"
    INVALID_CHECKPOINT_STATE = "INVALID_CHECKPOINT_STATE"
    PROTECTED_BRANCH = "PROTECTED_BRANCH"
    RUN_AUTHORIZATION_MISSING = "RUN_AUTHORIZATION_MISSING"
    RUN_AUTHORIZATION_INVALID = "RUN_AUTHORIZATION_INVALID"
    REF_UPDATE_CONFLICT = "REF_UPDATE_CONFLICT"
    INTERRUPTED_WRITE_UNATTRIBUTABLE = "INTERRUPTED_WRITE_UNATTRIBUTABLE"
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

# The single source of truth for where a resumed run may re-enter the graph.
# ``graph.Engine.build`` derives its bootstrap resume edge-map from these keys,
# and ``reconcile_for_resume`` may only ever set ``next_node`` to one of them;
# anything else fails closed (UNSAFE_RESUME_STATE). ``escalation_review`` is
# deliberately absent: a crash inside it reconciles to ``repair`` (read-only
# worker) or, if it completed, ``_AFTER_NODE`` maps it to ``repair`` too.
RESUMABLE_NODES: tuple[str, ...] = (
    "plan", "implement", "verify", "review", "repair", "decide",
    "freeze", "checkpoint", "end",
)

# Immutable-object checkpoint boundary. Each phase names exactly what has been
# frozen and what has NOT yet been accepted onto the feature branch.
#   none            - nothing frozen
#   candidate_frozen- candidate_tree_oid exists as a Git object; the deterministic
#                     commit spec (parent, message, author, committer) is fully
#                     persisted; candidate_commit_oid MAY or MAY NOT yet exist.
#                     A crash here reconstructs the commit FROM candidate_tree_oid
#                     + the persisted spec - never from the mutable working tree.
#                     The feature-branch ref is UNCHANGED.
#   verified        - the configured checks passed against candidate_commit_oid;
#                     verified_tree_oid == candidate_tree_oid AND
#                     verified_commit_oid == candidate_commit_oid
#   reviewed        - Codex returned a schema-valid PASS on that exact object;
#                     reviewed_tree_oid == candidate_tree_oid AND
#                     reviewed_commit_oid == candidate_commit_oid
#   ref_update_intent - about to CAS the feature-branch ref
#   ref_updated     - CAS succeeded: <target_ref> now == candidate_commit_oid
CheckpointPhase = Literal[
    "none", "candidate_frozen", "verified", "reviewed", "ref_update_intent", "ref_updated"
]

# Phases in which a deterministic candidate commit spec must already be persisted
# in full (so a tree->commit crash is recoverable without the working tree).
CANDIDATE_ACTIVE_PHASES: frozenset[str] = frozenset(
    {"candidate_frozen", "verified", "reviewed", "ref_update_intent", "ref_updated"}
)


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
    # review — infrastructure failure (never routed to Claude repair)
    review_error: Optional[str] = None
    review_error_retries: int = 0

    # stagnation
    last_candidate_tree_oid: Optional[str] = None
    stagnant_iterations: int = 0
    last_failure_fingerprint: Optional[str] = None
    repeated_failure_count: int = 0
    stagnation_escalated: bool = False

    # ---- run-level write authorization (established once at fresh bootstrap) --
    # The ONE feature ref this run is allowed to advance. It is set from the
    # actual checked-out branch at fresh bootstrap, mirrored into
    # ``.agent/runs/<run_id>/manifest.json``, and never re-derived afterwards
    # from HEAD, checkpoint state, candidate metadata, or expected_parent.
    # ``checkpoint_target_ref`` must always equal this once a candidate exists.
    run_target_ref: Optional[str] = None

    # git ownership / provenance of the working-tree changes
    baseline_head: Optional[str] = None   # HEAD commit at fresh bootstrap (immutable)
    expected_head: Optional[str] = None   # feature-branch tip the harness expects
    owned_paths: list[str] = Field(default_factory=list)  # this task's attributable changes
    pre_writer_paths: list[str] = Field(default_factory=list)
    pre_writer_hashes: dict[str, Optional[str]] = Field(default_factory=dict)

    # ---- immutable-object checkpoint boundary -------------------------------
    checkpoint_phase: CheckpointPhase = "none"
    checkpoint_task: Optional[str] = None
    checkpoint_paths: list[str] = Field(default_factory=list)   # owned pathspec frozen
    checkpoint_target_ref: Optional[str] = None                 # refs/heads/<feature>
    checkpoint_expected_parent: Optional[str] = None            # commit OID the ref must equal
    checkpoint_message: Optional[str] = None
    # deterministic commit identity (persisted BEFORE commit-tree so a retry
    # reproduces the same commit OID)
    checkpoint_author: Optional[list[str]] = None     # [name, email, git-date]
    checkpoint_committer: Optional[list[str]] = None
    candidate_tree_oid: Optional[str] = None
    candidate_commit_oid: Optional[str] = None
    # verify / review evidence - bound to BOTH the tree AND the commit OID so a
    # PASS from candidate A can never authorise a different candidate B.
    verified_tree_oid: Optional[str] = None
    verified_commit_oid: Optional[str] = None
    reviewed_tree_oid: Optional[str] = None
    reviewed_commit_oid: Optional[str] = None

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

    # ---- cross-field invariants (fail closed on impossible persisted state) --

    @model_validator(mode="after")
    def _check_checkpoint_invariants(self) -> "HarnessState":
        """Reject persisted state that can only be the result of corruption or a
        partial write we cannot safely interpret. We never *repair* it - a load
        that trips this raises and the state file is left untouched."""
        errors: list[str] = []

        if self.candidate_commit_oid and not self.candidate_tree_oid:
            errors.append("candidate_commit_oid set but candidate_tree_oid missing")
        if self.verified_commit_oid and not self.candidate_commit_oid:
            errors.append("verified_commit_oid set but candidate_commit_oid missing")
        if self.reviewed_commit_oid and not self.candidate_commit_oid:
            errors.append("reviewed_commit_oid set but candidate_commit_oid missing")
        if self.verified_tree_oid and self.candidate_tree_oid and \
                self.verified_tree_oid != self.candidate_tree_oid:
            errors.append("verified_tree_oid != candidate_tree_oid")
        if self.reviewed_tree_oid and self.candidate_tree_oid and \
                self.reviewed_tree_oid != self.candidate_tree_oid:
            errors.append("reviewed_tree_oid != candidate_tree_oid")
        if self.verified_commit_oid and self.candidate_commit_oid and \
                self.verified_commit_oid != self.candidate_commit_oid:
            errors.append("verified_commit_oid != candidate_commit_oid")
        if self.reviewed_commit_oid and self.candidate_commit_oid and \
                self.reviewed_commit_oid != self.candidate_commit_oid:
            errors.append("reviewed_commit_oid != candidate_commit_oid")

        if self.checkpoint_phase in ("verified", "reviewed", "ref_update_intent", "ref_updated"):
            if not self.candidate_commit_oid:
                errors.append(f"phase {self.checkpoint_phase!r} with no candidate_commit_oid")
        if self.checkpoint_phase in ("reviewed", "ref_update_intent", "ref_updated"):
            if self.reviewed_commit_oid != self.candidate_commit_oid:
                errors.append(f"phase {self.checkpoint_phase!r} without a bound review PASS")

        # -- run-target authorization consistency (pure string checks only) ---
        if is_protected_target_ref(self.run_target_ref):
            errors.append(f"run_target_ref {self.run_target_ref!r} is main/master")
        if (
            self.run_target_ref
            and self.checkpoint_target_ref
            and self.run_target_ref != self.checkpoint_target_ref
        ):
            errors.append(
                f"checkpoint_target_ref {self.checkpoint_target_ref!r} != "
                f"authorized run_target_ref {self.run_target_ref!r}"
            )
        if self.checkpoint_phase in CANDIDATE_ACTIVE_PHASES:
            if is_protected_target_ref(self.checkpoint_target_ref):
                errors.append(
                    f"phase {self.checkpoint_phase!r} with protected "
                    f"checkpoint_target_ref {self.checkpoint_target_ref!r}"
                )

        if errors:
            raise ValueError("; ".join(errors))
        return self


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


def update_stagnation(state: HarnessState, *, candidate_tree_oid: Optional[str]) -> None:
    """Recompute stagnation counters after a verify pass. ``candidate_tree_oid``
    is the frozen candidate's Git tree OID - stagnation accrues only while the
    same failure survives against an unchanged candidate tree."""
    fp = failure_fingerprint(state)
    still_failing = bool(state.failing_checks()) or state.review_status == "fail"

    if not still_failing:
        state.repeated_failure_count = 0
        state.stagnant_iterations = 0
        state.stagnation_escalated = False
        state.last_failure_fingerprint = fp
        state.last_candidate_tree_oid = candidate_tree_oid
        return

    same_failure = fp == state.last_failure_fingerprint
    same_diff = candidate_tree_oid == state.last_candidate_tree_oid
    if same_failure and same_diff:
        state.repeated_failure_count += 1
        state.stagnant_iterations += 1
    else:
        state.repeated_failure_count = 1
        state.stagnant_iterations = 0
        state.stagnation_escalated = False  # progress was made; reset the escalation latch

    state.last_failure_fingerprint = fp
    state.last_candidate_tree_oid = candidate_tree_oid


def is_stagnant(state: HarnessState, *, max_stagnant: int) -> bool:
    return state.stagnant_iterations >= max_stagnant


def clear_candidate_evidence(state: HarnessState) -> None:
    """Single point of truth for invalidating a candidate's identity + evidence.

    Called whenever the writer is about to produce a NEW candidate (a fresh
    freeze after implement / repair). A verify/review PASS is bound to an exact
    (tree OID, commit OID) pair; once the candidate changes, none of that
    evidence may survive - a PASS from candidate A must never authorise B.

    The deterministic commit *spec* fields (target ref, expected parent, message,
    author, committer) are left in place; ``freeze`` overwrites them wholesale
    and the branch-switch guard still needs the previous target ref.
    """
    state.candidate_tree_oid = None
    state.candidate_commit_oid = None
    state.verified_tree_oid = None
    state.verified_commit_oid = None
    state.reviewed_tree_oid = None
    state.reviewed_commit_oid = None
    state.review_status = None
    state.review_severity = None
    state.review_findings = []
    state.review_error = None
    state.checks_passed = None
    state.checkpoint_phase = "none"
