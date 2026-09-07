"""Engineering-agent routing: ROLE -> BACKEND -> MODEL.

The orchestration graph talks only to :class:`AgentRouter`. It asks for a *role*
(``plan`` / ``implement`` / ``repair`` / ``routine_review`` / ``escalation_review``)
and the router resolves that role to a backend adapter and a model from
configuration, invokes it, records telemetry at the invocation boundary, and
returns the *same* typed result the graph already expects.

Provider-specific command construction stays inside the backend adapters
(``workers/claude.py``, ``workers/codex.py``). Nothing about ``claude`` or
``codex`` argv leaks through here.

This changes no safety semantics: the router is a thin dispatch + telemetry
seam. Verification stays deterministic and independent, the writer still cannot
review itself, and the harness still owns the checkpoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agent_harness.config import Config, ConfigError, RoleAssignment, RoleName
from agent_harness.telemetry import AgentTelemetry
from agent_harness.workers.claude import ClaudeInvocation, ClaudeWorker, PlanResult
from agent_harness.workers.codex import CodexWorker, EscalateOutcome, ReviewOutcome

# Which backend each role's adapter lives in for this version. Only the two V0
# backends exist; a config that routes a role to the other backend is refused.
_ROLE_BACKEND: dict[RoleName, str] = {
    "planner": "claude_code",
    "implementer": "claude_code",
    "repairer": "claude_code",
    "routine_reviewer": "codex",
    "escalation_reviewer": "codex",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Closed vocabulary of durable telemetry categories. Telemetry describes *what
# kind* of thing happened; it never reproduces the thing's contents, and it
# never trusts a value a backend outcome put on the wire.
CLASSIFICATION_OK = "ok"
CLASSIFICATION_CLI_ERROR = "cli_error"
CLASSIFICATION_USAGE_LIMIT = "usage_limit"
VALID_CLASSIFICATIONS = frozenset({
    CLASSIFICATION_OK, CLASSIFICATION_CLI_ERROR, CLASSIFICATION_USAGE_LIMIT
})

ERROR_CODE_NONE = ""
ERROR_CODE_USAGE_LIMIT = "usage_limit"
ERROR_CODE_PROVIDER_EXECUTION_FAILED = "provider_execution_failed"
ERROR_CODE_INVOCATION_ERROR = "invocation_error"

# error_type is a bounded category, not an arbitrary class name.
KNOWN_ERROR_TYPES = frozenset({
    "PlanningError", "WorkerUsageLimitError", "ConfigError", "GitError",
    "TimeoutExpired", "RuntimeError", "ValueError", "OSError",
})
ERROR_TYPE_OTHER = "OtherError"


def _normalize_classification(value: object) -> str:
    """Durable classification is a genuine closed set. Any value outside it -
    including arbitrary backend-supplied text - fails closed to 'cli_error'; the
    original is never persisted. A no-op for the real closed-set values."""
    return value if value in VALID_CLASSIFICATIONS else CLASSIFICATION_CLI_ERROR


def _error_type(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if name in KNOWN_ERROR_TYPES else ERROR_TYPE_OTHER


def _error_code(exc: BaseException) -> str:
    if _is_usage_limit_exc(exc):
        return ERROR_CODE_USAGE_LIMIT
    if type(exc).__name__ == "PlanningError":
        return ERROR_CODE_PROVIDER_EXECUTION_FAILED
    return ERROR_CODE_INVOCATION_ERROR


@dataclass
class AgentInvocation:
    """Normalised, allowlisted record of one engineering-agent invocation.

    Every field here is structural metadata chosen explicitly by the harness -
    role/backend/model routing, timing, and a *category* of failure. It never
    carries provider stdout/stderr, prompts, model responses, exception
    messages, or any other free-form string from a failure path.
    """

    role: str
    backend: str
    model: str
    cwd: str
    iteration: int
    started_at: str
    ended_at: str
    duration_s: float
    ok: bool
    exit_code: Optional[int] = None
    classification: str = "ok"        # closed set: ok | cli_error | usage_limit
    timed_out: bool = False
    error_type: str = ""              # bounded: KNOWN_ERROR_TYPES | "" | "OtherError"
    error_code: str = ""              # closed set: see ERROR_CODE_* above
    run_id: str = ""


class AgentRouter:
    """Resolves engineering roles to (backend, model) and invokes them."""

    def __init__(
        self,
        config: Config,
        *,
        claude: Optional[ClaudeWorker] = None,
        codex: Optional[CodexWorker] = None,
        telemetry: Optional[AgentTelemetry] = None,
        log_dir: Optional[Path] = None,
        run_id: str = "",
    ) -> None:
        self.config = config
        self._claude = claude if claude is not None else ClaudeWorker(config)
        self._codex = codex if codex is not None else CodexWorker(config)
        self._telemetry = telemetry or AgentTelemetry.for_run_dir(log_dir)
        self._run_id = run_id
        self._assignments: dict[RoleName, RoleAssignment] = {}
        for role, expected_backend in _ROLE_BACKEND.items():
            a = config.resolve_role(role)
            if a.backend != expected_backend:
                raise ConfigError(
                    f"role {role!r} is assigned to backend {a.backend!r}, but this "
                    f"version only implements it on {expected_backend!r}"
                )
            self._assignments[role] = a
        if log_dir is not None:
            self._bind_workers(Path(log_dir))

    # -- run binding ----------------------------------------------------------

    def bind_run(self, run_id: str, log_dir: Path) -> None:
        """Point telemetry + worker logs at this run's log directory."""
        self._run_id = run_id
        self._telemetry = AgentTelemetry.for_run_dir(log_dir)
        self._bind_workers(Path(log_dir))

    def _bind_workers(self, log_dir: Path) -> None:
        self._claude.log_dir = log_dir
        self._codex.log_dir = log_dir

    # -- introspection ------------------------------------------------------

    @property
    def assignments(self) -> dict[str, RoleAssignment]:
        return dict(self._assignments)

    def assignment(self, role: RoleName) -> RoleAssignment:
        return self._assignments[role]

    @property
    def telemetry(self) -> AgentTelemetry:
        return self._telemetry

    def describe(self) -> list[str]:
        return [
            f"{role:<20} {a.backend} / {a.model}"
            for role, a in self._assignments.items()
        ]

    # -- role entry points (same signatures the graph already used) --------

    def plan(self, *, iteration: int = 0, **kw) -> PlanResult:
        return self._invoke(
            "planner", iteration, lambda m: self._claude.plan(iteration=iteration, model=m, **kw)
        )

    def implement(self, *, iteration: int = 0, **kw) -> ClaudeInvocation:
        return self._invoke(
            "implementer", iteration,
            lambda m: self._claude.implement(iteration=iteration, model=m, **kw),
        )

    def repair(self, *, iteration: int = 0, **kw) -> ClaudeInvocation:
        return self._invoke(
            "repairer", iteration,
            lambda m: self._claude.repair(iteration=iteration, model=m, **kw),
        )

    def routine_review(self, *, iteration: int = 0, **kw) -> ReviewOutcome:
        return self._invoke(
            "routine_reviewer", iteration,
            lambda m: self._codex.review(iteration=iteration, model=m, **kw),
        )

    def escalation_review(self, *, iteration: int = 0, **kw) -> EscalateOutcome:
        return self._invoke(
            "escalation_reviewer", iteration,
            lambda m: self._codex.escalate(iteration=iteration, model=m, **kw),
        )

    # -- invocation boundary ---------------------------------------------

    def _invoke(self, role: RoleName, iteration: int, call: Callable[[str], Any]) -> Any:
        a = self._assignments[role]
        started_at = _utcnow_iso()
        t0 = time.monotonic()
        error_type = error_code = ""
        outcome: Any = None
        try:
            outcome = call(a.model)
            ok, exit_code, classification, timed_out = _status_of(outcome)
        except BaseException as exc:  # record STRUCTURE only, then re-raise unchanged
            ok, exit_code, timed_out = False, None, False
            classification = (
                CLASSIFICATION_USAGE_LIMIT if _is_usage_limit_exc(exc)
                else CLASSIFICATION_CLI_ERROR
            )
            # NEVER str(exc): a provider error message can contain stderr / secrets.
            # error_type is a bounded category, not an arbitrary class name.
            error_type = _error_type(exc)
            error_code = _error_code(exc)
            self._record(role, a, iteration, started_at, t0, ok, exit_code,
                         classification, timed_out, error_type, error_code)
            raise
        self._record(role, a, iteration, started_at, t0, ok, exit_code,
                     classification, timed_out, error_type, error_code)
        return outcome

    def _record(self, role, a, iteration, started_at, t0, ok, exit_code,
                classification, timed_out, error_type, error_code) -> None:
        self._telemetry.record(AgentInvocation(
            role=role,
            backend=a.backend,
            model=a.model,
            cwd=str(self.config.repo_root),
            iteration=int(iteration),
            started_at=started_at,
            ended_at=_utcnow_iso(),
            duration_s=round(time.monotonic() - t0, 4),
            ok=bool(ok),
            exit_code=exit_code,
            # closed set at the router boundary; the sink re-checks (defence in depth)
            classification=_normalize_classification(classification),
            timed_out=bool(timed_out),
            error_type=error_type,
            error_code=error_code,
            run_id=self._run_id,
        ))


def _status_of(outcome: Any):
    """(ok, exit_code, classification, timed_out) from any worker result type.
    ``classification`` is normalised to the closed set here - a backend outcome
    never puts arbitrary text on the durable path."""
    if isinstance(outcome, PlanResult):
        return True, 0, CLASSIFICATION_OK, False
    if isinstance(outcome, ClaudeInvocation):
        r = outcome.result
        return (outcome.ok, r.exit_code,
                _normalize_classification(outcome.classification), r.timed_out)
    if isinstance(outcome, ReviewOutcome):
        r = outcome.result
        return (
            outcome.kind == "verdict",
            r.exit_code if r is not None else None,
            _normalize_classification(outcome.classification),
            bool(r.timed_out) if r is not None else False,
        )
    if isinstance(outcome, EscalateOutcome):
        return (outcome.root_cause is not None, None,
                _normalize_classification(outcome.classification), False)
    # Unknown (e.g. a test double) - assume it ran.
    return True, None, CLASSIFICATION_OK, False


def _is_usage_limit_exc(exc: BaseException) -> bool:
    if getattr(exc, "is_usage_limit", False):
        return True
    return type(exc).__name__ == "WorkerUsageLimitError"
