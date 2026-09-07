import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from agent_harness.git_tools import GitError, GitTools


@contextmanager
def temp_repo(branch="work"):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True,
                                        capture_output=True, text=True)
        run("init", "-q", "-b", branch)
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "T")
        (root / "app.py").write_text("print('v1')\n")
        run("add", "app.py")
        run("commit", "-qm", "init")
        yield root, run


class IntrospectionTests(unittest.TestCase):
    def test_branch_ref_and_clean(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            g.ensure_repo()
            self.assertEqual(g.current_branch_ref(), "refs/heads/work")
            self.assertTrue(g.is_clean())
            self.assertEqual(g.resolve_ref("refs/heads/work"), g.head())
            self.assertIsNone(g.resolve_ref("refs/heads/nope"))

    def test_detached_head(self):
        with temp_repo() as (root, run):
            run("commit", "--allow-empty", "-qm", "second")
            run("checkout", "-q", "HEAD~1")
            g = GitTools(root)
            self.assertTrue(g.is_detached())
            with self.assertRaises(GitError):
                g.current_branch_ref()

    def test_changed_paths_tracked_and_untracked_nul_safe(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("changed\n")
            (root / "a file.py").write_text("x=1\n")
            self.assertEqual(g.changed_paths(), ["a file.py", "app.py"])

    def test_protected_hits(self):
        hits = GitTools.protected_hits(
            ["src/x.py", "agent_harness/graph.py", "agent.toml"],
            ["agent_harness/", "agent.toml", ".git/"],
        )
        self.assertEqual(hits, ["agent.toml", "agent_harness/graph.py"])

    def test_attributable_changes_ignores_unchanged_preexisting(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            (root / "pre.py").write_text("pre\n")
            before = g.snapshot()
            (root / "new.py").write_text("new\n")
            after = g.snapshot()
            self.assertEqual(g.attributable_changes(before, after), ["new.py"])


class ImmutableCandidateTests(unittest.TestCase):
    def _freeze(self, g, owned):
        parent = g.head()
        tree = g.freeze_candidate_tree(parent=parent, owned_paths=owned)
        author = ["h", "h@h", "100000000 +0000"]
        commit = g.commit_tree(tree_oid=tree, parent=parent, message="harness: t",
                               author=author, committer=author)
        return parent, tree, commit

    def test_candidate_tree_contains_exact_owned_change_and_new_file(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("v2\n")
            (root / "brand new.py").write_text("NEW=1\n")
            _, _, commit = self._freeze(g, ["app.py", "brand new.py"])
            self.assertEqual(run("show", f"{commit}:app.py").stdout, "v2\n")
            self.assertEqual(run("show", f"{commit}:brand new.py").stdout, "NEW=1\n")

    def test_candidate_excludes_unrelated_normal_index_staged_file(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("v2\n")
            (root / "foreign.py").write_text("NOT MINE\n")
            run("add", "foreign.py")                       # user's shared index
            _, _, commit = self._freeze(g, ["app.py"])     # owned = app.py only
            names = run("show", "--format=", "--name-only", commit).stdout.split()
            self.assertIn("app.py", names)
            self.assertNotIn("foreign.py", names)

    def test_candidate_includes_deletion(self):
        with temp_repo() as (root, run):
            (root / "gone.py").write_text("bye\n")
            run("add", "gone.py"); run("commit", "-qm", "add gone")
            g = GitTools(root)
            (root / "gone.py").unlink()
            _, _, commit = self._freeze(g, ["gone.py"])
            names = run("show", "--format=", "--name-status", commit).stdout
            self.assertIn("D\tgone.py", names)

    def test_candidate_binds_executable_mode(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            f = root / "app.py"
            f.write_text("print('v2')\n")
            os.chmod(f, 0o755)
            _, _, commit = self._freeze(g, ["app.py"])
            mode = run("ls-tree", commit, "app.py").stdout.split()[0]
            self.assertEqual(mode, "100755")

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_candidate_binds_symlink(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            link = root / "lnk"
            try:
                os.symlink("app.py", link)
            except (OSError, NotImplementedError):
                self.skipTest("symlink not permitted on this platform")
            _, _, commit = self._freeze(g, ["lnk"])
            mode = run("ls-tree", commit, "lnk").stdout.split()[0]
            self.assertEqual(mode, "120000")
            self.assertEqual(run("show", f"{commit}:lnk").stdout, "app.py")

    def test_commit_tree_is_deterministic_across_retries(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("v2\n")
            parent = g.head()
            tree = g.freeze_candidate_tree(parent=parent, owned_paths=["app.py"])
            a = ["h", "h@h", "100000000 +0000"]
            c1 = g.commit_tree(tree_oid=tree, parent=parent, message="m", author=a, committer=a)
            c2 = g.commit_tree(tree_oid=tree, parent=parent, message="m", author=a, committer=a)
            self.assertEqual(c1, c2)
            self.assertEqual(g.commit_tree_of(c1), tree)

    def test_materialize_yields_exact_candidate_and_is_stable_after_worktree_mutation(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("candidate\n")
            _, tree, commit = self._freeze(g, ["app.py"])
            with g.materialize(commit) as wt:
                self.assertEqual((wt / "app.py").read_text(), "candidate\n")
                (root / "app.py").write_text("MUTATED AFTER FREEZE\n")  # original worktree
                self.assertEqual((wt / "app.py").read_text(), "candidate\n")  # unchanged
            self.assertEqual(g.commit_tree_of(commit), tree)  # candidate object unchanged

    def test_mode_change_after_freeze_does_not_alter_the_candidate(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            f = root / "app.py"
            f.write_text("v2\n")                         # 100644
            _, tree, commit = self._freeze(g, ["app.py"])
            self.assertEqual(run("ls-tree", commit, "app.py").stdout.split()[0], "100644")
            import os as _os
            _os.chmod(f, 0o755)                          # worktree mode flips AFTER freeze
            # candidate object is immutable
            self.assertEqual(run("ls-tree", commit, "app.py").stdout.split()[0], "100644")
            self.assertEqual(g.commit_tree_of(commit), tree)

    def test_diff_tree_covers_modify_add_delete(self):
        with temp_repo() as (root, run):
            (root / "del.py").write_text("x\n")
            run("add", "del.py"); run("commit", "-qm", "c")
            g = GitTools(root)
            (root / "app.py").write_text("v2\n")
            (root / "new.py").write_text("brand new content\n")
            (root / "del.py").unlink()
            parent, _, commit = self._freeze(g, ["app.py", "new.py", "del.py"])
            d = g.diff_tree(parent, commit)
            self.assertIn("app.py", d)
            self.assertIn("brand new content", d)
            self.assertIn("del.py", d)

    def test_cas_update_ref_advances_only_when_old_matches(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("v2\n")
            parent, _, commit = self._freeze(g, ["app.py"])
            with self.assertRaises(GitError):
                g.cas_update_ref("refs/heads/work", commit, "0" * 40)  # wrong old
            self.assertEqual(g.resolve_ref("refs/heads/work"), parent)
            g.cas_update_ref("refs/heads/work", commit, parent)         # correct old
            self.assertEqual(g.resolve_ref("refs/heads/work"), commit)

    def test_freeze_refuses_protected_and_empty(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            with self.assertRaises(GitError):
                g.freeze_candidate_tree(parent=g.head(), owned_paths=[])
            (root / "agent.toml").write_text("x\n")
            with self.assertRaises(GitError):
                g.freeze_candidate_tree(parent=g.head(), owned_paths=["agent.toml"],
                                        protected=["agent.toml"])

    def test_never_pushes_resets_or_add_all(self):
        with temp_repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("v2\n")
            seen = []
            real = subprocess.run

            def spy(argv, *a, **kw):
                if argv and argv[0] == "git":
                    seen.append(list(argv[1:]))
                return real(argv, *a, **kw)

            with mock.patch("agent_harness.git_tools.subprocess.run", side_effect=spy):
                parent, _, commit = self._freeze(g, ["app.py"])
                g.cas_update_ref("refs/heads/work", commit, parent)
                g.sync_index_to(commit)
            flat = [t for call in seen for t in call]
            for banned in ("push", "reset", "rebase", "-A", "--force", "filter-branch"):
                self.assertNotIn(banned, flat)
            self.assertIn("update-ref", flat)          # CAS, not commit
            self.assertNotIn("commit", flat)


if __name__ == "__main__":
    unittest.main()
