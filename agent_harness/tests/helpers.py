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


class FakeGit:
    """In-memory git model. Deliberately has no push/reset/checkout methods.

    Tracks per-path content so snapshot()/attributable_changes()/diff_hash()
    behave like the real GitTools for routing tests.
    """

    def __init__(self, repo_root: Path, *, clean=True):
        import hashlib
        self._hashlib = hashlib
        self.repo_root = Path(repo_root)
        self._clean = clean
        self._files: dict[str, str] = {}      # path -> current dirty content
        self._untracked: set[str] = set()
        self._head = "0" * 40
        self._branch = "agent-harness-v0"
        self._detached = False
        self._commits: dict[str, dict] = {}
        self._edit_serial = 0
        self.checkpoints: list[dict] = []

    # -- guards / introspection
    def ensure_repo(self): ...
    def is_detached(self): return self._detached
    def require_non_default_branch(self):
        if self._detached:
            raise GitToolsError("refusing to run on a detached HEAD")
        if self._branch in {"main", "master"}:
            raise GitToolsError(f"refusing to run on protected branch {self._branch!r}")
        return self._branch
    def current_branch(self): return "HEAD" if self._detached else self._branch
    def head(self): return self._head
    def is_clean(self): return self._clean and not self._files
    def status_porcelain(self):
        return "".join(f" M {p}\n" for p in self._files)
    def commit_parents(self, sha): return list(self._commits.get(sha, {}).get("parents", []))
    def commit_message(self, sha): return self._commits.get(sha, {}).get("message", "")

    def tracked_changes(self):
        return sorted(p for p in self._files if p not in self._untracked)
    def untracked_paths(self):
        return sorted(p for p in self._files if p in self._untracked)
    def changed_paths(self):
        return sorted(self._files)

    def diff(self):
        return "".join(f"--- a/{p}\n+++ b/{p}\n{c}\n" for p, c in sorted(self._files.items()))
    def diff_hash(self):
        return self._hashlib.sha256(self.diff().encode()).hexdigest()

    def review_diff(self, owned_paths):
        owned = sorted(set(owned_paths))
        return "".join(
            f"=== {p} ({'new file' if p in self._untracked else 'modified'}) ===\n"
            f"{self._files.get(p, '')}\n"
            for p in owned if p in self._files
        )

    # -- snapshots
    def snapshot(self, paths=None):
        from agent_harness.git_tools import WorktreeSnapshot
        changed = sorted(set(paths)) if paths is not None else self.changed_paths()
        hashes = {p: (self._hashlib.sha256(self._files[p].encode()).hexdigest()
                      if p in self._files else None) for p in changed}
        return WorktreeSnapshot(changed=changed, hashes=hashes)

    def attributable_changes(self, before, after):
        keys = set(before.hashes) | set(after.hashes) | set(after.changed)
        cur = {p: (self._hashlib.sha256(self._files[p].encode()).hexdigest()
                   if p in self._files else None) for p in keys}
        return sorted(p for p in keys if before.hashes.get(p) != after.hashes.get(p, cur.get(p)))

    @staticmethod
    def protected_hits(changed, protected):
        return GitTools.protected_hits(changed, protected)

    def checkpoint(self, message, *, pathspec, protected=()):
        bad = self.protected_hits(pathspec, protected) if protected else []
        if not pathspec:
            raise GitToolsError("empty pathspec")
        if bad:
            raise GitToolsError(f"protected: {bad}")
        parent = self._head
        self._head = f"c{len(self.checkpoints)+1:0>39}"
        self._commits[self._head] = {"parents": [parent], "message": message}
        self.checkpoints.append({"message": message, "pathspec": list(pathspec)})
        for p in list(pathspec):
            self._files.pop(p, None)
            self._untracked.discard(p)
        return self._head

    # -- test helpers
    def set_changed(self, paths, *, untracked=False):
        """Simulate a writer editing `paths` with fresh content each call."""
        self._edit_serial += 1
        for p in paths:
            self._files[p] = f"content-{self._edit_serial}-{p}"
            if untracked:
                self._untracked.add(p)
    def write(self, path, content, *, untracked=False):
        self._files[path] = content
        if untracked:
            self._untracked.add(path)
    def set_detached(self, val): self._detached = val
    def set_clean(self, val): self._clean = val
    def set_branch(self, name): self._branch = name


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


class SpyPrinter(Printer):
    def __init__(self):
        super().__init__(enabled=False)
        self.visited: list[str] = []

    def transition(self, node, state, detail=""):
        self.visited.append(node)

    def info(self, msg): ...


def base_state(objective="obj", **kw) -> HarnessState:
    return HarnessState(objective=objective, run_id="testrun", **kw)
