"""codex-app-mcp: local MCP bridge for the Codex App Server.

Exposes the ``codex`` / ``codex-reply`` MCP tools backed by Codex App Server
threads and turns, using the official ``openai-codex`` SDK. Noninteractive
compatibility subset; see ``docs/`` for the specification.
"""

__version__ = "0.1.0"

CHILD_ENV_FLAG = "CODEX_APP_MCP_CHILD"
"""Set in the child runtime environment to break recursive self-connections."""
