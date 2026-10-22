# Configuration

Unless `--config PATH` is supplied, the bridge searches upward from its launch
directory for the nearest `bridge.toml`, then checks the user configuration
directory. Built-in defaults are fail-closed. Per-setting precedence is
`CLI > CODEX_APP_MCP_* environment > TOML > built-in`.

A file found by the upward search is project-local and may be committed to an
untrusted checkout. Startup rejects it with `CONFIG_KEY_DENIED` unless it is
owned by the current user and not world-writable (POSIX), and unless every
`allowed_roots` entry resolves inside the directory holding the file. An
explicit `--config` file and the user configuration are not confined; use
`--config` for other workspaces, or on filesystems that cannot represent
ownership. Because discovery stops at the nearest file, the user configuration
is not read while a project-local file exists.

`allowed_roots` entries are resolved once at startup, relative entries against
the launch directory. Workspace checks never re-resolve them, so a symlink
created later cannot widen a root.

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
