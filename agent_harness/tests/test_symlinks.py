"""Finding 8: path-safety validation during candidate construction must be
LEXICAL. Git commits a symlink as its own tree entry (mode 120000, blob = link
target text); it does not follow the link. A symlink whose target is outside the
repo, or is dangling, is still a legitimate Git tree entry and must freeze.
"""

import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from agent_harness.git_tools import GitError, GitTools, path_is_repo_safe

PROTECTED = ["agent_harness/", "agent.toml", ".git/", ".agent/"]


def _symlinks_supported() -> bool:
    if not hasattr(os, "symlink"):
        return False
    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink("target", Path(d) / "l")
            return True
        except (OSError, NotImplementedError):
            return False


@contextmanager
def repo():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True,
                                        capture_output=True, text=True)
        run("init", "-q", "-b", "work")
        run("config", "user.email", "t@e.c"); run("config", "user.name", "T")
        (root / "app.py").write_text("v0\n")
        run("add", "-A"); run("commit", "-qm", "init")
        yield root, run


def _freeze(g, owned):
    parent = g.head()
    tree = g.freeze_candidate_tree(parent=parent, owned_paths=owned, protected=PROTECTED)
    a = ["h", "h@h", "100000000 +0000"]
    commit = g.commit_tree(tree_oid=tree, parent=parent, message="m", author=a, committer=a)
    return parent, tree, commit


class LexicalPathSafetyTests(unittest.TestCase):
    def test_accepts_ordinary_nested_paths(self):
        for p in ("app.py", "src/x.py", "a/b/c/d.txt", "name with spaces.py", "./rel.py"):
            self.assertTrue(path_is_repo_safe(p), p)

    def test_rejects_traversal_absolute_and_dotgit(self):
        for p in ("/etc/passwd", "../x", "a/../../b", ".git/config", "a/.git/x", "", "..", "."):
            self.assertFalse(path_is_repo_safe(p), p)

    def test_does_not_follow_symlink_target(self):
        # a path that IS a symlink to outside the repo is lexically in-repo
        self.assertTrue(path_is_repo_safe("external-link"))


@unittest.skipUnless(_symlinks_supported(), "symlinks not supported here")
class SymlinkFreezeTests(unittest.TestCase):
    def test_symlink_target_inside_repo(self):
        with repo() as (root, run):
            g = GitTools(root)
            os.symlink("app.py", root / "inside")
            _, _, commit = _freeze(g, ["inside"])
            self.assertEqual(run("ls-tree", commit, "inside").stdout.split()[0], "120000")
            self.assertEqual(run("show", f"{commit}:inside").stdout, "app.py")

    def test_symlink_target_outside_repo(self):
        with repo() as (root, run):
            g = GitTools(root)
            os.symlink("/etc/hosts", root / "external-link")
            _, _, commit = _freeze(g, ["external-link"])
            self.assertEqual(run("ls-tree", commit, "external-link").stdout.split()[0], "120000")
            self.assertEqual(run("show", f"{commit}:external-link").stdout, "/etc/hosts")

    def test_dangling_symlink(self):
        with repo() as (root, run):
            g = GitTools(root)
            os.symlink("nowhere-nothing", root / "dangling")
            _, _, commit = _freeze(g, ["dangling"])
            self.assertEqual(run("ls-tree", commit, "dangling").stdout.split()[0], "120000")
            self.assertEqual(run("show", f"{commit}:dangling").stdout, "nowhere-nothing")

    def test_symlink_target_change_produces_new_tree(self):
        with repo() as (root, run):
            g = GitTools(root)
            os.symlink("/a/one", root / "lnk")
            _, tree1, _ = _freeze(g, ["lnk"])
            os.unlink(root / "lnk"); os.symlink("/a/two", root / "lnk")
            _, tree2, _ = _freeze(g, ["lnk"])
            self.assertNotEqual(tree1, tree2)

    def test_symlink_replaced_by_regular_file(self):
        with repo() as (root, run):
            g = GitTools(root)
            os.symlink("/x", root / "swap"); run("add", "swap"); run("commit", "-qm", "link")
            os.unlink(root / "swap"); (root / "swap").write_text("now a real file\n")
            _, _, commit = _freeze(g, ["swap"])
            self.assertEqual(run("ls-tree", commit, "swap").stdout.split()[0], "100644")
            self.assertEqual(run("show", f"{commit}:swap").stdout, "now a real file\n")

    def test_regular_file_replaced_by_symlink(self):
        with repo() as (root, run):
            g = GitTools(root)
            (root / "swap").write_text("real\n"); run("add", "swap"); run("commit", "-qm", "file")
            os.unlink(root / "swap"); os.symlink("/x", root / "swap")
            _, _, commit = _freeze(g, ["swap"])
            self.assertEqual(run("ls-tree", commit, "swap").stdout.split()[0], "120000")
            self.assertEqual(run("show", f"{commit}:swap").stdout, "/x")

    def test_traversal_pathspec_still_refused(self):
        with repo() as (root, run):
            g = GitTools(root)
            with self.assertRaises(GitError):
                g.freeze_candidate_tree(parent=g.head(), owned_paths=["../escape.py"])
            with self.assertRaises(GitError):
                g.freeze_candidate_tree(parent=g.head(), owned_paths=[".git/hooks/evil"])


if __name__ == "__main__":
    unittest.main()
