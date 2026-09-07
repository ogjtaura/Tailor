"""Shared test doubles. No real ``claude`` / ``codex`` / network anywhere."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_harness.config import Config
from agent_harness.git_tools import GitError as GitToolsError, GitTools
from agent_harness.graph import Printer
from agent_harness.state import HarnessState
from agent_harness.workers.base import WorkerResult
from agent_harness.workers.claude import ClaudeInvocation
from agent_harness.workers.codex import EscalateOutcome, ReviewOutcome, ReviewVerdict


def make_config(repo_root: Path, **overrides) -> Config:
    agent = {"max_iterations": 15, "max_repairs_per_task": 5, "max_stagnant_iterations": 3}
    agent.update(overrides.pop("agent", {}))
    data = {
        "agent": agent,
        "claude": {"executable": "/bin/true", **overrides.pop("claude", {})},
        "codex": {"executable": "/bin/true", "review_retries": 1, **overrides.pop("codex", {})},
        "git": {"auto_commit": True, "branch_required": True, "require_clean_worktree": True},
        "safety": {"protected_paths": ["agent_harness/", "agent.toml", ".git/", ".agent/"]},
        "checks": {"commands": ["true"]},
        "classify": {"usage_limit_patterns": ["usage limit", "429", "reached your usage limit"]},
    }
    data.update(overrides)
    return Config(repo_root=repo_root, config_path=repo_root / "agent.toml", **data)


def worker_result(
    *, ok=True, exit_code=0, stdout="", stderr="", timed_out=False, classification="ok"
) -> WorkerResult:
    return WorkerResult(
        ok=ok,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_s=0.01,
        timed_out=timed_out,
        classification=classification,
        argv=["fake"],
    )


def claude_invocation(role="implement", *, ok=True, classification="ok", timed_out=False,
                      exit_code=0, result_text="done", is_error=False) -> ClaudeInvocation:
    return ClaudeInvocation(
        role=role,
        result=worker_result(
            ok=ok, exit_code=exit_code, timed_out=timed_out, classification=classification
        ),
        result_text=result_text,
        is_error=is_error,
    )


class FakeClaude:
    """Scriptable ClaudeWorker stand-in."""

    def __init__(self, *, plan_result=None, plan_exc=None,
                 implement_seq=None, repair_seq=None, on_edit=None):
        self.log_dir = None
        self._plan_result = plan_result
        self._plan_exc = plan_exc
        self._implement_seq = list(implement_seq or [])
        self._repair_seq = list(repair_seq or [])
        self._on_edit = on_edit  # callable() -> None, simulates file changes
        self.calls: list[str] = []

    def plan(self, **kw):
        self.calls.append("plan")
        if self._plan_exc:
            raise self._plan_exc
        return self._plan_result

    def implement(self, **kw):
        self.calls.append("implement")
        if self._on_edit:
            self._on_edit("implement")
        return self._implement_seq.pop(0) if self._implement_seq else claude_invocation("implement")

    def repair(self, **kw):
        self.calls.append("repair")
        if self._on_edit:
            self._on_edit("repair")
        return self._repair_seq.pop(0) if self._repair_seq else claude_invocation("repair")


class FakeCodex:
    def __init__(self, *, review_seq=None, escalate_result=None):
        self.log_dir = None
        self._review_seq = list(review_seq or [])
        self._escalate = escalate_result or EscalateOutcome(root_cause="root cause: X")
        self.calls: list[str] = []

    def review(self, **kw) -> ReviewOutcome:
        self.calls.append("review")
        if self._review_seq:
            return self._review_seq.pop(0)
        return ReviewOutcome(kind="verdict", verdict=ReviewVerdict(verdict="pass", severity="none"))

    def escalate(self, **kw) -> EscalateOutcome:
        self.calls.append("escalate")
        return self._escalate


import contextlib as _contextlib
import hashlib as _hashlib
import tempfile as _tempfile


def _sha(*parts) -> str:
    h = _hashlib.sha256()
    for p in parts:
        h.update(str(p).encode())
        h.update(b"\0")
    return h.hexdigest()


class FakeGit:
    """In-memory model of the immutable-object checkpoint boundary.

    Models: a working tree (`_files`), the feature-branch tip content
    (`_committed` = the "parent tree"), named refs (`_refs`), and commit/tree
    objects (`_objects`). Deliberately has no push/reset/checkout methods.
    """

    DEFAULT_REF = "refs/heads/agent-harness-v0"

    def __init__(self, repo_root: Path, *, clean=True):
        self.repo_root = Path(repo_root)
        self._clean = clean
        self._files: dict[str, str] = {}       # working-tree content of dirty paths
        self._untracked: set[str] = set()
        self._committed: dict[str, str] = {}    # content at the feature ref tip
        self._head_ref = self.DEFAULT_REF
        self._detached = False
        self._edit_serial = 0
        self._objects: dict[str, dict] = {}
        base_tree = _sha("tree", "")
        base_commit = "0" * 40
        self._objects[base_commit] = {"type": "commit", "tree": base_tree, "parent": None,
                                      "message": "init"}
        self._objects[base_tree] = {"type": "tree", "files": {}}
        self._refs: dict[str, str] = {self._head_ref: base_commit}
        self._symrefs: dict[str, str] = {}      # ref -> ref it symbolically points at
        self.cas_calls: list[tuple] = []
        # legacy attribute some tests inspect
        self.checkpoints: list[dict] = []

    # -- config hooks for tests --------------------------------------
    raise_on_diff_tree = False
    empty_diff_tree = False
    raise_on_freeze = False

    # -- guards / introspection ------------------------------------
    def ensure_repo(self): ...
    def is_detached(self): return self._detached
    def current_branch(self): return "HEAD" if self._detached else self._head_ref.rsplit("/", 1)[-1]
    def current_branch_ref(self):
        if self._detached:
            raise GitToolsError("HEAD is detached")
        return self._head_ref
    def require_non_default_branch(self):
        if self._detached:
            raise GitToolsError("detached HEAD")
        b = self.current_branch()
        if b in {"main", "master"}:
            raise GitToolsError(f"protected branch {b!r}")
        return b
    def head(self): return self._refs.get(self._head_ref)
    def resolve_ref(self, ref):
        seen = set()
        while ref in self._symrefs and ref not in seen:
            seen.add(ref); ref = self._symrefs[ref]
        return self._refs.get(ref)
    def ref_name_is_well_formed(self, ref):
        return isinstance(ref, str) and bool(ref) and " " not in ref and not ref.endswith("/")
    def symbolic_ref_target(self, ref): return self._symrefs.get(ref)
    def is_symbolic_ref(self, ref): return ref in self._symrefs
    def validate_checkpoint_target_ref(self, ref, *, expected_current=None):
        from agent_harness.git_tools import is_protected_target_ref, is_write_target_namespace
        if not isinstance(ref, str) or not ref:
            raise GitToolsError("checkpoint target ref is missing")
        if not is_write_target_namespace(ref):
            raise GitToolsError(f"{ref!r} is not a refs/heads/<name> branch ref")
        if not self.ref_name_is_well_formed(ref):
            raise GitToolsError(f"{ref!r} is not a well-formed ref name")
        if is_protected_target_ref(ref):
            raise GitToolsError(f"{ref!r} is permanently protected (main/master)")
        if ref in self._symrefs:
            raise GitToolsError(f"{ref!r} is a symbolic ref -> {self._symrefs[ref]!r}")
        if expected_current is not None and self._refs.get(ref) != expected_current:
            raise GitToolsError(f"{ref!r} is at {self._refs.get(ref)}, expected {expected_current}")
    def validate_candidate_identity(self, commit_oid, tree_oid, expected_parent):
        if not (commit_oid and tree_oid and expected_parent):
            raise GitToolsError("candidate identity incomplete")
        if self.object_type(commit_oid) != "commit":
            raise GitToolsError(f"{commit_oid} is not a commit")
        if self.object_type(tree_oid) != "tree":
            raise GitToolsError(f"{tree_oid} is not a tree")
        if self.commit_tree_of(commit_oid) != tree_oid:
            raise GitToolsError("candidate commit tree != candidate_tree_oid")
        if self.commit_parents(commit_oid) != [expected_parent]:
            raise GitToolsError("candidate parent != expected parent")
    def set_symref(self, ref, target): self._symrefs[ref] = target
    def object_exists(self, oid): return bool(oid) and oid in self._objects
    def object_type(self, oid):
        o = self._objects.get(oid)
        return o.get("type") if o else None
    def commit_tree_of(self, oid):
        o = self._objects.get(oid)
        if not o or o.get("type") != "commit":
            raise GitToolsError(f"{oid} is not a commit")
        return o["tree"]
    def commit_parents(self, oid):
        o = self._objects.get(oid)
        if not o or o.get("type") != "commit":
            raise GitToolsError(f"{oid} is not a commit")
        p = o.get("parent")
        return [p] if p else []
    def is_clean(self): return self._clean and not self._files

    def tracked_changes(self):
        return sorted(p for p in self._files if p not in self._untracked)
    def untracked_paths(self):
        return sorted(p for p in self._files if p in self._untracked)
    def changed_paths(self):
        return sorted(self._files)

    @staticmethod
    def protected_hits(changed, protected):
        return GitTools.protected_hits(changed, protected)

    # -- worktree snapshot / attribution -------------------------
    def snapshot(self, paths=None):
        from agent_harness.git_tools import WorktreeSnapshot
        changed = sorted(set(paths)) if paths is not None else self.changed_paths()
        hashes = {p: (_sha("blob", self._files[p]) if p in self._files else None) for p in changed}
        return WorktreeSnapshot(changed=changed, hashes=hashes)

    def attributable_changes(self, before, after):
        keys = set(before.hashes) | set(after.hashes) | set(after.changed)
        cur = {p: (_sha("blob", self._files[p]) if p in self._files else None) for p in keys}
        return sorted(p for p in keys if before.hashes.get(p) != after.hashes.get(p, cur.get(p)))

    def content_state(self, rel):
        return ("blob:" + _sha("blob", self._files[rel])) if rel in self._files else "absent"

    def owned_digest(self, paths):
        return _sha("owned", *(f"{p}={self.content_state(p)}" for p in sorted(set(paths))))

    # -- immutable candidate -------------------------------------
    def _tree_of(self, mapping):
        oid = _sha("tree", *(f"{p}={_sha('blob', c)}" for p, c in sorted(mapping.items())))
        self._objects.setdefault(oid, {"type": "tree", "files": dict(mapping)})
        return oid

    def freeze_candidate_tree(self, *, parent, owned_paths, protected=()):
        if self.raise_on_freeze:
            raise GitToolsError("forced freeze failure")
        bad = self.protected_hits(owned_paths, protected) if protected else []
        if bad:
            raise GitToolsError(f"freeze refused: protected {bad}")
        if not [p for p in owned_paths if p]:
            raise GitToolsError("freeze refused: empty owned pathspec")
        overlay = dict(self._committed)
        for p in owned_paths:
            if p in self._files:
                overlay[p] = self._files[p]
            else:
                overlay.pop(p, None)
        return self._tree_of(overlay)

    def commit_tree(self, *, tree_oid, parent, message, author, committer):
        oid = _sha("commit", tree_oid, parent, message)   # deterministic on content
        self._objects[oid] = {"type": "commit", "tree": tree_oid, "parent": parent,
                              "message": message}
        return oid

    @_contextlib.contextmanager
    def materialize(self, commit_oid):
        o = self._objects.get(commit_oid)
        if not o or o.get("type") != "commit":
            raise GitToolsError(f"cannot materialize {commit_oid}")
        files = self._objects[o["tree"]]["files"]
        d = _tempfile.mkdtemp(prefix="fakegit-wt-")
        try:
            for rel, content in files.items():
                fp = Path(d) / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content)
            yield Path(d)
        finally:
            __import__("shutil").rmtree(d, ignore_errors=True)

    def diff_tree(self, parent, commit_oid):
        if self.raise_on_diff_tree:
            raise GitToolsError("forced diff_tree failure")
        if self.empty_diff_tree:
            return ""
        o = self._objects.get(commit_oid, {})
        files = self._objects.get(o.get("tree"), {}).get("files", {})
        parent_files = self._objects.get(self._objects.get(parent, {}).get("tree"), {}).get("files", {})
        lines = []
        for p in sorted(set(files) | set(parent_files)):
            if files.get(p) != parent_files.get(p):
                lines.append(f"=== {p} ===\n{files.get(p, '(deleted)')}\n")
        return "".join(lines)

    def cas_update_ref(self, ref, new_oid, expected_old):
        # mirror the real helper: full target validation + no deref
        self.validate_checkpoint_target_ref(ref, expected_current=expected_old)
        self.cas_calls.append((ref, new_oid, expected_old))
        if self._refs.get(ref) != expected_old:
            raise GitToolsError(
                f"CAS {ref} {expected_old}->{new_oid} failed: is {self._refs.get(ref)}"
            )
        self._refs[ref] = new_oid
        if ref == self._head_ref and not self._detached:
            tree = self._objects[new_oid]["tree"]
            self._committed = dict(self._objects[tree]["files"])
            self._files = {}
            self._untracked = set()
        self.checkpoints.append({"ref": ref, "commit": new_oid})

    def sync_index_to(self, commit_oid): ...

    # -- test helpers -------------------------------------------
    def set_changed(self, paths, *, untracked=False):
        self._edit_serial += 1
        for p in paths:
            self._files[p] = f"content-{self._edit_serial}-{p}"
            if untracked:
                self._untracked.add(p)
    def write(self, path, content, *, untracked=False):
        self._files[path] = content
        if untracked:
            self._untracked.add(path)
    def commit_baseline(self, mapping):
        """Seed the feature-ref tip content (the parent tree)."""
        self._committed = dict(mapping)
        tree = self._tree_of(self._committed)
        parent = self._refs[self._head_ref]
        oid = _sha("commit", tree, parent, "baseline")
        self._objects[oid] = {"type": "commit", "tree": tree, "parent": parent, "message": "baseline"}
        self._refs[self._head_ref] = oid
        return oid
    def set_detached(self, val): self._detached = val
    def set_clean(self, val): self._clean = val
    def set_branch(self, name):
        self._head_ref = f"refs/heads/{name}"
        self._refs.setdefault(self._head_ref, self._refs.get(self.DEFAULT_REF, "0" * 40))
    def set_ref(self, ref, oid): self._refs[ref] = oid


class RecordingStore:
    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self.runs_dir = self.repo_root / ".agent" / "runs"
        self.saves: list[str] = []
        self._state = None

    def init_run(self, state): self.saves.append("init")
    def run_log_dir(self, state):
        d = self.runs_dir / state.run_id
        d.mkdir(parents=True, exist_ok=True)
        return d
    def save(self, state, note=""):
        self.saves.append(note)
        self._state = state.model_copy(deep=True)
    def load(self): return self._state

    def create_run_manifest(self, state, *, authorized_target_ref, initial_target_oid):
        from datetime import datetime, timezone
        from agent_harness.persistence import RunManifest, RunManifestError
        if getattr(self, "_manifest", None) is not None:
            raise RunManifestError("run manifest already exists")
        self._manifest = RunManifest(
            version=1, run_id=state.run_id,
            authorized_target_ref=authorized_target_ref,
            initial_target_oid=(initial_target_oid or "0" * 40),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return self._manifest
    write_run_manifest = create_run_manifest
    def read_run_manifest(self, run_id):
        from agent_harness.persistence import RunManifestError
        m = getattr(self, "_manifest", None)
        if m is None:
            return None
        if m.run_id != run_id:
            raise RunManifestError("run manifest run_id mismatch")
        return m


class SpyPrinter(Printer):
    def __init__(self):
        super().__init__(enabled=False)
        self.visited: list[str] = []

    def transition(self, node, state, detail=""):
        self.visited.append(node)

    def info(self, msg): ...


def base_state(objective="obj", **kw) -> HarnessState:
    return HarnessState(objective=objective, run_id="testrun", **kw)
