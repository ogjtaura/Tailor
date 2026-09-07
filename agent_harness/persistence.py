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

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from agent_harness.git_tools import GitError, is_protected_target_ref
from agent_harness.state import (
    RESUMABLE_NODES,
    HarnessState,
    StopReason,
    failure_fingerprint,
)

RUN_MANIFEST_VERSION = 1

# git object ids: SHA-1 (40) today, SHA-256 (64) if the repo ever migrates.
_HEX_OID_LENGTHS = (40, 64)
_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


class RunManifest(BaseModel):
    """The write-once run-authorization record persisted at
    ``.agent/runs/<run_id>/manifest.json``.

    There is exactly ONE supported schema for the current V0 format and it is
    validated strictly: every field is required, no defaults populate a missing
    field on read, unknown/extra fields are rejected, and no type coercion is
    performed. This is the only manifest parser the resume-authorization path
    uses.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1]
    run_id: str
    authorized_target_ref: str
    initial_target_oid: str
    created_at: str

    @field_validator("version", mode="before")
    @classmethod
    def _exact_int_version(cls, v: object) -> object:
        # Pydantic strict still coerces 1.0 / True -> 1 for Literal[1]; forbid
        # anything whose runtime type is not exactly int (bool is not int here).
        if type(v) is not int:
            raise ValueError("version must be the integer 1 (no bool/float/str)")
        return v

    @field_validator("run_id", "authorized_target_ref")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-blank string")
        return v

    @field_validator("initial_target_oid")
    @classmethod
    def _hex_oid(cls, v: str) -> str:
        if len(v) not in _HEX_OID_LENGTHS or any(c not in _HEX_CHARS for c in v):
            raise ValueError("must be a 40- or 64-character hex git object id")
        return v

    @field_validator("created_at")
    @classmethod
    def _iso8601(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"must be an ISO-8601 timestamp: {exc}") from exc
        return v


class StateCorruptError(RuntimeError):
    """``.agent/state.json`` exists but is not a valid HarnessState."""


class RunLockError(RuntimeError):
    """Another harness process holds the repository-local run lock."""


class RunManifestError(RuntimeError):
    """``.agent/runs/<run_id>/manifest.json`` is missing, corrupt, or its
    write-once run identity does not match."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)  # .agent/ may hold prompts, diffs, model output
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
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

    # -- run manifest: write-once authorized-target identity ---------------
    #
    # Two strictly separate operations that must NEVER be conflated:
    #   create_run_manifest()          - fresh bootstrap only; refuses to
    #                                    overwrite an existing manifest.
    #   read_run_manifest() + verify_run_authorization()
    #                                 - resume/read only; never write, never
    #                                    heal. A missing/invalid/mismatched
    #                                    manifest fails the run closed.

    def _manifest_path(self, run_id: str) -> Path:
        return self.runs_dir / run_id / "manifest.json"

    def create_run_manifest(
        self, state: HarnessState, *, authorized_target_ref: str, initial_target_oid: Optional[str]
    ) -> RunManifest:
        """Create ``.agent/runs/<run_id>/manifest.json`` - ONCE, during fresh-run
        authorization. It is the immutable-by-contract source of truth for which
        feature ref this run may advance. Refuses to overwrite an existing
        manifest (a run is authorized exactly once). Serialises the SAME
        :class:`RunManifest` model the reader validates, so writer schema ==
        reader schema."""
        path = self._manifest_path(state.run_id)
        if path.is_file():
            raise RunManifestError(
                f"run manifest for {state.run_id} already exists; a run is authorized once"
            )
        try:
            manifest = RunManifest(
                version=RUN_MANIFEST_VERSION,
                run_id=state.run_id,
                authorized_target_ref=authorized_target_ref,
                initial_target_oid=initial_target_oid,  # type: ignore[arg-type]
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        except ValidationError as exc:
            raise RunManifestError(f"cannot create a valid run manifest: {exc}") from exc
        _atomic_write(path, manifest.model_dump_json(indent=2))
        return manifest

    # Back-compat alias for the fresh-bootstrap caller / tests.
    write_run_manifest = create_run_manifest

    def read_run_manifest(self, run_id: str) -> Optional[RunManifest]:
        """The ONE runtime manifest parser used by resume authorization.

        bytes -> JSON -> strict :class:`RunManifest` validation -> typed object.
        Returns ``None`` only when the file is absent. Any other outcome - I/O
        error, invalid JSON, non-object root, missing/extra field, wrong type,
        unsupported version, bad OID/timestamp, or a ``run_id`` that does not
        match - raises :class:`RunManifestError`. No handwritten key checks, no
        loose dict fallback.
        """
        path = self._manifest_path(run_id)
        if not path.is_file():
            return None
        try:
            raw = path.read_text()
        except OSError as exc:
            raise RunManifestError(f"run manifest for {run_id} is unreadable: {exc}") from exc
        try:
            manifest = RunManifest.model_validate_json(raw)
        except Exception as exc:  # ValidationError / JSON error / anything -> controlled
            raise RunManifestError(
                f"run manifest for {run_id} failed strict validation: {exc}"
            ) from exc
        if manifest.run_id != run_id:
            raise RunManifestError(
                f"run manifest run_id {manifest.run_id!r} != state run_id {run_id!r}"
            )
        return manifest

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
            f"ckpt={state.checkpoint_phase} "
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
# Values are ALWAYS members of state.RESUMABLE_NODES.
_AFTER_NODE = {
    None: "plan",
    "bootstrap": "plan",
    "plan": "implement",
    "implement": "freeze",
    "repair": "freeze",
    "freeze": "verify",
    "verify": "decide",
    "review": "decide",
    "escalation_review": "repair",
    "decide": "decide",
    "checkpoint": "plan",
}

_PHASE_ENTRY = {"candidate_frozen": "verify", "verified": "review", "reviewed": "checkpoint"}


def _reconcile_checkpoint(state: HarnessState, git) -> HarnessState:
    """Resume into an in-progress immutable-object checkpoint using EXACT object
    identity - never a heuristic. The persisted feature ref is the only target."""
    ref = state.checkpoint_target_ref
    parent = state.checkpoint_expected_parent
    candidate = state.candidate_commit_oid
    tree = state.candidate_tree_oid
    phase = state.checkpoint_phase
    state.worker_in_flight = None

    if not (ref and parent and tree):
        state.stop_reason = StopReason.UNSAFE_RESUME_STATE
        state.next_node = None
        return state

    # -- INVARIANT: a persisted target of main/master fails closed. Never
    #    reinterpret it as another branch, never infer one from HEAD.
    if is_protected_target_ref(ref):
        state.stop_reason = StopReason.PROTECTED_BRANCH
        state.next_node = None
        return state

    # -- INVARIANT: the checkpoint target is exactly the authorized run target,
    #    and that ref is a direct (non-symbolic) refs/heads branch.
    if state.run_target_ref and ref != state.run_target_ref:
        state.stop_reason = StopReason.INVALID_CHECKPOINT_STATE
        state.next_node = None
        return state
    try:
        git.validate_checkpoint_target_ref(ref)
    except GitError:
        state.stop_reason = StopReason.PROTECTED_BRANCH
        state.next_node = None
        return state

    try:
        current = git.resolve_ref(ref)
    except Exception:
        state.stop_reason = StopReason.CLI_FATAL
        state.next_node = None
        return state

    # -- the ref has already been advanced to our exact candidate ----------
    if candidate and current == candidate:
        try:
            ok = (
                git.object_exists(candidate)
                and git.commit_tree_of(candidate) == tree
                and git.commit_parents(candidate) == [parent]
            )
        except Exception:
            ok = False
        if not ok:
            state.stop_reason = StopReason.CANDIDATE_INVALID
            state.next_node = None
            return state
        if not (state.verified_commit_oid == state.reviewed_commit_oid == candidate
                and state.verified_tree_oid == state.reviewed_tree_oid == tree):
            state.stop_reason = StopReason.INVALID_CHECKPOINT_STATE
            state.next_node = None
            return state
        state.checkpoint_phase = "ref_updated"
        state.next_node = "checkpoint"          # checkpoint node completes bookkeeping
        return state

    # -- ref still at the expected parent: candidate not yet accepted ------
    if current == parent:
        if phase == "candidate_frozen" and not candidate:
            # tree->commit crash window: the commit is rebuilt in `freeze` FROM
            # the persisted tree + spec, never from the working tree. All spec
            # fields must already be persisted or we cannot reproduce it.
            if not (state.checkpoint_message and state.checkpoint_author
                    and state.checkpoint_committer):
                state.stop_reason = StopReason.INVALID_CHECKPOINT_STATE
                state.next_node = None
                return state
            try:
                if git.object_type(tree) != "tree" or not git.object_exists(parent):
                    state.stop_reason = StopReason.INVALID_CHECKPOINT_STATE
                    state.next_node = None
                    return state
            except Exception:
                state.stop_reason = StopReason.INVALID_CHECKPOINT_STATE
                state.next_node = None
                return state
            state.next_node = "freeze"
            return state
        if candidate:
            try:
                if not git.object_exists(candidate) or git.commit_tree_of(candidate) != tree:
                    state.stop_reason = StopReason.CANDIDATE_INVALID
                    state.next_node = None
                    return state
            except Exception:
                state.stop_reason = StopReason.CANDIDATE_INVALID
                state.next_node = None
                return state
        state.next_node = _PHASE_ENTRY.get(phase, "checkpoint")
        return state

    # -- ref is neither our candidate nor the expected parent -> foreign --
    state.stop_reason = (
        StopReason.REF_UPDATE_CONFLICT
        if phase in _PHASE_ENTRY
        else StopReason.UNEXPECTED_WORKTREE_STATE
    )
    state.next_node = None
    return state


def reconcile_for_resume(state: HarnessState, git, protected_paths: list[str]) -> HarnessState:
    """Decide where a resumed run may safely re-enter the graph, or stop.

    Rules:
      * An in-progress immutable-object checkpoint is reconciled against exact
        Git ref/object identity (retry CAS / finish bookkeeping / stop).
      * A write-capable Claude call in flight is NEVER replayed, and is only
        resumed when the working-tree delta is fully attributable from the
        persisted pre-writer snapshot; otherwise it fails closed.
      * Read-only workers in flight are simply re-run.
      * Anything that cannot be reconciled unambiguously stops the run.
    """
    try:
        changed = git.changed_paths()
    except Exception:  # pragma: no cover - defensive
        state.stop_reason = StopReason.CLI_FATAL
        state.next_node = None
        return state

    hits = git.protected_hits(changed, protected_paths)
    if hits:
        state.stop_reason = StopReason.PROTECTED_PATH_MODIFIED
        state.next_node = None
        return state

    # 0) INVARIANT: a persisted target of main/master fails closed, regardless
    #    of phase, HEAD, or anything else. Never reinterpreted. Also: the
    #    checkpoint target must match the authorized run target, and every
    #    persisted branch target must be a direct (non-symbolic) refs/heads ref.
    if is_protected_target_ref(state.checkpoint_target_ref) or is_protected_target_ref(
        state.run_target_ref
    ):
        state.stop_reason = StopReason.PROTECTED_BRANCH
        state.next_node = None
        return state
    if (
        state.run_target_ref
        and state.checkpoint_target_ref
        and state.run_target_ref != state.checkpoint_target_ref
    ):
        state.stop_reason = StopReason.INVALID_CHECKPOINT_STATE
        state.next_node = None
        return state
    _authorized = state.run_target_ref or state.checkpoint_target_ref
    if _authorized:
        try:
            git.validate_checkpoint_target_ref(_authorized)
        except GitError:
            state.stop_reason = StopReason.PROTECTED_BRANCH
            state.next_node = None
            return state

    # 1) checkpoint reconciliation takes priority (object identity)
    if state.checkpoint_phase != "none":
        return _reconcile_checkpoint(state, git)

    # 2) expected feature-ref must still be where the harness left it
    if state.expected_head is not None and not state.is_write_worker_in_flight():
        try:
            ref = state.checkpoint_target_ref or git.current_branch_ref()
            tip = git.resolve_ref(ref)
        except Exception:
            tip = None
        if tip is not None and tip != state.expected_head:
            state.stop_reason = StopReason.EXPECTED_HEAD_MOVED
            state.next_node = None
            return state

    # 3) write-capable worker in flight -> NEVER replay. Section 9: from a shared
    #    working tree we cannot prove whether a mutation is Claude's or a
    #    concurrent human edit, so ANY mutation since the writer started fails
    #    closed. Only a writer that left no observable change is resumable.
    if state.is_write_worker_in_flight():
        pre_hashes = dict(state.pre_writer_hashes)
        owned = set(state.owned_paths)

        def _unattributable(why: str) -> HarnessState:
            state.stop_reason = StopReason.INTERRUPTED_WRITE_UNATTRIBUTABLE
            state.next_node = None
            return state

        # (a) a pre-writer change we never took ownership of - unexplained
        if set(state.pre_writer_paths) - owned:
            return _unattributable("pre-writer path not in owned_paths")
        # (b) any content delta since the writer started. In a shared worktree we
        #     cannot prove it is Claude's rather than a concurrent human edit, so
        #     it is never adopted, replayed, or routed to VERIFY as trusted output.
        try:
            now_hashes = dict(git.snapshot(sorted(set(changed) | set(pre_hashes))).hashes)
        except Exception:
            return _unattributable("could not snapshot the worktree")
        keys = set(changed) | set(pre_hashes)
        if any(pre_hashes.get(k) != now_hashes.get(k) for k in keys):
            return _unattributable("content delta since the writer started")
        # (c) a currently-changed path that was never owned - writer's partial output
        if set(changed) - owned:
            return _unattributable("changed path outside owned_paths")
        # nothing observable changed since the writer started -> safe retry
        state.worker_in_flight = None
        state.next_node = "freeze" if owned else "implement"
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

    # 5) no worker in flight -> derive the re-entry node from the last completed
    #    node only (deterministic, always a valid RESUMABLE_NODES member). The
    #    raw persisted ``next_node`` (which may be an edge key like "escalate")
    #    is not trusted here.
    nxt = _AFTER_NODE.get(state.last_completed_node, "__unknown__")
    if nxt not in RESUMABLE_NODES:
        state.stop_reason = StopReason.UNSAFE_RESUME_STATE
        state.next_node = None
        return state
    state.next_node = nxt
    return state


def verify_run_authorization(state: HarnessState, store, git) -> Optional[StopReason]:
    """Resume-path authorization check. READ-ONLY and FAIL-CLOSED.

    The write-once run manifest is the sole source of authorization. If it is
    missing, unreadable, malformed, wrong ``run_id``, missing the target ref, or
    inconsistent with the persisted graph state, the run stops. Authorization is
    NEVER inferred from ``state.run_target_ref`` / ``checkpoint_target_ref`` /
    HEAD / expected parent / candidate metadata, and the manifest is NEVER
    (re)created or healed here.

    Returns a ``StopReason`` to fail closed, or ``None`` on success (binding
    ``state.run_target_ref`` to the manifest value, which must already match if
    it was persisted).
    """
    try:
        manifest = store.read_run_manifest(state.run_id)   # -> RunManifest | None
    except RunManifestError:
        return StopReason.RUN_AUTHORIZATION_INVALID
    except Exception:  # defensive: no raw parser exception may escape the resume path
        return StopReason.RUN_AUTHORIZATION_INVALID
    if manifest is None:
        return StopReason.RUN_AUTHORIZATION_MISSING

    # `manifest` is a strictly-validated RunManifest: authorized is a non-blank str.
    authorized = manifest.authorized_target_ref
    if is_protected_target_ref(authorized):
        return StopReason.PROTECTED_BRANCH

    # The persisted graph state must ALREADY agree with the manifest - it is
    # never corrected to match, and a resumed run without a persisted
    # run_target_ref (pre-manifest / corrupt state) is not migrated, it stops.
    if state.run_target_ref != authorized:
        return StopReason.RUN_AUTHORIZATION_INVALID
    if state.checkpoint_target_ref and state.checkpoint_target_ref != authorized:
        return StopReason.RUN_AUTHORIZATION_INVALID

    # Even with a valid manifest, the ref must still pass every Git/ref-safety
    # invariant (direct, non-symbolic, refs/heads, live).
    try:
        git.validate_checkpoint_target_ref(authorized)
    except GitError:
        return StopReason.PROTECTED_BRANCH

    state.run_target_ref = authorized
    return None
