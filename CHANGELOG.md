# Changelog

All notable changes are documented here. This project follows Semantic
Versioning and uses Git tags as its release channel.

## [Unreleased]

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

[Unreleased]: https://github.com/PyYoshi/codex-app-mcp/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/PyYoshi/codex-app-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/PyYoshi/codex-app-mcp/releases/tag/v0.1.0
