# Configuration

Unless `--config PATH` is supplied, the bridge searches upward from its launch
directory for the nearest `bridge.toml`, then checks the user configuration
directory. Built-in defaults are fail-closed. Per-setting precedence is
`CLI > CODEX_APP_MCP_* environment > TOML > built-in`.

`codex-app-mcp init` creates a project-local file, allows the resolved Git root
(or current directory), and never overwrites an existing file. `init --global`
and `init --config PATH` select other destinations.

Tables are `runtime`, `defaults`, `policy`, `limits`, and `logging`.
Unknown keys are startup errors. Only the bundled runtime,
`approval_policy = "never"`, one active turn, and the `read-only` and
`workspace-write` sandboxes are supported. An empty `allowed_roots` denies
all workspaces; an empty `allowed_models` permits the runtime catalog.

`defaults.cwd` is optional. The launch directory is used when neither the call
nor configuration supplies cwd, so set it only for launchers with an unstable
working directory. See [bridge.example.toml](../bridge.example.toml) for all
limits. `doctor` checks configuration, SDK/runtime, authentication presence,
recursion, and the model catalog without inference or secret output.
