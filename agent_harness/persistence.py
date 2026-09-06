"""Recoverable state under ``.agent/`` (no database - JSON + text only).

Layout::

    .agent/state.json          latest HarnessState (atomic write)
    .agent/progress.md         append-only human-readable log
    .agent/failures.json       accumulated failure fingerprints + findings
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

from agent_harness.state import HarnessState, StopReason, failure_fingerprint


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


class Persistence:
    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.agent_dir = self.repo_root / ".agent"
        self.state_path = self.agent_dir / "state.json"
        self.progress_path = self.agent_dir / "progress.md"
        self.failures_path = self.agent_dir / "failures.json"
        self.runs_dir = self.agent_dir / "runs"

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
        data = json.loads(self.state_path.read_text())
        return HarnessState.model_validate(data)

    # -- derived files ---------------------------------------------------

    def _append_progress(self, state: HarnessState, note: str) -> None:
        line = (
            f"- {state.updated_at} iter={state.iteration} "
            f"status={state.status.value} "
            f"task={state.current_task!r} "
            f"checks_passed={state.checks_passed} review={state.review_status} "
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


def reconcile_for_resume(state: HarnessState, git, protected_paths: list[str]) -> HarnessState:
    """Decide where a resumed run may safely re-enter the graph.

    A write-capable Claude call that was in flight at crash time is NEVER
    replayed: we route straight to deterministic ``verify`` on whatever is on
    disk. Read-only workers are safe to re-run. Anything that cannot be
    reconciled stops the run instead of guessing.
    """
    changed = git.changed_paths()

    hits = git.protected_hits(changed, protected_paths)
    if hits:
        state.stop_reason = StopReason.PROTECTED_PATH_MODIFIED
        state.next_node = None
        return state

    if state.is_write_worker_in_flight():
        # Partial edits may be on disk. Do NOT re-run the writer. A changed path
        # is acceptable only if the harness already recorded touching it, or it
        # is a brand-new untracked file (plausibly created by the crashed
        # writer). Anything else is an unexplained modification -> stop.
        known = set(state.harness_touched_files)
        untracked = set(git.untracked_paths())
        unexpected = [p for p in changed if p not in known and p not in untracked]
        if unexpected:
            state.stop_reason = StopReason.UNEXPECTED_WORKTREE_STATE
            state.next_node = None
            return state
        state.record_touched(changed)
        state.worker_in_flight = None
        state.next_node = "verify"
        return state

    if state.worker_in_flight in ("codex_review", "codex_escalate"):
        state.next_node = "review" if state.worker_in_flight == "codex_review" else "repair"
        state.worker_in_flight = None
        return state

    if not state.next_node:
        state.next_node = "decide"
    return state
