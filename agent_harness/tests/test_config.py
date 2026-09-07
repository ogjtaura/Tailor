import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import config as cfgmod
from agent_harness.config import ConfigError, detect_max_turns_support, load_config

REPO = Path(__file__).resolve().parents[2]


class LoadConfigTests(unittest.TestCase):
    def test_loads_repo_agent_toml(self):
        cfg = load_config(REPO / "agent.toml", REPO)
        self.assertEqual(cfg.claude.model, "claude-sonnet-5")
        self.assertEqual(cfg.codex.review_model, "gpt-5.6-luna")
        self.assertEqual(cfg.codex.checkpoint_model, "gpt-5.6-terra")
        self.assertEqual(cfg.agent.max_iterations, 15)
        self.assertIn("agent_harness/", cfg.safety.protected_paths)
        self.assertEqual(cfg.claude.planner_tools, ["Read", "Grep", "Glob"])
        self.assertIn("Edit", cfg.claude.edit_tools)
        self.assertNotIn("Bash", cfg.claude.edit_tools)
        self.assertGreaterEqual(len(cfg.checks.commands), 1)

    def test_missing_file_is_clear_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(REPO / "nope.toml", REPO)
        self.assertIn("not found", str(ctx.exception))

    def test_max_iterations_override(self):
        cfg = load_config(REPO / "agent.toml", REPO).with_overrides(max_iterations=3)
        self.assertEqual(cfg.agent.max_iterations, 3)

    def test_repo_agent_toml_role_routing_matches_v0(self):
        cfg = load_config(REPO / "agent.toml", REPO)
        r = cfg.resolved_roles()
        self.assertEqual((r["planner"].backend, r["planner"].model), ("claude_code", "claude-sonnet-5"))
        self.assertEqual((r["implementer"].backend, r["implementer"].model), ("claude_code", "claude-sonnet-5"))
        self.assertEqual((r["repairer"].backend, r["repairer"].model), ("claude_code", "claude-sonnet-5"))
        self.assertEqual((r["routine_reviewer"].backend, r["routine_reviewer"].model), ("codex", "gpt-5.6-luna"))
        self.assertEqual((r["escalation_reviewer"].backend, r["escalation_reviewer"].model), ("codex", "gpt-5.6-terra"))
        # with_overrides round-trips the [roles] table
        self.assertEqual(cfg.with_overrides(max_iterations=1).resolve_role("planner").model,
                         "claude-sonnet-5")

    def test_roles_table_is_optional(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "agent.toml"
        p.write_text('[checks]\ncommands = ["true"]\n')   # no [roles] at all
        cfg = load_config(p, REPO)
        self.assertEqual(cfg.resolve_role("routine_reviewer").model, "gpt-5.6-luna")
        self.assertEqual(cfg.resolve_role("planner").backend, "claude_code")

    def test_bad_backend_in_roles_is_config_error(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "agent.toml"
        p.write_text('[checks]\ncommands = ["true"]\n'
                     '[roles]\nplanner = { backend = "kimi", model = "x" }\n')
        with self.assertRaises(ConfigError):
            load_config(p, REPO)


class ExecutableResolutionTests(unittest.TestCase):
    def test_configured_path_used_when_executable(self):
        cfg = load_config(REPO / "agent.toml", REPO)
        cfg.claude.executable = "/bin/sh"
        self.assertEqual(cfgmod.resolve_claude_executable(cfg), "/bin/sh")

    def test_configured_path_missing_raises(self):
        cfg = load_config(REPO / "agent.toml", REPO)
        cfg.claude.executable = "/definitely/not/here/claude"
        with self.assertRaises(ConfigError):
            cfgmod.resolve_claude_executable(cfg)

    def test_falls_back_to_which_then_glob(self):
        cfg = load_config(REPO / "agent.toml", REPO)
        cfg.codex.executable = ""
        with mock.patch("agent_harness.config.shutil.which", return_value="/usr/bin/codex") as w:
            self.assertEqual(cfgmod.resolve_codex_executable(cfg), "/usr/bin/codex")
            w.assert_called_once_with("codex")

    def test_error_when_nothing_resolves(self):
        cfg = load_config(REPO / "agent.toml", REPO)
        cfg.claude.executable = ""
        with mock.patch("agent_harness.config.shutil.which", return_value=None), \
             mock.patch("agent_harness.config.glob.glob", return_value=[]):
            with self.assertRaises(ConfigError):
                cfgmod.resolve_claude_executable(cfg)


class MaxTurnsProbeTests(unittest.TestCase):
    def _fake_run(self, help_text):
        def _run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, stdout=help_text, stderr="")
        return _run

    def test_false_when_help_lacks_flag(self):
        with mock.patch("agent_harness.config.subprocess.run",
                        side_effect=self._fake_run("Usage: claude [options]\n  --model\n  --print")):
            self.assertFalse(detect_max_turns_support("/x/claude"))

    def test_true_when_help_has_flag(self):
        with mock.patch("agent_harness.config.subprocess.run",
                        side_effect=self._fake_run("  --max-turns <n>  cap turns\n")):
            self.assertTrue(detect_max_turns_support("/x/claude"))

    def test_false_on_launch_error(self):
        with mock.patch("agent_harness.config.subprocess.run", side_effect=OSError("boom")):
            self.assertFalse(detect_max_turns_support("/x/claude"))


if __name__ == "__main__":
    unittest.main()
