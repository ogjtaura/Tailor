import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from agent_harness.git_tools import GitError, GitTools


@contextmanager
def temp_repo():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True,
                                        capture_output=True, text=True, env={**_env(), **env})
        run("init", "-q", "-b", "work")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "T")
        (root / "app.py").write_text("print('v1')\n")
        run("add", "app.py")
        run("commit", "-qm", "init")
        yield root, run


def _env():
    import os
    return dict(os.environ)


class GitToolsTests(unittest.TestCase):
    def test_branch_and_clean(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            g.ensure_repo()
            self.assertEqual(g.current_branch(), "work")
            self.assertTrue(g.is_clean())
            g.require_non_default_branch()

    def test_default_branch_refused(self):
        with temp_repo() as (root, run):
            run("branch", "-m", "work", "main")
            g = GitTools(root)
            with self.assertRaises(GitError):
                g.require_non_default_branch()

    def test_diff_hash_changes_with_content(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            h0 = g.diff_hash()
            (root / "app.py").write_text("print('v2')\n")
            h1 = g.diff_hash()
            self.assertNotEqual(h0, h1)
            self.assertEqual(h1, g.diff_hash())  # stable

    def test_changed_paths_includes_tracked_and_untracked(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("changed\n")
            (root / "new.py").write_text("new\n")
            self.assertEqual(g.changed_paths(), ["app.py", "new.py"])

    def test_protected_hits(self):
        hits = GitTools.protected_hits(
            ["src/x.py", "agent_harness/graph.py", "agent.toml", "docs/y.md"],
            ["agent_harness/", "agent.toml", ".git/"],
        )
        self.assertEqual(hits, ["agent.toml", "agent_harness/graph.py"])
        self.assertEqual(GitTools.protected_hits(["src/x.py"], ["agent_harness/"]), [])

    def test_checkpoint_commits_explicit_pathspec(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("print('v2')\n")
            (root / "extra.txt").write_text("junk\n")
            sha = g.checkpoint("harness: task [iter 1]", pathspec=["app.py"])
            self.assertTrue(sha)
            # extra.txt was NOT staged/committed
            self.assertIn("extra.txt", g.untracked_paths())
            log = run("log", "--oneline", "-1").stdout
            self.assertIn("harness: task", log)

    def test_checkpoint_refuses_empty_pathspec(self):
        with temp_repo() as (root, run):
            with self.assertRaises(GitError):
                GitTools(root).checkpoint("m", pathspec=[])

    def test_checkpoint_refuses_protected_path(self):
        with temp_repo() as (root, run):
            (root / "agent.toml").write_text("x\n")
            g = GitTools(root)
            with self.assertRaises(GitError):
                g.checkpoint("m", pathspec=["agent.toml"], protected=["agent.toml"])

    def test_checkpoint_refuses_toplevel_mismatch(self):
        with temp_repo() as (root, run):
            sub = root / "sub"
            sub.mkdir()
            g = GitTools(sub)  # not the repo top-level
            with self.assertRaises(GitError):
                g.checkpoint("m", pathspec=["x"])

    def test_checkpoint_never_pushes_resets_or_add_all(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("v2\n")
            real = subprocess.run
            seen = []

            def spy(argv, *a, **kw):
                if argv and argv[0] == "git":
                    seen.append(argv[1:])
                return real(argv, *a, **kw)

            with mock.patch("agent_harness.git_tools.subprocess.run", side_effect=spy):
                g.checkpoint("m", pathspec=["app.py"])
            flat = [tok for call in seen for tok in call]
            self.assertNotIn("push", flat)
            self.assertNotIn("reset", flat)
            self.assertNotIn("-A", flat)
            self.assertNotIn("--all", flat)
            self.assertTrue(any(call[:1] == ["add"] and "--" in call for call in seen))


if __name__ == "__main__":
    unittest.main()
