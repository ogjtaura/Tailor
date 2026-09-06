"""Integration: the REAL deterministic checks from agent.toml must run and pass
in this repository. Not mocked - this is the P0 gate that must actually work."""

import unittest
from pathlib import Path

from agent_harness import verifier
from agent_harness.config import ConfigError, load_config

REPO = Path(__file__).resolve().parents[2]


class ConfiguredChecksTests(unittest.TestCase):
    def test_every_configured_check_exits_zero_in_this_repo(self):
        cfg = load_config(REPO / "agent.toml", REPO)
        self.assertTrue(cfg.checks.commands, "agent.toml must configure checks")
        results = verifier.run_checks(cfg.checks.commands, cwd=REPO, timeout_s=600)
        for r in results:
            self.assertEqual(
                r.exit_code, 0,
                msg=f"configured check failed: {r.command}\n"
                    f"--- stdout ---\n{r.stdout_tail}\n--- stderr ---\n{r.stderr_tail}",
            )
        self.assertTrue(verifier.checks_passed(results))

    def test_empty_check_configuration_is_rejected_for_autonomous_run(self):
        cfg = load_config(REPO / "agent.toml", REPO)
        cfg.checks.commands = []
        with self.assertRaises(ConfigError):
            cfg.require_runnable()

        cfg.checks.commands = ["", "   "]
        with self.assertRaises(ConfigError):
            cfg.require_runnable()

    def test_nonempty_configuration_is_accepted(self):
        cfg = load_config(REPO / "agent.toml", REPO)
        cfg.require_runnable()  # must not raise


if __name__ == "__main__":
    unittest.main()
