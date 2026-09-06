import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from verifier import verify_output

bank = json.loads((ROOT / "applicants" / "jt" / "fact_bank.json").read_text())

def probe(name, claim, expect_fail=True):
    out = {"object_id": "P", "text": claim["text"], "claims": [claim]}
    f = verify_output(bank, out)
    verdict = "FAIL" if f else "PASS"
    should = "FAIL" if expect_fail else "PASS"
    flag = "  <-- FALSE ACCEPT" if (expect_fail and not f) else ("  <-- false reject" if (not expect_fail and f) else "")
    print(f"{verdict:5} (want {should:5}) {name}{flag}")
    if f:
        print(f"        codes: {[x.code for x in f]}")

C = lambda **kw: {"claim_id": "C", **kw}

print("--- A. self-reported metadata ---")
probe("A1 text says 'I' but labelled team",
      C(text="I personally achieved a 7.8-second solve.",
        fact_ids=["FACT-GWR-TEAM-7_8"], ownership="team", state="development_result"))
probe("A2 text says 'record holder' but labelled planned",
      C(text="Guinness World Record holder for fastest 4x4x4 solve.",
        fact_ids=["FACT-GWR-ATTEMPT"], ownership="team", state="planned"))
probe("A3 everything labelled non_factual",
      C(text="I single-handedly set the world record in 0.1 seconds at NASA.",
        fact_ids=[], ownership="non_factual", state="non_factual"))

print("\n--- B. all() vs any() in the gates ---")
probe("B1 individual claim, cite one individual + one team fact",
      C(text="I achieved a 7.8-second end-to-end solve.",
        fact_ids=["FACT-ROTATION-28MS", "FACT-GWR-TEAM-7_8"],
        ownership="individual", state="completed"))
probe("B2 completed claim, cite one completed + one planned fact",
      C(text="Set a Guinness World Record for the fastest robotic cube solve.",
        fact_ids=["FACT-ROTATION-28MS", "FACT-GWR-ATTEMPT"],
        ownership="individual", state="completed"))

print("\n--- C. cross-fact number recombination ---")
probe("C1 swap numbers between two cited facts",
      C(text="Catalogued over 600 filters and led 180 volunteers.",
        fact_ids=["FACT-FONTERRA-180", "FACT-SVA-600"],
        ownership="individual", state="completed"))

print("\n--- D. number regex coverage ---")
probe("D1 unit change, same digits (28 ms -> 28 s)",
      C(text="Achieved 28 s per 90 degree rotation.",
        fact_ids=["FACT-ROTATION-28MS"], ownership="individual", state="completed"))
probe("D2 no space before unit (30ms)",
      C(text="Achieved 30ms per 90 rotation.",
        fact_ids=["FACT-ROTATION-28MS"], ownership="individual", state="completed"))
probe("D3 number written as a word",
      C(text="Led over six hundred volunteers across two campaigns.",
        fact_ids=["FACT-SVA-600"], ownership="individual", state="completed"))
probe("D4 comma-formatted number",
      C(text="Catalogued over 1,800 filters.",
        fact_ids=["FACT-FONTERRA-180"], ownership="individual", state="completed"))

print("\n--- E. semantic invention with no numbers (known gap) ---")
probe("E1 responsibility inflation",
      C(text="Led the design of the Clean-In-Place filtration strategy.",
        fact_ids=["FACT-FONTERRA-180"], ownership="individual", state="completed"))
probe("E2 causal invention",
      C(text="Catalogued over 180 filters, reducing plant downtime.",
        fact_ids=["FACT-FONTERRA-180"], ownership="individual", state="completed"))
probe("E3 technology swap",
      C(text="Selected and configured 48V BLDC motors driven by Odrive Pro controllers.",
        fact_ids=["FACT-ROTATION-28MS"], ownership="individual", state="completed"))

print("\n--- F. cross-applicant / provenance ---")
probe("F1 fact from another applicant's bank (applicant_id unchecked)",
      C(text="Led over 600 volunteers.", fact_ids=["FACT-SVA-600"],
        ownership="individual", state="completed"), expect_fail=False)

print("\n--- G. supported paraphrase (false-rejection check) ---")
probe("G1 legitimate rewording",
      C(text="Identified and catalogued over 180 filters across CIP and water filtration systems.",
        fact_ids=["FACT-FONTERRA-180"], ownership="individual", state="completed"),
      expect_fail=False)
