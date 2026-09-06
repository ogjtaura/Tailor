"""Local autonomous engineering-agent harness (V0).

Drives an implement -> verify -> review -> checkpoint loop over this repository
using the user's already-authenticated local ``claude`` and ``codex`` CLIs as
subprocess workers. No LLM API keys, no LangChain model wrappers.

See ``agent_harness/README.md`` for architecture, the graph, and the V0 safety
model / limitations.
"""

__version__ = "0.0.0"
