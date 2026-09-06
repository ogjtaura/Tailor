import unittest

from agent_harness.state import (
    CheckResult,
    Finding,
    HarnessState,
    Status,
    StopReason,
    failure_fingerprint,
    is_stagnant,
    update_stagnation,
)


def _state(**kw):
    return HarnessState(objective="o", run_id="r", **kw)


class SerialisationTests(unittest.TestCase):
    def test_round_trip(self):
        s = _state(
            current_task="t",
            remaining_tasks=["a", "b"],
            iteration=3,
            check_results=[CheckResult(command="pytest", exit_code=1, stderr_tail="boom")],
            review_findings=[Finding(id="F1", description="d")],
            status=Status.VERIFYING,
            stop_reason=StopReason.REPAIR_LIMIT,
        )
        data = s.model_dump(mode="json")
        back = HarnessState.model_validate(data)
        self.assertEqual(back.iteration, 3)
        self.assertEqual(back.remaining_tasks, ["a", "b"])
        self.assertEqual(back.check_results[0].exit_code, 1)
        self.assertEqual(back.review_findings[0].id, "F1")
        self.assertEqual(back.status, Status.VERIFYING)
        self.assertEqual(back.stop_reason, StopReason.REPAIR_LIMIT)


class FingerprintTests(unittest.TestCase):
    def test_stable_and_sensitive(self):
        a = _state(check_results=[CheckResult(command="c", exit_code=1, stderr_tail="E1")])
        b = _state(check_results=[CheckResult(command="c", exit_code=1, stderr_tail="E1")])
        c = _state(check_results=[CheckResult(command="c", exit_code=1, stderr_tail="E2")])
        self.assertEqual(failure_fingerprint(a), failure_fingerprint(b))
        self.assertNotEqual(failure_fingerprint(a), failure_fingerprint(c))

    def test_passing_state_has_no_failing_checks(self):
        s = _state(check_results=[CheckResult(command="c", exit_code=0)])
        self.assertEqual(s.failing_checks(), [])


class StagnationTests(unittest.TestCase):
    def _failing(self):
        return _state(check_results=[CheckResult(command="c", exit_code=1, stderr_tail="same")])

    def test_accumulates_when_failure_and_diff_unchanged(self):
        s = self._failing()
        update_stagnation(s, new_diff_hash="h1")   # first failing pass
        self.assertEqual(s.stagnant_iterations, 0)
        s.check_results = [CheckResult(command="c", exit_code=1, stderr_tail="same")]
        update_stagnation(s, new_diff_hash="h1")   # identical again
        self.assertEqual(s.stagnant_iterations, 1)
        s.check_results = [CheckResult(command="c", exit_code=1, stderr_tail="same")]
        update_stagnation(s, new_diff_hash="h1")
        self.assertEqual(s.stagnant_iterations, 2)
        self.assertTrue(is_stagnant(s, max_stagnant=2))

    def test_resets_when_diff_moves(self):
        s = self._failing()
        update_stagnation(s, new_diff_hash="h1")
        s.check_results = [CheckResult(command="c", exit_code=1, stderr_tail="same")]
        update_stagnation(s, new_diff_hash="h1")
        self.assertEqual(s.stagnant_iterations, 1)
        s.check_results = [CheckResult(command="c", exit_code=1, stderr_tail="same")]
        update_stagnation(s, new_diff_hash="h2")   # diff changed
        self.assertEqual(s.stagnant_iterations, 0)

    def test_resets_when_checks_pass(self):
        s = self._failing()
        update_stagnation(s, new_diff_hash="h1")
        s.check_results = [CheckResult(command="c", exit_code=0)]
        update_stagnation(s, new_diff_hash="h1")
        self.assertEqual(s.stagnant_iterations, 0)
        self.assertEqual(s.repeated_failure_count, 0)


if __name__ == "__main__":
    unittest.main()
