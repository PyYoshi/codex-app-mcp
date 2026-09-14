# Testing and release gates

The default suite contains unit tests, fixed-SDK contract tests against a fake
App Server, subprocess protocol tests, fault injection, and lifecycle
regressions. Live tests are excluded by pytest configuration.

```sh
aqua exec -- uv run --frozen pytest -m 'not live' -q
aqua exec -- uv run --frozen ruff check src tests
aqua exec -- uv run --frozen ruff format --check src tests
aqua exec -- uv build
```

Live tests require explicit authorization because they use real credentials,
network inference, sandboxed file operations, cancellation, and SIGTERM:

```sh
aqua exec -- uv run --frozen pytest -m live -q
```

Release gates cover tool schemas, model/effort inheritance, approval denial,
workspace/sandbox enforcement, early-event delivery, cancellation during
startup and execution, transport loss, concurrency, stdout purity, output
bounds, recursion prevention, close/stop ownership, and signal shutdown.

The v0.1 implementation has also been exercised with a real Codex runtime for
new/reply, sandbox effectiveness, cancellation, and child termination, and with
OpenCode 1.18.30 for new/reply structured IDs and workspace-write. Claude Code
application-level validation and hostile OS-level process survival remain
outside the measured assurance. CI never runs live tests.
