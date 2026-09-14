# AGENTS.md

These instructions apply to the entire repository unless a deeper AGENTS.md
adds or overrides them.

## Project

`codex-app-mcp` is a local stdio bridge exposing official Codex SDK App Server
operations as the `codex` and `codex-reply` MCP tools. v0.1 is a
non-interactive, fail-closed compatibility subset, not a general App Server
gateway.

Key modules:

- `server.py`: MCP wiring and stdio lifecycle
- `coordinator.py`: turns, cancellation, deadlines, leases, and runtime stop
- `backend/codex_sdk.py`: the sole boundary to the pinned Codex SDK
- `policy.py`: workspace, model, approval, and sandbox policy
- `registry.py`: thread metadata and advisory locks
- `schemas/tools.json`: the only tool-schema source of truth

Read the root README, `docs/README.md`, and the relevant document under
`docs/` before changing behavior.

## Environment

Support Python 3.14.x only, using `.python-version` as the development major.
Use aqua for pinned developer tools and uv with `uv.lock` for Python
dependencies. The runtime boundaries are `openai-codex==0.154.0` and
`mcp==2.2.0`.

Do not change dependency versions to bypass failures or silently substitute a
different `codex` from PATH. Supporting a new Python major is a separate
compatibility change.

## Safety invariants

- stdio is the only transport; stdout contains protocol data only.
- Decline server approval requests and fail closed on unknown requests.
- Accept only `approval-policy=never`; reject `danger-full-access`.
- Permit one active turn per bridge process; do not queue or steer implicitly.
- Never automatically retry a turn whose execution state is uncertain.
- Validate cwd, sandbox, allowlists, and locks on both new and reply paths.
- Interrupt and stop the exact backend owned by the run.
- Retain ownership and locks when runtime stop cannot be confirmed; reject reuse.
- Share the first close result per client; a later no-op is not proof of stop.
- Tie shared stop-task lifetime to the task, not an individual waiter.
- Never report shutdown failure or cleanup-budget exhaustion as success.

Do not patch lifecycle state strings in isolation. Preserve the relationships
among owner references, close futures, replacement, leases, and shutdown.

## Validation

Standard non-live acceptance:

```sh
aqua exec -- uv run --frozen pytest -m 'not live' -q
aqua exec -- uv run --frozen ruff check src tests
aqua exec -- uv run --frozen ruff format --check src tests
aqua exec -- uv build
aqua exec -- betterleaks dir .
aqua exec -- betterleaks git . --platform github
```

Run unit and matching contract tests for input/output/configuration changes;
contract tests for SDK/protocol changes; fault-injection and regression tests
for cancellation, timeout, stop, and locks; process tests for stdio and signals;
and distribution checks for schema changes.

Never weaken safety assertions or hide failures with unconditional skip/xfail.
Live tests perform real authentication, inference, sandboxed file operations,
cancellation, and termination; run them only with explicit user authorization.
Distinguish fake, pinned-SDK/fake-server, subprocess, live-runtime, and
real-application evidence in reports.

## Documentation and repository hygiene

Update the root README for user-facing operations and the matching document
under `docs/` for contracts. Update architecture/execution for lifecycle
changes and testing for assurance changes. Do not record local absolute paths,
credentials, full environments, review archives, or execution transcripts.

Preserve user changes. Do not commit generated caches, `.venv/`, or `dist/`;
duplicate schemas; or overwrite the tree with review artifacts. Check references
before deletion. Commits are allowed when requested; pushing, publishing,
changing repository visibility, and modifying global MCP configuration require
explicit authorization.

Completion reports must separate code/test acceptance from real-environment
fitness and state changes, validation results, omissions, assurance limits, and
commit information.
