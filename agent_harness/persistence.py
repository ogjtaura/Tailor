"""Recoverable state under ``.agent/`` (no database - JSON + text only).

Layout::

    .agent/state.json          latest HarnessState (atomic write)
    .agent/progress.md         append-only human-readable log
    .agent/failures.json       accumulated failure fingerprints + findings
    .agent/harness.lock        OS advisory lock for the single-run guard
    .agent/runs/<run_id>/*.log raw worker stdout/stderr

Writes are atomic (temp file in the same dir -> ``os.replace``) so a crash
mid-write leaves the previous valid file intact.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from agent_harness.state import (
    RESUMABLE_NODES,
    HarnessState,
    StopReason,
    failure_fingerprint,
)


class StateCorruptError(RuntimeError):
    """``.agent/state.json`` exists but is not a valid HarnessState."""


class RunLockError(RuntimeError):
    """Another harness process holds the repository-local run lock."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class RunLock:
    """Repository-local single-run lock backed by a real OS advisory lock
    (``fcntl.flock``). The kernel releases the lock if the holder dies, so a
    stale lock file never blocks a later run."""

    def __init__(self, agent_dir: Path) -> None:
        self.path = Path(agent_dir) / "harness.lock"
        self._fh = None

    def acquire(self) -> "RunLock":
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise RunLockError(
                f"another harness run is active (lock held on {self.path})"
            ) from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{os.getpid()}\n")
        self._fh.flush()
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        import fcntl

        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


class Persistence:
    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.agent_dir = self.repo_root / ".agent"
        self.state_path = self.agent_dir / "state.json"
        self.progress_path = self.agent_dir / "progress.md"
        self.failures_path = self.agent_dir / "failures.json"
        self.runs_dir = self.agent_dir / "runs"

    def lock(self) -> RunLock:
        return RunLock(self.agent_dir)

    # -- setup ----------------------------------------------------------

    def init_run(self, state: HarnessState) -> Path:
        log_dir = self.runs_dir / state.run_id
        log_dir.mkdir(parents=True, exist_ok=True)
        if not self.progress_path.exists():
            _atomic_write(
                self.progress_path,
                f"# Harness progress\n\nrun {state.run_id} - {state.objective}\n",
            )
        self.save(state, note="run started")
        return log_dir

    def run_log_dir(self, state: HarnessState) -> Path:
        d = self.runs_dir / state.run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- save / load --------------------------------------------------

    def save(self, state: HarnessState, *, note: str = "") -> None:
        state.touch()
        _atomic_write(
            self.state_path,
            json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True),
        )
        self._append_progress(state, note)
        self._write_failures(state)

    def load(self) -> Optional[HarnessState]:
        if not self.state_path.is_file():
            return None
        try:
            data = json.loads(self.state_path.read_text())
            return HarnessState.model_validate(data)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise StateCorruptError(
                f"{self.state_path} is not a valid harness state: {exc}"
            ) from exc

    # -- derived files ---------------------------------------------------

    def _append_progress(self, state: HarnessState, note: str) -> None:
        line = (
            f"- {state.updated_at} iter={state.iteration} "
            f"status={state.status.value} "
            f"task={state.current_task!r} "
            f"checks_passed={state.checks_passed} review={state.review_status} "
            f"ckpt={state.checkpoint_status} "
        )
        if note:
            line += f"| {note}"
        if state.stop_reason:
            line += f" | STOP={state.stop_reason.value}"
        with self.progress_path.open("a") as fh:
            fh.write(line.rstrip() + "\n")

    def _write_failures(self, state: HarnessState) -> None:
        payload = {
            "run_id": state.run_id,
            "updated_at": state.updated_at,
            "fingerprint": failure_fingerprint(state),
            "repeated_failure_count": state.repeated_failure_count,
            "stagnant_iterations": state.stagnant_iterations,
            "failing_checks": [
                {"command": c.command, "exit_code": c.exit_code, "stderr_tail": c.stderr_tail}
                for c in state.failing_checks()
            ],
            "review_findings": [f.model_dump() for f in state.review_findings],
        }
        _atomic_write(self.failures_path, json.dumps(payload, indent=2, sort_keys=True))


# Where to re-enter the graph after a clean stop at a given completed node.
_AFTER_NODE = {
    None: "plan",
    "bootstrap": "plan",
    "plan": "implement",
    "implement": "verify",
    "verify": "decide",
    "review": "decide",
    "repair": "verify",
    "escalation_review": "repair",
    "decide": "decide",
    "checkpoint": "plan",
}


def reconcile_for_resume(state: HarnessState, git, protected_paths: list[str]) -> HarnessState:
    """Decide where a resumed run may safely re-enter the graph, or stop.

    Rules:
      * A write-capable Claude call in flight at crash time is NEVER replayed -
        route to deterministic ``verify`` on whatever is on disk.
      * A checkpoint intent is reconciled against HEAD (retry / adopt / stop).
      * Read-only workers in flight are simply re-run.
      * Anything that cannot be reconciled unambiguously stops the run.
    """
    try:
        changed = git.changed_paths()
    except Exception as exc:  # pragma: no cover - defensive
        state.stop_reason = StopReason.CLI_FATAL
        state.next_node = None
        return state

    hits = git.protected_hits(changed, protected_paths)
    if hits:
        state.stop_reason = StopReason.PROTECTED_PATH_MODIFIED
        state.next_node = None
        return state

    # 1) checkpoint reconciliation takes priority
    if state.checkpoint_status in ("intended", "committed"):
        state.worker_in_flight = None
        state.next_node = "checkpoint"
        return state

    # 2) HEAD must match what we expect (harness commits move it forward)
    if state.expected_head is not None:
        try:
            if git.head() != state.expected_head:
                state.stop_reason = StopReason.EXPECTED_HEAD_MOVED
                state.next_node = None
                return state
        except Exception:
            state.stop_reason = StopReason.CLI_FATAL
            state.next_node = None
            return state

    # 3) write-capable worker in flight -> never replay, go verify
    if state.is_write_worker_in_flight():
        known = set(state.owned_paths)
        try:
            untracked = set(git.untracked_paths())
        except Exception:
            untracked = set()
        unexpected = [p for p in changed if p not in known and p not in untracked]
        if unexpected:
            state.stop_reason = StopReason.UNEXPECTED_WORKTREE_STATE
            state.next_node = None
            return state
        state.add_owned(changed)
        state.worker_in_flight = None
        state.next_node = "verify"
        return state

    # 4) read-only workers / planner in flight -> re-run that node
    if state.worker_in_flight == "codex_review":
        state.worker_in_flight = None
        state.next_node = "review"
        return state
    if state.worker_in_flight == "codex_escalate":
        state.worker_in_flight = None
        state.next_node = "repair"
        return state
    if state.worker_in_flight == "claude_plan":
        state.worker_in_flight = None
        state.next_node = "plan"
        return state

    # 5) no worker in flight -> derive from the last completed node
    nxt = state.next_node
    if nxt not in RESUMABLE_NODES:
        nxt = _AFTER_NODE.get(state.last_completed_node)
    if nxt not in RESUMABLE_NODES:
        state.stop_reason = StopReason.UNSAFE_RESUME_STATE
        state.next_node = None
        return state
    state.next_node = nxt
    return state
