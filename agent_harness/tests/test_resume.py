import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import graph as graphmod
from agent_harness.graph import Engine
from agent_harness.state import CheckResult, StopReason
from agent_harness.tests.helpers import (
    FakeClaude,
    FakeCodex,
    FakeGit,
    RecordingStore,
    SpyPrinter,
    base_state,
    make_config,
)
from agent_harness.workers.codex import ReviewOutcome, ReviewVerdict


def passing_checks(commands, **kw):
    return [CheckResult(command="c", exit_code=0)]


class ResumeRoutingTests(unittest.TestCase):
    def _engine(self, resume_target, git):
        root = git.repo_root
        cfg = make_config(root)
        return Engine(
            cfg,
            claude=FakeClaude(plan_result=None, on_edit=lambda _r: None),
            codex=FakeCodex(review_seq=[ReviewOutcome(kind="verdict",
                                                      verdict=ReviewVerdict(verdict="pass"))]),
            git=git,
            store=RecordingStore(root),
            printer=SpyPrinter(),
            resume_target=resume_target,
        )

    def test_resume_from_verify_skips_bootstrap_cleancheck_and_plan_implement(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        git = FakeGit(Path(tmp.name), clean=False)   # dirty is OK on resume
        git.set_changed(["src/x.py"])
        eng = self._engine("verify", git)
        state = base_state(
            current_task="finish the thing",
            owned_paths=["src/x.py"],
            reviewed_paths=["src/x.py"],
            iteration=2,
            next_node="verify",
            baseline_head=git.head(),
            expected_head=git.head(),
        )
        with mock.patch.object(graphmod.verifier, "run_checks", passing_checks):
            final = eng.run(state)
        visited = eng.printer.visited
        self.assertNotIn("plan", visited)
        self.assertNotIn("implement", visited)
        self.assertEqual(visited[0], "bootstrap")
        self.assertEqual(visited[1], "verify")
        self.assertEqual(final.stop_reason, StopReason.SUCCESS)
        self.assertEqual(eng.claude.calls, [])  # no writer re-run

    def test_route_after_bootstrap_honours_resume_target(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        git = FakeGit(Path(tmp.name))
        eng = self._engine("review", git)
        s = base_state()
        self.assertEqual(eng._route_after_bootstrap(s), "review")
        s.stop_reason = StopReason.CLI_FATAL
        self.assertEqual(eng._route_after_bootstrap(s), "end")

    def test_first_write_crash_reenters_verify_not_implement(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        git = FakeGit(Path(tmp.name))
        git.set_changed(["src/x.py"])
        eng = self._engine("verify", git)
        state = base_state(current_task="t", worker_in_flight="claude_implement",
                           owned_paths=["src/x.py"], reviewed_paths=["src/x.py"],
                           iteration=1, next_node="verify",
                           baseline_head=git.head(), expected_head=git.head())
        with mock.patch.object(graphmod.verifier, "run_checks", passing_checks):
            final = eng.run(state)
        self.assertNotIn("implement", eng.printer.visited)
        self.assertEqual(eng.claude.calls, [])          # writer NOT replayed
        self.assertEqual(final.stop_reason, StopReason.SUCCESS)

    def test_planner_in_flight_crash_reruns_planner(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        git = FakeGit(Path(tmp.name))
        from agent_harness.workers.claude import PlanResult, PlanTask
        eng = self._engine("plan", git)
        eng.claude = FakeClaude(
            plan_result=PlanResult(tasks=[PlanTask(id="T1", title="do it", rationale="r")]),
            on_edit=lambda _r: git.set_changed(["src/x.py"]),
        )
        state = base_state(worker_in_flight="claude_plan", remaining_tasks=[], current_task=None,
                           next_node="plan", baseline_head=git.head(), expected_head=git.head())
        with mock.patch.object(graphmod.verifier, "run_checks", passing_checks):
            final = eng.run(state)
        self.assertEqual(eng.claude.calls[0], "plan")   # planner actually re-ran
        self.assertIn(final.stop_reason, (StopReason.SUCCESS, StopReason.ITERATION_LIMIT))

    def _checkpoint_ready_state(self, git):
        return base_state(
            current_task="t", checks_passed=True, review_status="pass",
            owned_paths=["src/x.py"], reviewed_paths=["src/x.py"],
            baseline_head="PARENT", expected_head="PARENT",
        )

    def test_resume_checkpoint_before_commit_retries_commit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        git = FakeGit(Path(tmp.name))
        git._head = "PARENT"
        git.set_changed(["src/x.py"])
        eng = self._engine("checkpoint", git)
        state = self._checkpoint_ready_state(git)
        state.checkpoint_status = "intended"
        state.checkpoint_task = "t"
        state.checkpoint_paths = ["src/x.py"]
        state.checkpoint_pre_head = "PARENT"
        state.next_node = "checkpoint"
        final = eng.run(state)
        self.assertEqual(len(git.checkpoints), 1)                 # commit was retried
        self.assertEqual(final.completed_tasks, ["t"])
        self.assertEqual(final.stop_reason, StopReason.SUCCESS)
        self.assertEqual(final.checkpoint_status, "none")

    def test_resume_checkpoint_after_commit_adopts_existing_commit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        git = FakeGit(Path(tmp.name))
        # the commit already happened before the crash; worktree is clean
        git._head = "CHILD"
        git._commits["CHILD"] = {"parents": ["PARENT"],
                                 "message": "harness: t [iter 1] [run testrun]"}
        eng = self._engine("checkpoint", git)
        state = self._checkpoint_ready_state(git)
        state.run_id = "testrun"
        state.checkpoint_status = "intended"
        state.checkpoint_task = "t"
        state.checkpoint_paths = ["src/x.py"]
        state.checkpoint_pre_head = "PARENT"
        state.next_node = "checkpoint"
        final = eng.run(state)
        self.assertEqual(git.checkpoints, [])                     # NO second commit
        self.assertIn("CHILD", final.commits)
        self.assertEqual(final.completed_tasks, ["t"])
        self.assertEqual(final.expected_head, "CHILD")
        self.assertEqual(final.stop_reason, StopReason.SUCCESS)


if __name__ == "__main__":
    unittest.main()
