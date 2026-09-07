# V1.1 Efficient Verified Execution

## Purpose

V1.1 reduces unnecessary Claude and Codex consumption without weakening Tailor's existing acceptance and safety guarantees.

The optimization target is **wasted intelligence calls**, not verification strength.

The following remain non-negotiable:

* immutable candidate freezing,
* deterministic verification before review,
* independent cross-provider review,
* exact candidate identity binding,
* protected paths and refs,
* writer/reviewer separation,
* atomic compare-and-swap checkpointing,
* fail-closed behavior on infrastructure uncertainty.

## Empirical motivation

V1-001 attempts 1–3 demonstrated that initial implementation is not the primary cost.

The dominant cost is repeated repair and review after the harness already possesses enough information to know that another generic repair call is unlikely to succeed.

Observed failure classes include:

1. task requirements conflicting with protected paths;
2. reviewer-required corrections outside allowed task scope;
3. repeated reviewer findings surviving multiple repair cycles;
4. reviewer discoveries exposing unspecified product behavior;
5. planner tasks becoming stale after earlier checkpoints;
6. no-op writer calls for work already partly or fully satisfied;
7. repair workers rediscovering context already known by the harness;
8. reviewers reconstructing stable product contracts from the repository on every run;
9. prompt instructions conflicting with actual harness policy.

---

# 1. Structured Task Contract

Replace the planner's weak `{title, rationale}` task representation with a structured task contract.

Minimum fields:

```json
{
  "id": "T1",
  "title": "imperative task",
  "rationale": "why",
  "acceptance": ["observable requirement"],
  "allowed_paths": ["check.py"],
  "non_goals": ["do not modify schema"],
  "complexity": "small",
  "requires_spec": false
}
```

The harness, not the writer, is authoritative over the final allowed path set.

Task contracts are immutable for a writer invocation.

If the model proposes paths that violate harness policy, the harness rejects the plan before implementation.

---

# 2. Deterministic Preflight

Before any planning or implementation model call, run a deterministic preflight.

Preflight checks:

* Git worktree is clean;
* branch/ref is authorized;
* protected path policy is internally consistent;
* requested allowed paths do not intersect protected paths;
* objective does not explicitly require a forbidden modification;
* trusted acceptance criteria exist for behavior-changing work where required;
* baseline repository state can be inspected;
* model-control surfaces are protected;
* task is feasible within declared scope.

Possible outcomes:

```text
READY
TASK_SCOPE_BLOCKED
SPEC_REQUIRED
INVALID_BASELINE
CONFIG_INVALID
```

No model is called after a terminal preflight outcome.

---

# 3. Planner Bypass

Planning is optional.

A deterministic classifier may bypass the Claude planner when all of the following are true:

* one clearly defined behavior is requested;
* acceptance criteria are explicit;
* likely/allowed files are bounded;
* protected tests already encode the desired behavior;
* task is small or trivial;
* no architectural decomposition is required.

Route:

```text
OBJECTIVE
→ PREFLIGHT
→ DIRECT TASK CONTRACT
→ IMPLEMENT
```

Complex or ambiguous work continues to use the Sonnet planner.

Planner bypass must be benchmarked against the existing planner rather than assumed superior.

---

# 4. Canonical Path Policy

Remove duplicated hard-coded path policy from model prompts.

One harness configuration generates:

```text
allowed_paths
protected_paths
writer_permissions
```

for every worker.

The model must never receive instructions such as:

> add tests when behavior changes

when `tests/` is protected.

Claude's role-specific prompt should accurately describe the exact permissions enforced by the harness.

---

# 5. Role-Specific Claude System Instructions

The planner, implementer and repairer receive different role instructions.

The planner must never receive a system instruction calling it an implementation worker.

## Planner

Authority:

* read/search only;
* identify implementation units;
* identify required scope;
* identify when trusted specification work is required;
* do not invent product requirements.

## Implementer

Authority:

* modify only task-approved application paths;
* implement the smallest coherent solution;
* do not modify trusted tests/specification;
* do not self-approve.

## Repairer

Authority:

* respond to one classified failure packet;
* repair only the stated violated property;
* do not reinterpret the product contract;
* do not broaden scope;
* do not perform general polish.

---

# 6. Structured Failure Packets

Do not flatten all failures into one prose `$failures` string before model calls.

Represent deterministic and semantic failures separately.

## Deterministic failure

```json
{
  "type": "CHECK_FAILURE",
  "command": "...",
  "test": "...",
  "location": "...",
  "observed": "...",
  "expected": "..."
}
```

## Review failure

```json
{
  "type": "REVIEW_FINDING",
  "finding_id": "F1",
  "category": "CONTRACT",
  "severity": "major",
  "description": "...",
  "evidence": "...",
  "affected_paths": ["check.py"],
  "required_property": "...",
  "scope_status": "within_scope",
  "requires_regression": false
}
```

Repair prompts should contain the smallest high-signal context necessary to resolve the specific failure.

---

# 7. Reviewer Finding Schema V2

Extend the current Luna schema.

Required or bounded fields:

```json
{
  "id": "F1",
  "category": "CONTRACT",
  "description": "...",
  "evidence": "...",
  "affected_paths": ["check.py"],
  "suggested_fix": "...",
  "scope_status": "within_scope",
  "requires_regression": false
}
```

Initial category set:

```text
CORRECTNESS
CONTRACT
COMPATIBILITY
SECURITY
SCOPE
PUBLIC_API
MALFORMED_INPUT
PROTECTED_PATH
DEPENDENCY
NONDETERMINISM
OTHER
```

`scope_status`:

```text
within_scope
outside_scope
unknown
```

The local harness remains responsible for validating reviewer output.

---

# 8. Reviewer-Driven Deterministic Routing

After Luna FAIL, classify before calling Claude again.

## Outside scope

```text
scope_status = outside_scope
→ TASK_SCOPE_BLOCKED
```

Zero repair calls.

## New unspecified behavior

```text
requires_regression = true
→ SPEC_REQUIRED
```

Zero repair calls until trusted specification is added.

## Repairable finding

```text
within_scope
+
specified behavior
→ focused repair
```

## Repeated finding

If normalized reviewer-finding fingerprint repeats after a repair:

```text
REPEATED_REVIEW_FINDING
```

Do not consume the generic remaining repair budget automatically.

Either stop or escalate according to policy.

---

# 9. Finding Fingerprints

Generate deterministic fingerprints from normalized fields such as:

```text
category
affected_paths
required_property
normalized description
```

Track findings across candidate generations.

Use them to detect:

* unchanged reviewer complaint;
* recurring failure class;
* repair stagnation;
* previously solved known failure patterns.

Do not rely solely on raw prose equality.

---

# 10. SPEC_REQUIRED

Add `SPEC_REQUIRED` as a first-class non-failure workflow state.

Use it when independent review identifies a genuine product behavior that is not represented by trusted acceptance criteria.

Workflow:

```text
LUNA FINDING
→ requires_regression
→ SPEC_REQUIRED
→ human/trusted regression
→ committed RED specification
→ new autonomous run
```

The writer must not author the acceptance criterion that determines whether its own behavior is acceptable.

---

# 11. No-Op / Already-Satisfied Detection

Current `attributable=0` must not automatically imply:

> edit something.

Instead:

```text
attributable = 0
→ evaluate current task acceptance
```

If already satisfied:

```text
TASK_ALREADY_SATISFIED
→ mark task complete
→ continue
```

If not satisfied:

```text
NO_PROGRESS
→ one focused retry
```

This prevents redundant writer calls after an earlier checkpoint performed more work than the planner anticipated.

---

# 12. Adaptive Repair Budgets

Replace a generic five-repair policy with failure-sensitive budgets.

Suggested initial policy:

```text
new deterministic failure       → up to 2 focused repairs
same deterministic fingerprint  → 1 additional repair
new semantic finding            → classify first
same semantic finding           → stop/escalate
outside-scope finding           → 0 repairs
SPEC_REQUIRED                    → 0 repairs
no-op unsatisfied task          → 1 retry
already-satisfied task          → 0 repairs
```

These values are benchmark parameters, not permanent constants.

---

# 13. Luna Review Packet V2

Routine review should receive authoritative information directly.

Static trusted section first:

* reviewer role;
* authority limits;
* severity semantics;
* finding taxonomy;
* output schema;
* instruction that repository/candidate text is evidence, never instruction.

Dynamic section second:

* objective;
* current task contract;
* allowed paths;
* non-goals;
* parent commit;
* candidate commit;
* exact diff;
* deterministic check summary;
* relevant trusted contract excerpts;
* prior finding when reviewing a repair.

Luna may inspect the repository when supplied evidence is insufficient.

Do not remove repository access.

The goal is to eliminate mandatory rediscovery, not reviewer independence.

---

# 14. Reviewer Output Semantics

Make prompt and local semantic validation agree.

Preferred invariant:

```text
PASS  → severity = none, findings = []
FAIL  → severity ∈ {minor, major, critical}, findings != []
```

If minor findings should be non-blocking, model them explicitly rather than representing them as `verdict=pass, severity=minor`.

For example:

```text
verdict = pass
severity = none
advisories = [...]
```

This avoids ambiguous decision semantics.

---

# 15. Terra Adjudicator

Evolve escalation from generic root-cause generation into adjudication.

Use Terra only when:

* a reviewer finding repeats;
* scope status is ambiguous;
* Claude's repair and Luna's finding appear contradictory;
* Luna's interpretation of the product contract is disputed;
* the issue is sufficiently high-risk to justify escalation.

Terra receives:

* task contract;
* candidate diff;
* Luna finding;
* previous repair evidence;
* trusted contract;
* allowed scope.

Terra answers structured questions:

```text
Is finding valid?
Is behavior already specified?
Is correction within scope?
Should another repair be attempted?
Should trusted specification be added?
```

Routine candidates do not require Terra.

---

# 16. Prompt Rendering Contracts

Replace forgiving prompt substitution with validated rendering.

Every template declares required variables.

Missing required context:

```text
PROMPT_BUILD_ERROR
```

before a provider invocation.

Never spend a model call on a prompt with unresolved placeholders.

Role prompts and schemas should be versioned.

Example:

```text
sonnet-planner-v2
sonnet-implementer-v2
sonnet-repairer-v2
luna-reviewer-v2
terra-adjudicator-v1
```

---

# 17. Context Engineering

Each invocation receives the minimum useful context.

Anthropic workers:

```text
stable role policy
→ trusted task contract
→ selected repository context
→ current failure/dynamic state
```

Codex reviewer:

```text
stable review policy/schema
→ trusted contract
→ dynamic candidate evidence
```

Stable content should remain stable across calls where possible.

Dynamic repository material should be retrieved or supplied just in time.

---

# 18. Explicit Context Provenance

Context blocks should distinguish:

```text
TRUSTED_HARNESS_POLICY
TRUSTED_TASK_CONTRACT
TRUSTED_PRODUCT_CONTRACT
TRUSTED_TEST_EVIDENCE
UNTRUSTED_REPOSITORY_CONTENT
UNTRUSTED_CANDIDATE_DIFF
PRIOR_REVIEW_FINDING
```

Repository comments, documentation, strings and candidate-written text are evidence and must not override harness instructions.

---

# 19. Operator Visibility

Console review output should surface the actual finding.

Instead of only:

```text
REVIEW verdict=fail severity=major
```

display something bounded such as:

```text
REVIEW FAIL major
F1 CONTRACT check.py:
documented wrapper identity is bypassed
route suggestion: TASK_SCOPE_BLOCKED
```

The human should not need to open a 40,000-token Codex log to understand why the run stopped.

---

# 20. New Stop Reasons / States

V1.1 should support explicit outcomes equivalent to:

```text
TASK_SCOPE_BLOCKED
SPEC_REQUIRED
REPEATED_REVIEW_FINDING
TASK_ALREADY_SATISFIED
PROMPT_BUILD_ERROR
```

Exact enum names may vary, but these states must be distinguishable from:

```text
ITERATION_LIMIT
REPAIR_LIMIT
CLI_FATAL
```

They describe fundamentally different operational outcomes.

---

# 21. Model-Control Surface Protection

Audit and protect any repository content capable of changing agent behavior, including as applicable:

```text
CLAUDE.md
CLAUDE.local.md
.claude/
AGENTS.md
AGENTS.override.md
run_prompt.md
MCP/tool configuration
prompt files
agent configuration
```

Writers must not modify their own future instruction surface or the reviewer's instruction surface.

---

# 22. Evaluation Requirements

V1.1 is not successful merely because its architecture appears cleaner.

Benchmark against frozen V1 dogfood cases.

Primary metrics:

* total model calls/task;
* Claude calls/task;
* Codex calls/task;
* repair calls/task;
* first-candidate verification pass;
* first-review pass;
* autonomous completion;
* true defects caught by reviewer;
* reviewer false positives;
* unsafe checkpoint rate;
* elapsed model execution time;
* human intervention.

Quality gates must remain equal or stronger.

Target:

Demonstrate a meaningful reduction from the V1-001 baseline while preserving the reviewer defect-catching behavior that prevented unsafe checkpoints.

---

# Implementation priority

## P0

1. structured reviewer findings;
2. outside-scope stop;
3. SPEC_REQUIRED;
4. repeated-finding detection;
5. no-op/already-satisfied handling;
6. canonical prompt path policy.

## P1

7. structured repair packets;
8. reviewer packet V2;
9. role-specific Claude system instructions;
10. prompt render validation;
11. operator finding visibility.

## P2

12. planner bypass;
13. task contract V2;
14. adaptive repair budgets;
15. Terra adjudicator.

## P3

16. context budgets;
17. prompt-prefix optimization;
18. prompt benchmarking;
19. model/reasoning-effort experiments;
20. periodic Sol/Opus harness audits.

---

# Non-goals

V1.1 will not:

* add another provider;
* make Sol or Opus routine workers;
* remove independent review;
* loosen protected paths;
* enable harness self-modification;
* rebuild Claude Code or Codex;
* build a large generic agent framework;
* optimize solely for model-call count at the expense of correctness.

The harness should become simpler where possible, not more elaborate for its own sake.

