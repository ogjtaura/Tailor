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
            harness_touched_files=["src/x.py"],
            iteration=2,
            next_node="verify",
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


if __name__ == "__main__":
    unittest.main()
