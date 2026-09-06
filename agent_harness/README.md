# agent_harness — local autonomous engineering-agent harness (V0)

A small orchestration loop that drives an implement → verify → review →
checkpoint cycle over **this** repository, using the local `claude` and `codex`
CLIs as subprocess workers. It is isolated from the Tailor application: it never
imports application code and never edits it itself — only the Claude worker does.

**V0 status:** wired and unit-tested with mocked subprocesses; a `--dry-run`
mode; one real read-only Codex smoke. No autonomous Claude run has been
performed. Activate deliberately.

## Architecture

| Concern | Module |
|---|---|
| Config (`agent.toml`) + CLI resolution | `config.py` |
| Typed, serialisable graph state | `state.py` |
| LangGraph graph: nodes + cyclic edges | `graph.py` |
| Deterministic project checks | `verifier.py` |
| Git introspection + checkpoint commits | `git_tools.py` |
| Recoverable state under `.agent/` (JSON, no DB) | `persistence.py` |
| `claude` subprocess worker (plan / implement / repair) | `workers/claude.py` |
| `codex` subprocess worker (review / escalate) | `workers/codex.py` |
| Shared subprocess plumbing (env scrub, timeout, classify) | `workers/base.py` |
| CLI entry point | `cli.py` / `__main__.py` |

- The **writer** (`claude`) is an untrusted transformation step. The
  **verifier** (deterministic exit codes) is the source of truth. A non-zero
  check exit always overrides any worker's claim of success — models never
  decide whether tests passed.
- The **reviewer** (`codex`, read-only) produces a schema-validated verdict.
  A `fail` verdict routes to repair. A reviewer *infrastructure* failure
  (timeout, crash, malformed JSON) is **not** an application failure and never
  routes to repair — it retries once, then stops.
- Git belongs to the orchestrator. The workers never run git. The harness never
  pushes, resets, rebases, rewrites history, deletes branches, or uses
  `git add -A`.

## The graph

```mermaid
flowchart TD
    START([start]) --> bootstrap
    bootstrap -->|dirty tree / not a repo| STOP([END])
    bootstrap --> plan
    plan -->|iteration limit / planning failed| STOP
    plan --> implement
    implement -->|protected path / usage limit / CLI fatal| STOP
    implement --> verify
    verify --> decide

    decide -->|checks fail, repairs left| repair
    decide -->|checks fail, stagnant| escalation_review
    decide -->|checks fail, repairs exhausted| STOP
    decide -->|checks fail, iteration limit| STOP
    decide -->|checks pass, review pending| review
    decide -->|reviewer infra error, retries left| review
    decide -->|reviewer infra error, exhausted| STOP
    decide -->|review FAIL verdict, repairs left| repair
    decide -->|review PASS verdict| checkpoint

    review --> decide
    repair --> verify
    escalation_review --> repair
    checkpoint -->|more tasks| plan
    checkpoint -->|objective complete| STOP
```

Loops live entirely in edges — nodes never recurse. `decide` is the only
router; because LangGraph does not persist mutations made inside conditional
edge functions, the routing decision (and any stop reason) is computed in the
`decide` **node** and merely read by the edge function.

### Stop reasons

`SUCCESS`, `ITERATION_LIMIT`, `REPAIR_LIMIT`, `STAGNATION_UNRESOLVED`,
`PLANNING_FAILED`, `REVIEWER_ERROR`, `PROTECTED_PATH_MODIFIED`,
`DIRTY_WORKTREE`, `UNEXPECTED_WORKTREE_STATE`, `CLI_FATAL`, `USAGE_LIMIT`,
`USER_ABORT`.

## Setup

```bash
python3 -m venv agent_harness/.venv
agent_harness/.venv/bin/pip install -r agent_harness/requirements.txt
```

The Tailor application has **no** dependencies; keep running its own suite with a
bare interpreter (`python3 -m unittest discover -s tests`). The harness deps
(langgraph, pydantic, rich) live only in `agent_harness/.venv` (gitignored).

## Authentication assumptions

- Uses the **existing** local CLI auth: Claude Pro (`claude`) and ChatGPT Plus
  (`codex`). No `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` — the harness actively
  **removes** those from the child process environment before launching a
  worker.
- The CLIs are not on `PATH` in this environment; they ship inside the VS Code
  extensions. Resolution order: `[claude]/[codex] executable` in `agent.toml`
  → `which` → `~/.vscode/extensions/<ext>-*/…` glob. Set the `executable` key
  if an extension update moves the binary.
- The installed `claude` (v2.1.263) has **no `--max-turns`** flag; the
  `*_max_turns` config keys are wired and forward-compatible but currently
  inert — `timeout_seconds` is the only outer bound.

## Configuration (`agent.toml`)

See `agent.toml` at the repo root. Highlights: `[agent]` limits;
`[claude] model` (default `claude-sonnet-5`) + per-role tool allowlists;
`[codex] review_model` / `checkpoint_model` (`gpt-5.6-luna` / `gpt-5.6-terra`)
+ `review_retries`; `[git]` `branch_required` / `require_clean_worktree`;
`[safety] protected_paths`; `[checks] commands` (the deterministic gate).

## Run

```bash
agent_harness/.venv/bin/python -m agent_harness "Fix all currently failing tests"
agent_harness/.venv/bin/python -m agent_harness --dry-run "objective"     # wiring check, no workers
agent_harness/.venv/bin/python -m agent_harness --max-iterations 5 "objective"
```

A fresh run requires: inside a git repo, on a non-`main`/`master` branch, with a
clean worktree. The terminal streams transitions
(`PLAN / IMPLEMENT / VERIFY / REPAIR / REVIEW / ESCALATION / CHECKPOINT`) with
the iteration number, and prints a final summary (status, stop reason,
iterations, completed tasks, commits, log dir).

## Resume

```bash
agent_harness/.venv/bin/python -m agent_harness --resume
```

Loads `.agent/state.json` and reconciles the worktree:

- If a **write-capable** Claude call was in flight at crash time, it is **never
  replayed** — the run re-enters at deterministic `verify` on whatever is on
  disk.
- Changes to a **protected path** → stop (`PROTECTED_PATH_MODIFIED`).
- Changes that cannot be attributed to the harness → stop
  (`UNEXPECTED_WORKTREE_STATE`). Nothing is discarded.
- A read-only worker in flight (review/escalation) is simply re-run.

V0 does not auto-wait after a usage limit — it saves state and exits so
`--resume` can pick up later.

## Safety model

These are **application-level guardrails, not an OS-level sandbox.** There is no
container, chroot, or seccomp:

- Claude runs with `--permission-mode plan` (planner, read-only) or
  `acceptEdits` (implement/repair), `--permission-prompts none`, `--add-dir`
  scoped to the repo, and a `--tools` allowlist that **excludes** `Bash`,
  `WebFetch`, `WebSearch`, and `Task`. No `--dangerously-skip-permissions`.
- Codex review/escalation runs `--sandbox read-only`.
- After every write-capable Claude call the harness diffs the worktree; any
  change to `agent_harness/`, `agent.toml`, `.git/`, or `.agent/` stops the run
  and is neither committed nor "repaired". `.git/` and `.agent/` coverage is
  best-effort (they don't appear in `git diff`; enforcement is via the tool
  allowlist and the explicit-pathspec checkpoint).
- The harness never pushes, deploys, touches remotes, rewrites history, deletes
  branches, or modifies files outside the repo root.

A determined or malfunctioning worker is not *physically* prevented from acting
outside these bounds. Run only in a repository you trust, on a feature branch,
with a clean tree.

## V0 limitations

- No `--max-turns` support in the installed `claude`; turn caps are inert.
- No auto-wait / auto-resume after subscription limits (save + exit only).
- No lint / typecheck checks (Tailor has none); `compileall` is the only static
  gate beyond the unit suite.
- Extension-bundled CLI paths are version-pinned; may need an `executable`
  override after an update.
- Single objective; linear task list from the planner (no task DAG, no
  parallelism); one worker at a time.
- Stagnation detection targets repeated **deterministic-check** failures; a pure
  review-verdict loop stops via `REPAIR_LIMIT` rather than escalation.
- Planner/reviewer quality is prompt-bounded; the loop trusts exit codes and
  schema-valid verdicts, not prose.
- LangGraph pulls `langchain-core` transitively — present but unused for model
  calls (all model access is CLI subprocess).
- End-to-end proof so far is `--dry-run` + one real read-only Codex review. A
  guided full run is the next stage.
