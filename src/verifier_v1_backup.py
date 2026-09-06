import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Set

NUMBER_RE = re.compile(r"(?<!\w)(?:\d+(?:\.\d+)?)(?:\+|%)?(?!\w)")

@dataclass
class Failure:
    code: str
    message: str
    object_id: str = ""
    claim_id: str = ""

def _fact_index(bank: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {f["fact_id"]: f for f in bank.get("facts", [])}

def _source_ids(bank: Dict[str, Any]) -> Set[str]:
    return {s["source_id"] for s in bank.get("sources", [])}

def extract_numbers(text: str) -> Set[str]:
    return set(NUMBER_RE.findall(text or ""))

def verify_protected_field(bank: Dict[str, Any], field_path: str, produced_value: Any) -> List[Failure]:
    cur = bank.get("protected_fields", {})
    try:
        for part in field_path.split("."):
            cur = cur[part]
    except (KeyError, TypeError):
        return [Failure("UNKNOWN_PROTECTED_FIELD", f"No canonical value for {field_path}")]
    if cur != produced_value:
        return [Failure(
            "PROTECTED_FIELD_DRIFT",
            f"{field_path}: expected {cur!r}, produced {produced_value!r}"
        )]
    return []

def verify_output(bank: Dict[str, Any], output_obj: Dict[str, Any]) -> List[Failure]:
    failures: List[Failure] = []
    facts = _fact_index(bank)
    sources = _source_ids(bank)
    object_id = output_obj.get("object_id", "")

    limit = output_obj.get("word_limit")
    if limit is not None:
        count = len(re.findall(r"\b[\w'-]+\b", output_obj.get("text", "")))
        if count > limit:
            failures.append(Failure(
                "WORD_LIMIT",
                f"{count} words exceeds limit of {limit}",
                object_id=object_id
            ))

    for claim in output_obj.get("claims", []):
        claim_id = claim.get("claim_id", "")
        claim_text = claim.get("text", "")
        fact_ids = claim.get("fact_ids", [])

        if claim.get("ownership") == "non_factual":
            continue

        if not fact_ids:
            failures.append(Failure(
                "NO_PROVENANCE",
                "Factual claim has no cited fact IDs",
                object_id, claim_id
            ))
            continue

        cited = []
        for fid in fact_ids:
            if fid not in facts:
                failures.append(Failure(
                    "UNKNOWN_FACT",
                    f"Unknown fact ID: {fid}",
                    object_id, claim_id
                ))
            else:
                cited.append(facts[fid])

        # Validate source IDs on cited facts.
        for fact in cited:
            for sid in fact.get("source_ids", []):
                if sid not in sources:
                    failures.append(Failure(
                        "UNKNOWN_SOURCE",
                        f"Fact {fact['fact_id']} cites unknown source {sid}",
                        object_id, claim_id
                    ))

        # Numeric drift.
        claim_nums = extract_numbers(claim_text)
        allowed_nums = set()
        for fact in cited:
            allowed_nums |= extract_numbers(str(fact.get("text", "")))
            allowed_nums |= extract_numbers(str(fact.get("value", "")))
        unsupported = claim_nums - allowed_nums
        if unsupported:
            failures.append(Failure(
                "NUMERIC_DRIFT",
                f"Unsupported number(s): {sorted(unsupported)}; allowed from cited facts: {sorted(allowed_nums)}",
                object_id, claim_id
            ))

        # Ownership drift.
        claim_ownership = claim.get("ownership")
        if claim_ownership == "individual" and cited:
            if all(f.get("ownership") != "individual" for f in cited):
                failures.append(Failure(
                    "OWNERSHIP_DRIFT",
                    "Individual claim is supported only by non-individual facts",
                    object_id, claim_id
                ))

        # Achievement-state drift.
        claim_state = claim.get("state")
        if claim_state == "completed" and cited:
            bad_states = {"planned", "pending_verification"}
            if all(f.get("state") in bad_states for f in cited):
                failures.append(Failure(
                    "STATE_DRIFT",
                    "Completed claim is supported only by planned/pending facts",
                    object_id, claim_id
                ))

    return failures

def coverage_report(requirement_bank: Dict[str, Any], supported_requirement_ids: Set[str], output_requirement_ids: Set[str]) -> Dict[str, Any]:
    rows = []
    for req in requirement_bank.get("requirements", []):
        rid = req["requirement_id"]
        rows.append({
            "requirement_id": rid,
            "priority": req["priority"],
            "applicant_supported": rid in supported_requirement_ids,
            "output_covered": rid in output_requirement_ids,
            "gap": (rid in supported_requirement_ids) and (rid not in output_requirement_ids),
        })
    return {"requirements": rows}

def failures_to_dict(failures: List[Failure]):
    return [asdict(f) for f in failures]
