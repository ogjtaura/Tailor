# Tailor Engineering State

Last updated: 2026-09-08

## Current repository

* Branch: `dogfood/v1-001`
* HEAD when last updated: `12d2703`
* Engineering harness V1 reviewed baseline: `a9448e4`
* Current dogfood task: `V1-001`
* Reviewed partial product checkpoint: `d6be1eb`

## V1-001 status

Goal: close Tailor's applicant-identity false accept at the documented application-wrapper boundary.

Attempts 1–3 produced useful safety, specification, orchestration, and efficiency findings.

Attempt 3 successfully checkpointed the identity helper/integration work at `d6be1eb`. Independent Luna review then discovered a remaining malformed-wrapper case: when `objects` exists but is not a list, verification must fail closed.

A protected regression now specifies that behavior.

Current state:

* V1-001 attempt 4: **READY**
* trusted specification: committed
* expected baseline state: **RED**
* Claude execution: **PAUSED to conserve quota**
* next product implementation scope: minimal malformed-wrapper repair
* deterministic verification remains mandatory
* independent review remains mandatory

## V1.1 status

Current phase: **V1.1 specification and protected acceptance-test design**

Completed:

* V1-001 attempts 1–3 analyzed
* telemetry baseline established
* current Sonnet planner/implementer/repairer prompts audited
* current Luna reviewer and Terra escalation interfaces audited
* orchestration and failure-routing behavior audited
* V1.1 Efficient Verified Execution design specified

Key telemetry baseline from V1-001 attempts 1–3:

* 28 total model invocations
* 18 Claude invocations
* 10 Luna reviews
* 11 repairer calls
* approximately 40 minutes total model execution time
* repair/review churn is the dominant intelligence cost

Primary V1.1 objective:

> Minimize unnecessary Claude and Codex calls while preserving or strengthening all existing quality and safety gates.

Highest-priority V1.1 capabilities:

1. deterministic feasibility/scope preflight
2. structured reviewer findings
3. out-of-scope repair detection
4. `SPEC_REQUIRED`
5. repeated-review-finding detection
6. no-op/already-satisfied task handling
7. canonical prompt path policy
8. structured repair packets
9. bounded Luna review packets
10. role-specific prompt/system instructions

## Model allocation

* planner: Claude Sonnet
* implementer: Claude Sonnet
* repairer: Claude Sonnet
* routine reviewer: Codex Luna
* escalation/adjudication reviewer: Codex Terra
* Sol/Opus: optional high-value escalation, benchmark adjudication, or periodic meta-analysis only

## Non-negotiable acceptance pipeline

OBJECTIVE
→ trusted specification
→ implementation
→ immutable candidate
→ deterministic verification
→ independent review
→ atomic checkpoint

No candidate ships merely because tests pass.

Model-usage optimization must never weaken the final acceptance standard.

## Trust boundary

Autonomous writers must not modify:

* `agent_harness/`
* `agent.toml`
* `.git/`
* `.agent/`
* `tests/`
* `docs/`
* `schemas/`

V1.1 will also audit and protect the broader model-control surface, including instruction files and configuration capable of influencing agent behavior.

## Canonical project memory

Protected Git-tracked files under `docs/harness/` are the canonical project state.

ChatGPT is used for:

* strategy
* architecture
* research
* run diagnosis
* specification design
* prompt/interface design

Hermes may later act as a local operational interface over the canonical state, Git, tests, and harness telemetry.

Claude and Codex are execution/review workers, not canonical memory stores.

## Immediate next actions

Do not spend Claude or Codex quota yet.

Next:

1. design the first protected V1.1 acceptance tests
2. commit those tests in a deliberately RED state
3. define the smallest P0 V1.1 implementation slice
4. prepare the corresponding autonomous implementation objective
5. run V1-001 attempt 4 when Claude quota is healthy
6. benchmark V1.1 against the frozen V1 dogfood cases

The first proposed protected V1.1 tests cover:

* out-of-scope reviewer finding stops without repair
* repeated reviewer finding stops instead of looping
* reviewer can route to `SPEC_REQUIRED`
* no-op but already-satisfied task completes without another repair
* model prompt path policy matches actual protected paths
* missing required prompt context fails before a provider invocation
