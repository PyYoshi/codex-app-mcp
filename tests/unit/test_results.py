"""Unit tests: reducer and envelopes (design 5.3, 7.3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from codex_app_mcp.errors import (
    OUTPUT_LIMIT_EXCEEDED,
    TURN_FAILED,
    BridgeError,
)
from codex_app_mcp.results import (
    TurnResultReducer,
    build_error_envelope,
    build_success_envelope,
)


@dataclass
class _Item:
    type: str
    text: str | None = None
    phase: str | None = None


@dataclass
class _ItemCompleted:
    item: Any


@dataclass
class _Turn:
    id: str
    status: str
    error: Any = None


@dataclass
class _TurnCompleted:
    thread_id: str
    turn: _Turn


@dataclass
class _ErrorPayload:
    error: Any
    will_retry: bool


def _observe_sequence(reducer: TurnResultReducer, items: list[_Item]) -> None:
    for item in items:
        wrapped = _ItemCompleted(item=item)

        # results.py unwraps .item.root via getattr; plain dataclass has no .root
        class _Root:
            pass

        object.__setattr__(wrapped, "item", _Wrap(item))
        reducer.observe("item/completed", wrapped)


@dataclass
class _Wrap:
    root: Any


def test_final_answer_preferred_over_fallback():
    reducer = TurnResultReducer()
    _observe_sequence(
        reducer,
        [
            _Item("agentMessage", "途中のコメント", "commentary"),
            _Item("agentMessage", "phase 未指定", None),
            _Item("agentMessage", "最終回答", "final_answer"),
        ],
    )
    reducer.observe("turn/completed", _TurnCompleted("th", _Turn("t1", "completed")))
    outcome = reducer.finalize()
    assert outcome.final_text == "最終回答"
    assert outcome.status == "completed"


def test_fallback_when_no_final_answer():
    reducer = TurnResultReducer()
    _observe_sequence(
        reducer,
        [
            _Item("agentMessage", "1 つ目", None),
            _Item("agentMessage", "2 つ目", None),
        ],
    )
    reducer.observe("turn/completed", _TurnCompleted("th", _Turn("t1", "completed")))
    # SDK scans from the end and keeps the last phase-unspecified message.
    assert reducer.finalize().final_text == "2 つ目"


def test_commentary_and_command_output_excluded():
    reducer = TurnResultReducer()
    _observe_sequence(
        reducer,
        [
            _Item("commandExecution"),
            _Item("agentMessage", "commentary text", "commentary"),
            _Item("reasoning"),
        ],
    )
    reducer.observe("turn/completed", _TurnCompleted("th", _Turn("t1", "completed")))
    outcome = reducer.finalize()
    assert outcome.final_text is None


def test_failed_turn_raises():
    reducer = TurnResultReducer()
    reducer.observe(
        "turn/completed",
        _TurnCompleted("th", _Turn("t1", "failed", error=_Msg("boom"))),
    )
    with pytest.raises(BridgeError) as excinfo:
        reducer.finalize()
    assert excinfo.value.code == TURN_FAILED
    assert "boom" in excinfo.value.message


@dataclass
class _Msg:
    message: str


def test_error_notification_recorded_not_terminal():
    reducer = TurnResultReducer()
    reducer.observe("error", _ErrorPayload(error=_Msg("transient"), will_retry=True))
    reducer.observe("turn/completed", _TurnCompleted("th", _Turn("t1", "completed")))
    outcome = reducer.finalize()
    assert outcome.last_error_notification == "transient"
    assert outcome.last_retryable_error is True
    assert outcome.status == "completed"


# --- envelopes -------------------------------------------------------------------


def test_success_envelope_shape():
    env = build_success_envelope(
        thread_id="th-1",
        text="レビュー結果です。",
        turn_id="turn-9",
        status="completed",
        requested_model="gpt-5.6-terra",
        requested_effort="medium",
        max_result_bytes=1024 * 1024,
    )
    assert env["content"] == [
        {"type": "text", "text": "レビュー結果です。"},
        {
            "type": "text",
            "text": '{"threadId": "th-1", "content": "レビュー結果です。"}',
        },
    ]
    assert env["structuredContent"] == {"threadId": "th-1", "content": "レビュー結果です。"}
    assert env["isError"] is False
    assert env["_meta"]["codex-app-mcp"] == {
        "turnId": "turn-9",
        "status": "completed",
        "requestedModel": "gpt-5.6-terra",
        "requestedEffort": "medium",
    }


def test_success_envelope_rejects_oversized_without_truncating():
    with pytest.raises(BridgeError) as excinfo:
        build_success_envelope(
            thread_id="th",
            text="あ" * 100,
            turn_id="t",
            status="completed",
            requested_model=None,
            requested_effort=None,
            max_result_bytes=50,
        )
    assert excinfo.value.code == OUTPUT_LIMIT_EXCEEDED
    assert excinfo.value.may_have_side_effects is True


def test_error_envelope_shape():
    error = BridgeError(
        code="THREAD_BUSY",
        message="thread is busy",
        retryable=True,
        thread_id="th-1",
        turn_id="t-1",
    )
    env = build_error_envelope(error)
    assert env["isError"] is True
    assert env["structuredContent"] == {"threadId": "th-1", "content": "thread is busy"}
    assert env["content"][1]["text"] == ('{"content": "thread is busy", "threadId": "th-1"}')
    meta = env["_meta"]["codex-app-mcp"]
    assert meta["code"] == "THREAD_BUSY"
    assert meta["retryable"] is True
    assert meta["turnId"] == "t-1"
    assert "mayHaveSideEffects" in meta


def test_success_envelope_conforms_to_output_schema():
    """The packaged output schema requires the emitted threadId."""
    import json

    from codex_app_mcp.server import TOOLS_SCHEMA_PATH

    schema_path = TOOLS_SCHEMA_PATH
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    output_schema = next(t["outputSchema"] for t in schema["tools"] if t["name"] == "codex")
    env = build_success_envelope(
        thread_id="th",
        text="ok",
        turn_id="t",
        status="completed",
        requested_model=None,
        requested_effort=None,
        max_result_bytes=1024,
    )
    structured = env["structuredContent"]
    for key in output_schema["required"]:
        assert key in structured, f"missing required output field: {key}"
    # A missing threadId would violate the schema (negative check).
    broken = {k: v for k, v in structured.items() if k != "threadId"}
    assert any(key not in broken for key in output_schema["required"])


def test_error_envelope_without_thread_id_does_not_fabricate():
    env = build_error_envelope(BridgeError(code="VALIDATION_ERROR", message="bad"))
    assert "threadId" not in env["structuredContent"]
    assert env["content"][1]["text"] == '{"content": "bad"}'
    assert "threadId" not in env["_meta"]["codex-app-mcp"]
