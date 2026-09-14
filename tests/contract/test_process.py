"""Process-level contract tests: CT-18 (stdout purity), CT-20 (self-connection).

These spawn the real ``codex-app-mcp serve`` subprocess. The fake App Server
is injected through the documented test hook
``CODEX_APP_MCP_TEST_LAUNCH_ARGS`` (flagged by ``doctor`` when set).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .harness import DEFAULT_MODELS, FAKE_SERVER_SCRIPT, FINAL_ANSWER_ITEM

NORMAL_TURN = {
    "steps": [
        {"action": "response"},
        {"action": "itemCompleted", "item": FINAL_ANSWER_ITEM},
        {"action": "turnCompleted", "status": "completed"},
    ]
}


def _write_config(tmp_path: Path, workspace: Path) -> Path:
    config_path = tmp_path / "bridge.toml"
    config_path.write_text(
        f"""
[defaults]
model = "gpt-5.6-terra"
effort = "medium"
cwd = "{workspace}"
sandbox = "read-only"
approval_policy = "never"

[policy]
allowed_roots = ["{workspace}"]
allowed_models = []

[limits]
startup_timeout_seconds = 20
rpc_timeout_seconds = 20
turn_timeout_seconds = 60

[logging]
level = "INFO"
format = "json"
""",
        encoding="utf-8",
    )
    return config_path


def _launch_env(scenario_path: Path, journal_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_APP_MCP_TEST_LAUNCH_ARGS"] = json.dumps(
        [sys.executable, str(FAKE_SERVER_SCRIPT), str(scenario_path), str(journal_path)]
    )
    return env


def _read_json_line(proc: subprocess.Popen, timeout: float = 30.0) -> dict:
    """Read one stdout line and assert it is pure JSON (CT-18 core check)."""
    import selectors

    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    while True:
        events = selector.select(timeout=max(0.0, deadline - time.monotonic()))
        if not events:
            raise TimeoutError("no stdout line within timeout")
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("bridge closed stdout unexpectedly")
        text = (
            line.strip() if isinstance(line, str) else line.decode("utf-8", errors="strict").strip()
        )
        assert text, "blank line on stdout"
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"non-JSON output on stdout: {text[:200]!r}") from exc
        assert isinstance(message, dict), text
        return message


def test_ct18_stdout_is_pure_jsonrpc(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scenario = {"models": DEFAULT_MODELS, "turns": [NORMAL_TURN]}
    scenario_path = tmp_path / "scenario.json"
    journal_path = tmp_path / "journal.jsonl"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    _write_config(tmp_path, workspace)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "codex_app_mcp.cli",
            "serve",
        ],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_launch_env(scenario_path, journal_path),
        text=True,
        bufsize=1,
    )
    try:
        # Minimal legacy-era JSON-RPC handshake.
        proc.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "ct18", "version": "0"},
                    },
                }
            )
            + "\n"
        )
        proc.stdin.flush()
        init_response = _read_json_line(proc)
        assert init_response.get("id") == 1
        assert "result" in init_response

        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.flush()

        proc.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "codex", "arguments": {"prompt": "stdout purity"}},
                }
            )
            + "\n"
        )
        proc.stdin.flush()

        while True:
            message = _read_json_line(proc)
            if message.get("id") == 2:
                assert "error" not in message, message
                result = message["result"]
                assert result["isError"] is False
                assert result["structuredContent"]["content"] == "レビュー結果です。"
                break

        # Diagnostics must be on stderr, never stdout: stop and inspect.
        proc.stdin.close()
        stderr = proc.stderr.read()
        proc.wait(timeout=15)
        assert proc.returncode == 0
        assert "stdout purity" not in stderr  # prompt never logged
        assert "レビュー結果です。" not in stderr  # answer text never logged
    finally:
        if proc.poll() is None:  # pragma: no cover - cleanup safety
            proc.kill()
            proc.wait(timeout=5)


def test_ct20_self_connection_guard_stops_serve(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = _write_config(tmp_path, workspace)
    env = dict(os.environ)
    env["CODEX_APP_MCP_CHILD"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "codex_app_mcp.cli",
            "serve",
            "--config",
            str(config_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 3, proc.stderr
    assert proc.stdout == ""  # nothing on stdout; refusal message on stderr
    assert "recursive self-connection" in proc.stderr


def test_cli_version_and_bad_config(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "codex_app_mcp.cli", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip()

    bad = tmp_path / "bad.toml"
    bad.write_text("[defaults]\nsandbox = 'danger-full-access'\n")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "codex_app_mcp.cli",
            "serve",
            "--config",
            str(bad),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "CONFIG_KEY_DENIED" in proc.stderr


def test_cli_init_creates_minimal_project_config_without_overwrite(tmp_path: Path):
    repo = tmp_path / "repo"
    nested = repo / "nested"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    proc = subprocess.run(
        [sys.executable, "-m", "codex_app_mcp.cli", "init"],
        cwd=nested,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    config_path = repo / "bridge.toml"
    assert proc.returncode == 0, proc.stderr
    config = config_path.read_text(encoding="utf-8")
    assert f'allowed_roots = ["{repo}"]' in config
    assert "cwd" not in config
    assert "model" not in config

    second = subprocess.run(
        [sys.executable, "-m", "codex_app_mcp.cli", "init"],
        cwd=nested,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert second.returncode == 0, second.stderr
    assert config_path.read_text(encoding="utf-8") == config


def test_serve_without_config_fails_before_stdio(tmp_path: Path):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    proc = subprocess.run(
        [sys.executable, "-m", "codex_app_mcp.cli", "serve"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "codex-app-mcp init" in proc.stderr


def test_serve_rejects_empty_allowed_roots_at_startup(tmp_path: Path):
    (tmp_path / "bridge.toml").write_text("[policy]\nallowed_roots = []\n")
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    proc = subprocess.run(
        [sys.executable, "-m", "codex_app_mcp.cli", "serve"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "allowed_roots is empty" in proc.stderr
    assert str(tmp_path / "bridge.toml") in proc.stderr


def test_cli_init_refuses_home_as_allowed_root(tmp_path: Path):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "codex_app_mcp.cli", "init", "--root", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert not (tmp_path / "bridge.toml").exists()
    assert "narrower workspace" in proc.stderr


def test_packaged_tools_json_is_available():
    """The single authoritative schema must be present in installed layouts."""
    from codex_app_mcp.server import TOOLS_SCHEMA_PATH, load_tool_definitions

    assert TOOLS_SCHEMA_PATH.is_file()
    assert [tool.name for tool in load_tool_definitions()] == ["codex", "codex-reply"]
