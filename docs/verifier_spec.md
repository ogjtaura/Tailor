# Verifier specification

## Gate 1 — Schema and provenance

Fail if:

- cited fact ID does not exist,
- cited source ID does not exist,
- cited fact has been superseded by another fact (`supersedes`),
- fact belongs to a different applicant,
- required claim fields are missing.

A corrected fact is added with a fresh `confirmed_at` and `supersedes` set to
the old `fact_id`. The old fact stays in the bank for history, but any claim
that cites it fails (`SUPERSEDED_FACT`).

## Gate 2 — Protected field drift

Structured application fields must be exact matches to the canonical bank.

Example:

```text
Expected: Quality Shift Intern
Produced: Quality Engineering Intern
Result: FAIL
```

## Gate 3 — Numeric drift

Every number in a generated factual claim must be present in one of its cited facts or explicitly permitted tokens.

Example:

```text
Source: 28 ms per 90° rotation
Generated: 30 ms per 90° rotation
Result: FAIL
```

## Gate 4 — Ownership drift

A `team` fact cannot be expressed as an `individual` claim.

Example:

```text
Fact: team development best ≈7.8 seconds
Claim: "I achieved a 7.8-second solve"
Result: FAIL
```

## Gate 5 — Achievement-state drift

A `planned` or `pending_verification` fact cannot be expressed as `completed`.

Example:

```text
Fact: Guinness World Records attempt planned
Claim: "Guinness World Record holder"
Result: FAIL
```

## Gate 6 — Word limit

Question-level limits are counted in code.

## Gate 7 — Coverage

Keep two inputs separate:

- applicant coverage (`supported_requirement_ids`): the model's evidence table
  says the bank supports the requirement.
- output coverage (`requirement_ids` on produced claims): a claim in the CV or
  letter actually cites evidence for the requirement.

`coverage_report` crosses them into four states:

| state           | supported | covered | meaning                                                       |
|-----------------|-----------|---------|--------------------------------------------------------------|
| `ok`            | yes       | yes     | evidence exists and the output uses it                       |
| `gap`           | yes       | no      | evidence exists, the output does not use it — targeted add   |
| `coverage_miss` | no        | yes     | a claim cites evidence, the evidence table dropped it — fix the table |
| `unmet`         | no        | no      | no evidence at all — a decision about whether to apply       |

A supported hard requirement that is missing from the output (`gap`) triggers a
targeted content-planning loop, not a global rewrite. A `coverage_miss` is a
defect in the model's evidence table, not the prose.
