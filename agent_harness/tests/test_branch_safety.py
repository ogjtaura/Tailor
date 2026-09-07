"""The checkpoint CAS targets the PERSISTED feature-branch ref, never whatever
branch happens to be checked out. An external switch to main can never cause a
commit/update to main.
"""

import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from agent_harness import graph as graphmod
from agent_harness.git_tools import GitTools
from agent_harness.graph import Engine
from agent_harness.persistence import Persistence, reconcile_for_resume
from agent_harness.state import CheckResult, HarnessState, StopReason
from agent_harness.tests.helpers import FakeClaude, FakeCodex, SpyPrinter, make_config
from agent_harness.workers.claude import PlanResult, PlanTask
from agent_harness.workers.codex import ReviewOutcome, ReviewVerdict

PROTECTED = ["agent_harness/", "agent.toml", ".git/", ".agent/"]
PASS = lambda: ReviewOutcome(kind="verdict", verdict=ReviewVerdict(verdict="pass"))


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
        run("branch", "main")                       # a 'main' also exists
        yield root, run


def _frozen(root, run):
    g = GitTools(root)
    ref = "refs/heads/agent-harness-v0"
    parent = g.resolve_ref(ref)
    (root / "app.py").write_text("candidate\n")
    tree = g.freeze_candidate_tree(parent=parent, owned_paths=["app.py"], protected=PROTECTED)
    a = ["agent-harness", "noreply@anthropic.com", "1700000000 +0000"]
    commit = g.commit_tree(tree_oid=tree, parent=parent, message="harness: t [iter 1] [run r]",
                           author=a, committer=a)
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
    Persistence(root).create_run_manifest(st, authorized_target_ref=ref, initial_target_oid=parent)
    return g, st, ref, parent, commit


class BranchSafetyTests(unittest.TestCase):
    def test_checkpoint_after_external_switch_to_main_updates_only_feature_ref(self):
        with repo() as (root, run):
            g, st, ref, parent, commit = _frozen(root, run)
            main_before = run("rev-parse", "main").stdout.strip()
            run("checkout", "-q", "main")                   # user switches away mid-run
            st.stop_reason = None
            st = reconcile_for_resume(st, g, PROTECTED)
            eng = Engine(make_config(root, checks={"commands": ["true"]}),
                         claude=FakeClaude(), codex=FakeCodex(), git=g,
                         store=Persistence(root), printer=SpyPrinter(),
                         resume_target=st.next_node or "checkpoint")
            final = eng.run(st)
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(g.resolve_ref(ref), commit)                       # feature advanced
            self.assertEqual(run("rev-parse", "main").stdout.strip(), main_before)  # main untouched
            self.assertEqual(run("rev-parse", "HEAD").stdout.strip(), main_before) # checkout untouched

    def _run_on_branch(self, root, run, branch, *, branch_required):
        if branch != "agent-harness-v0":
            run("checkout", "-q", branch)
        before = run("rev-parse", branch).stdout.strip()
        claude = FakeClaude(
            plan_result=PlanResult(tasks=[PlanTask(id="T1", title="t", rationale="r")]),
            on_edit=lambda _r: (root / "app.py").write_text("x\n"),
        )
        cfg = make_config(root, checks={"commands": ["true"]},
                          git={"branch_required": branch_required})
        eng = Engine(cfg, claude=claude, codex=FakeCodex(), git=GitTools(root),
                     store=Persistence(root), printer=SpyPrinter())
        with mock.patch.object(graphmod.verifier, "run_checks",
                               lambda c, **k: [CheckResult(command="c", exit_code=0)]):
            final = eng.run(HarnessState(objective="o", run_id="r"))
        return final, before

    def test_main_with_branch_required_true_is_blocked(self):
        with repo() as (root, run):
            final, before = self._run_on_branch(root, run, "main", branch_required=True)
            self.assertEqual(final.stop_reason, StopReason.PROTECTED_BRANCH)
            self.assertEqual(run("rev-parse", "main").stdout.strip(), before)

    def test_main_with_branch_required_false_is_STILL_blocked(self):
        with repo() as (root, run):
            final, before = self._run_on_branch(root, run, "main", branch_required=False)
            self.assertEqual(final.stop_reason, StopReason.PROTECTED_BRANCH)
            self.assertEqual(run("rev-parse", "main").stdout.strip(), before)

    def test_master_with_branch_required_false_is_STILL_blocked(self):
        with repo() as (root, run):
            run("branch", "master")
            final, before = self._run_on_branch(root, run, "master", branch_required=False)
            self.assertEqual(final.stop_reason, StopReason.PROTECTED_BRANCH)
            self.assertEqual(run("rev-parse", "master").stdout.strip(), before)

    def test_persisted_resume_target_main_fails_closed(self):
        with repo() as (root, run):
            g = GitTools(root)
            st = HarnessState(objective="o", run_id="r", current_task="t",
                              checkpoint_phase="none",
                              checkpoint_target_ref="refs/heads/main")
            st.stop_reason = None
            out = reconcile_for_resume(st, g, PROTECTED)
            self.assertEqual(out.stop_reason, StopReason.PROTECTED_BRANCH)
            self.assertIsNone(out.next_node)

    def test_cas_helper_refuses_main_and_master_directly(self):
        with repo() as (root, run):
            g = GitTools(root)
            parent = g.resolve_ref("refs/heads/main")
            (root / "app.py").write_text("candidate\n")
            tree = g.freeze_candidate_tree(parent=parent, owned_paths=["app.py"], protected=PROTECTED)
            a = ["agent-harness", "noreply@anthropic.com", "1700000000 +0000"]
            commit = g.commit_tree(tree_oid=tree, parent=parent, message="m", author=a, committer=a)
            for ref in ("refs/heads/main", "main", "refs/heads/master", "master"):
                with self.assertRaises(Exception):
                    g.cas_update_ref(ref, commit, parent)
            self.assertEqual(g.resolve_ref("refs/heads/main"), parent)

    def test_checked_out_main_but_feature_target_persisted_never_touches_main(self):
        # Finding 1.4: the CAS advances ONLY the persisted feature ref even when
        # the user has main checked out.
        with repo() as (root, run):
            g, st, ref, parent, commit = _frozen(root, run)
            main_before = run("rev-parse", "main").stdout.strip()
            run("checkout", "-q", "main")
            st.stop_reason = None
            st = reconcile_for_resume(st, g, PROTECTED)
            eng = Engine(make_config(root, checks={"commands": ["true"]}),
                         claude=FakeClaude(), codex=FakeCodex(), git=g,
                         store=Persistence(root), printer=SpyPrinter(),
                         resume_target=st.next_node or "checkpoint")
            final = eng.run(st)
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(g.resolve_ref(ref), commit)
            self.assertEqual(run("rev-parse", "main").stdout.strip(), main_before)

    def test_feature_ref_moved_externally_before_cas_conflicts(self):
        with repo() as (root, run):
            g, st, ref, parent, commit = _frozen(root, run)
            run("commit", "-qam", "external advance of feature")   # ref != parent, != candidate
            st.stop_reason = None
            st = reconcile_for_resume(st, g, PROTECTED)
            if st.stop_reason is None:
                eng = Engine(make_config(root, checks={"commands": ["true"]}),
                             claude=FakeClaude(), codex=FakeCodex(), git=g,
                             store=Persistence(root), printer=SpyPrinter(),
                             resume_target=st.next_node or "checkpoint")
                st = eng.run(st)
            self.assertEqual(st.stop_reason, StopReason.REF_UPDATE_CONFLICT)
            self.assertNotIn(commit, st.commits)


if __name__ == "__main__":
    unittest.main()
