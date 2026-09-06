"""``codex`` CLI worker - independent, read-only reviewer.

Runs ``codex exec --sandbox read-only``; it never writes production code.

Key contract (plan amendment 9): a *schema-valid* verdict of ``fail`` means the
application code may need repair. Any *infrastructure* failure - timeout,
non-zero exit, missing output file, malformed JSON, schema-invalid body - is
NOT an application failure and is returned as ``kind="infra_error"``. The graph
never routes an infra error to the Claude repair worker.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional

from pydantic import BaseModel, ValidationError

from agent_harness.config import Config, resolve_codex_executable
from agent_harness.promptlib import render
from agent_harness.workers.base import WorkerResult, run_subprocess

_VERDICT_SCHEMA = Path(__file__).parent.parent / "schemas" / "review_verdict.schema.json"


class ReviewFinding(BaseModel):
    id: str
    description: str = ""
    evidence: str = ""
    suggested_fix: str = ""


class ReviewVerdict(BaseModel):
    verdict: Literal["pass", "fail"]
    severity: Literal["none", "minor", "major", "critical"] = "none"
    findings: list[ReviewFinding] = []


@dataclass
class ReviewOutcome:
    kind: Literal["verdict", "infra_error"]
    verdict: Optional[ReviewVerdict] = None
    reason: str = ""
    classification: str = "ok"
    result: Optional[WorkerResult] = None

    @property
    def is_infra_error(self) -> bool:
        return self.kind == "infra_error"


class CodexWorker:
    def __init__(
        self,
        config: Config,
        *,
        executable: str | None = None,
        runner: Callable[..., WorkerResult] = run_subprocess,
        log_dir: Path | None = None,
    ) -> None:
        self.config = config
        self._executable = executable or resolve_codex_executable(config)
        self._runner = runner
        self.log_dir = log_dir

    @property
    def executable(self) -> str:
        return self._executable

    # -- routine review -----------------------------------------------------

    def review(self, *, diff: str, check_results: str, iteration: int = 0) -> ReviewOutcome:
        prompt = render("reviewer", diff=diff, check_results=check_results)
        with tempfile.TemporaryDirectory(prefix="agent-harness-review-") as tmp:
            out_file = Path(tmp) / "verdict.json"
            argv = [
                self._executable, "exec",
                "--model", self.config.codex.review_model,
                "--sandbox", "read-only",
                "--cd", str(self.config.repo_root),
                "--output-schema", str(_VERDICT_SCHEMA),
                "--output-last-message", str(out_file),
                "--color", "never",
                prompt,
            ]
            res = self._runner(
                argv,
                cwd=str(self.config.repo_root),
                timeout_s=self.config.codex.timeout_seconds,
                log_path=self._log_path(iteration, "review"),
                usage_limit_patterns=self.config.classify.usage_limit_patterns,
            )
            body = out_file.read_text() if out_file.is_file() else ""

        if res.timed_out or not res.ok:
            return ReviewOutcome(
                kind="infra_error",
                reason=_infra_reason(res),
                classification=res.classification,
                result=res,
            )

        raw = body.strip() or res.stdout.strip()
        try:
            data = json.loads(_extract_json(raw))
        except (json.JSONDecodeError, ValueError):
            return ReviewOutcome(
                kind="infra_error",
                reason="reviewer output was not valid JSON",
                classification="cli_error",
                result=res,
            )
        try:
            verdict = ReviewVerdict.model_validate(data)
        except ValidationError as exc:
            return ReviewOutcome(
                kind="infra_error",
                reason=f"reviewer output did not match verdict schema: {exc}",
                classification="cli_error",
                result=res,
            )
        return ReviewOutcome(kind="verdict", verdict=verdict, result=res)

    # -- escalation review (root-cause analysis) --------------------------

    def escalate(self, *, diff: str, failures: str, iteration: int = 0) -> Optional[str]:
        prompt = render("escalation_reviewer", diff=diff, failures=failures)
        argv = [
            self._executable, "exec",
            "--model", self.config.codex.checkpoint_model,
            "--sandbox", "read-only",
            "--cd", str(self.config.repo_root),
            "--color", "never",
            prompt,
        ]
        res = self._runner(
            argv,
            cwd=str(self.config.repo_root),
            timeout_s=self.config.codex.timeout_seconds,
            log_path=self._log_path(iteration, "escalation"),
            usage_limit_patterns=self.config.classify.usage_limit_patterns,
        )
        if not res.ok:
            return None  # non-fatal: repair proceeds without a root-cause hint
        return res.stdout.strip() or None

    # -- internals --------------------------------------------------------

    def _log_path(self, iteration: int, role: str) -> Optional[Path]:
        if self.log_dir is None:
            return None
        return Path(self.log_dir) / f"{iteration:03d}-{role}-codex.log"


def _infra_reason(res: WorkerResult) -> str:
    if res.timed_out:
        return "reviewer timed out"
    return f"reviewer CLI exited {res.exit_code}"


def _extract_json(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("empty")
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object")
    return text[start : end + 1]
