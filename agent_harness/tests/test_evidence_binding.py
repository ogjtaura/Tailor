"""Findings 3-6, 10: verify/review evidence is bound to the EXACT
(candidate_tree_oid, candidate_commit_oid) pair. A PASS from candidate A must
never authorise a different candidate B, and the final CAS re-checks every Git
object relationship.
"""

import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from agent_harness.git_tools import GitTools
from agent_harness.graph import Engine
from agent_harness.persistence import Persistence
from agent_harness.state import HarnessState, StopReason, clear_candidate_evidence
from agent_harness.tests.helpers import FakeClaude, FakeCodex, SpyPrinter, make_config

PROTECTED = ["agent_harness/", "agent.toml", ".git/", ".agent/"]


@contextmanager
def repo():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True,
                                        capture_output=True, text=True)
        run("init", "-q", "-b", "agent-harness-v0")
        run("config", "user.email", "t@e.c"); run("config", "user.name", "T")
        (root / ".gitignore").write_text(".agent/\n")
        (root / "app.py").write_text("v0\n")
        run("add", "-A"); run("commit", "-qm", "init")
        yield root, run


def _candidate(g, root, content, *, extra=None):
    ref = "refs/heads/agent-harness-v0"
    parent = g.resolve_ref(ref)
    (root / "app.py").write_text(content)
    owned = ["app.py"]
    if extra:
        (root / extra).write_text("x\n")
        owned.append(extra)
    tree = g.freeze_candidate_tree(parent=parent, owned_paths=owned, protected=PROTECTED)
    a = ["agent-harness", "noreply@anthropic.com", "1700000000 +0000"]
    commit = g.commit_tree(tree_oid=tree, parent=parent, message="harness: t [iter 1] [run r]",
                           author=a, committer=a)
    return ref, parent, tree, commit, a


def _reviewed_state(root, run, content="candidate A\n", extra=None):
    g = GitTools(root)
    ref, parent, tree, commit, a = _candidate(g, root, content, extra=extra)
    st = HarnessState(
        objective="o", run_id="r", current_task="t", owned_paths=["app.py"],
        run_target_ref=ref,
        checkpoint_paths=["app.py"], checkpoint_target_ref=ref,
        checkpoint_expected_parent=parent, checkpoint_message="harness: t [iter 1] [run r]",
        checkpoint_author=a, checkpoint_committer=a,
        candidate_tree_oid=tree, candidate_commit_oid=commit,
        verified_tree_oid=tree, verified_commit_oid=commit,
        reviewed_tree_oid=tree, reviewed_commit_oid=commit,
        checks_passed=True, review_status="pass",
        expected_head=parent, baseline_head=parent,
        checkpoint_phase="reviewed", next_node="checkpoint",
    )
    return g, st, ref, parent, tree, commit


def _run_checkpoint(root, g, st):
    """Drive the checkpoint NODE directly - these tests deliberately hand it
    internally-inconsistent evidence, which the graph's state schema would
    reject before the node ever saw it."""
    eng = Engine(make_config(root, checks={"commands": ["true"]}),
                 claude=FakeClaude(), codex=FakeCodex(), git=g,
                 store=Persistence(root), printer=SpyPrinter())
    return eng.checkpoint(st)


class EvidenceCommitBindingTests(unittest.TestCase):
    def test_happy_path_checkpoints(self):
        with repo() as (root, run):
            g, st, ref, parent, tree, commit = _reviewed_state(root, run)
            final = _run_checkpoint(root, g, st)
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(g.resolve_ref(ref), commit)

    def test_reviewed_tree_matches_but_reviewed_commit_differs_refuses(self):
        with repo() as (root, run):
            g, st, ref, parent, tree, commit = _reviewed_state(root, run)
            st.reviewed_commit_oid = "0" * 40           # tree still matches A, commit does not
            final = _run_checkpoint(root, g, st)
            self.assertEqual(final.stop_reason, StopReason.REVIEW_INVALIDATED)
            self.assertEqual(g.resolve_ref(ref), parent)

    def test_verified_commit_differs_refuses(self):
        with repo() as (root, run):
            g, st, ref, parent, tree, commit = _reviewed_state(root, run)
            st.verified_commit_oid = "0" * 40
            final = _run_checkpoint(root, g, st)
            self.assertEqual(final.stop_reason, StopReason.REVIEW_INVALIDATED)
            self.assertEqual(g.resolve_ref(ref), parent)

    def test_checks_not_pass_refuses(self):
        with repo() as (root, run):
            g, st, ref, parent, tree, commit = _reviewed_state(root, run)
            st.checks_passed = False
            final = _run_checkpoint(root, g, st)
            self.assertEqual(final.stop_reason, StopReason.REVIEW_INVALIDATED)
            self.assertEqual(g.resolve_ref(ref), parent)


class CommitObjectRelationshipTests(unittest.TestCase):
    def test_persisted_commit_with_wrong_tree_fails(self):
        with repo() as (root, run):
            g, st, ref, parent, tree, commit = _reviewed_state(root, run)
            # a real commit whose tree is the parent's tree, not the candidate's
            other = subprocess.run(
                ["git", "commit-tree", f"{parent}^{{tree}}", "-p", parent, "-m", "x"],
                cwd=root, check=True, capture_output=True, text=True,
                env={"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x",
                     "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
                     "PATH": __import__("os").environ["PATH"]},
            ).stdout.strip()
            st.candidate_commit_oid = other
            st.verified_commit_oid = st.reviewed_commit_oid = other
            final = _run_checkpoint(root, g, st)
            self.assertEqual(final.stop_reason, StopReason.CANDIDATE_INVALID)
            self.assertEqual(g.resolve_ref(ref), parent)

    def test_persisted_commit_with_wrong_parent_fails(self):
        with repo() as (root, run):
            g, st, ref, parent, tree, commit = _reviewed_state(root, run)
            root_parent = subprocess.run(
                ["git", "commit-tree", tree, "-m", "no parent"],
                cwd=root, check=True, capture_output=True, text=True,
                env={"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x",
                     "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
                     "PATH": __import__("os").environ["PATH"]},
            ).stdout.strip()
            st.candidate_commit_oid = root_parent
            st.verified_commit_oid = st.reviewed_commit_oid = root_parent
            final = _run_checkpoint(root, g, st)
            self.assertEqual(final.stop_reason, StopReason.CANDIDATE_INVALID)
            self.assertEqual(g.resolve_ref(ref), parent)


class EvidenceInvalidationTests(unittest.TestCase):
    def test_clear_candidate_evidence_nulls_everything(self):
        with repo() as (root, run):
            _, st, *_ = _reviewed_state(root, run)
            clear_candidate_evidence(st)
            for f in ("candidate_tree_oid", "candidate_commit_oid", "verified_tree_oid",
                      "verified_commit_oid", "reviewed_tree_oid", "reviewed_commit_oid",
                      "review_status", "review_severity", "checks_passed"):
                self.assertIsNone(getattr(st, f), f)
            self.assertEqual(st.checkpoint_phase, "none")

    def test_repair_produced_B_cannot_reuse_A_evidence(self):
        """Candidate A is verified + reviewed. A repair then produces candidate B
        (different tree/commit). B carries A's stale verify/review OIDs -> the
        checkpoint must refuse until B is re-verified and re-reviewed."""
        with repo() as (root, run):
            g, st, ref, parent, tree_A, commit_A = _reviewed_state(root, run)
            # a genuinely different candidate B
            _, _, tree_B, commit_B, _ = _candidate(g, root, "candidate B - repaired\n")
            self.assertNotEqual(tree_A, tree_B)
            st.candidate_tree_oid = tree_B
            st.candidate_commit_oid = commit_B
            # evidence still points at A
            final = _run_checkpoint(root, g, st)
            self.assertEqual(final.stop_reason, StopReason.REVIEW_INVALIDATED)
            self.assertEqual(g.resolve_ref(ref), parent)
            self.assertNotIn(commit_B, final.commits)


if __name__ == "__main__":
    unittest.main()
