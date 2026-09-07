# Harness Roadmap

## Current

### V1.0-alpha

Status: implemented and independently reviewed.

Core additions:
- role → backend → model routing
- Claude/Codex backend abstraction
- structural safe telemetry
- V0 safety properties preserved

## Immediate

### Finish V1-001

- committed trusted malformed-wrapper regression
- run attempt 4 when Claude quota is healthy
- require deterministic PASS + independent review PASS
- record final dogfood metrics

## V1.1 — Efficient Verified Execution

Goal:

Minimize unnecessary model calls while preserving the same quality and safety
gates.

Primary themes:
- deterministic preflight
- scope feasibility
- SPEC_REQUIRED
- reviewer finding fingerprints
- repeated-finding early stop
- out-of-scope repair detection
- planner skipping for precise tasks
- no-op/already-satisfied detection
- structured repair packets
- efficient review context
- clearer operator reporting
- model-control surface protection

## V1.2

- benchmark-driven prompt optimization
- run analytics/reporting
- context-budget experiments
- reasoning-effort routing
- targeted/delta repair review
- shadow audits

## V2

Only after sufficient evidence:

- safe self-hosting
- broader domain/workflow abstraction
- additional providers where justified
