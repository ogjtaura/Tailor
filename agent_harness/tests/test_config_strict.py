"""Every safety-relevant config model rejects unknown / misspelled fields."""

import tempfile
import unittest
from pathlib import Path

from agent_harness.config import ConfigError, load_config

REPO = Path(__file__).resolve().parents[2]


def _write(toml_text: str) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d) / "agent.toml"
    p.write_text(toml_text)
    return p


class StrictConfigTests(unittest.TestCase):
    BASE = (
        '[checks]\ncommands = ["true"]\n'
    )

    def _load(self, extra):
        return load_config(_write(self.BASE + extra), REPO)

    def test_valid_config_loads(self):
        self._load('[agent]\nmax_iterations = 5\n')

    def test_unknown_top_level_section_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('[bogus]\nx = 1\n')

    def test_unknown_agent_field_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('[agent]\nmax_iteration = 5\n')          # misspelled

    def test_unknown_safety_field_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('[safety]\nprotected_path = ["x/"]\n')   # missing the 's'

    def test_unknown_git_field_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('[git]\nrequire_clean_worktee = true\n')

    def test_unknown_claude_field_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('[claude]\nmodl = "x"\n')

    def test_unknown_codex_field_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('[codex]\nreview_retry = 2\n')

    def test_unknown_checks_field_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('[checks]\ncommand = ["true"]\n')        # note: overrides BASE's [checks]

    def test_unknown_classify_field_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('[classify]\nusage_pattern = ["x"]\n')

    def test_unknown_roles_role_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('[roles]\nplaner = { backend = "claude_code" }\n')   # misspelled role

    def test_unknown_role_assignment_field_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('[roles]\nplanner = { backend = "claude_code", modl = "x" }\n')

    def test_real_agent_toml_still_loads(self):
        load_config(REPO / "agent.toml", REPO)


if __name__ == "__main__":
    unittest.main()
