# Verifier specification

## Gate 1 — Schema and provenance

Fail if:

- cited fact ID does not exist,
- cited source ID does not exist,
- fact belongs to a different applicant,
- required claim fields are missing.

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

Keep two concepts separate:

- applicant coverage: does the applicant have evidence for the requirement?
- output coverage: did the CV/letter actually communicate that evidence?

A supported hard requirement that is missing from the output should trigger a targeted content-planning loop, not a global rewrite.
