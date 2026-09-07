# Dogfood Runs

## V1-001 — Applicant identity gate

### Attempt 1

Run:
`20260907T102228Z-6468f7`

Outcome:
`ITERATION_LIMIT`

Key lesson:
The requested production behavior conflicted with a protected existing fixture.
The writer could not update the protected specification, so no clean candidate
could satisfy both.

Safety result:
PASS — no unsafe checkpoint.

### Attempt 2

Run:
`20260907T104755Z-0e3788`

Outcome:
`ITERATION_LIMIT`

Key lesson:
The task was specified at the wrong architectural boundary. The documented
application format stores `applicant_id` on the wrapper, but the objective
restricted work to `src/verifier.py`. Independent review repeatedly identified
that `check.py` needed to participate.

Safety result:
PASS — no unsafe checkpoint.

### Attempt 3

Run:
`20260907T111314Z-97888f`

Outcome:
`ITERATION_LIMIT` with one successful intermediate checkpoint.

Checkpoint:
`d6be1eb`

Key lessons:
- task decomposition can lag actual completed work,
- no-op implementation occurred,
- deterministic tests passed while independent review found a real malformed
  wrapper edge case,
- reviewer-discovered behavior should often become a protected regression
  before another repair.

Safety result:
PASS — first reviewed subtask checkpointed; rejected second candidate did not
checkpoint.

## Dataset fields for future runs

Record:
- run ID
- objective
- baseline SHA
- final SHA
- planner calls
- implementer calls
- repair calls
- Luna reviews
- Terra reviews
- candidates frozen
- verification failures
- review findings
- repeated findings
- checkpoints
- elapsed time
- human interventions
- stop reason
- post-checkpoint defects
