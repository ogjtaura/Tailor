import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from verifier import verify_output, verify_protected_fields, coverage_report

class VerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Synthetic, tracked fixture: the deterministic suite must depend only on
        # files committed to Git (the harness verifies an immutable candidate in a
        # temp checkout where gitignored local files are absent).
        cls.bank = json.loads((ROOT / "tests" / "fixtures" / "fact_bank.json").read_text())
        cls.cases = json.loads((ROOT / "tests" / "adversarial_cases.json").read_text())

    def test_adversarial_cases(self):
        for case in self.cases:
            with self.subTest(case=case["case"]):
                if "coverage" in case:
                    coverage = case["coverage"]
                    report = coverage_report(
                        {"requirements": [coverage["requirement"]]},
                        set(coverage["supported_requirement_ids"]),
                        set(coverage["output_requirement_ids"]),
                    )
                    row = report["requirements"][0]
                    for state, expected in coverage["expected"].items():
                        self.assertEqual(row[state], expected, msg=state)
                    continue

                bank = deepcopy(self.bank)
                bank["facts"].extend(case.get("bank_facts", []))
                failures = verify_output(bank, case["output"])
                self.assertEqual(len(failures) == 0, case["should_pass"], msg=[f.code for f in failures])
                for code in case.get("expected_codes", []):
                    self.assertTrue(any(f.code == code for f in failures), msg=[f.code for f in failures])

    def test_missing_applicant_id_fails(self):
        output = {
            "object_id": "ID-MISSING",
            "claims": [],
        }
        failures = verify_output(self.bank, output)
        self.assertTrue(any(
            f.code == "APPLICANT_MISSING" and f.severity == "error"
            for f in failures
        ), msg=[f.code for f in failures])

    def test_blank_applicant_id_fails(self):
        output = {
            "object_id": "ID-BLANK",
            "applicant_id": "",
            "claims": [],
        }
        failures = verify_output(self.bank, output)
        self.assertTrue(any(
            f.code == "APPLICANT_MISSING" and f.severity == "error"
            for f in failures
        ), msg=[f.code for f in failures])

    def test_wrong_applicant_id_fails_with_mismatch(self):
        output = {
            "object_id": "ID-WRONG",
            "applicant_id": "SOMEONE-ELSE",
            "claims": [],
        }
        failures = verify_output(self.bank, output)
        self.assertTrue(any(
            f.code == "APPLICANT_MISMATCH" and f.severity == "error"
            for f in failures
        ), msg=[f.code for f in failures])
        self.assertFalse(any(
            f.code == "APPLICANT_MISSING"
            for f in failures
        ), msg=[f.code for f in failures])

    def test_correct_applicant_id_passes_identity_gate(self):
        output = {
            "object_id": "ID-CORRECT",
            "applicant_id": self.bank["applicant_id"],
            "claims": [],
        }
        failures = verify_output(self.bank, output)
        self.assertEqual(failures, [])

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
            "applicant_id": self.bank["applicant_id"],
            "text": text,
            "word_limit": 200,
            "claims": []
        }
        failures = verify_output(self.bank, output)
        self.assertTrue(any(f.code == "WORD_LIMIT" for f in failures))

    def test_superseded_fact_cannot_be_cited(self):
        bank = json.loads(json.dumps(self.bank))
        old = dict(bank["facts"][3])          # FACT-FONTERRA-180
        old["fact_id"] = "FACT-FONTERRA-OLD"
        bank["facts"].append(old)
        bank["facts"][3]["supersedes"] = "FACT-FONTERRA-OLD"

        output = {
            "object_id": "CV",
            "applicant_id": self.bank["applicant_id"],
            "text": "Catalogued over 180 filters.",
            "claims": [{
                "claim_id": "C1",
                "text": "Catalogued over 180 filters.",
                "fact_ids": ["FACT-FONTERRA-OLD"],
                "ownership": "individual",
                "state": "completed",
            }],
        }
        failures = verify_output(bank, output)
        self.assertTrue(any(f.code == "SUPERSEDED_FACT" for f in failures))

    def test_coverage_states_are_distinct(self):
        reqs = {"requirements": [
            {"requirement_id": "R-UNMET", "text": "", "priority": "hard"},
            {"requirement_id": "R-GAP", "text": "", "priority": "hard"},
            {"requirement_id": "R-MISS", "text": "", "priority": "hard"},
            {"requirement_id": "R-OK", "text": "", "priority": "hard"},
        ]}
        rep = coverage_report(reqs, supported_requirement_ids={"R-GAP", "R-OK"},
                              output_requirement_ids={"R-MISS", "R-OK"})
        by_id = {r["requirement_id"]: r for r in rep["requirements"]}
        self.assertTrue(by_id["R-UNMET"]["unmet"])
        self.assertTrue(by_id["R-GAP"]["gap"])
        self.assertTrue(by_id["R-MISS"]["coverage_miss"])
        self.assertFalse(by_id["R-MISS"]["unmet"])
        self.assertTrue(all(not by_id["R-OK"][k] for k in ("unmet", "gap", "coverage_miss")))

if __name__ == "__main__":
    unittest.main()
