"""The engineering-agent routing seam: ROLE -> BACKEND -> MODEL.

Covers role resolution, that Claude roles hit the Claude adapter and Codex roles
hit the Codex adapter, that provider argv never leaks into orchestration, that
failures propagate unchanged, that telemetry captures role/backend/model, and
that the safety-stage ordering of the graph is untouched.
"""

import json
import tempfile
import unittest
from pathlib import Path

from agent_harness.agents import AgentInvocation, AgentRouter
from agent_harness.config import Config, ConfigError
from agent_harness.graph import Engine
from agent_harness.state import StopReason
from agent_harness.telemetry import AgentTelemetry
from agent_harness.tests.helpers import (
    FakeClaude,
    FakeCodex,
    RecordingStore,
    SpyPrinter,
    base_state,
    claude_invocation,
    make_config,
)
from agent_harness.workers.claude import PlanningError, WorkerUsageLimitError
from agent_harness.workers.claude import PlanResult, PlanTask
from agent_harness.workers.codex import EscalateOutcome, ReviewOutcome, ReviewVerdict


def _plan():
    return PlanResult(tasks=[PlanTask(id="T1", title="do the thing", rationale="r")])


class _SpyClaude:
    """Records the model each role was invoked with."""

    def __init__(self):
        self.log_dir = None
        self.seen = []  # (role, model)

    def plan(self, *, model=None, **kw):
        self.seen.append(("plan", model))
        return _plan()

    def implement(self, *, model=None, **kw):
        self.seen.append(("implement", model))
        return claude_invocation("implement")

    def repair(self, *, model=None, **kw):
        self.seen.append(("repair", model))
        return claude_invocation("repair")


class _SpyCodex:
    def __init__(self):
        self.log_dir = None
        self.seen = []

    def review(self, *, model=None, **kw):
        self.seen.append(("review", model))
        return ReviewOutcome(kind="verdict", verdict=ReviewVerdict(verdict="pass", severity="none"))

    def escalate(self, *, model=None, **kw):
        self.seen.append(("escalate", model))
        return EscalateOutcome(root_cause="root cause: X")


def _router(cfg=None, *, claude=None, codex=None, telemetry=None):
    with tempfile.TemporaryDirectory() as d:
        cfg = cfg or make_config(Path(d))
        return AgentRouter(cfg, claude=claude or _SpyClaude(), codex=codex or _SpyCodex(),
                           telemetry=telemetry)


class RoleResolutionTests(unittest.TestCase):
    def test_default_assignments_match_v0(self):
        cfg = make_config(Path("/tmp"))
        r = cfg.resolved_roles()
        self.assertEqual((r["planner"].backend, r["planner"].model), ("claude_code", "claude-sonnet-5"))
        self.assertEqual((r["implementer"].backend, r["implementer"].model), ("claude_code", "claude-sonnet-5"))
        self.assertEqual((r["repairer"].backend, r["repairer"].model), ("claude_code", "claude-sonnet-5"))
        self.assertEqual((r["routine_reviewer"].backend, r["routine_reviewer"].model), ("codex", "gpt-5.6-luna"))
        self.assertEqual((r["escalation_reviewer"].backend, r["escalation_reviewer"].model), ("codex", "gpt-5.6-terra"))

    def test_explicit_role_override_wins(self):
        cfg = make_config(Path("/tmp"), roles={
            "routine_reviewer": {"backend": "codex", "model": "gpt-5.6-luna-mini"},
        })
        self.assertEqual(cfg.resolve_role("routine_reviewer").model, "gpt-5.6-luna-mini")
        # untouched roles still fall back to the V0 default
        self.assertEqual(cfg.resolve_role("planner").model, "claude-sonnet-5")

    def test_blank_model_uses_backend_default(self):
        cfg = make_config(Path("/tmp"), roles={
            "escalation_reviewer": {"backend": "codex", "model": ""},
        })
        self.assertEqual(cfg.resolve_role("escalation_reviewer").model, "gpt-5.6-terra")

    def test_router_refuses_role_on_unsupported_backend(self):
        cfg = make_config(Path("/tmp"), roles={"planner": {"backend": "codex", "model": "x"}})
        with self.assertRaises(ConfigError):
            AgentRouter(cfg, claude=_SpyClaude(), codex=_SpyCodex())

    def test_unknown_backend_rejected_by_config(self):
        with self.assertRaises(Exception):
            make_config(Path("/tmp"), roles={"planner": {"backend": "kimi", "model": "x"}})


class BackendRoutingTests(unittest.TestCase):
    def test_claude_roles_hit_claude_adapter_with_resolved_model(self):
        c, x = _SpyClaude(), _SpyCodex()
        r = _router(claude=c, codex=x)
        r.plan(objective="o", iteration=1)
        r.implement(objective="o", task="t", iteration=2)
        r.repair(objective="o", task="t", failures="f", iteration=3)
        self.assertEqual(c.seen, [("plan", "claude-sonnet-5"),
                                  ("implement", "claude-sonnet-5"),
                                  ("repair", "claude-sonnet-5")])
        self.assertEqual(x.seen, [])

    def test_codex_roles_hit_codex_adapter_with_resolved_model(self):
        c, x = _SpyClaude(), _SpyCodex()
        r = _router(claude=c, codex=x)
        r.routine_review(diff="d", check_results="c", iteration=1)
        r.escalation_review(diff="d", failures="f", iteration=2)
        self.assertEqual(x.seen, [("review", "gpt-5.6-luna"), ("escalate", "gpt-5.6-terra")])
        self.assertEqual(c.seen, [])

    def test_no_provider_argv_leaks_into_orchestration(self):
        graph_src = (Path(__file__).resolve().parents[1] / "graph.py").read_text()
        for token in ("--permission-mode", "--sandbox", "--output-schema",
                      "--json-schema", "exec", "acceptEdits", "resolve_claude_executable"):
            self.assertNotIn(token, graph_src, msg=f"{token!r} leaked into graph.py")
        # the Engine talks only to the router
        self.assertNotIn("self.claude.", graph_src)
        self.assertNotIn("self.codex.", graph_src)


class FailurePropagationTests(unittest.TestCase):
    def test_planning_error_propagates_unchanged(self):
        class Boom(_SpyClaude):
            def plan(self, **kw):
                raise PlanningError("planner exploded")
        with self.assertRaises(PlanningError):
            _router(claude=Boom()).plan(objective="o")

    def test_usage_limit_propagates_unchanged(self):
        class Boom(_SpyClaude):
            def plan(self, **kw):
                raise WorkerUsageLimitError("limit")
        with self.assertRaises(WorkerUsageLimitError):
            _router(claude=Boom()).plan(objective="o")

    def test_failed_claude_invocation_returned_intact(self):
        class Bad(_SpyClaude):
            def implement(self, **kw):
                return claude_invocation("implement", ok=False, exit_code=1, classification="cli_error")
        inv = _router(claude=Bad()).implement(objective="o", task="t")
        self.assertFalse(inv.ok)
        self.assertEqual(inv.result.exit_code, 1)

    def test_reviewer_infra_error_returned_intact(self):
        class Bad(_SpyCodex):
            def review(self, **kw):
                return ReviewOutcome(kind="infra_error", reason="timeout", classification="cli_error")
        out = _router(codex=Bad()).routine_review(diff="d", check_results="c")
        self.assertTrue(out.is_infra_error)


class TelemetryTests(unittest.TestCase):
    def test_records_role_backend_model_and_status(self):
        with tempfile.TemporaryDirectory() as d:
            tel = AgentTelemetry(Path(d) / "agents.jsonl")
            r = _router(telemetry=tel)
            r.plan(objective="o", iteration=4)
            r.routine_review(diff="d", check_results="c", iteration=4)
            rows = tel.read_all()
            self.assertEqual(len(rows), 2)
            by_role = {row["role"]: row for row in rows}
            self.assertEqual(by_role["planner"]["backend"], "claude_code")
            self.assertEqual(by_role["planner"]["model"], "claude-sonnet-5")
            self.assertTrue(by_role["planner"]["ok"])
            self.assertEqual(by_role["planner"]["iteration"], 4)
            self.assertEqual(by_role["routine_reviewer"]["backend"], "codex")
            self.assertEqual(by_role["routine_reviewer"]["model"], "gpt-5.6-luna")
            for row in rows:
                self.assertIn("started_at", row)
                self.assertIn("ended_at", row)
                self.assertIn("duration_s", row)

    def test_records_a_failed_invocation_then_reraises(self):
        class Boom(_SpyClaude):
            def plan(self, **kw):
                raise PlanningError("nope")
        with tempfile.TemporaryDirectory() as d:
            tel = AgentTelemetry(Path(d) / "agents.jsonl")
            with self.assertRaises(PlanningError):
                _router(claude=Boom(), telemetry=tel).plan(objective="o")
            rows = tel.read_all()
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]["ok"])
            # structural only - no free-form message
            self.assertEqual(rows[0]["error_type"], "PlanningError")
            self.assertEqual(rows[0]["error_code"], "provider_execution_failed")
            self.assertNotIn("error", rows[0])
            self.assertEqual(rows[0]["role"], "planner")
            self.assertEqual(rows[0]["backend"], "claude_code")
            self.assertEqual(rows[0]["model"], "claude-sonnet-5")

    def test_usage_limit_failure_recorded_structurally(self):
        class Boom(_SpyClaude):
            def plan(self, **kw):
                raise WorkerUsageLimitError("hit the wall")
        with tempfile.TemporaryDirectory() as d:
            tel = AgentTelemetry(Path(d) / "agents.jsonl")
            with self.assertRaises(WorkerUsageLimitError):
                _router(claude=Boom(), telemetry=tel).plan(objective="o")
            row = tel.read_all()[0]
            self.assertEqual(row["error_type"], "WorkerUsageLimitError")
            self.assertEqual(row["error_code"], "usage_limit")
            self.assertEqual(row["classification"], "usage_limit")

    def test_provider_error_text_including_secrets_never_reaches_agents_jsonl(self):
        """Codex's exact reproduction: a synthetic secret marker in a provider
        error must not appear anywhere in durable engineering telemetry."""
        MARKER = "SECRET_MARKER_7f5a"

        class Leaky(_SpyClaude):
            def plan(self, **kw):
                # mimics ClaudeWorker.plan embedding provider stderr in the message
                raise PlanningError(
                    f"planner CLI failed (exit=1, class=cli_error): "
                    f"ANTHROPIC_API_KEY=sk-live-{MARKER} leaked on stderr"
                )

        with tempfile.TemporaryDirectory() as d:
            jsonl = Path(d) / "agents.jsonl"
            tel = AgentTelemetry(jsonl)
            with self.assertRaises(PlanningError) as ctx:
                _router(claude=Leaky(), telemetry=tel).plan(objective="o", iteration=2)

            # 1) the invocation still fails, and the interactive message is unchanged
            self.assertIn(MARKER, str(ctx.exception))

            # 2) telemetry still records a useful structural failure event
            rows = tel.read_all()
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertFalse(r["ok"])
            self.assertEqual(r["error_type"], "PlanningError")
            self.assertEqual(r["error_code"], "provider_execution_failed")
            self.assertEqual(r["role"], "planner")
            self.assertEqual(r["backend"], "claude_code")
            self.assertEqual(r["model"], "claude-sonnet-5")
            self.assertEqual(r["iteration"], 2)

            # 3) the marker is NOWHERE in the durable file (raw bytes)
            self.assertNotIn(MARKER, jsonl.read_text())
            self.assertNotIn("sk-live", jsonl.read_text())
            self.assertNotIn("ANTHROPIC_API_KEY", jsonl.read_text())

    def test_sink_drops_unknown_keys_even_if_they_carry_text(self):
        MARKER = "SECRET_MARKER_7f5a"
        with tempfile.TemporaryDirectory() as d:
            jsonl = Path(d) / "agents.jsonl"
            tel = AgentTelemetry(jsonl)
            tel.record({"role": "planner", "backend": "claude_code",
                        "stderr": f"boom {MARKER}", "error": MARKER, "prompt": MARKER})
            self.assertNotIn(MARKER, jsonl.read_text())
            row = tel.read_all()[0]
            self.assertEqual(row["role"], "planner")
            self.assertNotIn("stderr", row)
            self.assertNotIn("error", row)
            self.assertNotIn("prompt", row)

    def test_telemetry_write_failure_is_swallowed(self):
        # unwritable path -> record() must not raise
        tel = AgentTelemetry(Path("/proc/nonexistent/agents.jsonl"))
        tel.record(AgentInvocation(role="planner", backend="claude_code", model="m",
                                   cwd="/x", iteration=0, started_at="a", ended_at="b",
                                   duration_s=0.0, ok=True))
        self.assertEqual(tel.read_all(), [])

    def test_no_telemetry_sink_is_a_noop(self):
        r = _router(telemetry=AgentTelemetry(None))
        r.plan(objective="o")   # must not raise


_VALID_CLASSIFICATIONS = {"ok", "cli_error", "usage_limit"}
BAD = "SECRET_MARKER_7f5a"


class ClassificationClosedSetTests(unittest.TestCase):
    """Codex's reproduction: an arbitrary `classification` string from a backend
    outcome must not reach agents.jsonl; the durable value is a closed set."""

    def _run_and_read(self, *, claude=None, codex=None, call):
        with tempfile.TemporaryDirectory() as d:
            jsonl = Path(d) / "agents.jsonl"
            tel = AgentTelemetry(jsonl)
            r = _router(claude=claude, codex=codex, telemetry=tel)
            outcome = call(r)
            text = jsonl.read_text() if jsonl.is_file() else ""
            return outcome, text, tel.read_all()

    def test_routine_reviewer_arbitrary_classification_is_contained(self):
        class Leaky(_SpyCodex):
            def review(self, *, model=None, **kw):
                return ReviewOutcome(kind="infra_error", reason="x", classification=BAD)

        outcome, text, rows = self._run_and_read(
            codex=Leaky(),
            call=lambda r: r.routine_review(diff="d", check_results="c", iteration=1),
        )
        self.assertTrue(outcome.is_infra_error)            # runtime semantics preserved
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["classification"], _VALID_CLASSIFICATIONS)
        self.assertEqual(rows[0]["classification"], "cli_error")   # fail-closed
        self.assertEqual(rows[0]["role"], "routine_reviewer")
        self.assertEqual(rows[0]["backend"], "codex")
        self.assertEqual(rows[0]["model"], "gpt-5.6-luna")
        self.assertNotIn(BAD, text)

    def test_escalation_reviewer_arbitrary_classification_is_contained(self):
        class Leaky(_SpyCodex):
            def escalate(self, *, model=None, **kw):
                return EscalateOutcome(root_cause=None, classification=BAD)

        outcome, text, rows = self._run_and_read(
            codex=Leaky(),
            call=lambda r: r.escalation_review(diff="d", failures="f", iteration=2),
        )
        self.assertIsNone(outcome.root_cause)              # runtime semantics preserved
        self.assertEqual(rows[0]["classification"], "cli_error")
        self.assertEqual(rows[0]["role"], "escalation_reviewer")
        self.assertEqual(rows[0]["model"], "gpt-5.6-terra")
        self.assertNotIn(BAD, text)

    def test_claude_invocation_arbitrary_classification_is_contained(self):
        from agent_harness.workers.base import WorkerResult
        from agent_harness.workers.claude import ClaudeInvocation

        class Leaky(_SpyClaude):
            def implement(self, *, model=None, **kw):
                res = WorkerResult(ok=False, exit_code=1, stdout="", stderr="",
                                   duration_s=0.0, classification=BAD)
                return ClaudeInvocation(role="implement", result=res, model=model or "x")

        outcome, text, rows = self._run_and_read(
            claude=Leaky(),
            call=lambda r: r.implement(objective="o", task="t", iteration=3),
        )
        self.assertFalse(outcome.ok)                       # runtime semantics preserved
        self.assertEqual(rows[0]["classification"], "cli_error")
        self.assertEqual(rows[0]["role"], "implementer")
        self.assertEqual(rows[0]["backend"], "claude_code")
        self.assertNotIn(BAD, text)

    def test_valid_classifications_are_passed_through_unchanged(self):
        for value in sorted(_VALID_CLASSIFICATIONS):
            class C(_SpyCodex):
                def review(self, *, model=None, _v=value, **kw):
                    return ReviewOutcome(kind="verdict",
                                         verdict=ReviewVerdict(verdict="pass", severity="none"),
                                         classification=_v)
            _, _, rows = self._run_and_read(
                codex=C(), call=lambda r: r.routine_review(diff="d", check_results="c"))
            self.assertEqual(rows[0]["classification"], value)

    def test_sink_rejects_arbitrary_classification_from_plain_dict(self):
        with tempfile.TemporaryDirectory() as d:
            jsonl = Path(d) / "agents.jsonl"
            tel = AgentTelemetry(jsonl)
            tel.record({"role": "planner", "backend": "claude_code",
                        "classification": BAD, "error_code": BAD, "error_type": BAD})
            row = tel.read_all()[0]
            self.assertEqual(row["classification"], "cli_error")
            self.assertEqual(row["error_code"], "invocation_error")
            self.assertEqual(row["error_type"], "OtherError")
            self.assertNotIn(BAD, jsonl.read_text())

    def test_sink_drops_non_scalar_values_without_str_coercion(self):
        class Weird:
            def __str__(self):  # would leak via default=str
                return "OBJ_" + BAD
        with tempfile.TemporaryDirectory() as d:
            jsonl = Path(d) / "agents.jsonl"
            tel = AgentTelemetry(jsonl)
            tel.record({"role": "planner", "iteration": Weird(), "duration_s": Weird(),
                        "exit_code": Weird(), "ok": Weird(), "model": Weird()})
            text = jsonl.read_text()
            self.assertNotIn(BAD, text)
            self.assertNotIn("OBJ_", text)
            row = tel.read_all()[0]
            self.assertEqual(row["role"], "planner")
            for dropped in ("iteration", "duration_s", "exit_code", "ok", "model"):
                self.assertNotIn(dropped, row)


class SafetyStageOrderingTests(unittest.TestCase):
    """A full run through the router must visit the safety stages in the same
    order as before: plan -> implement -> freeze -> verify -> review -> checkpoint."""

    def test_stage_ordering_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            from unittest import mock
            from agent_harness import graph as graphmod
            from agent_harness.state import CheckResult
            from agent_harness.tests.helpers import FakeGit

            cfg = make_config(root)
            claude = FakeClaude(plan_result=_plan(),
                                on_edit=lambda _r: git.set_changed(["src/x.py"]))
            codex = FakeCodex()
            git = FakeGit(root)
            printer = SpyPrinter()
            eng = Engine(cfg, claude=claude, codex=codex, git=git,
                         store=RecordingStore(root), printer=printer)
            with mock.patch.object(graphmod.verifier, "run_checks",
                                   lambda c, **k: [CheckResult(command="c", exit_code=0)]):
                final = eng.run(base_state())
            self.assertEqual(final.stop_reason, StopReason.SUCCESS)
            first_seen = []
            for node in printer.visited:
                if node not in first_seen:
                    first_seen.append(node)
            order = [n for n in first_seen if n in
                     ("plan", "implement", "freeze", "verify", "review", "checkpoint")]
            self.assertEqual(order, ["plan", "implement", "freeze", "verify", "review", "checkpoint"])
            # telemetry landed in the run log dir
            tel = eng.agents.telemetry
            roles = {row["role"] for row in tel.read_all()}
            self.assertIn("planner", roles)
            self.assertIn("implementer", roles)
            self.assertIn("routine_reviewer", roles)


if __name__ == "__main__":
    unittest.main()
