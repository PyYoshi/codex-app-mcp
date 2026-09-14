# codex-app-mcp

[日本語](README.ja.md)

`codex-app-mcp` is a local stdio MCP server that exposes Codex App Server
threads through the `codex` and `codex-reply` tools. It uses the official
Codex SDK and deliberately implements a small, non-interactive, fail-closed
compatibility surface.

## Why this project exists

Codex CLI previously exposed an MCP server mode. After that mode was removed,
tools without an official Codex integration—such as OpenCode—no longer had a
simple way to use Codex as a collaborating agent. This project restores that
workflow as a narrowly scoped bridge built on the supported Codex SDK: any MCP
client can start a Codex thread and continue it by thread ID without embedding
or reimplementing the Codex runtime.

## Safety model

- Approval requests are declined; only `approval-policy=never` is supported.
- The default sandbox is `read-only`; `danger-full-access` is rejected.
- One turn may be active per bridge process. Calls are never silently queued.
- A turn whose execution state is unknown is never submitted again automatically.
- `allowed_roots` restricts accepted workspaces and is empty (deny all) by default.
- Logs go to stderr and omit prompts, answers, credentials, and file contents.

This is not a general-purpose App Server gateway. Interactive approval, steering
an active turn, and HTTP transport are outside the v0.1 scope.

## Requirements

- Python 3.14.x (the latest stable Python major supported by this release)
- [uv](https://docs.astral.sh/uv/)
- an existing Codex login, such as `~/.codex/auth.json`
- network access to OpenAI when running inference

Runtime boundaries are pinned to `openai-codex==0.154.0` and `mcp==2.2.0`.
The Codex runtime bundled with the SDK is used; an unrelated `codex` on `PATH`
is not substituted.

## Quick start

Create a safe project-local configuration from the target repository:

```sh
cd /absolute/path/to/target-project
uvx --from git+https://github.com/PyYoshi/codex-app-mcp.git@v0.1.1 \
  codex-app-mcp init
uvx --from git+https://github.com/PyYoshi/codex-app-mcp.git@v0.1.1 \
  codex-app-mcp doctor
```

`init` creates `bridge.toml` at the Git root (or current directory) and places
that workspace in `allowed_roots`. It never overwrites an existing file. A
global config can be created with `init --global`, although project-local
configuration is recommended. The first `uvx` run downloads the pinned runtime
and may transfer roughly 120 MiB.

Configuration discovery is bottom-up:

1. `--config PATH`, when supplied
2. the nearest `bridge.toml`, searching from the bridge launch directory upward
3. the user config (`$XDG_CONFIG_HOME/codex-app-mcp/bridge.toml`, or the platform equivalent)
4. built-in fail-closed defaults

Individual values use `CLI > CODEX_APP_MCP_* environment > TOML > built-in`
precedence. `defaults.cwd` remains supported for unusual launchers, but normally
the client should launch the bridge in its workspace and leave it unset. See
[configuration](docs/configuration.md).

## MCP client configuration

Use an executable plus an argument array, not a shell command string. Before the
`v0.1.1` tag exists, replace it with a commit SHA.

### Claude Code

```sh
claude mcp add codex -- \
  uvx --from git+https://github.com/PyYoshi/codex-app-mcp.git@v0.1.1 \
  codex-app-mcp serve
```

### OpenCode

```json
{
  "mcp": {
    "codex": {
      "type": "local",
      "command": [
        "uvx", "--from",
        "git+https://github.com/PyYoshi/codex-app-mcp.git@v0.1.1",
        "codex-app-mcp", "serve"
      ],
      "enabled": true
    }
  }
}
```

Do not register this bridge in the child Codex runtime's own MCP configuration;
that can create recursive self-connection. The bridge includes a guard, but the
configuration itself is unsupported.

## Tools

Start a thread:

```json
{"prompt":"Summarize this repository.","sandbox":"read-only"}
```

Continue it with the returned opaque thread ID:

```json
{"prompt":"Now list the main risks.","threadId":"01a0..."}
```

Successful results expose `threadId` and `content` in `structuredContent` and in
a JSON text fallback for clients that do not surface structured content. Full
input, output, model, effort, cancellation, and error contracts are documented
in [docs](docs/README.md).

## Development

Tools used by this repository are pinned with [aqua](https://aquaproj.github.io/):

```sh
aqua install
aqua exec -- uv sync --frozen --all-groups
aqua exec -- uv run --frozen pytest -m 'not live' -q
aqua exec -- uv run --frozen ruff check src tests
aqua exec -- uv run --frozen ruff format --check src tests
aqua exec -- uv build
aqua exec -- betterleaks dir .
aqua exec -- betterleaks git . --platform github
```

Live tests perform real authentication, inference, sandboxed file operations,
cancellation, and process termination. Run them only with explicit authorization:

```sh
aqua exec -- uv run --frozen pytest -m live -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[the testing guide](docs/testing.md). This project is distributed under the
[MIT License](LICENSE) and is installed directly from GitHub; it is not
published on PyPI.
