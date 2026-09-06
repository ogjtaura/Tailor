"""Command-line entry point.

    python -m agent_harness "Fix all currently failing tests"
    python -m agent_harness --resume
    python -m agent_harness --dry-run "objective"
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent_harness.config import (
    Config,
    ConfigError,
    detect_max_turns_support,
    load_config,
    resolve_claude_executable,
    resolve_codex_executable,
)
from agent_harness.git_tools import GitError, GitTools
from agent_harness.graph import Engine, Printer
from agent_harness.persistence import Persistence, reconcile_for_resume
from agent_harness.state import HarnessState, Status, StopReason


def _repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return Path.cwd()


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="agent_harness", description=__doc__)
    p.add_argument("objective", nargs="?", help="high-level engineering objective")
    p.add_argument("--resume", action="store_true", help="continue from .agent/state.json")
    p.add_argument("--config", default=None, help="path to agent.toml (default: <repo>/agent.toml)")
    p.add_argument("--max-iterations", type=int, default=None, help="override [agent] max_iterations")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve config, check CLIs, compile the graph; run no workers and make no commits",
    )
    return p.parse_args(argv)


def _banner(cfg: Config, printer: Printer, *, dry_run: bool) -> None:
    try:
        claude_exe = resolve_claude_executable(cfg)
    except ConfigError as exc:
        claude_exe = f"<unresolved: {exc}>"
    try:
        codex_exe = resolve_codex_executable(cfg)
    except ConfigError as exc:
        codex_exe = f"<unresolved: {exc}>"
    mt = detect_max_turns_support(claude_exe) if Path(str(claude_exe)).exists() else False
    printer.info("=" * 72)
    printer.info(f"agent_harness{'  (dry-run)' if dry_run else ''}")
    printer.info(f"  repo            {cfg.repo_root}")
    printer.info(f"  config          {cfg.config_path}")
    printer.info(f"  claude          {claude_exe}")
    printer.info(f"  codex           {codex_exe}")
    printer.info(f"  claude model    {cfg.claude.model}")
    printer.info(f"  codex models    review={cfg.codex.review_model} checkpoint={cfg.codex.checkpoint_model}")
    printer.info(f"  --max-turns     {'supported' if mt else 'NOT supported by installed claude (timeout is the bound)'}")
    printer.info(f"  limits          iterations={cfg.agent.max_iterations} repairs/task={cfg.agent.max_repairs_per_task} stagnation={cfg.agent.max_stagnant_iterations}")
    printer.info(f"  planner tools   {cfg.claude.planner_tools}")
    printer.info(f"  edit tools      {cfg.claude.edit_tools}")
    printer.info(f"  protected       {cfg.safety.protected_paths}")
    printer.info(f"  checks          {cfg.checks.commands}")
    printer.info("=" * 72)


def _summary(state: HarnessState, printer: Printer, store: Persistence) -> None:
    printer.info("-" * 72)
    printer.info(f"status        {state.status.value}")
    printer.info(f"stop reason   {state.stop_reason.value if state.stop_reason else '(none)'}")
    printer.info(f"iterations    {state.iteration}")
    printer.info(f"completed     {state.completed_tasks}")
    printer.info(f"remaining     {state.remaining_tasks}")
    printer.info(f"commits       {[c[:9] for c in state.commits]}")
    printer.info(f"logs          {store.runs_dir / state.run_id}")
    printer.info("-" * 72)


def _dry_run(cfg: Config, printer: Printer) -> int:
    _banner(cfg, printer, dry_run=True)
    # resolve CLIs (existence only)
    problems = []
    for name, resolver in (("claude", resolve_claude_executable), ("codex", resolve_codex_executable)):
        try:
            exe = resolver(cfg)
            printer.info(f"  {name} resolved -> {exe}")
        except ConfigError as exc:
            problems.append(str(exc))
    # compile the graph without collaborators that need to run
    try:
        engine = Engine(
            cfg,
            claude=_Unusable("claude"),
            codex=_Unusable("codex"),
            git=GitTools(cfg.repo_root),
            store=Persistence(cfg.repo_root),
            printer=Printer(enabled=False),
        )
        engine.build()
        printer.info("  graph compiled OK")
        printer.info("  nodes: bootstrap -> plan -> implement -> verify -> decide")
        printer.info("         decide -> {repair, escalation_review, review, checkpoint, END}")
        printer.info("         repair -> verify ; escalation_review -> repair ; checkpoint -> plan/END")
    except Exception as exc:  # pragma: no cover - defensive
        problems.append(f"graph build failed: {exc}")
    if problems:
        for pr in problems:
            printer.info(f"  PROBLEM: {pr}")
        return 1
    printer.info("dry-run OK: no workers invoked, no commits made.")
    return 0


class _Unusable:
    """Placeholder collaborator for --dry-run graph compilation."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.log_dir = None

    def __getattr__(self, item):  # pragma: no cover - should never be called in dry-run
        raise RuntimeError(f"{self._name}.{item} called during dry-run")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    printer = Printer()
    root = _repo_root()
    config_path = Path(args.config) if args.config else root / "agent.toml"

    try:
        cfg = load_config(config_path, root)
    except ConfigError as exc:
        printer.info(f"config error: {exc}")
        return 2
    if args.max_iterations is not None:
        cfg = cfg.with_overrides(max_iterations=args.max_iterations)

    if args.dry_run:
        return _dry_run(cfg, printer)

    store = Persistence(cfg.repo_root)
    git = GitTools(cfg.repo_root)

    if args.resume:
        state = store.load()
        if state is None:
            printer.info("nothing to resume: no .agent/state.json found")
            return 2
        state.stop_reason = None
        try:
            state = reconcile_for_resume(state, git, cfg.safety.protected_paths)
        except GitError as exc:
            printer.info(f"resume aborted: {exc}")
            return 2
        if state.stop_reason is not None:
            state.status = Status.STOPPED
            store.save(state, note="resume reconciliation refused")
            _summary(state, printer, store)
            return 1
        _banner(cfg, printer, dry_run=False)
        engine = Engine(cfg, git=git, store=store, printer=printer, resume_target=state.next_node or "decide")
        final = engine.run(state)
        _summary(final, printer, store)
        return 0 if final.stop_reason == StopReason.SUCCESS else 1

    if not args.objective:
        printer.info("error: provide an objective, or use --resume")
        return 2

    _banner(cfg, printer, dry_run=False)
    state = HarnessState(objective=args.objective, run_id=_new_run_id())
    engine = Engine(cfg, git=git, store=store, printer=printer)
    final = engine.run(state)
    _summary(final, printer, store)
    return 0 if final.stop_reason == StopReason.SUCCESS else 1
