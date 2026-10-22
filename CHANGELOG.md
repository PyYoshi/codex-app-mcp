# Changelog

All notable changes are documented here. This project follows Semantic
Versioning and uses Git tags as its release channel.

## [Unreleased]

## [0.1.2] - 2026-10-23

- Security: a `bridge.toml` found by upward discovery can no longer allow
  workspaces outside its own directory, and on POSIX must be owned by the
  current user and not world-writable. Previously a cloned repository could
  commit one that replaced the user's policy with `allowed_roots = ["/"]`.
- Security: `allowed_roots` are resolved once at startup, so a symlink created
  after startup can no longer redirect a root outside the configured path.
- Changed the supported Python major from 3.14 to 3.15; Python 3.14 is no
  longer supported.
- Updated the runtime boundaries to `openai-codex==0.163.0` (with its bundled
  runtime) and `mcp==2.3.0`.
- Updated the pinned developer tools (uv 0.13.5, betterleaks 1.9.0) and the CI
  aqua installation (aqua 2.65.2).

## [0.1.1] - 2026-09-14

- Fixed `doctor` incorrectly treating a normal Codex project trust entry as a
  recursive MCP server registration.

## [0.1.0] - 2026-09-14

- Added the `codex` and `codex-reply` MCP tools over stdio.
- Added fail-closed approval, sandbox, workspace, model, and effort policy.
- Added cancellation, timeout, advisory locking, and runtime shutdown handling.
- Added project-local configuration discovery, safe initialization, and doctor diagnostics.
- Added structured results with a text fallback for MCP clients including OpenCode.
- Validated the pinned SDK against fake App Server contract tests and live Codex runtime tests.

[Unreleased]: https://github.com/PyYoshi/codex-app-mcp/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/PyYoshi/codex-app-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/PyYoshi/codex-app-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/PyYoshi/codex-app-mcp/releases/tag/v0.1.0
