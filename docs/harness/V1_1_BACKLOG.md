# V1.1 Backlog — Efficient Verified Execution

## P0 — Prevent wasted model calls

- [ ] deterministic feasibility preflight
- [ ] protected-path/task-scope conflict detection
- [ ] `SPEC_REQUIRED` state
- [ ] reviewer finding fingerprinting
- [ ] repeated-review-finding early stop
- [ ] out-of-scope reviewer repair detection
- [ ] skip planner for precise small tasks
- [ ] no-op/already-satisfied task detection
- [ ] failure-type-specific repair budgets

## P1 — Better calls

- [ ] structured Sonnet implementation packets
- [ ] structured reviewer-driven repair packets
- [ ] deterministic source/test localization
- [ ] plan next task from current HEAD and accepted diff
- [ ] context budgets

## P2 — Review efficiency

- [ ] bounded Luna review packet
- [ ] stable review prompt prefix
- [ ] targeted repair review
- [ ] comprehensive final acceptance review
- [ ] Terra adjudication only for ambiguous/disputed cases
- [ ] reasoning-effort routing based on benchmark evidence

## P3 — Operator experience

- [ ] surface reviewer findings in console
- [ ] explicit stop reason with root cause
- [ ] `agent_harness report`
- [ ] model invocation/duration counts
- [ ] quota-aware start/preparation state

## P4 — Trust boundary

- [ ] audit `CLAUDE.md`
- [ ] audit `.claude/`
- [ ] audit `AGENTS.md`
- [ ] audit `run_prompt.md`
- [ ] audit MCP/tool configuration
- [ ] protect all model-control surfaces
- [ ] distinguish trusted instructions from untrusted repository content
