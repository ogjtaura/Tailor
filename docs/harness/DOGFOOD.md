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

## V1-001 telemetry baseline

Attempts 1-3:

| Metric | Value |
|---|---:|
| Total model invocations | 28 |
| Claude invocations | 18 |
| Luna reviews | 10 |
| Terra invocations | 0 |
| Total model execution time | 39m 59s |
| Claude execution time | 25m 50s |
| Codex execution time | 14m 09s |
| Planner calls/time | 3 / 3m 53s |
| Implementer calls/time | 4 / 1m 47s |
| Repairer calls/time | 11 / 20m 11s |
| Luna calls/time | 10 / 14m 09s |

### Primary conclusion

Repair/review churn, rather than initial implementation, is the dominant
intelligence cost.

A conservative retrospective estimate suggests that scope-aware early stopping,
SPEC_REQUIRED handling, and no-op task detection could have avoided about
11/28 model calls (~39%) and ~18m26s of model execution (~46%) across attempts
2-3 without weakening final acceptance gates.

Including the spec-first lesson from attempt 1 raises the retrospective ideal
to roughly 19/28 calls (~68%) and ~29m15s (~73%), though this should be treated
as an upper-bound retrospective estimate rather than an expected immediate
V1.1 result.

### Priority implications

1. out-of-scope reviewer finding stop
2. reviewer finding fingerprinting
3. SPEC_REQUIRED
4. no-op/already-satisfied handling
5. structured repair packets
6. bounded Luna review packets
7. planner skipping for precise tasks
