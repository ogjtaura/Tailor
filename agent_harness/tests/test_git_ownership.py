"""Immutable-candidate content binding + no-op handling, driven end-to-end
through the real Engine against a REAL temporary Git repo.

Because the candidate tree/commit is frozen BEFORE verify and review, a
post-freeze working-tree / index mutation cannot change what gets committed -
the invariant is structural, not a pre-commit recheck.
"""

import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from agent_harness import graph as graphmod
from agent_harness.git_tools import GitTools
from agent_harness.graph import Engine
from agent_harness.persistence import Persistence
from agent_harness.state import CheckResult, HarnessState, StopReason
from agent_harness.tests.helpers import FakeClaude, FakeCodex, SpyPrinter, make_config
from agent_harness.workers.claude import PlanResult, PlanTask
from agent_harness.workers.codex import ReviewOutcome, ReviewVerdict
from unittest import mock


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
        (root / "tests").mkdir()
        (root / "tests/test_x.py").write_text("def test_ok():\n    assert True\n")
        run("add", "-A"); run("commit", "-qm", "init")
        yield root, run


def plan1():
    return PlanResult(tasks=[PlanTask(id="T1", title="do it", rationale="r")])


PASS = lambda: ReviewOutcome(kind="verdict", verdict=ReviewVerdict(verdict="pass"))


def checks(passing):
    def _run(commands, **kw):
        return [CheckResult(command="c", exit_code=0 if passing else 1)]
    return _run


def engine(root, *, claude, codex):
    return Engine(make_config(root, checks={"commands": ["true"]}),
                  claude=claude, codex=codex, git=GitTools(root),
                  store=Persistence(root), printer=SpyPrinter())


class ContentBindingTests(unittest.TestCase):
    def test_worktree_mutation_after_freeze_does_not_change_the_commit(self):
        with repo() as (root, run):
            def edit(_r):
                (root / "app.py").write_text("REVIEWED CONTENT\n")
            # Codex mutates the real working tree while "reviewing", then PASSes.
            def review(**kw):
                (root / "app.py").write_text("SNEAKY POST-REVIEW MUTATION\n")
                return PASS()
            codex = FakeCodex(); codex.review = review
            claude = FakeClaude(plan_result=plan1(), on_edit=edit)
            with mock.patch.object(graphmod.verifier, "run_checks", checks(True)):
                final = engine(root, claude=claude, codex=codex).run(
                    HarnessState(objective="o", run_id="r"))
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            tip = run("rev-parse", "refs/heads/agent-harness-v0").stdout.strip()
            self.assertEqual(run("show", f"{tip}:app.py").stdout, "REVIEWED CONTENT\n")
            self.assertEqual(final.commits, [tip])

    def test_foreign_index_staging_after_freeze_never_enters_commit(self):
        with repo() as (root, run):
            def edit(_r):
                (root / "app.py").write_text("owned change\n")
            def review(**kw):
                (root / "foreign.py").write_text("NOT MINE\n")
                run("add", "foreign.py")                 # stage into the shared index
                return PASS()
            codex = FakeCodex(); codex.review = review
            claude = FakeClaude(plan_result=plan1(), on_edit=edit)
            with mock.patch.object(graphmod.verifier, "run_checks", checks(True)):
                final = engine(root, claude=claude, codex=codex).run(
                    HarnessState(objective="o", run_id="r"))
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            tip = run("rev-parse", "refs/heads/agent-harness-v0").stdout.strip()
            names = run("show", "--format=", "--name-only", tip).stdout.split()
            self.assertIn("app.py", names)
            self.assertNotIn("foreign.py", names)

    def test_exec_mode_change_is_bound_into_the_candidate(self):
        with repo() as (root, run):
            def edit(_r):
                import os
                (root / "app.py").write_text("v1\n")
                os.chmod(root / "app.py", 0o755)
            claude = FakeClaude(plan_result=plan1(), on_edit=edit)
            with mock.patch.object(graphmod.verifier, "run_checks", checks(True)):
                final = engine(root, claude=claude, codex=FakeCodex(review_seq=[PASS()])).run(
                    HarnessState(objective="o", run_id="r"))
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            tip = run("rev-parse", "refs/heads/agent-harness-v0").stdout.strip()
            self.assertEqual(run("ls-tree", tip, "app.py").stdout.split()[0], "100755")


class NoProgressTests(unittest.TestCase):
    def test_noop_writer_never_completes_the_task(self):
        with repo() as (root, run):
            claude = FakeClaude(plan_result=plan1(), on_edit=lambda _r: None)  # edits nothing
            eng = Engine(
                make_config(root, checks={"commands": ["true"]},
                            agent={"max_iterations": 20, "max_repairs_per_task": 2}),
                claude=claude, codex=FakeCodex(), git=GitTools(root),
                store=Persistence(root), printer=SpyPrinter())
            with mock.patch.object(graphmod.verifier, "run_checks", checks(True)):
                final = eng.run(HarnessState(objective="o", run_id="r"))
            self.assertEqual(final.stop_reason, StopReason.NO_PROGRESS)
            self.assertEqual(final.completed_tasks, [])
            self.assertEqual(final.commits, [])
            tip = run("rev-parse", "refs/heads/agent-harness-v0").stdout.strip()
            self.assertEqual(run("rev-list", "--count", tip).stdout.strip(), "1")

    def test_writer_reverting_its_own_change_is_a_noop(self):
        with repo() as (root, run):
            calls = {"n": 0}
            def edit(_r):
                calls["n"] += 1
                # first attempt changes app.py, later "repairs" revert it to v0
                (root / "app.py").write_text("changed\n" if calls["n"] == 1 else "v0\n")
            eng = Engine(
                make_config(root, checks={"commands": ["true"]},
                            agent={"max_iterations": 20, "max_repairs_per_task": 2}),
                claude=FakeClaude(plan_result=plan1(), on_edit=edit),
                codex=FakeCodex(review_seq=[ReviewOutcome(kind="verdict",
                                 verdict=ReviewVerdict(verdict="fail", severity="major",
                                 findings=[__import__("agent_harness.workers.codex",
                                 fromlist=["ReviewFinding"]).ReviewFinding(id="F1", description="x")]))]),
                git=GitTools(root), store=Persistence(root), printer=SpyPrinter())
            with mock.patch.object(graphmod.verifier, "run_checks", checks(True)):
                final = eng.run(HarnessState(objective="o", run_id="r"))
            # after the revert, freeze sees candidate tree == parent tree -> no-op
            self.assertEqual(final.stop_reason, StopReason.NO_PROGRESS)
            self.assertEqual(final.commits, [])


if __name__ == "__main__":
    unittest.main()
