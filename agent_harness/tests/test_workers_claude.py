import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from agent_harness.tests.helpers import make_config, worker_result
from agent_harness.workers import base
from agent_harness.workers.base import run_subprocess
from agent_harness.workers.claude import ClaudeWorker, PlanningError, WorkerUsageLimitError

REPO = Path(__file__).resolve().parents[2]


class BaseRunnerTests(unittest.TestCase):
    def _completed(self, code=0, out="", err=""):
        return subprocess.CompletedProcess(["x"], code, stdout=out, stderr=err)

    def test_success(self):
        with mock.patch.object(base.subprocess, "run", return_value=self._completed(0, "ok")):
            r = run_subprocess(["claude"], cwd=REPO, timeout_s=10)
        self.assertTrue(r.ok)
        self.assertEqual(r.classification, "ok")

    def test_failed_exit_code(self):
        with mock.patch.object(base.subprocess, "run", return_value=self._completed(2, "", "bad")):
            r = run_subprocess(["claude"], cwd=REPO, timeout_s=10)
        self.assertFalse(r.ok)
        self.assertEqual(r.exit_code, 2)
        self.assertEqual(r.classification, "cli_error")

    def test_timeout_returns_result_not_exception(self):
        with mock.patch.object(base.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)):
            r = run_subprocess(["claude"], cwd=REPO, timeout_s=1)
        self.assertTrue(r.timed_out)
        self.assertFalse(r.ok)
        self.assertIsNone(r.exit_code)

    def test_api_keys_stripped_from_child_env(self):
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw)
            return self._completed(0, "ok")

        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"}):
            with mock.patch.object(base.subprocess, "run", side_effect=fake_run):
                run_subprocess(["claude"], cwd=REPO, timeout_s=10)
        self.assertIn("env", seen)
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])
        self.assertNotIn("OPENAI_API_KEY", seen["env"])

    def test_usage_limit_classification(self):
        with mock.patch.object(base.subprocess, "run",
                               return_value=self._completed(1, "", "Error: Reached your usage limit")):
            r = run_subprocess(["claude"], cwd=REPO, timeout_s=10,
                               usage_limit_patterns=["reached your usage limit"])
        self.assertEqual(r.classification, "usage_limit")
        self.assertTrue(r.is_usage_limit)


class _Recorder:
    def __init__(self, result):
        self.result = result
        self.argv = None

    def __call__(self, argv, **kw):
        self.argv = list(argv)
        self.kw = kw
        return self.result


class ClaudeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(REPO)

    def _worker(self, result, *, max_turns=False):
        rec = _Recorder(result)
        w = ClaudeWorker(self.cfg, executable="/bin/true", runner=rec, max_turns_supported=max_turns)
        return w, rec

    def test_implement_argv_capabilities(self):
        w, rec = self._worker(worker_result(stdout=json.dumps({"result": "done", "is_error": False})))
        inv = w.implement(objective="o", task="t")
        self.assertTrue(inv.ok)
        argv = rec.argv
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("Edit", argv)
        self.assertIn("Write", argv)
        self.assertNotIn("Bash", argv)
        self.assertNotIn("WebFetch", argv)
        self.assertNotIn("--max-turns", argv)
        self.assertIn("--permission-prompts", argv)
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")

    def test_implement_passes_max_turns_when_supported(self):
        w, rec = self._worker(
            worker_result(stdout=json.dumps({"result": "x"})), max_turns=True
        )
        w.implement(objective="o", task="t")
        self.assertIn("--max-turns", rec.argv)

    def test_plan_argv_is_read_only_and_structured(self):
        plan_json = json.dumps({"tasks": [{"id": "T1", "title": "do a thing", "rationale": "why"}]})
        w, rec = self._worker(worker_result(stdout=json.dumps({"result": plan_json})))
        plan = w.plan(objective="o")
        self.assertEqual(plan.tasks[0].id, "T1")
        argv = rec.argv
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertIn("--json-schema", argv)
        self.assertIn("Read", argv)
        self.assertNotIn("Edit", argv)
        self.assertNotIn("Write", argv)
        self.assertNotIn("Bash", argv)

    def test_plan_invalid_output_raises_planning_error(self):
        w, _ = self._worker(worker_result(stdout=json.dumps({"result": "sorry, no JSON here"})))
        with self.assertRaises(PlanningError):
            w.plan(objective="o")

    def test_plan_schema_mismatch_raises(self):
        bad = json.dumps({"steps": ["not tasks"]})
        w, _ = self._worker(worker_result(stdout=json.dumps({"result": bad})))
        with self.assertRaises(PlanningError):
            w.plan(objective="o")

    def test_plan_cli_failure_raises_planning_error(self):
        w, _ = self._worker(worker_result(ok=False, exit_code=1, classification="cli_error"))
        with self.assertRaises(PlanningError):
            w.plan(objective="o")

    def test_plan_usage_limit_raises_worker_usage_limit(self):
        w, _ = self._worker(worker_result(ok=False, exit_code=1, classification="usage_limit"))
        with self.assertRaises(WorkerUsageLimitError):
            w.plan(objective="o")

    def test_structured_is_error_true_makes_invocation_not_ok(self):
        # exit 0, valid JSON, but is_error: true
        body = json.dumps({"result": "sorry", "is_error": True, "subtype": "error_during_execution"})
        w, _ = self._worker(worker_result(stdout=body, exit_code=0, ok=True))
        inv = w.implement(objective="o", task="t")
        self.assertFalse(inv.ok)
        self.assertTrue(inv.is_error)
        self.assertFalse(inv.is_usage_limit)

    def test_structured_usage_limit_subtype_is_usage_limit(self):
        body = json.dumps({"result": "", "is_error": True, "subtype": "usage_limit_reached"})
        w, _ = self._worker(worker_result(stdout=body, exit_code=0, ok=True))
        inv = w.implement(objective="o", task="t")
        self.assertFalse(inv.ok)
        self.assertTrue(inv.is_usage_limit)

    def test_unparseable_output_is_not_ok_even_on_exit_zero(self):
        w, _ = self._worker(worker_result(stdout="not json at all", exit_code=0, ok=True))
        inv = w.implement(objective="o", task="t")
        self.assertFalse(inv.parsed)
        self.assertFalse(inv.ok)

    def test_plan_is_error_true_raises_planning_error(self):
        body = json.dumps({"result": '{"tasks":[{"id":"T1","title":"x","rationale":"y"}]}',
                           "is_error": True, "subtype": "error_during_execution"})
        w, _ = self._worker(worker_result(stdout=body, exit_code=0, ok=True))
        with self.assertRaises(PlanningError):
            w.plan(objective="o")

    def test_plan_is_error_usage_subtype_raises_usage_limit(self):
        body = json.dumps({"result": "", "is_error": True, "subtype": "rate_limit"})
        w, _ = self._worker(worker_result(stdout=body, exit_code=0, ok=True))
        with self.assertRaises(WorkerUsageLimitError):
            w.plan(objective="o")


if __name__ == "__main__":
    unittest.main()
