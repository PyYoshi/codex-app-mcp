# Contributing

Thank you for helping improve `codex-app-mcp`.

## Development setup

The project supports Python 3.14.x only. Install aqua, then run:

```sh
aqua install
aqua exec -- uv sync --frozen --all-groups
```

Before submitting a pull request, run the non-live release checks documented in
the README. Do not run live tests without explicit authorization from the owner
of the credentials and environment involved.

## Change guidelines

- Preserve the fail-closed approval, workspace, sandbox, and lifecycle rules.
- Update the relevant document under `docs/` with behavioral changes.
- Add focused tests; do not weaken assertions or hide failures with skips.
- Keep stdout exclusively for MCP JSON-RPC and avoid logging sensitive content.
- Do not change pinned runtime dependencies merely to bypass a failure.
- Never include credentials, local absolute paths, review archives, or generated
  environments in a commit.

Open an issue before proposing a large compatibility expansion. By contributing,
you agree that your work is licensed under the repository's MIT License.
