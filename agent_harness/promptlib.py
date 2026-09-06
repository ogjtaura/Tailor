"""Load and render the plain-text prompt templates in ``agent_harness/prompts/``.

Templates use ``$name`` placeholders (:class:`string.Template`) so that JSON
braces in the body do not need escaping.
"""

from __future__ import annotations

from pathlib import Path
from string import Template

PROMPT_DIR = Path(__file__).parent / "prompts"


def render(name: str, /, **context: object) -> str:
    path = PROMPT_DIR / f"{name}.md"
    text = path.read_text()
    return Template(text).safe_substitute(
        {k: ("" if v is None else str(v)) for k, v in context.items()}
    )
