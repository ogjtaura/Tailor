import unittest

import pydantic

from agent_harness.state import (
    CheckResult,
    Finding,
    HarnessState,
    Status,
    StopReason,
    clear_candidate_evidence,
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


class CheckpointInvariantTests(unittest.TestCase):
    """Finding 11: impossible persisted checkpoint state is rejected on
    construction / load - never silently repaired."""

    def _ok(self, **kw):
        return _state(**kw)

    def test_valid_reviewed_state_constructs(self):
        self._ok(checkpoint_phase="reviewed", candidate_tree_oid="t", candidate_commit_oid="c",
                 verified_tree_oid="t", verified_commit_oid="c",
                 reviewed_tree_oid="t", reviewed_commit_oid="c")

    def test_commit_without_tree_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self._ok(candidate_commit_oid="c")

    def test_reviewed_commit_without_candidate_commit_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self._ok(candidate_tree_oid="t", reviewed_tree_oid="t", reviewed_commit_oid="c")

    def test_verified_commit_mismatch_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self._ok(candidate_tree_oid="t", candidate_commit_oid="c",
                     verified_tree_oid="t", verified_commit_oid="OTHER")

    def test_reviewed_tree_mismatch_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self._ok(candidate_tree_oid="t", candidate_commit_oid="c",
                     reviewed_tree_oid="OTHER", reviewed_commit_oid="c")

    def test_ref_updated_phase_without_candidate_commit_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self._ok(checkpoint_phase="ref_updated", candidate_tree_oid="t")

    def test_reviewed_phase_without_bound_pass_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self._ok(checkpoint_phase="reviewed", candidate_tree_oid="t", candidate_commit_oid="c",
                     verified_tree_oid="t", verified_commit_oid="c")

    def test_run_target_ref_main_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self._ok(run_target_ref="refs/heads/main")

    def test_checkpoint_target_ne_run_target_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self._ok(run_target_ref="refs/heads/feature-a",
                     checkpoint_target_ref="refs/heads/feature-b")

    def test_active_phase_with_protected_checkpoint_target_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self._ok(checkpoint_phase="candidate_frozen", candidate_tree_oid="t",
                     candidate_commit_oid="c", checkpoint_target_ref="refs/heads/master")

    def test_matching_run_and_checkpoint_target_ok(self):
        self._ok(run_target_ref="refs/heads/feat", checkpoint_target_ref="refs/heads/feat")

    def test_round_trips_through_json(self):
        s = _state(checkpoint_phase="reviewed", candidate_tree_oid="t", candidate_commit_oid="c",
                   verified_tree_oid="t", verified_commit_oid="c",
                   reviewed_tree_oid="t", reviewed_commit_oid="c")
        back = HarnessState.model_validate(s.model_dump(mode="json"))
        self.assertEqual(back.reviewed_commit_oid, "c")

    def test_clear_candidate_evidence_resets_all(self):
        s = _state(checkpoint_phase="reviewed", candidate_tree_oid="t", candidate_commit_oid="c",
                   verified_tree_oid="t", verified_commit_oid="c",
                   reviewed_tree_oid="t", reviewed_commit_oid="c",
                   checks_passed=True, review_status="pass")
        clear_candidate_evidence(s)
        self.assertEqual(s.checkpoint_phase, "none")
        self.assertIsNone(s.candidate_commit_oid)
        self.assertIsNone(s.reviewed_commit_oid)
        self.assertIsNone(s.checks_passed)


class StagnationTests(unittest.TestCase):
    def _failing(self):
        return _state(check_results=[CheckResult(command="c", exit_code=1, stderr_tail="same")])

    def test_accumulates_when_failure_and_diff_unchanged(self):
        s = self._failing()
        update_stagnation(s, candidate_tree_oid="h1")   # first failing pass
        self.assertEqual(s.stagnant_iterations, 0)
        s.check_results = [CheckResult(command="c", exit_code=1, stderr_tail="same")]
        update_stagnation(s, candidate_tree_oid="h1")   # identical again
        self.assertEqual(s.stagnant_iterations, 1)
        s.check_results = [CheckResult(command="c", exit_code=1, stderr_tail="same")]
        update_stagnation(s, candidate_tree_oid="h1")
        self.assertEqual(s.stagnant_iterations, 2)
        self.assertTrue(is_stagnant(s, max_stagnant=2))

    def test_resets_when_diff_moves(self):
        s = self._failing()
        update_stagnation(s, candidate_tree_oid="h1")
        s.check_results = [CheckResult(command="c", exit_code=1, stderr_tail="same")]
        update_stagnation(s, candidate_tree_oid="h1")
        self.assertEqual(s.stagnant_iterations, 1)
        s.check_results = [CheckResult(command="c", exit_code=1, stderr_tail="same")]
        update_stagnation(s, candidate_tree_oid="h2")   # diff changed
        self.assertEqual(s.stagnant_iterations, 0)

    def test_resets_when_checks_pass(self):
        s = self._failing()
        update_stagnation(s, candidate_tree_oid="h1")
        s.check_results = [CheckResult(command="c", exit_code=0)]
        update_stagnation(s, candidate_tree_oid="h1")
        self.assertEqual(s.stagnant_iterations, 0)
        self.assertEqual(s.repeated_failure_count, 0)


if __name__ == "__main__":
    unittest.main()
