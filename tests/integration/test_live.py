"""Live integration tests: real auth, real runtime,
real MCP client. Run explicitly:

    uv run pytest tests/integration -m live -q

These may perform real inference and (for CT-21) real file operations inside
an isolated temporary workspace. They are excluded from the default suite.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.live

BRIDGE_CMD = [sys.executable, "-m", "codex_app_mcp.cli"]
MODEL = "gpt-5.6-terra"


def _live_config(workspace: Path) -> str:
    return f"""
[defaults]
model = "{MODEL}"
effort = "low"
cwd = "{workspace}"
sandbox = "read-only"
approval_policy = "never"

[policy]
allowed_roots = ["{workspace}"]
allowed_models = ["{MODEL}"]
allowed_sandboxes = ["read-only", "workspace-write"]

[limits]
startup_timeout_seconds = 60
rpc_timeout_seconds = 60
turn_timeout_seconds = 600

[logging]
level = "INFO"
format = "json"
"""


def _bridge_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CODEX_APP_MCP_TEST_LAUNCH_ARGS", None)
    return env


def _child_runtime_pids() -> set[int]:
    """PIDs of codex app-server processes spawned by this bridge (marked
    via CODEX_APP_MCP_CHILD=1 in their environment)."""
    pids: set[int] = set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            environ = (proc / "environ").read_bytes()
        except OSError:
            continue
        if b"CODEX_APP_MCP_CHILD=1" in environ:
            pids.add(int(proc.name))
    return pids


# --- CT-02/FR-01/02: real turn over a real MCP client ------------------------


async def test_live_real_client_turn_and_reply(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    config_path = tmp_path / "bridge.toml"
    config_path.write_text(_live_config(workspace), encoding="utf-8")

    params = StdioServerParameters(
        command=BRIDGE_CMD[0],
        args=[*BRIDGE_CMD[1:], "serve", "--config", str(config_path)],
        env=_bridge_env(),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {"codex", "codex-reply"}

            first = await session.call_tool(
                "codex",
                {"prompt": "Reply with exactly the word: acknowledged"},
                read_timeout_seconds=300,
            )
            assert first.is_error is False, first.content
            text = first.content[0].text
            assert "acknowledged" in text.lower()
            thread_id = first.structured_content["threadId"]
            meta = first.meta["codex-app-mcp"]
            assert meta["status"] == "completed"
            assert meta["requestedModel"] is None  # inherited, not requested

            second = await session.call_tool(
                "codex-reply",
                {"prompt": "Now reply with exactly: done", "threadId": thread_id},
                read_timeout_seconds=300,
            )
            assert second.is_error is False, second.content
            assert "done" in second.content[0].text.lower()
            assert second.structured_content["threadId"] == thread_id


# --- CT-21: effective sandbox in an isolated workspace ------------------------


async def test_live_ct21_sandbox_effectiveness(tmp_path: Path):
    workspace = tmp_path / "sandbox-ws"
    workspace.mkdir()
    config_path = tmp_path / "bridge.toml"
    config_path.write_text(
        _live_config(workspace).replace('sandbox = "read-only"', 'sandbox = "workspace-write"'),
        encoding="utf-8",
    )
    params = StdioServerParameters(
        command=BRIDGE_CMD[0],
        args=[*BRIDGE_CMD[1:], "serve", "--config", str(config_path)],
        env=_bridge_env(),
    )
    marker = f"ct21-write-{time.monotonic_ns()}.txt"

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # workspace-write: the model may create the file.
            result = await session.call_tool(
                "codex",
                {
                    "prompt": (
                        f"Create a file named {marker} in the current working "
                        "directory containing the single word ok. Do not run "
                        "anything else. Reply with the word ok."
                    ),
                    "sandbox": "workspace-write",
                },
                read_timeout_seconds=600,
            )
            assert result.is_error is False, result.content
    assert (workspace / marker).is_file(), "workspace-write did not permit writing"

    # read-only: writing must not take effect.
    read_only_config = tmp_path / "bridge-ro.toml"
    read_only_config.write_text(_live_config(workspace), encoding="utf-8")
    marker2 = f"ct21-ro-{time.monotonic_ns()}.txt"
    params = StdioServerParameters(
        command=BRIDGE_CMD[0],
        args=[*BRIDGE_CMD[1:], "serve", "--config", str(read_only_config)],
        env=_bridge_env(),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "codex",
                {
                    "prompt": (
                        f"Create a file named {marker2} in the current working "
                        "directory containing the word ok, then reply ok. If you "
                        "cannot create it, reply with the error you saw."
                    )
                },
                read_timeout_seconds=600,
            )
            assert result.is_error is False  # the turn completes either way
    assert not (workspace / marker2).is_file(), "read-only sandbox permitted a write"


# --- CT-22: cancellation over a real client ------------------------------------


async def test_live_ct22_cancel_long_turn(tmp_path: Path):
    workspace = tmp_path / "cancel-ws"
    workspace.mkdir()
    config_path = tmp_path / "bridge.toml"
    config_path.write_text(_live_config(workspace), encoding="utf-8")
    params = StdioServerParameters(
        command=BRIDGE_CMD[0],
        args=[*BRIDGE_CMD[1:], "serve", "--config", str(config_path)],
        env=_bridge_env(),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            task = asyncio.create_task(
                session.call_tool(
                    "codex",
                    {
                        "prompt": (
                            "Write a detailed 2000-word essay about the history "
                            "of Unix. Take your time and be thorough."
                        )
                    },
                    read_timeout_seconds=600,
                )
            )
            await asyncio.sleep(8)  # let the turn start
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=120)

            # The bridge remains usable after cancellation. While the
            # cancelled run still occupies the single slot the answer is a
            # retryable SERVER_BUSY, so poll until the cleanup completes.
            deadline = time.monotonic() + 120
            followup = None
            while time.monotonic() < deadline:
                followup = await session.call_tool(
                    "codex",
                    {"prompt": "Reply with exactly: still-here"},
                    read_timeout_seconds=300,
                )
                if followup.is_error is False:
                    break
                meta = (followup.meta or {}).get("codex-app-mcp", {})
                assert meta.get("retryable") is True, meta
                await asyncio.sleep(2)
            assert followup is not None and followup.is_error is False
            assert "still-here" in followup.content[0].text.lower()


# --- CT-24: shutdown stops runtime children -------------------------------------


async def test_live_ct24_sigterm_stops_children(tmp_path: Path):
    workspace = tmp_path / "shutdown-ws"
    workspace.mkdir()
    config_path = tmp_path / "bridge.toml"
    config_path.write_text(_live_config(workspace), encoding="utf-8")
    proc = subprocess.Popen(
        [*BRIDGE_CMD, "serve", "--config", str(config_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_bridge_env(),
        text=True,
        bufsize=1,
    )
    try:
        # Minimal handshake so the bridge is fully up.
        proc.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "ct24", "version": "0"},
                    },
                }
            )
            + "\n"
        )
        proc.stdin.flush()
        deadline = time.monotonic() + 60
        line = ""
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise AssertionError("bridge exited during handshake")
            message = json.loads(line)
            if message.get("id") == 1:
                break
        else:
            raise TimeoutError("initialize response missing")
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.flush()
        # The runtime starts lazily on the first execution request; trigger it
        # with a minimal real call.
        proc.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "codex", "arguments": {"prompt": "Reply ok"}},
                }
            )
            + "\n"
        )
        proc.stdin.flush()
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise AssertionError("bridge exited mid-call")
            message = json.loads(line)
            if message.get("id") == 2:
                assert message["result"]["isError"] is False
                break
        else:
            raise TimeoutError("tools/call response missing")

        children = _child_runtime_pids()
        assert children, "no marked runtime child found (marker env missing?)"

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            raise

        # All bridge-owned runtime children must be gone after SIGTERM.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            remaining = _child_runtime_pids() & children
            if not remaining:
                break
            await asyncio.sleep(0.5)
        remaining = _child_runtime_pids() & children
        assert not remaining, f"runtime children survived shutdown: {remaining}"
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()
            proc.wait(timeout=5)
