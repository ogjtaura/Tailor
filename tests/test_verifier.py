import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from verifier import verify_output, verify_protected_fields

class VerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank = json.loads((ROOT / "private_fixtures" / "johann_tait_2026" / "fact_bank.json").read_text())
        cls.cases = json.loads((ROOT / "tests" / "adversarial_cases.json").read_text())

    def test_adversarial_cases(self):
        for case in self.cases:
            with self.subTest(case=case["case"]):
                failures = verify_output(self.bank, case["output"])
                self.assertEqual(len(failures) == 0, case["should_pass"], msg=[f.code for f in failures])

    def test_exact_protected_field_passes(self):
        failures = verify_protected_fields(
            self.bank, {"employment.f1.job_title": "Quality Shift Intern"}
        )
        self.assertEqual(failures, [])

    def test_protected_field_drift_fails(self):
        failures = verify_protected_fields(
            self.bank, {"employment.f1.job_title": "Quality Engineering Intern"}
        )
        self.assertTrue(any(f.code == "PROTECTED_FIELD_DRIFT" for f in failures))

    def test_word_limit(self):
        text = "word " * 201
        output = {
            "object_id": "Q1",
            "text": text,
            "word_limit": 200,
            "claims": []
        }
        failures = verify_output(self.bank, output)
        self.assertTrue(any(f.code == "WORD_LIMIT" for f in failures))

if __name__ == "__main__":
    unittest.main()
