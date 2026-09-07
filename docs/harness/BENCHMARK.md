# Harness Benchmark

## Purpose

Measure whether harness changes improve reliability and efficiency rather than
relying on intuition.

## Initial cases

- H001 protected-path conflict
- H002 applicant identity missing
- H003 wrapper/child identity contract
- H004 malformed `objects`
- H005 telemetry untrusted-classification injection

## Metrics

- autonomous completion rate
- first-candidate verification pass
- first-review pass
- repairs per task
- Claude calls per task
- Codex calls per task
- candidate count
- elapsed time
- human intervention count
- reviewer true-positive findings
- reviewer false positives
- post-checkpoint defects
- protected-path violations
- no-op invocations

## Baseline comparison

Compare against:

Claude Code directly
+ repository tests
+ normal human review

The harness must earn its additional complexity.
