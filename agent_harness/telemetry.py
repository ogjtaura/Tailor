"""Lightweight engineering-agent execution telemetry.

One JSONL line per engineering-agent invocation, appended to
``.agent/runs/<run_id>/agents.jsonl``. This records *which backend/model served
which role and how it went* so future provider comparisons are possible. It is
deliberately not an analytics system, and it is entirely separate from any
Tailor product-level analytics.

Safety contract: telemetry must never compromise the pipeline. Every write is
best-effort - a failure to open/append the file is swallowed.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- durable-value contract ------------------------------------------------
# The sink is the last line of defence. It persists ONLY the keys below, ONLY
# with an explicitly supported scalar type, and forces the category fields to a
# genuine closed set. There is no generic object->string coercion: an
# unsupported value is dropped (or, for the always-present category fields,
# fails closed to a safe structural value). Nothing free-form from a backend
# outcome or a caller-supplied dict can reach agents.jsonl.

_DROP = object()
_MAX_STR = 256

# closed sets (independent copy - telemetry must not import the router)
CLASSIFICATIONS = frozenset({"ok", "cli_error", "usage_limit"})
ERROR_CODES = frozenset({"", "usage_limit", "provider_execution_failed", "invocation_error"})
ERROR_TYPES = frozenset({
    "", "PlanningError", "WorkerUsageLimitError", "ConfigError", "GitError",
    "TimeoutExpired", "RuntimeError", "ValueError", "OSError", "OtherError",
})


def _short_str(v: Any):
    return v if isinstance(v, str) and len(v) <= _MAX_STR else _DROP


def _plain_int(v: Any):
    return v if isinstance(v, int) and not isinstance(v, bool) else _DROP


def _plain_bool(v: Any):
    return v if isinstance(v, bool) else _DROP


def _number(v: Any):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else _DROP


def _exit_code(v: Any):
    return v if (v is None or (isinstance(v, int) and not isinstance(v, bool))) else _DROP


# key -> (value validator/normaliser). Any key not here is not persisted.
_FIELD_RULES = {
    "logged_at": _short_str,
    "run_id": _short_str,
    "role": _short_str,
    "backend": _short_str,
    "model": _short_str,
    "cwd": _short_str,
    "started_at": _short_str,
    "ended_at": _short_str,
    "iteration": _plain_int,
    "duration_s": _number,
    "ok": _plain_bool,
    "timed_out": _plain_bool,
    "exit_code": _exit_code,
    # always-present category fields: fail closed to a safe structural value
    "classification": lambda v: v if v in CLASSIFICATIONS else "cli_error",
    "error_code": lambda v: v if v in ERROR_CODES else "invocation_error",
    "error_type": lambda v: v if v in ERROR_TYPES else "OtherError",
}

# Retained for any external reference: the set of keys the sink may persist.
_SAFE_KEYS = frozenset(_FIELD_RULES)


class AgentTelemetry:
    """Append-only JSONL sink for engineering-agent invocations."""

    FILENAME = "agents.jsonl"

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path: Optional[Path] = Path(path) if path is not None else None

    @classmethod
    def for_run_dir(cls, run_dir: Optional[os.PathLike[str] | str]) -> "AgentTelemetry":
        if run_dir is None:
            return cls(None)
        return cls(Path(run_dir) / cls.FILENAME)

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def record(self, event: Any) -> None:
        """Append one event. ``event`` may be a dataclass or a plain dict.
        Any error here is swallowed - the safety pipeline takes precedence."""
        if self._path is None:
            return
        try:
            raw = asdict(event) if is_dataclass(event) else dict(event)
            raw.setdefault("logged_at", _utcnow_iso())
            # Allowlist keys AND enforce a per-key value contract. No default=str:
            # only explicitly supported scalars are persisted; category fields
            # fail closed to a safe structural value.
            payload = {}
            for key, rule in _FIELD_RULES.items():
                if key not in raw:
                    continue
                value = rule(raw[key])
                if value is _DROP:
                    continue
                payload[key] = value
            line = json.dumps(payload, sort_keys=True)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except Exception:  # never let telemetry break the run
            pass

    def read_all(self) -> list[dict]:
        """Best-effort read of every recorded event (diagnostics / tests)."""
        if self._path is None or not self._path.is_file():
            return []
        out: list[dict] = []
        for raw in self._path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return out
