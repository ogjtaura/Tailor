"""Git-ownership, expected-HEAD, no-op-writer and checkpoint-guard behaviour,
driven through the real Engine nodes with fake collaborators."""

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
from agent_harness.workers.claude import PlanResult, PlanTask


def one_task_plan():
    return PlanResult(tasks=[PlanTask(id="T1", title="do the thing", rationale="r")])


def checks(passing):
    def _run(commands, **kw):
        code = 0 if passing else 1
        return [CheckResult(command="c", exit_code=code, stderr_tail="" if passing else "boom")]
    return _run


class NoProgressTests(unittest.TestCase):
    def _engine(self, root, claude):
        cfg = make_config(root, agent={"max_iterations": 10, "max_repairs_per_task": 2,
                                       "max_stagnant_iterations": 9})
        git = FakeGit(root)
        return Engine(cfg, claude=claude, codex=FakeCodex(), git=git,
                      store=RecordingStore(root), printer=SpyPrinter()), git

    def test_noop_writer_cannot_produce_success(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            claude = FakeClaude(plan_result=one_task_plan(), on_edit=lambda _r: None)  # never edits
            eng, git = self._engine(root, claude)
            with mock.patch.object(graphmod.verifier, "run_checks", checks(True)):
                final = eng.run(base_state())
            self.assertEqual(final.stop_reason, StopReason.NO_PROGRESS)
            self.assertEqual(final.completed_tasks, [])
            self.assertEqual(final.commits, [])
            self.assertEqual(git.checkpoints, [])
            self.assertNotIn("checkpoint", eng.printer.visited)


class ExpectedHeadTests(unittest.TestCase):
    def _engine(self, root):
        cfg = make_config(root)
        git = FakeGit(root)
        eng = Engine(cfg, claude=FakeClaude(plan_result=one_task_plan()), codex=FakeCodex(),
                     git=git, store=RecordingStore(root), printer=SpyPrinter())
        return eng, git

    def test_writer_node_refuses_when_head_moved(self):
        with tempfile.TemporaryDirectory() as d:
            eng, git = self._engine(Path(d))
            st = base_state(current_task="t", expected_head="EXPECTED")
            git._head = "SOMETHING_ELSE"
            out = eng.implement(st)
            self.assertEqual(out.stop_reason, StopReason.EXPECTED_HEAD_MOVED)
            self.assertEqual(eng.claude.calls, [])  # writer never invoked

    def test_writer_node_refuses_unrelated_preexisting_changes(self):
        with tempfile.TemporaryDirectory() as d:
            eng, git = self._engine(Path(d))
            git.write("someone_elses.py", "not ours\n")
            st = base_state(current_task="t", expected_head=git.head(), owned_paths=[])
            out = eng.implement(st)
            self.assertEqual(out.stop_reason, StopReason.UNEXPECTED_WORKTREE_STATE)
            self.assertEqual(eng.claude.calls, [])

    def test_checkpoint_refuses_when_head_moved(self):
        with tempfile.TemporaryDirectory() as d:
            eng, git = self._engine(Path(d))
            git.set_changed(["src/x.py"])
            st = base_state(current_task="t", checks_passed=True, review_status="pass",
                            owned_paths=["src/x.py"], reviewed_paths=["src/x.py"],
                            expected_head="EXPECTED")
            git._head = "MOVED"
            out = eng.checkpoint(st)
            self.assertEqual(out.stop_reason, StopReason.EXPECTED_HEAD_MOVED)
            self.assertEqual(git.checkpoints, [])

    def test_checkpoint_refuses_detached_head(self):
        with tempfile.TemporaryDirectory() as d:
            eng, git = self._engine(Path(d))
            git.set_changed(["src/x.py"])
            git.set_detached(True)
            st = base_state(current_task="t", checks_passed=True, review_status="pass",
                            owned_paths=["src/x.py"], reviewed_paths=["src/x.py"],
                            expected_head=git.head())
            out = eng.checkpoint(st)
            self.assertEqual(out.stop_reason, StopReason.DETACHED_HEAD)
            self.assertEqual(git.checkpoints, [])

    def test_checkpoint_revalidates_branch(self):
        with tempfile.TemporaryDirectory() as d:
            eng, git = self._engine(Path(d))
            git.set_changed(["src/x.py"])
            git.set_branch("main")
            st = base_state(current_task="t", checks_passed=True, review_status="pass",
                            owned_paths=["src/x.py"], reviewed_paths=["src/x.py"],
                            expected_head=git.head())
            out = eng.checkpoint(st)
            self.assertIsNotNone(out.stop_reason)
            self.assertEqual(git.checkpoints, [])  # never committed onto main

    def test_checkpoint_refuses_unowned_changes(self):
        with tempfile.TemporaryDirectory() as d:
            eng, git = self._engine(Path(d))
            git.set_changed(["src/x.py"])
            git.write("stray.py", "not owned\n")
            st = base_state(current_task="t", checks_passed=True, review_status="pass",
                            owned_paths=["src/x.py"], reviewed_paths=["src/x.py"],
                            expected_head=git.head())
            out = eng.checkpoint(st)
            self.assertEqual(out.stop_reason, StopReason.UNEXPECTED_WORKTREE_STATE)
            self.assertEqual(git.checkpoints, [])

    def test_checkpoint_refuses_unreviewed_owned_path(self):
        with tempfile.TemporaryDirectory() as d:
            eng, git = self._engine(Path(d))
            git.set_changed(["src/x.py", "src/y.py"])
            st = base_state(current_task="t", checks_passed=True, review_status="pass",
                            owned_paths=["src/x.py", "src/y.py"], reviewed_paths=["src/x.py"],
                            expected_head=git.head())
            out = eng.checkpoint(st)
            self.assertEqual(out.stop_reason, StopReason.REVIEW_COVERAGE_GAP)
            self.assertEqual(git.checkpoints, [])

    def test_expected_head_advances_after_commit(self):
        with tempfile.TemporaryDirectory() as d:
            eng, git = self._engine(Path(d))
            git.set_changed(["src/x.py"])
            st = base_state(current_task="t", checks_passed=True, review_status="pass",
                            owned_paths=["src/x.py"], reviewed_paths=["src/x.py"],
                            expected_head=git.head(), remaining_tasks=["next task"])
            out = eng.checkpoint(st)
            self.assertEqual(len(git.checkpoints), 1)
            self.assertEqual(out.expected_head, git.head())
            self.assertEqual(out.completed_tasks, ["t"])
            self.assertEqual(out.next_node, "plan")


if __name__ == "__main__":
    unittest.main()
