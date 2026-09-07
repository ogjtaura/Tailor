# Harness Runbook

## Before autonomous work

1. Confirm clean working tree.
2. Confirm intended branch.
3. Confirm protected acceptance criteria.
4. Confirm allowed files.
5. Confirm task is feasible within scope.
6. Prefer a deliberately RED trusted test for behavior changes.
7. Check model budget before starting.

## During a run

Use `.agent/progress.md` for progress.

Do not manually alter writer-owned files during the run.

## After a run

Record:
- run ID
- result
- candidate/checkpoint SHAs
- verification result
- review result
- major findings
- model call counts
- human intervention
- next action

Never manually accept a candidate rejected by independent review without first
resolving or adjudicating the finding.

## Canonical context

`docs/harness/STATE.md` is the concise current project snapshot.

Supporting durable context lives in the other files in this directory.
