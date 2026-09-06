#!/usr/bin/env python3
"""Gate one application run. Offline, stdlib only, no API.

    python check.py runs/tait-2026/2026-09-06

Expects in that folder:
    output.json        pasted from the model (see schemas/application_output.schema.json)
                       with _bank_path / _reqs_path pointing at the applicant
                       and job files.

Or pass paths explicitly:

    python check.py --bank applicants/jt/fact_bank.json \
                    --reqs jobs/tait-2026/requirements.json \
                    --output runs/tait-2026/2026-09-06/output.json

Exit code 0 if clean, 1 if any error-severity failure.
"""
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from verifier import verify_output, verify_protected_fields, coverage_report

BOLD, RED, YEL, GRN, DIM, OFF = "\033[1m", "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[0m"


def load(p):
    return json.loads(Path(p).read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--bank")
    ap.add_argument("--reqs")
    ap.add_argument("--output")
    a = ap.parse_args()

    if a.run_dir and not a.output:
        a.output = str(Path(a.run_dir) / "output.json")
    if not a.output:
        ap.error("need a run_dir or --output")

    out = load(a.output)
    bank = load(a.bank or out["_bank_path"])
    reqs = load(a.reqs or out["_reqs_path"]) if (a.reqs or out.get("_reqs_path")) else None

    failures = []
    for obj in out.get("objects", [out]):
        failures += verify_output(bank, obj)
    if out.get("protected_fields_produced"):
        failures += verify_protected_fields(bank, out["protected_fields_produced"])

    errors = [f for f in failures if f.severity == "error"]
    reviews = [f for f in failures if f.severity == "review"]

    print(f"\n{BOLD}GATES{OFF}")
    if not failures:
        print(f"  {GRN}clean{OFF}")
    for f in errors:
        loc = f"{f.object_id}/{f.claim_id}".strip("/")
        print(f"  {RED}FAIL{OFF} {f.code:24} {DIM}{loc}{OFF}\n       {f.message}")
    for f in reviews:
        loc = f"{f.object_id}/{f.claim_id}".strip("/")
        print(f"  {YEL}CHECK{OFF} {f.code:23} {DIM}{loc}{OFF}\n       {f.message}")

    if reqs:
        # supported: what the evidence table claimed. covered: what the claims actually cite.
        supported = set(out.get("supported_requirement_ids", []))
        covered = set()
        for obj in out.get("objects", [out]):
            for c in obj.get("claims", []):
                covered |= set(c.get("requirement_ids", []))

        rep = coverage_report(reqs, supported, covered)
        print(f"\n{BOLD}COVERAGE{OFF}")
        for r in rep["requirements"]:
            if r["unmet"]:
                mark, note = f"{RED}none{OFF} ", "no evidence in bank"
            elif r["coverage_miss"]:
                mark, note = f"{YEL}miss{OFF} ", "a claim cites evidence for this, but supported_requirement_ids omits it"
            elif r["gap"]:
                mark, note = f"{YEL}gap{OFF}  ", "you have evidence, the output does not use it"
            else:
                mark, note = f"{GRN}ok{OFF}   ", ""
            print(f"  {mark} [{r['priority']:9}] {r['requirement_id']:10} {r['text'][:52]}")
            if note:
                print(f"         {DIM}{note}{OFF}")

        gaps = [r for r in rep["requirements"] if r["gap"]]
        misses = [r for r in rep["requirements"] if r["coverage_miss"]]
        unmet = [r for r in rep["requirements"] if r["unmet"] and r["priority"] == "hard"]
        print(f"\n  {len(gaps)} gap(s) to fix by adding content. "
              f"{len(misses)} coverage miss(es) to fix in the evidence table. "
              f"{len(unmet)} hard requirement(s) with no evidence at all.")
        if unmet:
            print(f"  {DIM}Hard requirements with no evidence are a decision about whether to "
                  f"apply, not a writing problem.{OFF}")

    print(f"\n{BOLD}{len(errors)} error(s), {len(reviews)} to check by hand{OFF}\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
