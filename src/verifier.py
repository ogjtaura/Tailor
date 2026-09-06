"""Deterministic verifier v2.

Changes from v1, each closing a measured false accept:
  - ownership/state gates use any(), not all()
  - individual claims may cite only individual facts (strict; false rejects are cheap)
  - completed claims may cite only completed/current facts
  - numbers are bound to their units, and spelled-out numbers are caught
  - claim TEXT is checked against the declared labels, so a mislabelled claim
    no longer bypasses the gates
  - non_factual no longer silently skips everything
  - protected fields are swept, not just checkable one at a time
  - applicant_id is enforced

No third-party dependencies. No network. Runs offline, free.
"""

import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Set, Tuple

# number, optional unit with or without a space: 28ms, 28 ms, 7.8 seconds, 600+, 95%
NUM_UNIT_RE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)[-\s]*([%+]|[a-zA-Z\u00b0]{1,12})?")

WORD_NUMBERS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "hundred": "100", "thousand": "1000",
}

FIRST_PERSON_RE = re.compile(r"\b(I|I'm|I've|my|myself|personally|single-handedly)\b", re.I)
COLLECTIVE_RE = re.compile(r"\b(we|our|team|group|collectively|jointly)\b", re.I)
COMPLETED_RE = re.compile(
    r"\b(achieved|achieving|holds?|held|won|winner|set a|set the|record holder|"
    r"delivered|completed|awarded|certified|broke)\b", re.I)
PLANNED_RE = re.compile(r"\b(preparing|planned|upcoming|will|intend|aiming|scheduled|pending)\b", re.I)

INDIVIDUAL_OK = {"individual"}
NOT_COMPLETED = {"planned", "pending_verification", "development_result"}


@dataclass
class Failure:
    code: str
    message: str
    object_id: str = ""
    claim_id: str = ""
    severity: str = "error"   # error | review


def _fact_index(bank): return {f["fact_id"]: f for f in bank.get("facts", [])}
def _source_ids(bank): return {s["source_id"] for s in bank.get("sources", [])}


def extract_num_units(text: str) -> Set[Tuple[str, str]]:
    """Return {(normalised_number, unit)}. Unit is '' when absent."""
    out = set()
    for num, unit in NUM_UNIT_RE.findall(text or ""):
        n = num.replace(",", "").rstrip(".")
        if n.endswith(".0"):
            n = n[:-2]
        u = (unit or "").lower()
        if len(u) > 3 and u.endswith("s"):
            u = u[:-1]
        out.add((n, u))
    for word, digits in WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", text or "", re.I):
            out.add((digits, ""))
    return out


def _fact_num_units(fact) -> Set[Tuple[str, str]]:
    return extract_num_units(str(fact.get("text", ""))) | extract_num_units(str(fact.get("value", "")))


def verify_protected_fields(bank: Dict[str, Any], produced: Dict[str, Any]) -> List[Failure]:
    """Sweep every leaf under protected_fields, using dotted paths."""
    failures, seen = [], set()

    def walk(canon, path):
        if isinstance(canon, dict):
            for k, v in canon.items():
                walk(v, f"{path}.{k}" if path else k)
            return
        seen.add(path)
        if path in produced and produced[path] != canon:
            failures.append(Failure("PROTECTED_FIELD_DRIFT",
                f"{path}: expected {canon!r}, produced {produced[path]!r}"))

    walk(bank.get("protected_fields", {}), "")
    for path in produced:
        if path not in seen:
            failures.append(Failure("UNKNOWN_PROTECTED_FIELD", f"No canonical value for {path}"))
    return failures


def verify_output(bank: Dict[str, Any], output_obj: Dict[str, Any]) -> List[Failure]:
    failures: List[Failure] = []
    facts, sources = _fact_index(bank), _source_ids(bank)
    object_id = output_obj.get("object_id", "")

    if output_obj.get("applicant_id") and output_obj["applicant_id"] != bank.get("applicant_id"):
        failures.append(Failure("APPLICANT_MISMATCH",
            f"Output is for {output_obj['applicant_id']}, bank is {bank.get('applicant_id')}", object_id))

    limit = output_obj.get("word_limit")
    if limit is not None:
        count = len(re.findall(r"\b[\w'-]+\b", output_obj.get("text", "")))
        if count > limit:
            failures.append(Failure("WORD_LIMIT", f"{count} words exceeds limit of {limit}", object_id))

    for claim in output_obj.get("claims", []):
        cid = claim.get("claim_id", "")
        text = claim.get("text", "")
        fact_ids = claim.get("fact_ids", [])
        own = claim.get("ownership")
        state = claim.get("state")

        # v1 hole: non_factual skipped every gate. Now it must earn the label.
        if own == "non_factual":
            if extract_num_units(text) or fact_ids:
                failures.append(Failure("BAD_NON_FACTUAL",
                    "Labelled non_factual but carries numbers or citations", object_id, cid))
            else:
                failures.append(Failure("NON_FACTUAL_REVIEW",
                    "Labelled non_factual, gates skipped. Confirm by hand.",
                    object_id, cid, severity="review"))
            continue

        if not fact_ids:
            failures.append(Failure("NO_PROVENANCE", "Factual claim has no cited fact IDs", object_id, cid))
            continue

        cited = []
        for fid in fact_ids:
            if fid not in facts:
                failures.append(Failure("UNKNOWN_FACT", f"Unknown fact ID: {fid}", object_id, cid))
            else:
                cited.append(facts[fid])
        if not cited:
            continue

        for f in cited:
            for sid in f.get("source_ids", []):
                if sid not in sources:
                    failures.append(Failure("UNKNOWN_SOURCE",
                        f"Fact {f['fact_id']} cites unknown source {sid}", object_id, cid))

        # numbers, bound to units
        allowed = set()
        for f in cited:
            allowed |= _fact_num_units(f)
        allowed_bare = {n for n, _ in allowed}
        for n, u in extract_num_units(text):
            if (n, u) in allowed:
                continue
            if u == "" and n in allowed_bare:
                continue
            if n in allowed_bare:
                failures.append(Failure("UNIT_DRIFT",
                    f"Number {n} appears in a cited fact but not with unit {u!r}", object_id, cid))
            else:
                failures.append(Failure("NUMERIC_DRIFT",
                    f"Unsupported number {n}{u}; cited facts allow {sorted(allowed)}", object_id, cid))

        # ownership: strict, plus text cross-check
        if own == "individual" and any(f.get("ownership") not in INDIVIDUAL_OK for f in cited):
            failures.append(Failure("OWNERSHIP_DRIFT",
                "Individual claim cites a team/organisation fact", object_id, cid))
        if own in ("team", "organisation") and FIRST_PERSON_RE.search(text) and not COLLECTIVE_RE.search(text):
            failures.append(Failure("OWNERSHIP_TEXT_MISMATCH",
                f"Labelled {own} but the text reads as individual", object_id, cid))
        if own == "individual" and COLLECTIVE_RE.search(text) and not FIRST_PERSON_RE.search(text):
            failures.append(Failure("OWNERSHIP_TEXT_MISMATCH",
                "Labelled individual but the text reads as collective", object_id, cid, severity="review"))

        # state: strict, plus text cross-check
        if state == "completed" and any(f.get("state") in NOT_COMPLETED for f in cited):
            failures.append(Failure("STATE_DRIFT",
                "Completed claim cites a planned/pending/development fact", object_id, cid))
        if state in {"planned", "pending_verification"} and COMPLETED_RE.search(text) and not PLANNED_RE.search(text):
            failures.append(Failure("STATE_TEXT_MISMATCH",
                f"Labelled {state} but the text asserts completion", object_id, cid))

    return failures


def coverage_report(requirement_bank, supported_requirement_ids, output_requirement_ids):
    rows = []
    for req in requirement_bank.get("requirements", []):
        rid = req["requirement_id"]
        supported = rid in supported_requirement_ids
        covered = rid in output_requirement_ids
        rows.append({
            "requirement_id": rid,
            "priority": req["priority"],
            "text": req.get("text", ""),
            "applicant_supported": supported,
            "output_covered": covered,
            "gap": supported and not covered,
            "unmet": not supported,
        })
    return {"requirements": rows}


def failures_to_dict(failures): return [asdict(f) for f in failures]
