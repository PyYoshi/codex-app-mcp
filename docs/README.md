# Documentation

Current behavior is defined, in descending order of authority, by the
implementation and automated tests, these documents, and the root README.

| Document | Scope |
|---|---|
| [architecture.md](architecture.md) | Components, state, and runtime ownership |
| [configuration.md](configuration.md) | TOML discovery, environment, CLI, and defaults |
| [tools.md](tools.md) | `codex` and `codex-reply` contracts |
| [model-and-effort.md](model-and-effort.md) | Model and reasoning-effort resolution |
| [execution.md](execution.md) | Turns, cancellation, timeouts, and exclusion |
| [security.md](security.md) | Approval, sandbox, policy, and recursion guard |
| [errors.md](errors.md) | Public errors and result envelopes |
| [logging.md](logging.md) | stderr diagnostics and sensitive data handling |
| [testing.md](testing.md) | Test layers and release gates |

The only tool-schema source of truth is
`src/codex_app_mcp/schemas/tools.json`. Update the matching document and tests
with every behavioral change. Keep fake, fixed-SDK, subprocess, live-runtime,
and real-application validation distinct. Do not preserve local paths,
credentials, review artifacts, or execution transcripts in permanent docs.
