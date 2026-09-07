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
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# --- engineering-harness agent routing -------------------------------------
# A *role* (what the harness needs done) is resolved to a *backend* (which CLI
# adapter runs it) and a *model*. Only the two backends the V0 pipeline already
# uses are supported.
BackendName = Literal["claude_code", "codex"]
RoleName = Literal[
    "planner", "implementer", "repairer", "routine_reviewer", "escalation_reviewer"
]
ROLE_NAMES: tuple[RoleName, ...] = (
    "planner", "implementer", "repairer", "routine_reviewer", "escalation_reviewer"
)

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
    model_config = ConfigDict(extra="forbid")
    max_iterations: int = 15
    max_repairs_per_task: int = 5
    max_stagnant_iterations: int = 3


class ClaudeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
    model_config = ConfigDict(extra="forbid")
    review_model: str = "gpt-5.6-luna"
    checkpoint_model: str = "gpt-5.6-terra"
    executable: str = ""
    timeout_seconds: int = 1800
    review_retries: int = 1


class GitSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_commit: bool = True
    branch_required: bool = True
    require_clean_worktree: bool = True


class SafetySection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protected_paths: list[str] = Field(
        default_factory=lambda: ["agent_harness/", "agent.toml", ".git/", ".agent/"]
    )


class ChecksSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[str] = Field(default_factory=list)


class RoleAssignment(BaseModel):
    """One engineering role -> (backend, model). ``model`` may be omitted, in
    which case the backend's own default model is used."""
    model_config = ConfigDict(extra="forbid")
    backend: BackendName
    model: str = ""


class RolesSection(BaseModel):
    """Optional explicit role routing. Any role left unset falls back to the
    V0 default (see :meth:`Config.resolve_role`)."""
    model_config = ConfigDict(extra="forbid")
    planner: Optional[RoleAssignment] = None
    implementer: Optional[RoleAssignment] = None
    repairer: Optional[RoleAssignment] = None
    routine_reviewer: Optional[RoleAssignment] = None
    escalation_reviewer: Optional[RoleAssignment] = None


class ClassifySection(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
    model_config = ConfigDict(extra="forbid")
    agent: AgentSection = Field(default_factory=AgentSection)
    claude: ClaudeSection = Field(default_factory=ClaudeSection)
    codex: CodexSection = Field(default_factory=CodexSection)
    git: GitSection = Field(default_factory=GitSection)
    safety: SafetySection = Field(default_factory=SafetySection)
    checks: ChecksSection = Field(default_factory=ChecksSection)
    classify: ClassifySection = Field(default_factory=ClassifySection)
    roles: RolesSection = Field(default_factory=RolesSection)

    # Populated at load time (not from TOML).
    repo_root: Path
    config_path: Path

    def with_overrides(self, *, max_iterations: int | None = None) -> "Config":
        data = self.model_dump()
        if max_iterations is not None:
            data["agent"]["max_iterations"] = max_iterations
        return Config(**data)

    # -- engineering-role routing -----------------------------------------

    def resolve_role(self, role: RoleName) -> RoleAssignment:
        """Resolve a role to ``(backend, model)``. An explicit ``[roles.<role>]``
        wins; otherwise the V0 default applies:

            planner / implementer / repairer -> claude_code / [claude].model
            routine_reviewer                 -> codex / [codex].review_model
            escalation_reviewer              -> codex / [codex].checkpoint_model
        """
        explicit = getattr(self.roles, role, None)
        if explicit is not None:
            model = explicit.model or self._default_model(explicit.backend, role)
            return RoleAssignment(backend=explicit.backend, model=model)
        if role in ("planner", "implementer", "repairer"):
            return RoleAssignment(backend="claude_code", model=self.claude.model)
        if role == "routine_reviewer":
            return RoleAssignment(backend="codex", model=self.codex.review_model)
        if role == "escalation_reviewer":
            return RoleAssignment(backend="codex", model=self.codex.checkpoint_model)
        raise ConfigError(f"unknown engineering role {role!r}")

    def _default_model(self, backend: BackendName, role: RoleName) -> str:
        if backend == "claude_code":
            return self.claude.model
        if role == "escalation_reviewer":
            return self.codex.checkpoint_model
        return self.codex.review_model

    def resolved_roles(self) -> dict[str, RoleAssignment]:
        return {r: self.resolve_role(r) for r in ROLE_NAMES}

    def require_runnable(self) -> None:
        """Raise :class:`ConfigError` if the config cannot support an autonomous
        run. A run with zero deterministic checks has no verification gate and is
        rejected."""
        real = [c for c in self.checks.commands if c and c.strip()]
        if not real:
            raise ConfigError(
                "[checks] commands is empty: an autonomous run needs at least one "
                "deterministic check (its exit code is the source of truth)."
            )


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
