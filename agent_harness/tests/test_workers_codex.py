import json
import unittest
from pathlib import Path

from agent_harness.tests.helpers import make_config, worker_result
from agent_harness.workers.codex import CodexWorker

REPO = Path(__file__).resolve().parents[2]

VALID_VERDICT = {
    "verdict": "fail",
    "severity": "major",
    "findings": [
        {"id": "F1", "description": "off-by-one", "evidence": "line 12", "suggested_fix": "use <="}
    ],
}


def runner_factory(*, write_body=None, result=None):
    """Return a fake runner that optionally writes the --output-last-message file."""

    def _runner(argv, **kw):
        argv = list(argv)
        if write_body is not None and "--output-last-message" in argv:
            out = Path(argv[argv.index("--output-last-message") + 1])
            out.write_text(write_body)
        return result or worker_result()

    return _runner


class CodexReviewTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(REPO)

    def _worker(self, runner):
        return CodexWorker(self.cfg, executable="/bin/true", runner=runner)

    def test_valid_verdict_is_parsed(self):
        w = self._worker(runner_factory(write_body=json.dumps(VALID_VERDICT)))
        out = w.review(diff="d", check_results="ok")
        self.assertEqual(out.kind, "verdict")
        self.assertEqual(out.verdict.verdict, "fail")
        self.assertEqual(out.verdict.severity, "major")
        self.assertEqual(out.verdict.findings[0].id, "F1")

    def test_malformed_json_is_infra_error_not_a_fail(self):
        w = self._worker(runner_factory(write_body="{not json"))
        out = w.review(diff="d", check_results="ok")
        self.assertEqual(out.kind, "infra_error")
        self.assertIsNone(out.verdict)

    def test_schema_invalid_body_is_infra_error(self):
        w = self._worker(runner_factory(write_body=json.dumps({"verdict": "maybe"})))
        out = w.review(diff="d", check_results="ok")
        self.assertEqual(out.kind, "infra_error")

    def test_missing_output_file_is_infra_error(self):
        w = self._worker(runner_factory(write_body=None))  # never writes the file
        out = w.review(diff="d", check_results="ok")
        self.assertEqual(out.kind, "infra_error")

    def test_nonzero_exit_is_infra_error(self):
        w = self._worker(runner_factory(
            write_body=json.dumps(VALID_VERDICT),
            result=worker_result(ok=False, exit_code=1, classification="cli_error"),
        ))
        out = w.review(diff="d", check_results="ok")
        self.assertEqual(out.kind, "infra_error")
        self.assertEqual(out.classification, "cli_error")

    def test_timeout_is_infra_error(self):
        w = self._worker(runner_factory(
            result=worker_result(ok=False, exit_code=None, timed_out=True, classification="cli_error"),
        ))
        out = w.review(diff="d", check_results="ok")
        self.assertEqual(out.kind, "infra_error")
        self.assertIn("timed out", out.reason)

    def test_usage_limit_passes_through_classification(self):
        w = self._worker(runner_factory(
            result=worker_result(ok=False, exit_code=1, classification="usage_limit"),
        ))
        out = w.review(diff="d", check_results="ok")
        self.assertEqual(out.kind, "infra_error")
        self.assertEqual(out.classification, "usage_limit")

    def test_review_argv_is_read_only(self):
        seen = {}

        def spy(argv, **kw):
            seen["argv"] = list(argv)
            out = Path(list(argv)[list(argv).index("--output-last-message") + 1])
            out.write_text(json.dumps({"verdict": "pass", "severity": "none", "findings": []}))
            return worker_result()

        CodexWorker(self.cfg, executable="/bin/true", runner=spy).review(diff="d", check_results="c")
        argv = seen["argv"]
        self.assertIn("exec", argv)
        self.assertIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--output-schema", argv)

    def test_escalate_returns_text_or_none(self):
        w_ok = self._worker(runner_factory(result=worker_result(stdout="root cause: foo")))
        self.assertEqual(w_ok.escalate(diff="d", failures="f"), "root cause: foo")
        w_bad = self._worker(runner_factory(result=worker_result(ok=False, exit_code=1)))
        self.assertIsNone(w_bad.escalate(diff="d", failures="f"))


if __name__ == "__main__":
    unittest.main()
