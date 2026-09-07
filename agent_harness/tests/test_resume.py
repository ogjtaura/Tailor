"""Resume routing: bootstrap honours the resume target, an interrupted writer
is never replayed (it re-enters at `freeze`), and a crashed planner re-runs.

Deep checkpoint crash-window coverage lives in test_checkpoint_crash.py.
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
from agent_harness.tests.helpers import FakeClaude, FakeCodex, SpyPrinter, base_state, make_config
from agent_harness.workers.claude import PlanResult, PlanTask
from agent_harness.workers.codex import ReviewOutcome, ReviewVerdict

PROTECTED = ["agent_harness/", "agent.toml", ".git/", ".agent/"]
PASS = lambda: ReviewOutcome(kind="verdict", verdict=ReviewVerdict(verdict="pass"))


def checks_ok(commands, **kw):
    return [CheckResult(command="c", exit_code=0)]


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


def _engine(root, *, claude, codex, resume_target=None):
    return Engine(make_config(root, checks={"commands": ["true"]}),
                  claude=claude, codex=codex, git=GitTools(root),
                  store=Persistence(root), printer=SpyPrinter(), resume_target=resume_target)


class BootstrapResumeTests(unittest.TestCase):
    def test_route_after_bootstrap_honours_resume_target(self):
        with repo() as (root, run):
            eng = _engine(root, claude=FakeClaude(), codex=FakeCodex(), resume_target="review")
            s = base_state()
            self.assertEqual(eng._route_after_bootstrap(s), "review")
            s.stop_reason = StopReason.CLI_FATAL
            self.assertEqual(eng._route_after_bootstrap(s), "end")


class InterruptedWriterResumeTests(unittest.TestCase):
    def test_interrupted_writer_with_partial_edit_fails_closed_no_replay(self):
        with repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("half-applied by the crashed writer\n")
            state = HarnessState(
                objective="o", run_id="r", current_task="t",
                worker_in_flight="claude_implement",
                owned_paths=[], pre_writer_paths=[], pre_writer_hashes={},
                expected_head=g.head(), baseline_head=g.head(),
            )
            out = reconcile_for_resume(state, g, PROTECTED)
            self.assertEqual(out.stop_reason, StopReason.INTERRUPTED_WRITE_UNATTRIBUTABLE)
            self.assertIsNone(out.next_node)

    def test_interrupted_writer_no_mutation_reenters_freeze(self):
        with repo() as (root, run):
            g = GitTools(root)
            state = HarnessState(
                objective="o", run_id="r", current_task="t",
                worker_in_flight="claude_repair", owned_paths=["app.py"],
                pre_writer_paths=[], pre_writer_hashes={},
                expected_head=g.head(), baseline_head=g.head(),
            )
            out = reconcile_for_resume(state, g, PROTECTED)
            self.assertIsNone(out.stop_reason)
            self.assertEqual(out.next_node, "freeze")
            self.assertIsNone(out.worker_in_flight)


class PlannerResumeTests(unittest.TestCase):
    def test_planner_in_flight_crash_reruns_planner_and_completes(self):
        with repo() as (root, run):
            claude = FakeClaude(
                plan_result=PlanResult(tasks=[PlanTask(id="T1", title="do it", rationale="r")]),
                on_edit=lambda _r: (root / "app.py").write_text("done\n"),
            )
            g = GitTools(root)
            state = HarnessState(objective="o", run_id="r", worker_in_flight="claude_plan",
                                 run_target_ref="refs/heads/agent-harness-v0",
                                 expected_head=g.head(), baseline_head=g.head())
            Persistence(root).create_run_manifest(
                state, authorized_target_ref="refs/heads/agent-harness-v0",
                initial_target_oid=g.head())
            out = reconcile_for_resume(state, g, PROTECTED)
            self.assertEqual(out.next_node, "plan")
            eng = _engine(root, claude=claude, codex=FakeCodex(review_seq=[PASS()]),
                          resume_target="plan")
            with mock.patch.object(graphmod.verifier, "run_checks", checks_ok):
                final = eng.run(out)
            self.assertEqual(claude.calls[0], "plan")
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(final.commits[0],
                             run("rev-parse", "refs/heads/agent-harness-v0").stdout.strip())

    def test_resume_at_freeze_runs_full_tail_to_success(self):
        with repo() as (root, run):
            g = GitTools(root)
            (root / "app.py").write_text("owned edit\n")     # the interrupted writer's change
            state = HarnessState(
                objective="o", run_id="r", current_task="t",
                run_target_ref="refs/heads/agent-harness-v0",
                owned_paths=["app.py"], iteration=1, next_node="freeze",
                expected_head=g.head(), baseline_head=g.head(), last_completed_node="implement",
            )
            Persistence(root).create_run_manifest(
                state, authorized_target_ref="refs/heads/agent-harness-v0",
                initial_target_oid=g.head())
            eng = _engine(root, claude=FakeClaude(), codex=FakeCodex(review_seq=[PASS()]),
                          resume_target="freeze")
            with mock.patch.object(graphmod.verifier, "run_checks", checks_ok):
                final = eng.run(state)
            visited = eng.printer.visited
            self.assertNotIn("plan", visited)
            self.assertNotIn("implement", visited)
            self.assertEqual(visited[1], "freeze")
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            self.assertEqual(final.completed_tasks, ["t"])


if __name__ == "__main__":
    unittest.main()
