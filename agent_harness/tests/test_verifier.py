import unittest
from pathlib import Path

from agent_harness import verifier

HERE = Path(__file__).resolve().parent


class VerifierTests(unittest.TestCase):
    def test_passing_command(self):
        r = verifier.run_check("true", cwd=HERE)
        self.assertEqual(r.exit_code, 0)
        self.assertTrue(r.passed)

    def test_failing_command_records_nonzero(self):
        r = verifier.run_check("false", cwd=HERE)
        self.assertNotEqual(r.exit_code, 0)
        self.assertFalse(r.passed)

    def test_missing_binary_is_not_an_exception(self):
        r = verifier.run_check("this-binary-does-not-exist --x", cwd=HERE)
        self.assertEqual(r.exit_code, 127)

    def test_timeout_is_captured(self):
        r = verifier.run_check("sleep 5", cwd=HERE, timeout_s=1)
        self.assertEqual(r.exit_code, 124)
        self.assertIn("timed out", r.stderr_tail)

    def test_checks_passed_needs_all_zero(self):
        ok = verifier.run_checks(["true", "true"], cwd=HERE)
        mixed = verifier.run_checks(["true", "false"], cwd=HERE)
        self.assertTrue(verifier.checks_passed(ok))
        self.assertFalse(verifier.checks_passed(mixed))

    def test_summarise_mentions_failures(self):
        res = verifier.run_checks(["true", "false"], cwd=HERE)
        self.assertIn("FAILED", verifier.summarise(res))


if __name__ == "__main__":
    unittest.main()
