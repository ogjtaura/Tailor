"""Git operations - owned by the orchestrator, never by Claude or Codex.

Permitted: read repo state, hash the working tree, build a review payload, and
make checkpoint commits from an *explicit pathspec* after checks + review pass.

Forbidden (never called): push, reset, checkout --, rebase, filter-branch,
filter-repo, branch deletion, any remote or --force operation, and ``git add -A``.

All path parsing is NUL-delimited (``-z``): git output is never split on
whitespace, so filenames containing spaces or newlines are handled correctly.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

DEFAULT_BRANCHES = {"main", "master"}
CO_AUTHOR_TRAILER = "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
_MISSING = None  # hash sentinel for a path that does not exist on disk


class GitError(RuntimeError):
    pass


@dataclass
class WorktreeSnapshot:
    """Changed paths (tracked-modified + untracked) and their content hashes."""

    changed: list[str] = field(default_factory=list)
    hashes: dict[str, Optional[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"changed": list(self.changed), "hashes": dict(self.hashes)}

    @classmethod
    def from_dict(cls, data: dict) -> "WorktreeSnapshot":
        return cls(changed=list(data.get("changed", [])), hashes=dict(data.get("hashes", {})))


class GitTools:
    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    # -- low level --------------------------------------------------------

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc

    @staticmethod
    def _split_z(text: str) -> list[str]:
        return [p for p in text.split("\0") if p]

    # -- guards ---------------------------------------------------------------

    def ensure_repo(self) -> None:
        proc = self._run("rev-parse", "--show-toplevel", check=False)
        if proc.returncode != 0:
            raise GitError(f"{self.repo_root} is not inside a git repository")
        top = Path(proc.stdout.strip()).resolve()
        if top != self.repo_root:
            raise GitError(
                f"git top-level {top} does not match configured repo root {self.repo_root}"
            )

    def is_detached(self) -> bool:
        proc = self._run("symbolic-ref", "-q", "HEAD", check=False)
        return proc.returncode != 0

    def require_non_default_branch(self) -> str:
        if self.is_detached():
            raise GitError("refusing to run on a detached HEAD")
        branch = self.current_branch()
        if branch in DEFAULT_BRANCHES:
            raise GitError(
                f"refusing to run on protected branch {branch!r}; create a feature branch"
            )
        return branch

    # -- introspection ---------------------------------------------------

    def current_branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def head(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.strip()

    def commit_parents(self, sha: str) -> list[str]:
        out = self._run("rev-list", "--parents", "-n", "1", sha).stdout.split()
        return out[1:] if len(out) > 1 else []

    def commit_message(self, sha: str) -> str:
        return self._run("log", "-1", "--format=%B", sha).stdout

    def status_porcelain(self) -> str:
        return self._run("status", "--porcelain").stdout

    def is_clean(self) -> bool:
        return self._run("status", "--porcelain", "-z").stdout.strip("\0").strip() == ""

    def tracked_changes(self) -> list[str]:
        return sorted(set(self._split_z(self._run("diff", "--name-only", "-z", "HEAD").stdout)))

    def untracked_paths(self) -> list[str]:
        return sorted(
            set(self._split_z(self._run("ls-files", "--others", "--exclude-standard", "-z").stdout))
        )

    def changed_paths(self) -> list[str]:
        """Tracked modifications + untracked files, repo-relative, sorted, NUL-safe."""
        return sorted(set(self.tracked_changes()) | set(self.untracked_paths()))

    # -- content hashing / snapshots ---------------------------------

    def _hash_path(self, rel: str) -> Optional[str]:
        p = self.repo_root / rel
        try:
            data = p.read_bytes()
        except (OSError, IsADirectoryError):
            return _MISSING
        return hashlib.sha256(data).hexdigest()

    def snapshot(self, paths: Optional[Iterable[str]] = None) -> WorktreeSnapshot:
        changed = sorted(set(paths)) if paths is not None else self.changed_paths()
        return WorktreeSnapshot(changed=changed, hashes={p: self._hash_path(p) for p in changed})

    def attributable_changes(
        self, before: WorktreeSnapshot, after: WorktreeSnapshot
    ) -> list[str]:
        """Paths whose content differs between the two snapshots (new, edited, or
        deleted). Pre-existing changes with unchanged content are NOT attributed."""
        keys = set(before.hashes) | set(after.hashes) | set(after.changed)
        out = [k for k in keys if before.hashes.get(k) != after.hashes.get(k, self._hash_path(k))]
        return sorted(set(out))

    # -- protected-path detection --------------------------------------

    @staticmethod
    def protected_hits(changed: Iterable[str], protected: Sequence[str]) -> list[str]:
        hits: list[str] = []
        for path in changed:
            norm = path.lstrip("./")
            for pat in protected:
                pat_norm = pat.lstrip("./")
                if pat_norm.endswith("/"):
                    if norm == pat_norm[:-1] or norm.startswith(pat_norm):
                        hits.append(path)
                        break
                elif norm == pat_norm:
                    hits.append(path)
                    break
        return sorted(set(hits))

    # -- review payload ------------------------------------------------

    def review_diff(self, owned_paths: Sequence[str]) -> str:
        """Diff text covering every owned path that a checkpoint could commit -
        including brand-new untracked files (via ``diff --no-index``), which a
        plain ``git diff HEAD`` would miss. Does not touch the index."""
        owned = sorted(set(owned_paths))
        tracked = set(self._split_z(self._run("ls-files", "-z").stdout))
        parts: list[str] = []

        tracked_owned = [p for p in owned if p in tracked]
        if tracked_owned:
            parts.append(self._run("diff", "HEAD", "--", *tracked_owned).stdout)

        for p in owned:
            if p in tracked:
                continue
            if not (self.repo_root / p).is_file():
                continue
            proc = self._run("diff", "--no-index", "--", "/dev/null", p, check=False)
            # exit 1 just means "differences found", which is expected here
            if proc.returncode not in (0, 1):
                raise GitError(f"diff --no-index failed for {p!r}: {proc.stderr.strip()}")
            parts.append(proc.stdout)

        return "\n".join(part for part in parts if part.strip())

    def diff(self) -> str:
        return self._run("diff", "HEAD").stdout

    def diff_hash(self) -> str:
        return hashlib.sha256(self.diff().encode("utf-8", "replace")).hexdigest()

    # -- checkpoint -----------------------------------------------------

    def checkpoint(
        self, message: str, *, pathspec: Sequence[str], protected: Sequence[str] = ()
    ) -> str:
        """Stage exactly ``pathspec`` and commit. Returns the new commit sha."""
        self.ensure_repo()
        paths = [p for p in pathspec if p]
        if not paths:
            raise GitError("checkpoint refused: empty pathspec")
        bad = self.protected_hits(paths, protected) if protected else []
        if bad:
            raise GitError(f"checkpoint refused: pathspec touches protected paths {bad}")
        for p in paths:
            resolved = (self.repo_root / p).resolve()
            if self.repo_root not in resolved.parents and resolved != self.repo_root:
                raise GitError(f"checkpoint refused: {p!r} escapes the repo root")

        self._run("add", "--", *paths)
        if self._run("diff", "--cached", "--quiet", check=False).returncode == 0:
            raise GitError("checkpoint refused: nothing staged (would be an empty commit)")
        self._run("commit", "-m", f"{message}\n\n{CO_AUTHOR_TRAILER}\n")
        return self.head()
