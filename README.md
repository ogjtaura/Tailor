# CV Tailor — Verifier-First v1

This package externalises the project state so the workflow does **not** depend on a single ChatGPT context window.

## Core principle

The writer is an untrusted transformation step. The verifier is the product.

Final outputs are assembled only from structured objects that have passed verification.

## Folder structure

- `docs/architecture.md` — system architecture and graph/loop design.
- `docs/verifier_spec.md` — verification rules and failure semantics.
- `schemas/` — JSON schemas for facts, requirements and generated outputs.
- `src/verifier.py` — deterministic verifier v1.
- `tests/` — adversarial tests.
- `private_fixtures/` — applicant-specific test fixtures. This folder is gitignored by default.

## What Verifier v1 checks

1. Protected structured fields must match the canonical fact bank exactly.
2. Every generated claim must cite valid source/fact IDs.
3. Numbers in generated prose must be supported by cited facts.
4. Team achievements cannot silently become individual achievements.
5. Planned/pending achievements cannot be represented as completed achievements.
6. Word limits are enforced deterministically.
7. Requirement coverage can be reported separately from applicant capability.

## Run tests

```bash
python -m unittest discover -s tests -v
```

## Next engineering milestone

Add semantic entailment verification for atomic claims, then put it behind the same deterministic gates in `src/verifier.py`.
