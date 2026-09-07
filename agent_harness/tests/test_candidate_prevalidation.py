"""Finding 5/11: a candidate whose parent != expected_parent (or whose object
types / tree are wrong) must fail closed BEFORE the deterministic checks run and
BEFORE any paid Codex review - not only at the final CAS.
"""

import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from agent_harness import graph as graphmod
from agent_harness.git_tools import GitError, GitTools
from agent_harness.graph import Engine
from agent_harness.persistence import Persistence
from agent_harness.state import CheckResult, HarnessState, StopReason
from agent_harness.tests.helpers import FakeClaude, FakeCodex, SpyPrinter, make_config

PROTECTED = ["agent_harness/", "agent.toml", ".git/", ".agent/"]
AUTHOR = ["agent-harness", "noreply@anthropic.com", "1700000000 +0000"]
REF = "refs/heads/agent-harness-v0"


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
        run("add", "-A"); run("commit", "-qm", "P")
        yield root, run


def _wrong_parent_candidate(root, run):
    """A candidate commit whose parent is Q, while expected_parent is P."""
    g = GitTools(root)
    P = g.resolve_ref(REF)
    run("commit", "--allow-empty", "-qm", "Q")
    Q = g.resolve_ref(REF)
    run("update-ref", REF, P)                      # move ref back to P
    (root / "app.py").write_text("candidate\n")
    tree = g.freeze_candidate_tree(parent=P, owned_paths=["app.py"], protected=PROTECTED)
    bad = g.commit_tree(tree_oid=tree, parent=Q, message="harness: t [iter 1] [run r]",
                        author=AUTHOR, committer=AUTHOR)   # parent Q, not P
    st = HarnessState(
        objective="o", run_id="r", current_task="t", owned_paths=["app.py"],
        run_target_ref=REF, checkpoint_paths=["app.py"], checkpoint_target_ref=REF,
        checkpoint_expected_parent=P, checkpoint_message="harness: t [iter 1] [run r]",
        checkpoint_author=AUTHOR, checkpoint_committer=AUTHOR,
        candidate_tree_oid=tree, candidate_commit_oid=bad,
        checks_passed=None, checkpoint_phase="candidate_frozen",
        expected_head=P, baseline_head=P,
    )
    Persistence(root).create_run_manifest(st, authorized_target_ref=REF, initial_target_oid=P)
    return g, st, P, Q, tree, bad


class WrongParentPreValidationTests(unittest.TestCase):
    def _engine(self, root, g, codex):
        return Engine(make_config(root, checks={"commands": ["true"]}),
                      claude=FakeClaude(), codex=codex, git=g,
                      store=Persistence(root), printer=SpyPrinter())

    def test_verify_node_rejects_wrong_parent_before_running_checks(self):
        with repo() as (root, run):
            g, st, P, Q, tree, bad = _wrong_parent_candidate(root, run)
            ran = []
            with mock.patch.object(graphmod.verifier, "run_checks",
                                   lambda *a, **k: ran.append(1) or [CheckResult(command="c", exit_code=0)]):
                final = self._engine(root, g, FakeCodex()).verify(st)
            self.assertEqual(final.stop_reason, StopReason.INVALID_CHECKPOINT_STATE)
            self.assertEqual(ran, [])                       # checks never executed
            self.assertIsNone(final.verified_commit_oid)

    def test_review_node_rejects_wrong_parent_before_calling_codex(self):
        with repo() as (root, run):
            g, st, P, Q, tree, bad = _wrong_parent_candidate(root, run)
            # pretend verify somehow marked it (it would not) to isolate review
            st.checkpoint_phase = "verified"
            st.verified_tree_oid = tree
            st.verified_commit_oid = bad
            st.checks_passed = True
            codex = FakeCodex()
            final = self._engine(root, g, codex).review(st)
            self.assertEqual(final.stop_reason, StopReason.INVALID_CHECKPOINT_STATE)
            self.assertEqual(codex.calls, [])              # no paid review
            self.assertIsNone(final.reviewed_commit_oid)

    def test_full_tail_wrong_parent_no_review_no_cas(self):
        with repo() as (root, run):
            g, st, P, Q, tree, bad = _wrong_parent_candidate(root, run)
            codex = FakeCodex()
            eng = Engine(make_config(root, checks={"commands": ["true"]}),
                         claude=FakeClaude(), codex=codex, git=g,
                         store=Persistence(root), printer=SpyPrinter(),
                         resume_target="verify")
            with mock.patch.object(graphmod.verifier, "run_checks",
                                   lambda *a, **k: [CheckResult(command="c", exit_code=0)]):
                final = eng.run(st)
            self.assertEqual(final.stop_reason, StopReason.INVALID_CHECKPOINT_STATE)
            self.assertEqual(codex.calls, [])
            self.assertEqual(final.commits, [])
            self.assertEqual(g.resolve_ref(REF), P)         # ref untouched

    def test_control_correct_parent_proceeds(self):
        with repo() as (root, run):
            g = GitTools(root)
            P = g.resolve_ref(REF)
            (root / "app.py").write_text("candidate\n")
            tree = g.freeze_candidate_tree(parent=P, owned_paths=["app.py"], protected=PROTECTED)
            good = g.commit_tree(tree_oid=tree, parent=P, message="harness: t [iter 1] [run r]",
                                 author=AUTHOR, committer=AUTHOR)
            st = HarnessState(
                objective="o", run_id="r", current_task="t", owned_paths=["app.py"],
                run_target_ref=REF, checkpoint_paths=["app.py"], checkpoint_target_ref=REF,
                checkpoint_expected_parent=P, checkpoint_message="harness: t [iter 1] [run r]",
                checkpoint_author=AUTHOR, checkpoint_committer=AUTHOR,
                candidate_tree_oid=tree, candidate_commit_oid=good,
                checkpoint_phase="candidate_frozen", expected_head=P, baseline_head=P,
            )
            Persistence(root).create_run_manifest(st, authorized_target_ref=REF, initial_target_oid=P)
            eng = Engine(make_config(root, checks={"commands": ["true"]}),
                         claude=FakeClaude(), codex=FakeCodex(), git=g,
                         store=Persistence(root), printer=SpyPrinter(),
                         resume_target="verify")
            with mock.patch.object(graphmod.verifier, "run_checks",
                                   lambda *a, **k: [CheckResult(command="c", exit_code=0)]):
                final = eng.run(st)
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(g.resolve_ref(REF), good)


if __name__ == "__main__":
    unittest.main()
