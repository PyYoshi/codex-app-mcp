"""Fault-injection tests: CT-19 (output/memory), turn timeout."""

from __future__ import annotations

import pathlib
import tempfile

from codex_app_mcp.config import LimitsConfig
from tests.conftest import _test_config
from tests.contract.harness import FINAL_ANSWER_ITEM


def _command_item(index: int, output_size: int = 8192) -> dict:
    return {
        "type": "commandExecution",
        "id": f"cmd-{index}",
        "command": f"echo {index}",
        "commandActions": [],
        "cwd": "/tmp",
        "aggregatedOutput": "x" * output_size,
        "exitCode": 0,
        "status": "completed",
        "timedOut": False,
    }


async def test_ct19_command_output_not_accumulated(make_bridge):
    """Many large command outputs must not accumulate in bridge memory or
    leak into the final answer."""
    steps: list[dict] = [{"action": "response"}]
    for index in range(200):
        steps.append({"action": "itemCompleted", "item": _command_item(index)})
    steps.append({"action": "itemCompleted", "item": FINAL_ANSWER_ITEM})
    steps.append({"action": "turnCompleted", "status": "completed"})

    async with make_bridge({"turns": [{"steps": steps}]}) as bridge:
        result = await bridge.call("codex", {"prompt": "run lots of commands"})
        assert result.is_error is False
        text = result.content[0].text
        assert text == "レビュー結果です。"
        assert "xxxx" not in text


async def test_ct19_output_limit_exceeded_is_error_not_truncated(make_bridge):
    big_text = "あ" * 5000
    steps = [
        {"action": "response"},
        {
            "action": "itemCompleted",
            "item": {
                "type": "agentMessage",
                "id": "i",
                "text": big_text,
                "phase": "final_answer",
            },
        },
        {"action": "turnCompleted", "status": "completed"},
    ]
    async with make_bridge({"turns": [{"steps": steps}]}) as bridge:
        result = await bridge.call("codex", {"prompt": "big answer"})
        assert result.is_error is False  # 5000 chars < 1 MiB default

    with tempfile.TemporaryDirectory() as tmp:
        workspace = pathlib.Path(tmp) / "ws"
        workspace.mkdir()
        config = _test_config(workspace, limits=LimitsConfig(max_result_bytes=100))
        async with make_bridge({"turns": [{"steps": steps}]}, config=config) as bridge2:
            result = await bridge2.call("codex", {"prompt": "big answer"})
            assert result.is_error is True
            meta = result.meta["codex-app-mcp"]
            assert meta["code"] == "OUTPUT_LIMIT_EXCEEDED"
            assert meta["mayHaveSideEffects"] is True
            assert "あ" not in result.content[0].text


async def test_turn_timeout_interrupts_and_reports(make_bridge):
    """Turn timeout: interrupt is initiated, error carries side-effect note,
    and the turn is never resent."""
    steps = [
        {"action": "response"},
        {"action": "sleep", "ms": 60000},
        {"action": "turnCompleted", "status": "completed"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        workspace = pathlib.Path(tmp) / "ws"
        workspace.mkdir()
        config = _test_config(
            workspace,
            limits=LimitsConfig(
                turn_timeout_seconds=2.0,
                interrupt_grace_seconds=3.0,
                rpc_timeout_seconds=10.0,
            ),
        )
        async with make_bridge(
            {"turns": [{"steps": steps}], "interrupt": {"notifyCompleted": True}},
            config=config,
        ) as bridge:
            result = await bridge.call("codex", {"prompt": "run forever"})
            assert result.is_error is True
            meta = result.meta["codex-app-mcp"]
            assert meta["code"] == "TURN_TIMEOUT"
            assert meta["mayHaveSideEffects"] is True
            await bridge.wait_for_turns(1)
            assert bridge.requests("turn/interrupt"), "no interrupt on timeout"
            assert len(bridge.requests("turn/start")) == 1
