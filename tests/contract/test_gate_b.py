"""Gate B contract tests: CT-01..CT-09, CT-13, CT-17, CT-20 (design 14.2).

These drive the real MCP wire (in-process memory streams) against the real
coordinator/server stack over the fake App Server.
"""

from __future__ import annotations

import asyncio

import pytest

from codex_app_mcp.config import PolicyConfig
from tests.contract.conftest import _test_config

from .harness import FINAL_ANSWER_ITEM

NORMAL_TURN = {
    "steps": [
        {"action": "response"},
        {"action": "itemCompleted", "item": FINAL_ANSWER_ITEM},
        {"action": "turnCompleted", "status": "completed"},
    ]
}
SLOW_TURN = {
    "steps": [
        {"action": "response"},
        {"action": "sleep", "ms": 400},
        {"action": "itemCompleted", "item": FINAL_ANSWER_ITEM},
        {"action": "sleep", "ms": 30000},
        {"action": "turnCompleted", "status": "completed"},
    ]
}


def _meta(result) -> dict:
    meta = getattr(result, "meta", None)
    if isinstance(meta, dict):
        return meta.get("codex-app-mcp", {})
    return getattr(meta, "codex-app-mcp", {})


def _thread_id(result) -> str:
    return result.structured_content["threadId"]


# --- CT-01: tool catalog ---------------------------------------------------------


async def test_ct01_tool_catalog(make_bridge):
    async with make_bridge({"turns": [NORMAL_TURN]}) as bridge:
        result = await bridge.client.list_tools()
        tools = {tool.name: tool for tool in result.tools}
        assert set(tools) == {"codex", "codex-reply"}

        codex = tools["codex"]
        props = codex.input_schema["properties"]
        for kebab in (
            "approval-policy",
            "base-instructions",
            "developer-instructions",
            "compact-prompt",
        ):
            assert kebab in props, f"kebab-case arg missing: {kebab}"
        assert codex.input_schema["required"] == ["prompt"]
        assert codex.input_schema["additionalProperties"] is False
        assert sorted(codex.output_schema["required"]) == ["content", "threadId"]

        reply = tools["codex-reply"]
        reply_props = reply.input_schema["properties"]
        assert "threadId" in reply_props and "conversationId" in reply_props
        assert "model" in reply_props and "effort" in reply_props

        assert codex.annotations.destructive_hint is True
        assert codex.annotations.read_only_hint is False


# --- CT-02: normal success shape ----------------------------------------------------


async def test_ct02_success_output_matches(make_bridge):
    async with make_bridge({"turns": [NORMAL_TURN]}) as bridge:
        result = await bridge.call("codex", {"prompt": "レビューして。"})
        assert result.is_error is False
        text = result.content[0].text
        structured = result.structured_content
        assert structured["content"] == text
        assert text == "レビュー結果です。"
        assert structured["threadId"].startswith("thread-")
        meta = _meta(result)
        assert meta["status"] == "completed"
        assert meta["requestedModel"] is None
        assert meta["turnId"].startswith("turn-")


# --- CT-03: legacy alias ---------------------------------------------------------------


async def test_ct03_conversation_id_alias(make_bridge):
    async with make_bridge({"turns": [NORMAL_TURN, NORMAL_TURN]}) as bridge:
        first = await bridge.call("codex", {"prompt": "start"})
        thread_id = _thread_id(first)
        via_alias = await bridge.call(
            "codex-reply", {"prompt": "continue", "conversationId": thread_id}
        )
        assert via_alias.is_error is False

        mismatch = await bridge.call(
            "codex-reply",
            {"prompt": "x", "threadId": thread_id, "conversationId": "other"},
        )
        assert mismatch.is_error is True
        assert _meta(mismatch)["code"] == "VALIDATION_ERROR"

        missing = await bridge.call("codex-reply", {"prompt": "x"})
        assert missing.is_error is True


# --- CT-04: startup defaults reach the runtime ------------------------------------------


async def test_ct04_startup_defaults_applied(make_bridge, tmp_path):
    async with make_bridge({"turns": [NORMAL_TURN]}) as bridge:
        await bridge.call("codex", {"prompt": "defaults"})
        await bridge.wait_for_turns(1)
        thread_start = bridge.requests("thread/start")
        assert thread_start, "thread/start missing"
        params = thread_start[-1]["params"]
        assert params["model"] == "gpt-5.6-terra"
        assert params["config"]["model_reasoning_effort"] == "medium"
        assert params["approvalPolicy"] == "never"
        assert params["sandbox"] == "read-only"
        turn_start = bridge.requests("turn/start")[-1]["params"]
        assert turn_start["model"] == "gpt-5.6-terra"
        assert turn_start["effort"] == "medium"


# --- CT-05: call overrides reach the runtime ----------------------------------------------


async def test_ct05_call_overrides_passed(make_bridge):
    async with make_bridge({"turns": [NORMAL_TURN]}) as bridge:
        await bridge.call(
            "codex",
            {"prompt": "override", "model": "gpt-5.6-atlas", "effort": "xhigh"},
        )
        await bridge.wait_for_turns(1)
        thread_params = bridge.requests("thread/start")[-1]["params"]
        turn_params = bridge.requests("turn/start")[-1]["params"]
        assert thread_params["model"] == "gpt-5.6-atlas"
        assert turn_params["model"] == "gpt-5.6-atlas"
        assert turn_params["effort"] == "xhigh"


# --- CT-06: continuation inheritance --------------------------------------------------------


async def test_ct06_effort_inheritance(make_bridge):
    turns = [dict(NORMAL_TURN) for _ in range(4)]
    async with make_bridge({"turns": turns}) as bridge:
        first = await bridge.call("codex", {"prompt": "1"})
        thread_id = _thread_id(first)
        await bridge.call("codex-reply", {"prompt": "2", "threadId": thread_id, "effort": "high"})
        await bridge.call("codex-reply", {"prompt": "3", "threadId": thread_id})
        await bridge.call("codex-reply", {"prompt": "4", "threadId": thread_id, "effort": "medium"})
        await bridge.wait_for_turns(4)
        efforts = [req["params"].get("effort") for req in bridge.requests("turn/start")]
        assert efforts == ["medium", "high", "high", "medium"]
        models = [req["params"].get("model") for req in bridge.requests("turn/start")]
        assert models == ["gpt-5.6-terra"] * 4


# --- CT-07: resume after bridge restart -------------------------------------------------------


async def test_ct07_resume_inherits_runtime_state_not_startup(make_bridge, tmp_path):
    scenario = {
        "threadStart": {"model": "gpt-5.6-terra", "reasoningEffort": "medium"},
        "threadResume": {"model": "gpt-5.6-atlas", "reasoningEffort": "high"},
        "threadRead": {"model": "gpt-5.6-atlas", "cwd": str(tmp_path / "workspace")},
        "turns": [NORMAL_TURN, NORMAL_TURN],
    }
    # First bridge process creates the thread, then "restarts" (new
    # coordinator + registry) and replies with no explicit model/effort.
    async with make_bridge(scenario) as bridge:
        first = await bridge.call("codex", {"prompt": "original process"})
        thread_id = _thread_id(first)

    async with make_bridge(scenario) as bridge2:
        resumed = await bridge2.call(
            "codex-reply", {"prompt": "after restart", "threadId": thread_id}
        )
        assert resumed.is_error is False
        await bridge2.wait_for_turns(1)
        # Resume params re-apply bridge safety settings (design 7.2).
        resume_params = bridge2.requests("thread/resume")[-1]["params"]
        assert resume_params.get("approvalPolicy") == "never"
        # Turn uses the thread's runtime-persisted model, not startup default.
        turn_params = bridge2.requests("turn/start")[-1]["params"]
        assert turn_params["model"] == "gpt-5.6-atlas"
        assert turn_params["effort"] == "high"
        # No silent overwrite by the startup default (Terra/medium).
        assert turn_params["model"] != "gpt-5.6-terra"


# --- CT-08: unsupported model / effort never start a turn --------------------------------------


async def test_ct08_unsupported_model_and_effort_rejected(make_bridge, tmp_path):
    async with make_bridge({"turns": [NORMAL_TURN]}) as bridge:
        unknown_model = await bridge.call("codex", {"prompt": "x", "model": "gpt-nope"})
        assert unknown_model.is_error is True
        assert _meta(unknown_model)["code"] == "MODEL_UNAVAILABLE"

        bad_effort = await bridge.call(
            "codex",
            {"prompt": "x", "effort": "xhigh"},  # terra lacks xhigh
        )
        assert bad_effort.is_error is True
        assert _meta(bad_effort)["code"] == "UNSUPPORTED_EFFORT"

        # Catalog refresh happened exactly once for the unknown model.
        assert len(bridge.requests("model/list")) == 2
        # No thread or turn was started for rejected calls.
        assert bridge.requests("thread/start") == []
        assert bridge.requests("turn/start") == []


async def test_ct08_model_not_allowed(make_bridge, tmp_path):
    config = _test_config(
        tmp_path / "ws",
        policy=PolicyConfig(
            allowed_roots=(str(tmp_path / "ws"),), allowed_models=("gpt-5.6-terra",)
        ),
    )
    async with make_bridge({"turns": [NORMAL_TURN]}, config=config) as bridge:
        denied = await bridge.call("codex", {"prompt": "x", "model": "gpt-5.6-atlas"})
        assert denied.is_error is True
        assert _meta(denied)["code"] == "MODEL_NOT_ALLOWED"
        assert bridge.requests("turn/start") == []


# --- CT-09: config bypass attempts rejected -------------------------------------------------------


async def test_ct09_config_bypass_rejected(make_bridge):
    async with make_bridge({"turns": [NORMAL_TURN]}) as bridge:
        cases = [
            ({"prompt": "x", "config": {"sandbox": "danger-full-access"}}, "CONFIG_KEY_DENIED"),
            ({"prompt": "x", "config": {"approval_policy": "on-request"}}, "CONFIG_KEY_DENIED"),
            ({"prompt": "x", "config": {"mcp_servers": {}}}, "CONFIG_KEY_DENIED"),
            ({"prompt": "x", "approval-policy": "on-request"}, "UNSUPPORTED_APPROVAL_POLICY"),
            ({"prompt": "x", "sandbox": "danger-full-access"}, "VALIDATION_ERROR"),
        ]
        for arguments, code in cases:
            result = await bridge.call("codex", arguments)
            assert result.is_error is True, arguments
            assert _meta(result)["code"] == code, (arguments, _meta(result))
        assert bridge.requests("thread/start") == []
        assert bridge.requests("turn/start") == []


async def test_ct09_sandbox_policy_restriction(make_bridge, tmp_path):
    config = _test_config(
        tmp_path / "ws",
        policy=PolicyConfig(
            allowed_roots=(str(tmp_path / "ws"),),
            allowed_sandboxes=("read-only",),
        ),
    )
    async with make_bridge({"turns": [NORMAL_TURN]}, config=config) as bridge:
        result = await bridge.call("codex", {"prompt": "x", "sandbox": "workspace-write"})
        assert result.is_error is True
        assert _meta(result)["code"] == "WORKSPACE_DENIED"
        assert bridge.requests("turn/start") == []


async def test_ct09_workspace_outside_roots(make_bridge, tmp_path):
    async with make_bridge({"turns": [NORMAL_TURN]}) as bridge:
        result = await bridge.call("codex", {"prompt": "x", "cwd": "/etc"})
        assert result.is_error is True
        assert _meta(result)["code"] == "WORKSPACE_DENIED"
        assert bridge.requests("thread/start") == []


# --- CT-13: normal cancellation ----------------------------------------------------------------


async def test_ct13_cancellation_interrupts_target_turn(make_bridge):
    scenario = {
        "turns": [SLOW_TURN],
        "interrupt": {"notifyCompleted": True, "status": "interrupted", "delayMs": 100},
    }
    async with make_bridge(scenario) as bridge:
        task = asyncio.create_task(bridge.call("codex", {"prompt": "long one"}))
        # Wait until the turn is actually running before cancelling: with R5,
        # a cancellation that lands before submission correctly never starts
        # the turn, so the test must target the submitted case.
        await bridge.wait_for_turns(1, timeout=30)
        await asyncio.sleep(0.2)  # let the pump block on the long turn
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=15)

        # The cancelled request produced no further result; the target turn
        # was interrupted specifically.
        def has_interrupt(journal):
            return any(
                e.get("event") == "request" and e.get("method") == "turn/interrupt" for e in journal
            )

        deadline_iters = 0
        while not has_interrupt(bridge.journal()) and deadline_iters < 300:
            await asyncio.sleep(0.05)
            deadline_iters += 1
        assert has_interrupt(bridge.journal()), "turn/interrupt not sent"
        interrupts = bridge.requests("turn/interrupt")
        assert interrupts, "turn/interrupt not sent"
        # The turn ID the fake assigned is journaled on its turn/start
        # response; the interrupt must target exactly that turn.
        turn_ids = [
            e.get("turnId")
            for e in bridge.journal()
            if e.get("event") == "sent_response" and e.get("turnId")
        ]
        assert turn_ids, "no turn/start response journaled"
        assert interrupts[-1]["params"]["turnId"] == turn_ids[-1]
        # Only one turn ever started: no resend after cancellation.
        await asyncio.sleep(0.5)
        assert len(bridge.requests("turn/start")) == 1


# --- CT-17: concurrency limits -------------------------------------------------------------------


async def test_ct17_server_busy_for_second_call(make_bridge):
    scenario = {"turns": [SLOW_TURN]}
    async with make_bridge(scenario) as bridge:
        first = asyncio.create_task(bridge.call("codex", {"prompt": "first"}))
        await asyncio.sleep(0.25)
        second = await bridge.call("codex", {"prompt": "second"})
        assert second.is_error is True
        assert _meta(second)["code"] == "SERVER_BUSY"
        # No queueing: the second call returned immediately as an error.
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(first), timeout=15)


async def test_ct17_thread_busy_for_active_thread(make_bridge):
    scenario = {"turns": [SLOW_TURN]}
    async with make_bridge(scenario) as bridge:
        first = asyncio.create_task(bridge.call("codex", {"prompt": "running"}))
        await asyncio.sleep(0.25)
        thread_id = None

        def active_thread(events):
            return any(e.get("event") == "sent_response" and e.get("threadId") for e in events)

        await bridge.wait_for_turns(1)
        for e in bridge.journal():
            if e.get("event") == "sent_response" and e.get("threadId"):
                thread_id = e["threadId"]
        assert thread_id, "no thread started"
        # Reply to the SAME active thread distinguishes THREAD_BUSY from
        # SERVER_BUSY (design 8.3).
        reply = await bridge.call("codex-reply", {"prompt": "steer?", "threadId": thread_id})
        assert reply.is_error is True
        assert _meta(reply)["code"] == "THREAD_BUSY"
        # A call for a different target stays SERVER_BUSY.
        other = await bridge.call("codex", {"prompt": "another"})
        assert _meta(other)["code"] == "SERVER_BUSY"
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(first), timeout=15)
