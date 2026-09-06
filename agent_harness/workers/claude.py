"""``claude`` CLI worker - the only worker allowed to modify application source.

Three roles, each with its own permission mode and tool allowlist:

* ``plan``      - read-only (``--permission-mode plan``), structured JSON output
* ``implement`` - ``--permission-mode acceptEdits``, file read/search/edit tools only
* ``repair``    - same capability as ``implement``

No ``--dangerously-skip-permissions``. No ``Bash`` / ``WebFetch`` / ``WebSearch``
/ ``Task`` for any role. API-key env vars are stripped by the base runner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ValidationError

from agent_harness.config import Config, detect_max_turns_support, resolve_claude_executable
from agent_harness.promptlib import render
from agent_harness.workers.base import WorkerResult, run_subprocess

_PLAN_SCHEMA = Path(__file__).parent.parent / "schemas" / "plan.schema.json"

_SYSTEM_APPEND = (
    "You are the implementation worker inside an autonomous harness. Stay strictly "
    "within this repository. Never modify agent_harness/, agent.toml, .git/, or "
    ".agent/. Do not run git. Do not install packages or access the network. Make "
    "the smallest change that satisfies the current task, then stop and reply."
)


class PlanningError(RuntimeError):
    """Planner output was missing or did not match plan.schema.json."""


class PlanTask(BaseModel):
    id: str
    title: str
    rationale: str = ""


class PlanResult(BaseModel):
    tasks: list[PlanTask]


@dataclass
class ClaudeInvocation:
    role: str
    result: WorkerResult
    result_text: str = ""
    is_error: bool = False
    subtype: str = ""
    num_turns: int | None = None
    total_cost_usd: float | None = None
    session_id: str = ""

    @property
    def ok(self) -> bool:
        return self.result.ok and not self.is_error

    @property
    def classification(self) -> str:
        return self.result.classification


def _parse_cli_json(stdout: str) -> dict:
    """`claude -p --output-format json` prints a single JSON object."""
    stdout = stdout.strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # Be lenient: take the last JSON object on the last non-empty line.
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    return {}


class ClaudeWorker:
    def __init__(
        self,
        config: Config,
        *,
        executable: str | None = None,
        runner: Callable[..., WorkerResult] = run_subprocess,
        max_turns_supported: bool | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self.config = config
        self._executable = executable or resolve_claude_executable(config)
        self._runner = runner
        self._max_turns_supported = max_turns_supported
        self.log_dir = log_dir

    # -- capability probing --------------------------------------------------

    @property
    def executable(self) -> str:
        return self._executable

    def max_turns_supported(self) -> bool:
        if self._max_turns_supported is None:
            self._max_turns_supported = detect_max_turns_support(self._executable)
        return self._max_turns_supported

    # -- roles ------------------------------------------------------------------

    def plan(self, *, objective: str, repo_context: str = "", iteration: int = 0) -> PlanResult:
        prompt = render(
            "planner", objective=objective, repo_context=repo_context
        )
        argv = self._base_argv(prompt, role="plan") + [
            "--permission-mode", "plan",
            "--tools", *self.config.claude.planner_tools,
            "--json-schema", str(_PLAN_SCHEMA),
        ]
        argv += self._maybe_max_turns(self.config.claude.planner_max_turns)
        inv = self._run(argv, role="plan", iteration=iteration)
        if not inv.ok:
            raise PlanningError(
                f"planner CLI failed (exit={inv.result.exit_code}, "
                f"class={inv.classification}): {inv.result.stderr[:400]}"
            )
        return _validate_plan(inv.result_text)

    def implement(
        self, *, objective: str, task: str, repo_context: str = "", iteration: int = 0
    ) -> ClaudeInvocation:
        prompt = render(
            "implementer", objective=objective, task=task, repo_context=repo_context
        )
        return self._edit_role(prompt, role="implement", iteration=iteration,
                               max_turns=self.config.claude.implement_max_turns)

    def repair(
        self,
        *,
        objective: str,
        task: str,
        failures: str,
        root_cause: str = "",
        iteration: int = 0,
    ) -> ClaudeInvocation:
        prompt = render(
            "repairer",
            objective=objective,
            task=task,
            failures=failures,
            root_cause=root_cause,
        )
        return self._edit_role(prompt, role="repair", iteration=iteration,
                               max_turns=self.config.claude.repair_max_turns)

    # -- internals ------------------------------------------------------------

    def _edit_role(self, prompt: str, *, role: str, iteration: int, max_turns: int) -> ClaudeInvocation:
        argv = self._base_argv(prompt, role=role) + [
            "--permission-mode", "acceptEdits",
            "--tools", *self.config.claude.edit_tools,
        ]
        argv += self._maybe_max_turns(max_turns)
        return self._run(argv, role=role, iteration=iteration)

    def _base_argv(self, prompt: str, *, role: str) -> list[str]:
        return [
            self._executable,
            "-p", prompt,
            "--model", self.config.claude.model,
            "--output-format", "json",
            "--add-dir", str(self.config.repo_root),
            "--permission-prompts", "none",
            "--append-system-prompt", _SYSTEM_APPEND,
        ]

    def _maybe_max_turns(self, n: int) -> list[str]:
        return ["--max-turns", str(n)] if self.max_turns_supported() else []

    def _run(self, argv: list[str], *, role: str, iteration: int) -> ClaudeInvocation:
        log_path = None
        if self.log_dir is not None:
            log_path = Path(self.log_dir) / f"{iteration:03d}-{role}-claude.log"
        res = self._runner(
            argv,
            cwd=str(self.config.repo_root),
            timeout_s=self.config.claude.timeout_seconds,
            log_path=log_path,
            usage_limit_patterns=self.config.classify.usage_limit_patterns,
        )
        data = _parse_cli_json(res.stdout)
        return ClaudeInvocation(
            role=role,
            result=res,
            result_text=str(data.get("result", "")) if data else res.stdout,
            is_error=bool(data.get("is_error", False)),
            subtype=str(data.get("subtype", "")),
            num_turns=data.get("num_turns"),
            total_cost_usd=data.get("total_cost_usd"),
            session_id=str(data.get("session_id", "")),
        )


def _validate_plan(result_text: str) -> PlanResult:
    text = (result_text or "").strip()
    if not text:
        raise PlanningError("planner returned empty output")
    # The model may wrap JSON in prose or a fenced block; extract the object.
    candidate = text
    if "```" in candidate:
        candidate = candidate.split("```")[1]
        candidate = candidate[4:] if candidate.lower().startswith("json") else candidate
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1:
        raise PlanningError(f"planner output is not JSON: {text[:200]}")
    try:
        data = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise PlanningError(f"planner output is not valid JSON: {exc}") from exc
    try:
        plan = PlanResult.model_validate(data)
    except ValidationError as exc:
        raise PlanningError(f"planner output does not match plan schema: {exc}") from exc
    if not plan.tasks:
        raise PlanningError("planner returned zero tasks")
    return plan
