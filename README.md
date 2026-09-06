# CV Tailor — Verifier-First v1

This package externalises the project state so the workflow does **not** depend on a single ChatGPT context window.

## Core principle

The writer is an untrusted transformation step. The verifier is the product.

Final outputs are assembled only from structured objects that have passed verification.

## Folder structure

- `docs/architecture.md` — system architecture and graph/loop design.
- `docs/verifier_spec.md` — verification rules and failure semantics.
- `schemas/` — JSON schemas for facts, requirements and generated outputs.
- `src/verifier.py` — deterministic verifier v2.
- `check.py` — gate one application run from the command line.
- `tests/` — adversarial tests and `probe_v1.py`.
- `applicants/<id>/fact_bank.json` — canonical facts, one bank per person.
- `jobs/<job_id>/` — `ad.txt` and parsed `requirements.json`.
- `runs/<job_id>/<date>/` — one directory per attempt (`raw_output.md`, `output.json`, `final.md`, `notes.md`).

`applicants/`, `jobs/` and `runs/` hold personal data and are gitignored; only each directory's `README.md` is tracked.

## What Verifier v2 checks

1. Protected structured fields must match the canonical fact bank exactly.
2. Every generated claim must cite valid source/fact IDs, and none that have been superseded.
3. Numbers in generated prose must be supported by cited facts, with their units.
4. Team achievements cannot silently become individual achievements (label and text).
5. Planned/pending achievements cannot be represented as completed achievements (label and text).
6. Word limits are enforced deterministically.
7. Requirement coverage separates `unmet`, `gap` and `coverage_miss` — see the spec.

## Run tests

```bash
python -m unittest discover -s tests -v
```

## Next engineering milestone

Add semantic entailment verification for atomic claims, then put it behind the same deterministic gates in `src/verifier.py`.
