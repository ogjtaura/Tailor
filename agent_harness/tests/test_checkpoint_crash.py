"""Crash windows of the immutable-object checkpoint boundary, against a REAL
temporary Git repo. Resume decisions use EXACT ref/object identity - never a
heuristic on parent/message/paths.
"""

import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from agent_harness.git_tools import GitTools
from agent_harness.graph import Engine
from agent_harness.persistence import Persistence, reconcile_for_resume
from agent_harness.state import HarnessState, StopReason
from agent_harness.tests.helpers import FakeClaude, FakeCodex, SpyPrinter, make_config

PROTECTED = ["agent_harness/", "agent.toml", ".git/", ".agent/"]


@contextmanager
def repo():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        run = lambda *a, **kw: subprocess.run(["git", *a], cwd=root, check=True,
                                              capture_output=True, text=True, **kw)
        run("init", "-q", "-b", "agent-harness-v0")
        run("config", "user.email", "t@e.c"); run("config", "user.name", "T")
        (root / ".gitignore").write_text(".agent/\n")
        (root / "app.py").write_text("v0\n")
        run("add", "-A"); run("commit", "-qm", "init")
        yield root, run


def _frozen_candidate(root, run, *, message="harness: t [iter 1] [run testrun]"):
    g = GitTools(root)
    ref = "refs/heads/agent-harness-v0"
    parent = g.resolve_ref(ref)
    (root / "app.py").write_text("candidate content\n")
    (root / "added.py").write_text("new\n")
    owned = ["app.py", "added.py"]
    tree = g.freeze_candidate_tree(parent=parent, owned_paths=owned, protected=PROTECTED)
    author = ["agent-harness", "noreply@anthropic.com", "1700000000 +0000"]
    commit = g.commit_tree(tree_oid=tree, parent=parent, message=message,
                           author=author, committer=author)
    st = HarnessState(
        objective="o", run_id="testrun", current_task="t",
        owned_paths=owned, checkpoint_paths=owned,
        run_target_ref=ref,
        checkpoint_target_ref=ref, checkpoint_expected_parent=parent,
        checkpoint_message=message, checkpoint_author=author, checkpoint_committer=author,
        candidate_tree_oid=tree, candidate_commit_oid=commit,
        verified_tree_oid=tree, verified_commit_oid=commit,
        reviewed_tree_oid=tree, reviewed_commit_oid=commit,
        checks_passed=True, review_status="pass",
        expected_head=parent, baseline_head=parent,
        checkpoint_phase="reviewed", next_node="checkpoint",
    )
    Persistence(root).create_run_manifest(st, authorized_target_ref=ref, initial_target_oid=parent)
    return g, st, ref, parent, commit, tree


def _resume(root, st):
    st.stop_reason = None
    g = GitTools(root)
    st = reconcile_for_resume(st, g, PROTECTED)
    if st.stop_reason is not None:
        return st
    eng = Engine(make_config(root, checks={"commands": ["true"]}),
                 claude=FakeClaude(), codex=FakeCodex(), git=g,
                 store=Persistence(root), printer=SpyPrinter(),
                 resume_target=st.next_node or "decide")
    return eng.run(st)


class CheckpointCrashTests(unittest.TestCase):
    def test_crash_at_reviewed_completes_via_cas(self):
        with repo() as (root, run):
            g, st, ref, parent, commit, tree = _frozen_candidate(root, run)
            final = _resume(root, st)
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(g.resolve_ref(ref), commit)
            self.assertEqual(final.commits, [commit])
            self.assertEqual(final.completed_tasks, ["t"])

    def test_crash_after_ref_update_intent_retries_cas(self):
        with repo() as (root, run):
            g, st, ref, parent, commit, tree = _frozen_candidate(root, run)
            st.checkpoint_phase = "ref_update_intent"
            final = _resume(root, st)
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(g.resolve_ref(ref), commit)
            self.assertEqual(len(final.commits), 1)

    def test_crash_after_cas_before_persist_finishes_once_no_dup(self):
        with repo() as (root, run):
            g, st, ref, parent, commit, tree = _frozen_candidate(root, run)
            g.cas_update_ref(ref, commit, parent)
            st.checkpoint_phase = "ref_update_intent"
            n_before = int(run("rev-list", "--count", ref).stdout)
            final = _resume(root, st)
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(final.commits, [commit])
            self.assertEqual(int(run("rev-list", "--count", ref).stdout), n_before)

    def test_crash_at_ref_updated_finishes_bookkeeping_only(self):
        with repo() as (root, run):
            g, st, ref, parent, commit, tree = _frozen_candidate(root, run)
            g.cas_update_ref(ref, commit, parent)
            st.checkpoint_phase = "ref_updated"
            st.commits = [commit]
            n_before = int(run("rev-list", "--count", ref).stdout)
            final = _resume(root, st)
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(final.completed_tasks, ["t"])
            self.assertEqual(int(run("rev-list", "--count", ref).stdout), n_before)

    def test_foreign_ref_move_fails_closed_no_adoption(self):
        with repo() as (root, run):
            g, st, ref, parent, commit, tree = _frozen_candidate(root, run)
            (root / "app.py").write_text("someone else\n")
            run("commit", "-qam", "external")
            self.assertNotIn(g.resolve_ref(ref), (parent, commit))
            final = _resume(root, st)
            self.assertEqual(final.stop_reason, StopReason.REF_UPDATE_CONFLICT)
            self.assertEqual(final.completed_tasks, [])
            self.assertNotIn(commit, final.commits)

    def test_forged_commit_same_parent_and_message_different_tree_rejected(self):
        with repo() as (root, run):
            g, st, ref, parent, commit, tree = _frozen_candidate(root, run)
            fb = run("hash-object", "-w", "--stdin", input="forged\n").stdout.strip()
            mk = run("mktree", input=f"100644 blob {fb}\tapp.py\n").stdout.strip()
            forged = subprocess.run(
                ["git", "commit-tree", mk, "-p", parent, "-m", st.checkpoint_message],
                cwd=root, check=True, capture_output=True, text=True,
                env={**os.environ, "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x",
                     "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x"},
            ).stdout.strip()
            run("update-ref", ref, forged, parent)
            self.assertNotEqual(forged, commit)
            final = _resume(root, st)
            self.assertEqual(final.stop_reason, StopReason.REF_UPDATE_CONFLICT)
            self.assertNotIn(forged, final.commits)
            self.assertEqual(final.completed_tasks, [])

    def test_candidate_object_missing_is_candidate_invalid(self):
        with repo() as (root, run):
            g, st, ref, parent, commit, tree = _frozen_candidate(root, run)
            st.candidate_commit_oid = "0" * 40
            final = _resume(root, st)
            self.assertEqual(final.stop_reason, StopReason.CANDIDATE_INVALID)
            self.assertEqual(final.completed_tasks, [])

    def test_crash_at_candidate_frozen_before_commit_reruns_freeze(self):
        with repo() as (root, run):
            g, st, ref, parent, commit, tree = _frozen_candidate(root, run)
            st.checkpoint_phase = "candidate_frozen"
            st.candidate_commit_oid = None
            st.verified_tree_oid = st.reviewed_tree_oid = None
            st.verified_commit_oid = st.reviewed_commit_oid = None
            st.checks_passed = None; st.review_status = None
            out = reconcile_for_resume(st, GitTools(root), PROTECTED)
            self.assertIsNone(out.stop_reason)
            self.assertEqual(out.next_node, "freeze")

    # -- Finding 2: persisted tree identity survives a tree->commit crash -----

    def _tree_only_state(self, root, run):
        """State as it is persisted at crash boundary A: candidate_tree_oid +
        the full deterministic commit spec, but no candidate_commit_oid yet."""
        g, st, ref, parent, commit, tree = _frozen_candidate(root, run)
        st.candidate_commit_oid = None
        st.checkpoint_phase = "candidate_frozen"
        st.verified_tree_oid = st.reviewed_tree_oid = None
        st.verified_commit_oid = st.reviewed_commit_oid = None
        st.checks_passed = None
        st.review_status = None
        return g, st, ref, parent, tree

    def test_tree_frozen_then_worktree_mutated_resume_keeps_tree_A(self):
        with repo() as (root, run):
            g, st, ref, parent, tree_A = self._tree_only_state(root, run)
            # the working tree drifts to "B" after the freeze / before resume
            (root / "app.py").write_text("MUTATED to B after freeze\n")
            (root / "added.py").write_text("B\n")
            (root / "sneaked_in.py").write_text("attacker\n")
            final = _resume(root, st)
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            landed = g.resolve_ref(ref)
            self.assertEqual(g.commit_tree_of(landed), tree_A)          # tree A, not B
            self.assertEqual(run("show", f"{landed}:app.py").stdout, "candidate content\n")
            # "B" content never entered the candidate identity
            self.assertNotIn("sneaked_in.py",
                             run("show", "--name-only", "--format=", landed).stdout)

    def test_tree_frozen_with_missing_commit_metadata_fails_closed(self):
        with repo() as (root, run):
            g, st, ref, parent, tree_A = self._tree_only_state(root, run)
            st.checkpoint_author = None            # deterministic spec incomplete
            final = _resume(root, st)
            self.assertEqual(final.stop_reason, StopReason.INVALID_CHECKPOINT_STATE)
            self.assertEqual(g.resolve_ref(ref), parent)               # ref untouched
            self.assertEqual(final.completed_tasks, [])

    def test_tree_frozen_message_missing_fails_closed(self):
        with repo() as (root, run):
            g, st, ref, parent, tree_A = self._tree_only_state(root, run)
            st.checkpoint_message = None
            final = _resume(root, st)
            self.assertEqual(final.stop_reason, StopReason.INVALID_CHECKPOINT_STATE)
            self.assertEqual(g.resolve_ref(ref), parent)


if __name__ == "__main__":
    unittest.main()
