"""Git operations - owned by the orchestrator, never by Claude or Codex.

The checkpoint boundary is built from **immutable Git objects**, not from
``git add`` + ``git commit`` on the user's shared index:

    freeze_candidate_tree()  isolated GIT_INDEX_FILE: <parent tree> + owned paths
    commit_tree()            unreachable commit object (no ref updated)
    materialize()            detached worktree of that exact commit, for checks
    diff_tree()              object-level review payload (parent -> candidate)
    cas_update_ref()         compare-and-swap the persisted feature-branch ref

Forbidden (never called): push, reset --hard, checkout --, rebase, filter-branch,
filter-repo, branch deletion, any remote or --force* operation, ``git add -A`` on
the shared index, and updating whatever branch merely happens to be checked out.

All path parsing is NUL-delimited (``-z``): git output is never split on
whitespace, so filenames containing spaces or newlines are handled correctly.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

DEFAULT_BRANCHES = {"main", "master"}
# refs the autonomous harness must NEVER advance, under ANY configuration,
# resume state, CLI override, or checkpoint phase. This is an invariant, not a
# preference - see is_protected_target_ref().
PROTECTED_TARGET_REFS = frozenset(
    {"main", "master", "refs/heads/main", "refs/heads/master"}
)
CO_AUTHOR_TRAILER = "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
HARNESS_IDENTITY = ("agent-harness", "noreply@anthropic.com")
_MISSING = None  # hash sentinel for a path that does not exist on disk


class GitError(RuntimeError):
    pass


_REF_HEADS_PREFIX = "refs/heads/"


def is_protected_target_ref(ref: Optional[str]) -> bool:
    """True if ``ref`` names main/master (bare or fully-qualified).

    The single, central definition used by every write-enabled acceptance path:
    fresh bootstrap, first persistence of the target ref, resume/load, the CAS
    itself, and checkpoint-state validation. ``branch_required`` and every other
    config value are irrelevant here - main/master are permanently off limits.
    """
    if not ref:
        return False
    return ref.strip() in PROTECTED_TARGET_REFS


def is_write_target_namespace(ref: Optional[str]) -> bool:
    """True only for a fully-qualified ``refs/heads/<name>`` branch ref - not
    ``HEAD``, not a bare branch name, not tags/remotes/notes/any other
    namespace."""
    return (
        isinstance(ref, str)
        and ref.startswith(_REF_HEADS_PREFIX)
        and len(ref) > len(_REF_HEADS_PREFIX)
        and "\n" not in ref
    )


def path_is_repo_safe(rel: str) -> bool:
    """Lexical (no filesystem, no symlink resolution) check that ``rel`` names an
    entry *inside* the repository namespace. A symlink whose target points
    outside the repo is still a legitimate Git tree entry (mode 120000, blob =
    link text); we must not reject it by following the link. So we validate the
    repository ENTRY PATH syntactically only."""
    if not rel or rel in (".", ".."):
        return False
    if os.path.isabs(rel) or rel.startswith(("/", "\\")):
        return False
    if "\0" in rel:
        return False
    norm = os.path.normpath(rel).replace(os.sep, "/")
    if norm == ".." or norm.startswith("../") or "/../" in norm:
        return False
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts or ".git" in parts:
        return False
    return True


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

    def _run(
        self, *args: str, check: bool = True, env: Optional[dict] = None
    ) -> subprocess.CompletedProcess:
        run_env = None
        if env:
            run_env = os.environ.copy()
            run_env.update(env)
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
            env=run_env,
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

    def content_state(self, rel: str) -> str:
        """Canonical per-path state token: ``blob:<sha256>`` or ``absent``."""
        h = self._hash_path(rel)
        return "absent" if h is None else f"blob:{h}"

    def owned_digest(self, paths: Iterable[str]) -> str:
        """One content-sensitive SHA-256 over the exact proposed checkpoint
        state: sorted ``<path>\\0<content_state>`` records. Distinguishes
        modified / deleted / new / renamed (rename = old->absent + new->blob).
        """
        recs = [f"{p}\0{self.content_state(p)}" for p in sorted(set(paths))]
        return hashlib.sha256("\n".join(recs).encode("utf-8", "replace")).hexdigest()

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

    # -- ref / object plumbing (immutable-object checkpoint boundary) ------

    def current_branch_ref(self) -> str:
        """``refs/heads/<name>`` for the checked-out branch. Raises on detached HEAD."""
        proc = self._run("symbolic-ref", "-q", "HEAD", check=False)
        if proc.returncode != 0:
            raise GitError("HEAD is detached; a feature branch ref is required")
        return proc.stdout.strip()

    def resolve_ref(self, ref: str) -> Optional[str]:
        """The commit OID a ref points at, or None if the ref does not exist."""
        proc = self._run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        out = proc.stdout.strip()
        return out or None

    # -- ref policy: the ONE authoritative target validator ------------------

    def ref_name_is_well_formed(self, ref: str) -> bool:
        return self._run("check-ref-format", ref, check=False).returncode == 0

    def symbolic_ref_target(self, ref: str) -> Optional[str]:
        """If ``ref`` is a symbolic ref, the ref name of its FIRST hop
        (``--no-recurse``); otherwise None. Used to refuse symbolic checkpoint
        targets outright - a symref like ``refs/heads/feature -> refs/heads/main``
        must never be a write target."""
        proc = self._run("symbolic-ref", "--quiet", "--no-recurse", ref, check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def is_symbolic_ref(self, ref: str) -> bool:
        return self.symbolic_ref_target(ref) is not None

    def validate_checkpoint_target_ref(
        self, ref: Optional[str], *, expected_current: Optional[str] = None
    ) -> None:
        """The single authoritative gate for "may the harness advance <ref>?".
        Raises :class:`GitError` on ANY failure; never has a side effect. Used at
        bootstrap, target persistence, freeze, resume, before verify/review, and
        immediately before (and inside) the CAS.

        A symbolic ref is NEVER a valid autonomous checkpoint target.
        """
        if not isinstance(ref, str) or not ref:
            raise GitError("checkpoint target ref is missing")
        if not is_write_target_namespace(ref):
            raise GitError(f"checkpoint target {ref!r} is not a refs/heads/<name> branch ref")
        if not self.ref_name_is_well_formed(ref):
            raise GitError(f"checkpoint target {ref!r} is not a well-formed git ref name")
        if is_protected_target_ref(ref):
            raise GitError(f"checkpoint target {ref!r} is permanently protected (main/master)")
        sym = self.symbolic_ref_target(ref)
        if sym is not None:
            raise GitError(
                f"checkpoint target {ref!r} is a symbolic ref -> {sym!r}; "
                "a symbolic ref is never a valid write target"
            )
        if expected_current is not None:
            cur = self.resolve_ref(ref)
            if cur != expected_current:
                raise GitError(
                    f"checkpoint target {ref!r} is at {cur}, expected {expected_current}"
                )

    def validate_candidate_identity(
        self, commit_oid: Optional[str], tree_oid: Optional[str], expected_parent: Optional[str]
    ) -> None:
        """Prove the immutable candidate's Git identity BEFORE it is materialised
        for deterministic verification and certainly before any paid Codex
        review. Raises :class:`GitError` on any failure."""
        if not (commit_oid and tree_oid and expected_parent):
            raise GitError("candidate identity is incomplete (commit/tree/parent)")
        if self.object_type(commit_oid) != "commit":
            raise GitError(f"candidate {commit_oid} is not a commit object")
        if self.object_type(tree_oid) != "tree":
            raise GitError(f"candidate tree {tree_oid} is not a tree object")
        if self.commit_tree_of(commit_oid) != tree_oid:
            raise GitError("candidate commit tree != candidate_tree_oid")
        parents = self.commit_parents(commit_oid)
        if parents != [expected_parent]:
            raise GitError(
                f"candidate parents {parents} != [expected_parent {expected_parent}]"
            )

    def object_exists(self, oid: str) -> bool:
        return bool(oid) and self._run("cat-file", "-e", f"{oid}^{{object}}", check=False).returncode == 0

    def object_type(self, oid: str) -> Optional[str]:
        """``"commit"`` / ``"tree"`` / ``"blob"`` / ``"tag"`` or None if absent."""
        if not oid:
            return None
        proc = self._run("cat-file", "-t", oid, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def commit_tree_of(self, commit_oid: str) -> str:
        """The tree OID of a commit object. Raises if it is not a commit."""
        return self._run("rev-parse", "--verify", f"{commit_oid}^{{tree}}").stdout.strip()

    def tree_of_ref(self, ref_or_oid: str) -> str:
        return self._run("rev-parse", "--verify", f"{ref_or_oid}^{{tree}}").stdout.strip()

    # -- freeze an immutable candidate -----------------------------------

    def freeze_candidate_tree(
        self, *, parent: str, owned_paths: Sequence[str], protected: Sequence[str] = ()
    ) -> str:
        """Build a Git tree = <parent tree> with ONLY ``owned_paths`` overlaid
        from the working tree, using an index isolated from the user's
        ``.git/index``. Returns the candidate tree OID.

        The tree inherently binds path, blob identity, file mode, symlink
        representation, presence/deletion and directory structure - it is the
        canonical content identity, not a byte digest.
        """
        paths = [p for p in owned_paths if p]
        if not paths:
            raise GitError("freeze refused: empty owned pathspec")
        bad = self.protected_hits(paths, protected) if protected else []
        if bad:
            raise GitError(f"freeze refused: owned pathspec touches protected paths {bad}")
        for p in paths:
            # Validate the repository ENTRY PATH lexically. Do NOT call
            # Path.resolve(): it follows the final symlink, which would wrongly
            # reject a valid in-repo symlink whose target is outside the repo (or
            # dangling). Git commits the link itself (mode 120000), not its
            # target.
            if not path_is_repo_safe(p):
                raise GitError(f"freeze refused: {p!r} is not a safe in-repo path")

        fd, idx = tempfile.mkstemp(prefix="agent-harness-idx-", dir=str(self.repo_root / ".git"))
        os.close(fd)
        os.unlink(idx)  # git wants to create it itself
        try:
            env = {"GIT_INDEX_FILE": idx}
            self._run("read-tree", parent, env=env)
            # --all within a pathspec stages adds, modifications AND deletions
            # for exactly those paths; nothing else from the working tree.
            self._run("add", "--all", "--", *paths, env=env)
            return self._run("write-tree", env=env).stdout.strip()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(idx)

    def commit_tree(
        self,
        *,
        tree_oid: str,
        parent: str,
        message: str,
        author: Sequence[str],
        committer: Sequence[str],
    ) -> str:
        """Create a commit object (NOT reachable from any ref). ``author`` and
        ``committer`` are ``(name, email, git-date)`` triples; with identical
        inputs this reproduces the same commit OID after a crash."""
        an, ae, ad = author
        cn, ce, cd = committer
        env = {
            "GIT_AUTHOR_NAME": an, "GIT_AUTHOR_EMAIL": ae, "GIT_AUTHOR_DATE": ad,
            "GIT_COMMITTER_NAME": cn, "GIT_COMMITTER_EMAIL": ce, "GIT_COMMITTER_DATE": cd,
        }
        full = f"{message}\n\n{CO_AUTHOR_TRAILER}\n"
        return self._run("commit-tree", tree_oid, "-p", parent, "-m", full, env=env).stdout.strip()

    # -- verify / review against the immutable candidate ----------------

    @contextlib.contextmanager
    def materialize(self, commit_oid: str) -> Iterator[Path]:
        """Yield a throwaway detached worktree containing exactly ``commit_oid``.
        Used to run the deterministic checks against the candidate, never the
        mutable original working tree."""
        base = tempfile.mkdtemp(prefix="agent-harness-wt-")
        wt = Path(base) / "candidate"
        try:
            self._run("worktree", "add", "--detach", "-q", str(wt), commit_oid)
            yield wt
        finally:
            self._run("worktree", "remove", "--force", str(wt), check=False)
            shutil.rmtree(base, ignore_errors=True)
            self._run("worktree", "prune", check=False)

    def diff_tree(self, parent: str, commit_oid: str) -> str:
        """Full object-level diff ``parent -> commit_oid`` (modifications,
        additions, deletions, mode changes, symlink changes, renames). No
        working tree or index involvement."""
        return self._run("diff", "--find-renames", parent, commit_oid).stdout

    # -- atomically advance the feature-branch ref (compare-and-swap) ----

    def cas_update_ref(self, ref: str, new_oid: str, expected_old: str) -> None:
        """``git update-ref <ref> <new> <old>`` - succeeds only if ``ref`` still
        equals ``expected_old``. Never touches the index or working tree.

        Re-runs the full target validator immediately before the write (syntax,
        refs/heads namespace, main/master exclusion, and crucially non-symbolic)
        and then uses ``--no-deref`` so that even if ``ref`` becomes a symbolic
        ref in the microsecond after the check, the write targets the ref itself
        and can NEVER advance its referent (main/master). The old-oid
        precondition still guards a lost race."""
        self.validate_checkpoint_target_ref(ref, expected_current=expected_old)
        proc = self._run("update-ref", "--no-deref", ref, new_oid, expected_old, check=False)
        if proc.returncode != 0:
            raise GitError(
                f"CAS update-ref --no-deref {ref} {expected_old[:9]}->{new_oid[:9]} failed: "
                f"{proc.stderr.strip()}"
            )

    def sync_index_to(self, commit_oid: str) -> None:
        """Load ``commit_oid``'s tree into the REAL index (index only; the
        working tree is not touched). Post-CAS hygiene so ``git status`` is not
        left showing phantom staged changes."""
        self._run("read-tree", commit_oid, check=False)

    # -- diagnostics only (NOT a security boundary) --------------------

    def owned_worktree_digest(self, paths: Iterable[str]) -> str:
        return self.owned_digest(paths)
