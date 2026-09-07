"""Adversarial regression coverage for the V0.1b Codex findings.

C3  review-payload construction fails closed
C4  Claude structured is_error:true is a failed writer call
M1  every declared resumable node is genuinely routable
M2  interrupted first-writer edit reconciliation uses the pre-writer snapshot
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import graph as graphmod
from agent_harness.graph import Engine
from agent_harness.persistence import _AFTER_NODE, reconcile_for_resume
from agent_harness.state import RESUMABLE_NODES, CheckResult, HarnessState, StopReason
from agent_harness.tests.helpers import (
    FakeClaude,
    FakeCodex,
    FakeGit,
    RecordingStore,
    SpyPrinter,
    base_state,
    claude_invocation,
    make_config,
)
from agent_harness.workers.claude import PlanResult, PlanTask


def one_task_plan():
    return PlanResult(tasks=[PlanTask(id="T1", title="do the thing", rationale="r")])


def checks(passing):
    def _run(commands, **kw):
        return [CheckResult(command="c", exit_code=0 if passing else 1,
                            stderr_tail="" if passing else "boom")]
    return _run


class _Base(unittest.TestCase):
    def build(self, *, claude=None, codex=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        cfg = make_config(root)
        self.git = FakeGit(root)
        self.claude = claude or FakeClaude(plan_result=one_task_plan(),
                                           on_edit=lambda _r: self.git.set_changed(["src/x.py"]))
        self.codex = codex or FakeCodex()
        self.printer = SpyPrinter()
        self.store = RecordingStore(root)
        return Engine(cfg, claude=self.claude, codex=self.codex, git=self.git,
                      store=self.store, printer=self.printer)

    def run_engine(self, eng, script):
        with mock.patch.object(graphmod.verifier, "run_checks", script):
            return eng.run(base_state())


class ReviewPayloadFailClosedTests(_Base):
    def test_diff_tree_raising_fails_closed_no_codex_no_checkpoint(self):
        eng = self.build()
        self.git.raise_on_diff_tree = True
        final = self.run_engine(eng, checks(True))
        self.assertEqual(final.stop_reason, StopReason.REVIEW_PAYLOAD_ERROR)
        self.assertEqual(self.codex.calls, [])                 # Codex never invoked
        self.assertNotIn("checkpoint", self.printer.visited)
        self.assertEqual(self.git.cas_calls, [])
        self.assertIsNone(final.reviewed_tree_oid)

    def test_empty_review_payload_fails_closed(self):
        eng = self.build()
        self.git.empty_diff_tree = True
        final = self.run_engine(eng, checks(True))
        self.assertEqual(final.stop_reason, StopReason.REVIEW_PAYLOAD_ERROR)
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(self.git.cas_calls, [])

    def test_untracked_content_reaches_a_successful_review_payload(self):
        eng = self.build(claude=FakeClaude(
            plan_result=one_task_plan(),
            on_edit=lambda _r: self.git.set_changed(["fresh_new.py"], untracked=True),
        ))
        seen = {}
        real_review = self.codex.review

        def spy_review(**kw):
            seen["diff"] = kw.get("diff", "")
            return real_review(**kw)

        self.codex.review = spy_review
        final = self.run_engine(eng, checks(True))
        self.assertIn("fresh_new.py", seen.get("diff", ""))
        self.assertEqual(final.stop_reason, StopReason.SUCCESS)


class StructuredClaudeErrorTests(_Base):
    def test_exit_zero_is_error_true_implementation_cannot_checkpoint(self):
        eng = self.build(claude=FakeClaude(
            plan_result=one_task_plan(),
            implement_seq=[claude_invocation("implement", is_error=True, ok=True, exit_code=0)],
            on_edit=lambda _r: self.git.set_changed(["src/x.py"]),
        ))
        final = self.run_engine(eng, checks(True))
        self.assertEqual(final.stop_reason, StopReason.CLI_FATAL)
        self.assertNotIn("checkpoint", self.printer.visited)
        self.assertEqual(self.git.cas_calls, [])
        # filesystem changes are preserved, not discarded
        self.assertIn("src/x.py", final.owned_paths)

    def test_exit_zero_is_error_usage_subtype_stops_before_next_paid_worker(self):
        inv = claude_invocation("implement", is_error=True, ok=True, exit_code=0)
        inv.subtype = "usage_limit"
        eng = self.build(claude=FakeClaude(
            plan_result=one_task_plan(), implement_seq=[inv],
            on_edit=lambda _r: self.git.set_changed(["src/x.py"]),
        ))
        final = self.run_engine(eng, checks(True))
        self.assertEqual(final.stop_reason, StopReason.USAGE_LIMIT)
        self.assertEqual(self.codex.calls, [])        # no review after usage limit
        self.assertNotIn("repair", self.claude.calls)


class ResumableNodeContractTests(unittest.TestCase):
    def _engine(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        return Engine(make_config(root), claude=FakeClaude(), codex=FakeCodex(),
                      git=FakeGit(root), store=RecordingStore(root), printer=SpyPrinter())

    def test_every_resumable_node_is_a_real_graph_node_or_end(self):
        eng = self._engine()
        compiled = eng.build()
        graph_nodes = set(compiled.get_graph().nodes)
        for n in RESUMABLE_NODES:
            if n == "end":
                continue
            self.assertIn(n, graph_nodes, msg=f"{n!r} declared resumable but not a graph node")

    def test_route_after_bootstrap_accepts_every_resumable_target(self):
        eng = self._engine()
        for n in RESUMABLE_NODES:
            eng.resume_target = n
            self.assertEqual(eng._route_after_bootstrap(base_state()), n)

    def test_after_node_map_only_targets_resumable_nodes(self):
        for src, tgt in _AFTER_NODE.items():
            self.assertIn(tgt, RESUMABLE_NODES, msg=f"{src!r} -> {tgt!r}")

    def test_completed_escalation_review_resumes_to_repair(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        s = HarnessState(objective="o", run_id="r", last_completed_node="escalation_review",
                         next_node="escalate", expected_head=None)
        out = reconcile_for_resume(s, FakeGit(root), ["agent_harness/"])
        self.assertEqual(out.next_node, "repair")
        self.assertIsNone(out.stop_reason)


class InterruptedWriterReconcileTests(unittest.TestCase):
    """Section 9: a shared-worktree writer interrupted with ANY unattributable
    mutation fails closed - it is never replayed, and human edits are never
    silently adopted."""

    def _git(self, root):
        return FakeGit(root)

    def test_interrupted_tracked_edit_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(Path(d))
            g.set_changed(["src/x.py"])
            s = HarnessState(objective="o", run_id="r", worker_in_flight="claude_implement",
                             owned_paths=[], pre_writer_paths=[], pre_writer_hashes={})
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.INTERRUPTED_WRITE_UNATTRIBUTABLE)
            self.assertNotIn("src/x.py", out.owned_paths)   # NOT silently adopted

    def test_interrupted_new_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(Path(d))
            g.write("created.py", "half written\n", untracked=True)
            s = HarnessState(objective="o", run_id="r", worker_in_flight="claude_implement",
                             owned_paths=[], pre_writer_paths=[], pre_writer_hashes={})
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.INTERRUPTED_WRITE_UNATTRIBUTABLE)

    def test_interrupted_writer_no_mutation_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(Path(d))                          # clean worktree
            s = HarnessState(objective="o", run_id="r", worker_in_flight="claude_implement",
                             owned_paths=[], pre_writer_paths=[], pre_writer_hashes={})
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertIsNone(out.stop_reason)
            self.assertEqual(out.next_node, "implement")    # nothing done -> re-run

    def test_interrupted_writer_protected_path_mutation_stops_as_protected(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(Path(d))
            g.set_changed(["agent_harness/graph.py"])
            s = HarnessState(objective="o", run_id="r", worker_in_flight="claude_repair",
                             owned_paths=[], pre_writer_paths=[], pre_writer_hashes={})
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.PROTECTED_PATH_MODIFIED)

    def test_pre_writer_paths_not_subset_of_owned_is_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            g = self._git(Path(d))
            g.set_changed(["a.py"])
            s = HarnessState(objective="o", run_id="r", worker_in_flight="claude_repair",
                             owned_paths=[], pre_writer_paths=["a.py"],
                             pre_writer_hashes={"a.py": "deadbeef"})
            out = reconcile_for_resume(s, g, ["agent_harness/"])
            self.assertEqual(out.stop_reason, StopReason.INTERRUPTED_WRITE_UNATTRIBUTABLE)


if __name__ == "__main__":
    unittest.main()
