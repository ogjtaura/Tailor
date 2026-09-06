"""Load and validate ``agent.toml`` into a typed :class:`Config`.

Also owns resolution of the ``claude`` / ``codex`` executables (which are not on
PATH in this environment - they ship inside the VS Code extensions) and the
one-time probe for whether the installed ``claude`` supports ``--max-turns``.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

REPO_ROOT_ENV = "AGENT_HARNESS_REPO_ROOT"

# Fallback globs for the CLIs when they are not on PATH (VS Code extension bundles).
_CLAUDE_GLOBS = [
    "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude",
    "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/*/claude",
]
_CODEX_GLOBS = [
    "~/.vscode/extensions/openai.chatgpt-*/bin/*/codex",
    "~/.vscode/extensions/openai.chatgpt-*/bin/codex",
]


class ConfigError(RuntimeError):
    """Raised when ``agent.toml`` is missing, malformed, or a CLI cannot be found."""


class AgentSection(BaseModel):
    max_iterations: int = 15
    max_repairs_per_task: int = 5
    max_stagnant_iterations: int = 3


class ClaudeSection(BaseModel):
    model: str = "claude-sonnet-5"
    executable: str = ""
    timeout_seconds: int = 1800
    planner_max_turns: int = 6
    implement_max_turns: int = 20
    repair_max_turns: int = 12
    planner_tools: list[str] = Field(default_factory=lambda: ["Read", "Grep", "Glob"])
    edit_tools: list[str] = Field(
        default_factory=lambda: ["Read", "Grep", "Glob", "Edit", "MultiEdit", "Write"]
    )


class CodexSection(BaseModel):
    review_model: str = "gpt-5.6-luna"
    checkpoint_model: str = "gpt-5.6-terra"
    executable: str = ""
    timeout_seconds: int = 1800
    review_retries: int = 1


class GitSection(BaseModel):
    auto_commit: bool = True
    branch_required: bool = True
    require_clean_worktree: bool = True


class SafetySection(BaseModel):
    protected_paths: list[str] = Field(
        default_factory=lambda: ["agent_harness/", "agent.toml", ".git/", ".agent/"]
    )


class ChecksSection(BaseModel):
    commands: list[str] = Field(default_factory=list)


class ClassifySection(BaseModel):
    usage_limit_patterns: list[str] = Field(
        default_factory=lambda: [
            "usage limit",
            "rate limit",
            "quota",
            "429",
            "too many requests",
            "reached your usage limit",
        ]
    )


class Config(BaseModel):
    agent: AgentSection = Field(default_factory=AgentSection)
    claude: ClaudeSection = Field(default_factory=ClaudeSection)
    codex: CodexSection = Field(default_factory=CodexSection)
    git: GitSection = Field(default_factory=GitSection)
    safety: SafetySection = Field(default_factory=SafetySection)
    checks: ChecksSection = Field(default_factory=ChecksSection)
    classify: ClassifySection = Field(default_factory=ClassifySection)

    # Populated at load time (not from TOML).
    repo_root: Path
    config_path: Path

    def with_overrides(self, *, max_iterations: int | None = None) -> "Config":
        data = self.model_dump()
        if max_iterations is not None:
            data["agent"]["max_iterations"] = max_iterations
        return Config(**data)


def _resolve_executable(configured: str, name: str, globs: list[str]) -> str:
    """config value -> PATH -> extension glob -> error."""
    if configured:
        p = Path(configured).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        raise ConfigError(
            f"[{name}] executable {configured!r} is not an executable file"
        )

    found = shutil.which(name)
    if found:
        return found

    candidates: list[str] = []
    for pattern in globs:
        candidates.extend(glob.glob(os.path.expanduser(pattern)))
    candidates = sorted(
        (c for c in candidates if os.path.isfile(c) and os.access(c, os.X_OK)),
        reverse=True,  # highest version dir first
    )
    if candidates:
        return candidates[0]

    raise ConfigError(
        f"Could not find the {name!r} CLI. Put it on PATH or set "
        f"[{name}] executable in agent.toml."
    )


def resolve_claude_executable(cfg: Config) -> str:
    return _resolve_executable(cfg.claude.executable, "claude", _CLAUDE_GLOBS)


def resolve_codex_executable(cfg: Config) -> str:
    return _resolve_executable(cfg.codex.executable, "codex", _CODEX_GLOBS)


def detect_max_turns_support(claude_executable: str) -> bool:
    """True iff ``claude --help`` advertises a ``--max-turns`` flag.

    The bundled claude v2.1.263 does not; the config keys are forward-compatible.
    """
    try:
        out = subprocess.run(
            [claude_executable, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "--max-turns" in (out.stdout or "") + (out.stderr or "")


def load_config(config_path: str | os.PathLike[str], repo_root: str | os.PathLike[str]) -> Config:
    cfg_path = Path(config_path).expanduser().resolve()
    root = Path(repo_root).expanduser().resolve()
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path}")
    try:
        raw = tomllib.loads(cfg_path.read_text())
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - message passthrough
        raise ConfigError(f"invalid TOML in {cfg_path}: {exc}") from exc
    try:
        return Config(repo_root=root, config_path=cfg_path, **raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration in {cfg_path}:\n{exc}") from exc
