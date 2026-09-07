# Harness Decisions

## 2026-09-08 — Quality gates are not the optimization target

We will reduce model usage by eliminating futile calls, not by weakening:

- deterministic verification,
- independent review,
- immutable candidate binding,
- protected paths,
- provider separation,
- CAS checkpointing.

## 2026-09-08 — Specification precedes autonomous implementation

Behavior-changing product tasks should normally have a trusted protected
acceptance test before the writer begins.

Reviewer-discovered product-contract gaps may transition the run to
`SPEC_REQUIRED`.

## 2026-09-08 — Existing agent systems are dependencies, not competitors

The project will not try to rebuild generic Claude Code or Codex capabilities.

The reusable value should concentrate in:
- task specification,
- verification,
- review,
- routing,
- budget control,
- evidence,
- auditability,
- domain rules.

## 2026-09-08 — Canonical memory is local and protected

Git-tracked protected files are the source of truth.

ChatGPT and Hermes are consumers of canonical project state, not the sole
holders of it.

## 2026-09-08 — Harness improvements must pay rent

A significant harness feature must be justified by observed real-work failure
or measurable improvement in reliability, time, or model consumption.
