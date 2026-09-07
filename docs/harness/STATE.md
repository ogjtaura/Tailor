# Tailor Engineering State

Last updated: 2026-09-08

## Current repository

- Branch: `dogfood/v1-001`
- HEAD at state creation: `5ec2d60`
- Engineering harness V1 reviewed baseline: `a9448e4`
- Current dogfood task: `V1-001`
- Reviewed partial product checkpoint: `d6be1eb`

## V1-001 status

Goal: close Tailor's applicant-identity false accept at the documented
application-wrapper boundary.

Attempts 1-3 produced useful safety and efficiency findings.

Attempt 3 successfully checkpointed the identity helper/integration work at
`d6be1eb`, then independent review discovered a remaining malformed-wrapper
case: when `objects` exists but is not a list, verification must fail closed.

A protected regression now specifies that behavior.

Current state:

- V1-001 attempt 4: READY
- specification: committed
- expected baseline state: RED
- Claude execution: PAUSED to conserve quota
- next implementation scope: minimal malformed-wrapper repair
- independent review remains mandatory

## Model allocation

- planner: Claude Sonnet
- implementer: Claude Sonnet
- repairer: Claude Sonnet
- routine reviewer: Codex Luna
- escalation reviewer: Codex Terra
- Sol/Opus: optional high-value escalation/meta-analysis only

## Non-negotiable acceptance pipeline

OBJECTIVE
→ trusted specification
→ implementation
→ immutable candidate
→ deterministic verification
→ independent review
→ atomic checkpoint

No candidate ships merely because tests pass.

## Trust boundary

Autonomous writers must not modify:

- `agent_harness/`
- `agent.toml`
- `.git/`
- `.agent/`
- `tests/`
- `docs/`
- `schemas/`

The model-control surface should be audited and expanded in V1.1.

## Immediate next action

Do not spend Claude quota yet.

First:
1. analyze V1-001 telemetry,
2. audit current role prompts,
3. finalize V1.1 design.

When Claude quota is healthy, run V1-001 attempt 4.
