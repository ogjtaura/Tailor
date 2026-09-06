"""Git operations - owned by the orchestrator, never by Claude or Codex.

Permitted: read repo state, hash the working-tree diff, and make checkpoint
commits from an *explicit pathspec* after checks + review pass.

Forbidden (never called): push, reset, checkout --, rebase, filter-branch,
filter-repo, branch deletion, any remote or --force operation, and ``git add -A``.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_BRANCHES = {"main", "master"}
CO_AUTHOR_TRAILER = "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"


class GitError(RuntimeError):
    pass


@dataclass
class GitStatus:
    branch: str
    clean: bool
    porcelain: str
    head: str


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

    def require_non_default_branch(self) -> str:
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

    def status_porcelain(self) -> str:
        return self._run("status", "--porcelain").stdout

    def is_clean(self) -> bool:
        return self.status_porcelain().strip() == ""

    def status(self) -> GitStatus:
        porcelain = self.status_porcelain()
        return GitStatus(
            branch=self.current_branch(),
            clean=porcelain.strip() == "",
            porcelain=porcelain,
            head=self.head(),
        )

    def diff(self) -> str:
        """Full working-tree diff against HEAD (tracked changes)."""
        return self._run("diff", "HEAD").stdout

    def diff_hash(self) -> str:
        return hashlib.sha256(self.diff().encode("utf-8", "replace")).hexdigest()

    def tracked_changes(self) -> list[str]:
        return sorted(set(self._run("diff", "--name-only", "HEAD").stdout.split()))

    def untracked_paths(self) -> list[str]:
        return sorted(
            set(self._run("ls-files", "--others", "--exclude-standard").stdout.split())
        )

    def changed_paths(self) -> list[str]:
        """Tracked modifications plus untracked files, repo-relative, sorted."""
        return sorted(set(self.tracked_changes()) | set(self.untracked_paths()))

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
        full_message = f"{message}\n\n{CO_AUTHOR_TRAILER}\n"
        self._run("commit", "-m", full_message)
        return self.head()
