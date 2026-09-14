# Security model

The bridge is non-interactive and fail-closed. File-change and command approval
requests are declined immediately. Unknown server-initiated requests fail the
connection; no synthetic success is returned. Only `approval-policy=never` is
accepted.

The default sandbox is `read-only`. `workspace-write` requires operator
permission; `danger-full-access` is rejected. `allowed_roots` controls which
cwd and resumed-thread workspaces are accepted, but it is not itself a file-read
sandbox. New and resumed threads must confirm the requested sandbox and approval
settings before a turn starts.

Existing Codex authentication is inherited; tools never accept API keys or
login operations. Logs exclude prompts, answers, file contents, tokens, and raw
SDK stderr. The child runtime receives `CODEX_APP_MCP_CHILD=1`; attempting to
start the bridge in that environment exits to prevent recursive MCP wiring.
The test-only launch override is rejected by `doctor` and must not be used in
production. Advisory locks coordinate cooperating bridge processes only.
