"""Shared subprocess plumbing for the CLI workers.

* strips ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` from the child env so the
  workers can only use the user's existing subscription CLI auth,
* enforces a hard timeout,
* never lets an exception escape - failures come back as a typed result,
* classifies obvious rate / usage-limit conditions,
* preserves raw stdout/stderr to a log file.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# Never forward these to the child - force subscription CLI auth.
STRIPPED_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")

Classification = str  # "ok" | "cli_error" | "usage_limit"


@dataclass
class WorkerResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    classification: Classification = "ok"
    argv: list[str] = field(default_factory=list)

    @property
    def is_usage_limit(self) -> bool:
        return self.classification == "usage_limit"


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in STRIPPED_ENV_VARS:
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def classify_failure(
    stdout: str,
    stderr: str,
    exit_code: int | None,
    *,
    usage_limit_patterns: Sequence[str],
) -> Classification:
    if exit_code == 0:
        return "ok"
    haystack = f"{stdout}\n{stderr}".lower()
    for pat in usage_limit_patterns:
        if re.search(re.escape(pat.lower()), haystack):
            return "usage_limit"
    return "cli_error"


def _tail(text: str, limit: int = 20_000) -> str:
    if len(text) <= limit:
        return text
    return "...<truncated>...\n" + text[-limit:]


def run_subprocess(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    timeout_s: int,
    log_path: str | os.PathLike[str] | None = None,
    usage_limit_patterns: Sequence[str] = (),
    stdin_text: str | None = None,
) -> WorkerResult:
    """Run ``argv`` to completion (or timeout) and return a :class:`WorkerResult`."""
    argv = [str(a) for a in argv]
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=scrubbed_env(),
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        stdout, stderr, exit_code = proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stderr = (stderr + f"\n[harness] timed out after {timeout_s}s").strip()
        exit_code = None
    except OSError as exc:
        stdout, stderr, exit_code = "", f"[harness] failed to launch {argv[0]!r}: {exc}", None

    duration = time.monotonic() - start

    if timed_out or exit_code is None:
        classification = "cli_error"
    else:
        classification = classify_failure(
            stdout, stderr, exit_code, usage_limit_patterns=usage_limit_patterns
        )

    if log_path is not None:
        # Run logs can contain prompts, diffs, model output and source
        # fragments (never credentials - the child env is scrubbed). Create them
        # user-only.
        try:
            p = Path(log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(
                    f"$ {' '.join(argv)}\n"
                    f"# cwd={cwd}\n# exit={exit_code} timed_out={timed_out} "
                    f"duration={duration:.1f}s classification={classification}\n\n"
                    f"----- stdout -----\n{_tail(stdout)}\n\n"
                    f"----- stderr -----\n{_tail(stderr)}\n"
                )
        except OSError:
            pass

    return WorkerResult(
        ok=(exit_code == 0 and not timed_out),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_s=duration,
        timed_out=timed_out,
        classification=classification,
        argv=argv,
    )
