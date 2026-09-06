import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import graph as graphmod
from agent_harness.graph import Engine, recursion_budget
from agent_harness.state import CheckResult, StopReason
from agent_harness.workers.claude import WorkerUsageLimitError
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
from agent_harness.workers.codex import ReviewFinding, ReviewOutcome, ReviewVerdict


def one_task_plan():
    return PlanResult(tasks=[PlanTask(id="T1", title="do the thing", rationale="needed")])


def checks(*outcomes):
    """outcomes: iterable of bool (True=pass). Last value repeats forever."""
    seq = list(outcomes)

    def _run(commands, **kw):
        val = seq.pop(0) if len(seq) > 1 else seq[0]
        code = 0 if val else 1
        return [CheckResult(command="check", exit_code=code, stderr_tail="" if val else "boom")]

    return _run


def verdict(v, sev="none", findings=()):
    return ReviewOutcome(
        kind="verdict",
        verdict=ReviewVerdict(verdict=v, severity=sev, findings=list(findings)),
    )


INFRA = ReviewOutcome(kind="infra_error", reason="bad json", classification="cli_error")


class RoutingScenarioBase(unittest.TestCase):
    def build(self, *, agent=None, claude=None, codex=None, git_clean=True):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        cfg = make_config(root, agent=agent or {})
        self.git = FakeGit(root, clean=git_clean)
        self.claude = claude or FakeClaude(plan_result=one_task_plan(),
                                          on_edit=lambda _r: self.git.set_changed(["src/x.py"]))
        self.codex = codex or FakeCodex()
        self.printer = SpyPrinter()
        self.store = RecordingStore(root)
        return Engine(cfg, claude=self.claude, codex=self.codex, git=self.git,
                      store=self.store, printer=self.printer)

    def run_engine(self, engine, check_script, state=None):
        with mock.patch.object(graphmod.verifier, "run_checks", check_script):
            return engine.run(state or base_state())


class VerifyRepairRoutingTests(RoutingScenarioBase):
    def test_failed_check_routes_through_repair_then_reverify(self):
        eng = self.build()
        final = self.run_engine(eng, checks(False, True))
        v = self.printer.visited
        self.assertIn("repair", v)
        first_verify = v.index("verify")
        repair_at = v.index("repair")
        self.assertLess(first_verify, repair_at)
        self.assertIn("verify", v[repair_at + 1:])          # re-verified after repair
        self.assertEqual(final.stop_reason, StopReason.SUCCESS)
        self.assertEqual(self.claude.calls, ["plan", "implement", "repair"])

    def test_review_fail_verdict_routes_to_repair_not_end(self):
        fail = verdict("fail", "major", [ReviewFinding(id="F1", description="bug")])
        eng = self.build(codex=FakeCodex(review_seq=[fail, verdict("pass")]))
        final = self.run_engine(eng, checks(True))
        v = self.printer.visited
        self.assertIn("repair", v)
        self.assertLess(v.index("review"), v.index("repair"))
        self.assertEqual(final.stop_reason, StopReason.SUCCESS)


class CheckpointTests(RoutingScenarioBase):
    def test_happy_path_commits_once(self):
        eng = self.build()
        final = self.run_engine(eng, checks(True))
        self.assertEqual(len(self.git.checkpoints), 1)
        self.assertEqual(len(final.commits), 1)
        self.assertIn("do the thing", self.git.checkpoints[0]["message"])
        self.assertEqual(final.completed_tasks, ["do the thing"])
        self.assertEqual(final.stop_reason, StopReason.SUCCESS)


class StagnationTests(RoutingScenarioBase):
    def test_repeated_identical_failure_escalates_then_gives_up(self):
        # on_edit does NOT change the diff -> fingerprint + diff hash stay constant
        eng = self.build(agent={"max_iterations": 20, "max_repairs_per_task": 6,
                                "max_stagnant_iterations": 2},
                         claude=FakeClaude(plan_result=one_task_plan(), on_edit=lambda _r: None))
        final = self.run_engine(eng, checks(False))
        self.assertIn("escalation_review", self.printer.visited)
        self.assertIn("escalate", self.codex.calls)
        # P2: STAGNATION_UNRESOLVED is now wired (escalation did not break the loop)
        self.assertEqual(final.stop_reason, StopReason.STAGNATION_UNRESOLVED)


class LimitTests(RoutingScenarioBase):
    def test_iteration_limit_stops_before_next_repair(self):
        eng = self.build(agent={"max_iterations": 2, "max_repairs_per_task": 10,
                                "max_stagnant_iterations": 9},
                         claude=FakeClaude(plan_result=one_task_plan(), on_edit=lambda _r: None))
        final = self.run_engine(eng, checks(False))
        self.assertEqual(final.stop_reason, StopReason.ITERATION_LIMIT)
        self.assertEqual(final.iteration, 2)

    def test_dirty_worktree_refuses_to_start(self):
        eng = self.build(git_clean=False)
        final = self.run_engine(eng, checks(True))
        self.assertEqual(final.stop_reason, StopReason.DIRTY_WORKTREE)
        self.assertEqual(self.claude.calls, [])
        self.assertEqual(self.codex.calls, [])

    def test_protected_path_modification_stops_without_repair_or_commit(self):
        eng = self.build(claude=FakeClaude(
            plan_result=one_task_plan(),
            on_edit=lambda _r: self.git.set_changed(["agent_harness/graph.py"]),
        ))
        final = self.run_engine(eng, checks(True))
        self.assertEqual(final.stop_reason, StopReason.PROTECTED_PATH_MODIFIED)
        self.assertNotIn("repair", self.printer.visited)
        self.assertNotIn("verify", self.printer.visited)
        self.assertEqual(self.git.checkpoints, [])


class ReviewerInfraTests(RoutingScenarioBase):
    def test_malformed_reviewer_output_never_triggers_repair(self):
        eng = self.build(codex=FakeCodex(review_seq=[INFRA, INFRA, INFRA]))
        final = self.run_engine(eng, checks(True))
        self.assertEqual(final.stop_reason, StopReason.REVIEWER_ERROR)
        self.assertNotIn("repair", self.printer.visited)
        self.assertNotIn("repair", self.claude.calls)
        self.assertEqual(self.codex.calls, ["review", "review"])  # 1 try + 1 retry


class UsageLimitPropagationTests(RoutingScenarioBase):
    def test_planning_usage_limit_stops_before_any_paid_worker(self):
        claude = FakeClaude(plan_exc=WorkerUsageLimitError("planner limit"))
        eng = self.build(claude=claude)
        final = self.run_engine(eng, checks(True))
        self.assertEqual(final.stop_reason, StopReason.USAGE_LIMIT)
        self.assertEqual(claude.calls, ["plan"])          # no implement/repair
        self.assertEqual(self.codex.calls, [])            # no review/escalate

    def test_review_usage_limit_stops_before_repair(self):
        limited = ReviewOutcome(kind="infra_error", reason="rate limited",
                                classification="usage_limit")
        eng = self.build(codex=FakeCodex(review_seq=[limited]))
        final = self.run_engine(eng, checks(True))
        self.assertEqual(final.stop_reason, StopReason.USAGE_LIMIT)
        self.assertNotIn("repair", self.printer.visited)
        self.assertNotIn("repair", self.claude.calls)
        self.assertEqual(self.codex.calls, ["review"])

    def test_escalation_usage_limit_does_not_continue_into_repair(self):
        from agent_harness.workers.codex import EscalateOutcome
        eng = self.build(
            agent={"max_iterations": 20, "max_repairs_per_task": 9, "max_stagnant_iterations": 1},
            claude=FakeClaude(plan_result=one_task_plan(), on_edit=lambda _r: None),
            codex=FakeCodex(escalate_result=EscalateOutcome(root_cause=None,
                                                            classification="usage_limit")),
        )
        final = self.run_engine(eng, checks(False))
        self.assertEqual(final.stop_reason, StopReason.USAGE_LIMIT)
        self.assertIn("escalation_review", self.printer.visited)
        self.assertEqual(self.printer.visited[-1], "escalation_review")  # nothing after it
        self.assertEqual(self.claude.calls.count("repair"), 1)  # only the pre-escalation repair


class RecursionBudgetTests(RoutingScenarioBase):
    def test_budget_scales_with_config(self):
        from agent_harness.tests.helpers import make_config
        small = make_config(Path("/tmp"), agent={"max_iterations": 2}, codex={"review_retries": 0})
        big = make_config(Path("/tmp"), agent={"max_iterations": 40}, codex={"review_retries": 5})
        self.assertLess(recursion_budget(small), recursion_budget(big))
        self.assertGreater(recursion_budget(small), 2 * 9)  # comfortably above worst path

    def test_worst_case_path_terminates_without_graph_recursion_error(self):
        # tiny limits, everything fails: max repair cycles + stagnation + iteration cap
        eng = self.build(
            agent={"max_iterations": 4, "max_repairs_per_task": 3, "max_stagnant_iterations": 2},
            claude=FakeClaude(
                plan_result=PlanResult(tasks=[PlanTask(id=f"T{i}", title=f"task {i}", rationale="r")
                                              for i in range(3)]),
                on_edit=lambda _r: None,
            ),
            codex=FakeCodex(review_seq=[INFRA] * 50),
        )
        final = self.run_engine(eng, checks(False))
        self.assertIn(
            final.stop_reason,
            {StopReason.ITERATION_LIMIT, StopReason.REPAIR_LIMIT,
             StopReason.STAGNATION_UNRESOLVED, StopReason.NO_PROGRESS},
        )
        # if a GraphRecursionError had leaked, Engine.run would have set CLI_FATAL
        self.assertNotEqual(final.stop_reason, StopReason.CLI_FATAL)


class DecideBoundaryTests(unittest.TestCase):
    def _engine(self, max_iter):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        cfg = make_config(root, agent={"max_iterations": max_iter,
                                       "max_repairs_per_task": 5,
                                       "max_stagnant_iterations": 3})
        return Engine(cfg, claude=FakeClaude(), codex=FakeCodex(),
                      git=FakeGit(root), store=RecordingStore(root),
                      printer=SpyPrinter())

    def test_final_iteration_still_reviews_and_checkpoints(self):
        eng = self._engine(3)
        s = base_state(iteration=3, checks_passed=True, review_status="pending",
                       owned_paths=["src/x.py"], reviewed_paths=["src/x.py"])
        self.assertEqual(eng._decide_route(s), "review")
        self.assertIsNone(s.stop_reason)
        s.review_status = "pass"
        self.assertEqual(eng._decide_route(s), "checkpoint")
        self.assertIsNone(s.stop_reason)

    def test_final_iteration_with_failing_checks_stops(self):
        eng = self._engine(3)
        s = base_state(iteration=3, checks_passed=False, repair_attempts=0)
        self.assertEqual(eng._decide_route(s), "end")
        self.assertEqual(s.stop_reason, StopReason.ITERATION_LIMIT)

    def test_no_progress_at_iteration_limit_stops(self):
        eng = self._engine(3)
        s = base_state(iteration=3, checks_passed=True, owned_paths=[], no_progress_count=1)
        self.assertEqual(eng._decide_route(s), "end")
        self.assertEqual(s.stop_reason, StopReason.ITERATION_LIMIT)

    def test_no_progress_below_limits_routes_to_repair(self):
        eng = self._engine(9)
        s = base_state(iteration=2, checks_passed=True, owned_paths=[], no_progress_count=1)
        self.assertEqual(eng._decide_route(s), "repair")
        self.assertIsNone(s.stop_reason)

    def test_no_progress_exhausted_stops_with_no_progress(self):
        eng = self._engine(20)
        s = base_state(iteration=2, checks_passed=True, owned_paths=[], no_progress_count=5)
        self.assertEqual(eng._decide_route(s), "end")
        self.assertEqual(s.stop_reason, StopReason.NO_PROGRESS)


if __name__ == "__main__":
    unittest.main()
