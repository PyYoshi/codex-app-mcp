# Logging

stdout is exclusively for MCP JSON-RPC. Diagnostics use stderr, as JSON lines
by default or text for local troubleshooting.

Logs may contain request/thread/turn IDs, runtime version and generation,
configuration provenance, duration, result class, and public error code. They
omit prompts, answers, file contents, credentials, and raw SDK stderr. Internal
exception details are sanitized and bounded; user messages and diagnostics are
kept separate.
