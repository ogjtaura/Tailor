import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness.persistence import (
    Persistence,
    RunLock,
    RunLockError,
    StateCorruptError,
    _atomic_write,
    reconcile_for_resume,
)
from agent_harness.state import HarnessState, StopReason
from agent_harness.tests.helpers import FakeGit


def _state(**kw):
    return HarnessState(objective="o", run_id="r1", **kw)


class AtomicWriteTests(unittest.TestCase):
    def test_round_trip_state(self):
        with tempfile.TemporaryDirectory() as d:
            store = Persistence(d)
            s = _state(iteration=4, remaining_tasks=["a"], owned_paths=["src/x.py"])
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

    def test_interrupted_writer_with_any_mutation_fails_closed(self):
        # Section 9: from a shared worktree we cannot prove a mutation is
        # Claude's vs a concurrent human edit -> ANY delta since the writer
        # started fails closed. The writer is NEVER replayed.
        with tempfile.TemporaryDirectory() as d:
            s = _state(worker_in_flight="claude_implement", owned_paths=[],
                       pre_writer_paths=[], pre_writer_hashes={})
            g = self._git(d, ["src/x.py"])                 # something changed
            out = reconcile_for_resume(s, g, ["agent_harness/", ".git/"])
            self.assertEqual(out.stop_reason, StopReason.INTERRUPTED_WRITE_UNATTRIBUTABLE)
            self.assertIsNone(out.next_node)

    def test_interrupted_writer_new_untracked_file_also_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(d, ["src/x.py"])
            pre = g.snapshot(["src/x.py"])
            s = _state(worker_in_flight="claude_repair", owned_paths=["src/x.py"],
                       pre_writer_paths=["src/x.py"], pre_writer_hashes=dict(pre.hashes))
            g.write("brand_new.py", "half written\n", untracked=True)
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.INTERRUPTED_WRITE_UNATTRIBUTABLE)

    def test_interrupted_writer_further_mutating_an_owned_file_fails_closed(self):
        # The writer was mid-repair on an already-owned file and crashed. We
        # cannot prove the on-disk content is Claude's final output vs a partial
        # write vs a concurrent human edit -> fail closed, never route to VERIFY.
        with tempfile.TemporaryDirectory() as d:
            g = self._git(d, ["src/x.py"])
            pre = g.snapshot(["src/x.py"])
            s = _state(worker_in_flight="claude_repair", owned_paths=["src/x.py"],
                       pre_writer_paths=["src/x.py"], pre_writer_hashes=dict(pre.hashes))
            g.set_changed(["src/x.py"])                 # content moved again after the snapshot
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.INTERRUPTED_WRITE_UNATTRIBUTABLE)
            self.assertIsNone(out.next_node)
            self.assertEqual(out.owned_paths, ["src/x.py"])   # ownership NOT expanded

    def test_interrupted_writer_with_no_observable_change_resumes_freeze(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(d, [])                            # nothing changed on disk
            s = _state(worker_in_flight="claude_repair", owned_paths=["src/x.py"],
                       pre_writer_paths=[], pre_writer_hashes={})
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertIsNone(out.stop_reason)
            self.assertEqual(out.next_node, "freeze")       # owned -> re-freeze + re-verify

    def test_interrupted_first_writer_no_change_reruns_implement(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(d, [])
            s = _state(worker_in_flight="claude_implement", owned_paths=[],
                       pre_writer_paths=[], pre_writer_hashes={})
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertIsNone(out.stop_reason)
            self.assertEqual(out.next_node, "implement")

    def test_protected_change_stops(self):
        with tempfile.TemporaryDirectory() as d:
            s = _state(worker_in_flight="claude_implement", owned_paths=[])
            g = self._git(d, ["agent_harness/graph.py"])
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.PROTECTED_PATH_MODIFIED)

    def test_readonly_worker_reenters_review(self):
        with tempfile.TemporaryDirectory() as d:
            s = _state(worker_in_flight="codex_review")
            out = reconcile_for_resume(s, self._git(d, []), ["agent_harness/"])
            self.assertEqual(out.next_node, "review")
            self.assertIsNone(out.worker_in_flight)

    def test_no_worker_in_flight_derives_from_last_completed_node(self):
        with tempfile.TemporaryDirectory() as d:
            # raw next_node is NOT trusted; re-entry derives from last_completed_node
            s = _state(worker_in_flight=None, next_node="escalate", last_completed_node="verify")
            out = reconcile_for_resume(s, self._git(d, []), ["agent_harness/"])
            self.assertEqual(out.next_node, "decide")   # _AFTER_NODE["verify"]

    def test_every_after_node_target_is_resumable(self):
        from agent_harness.persistence import _AFTER_NODE
        from agent_harness.state import RESUMABLE_NODES
        for src, target in _AFTER_NODE.items():
            self.assertIn(target, RESUMABLE_NODES, msg=f"{src!r} -> {target!r} not resumable")

    def test_planner_in_flight_reruns_plan(self):
        with tempfile.TemporaryDirectory() as d:
            s = _state(worker_in_flight="claude_plan")
            out = reconcile_for_resume(s, self._git(d, []), ["agent_harness/"])
            self.assertEqual(out.next_node, "plan")
            self.assertIsNone(out.worker_in_flight)

    def test_unknown_next_node_is_unsafe(self):
        with tempfile.TemporaryDirectory() as d:
            s = _state(worker_in_flight=None, next_node="banana", last_completed_node="mystery")
            out = reconcile_for_resume(s, self._git(d, []), ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.UNSAFE_RESUME_STATE)

    def test_reviewed_phase_routes_to_checkpoint_when_ref_at_parent(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(d, [])
            ref, parent = g._head_ref, g.head()
            # fabricate a candidate object in the fake object store
            tree = g._tree_of({"src/x.py": "candidate"})
            commit = g.commit_tree(tree_oid=tree, parent=parent, message="m",
                                   author=["h", "h", "0"], committer=["h", "h", "0"])
            s = _state(checkpoint_phase="reviewed", checkpoint_target_ref=ref,
                       checkpoint_expected_parent=parent, candidate_tree_oid=tree,
                       candidate_commit_oid=commit, verified_tree_oid=tree,
                       verified_commit_oid=commit, reviewed_commit_oid=commit,
                       reviewed_tree_oid=tree, next_node="decide")
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertIsNone(out.stop_reason)
            self.assertEqual(out.next_node, "checkpoint")

    def test_reviewed_phase_ref_at_candidate_means_cas_landed(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(d, [])
            ref, parent = g._head_ref, g.head()
            tree = g._tree_of({"src/x.py": "candidate"})
            commit = g.commit_tree(tree_oid=tree, parent=parent, message="m",
                                   author=["h", "h", "0"], committer=["h", "h", "0"])
            g._refs[ref] = commit                                  # CAS already happened
            s = _state(checkpoint_phase="reviewed", checkpoint_target_ref=ref,
                       checkpoint_expected_parent=parent, candidate_tree_oid=tree,
                       candidate_commit_oid=commit, verified_tree_oid=tree,
                       verified_commit_oid=commit, reviewed_commit_oid=commit,
                       reviewed_tree_oid=tree)
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.checkpoint_phase, "ref_updated")
            self.assertEqual(out.next_node, "checkpoint")

    def test_reviewed_phase_foreign_ref_move_conflicts(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(d, [])
            ref, parent = g._head_ref, g.head()
            tree = g._tree_of({"src/x.py": "candidate"})
            commit = g.commit_tree(tree_oid=tree, parent=parent, message="m",
                                   author=["h", "h", "0"], committer=["h", "h", "0"])
            g._refs[ref] = "somethingelse" * 3
            s = _state(checkpoint_phase="reviewed", checkpoint_target_ref=ref,
                       checkpoint_expected_parent=parent, candidate_tree_oid=tree,
                       candidate_commit_oid=commit, verified_tree_oid=tree,
                       verified_commit_oid=commit, reviewed_commit_oid=commit,
                       reviewed_tree_oid=tree)
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.REF_UPDATE_CONFLICT)

    def test_expected_head_moved_externally_stops(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(d, [])
            g._refs[g._head_ref] = "actualHEAD"
            s = _state(worker_in_flight=None, next_node="verify", expected_head="oldHEAD")
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.EXPECTED_HEAD_MOVED)


class CorruptStateTests(unittest.TestCase):
    def test_malformed_json_raises_state_corrupt_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            store = Persistence(d)
            store.state_path.parent.mkdir(parents=True)
            store.state_path.write_text("{ this is not json")
            with self.assertRaises(StateCorruptError):
                store.load()
            self.assertEqual(store.state_path.read_text(), "{ this is not json")

    def test_schema_invalid_json_raises_state_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            store = Persistence(d)
            store.state_path.parent.mkdir(parents=True)
            store.state_path.write_text(json.dumps({"objective": "o"}))  # missing run_id, etc.
            with self.assertRaises(StateCorruptError):
                store.load()


class RunLockTests(unittest.TestCase):
    def test_second_acquire_fails_then_succeeds_after_release(self):
        with tempfile.TemporaryDirectory() as d:
            agent_dir = Path(d) / ".agent"
            a = RunLock(agent_dir).acquire()
            try:
                with self.assertRaises(RunLockError):
                    RunLock(agent_dir).acquire()
            finally:
                a.release()
            # lock is reusable after release
            b = RunLock(agent_dir).acquire()
            b.release()

    def test_context_manager_releases_on_exception(self):
        with tempfile.TemporaryDirectory() as d:
            agent_dir = Path(d) / ".agent"
            with self.assertRaises(RuntimeError):
                with RunLock(agent_dir):
                    raise RuntimeError("boom")
            RunLock(agent_dir).acquire().release()  # not stuck


if __name__ == "__main__":
    unittest.main()
