"""Regression tests for the independent review findings (B-1, B-2, M-1)."""

from __future__ import annotations

import asyncio
import os

import pytest

from codex_app_mcp.errors import (
    RUNTIME_MISMATCH,
    STARTUP_TIMEOUT,
    UNSUPPORTED_SERVER_REQUEST,
    BridgeError,
)

from .harness import DEFAULT_MODELS, FINAL_ANSWER_ITEM


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _started_pid(rt) -> int | None:
    for event in rt.journal():
        if event.get("event") == "started":
            return int(event["pid"])
    return None


NORMAL_TURN = {
    "steps": [
        {"action": "response"},
        {"action": "itemCompleted", "item": FINAL_ANSWER_ITEM},
        {"action": "turnCompleted", "status": "completed"},
    ]
}


async def test_b1_startup_timeout_kills_half_started_runtime(make_runtime):
    """B-1: the hung-initialize path must not leak the process or worker."""
    rt = await make_runtime(
        {"models": DEFAULT_MODELS, "hangInitialize": True, "turns": []},
        startup_timeout=1.0,
        start=False,
    )
    with pytest.raises(BridgeError) as excinfo:
        await rt.backend.start_runtime()
    assert excinfo.value.code == STARTUP_TIMEOUT

    pid = _started_pid(rt)
    assert pid is not None
    # The half-started child process must be terminated by close().
    deadline = asyncio.get_event_loop().time() + 5
    while _pid_alive(pid) and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
    assert not _pid_alive(pid), f"half-started runtime pid {pid} leaked"

    # close() is bounded and idempotent after the timeout path.
    await asyncio.wait_for(rt.backend.close(), timeout=10)
    await asyncio.wait_for(rt.backend.close(), timeout=10)


async def test_b2_unsupported_request_stops_runtime_and_next_call_recovers(make_bridge, tmp_path):
    """B-2: after an unsupported server request the runtime is stopped and
    the next call gets a fresh runtime; the old process must not leak."""
    scenario = {
        "models": DEFAULT_MODELS,
        "threadRead": {"model": "gpt-5.6-terra", "cwd": str(tmp_path / "workspace")},
        "turnsByPrompt": [
            {
                "match": "trigger unknown",
                "steps": [
                    {"action": "response"},
                    {
                        "action": "unknownRequest",
                        "method": "item/someNewPermission/requestApproval",
                    },
                    {"action": "turnCompleted", "status": "completed"},
                ],
            },
            {"match": "recovered", "steps": NORMAL_TURN["steps"]},
        ],
        "turns": [],
    }
    async with make_bridge(scenario) as bridge:
        failed = await bridge.call("codex", {"prompt": "trigger unknown"})
        assert failed.is_error is True
        assert failed.meta["codex-app-mcp"]["code"] == UNSUPPORTED_SERVER_REQUEST
        first_pid = _started_pid(bridge)

        # The next call recovers on a fresh runtime process.
        recovered = await bridge.call("codex", {"prompt": "recovered"})
        assert recovered.is_error is False, recovered.content[0].text
        second_pid = _started_pid(bridge)
        assert second_pid != first_pid

        # The old process must be gone (no leak).
        deadline = asyncio.get_event_loop().time() + 5
        while _pid_alive(first_pid) and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.1)
        assert not _pid_alive(first_pid), f"degraded runtime pid {first_pid} leaked"
        assert _pid_alive(second_pid)


async def test_m1_resume_reapplies_bridge_sandbox(make_bridge, tmp_path):
    """M-1: persisted workspace-write threads resume under the bridge's
    sandbox; a runtime that cannot apply it fails before the turn starts."""
    scenario = {
        "threadStart": {"model": "gpt-5.6-terra", "reasoningEffort": "medium"},
        "threadRead": {"model": "gpt-5.6-terra", "cwd": str(tmp_path / "workspace")},
        "turns": [NORMAL_TURN, NORMAL_TURN],
    }
    async with make_bridge(scenario) as bridge:
        first = await bridge.call("codex", {"prompt": "create thread"})
        thread_id = first.structured_content["threadId"]

    # New bridge process: thread persisted under workspace-write; the bridge
    # defaults to read-only and must re-apply it on resume (design 7.2).
    async with make_bridge(scenario) as bridge2:
        resumed = await bridge2.call(
            "codex-reply", {"prompt": "after restart", "threadId": thread_id}
        )
        assert resumed.is_error is False
        resume_params = bridge2.requests("thread/resume")[-1]["params"]
        assert resume_params.get("sandbox") == "read-only"

    # Mismatch: the runtime confirms a higher sandbox than requested ->
    # refuse to start a turn.
    mismatch_scenario = {
        "threadResume": {
            "model": "gpt-5.6-terra",
            "reasoningEffort": "medium",
            "forceSandbox": {"type": "workspaceWrite"},
        },
        "threadRead": {"model": "gpt-5.6-terra", "cwd": str(tmp_path / "workspace")},
        "turns": [NORMAL_TURN],
    }
    async with make_bridge(mismatch_scenario) as bridge3:
        denied = await bridge3.call("codex-reply", {"prompt": "mismatch", "threadId": "thread-1"})
        assert denied.is_error is True
        meta = denied.meta["codex-app-mcp"]
        assert meta["code"] == RUNTIME_MISMATCH
        assert bridge3.requests("turn/start") == []
