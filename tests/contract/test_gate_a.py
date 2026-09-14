"""Contract tests for the SDK adapter (design section 15, phase A gate).

Gate A: CT-10, CT-11, CT-12, CT-14, CT-15, CT-16 against the fake App Server.
The MCP layer is not built yet — these tests prove the adapter foundation.
"""

from __future__ import annotations

import asyncio

import pytest

from codex_app_mcp.backend.interface import (
    RUNTIME_DEGRADED,
    RUNTIME_READY,
    StartThreadRequest,
    StartTurnRequest,
)
from codex_app_mcp.errors import (
    RUNTIME_DISCONNECTED,
    STARTUP_TIMEOUT,
    UNSUPPORTED_SERVER_REQUEST,
    BridgeError,
)

from .harness import (
    COMMENTARY_ITEM,
    FINAL_ANSWER_ITEM,
    NO_PHASE_ITEM,
    FakeRuntime,
)


async def _start_thread(rt: FakeRuntime):
    return await rt.backend.start_thread(
        StartThreadRequest(
            cwd="/tmp",
            model="gpt-5.6-terra",
            effort="medium",
            sandbox_mode="read-only",
            approval_policy="never",
        )
    )


async def _collect_turn(rt: FakeRuntime, turn_id: str, *, max_events: int = 50):
    """Consume events for a turn until turn/completed or transport failure."""
    events = []
    for _ in range(max_events):
        event = await rt.backend.next_turn_event(turn_id)
        events.append(event)
        if event.method == "turn/completed":
            return events
    raise AssertionError("turn/completed not seen within event budget")


# --- startup / handshake ------------------------------------------------------


async def test_initialize_handshake_and_single_initialized(make_runtime):
    rt = await make_runtime({"turns": []})
    assert rt.backend.state == RUNTIME_READY
    assert rt.backend.generation == 1
    # Exactly one `initialized` notification from the SDK; the bridge adds none.
    initialized = [e for e in rt.events("notification") if e["method"] == "initialized"]
    assert len(initialized) == 1
    await rt.backend.start_runtime()
    assert rt.backend.generation == 1  # idempotent reuse


async def test_start_runtime_startup_timeout(make_runtime):
    rt = await make_runtime({"turns": []}, startup_timeout=0.05, start=False)
    with pytest.raises(BridgeError) as excinfo:
        await rt.backend.start_runtime()
    assert excinfo.value.code == STARTUP_TIMEOUT


# --- CT-10: approval callback never accepts ------------------------------------


@pytest.mark.parametrize("kind", ["commandExecution", "fileChange"])
async def test_ct10_approval_requests_are_declined(make_runtime, kind):
    rt = await make_runtime(
        {
            "turns": [
                {
                    "steps": [
                        {"action": "response"},
                        {"action": "approvalRequest", "kind": kind, "wait": True},
                        {"action": "itemCompleted", "item": FINAL_ANSWER_ITEM},
                        {"action": "turnCompleted", "status": "completed"},
                    ]
                }
            ]
        }
    )
    thread = await _start_thread(rt)
    receipt = await rt.backend.start_turn(
        StartTurnRequest(thread_id=thread.thread_id, prompt="do something risky")
    )
    events = await _collect_turn(rt, receipt.turn_id)
    assert events[-1].method == "turn/completed"

    approvals = [
        e for e in rt.events("client_response") if e["method"] == f"item/{kind}/requestApproval"
    ]
    assert approvals, "approval response missing from journal"
    for entry in approvals:
        assert entry["result"] == {"decision": "decline"}, entry
    # No accept decision ever sent for any server request.
    for entry in rt.events("client_response"):
        result = entry["result"]
        assert not (isinstance(result, dict) and result.get("decision") == "accept")


# --- CT-11: unknown server request stops the runtime ----------------------------


async def test_ct11_unknown_request_fails_waits_and_degrades(make_runtime):
    rt = await make_runtime(
        {
            "turns": [
                {
                    "steps": [
                        {"action": "response"},
                        {
                            "action": "unknownRequest",
                            "method": "item/someNewPermission/requestApproval",
                        },
                        {"action": "itemCompleted", "item": FINAL_ANSWER_ITEM},
                        {"action": "turnCompleted", "status": "completed"},
                    ]
                }
            ]
        }
    )
    thread = await _start_thread(rt)
    receipt = await rt.backend.start_turn(
        StartTurnRequest(thread_id=thread.thread_id, prompt="trigger unknown request")
    )
    with pytest.raises(BridgeError) as excinfo:
        await _collect_turn(rt, receipt.turn_id)
    assert excinfo.value.code == UNSUPPORTED_SERVER_REQUEST
    assert rt.backend.state == RUNTIME_DEGRADED
    assert rt.backend.last_unsupported_request is not None
    assert rt.backend.last_unsupported_request.method == "item/someNewPermission/requestApproval"
    # No fabricated success response was sent to the unknown request.
    responses = [e for e in rt.events("client_response") if "someNewPermission" in e["method"]]
    assert responses == []
    # Subsequent operations fail fast instead of hanging.
    with pytest.raises(BridgeError):
        await rt.backend.start_turn(
            StartTurnRequest(thread_id=thread.thread_id, prompt="second try")
        )
    # close() still works from the degraded state.
    await asyncio.wait_for(rt.backend.close(), timeout=10)


# --- CT-12: early notifications before turn/start response -----------------------


async def test_ct12_early_notifications_are_not_lost(make_runtime):
    rt = await make_runtime(
        {
            "turns": [
                {
                    "steps": [
                        # Notifications are emitted BEFORE the turn/start
                        # response is written.
                        {"action": "turnStarted"},
                        {"action": "itemCompleted", "item": COMMENTARY_ITEM},
                        {"action": "itemCompleted", "item": FINAL_ANSWER_ITEM},
                        {"action": "turnCompleted", "status": "completed"},
                        {"action": "response"},
                    ]
                }
            ]
        }
    )
    thread = await _start_thread(rt)
    receipt = await rt.backend.start_turn(
        StartTurnRequest(thread_id=thread.thread_id, prompt="early completion")
    )
    events = await _collect_turn(rt, receipt.turn_id)
    methods = [e.method for e in events]
    assert "turn/completed" in methods
    assert "item/completed" in methods
    completed_items = [e for e in events if e.method == "item/completed"]
    assert any(
        getattr(getattr(e.payload, "item", None), "root", None) is not None or e.payload is not None
        for e in completed_items
    )


# --- CT-14: cancel during turn start, late receipt, targeted interrupt -----------


async def test_ct14_supervisor_keeps_start_and_interrupts_late_turn(make_runtime):
    rt = await make_runtime(
        {
            "turns": [
                {
                    "steps": [
                        {"action": "sleep", "ms": 700},
                        {"action": "response"},
                        {"action": "itemCompleted", "item": FINAL_ANSWER_ITEM},
                        {"action": "sleep", "ms": 5000},
                        {"action": "turnCompleted", "status": "completed"},
                    ]
                }
            ]
        }
    )
    thread = await _start_thread(rt)
    # The supervisor owns the start operation, not the cancelled handler:
    # shield the start and cancel only the outer (request-handler) await.
    start_task = asyncio.create_task(
        rt.backend.start_turn(StartTurnRequest(thread_id=thread.thread_id, prompt="slow start"))
    )
    outer = asyncio.ensure_future(asyncio.shield(start_task))
    await asyncio.sleep(0.15)
    assert not start_task.done()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert not start_task.done(), "handler cancellation must not abort the start op"

    # Supervisor still obtains the late receipt (response arrives at ~0.7s).
    receipt = await asyncio.wait_for(start_task, timeout=10)
    assert receipt.turn_id

    # The late-arriving turn ID is interrupted specifically.
    await asyncio.wait_for(rt.backend.interrupt_turn(thread.thread_id, receipt.turn_id), timeout=10)
    await rt.wait_for(
        lambda events: any(
            e.get("event") == "request" and e.get("method") == "turn/interrupt" for e in events
        )
    )
    interrupts = [e for e in rt.events("request") if e["method"] == "turn/interrupt"]
    assert interrupts[-1]["params"]["turnId"] == receipt.turn_id
    # Drain events so the turn route completes (interrupt triggers completion).
    events = await _collect_turn(rt, receipt.turn_id)
    assert events[-1].method == "turn/completed"


# --- CT-15: control ops responsive while notification wait blocks ----------------


async def test_ct15_interrupt_works_while_event_pump_blocks(make_runtime):
    rt = await make_runtime(
        {
            "turns": [
                {
                    "steps": [
                        {"action": "response"},
                        {"action": "sleep", "ms": 60000},
                        {"action": "turnCompleted", "status": "completed"},
                    ]
                }
            ],
            "interrupt": {"notifyCompleted": True, "status": "interrupted"},
        }
    )
    thread = await _start_thread(rt)
    receipt = await rt.backend.start_turn(
        StartTurnRequest(thread_id=thread.thread_id, prompt="long turn")
    )
    # Event pump blocks in the notification executor.
    pump = asyncio.create_task(_collect_turn(rt, receipt.turn_id))
    await asyncio.sleep(0.2)
    assert not pump.done()
    # interrupt must complete while the pump is blocked (worker separation).
    await asyncio.wait_for(rt.backend.interrupt_turn(thread.thread_id, receipt.turn_id), timeout=5)
    # Fake notifies turn/completed on interrupt; pump finishes.
    events = await asyncio.wait_for(pump, timeout=10)
    assert events[-1].method == "turn/completed"


# --- CT-16: runtime abnormal termination releases all waits ----------------------


async def test_ct16_crash_fails_pending_waits_within_bound(make_runtime):
    rt = await make_runtime(
        {
            "turns": [
                {
                    "steps": [
                        {"action": "response"},
                        {"action": "sleep", "ms": 200},
                        {"action": "crash", "code": 1},
                    ]
                }
            ]
        }
    )
    thread = await _start_thread(rt)
    receipt = await rt.backend.start_turn(
        StartTurnRequest(thread_id=thread.thread_id, prompt="about to crash")
    )
    with pytest.raises(BridgeError) as excinfo:
        await asyncio.wait_for(_collect_turn(rt, receipt.turn_id), timeout=15)
    assert excinfo.value.code in {RUNTIME_DISCONNECTED, UNSUPPORTED_SERVER_REQUEST}
    assert rt.backend.state == RUNTIME_DEGRADED
    # Further RPCs fail instead of hanging; no turn auto-resend happens.
    requests_before = len([e for e in rt.events("request") if e["method"] == "turn/start"])
    with pytest.raises(BridgeError):
        await rt.backend.start_turn(
            StartTurnRequest(thread_id=thread.thread_id, prompt="after crash")
        )
    requests_after = len([e for e in rt.events("request") if e["method"] == "turn/start"])
    assert requests_after == requests_before
    # close() completes in bounded time even after the crash.
    await asyncio.wait_for(rt.backend.close(), timeout=10)


# --- result aggregation primitives at the event level ---------------------------


async def test_event_stream_carries_final_answer_items(make_runtime):
    rt = await make_runtime(
        {
            "turns": [
                {
                    "steps": [
                        {"action": "response"},
                        {"action": "itemCompleted", "item": NO_PHASE_ITEM},
                        {"action": "itemCompleted", "item": FINAL_ANSWER_ITEM},
                        {"action": "turnCompleted", "status": "completed"},
                    ]
                }
            ]
        }
    )
    thread = await _start_thread(rt)
    receipt = await rt.backend.start_turn(
        StartTurnRequest(thread_id=thread.thread_id, prompt="aggregate")
    )
    events = await _collect_turn(rt, receipt.turn_id)
    payloads = [e.payload for e in events if e.method == "item/completed"]
    texts = []
    for payload in payloads:
        item = getattr(payload, "item", None)
        root = getattr(item, "root", item) if item is not None else None
        if root is not None and getattr(root, "type", None) == "agentMessage":
            texts.append((getattr(root, "phase", None), getattr(root, "text", "")))
    assert any(phase is not None and phase.value == "final_answer" for phase, _ in texts)
    assert any(phase is None for phase, _ in texts)
