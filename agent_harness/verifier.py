"""Deterministic project checks.

Runs the commands from ``[checks] commands`` in ``agent.toml``. Exit code 0 is
the only definition of "passed". A non-zero exit always overrides any worker's
claim that its implementation succeeded - models never decide this.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Sequence

from agent_harness.state import CheckResult

_TAIL = 4_000


def _tail(text: str) -> str:
    text = text or ""
    return text if len(text) <= _TAIL else "...<truncated>...\n" + text[-_TAIL:]


def run_check(command: str, *, cwd: str | Path, timeout_s: int = 900) -> CheckResult:
    argv = shlex.split(command)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        exit_code, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        exit_code, out, err = 124, "", f"[harness] check timed out after {timeout_s}s"
    except OSError as exc:
        exit_code, out, err = 127, "", f"[harness] failed to run {argv[0]!r}: {exc}"
    return CheckResult(
        command=command,
        exit_code=exit_code,
        stdout_tail=_tail(out),
        stderr_tail=_tail(err),
        duration_s=round(time.monotonic() - start, 3),
    )


def run_checks(
    commands: Sequence[str], *, cwd: str | Path, timeout_s: int = 900
) -> list[CheckResult]:
    return [run_check(cmd, cwd=cwd, timeout_s=timeout_s) for cmd in commands]


def checks_passed(results: Sequence[CheckResult]) -> bool:
    return all(r.exit_code == 0 for r in results)


def summarise(results: Sequence[CheckResult]) -> str:
    if not results:
        return "no checks configured"
    failed = [r for r in results if r.exit_code != 0]
    if not failed:
        return f"{len(results)} check(s) passed"
    names = ", ".join(shlex.split(r.command)[0:3][-1] for r in failed)
    return f"{len(failed)}/{len(results)} check(s) FAILED ({names})"
