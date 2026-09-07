"""The bundled JSON schemas are handed to `claude --json-schema` /
`codex --output-schema` as literal JSON. The installed Claude CLI rejects a
top-level `$schema` dialect declaration ("no schema with key or ref
https://json-schema.org/draft/2020-12/schema"), so the files must not carry one -
while keeping their existing structural contract.
"""

import json
import unittest
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _load(name):
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


class PlanSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = _load("plan.schema.json")

    def test_no_dollar_schema_dialect_declaration(self):
        self.assertNotIn("$schema", self.schema)

    def test_still_the_expected_plan_shape(self):
        self.assertEqual(self.schema["type"], "object")
        self.assertEqual(self.schema["required"], ["tasks"])
        item = self.schema["properties"]["tasks"]["items"]
        self.assertEqual(item["type"], "object")
        self.assertEqual(sorted(item["required"]), ["id", "rationale", "title"])
        for field in ("id", "title", "rationale"):
            self.assertEqual(item["properties"][field]["type"], "string")
        self.assertIs(self.schema["additionalProperties"], False)
        self.assertIs(item["additionalProperties"], False)


class ReviewVerdictSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = _load("review_verdict.schema.json")

    def test_no_dollar_schema_dialect_declaration(self):
        self.assertNotIn("$schema", self.schema)

    def test_still_the_expected_verdict_shape(self):
        self.assertEqual(self.schema["type"], "object")
        self.assertEqual(sorted(self.schema["required"]), ["findings", "severity", "verdict"])
        self.assertEqual(self.schema["properties"]["verdict"]["enum"], ["pass", "fail"])
        self.assertEqual(
            self.schema["properties"]["severity"]["enum"],
            ["none", "minor", "major", "critical"],
        )
        finding = self.schema["properties"]["findings"]["items"]
        self.assertEqual(
            sorted(finding["required"]), ["description", "evidence", "id", "suggested_fix"]
        )
        self.assertIs(self.schema["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()
