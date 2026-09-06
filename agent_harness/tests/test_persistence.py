import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness.persistence import Persistence, _atomic_write, reconcile_for_resume
from agent_harness.state import HarnessState, StopReason
from agent_harness.tests.helpers import FakeGit


def _state(**kw):
    return HarnessState(objective="o", run_id="r1", **kw)


class AtomicWriteTests(unittest.TestCase):
    def test_round_trip_state(self):
        with tempfile.TemporaryDirectory() as d:
            store = Persistence(d)
            s = _state(iteration=4, remaining_tasks=["a"], harness_touched_files=["src/x.py"])
            store.init_run(s)
            store.save(s, note="hello")
            loaded = store.load()
            self.assertEqual(loaded.iteration, 4)
            self.assertEqual(loaded.remaining_tasks, ["a"])
            self.assertTrue(store.progress_path.read_text().strip())
            self.assertTrue(store.failures_path.is_file())

    def test_crash_mid_write_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "state.json"
            _atomic_write(target, '{"v": 1}')
            with mock.patch("agent_harness.persistence.os.replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    _atomic_write(target, '{"v": 2}')
            self.assertEqual(json.loads(target.read_text()), {"v": 1})
            # no leftover temp files
            self.assertEqual([p.name for p in Path(d).iterdir()], ["state.json"])


class ReconcileResumeTests(unittest.TestCase):
    def _git(self, root, changed):
        g = FakeGit(root)
        g.set_changed(changed)
        return g

    def test_write_worker_in_flight_routes_to_verify(self):
        with tempfile.TemporaryDirectory() as d:
            s = _state(worker_in_flight="claude_implement", harness_touched_files=["src/x.py"])
            g = self._git(d, ["src/x.py"])
            out = reconcile_for_resume(s, g, ["agent_harness/", ".git/"])
            self.assertIsNone(out.stop_reason)
            self.assertEqual(out.next_node, "verify")
            self.assertIsNone(out.worker_in_flight)

    def test_unexpected_change_stops(self):
        with tempfile.TemporaryDirectory() as d:
            s = _state(worker_in_flight="claude_repair", harness_touched_files=["src/x.py"])
            g = self._git(d, ["src/x.py", "src/surprise.py"])
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.UNEXPECTED_WORKTREE_STATE)

    def test_protected_change_stops(self):
        with tempfile.TemporaryDirectory() as d:
            s = _state(worker_in_flight="claude_implement", harness_touched_files=[])
            g = self._git(d, ["agent_harness/graph.py"])
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.PROTECTED_PATH_MODIFIED)

    def test_readonly_worker_reenters_review(self):
        with tempfile.TemporaryDirectory() as d:
            s = _state(worker_in_flight="codex_review")
            out = reconcile_for_resume(s, self._git(d, []), ["agent_harness/"])
            self.assertEqual(out.next_node, "review")
            self.assertIsNone(out.worker_in_flight)

    def test_no_worker_in_flight_uses_next_node(self):
        with tempfile.TemporaryDirectory() as d:
            s = _state(worker_in_flight=None, next_node="decide")
            out = reconcile_for_resume(s, self._git(d, []), ["agent_harness/"])
            self.assertEqual(out.next_node, "decide")


if __name__ == "__main__":
    unittest.main()
