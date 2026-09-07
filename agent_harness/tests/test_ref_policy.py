"""Round 5 release blockers, against REAL temporary Git repos:

* a symbolic feature ref (feature -> main / -> master / -> other) is NEVER a
  valid checkpoint target and the final CAS can never advance its referent;
* the checkpoint target cannot be substituted to another branch - only the ref
  authorized at run start (the run manifest) may move;
* current checked-out branch never selects the target.
"""

import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from agent_harness.git_tools import GitError, GitTools
from agent_harness.graph import Engine
from agent_harness.persistence import (
    Persistence,
    reconcile_for_resume,
    verify_run_authorization,
)
from agent_harness.state import HarnessState, StopReason
from agent_harness.tests.helpers import FakeClaude, FakeCodex, SpyPrinter, make_config

PROTECTED = ["agent_harness/", "agent.toml", ".git/", ".agent/"]
AUTHOR = ["agent-harness", "noreply@anthropic.com", "1700000000 +0000"]


@contextmanager
def repo(*extra_branches):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        run = lambda *a, **kw: subprocess.run(["git", *a], cwd=root, check=True,
                                              capture_output=True, text=True, **kw)
        run("init", "-q", "-b", "feature-original")
        run("config", "user.email", "t@e.c"); run("config", "user.name", "T")
        (root / ".gitignore").write_text(".agent/\n")
        (root / "app.py").write_text("v0\n")
        run("add", "-A"); run("commit", "-qm", "init")
        for b in ("main", "master", *extra_branches):
            run("branch", b)
        yield root, run


def _make_candidate(root, run, ref="refs/heads/feature-original", content="candidate\n"):
    g = GitTools(root)
    parent = g.resolve_ref(ref)
    (root / "app.py").write_text(content)
    tree = g.freeze_candidate_tree(parent=parent, owned_paths=["app.py"], protected=PROTECTED)
    commit = g.commit_tree(tree_oid=tree, parent=parent, message="harness: t [iter 1] [run r]",
                           author=AUTHOR, committer=AUTHOR)
    return g, parent, tree, commit


def _reviewed_state(root, run, *, run_target="refs/heads/feature-original",
                    checkpoint_target=None):
    g, parent, tree, commit = _make_candidate(root, run, ref=run_target)
    st = HarnessState(
        objective="o", run_id="r", current_task="t", owned_paths=["app.py"],
        run_target_ref=run_target,
        checkpoint_paths=["app.py"],
        checkpoint_target_ref=checkpoint_target or run_target,
        checkpoint_expected_parent=parent, checkpoint_message="harness: t [iter 1] [run r]",
        checkpoint_author=AUTHOR, checkpoint_committer=AUTHOR,
        candidate_tree_oid=tree, candidate_commit_oid=commit,
        verified_tree_oid=tree, verified_commit_oid=commit,
        reviewed_tree_oid=tree, reviewed_commit_oid=commit,
        checks_passed=True, review_status="pass",
        expected_head=parent, baseline_head=parent,
        checkpoint_phase="reviewed", next_node="checkpoint",
    )
    return g, st, parent, tree, commit


def _write_manifest(root, *, run_id="r", authorized="refs/heads/feature-original", oid=None):
    store = Persistence(root)
    s = HarnessState(objective="o", run_id=run_id)
    oid = oid or GitTools(root).resolve_ref("refs/heads/feature-original") or "0" * 40
    store.create_run_manifest(s, authorized_target_ref=authorized, initial_target_oid=oid)


# --------------------------------------------------------------------------
# 7 + 9  low-level: validate_checkpoint_target_ref / cas_update_ref
# --------------------------------------------------------------------------

class LowLevelTargetValidatorTests(unittest.TestCase):
    def test_direct_feature_ref_is_valid(self):
        with repo() as (root, run):
            g = GitTools(root)
            g.validate_checkpoint_target_ref("refs/heads/feature-original")  # no raise

    def test_bare_name_and_HEAD_and_tag_namespace_rejected(self):
        with repo() as (root, run):
            g = GitTools(root)
            for bad in ("feature-original", "HEAD", "refs/tags/v1", "refs/remotes/o/main", ""):
                with self.assertRaises(GitError):
                    g.validate_checkpoint_target_ref(bad)

    def test_main_and_master_rejected(self):
        with repo() as (root, run):
            g = GitTools(root)
            for bad in ("refs/heads/main", "refs/heads/master", "main", "master"):
                with self.assertRaises(GitError):
                    g.validate_checkpoint_target_ref(bad)

    def test_symbolic_ref_to_main_rejected(self):
        with repo() as (root, run):
            run("symbolic-ref", "refs/heads/feature-original", "refs/heads/main")
            g = GitTools(root)
            with self.assertRaises(GitError):
                g.validate_checkpoint_target_ref("refs/heads/feature-original")

    def test_symbolic_ref_to_other_feature_rejected(self):
        with repo("other-feature") as (root, run):
            run("symbolic-ref", "refs/heads/feature-original", "refs/heads/other-feature")
            g = GitTools(root)
            with self.assertRaises(GitError):
                g.validate_checkpoint_target_ref("refs/heads/feature-original")

    def test_cas_helper_refuses_symbolic_target_main_stays(self):
        with repo() as (root, run):
            g, parent, tree, commit = _make_candidate(root, run)
            main_before = run("rev-parse", "refs/heads/main").stdout.strip()
            run("symbolic-ref", "refs/heads/feature-original", "refs/heads/main")
            with self.assertRaises(GitError):
                g.cas_update_ref("refs/heads/feature-original", commit, parent)
            self.assertEqual(run("rev-parse", "refs/heads/main").stdout.strip(), main_before)

    def test_cas_helper_refuses_symbolic_target_master_stays(self):
        with repo() as (root, run):
            g, parent, tree, commit = _make_candidate(root, run)
            master_before = run("rev-parse", "refs/heads/master").stdout.strip()
            run("symbolic-ref", "refs/heads/feature-original", "refs/heads/master")
            with self.assertRaises(GitError):
                g.cas_update_ref("refs/heads/feature-original", commit, parent)
            self.assertEqual(run("rev-parse", "refs/heads/master").stdout.strip(), master_before)

    def test_cas_helper_direct_ref_succeeds_with_no_deref(self):
        with repo() as (root, run):
            g, parent, tree, commit = _make_candidate(root, run)
            seen = []
            real = subprocess.run

            def spy(argv, *a, **kw):
                if argv[:2] == ["git", "update-ref"]:
                    seen.append(argv)
                return real(argv, *a, **kw)

            import unittest.mock as m
            with m.patch("agent_harness.git_tools.subprocess.run", side_effect=spy):
                g.cas_update_ref("refs/heads/feature-original", commit, parent)
            self.assertEqual(g.resolve_ref("refs/heads/feature-original"), commit)
            self.assertTrue(seen and "--no-deref" in seen[0])   # the real primitive

    def test_direct_then_symbolic_race_cannot_advance_main(self):
        # target validated as direct, then flipped symbolic immediately before
        # the write. --no-deref means main can never move.
        with repo() as (root, run):
            g, parent, tree, commit = _make_candidate(root, run)
            main_before = run("rev-parse", "refs/heads/main").stdout.strip()
            real = GitTools.validate_checkpoint_target_ref

            def flip_then_validate(self, ref, *, expected_current=None):
                out = real(self, ref, expected_current=expected_current)
                subprocess.run(["git", "symbolic-ref", ref, "refs/heads/main"],
                               cwd=root, check=True, capture_output=True)
                return out

            import unittest.mock as m
            with m.patch.object(GitTools, "validate_checkpoint_target_ref", flip_then_validate):
                try:
                    g.cas_update_ref("refs/heads/feature-original", commit, parent)
                except GitError:
                    pass
            self.assertEqual(run("rev-parse", "refs/heads/main").stdout.strip(), main_before)


# --------------------------------------------------------------------------
# 9E / 10  full checkpoint node: symbolic + substitution
# --------------------------------------------------------------------------

class CheckpointNodeTargetTests(unittest.TestCase):
    def _engine(self, root, g):
        return Engine(make_config(root, checks={"commands": ["true"]}),
                      claude=FakeClaude(), codex=FakeCodex(), git=g,
                      store=Persistence(root), printer=SpyPrinter())

    def test_symbolic_run_target_blocks_checkpoint_main_untouched(self):
        with repo() as (root, run):
            g, st, parent, tree, commit = _reviewed_state(root, run)
            main_before = run("rev-parse", "refs/heads/main").stdout.strip()
            run("symbolic-ref", "refs/heads/feature-original", "refs/heads/main")
            final = self._engine(root, g).checkpoint(st)
            self.assertIn(final.stop_reason,
                          {StopReason.PROTECTED_BRANCH, StopReason.INVALID_CHECKPOINT_STATE})
            self.assertEqual(run("rev-parse", "refs/heads/main").stdout.strip(), main_before)
            self.assertEqual(final.commits, [])

    def test_checkpoint_target_substituted_to_other_feature_is_blocked(self):
        with repo("other-feature") as (root, run):
            # run authorized for feature-original; both branches at the same parent
            g, st, parent, tree, commit = _reviewed_state(
                root, run, run_target="refs/heads/feature-original")
            st.checkpoint_target_ref = "refs/heads/other-feature"   # the substitution
            other_before = run("rev-parse", "refs/heads/other-feature").stdout.strip()
            feat_before = run("rev-parse", "refs/heads/feature-original").stdout.strip()
            final = self._engine(root, g).checkpoint(st)
            self.assertEqual(final.stop_reason, StopReason.INVALID_CHECKPOINT_STATE)
            self.assertEqual(run("rev-parse", "refs/heads/other-feature").stdout.strip(), other_before)
            self.assertEqual(run("rev-parse", "refs/heads/feature-original").stdout.strip(), feat_before)
            self.assertEqual(final.commits, [])

    def test_checkpoint_advances_only_the_authorized_run_target(self):
        with repo("other-feature") as (root, run):
            g, st, parent, tree, commit = _reviewed_state(root, run)
            other_before = run("rev-parse", "refs/heads/other-feature").stdout.strip()
            final = self._engine(root, g).checkpoint(st)
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(g.resolve_ref("refs/heads/feature-original"), commit)
            self.assertEqual(run("rev-parse", "refs/heads/other-feature").stdout.strip(), other_before)


# --------------------------------------------------------------------------
# 12  resume: authorized-target identity preserved or fail closed
# --------------------------------------------------------------------------

class ResumeAuthorizationTests(unittest.TestCase):
    def test_manifest_mismatch_with_checkpoint_target_fails_closed(self):
        with repo("other-feature") as (root, run):
            _write_manifest(root, authorized="refs/heads/feature-original")
            store = Persistence(root)
            g, st, parent, tree, commit = _reviewed_state(root, run)
            st.checkpoint_target_ref = "refs/heads/other-feature"     # substitution
            reason = verify_run_authorization(st, store, g)
            self.assertEqual(reason, StopReason.RUN_AUTHORIZATION_INVALID)

    def test_manifest_authorizes_matching_state(self):
        with repo() as (root, run):
            _write_manifest(root, authorized="refs/heads/feature-original")
            store = Persistence(root)
            g, st, parent, tree, commit = _reviewed_state(root, run)
            reason = verify_run_authorization(st, store, g)
            self.assertIsNone(reason)
            self.assertEqual(st.run_target_ref, "refs/heads/feature-original")

    def test_resume_without_persisted_run_target_fails_closed(self):
        with repo() as (root, run):
            _write_manifest(root, authorized="refs/heads/feature-original")
            store = Persistence(root)
            g, st, *_ = _reviewed_state(root, run)
            st.run_target_ref = None                       # pre-manifest / corrupt state
            self.assertEqual(verify_run_authorization(st, store, g),
                             StopReason.RUN_AUTHORIZATION_INVALID)

    def test_no_manifest_fails_closed_missing(self):
        with repo() as (root, run):
            store = Persistence(root)
            g, st, *_ = _reviewed_state(root, run)
            self.assertEqual(verify_run_authorization(st, store, g),
                             StopReason.RUN_AUTHORIZATION_MISSING)

    def test_manifest_authorized_main_fails_closed(self):
        with repo() as (root, run):
            _write_manifest(root, authorized="refs/heads/main")
            store = Persistence(root)
            g, st, *_ = _reviewed_state(root, run)
            st.checkpoint_target_ref = None
            self.assertEqual(verify_run_authorization(st, store, g),
                             StopReason.PROTECTED_BRANCH)

    def test_symbolic_authorized_target_fails_closed_on_resume(self):
        with repo() as (root, run):
            _write_manifest(root, authorized="refs/heads/feature-original")
            store = Persistence(root)
            g, st, *_ = _reviewed_state(root, run)
            run("symbolic-ref", "refs/heads/feature-original", "refs/heads/main")
            self.assertEqual(verify_run_authorization(st, store, g),
                             StopReason.PROTECTED_BRANCH)

    def test_reconcile_rejects_checkpoint_target_ne_run_target(self):
        with repo("other-feature") as (root, run):
            g, st, *_ = _reviewed_state(root, run)
            st.run_target_ref = "refs/heads/feature-original"
            # can't build a HarnessState with the mismatch (model validator), so
            # mutate after construction
            st.checkpoint_target_ref = "refs/heads/other-feature"
            st.stop_reason = None
            out = reconcile_for_resume(st, g, PROTECTED)
            self.assertEqual(out.stop_reason, StopReason.INVALID_CHECKPOINT_STATE)

    def test_full_resume_symbolic_target_never_moves_main(self):
        with repo() as (root, run):
            _write_manifest(root, authorized="refs/heads/feature-original")
            g, st, parent, tree, commit = _reviewed_state(root, run)
            main_before = run("rev-parse", "refs/heads/main").stdout.strip()
            run("symbolic-ref", "refs/heads/feature-original", "refs/heads/main")
            st.stop_reason = None
            st2 = reconcile_for_resume(st, g, PROTECTED)
            if st2.stop_reason is None:
                eng = Engine(make_config(root, checks={"commands": ["true"]}),
                             claude=FakeClaude(), codex=FakeCodex(), git=GitTools(root),
                             store=Persistence(root), printer=SpyPrinter(),
                             resume_target=st2.next_node or "checkpoint")
                st2 = eng.run(st2)
            self.assertIsNotNone(st2.stop_reason)
            self.assertNotEqual(st2.stop_reason, StopReason.SUCCESS)
            self.assertEqual(run("rev-parse", "refs/heads/main").stdout.strip(), main_before)


if __name__ == "__main__":
    unittest.main()
