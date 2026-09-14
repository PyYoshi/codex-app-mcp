"""Follow-up review probes. No SDK, authentication, network, or real inference.

Assertions express required safe behavior; failing tests document remaining defects.
Run with PYTHONPATH=<submitted-project>/src and pytest (see README).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from codex_app_mcp.contracts import CodexCall, CodexReplyCall
from codex_app_mcp.coordinator import ExecutionCoordinator
from codex_app_mcp.errors import BridgeError

from .previous_review_support import MODEL, OTHER, FakeBackend, entry, setup


def coordinator_for(backend, config, registry):
    return ExecutionCoordinator(
        lambda _: backend, config, bridge_cwd=backend.cwd, registry=registry
    )


async def finish(coordinator, backend):
    # Explicit test-fixture cleanup, independent of the defective production path.
    # Observations/assertions are made BEFORE cleanup.
    await FakeBackend.close(backend)
    with suppress(Exception, asyncio.CancelledError):
        await coordinator.aclose()


async def eventually(predicate, timeout=1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("cache", ["cold", "warm_missing"])
def test_F1_runtime_resolved_model_must_pass_allowlist(tmp_path, cache):
    async def scenario():
        _, backend, config, registry = setup(tmp_path)
        config = replace(
            config,
            defaults=replace(config.defaults, model=None, effort=None),
            policy=replace(config.policy, allowed_models=(MODEL,)),
        )
        coordinator = coordinator_for(backend, config, registry)
        backend.runtime_config_model = OTHER
        if cache == "warm_missing":
            backend.models = [entry(MODEL, True)]
            await coordinator._get_catalog(backend)
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert result["isError"] and not backend.turn_calls, (
                f"cache={cache}, allowed={config.policy.allowed_models}, "
                f"executed={[t.model for t in backend.turn_calls]}, "
                f"model/list={backend.list_calls}, isError={result['isError']}"
            )
        finally:
            await finish(coordinator, backend)

    asyncio.run(scenario())


def test_F1_runtime_resolved_model_effort_must_be_validated(tmp_path):
    async def scenario():
        _, backend, config, registry = setup(tmp_path)
        config = replace(config, defaults=replace(config.defaults, model=None, effort="xhigh"))
        coordinator = coordinator_for(backend, config, registry)
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert result["isError"] and not backend.turn_calls, (
                f"supported={backend.models[1].supported_reasoning_efforts}, "
                f"executed={[t.effort for t in backend.turn_calls]}, model/list={backend.list_calls}"
            )
        finally:
            await finish(coordinator, backend)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["ignore_interrupt", "start_timeout"])
def test_F2_close_failure_must_not_claim_stopped_and_release_lease(tmp_path, mode):
    async def scenario():
        coordinator, backend, _, registry = setup(tmp_path, sandbox="workspace-write")
        backend.mode = mode

        async def broken_close():
            backend.close_calls += 1
            raise OSError("injected runtime termination failure")

        backend.close = broken_close
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            run = coordinator._active
            lock_free = False
            try:
                with registry.workspace_lock(backend.cwd):
                    lock_free = True
            except BridgeError:
                pass
            details = (
                f"mode={mode}, running={backend.running}, "
                f"runtime_stop_assured={run.runtime_stop_assured}, "
                f"slot_free={run.terminal_event.is_set()}, lock_free={lock_free}, "
                f"accepts_new={not coordinator._closed}, result={result['_meta']}"
            )
            assert not backend.running or (
                not run.runtime_stop_assured and not run.terminal_event.is_set() and not lock_free
            ), details
        finally:
            backend.close = lambda: FakeBackend.close(backend)
            await finish(coordinator, backend)

    asyncio.run(scenario())


def test_F2_interrupt_timeout_must_still_force_stop(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        backend.mode = "ignore_interrupt"

        async def hanging_interrupt(thread_id, turn_id):
            backend.interrupts.append((thread_id, turn_id))
            await asyncio.Event().wait()

        backend.interrupt_turn = hanging_interrupt
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert not backend.running, (
                f"code={result['_meta']['codex-app-mcp']['code']}, "
                f"close_calls={backend.close_calls}, running={backend.running}, "
                f"slot_free={coordinator._active.terminal_event.is_set()}"
            )
        finally:
            await finish(coordinator, backend)

    asyncio.run(scenario())


def test_F3_pre_submission_cancel_cleanup_must_not_kill_next_run(tmp_path):
    async def scenario():
        _, backend, config, registry = setup(tmp_path)
        config = replace(
            config,
            limits=replace(config.limits, turn_timeout_seconds=2, interrupt_grace_seconds=0.12),
        )
        coordinator = coordinator_for(backend, config, registry)
        backend.catalog_gate = asyncio.Event()
        first = asyncio.create_task(coordinator.run_codex(CodexCall(prompt="cancel before send")))
        second = None
        try:
            await backend.catalog_entered.wait()
            old_run = coordinator._active
            first.cancel()
            await eventually(lambda: old_run.cancellation_requested)
            backend.catalog_gate.set()
            await eventually(lambda: old_run.terminal_event.is_set())
            assert not backend.turn_calls, "control: first turn must never have been submitted"

            # A new call is accepted while the cancelled run's cleanup is still pending.
            backend.mode = "ignore_interrupt"
            second = asyncio.create_task(coordinator.run_codex(CodexCall(prompt="next call")))
            await eventually(lambda: bool(backend.turn_calls))
            with suppress(asyncio.CancelledError):
                await first
            closes_before_completion = backend.close_calls
            # Complete only after the previous call's cancellation cleanup has settled.
            backend.complete("thread-1", "turn-1")
            result = await asyncio.wait_for(second, timeout=1)
            assert closes_before_completion == 0 and not result["isError"], (
                f"old_cleanup_closed_runtime={closes_before_completion}, "
                f"next_isError={result['isError']}, next_meta={result['_meta']}"
            )
        finally:
            backend.catalog_gate.set()
            await finish(coordinator, backend)
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(Exception, asyncio.CancelledError):
                        await task

    asyncio.run(scenario())


def test_F4_malformed_terminal_must_not_release_a_running_turn(tmp_path):
    async def scenario():
        coordinator, backend, _, registry = setup(tmp_path, sandbox="workspace-write")
        backend.mode = "malformed_terminal"

        # Unlike the original fake, a malformed message is NOT evidence of stopped work.
        async def next_event(turn_id):
            event = await backend.queues[turn_id].get()
            if isinstance(event, BaseException):
                raise event
            return event

        backend.next_turn_event = next_event
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            run = coordinator._active
            lock_free = False
            try:
                with registry.workspace_lock(backend.cwd):
                    lock_free = True
            except BridgeError:
                pass
            assert not backend.running or not lock_free, (
                f"code={result['_meta']['codex-app-mcp']['code']}, "
                f"confirmed={run.turn_terminal_confirmed}, "
                f"terminal_event={run.turn_terminal_event.is_set()}, close_calls={backend.close_calls}, "
                f"running={backend.running}, lock_free={lock_free}"
            )
        finally:
            await finish(coordinator, backend)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["inProgress", "unknown-status"])
def test_F4_only_completed_status_can_produce_success(tmp_path, status):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        original_complete = backend.complete
        backend.complete = lambda thread_id, turn_id: original_complete(thread_id, turn_id, status)
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert result["isError"], f"nonterminal/unknown status returned success: {result}"
        finally:
            await finish(coordinator, backend)

    asyncio.run(scenario())


def test_F5_cached_resume_cwd_must_match_the_held_workspace_lock(tmp_path):
    async def scenario():
        _, backend, config, registry = setup(tmp_path, sandbox="workspace-write")
        other = tmp_path / "other-workspace"
        other.mkdir()
        config = replace(config, policy=replace(config.policy, allowed_roots=(str(tmp_path),)))
        coordinator = coordinator_for(backend, config, registry)
        try:
            first = await coordinator.run_codex(CodexCall(prompt="create known thread"))
            assert not first["isError"]
            thread_id = first["structuredContent"]["threadId"]
            backend.resume_cwd = str(other)
            calls_before = len(backend.turn_calls)
            with registry.workspace_lock(str(other)):
                result = await coordinator.run_reply(
                    CodexReplyCall(prompt="resume", thread_id=thread_id)
                )
            assert result["isError"] and len(backend.turn_calls) == calls_before, (
                f"cached_cwd={backend.cwd}, resumed_cwd={other}, "
                f"other_workspace_locked=True, new_turns={len(backend.turn_calls) - calls_before}, "
                f"isError={result['isError']}"
            )
        finally:
            await finish(coordinator, backend)

    asyncio.run(scenario())


def test_control_explicit_model_still_enforces_allowlist(tmp_path):
    async def scenario():
        _, backend, config, registry = setup(tmp_path)
        config = replace(config, policy=replace(config.policy, allowed_models=(MODEL,)))
        coordinator = coordinator_for(backend, config, registry)
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test", model=OTHER))
            assert result["isError"] and not backend.turn_calls
        finally:
            await finish(coordinator, backend)

    asyncio.run(scenario())


def test_control_confirmed_completion_succeeds(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert not result["isError"] and not backend.running
        finally:
            await finish(coordinator, backend)

    asyncio.run(scenario())


def test_F2_actual_adapter_close_must_not_mark_failed_client_as_stopped():
    """Execute the uploaded close() method verbatim via AST; no Codex SDK is imported."""
    import ast
    import logging
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import codex_app_mcp

    path = Path(codex_app_mcp.__file__).parent / "backend/codex_sdk.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CodexSdkBackend")
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "close")
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace = {
        "asyncio": asyncio,
        "RUNTIME_STOPPING": "STOPPING",
        "RUNTIME_STOPPED": "STOPPED",
        "_logger": logging.getLogger("review.adapter"),
    }
    exec(compile(module, str(path), "exec"), namespace)

    async def scenario():
        class FailedClient:
            calls = 0

            def close(self):
                self.calls += 1
                raise BrokenPipeError("injected SDK client close failure")

        client = FailedClient()
        adapter = NS(
            _close_lock=asyncio.Lock(),
            _state="READY",
            _drain_task=None,
            _adopt_lock=threading.Lock(),
            _start_aborted=False,
            _client=client,
            _pending_start_client=None,
            _control_exec=ThreadPoolExecutor(max_workers=1),
            _notif_exec=ThreadPoolExecutor(max_workers=1),
            _closed_executors=False,
        )
        error = None
        try:
            try:
                await namespace["close"](adapter)
            except Exception as exc:
                error = exc
            assert adapter._state != "STOPPED" and error is not None, (
                f"SDK.close calls={client.calls}, returned_error={error}, "
                f"adapter_state={adapter._state}, client_ref={adapter._client}"
            )
        finally:
            adapter._control_exec.shutdown(wait=True)
            adapter._notif_exec.shutdown(wait=True)

    asyncio.run(scenario())
