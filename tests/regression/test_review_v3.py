"""Third-round boundary tests against unmodified submitted source.

No SDK installation, real inference, account, network or runtime process is used.
Adapter/shutdown methods are extracted verbatim with AST; only dependencies are faked.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import MethodType
from types import SimpleNamespace as NS

import pytest

import codex_app_mcp
from codex_app_mcp.backend.interface import RuntimeInfo
from codex_app_mcp.contracts import CodexCall
from codex_app_mcp.coordinator import ExecutionCoordinator
from codex_app_mcp.errors import BridgeError

from .previous_review_support import FakeBackend, setup

ROOT = Path(codex_app_mcp.__file__).parent


async def eventually(predicate, timeout=1):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


def extract_method(relative, class_name, method_name, namespace):
    path = ROOT / relative
    tree = ast.parse(path.read_text())
    cls = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == class_name)
    method = next(
        x
        for x in cls.body
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == method_name
    )
    module = ast.fix_missing_locations(
        ast.Module(
            body=[
                ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                method,
            ],
            type_ignores=[],
        )
    )
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[method_name]


def adapter_for(client=None, pending=None, state="READY"):
    from codex_app_mcp.backend import interface

    namespace = {
        "asyncio": asyncio,
        "_logger": logging.getLogger("review.adapter"),
        "BridgeError": BridgeError,
        "RuntimeInfo": RuntimeInfo,
    }
    namespace.update(
        {
            k: getattr(interface, k)
            for k in dir(interface)
            if k.startswith("RUNTIME_") or k == "RuntimeUnavailableError"
        }
    )
    namespace["RUNTIME_DISCONNECTED"] = "RUNTIME_DISCONNECTED"
    namespace["STARTUP_TIMEOUT"] = "STARTUP_TIMEOUT"
    adapter = NS(
        _close_lock=asyncio.Lock(),
        _start_lock=asyncio.Lock(),
        _state=state,
        _drain_task=None,
        _adopt_lock=threading.Lock(),
        _start_aborted=False,
        _client=client,
        _pending_start_client=pending,
        _generation=0,
        _runtime_info=None,
        _unsupported=None,
        _options=NS(startup_timeout=0.1),
        _control_exec=ThreadPoolExecutor(max_workers=2),
        _notif_exec=ThreadPoolExecutor(max_workers=1),
        _closed_executors=False,
        _start_global_drain=lambda: None,
        _map_error=lambda exc, **kw: BridgeError(code=kw["default_code"], message=str(exc)),
    )
    for name in ("close", "start_runtime"):
        fn = extract_method("backend/codex_sdk.py", "CodexSdkBackend", name, namespace)
        setattr(adapter, name, MethodType(fn, adapter))
    return adapter


def dispose(adapter):
    adapter._control_exec.shutdown(wait=True)
    adapter._notif_exec.shutdown(wait=True)


class BrokenClient:
    def __init__(self):
        self.calls = 0
        self.alive = True

    def close(self):
        self.calls += 1
        raise OSError("injected close failure; simulated runtime remains alive")


def test_G1_inflight_cancel_cleanup_must_not_stop_replacement_runtime(tmp_path):
    async def scenario():
        _, old, config, registry = setup(tmp_path)
        new = FakeBackend(Path(old.cwd), mode="ignore_interrupt")
        config = replace(
            config,
            limits=replace(config.limits, turn_timeout_seconds=2, interrupt_grace_seconds=0.1),
        )
        pending = [old, new]
        coordinator = ExecutionCoordinator(
            lambda _: pending.pop(0), config, bridge_cwd=old.cwd, registry=registry
        )
        submitted = asyncio.Event()
        release_ack = asyncio.Event()
        entered_cleanup = asyncio.Event()
        old.mode = "ignore_interrupt"
        original_start = old.start_turn

        async def lost_ack(request):
            await original_start(request)
            submitted.set()
            await release_ack.wait()
            raise BridgeError(code="RPC_TIMEOUT", message="injected lost start ACK")

        old.start_turn = lost_ack
        original_wait = coordinator._wait_terminal_or_stop

        async def observed_wait(run, grace):
            entered_cleanup.set()
            await original_wait(run, grace)

        coordinator._wait_terminal_or_stop = observed_wait
        first = second = None
        try:
            first = asyncio.create_task(coordinator.run_codex(CodexCall(prompt="A")))
            await submitted.wait()
            run_a = coordinator._active
            first.cancel()
            await entered_cleanup.wait()
            release_ack.set()
            await eventually(lambda: run_a.terminal_event.is_set())
            assert run_a.runtime_stop_assured and old.close_calls >= 1

            second = asyncio.create_task(coordinator.run_codex(CodexCall(prompt="B")))
            await eventually(lambda: len(new.turn_calls) == 1)
            with suppress(asyncio.CancelledError):
                await first
            close_calls_before_B_completion = new.close_calls
            if new.state == "READY":
                new.complete("thread-1", "turn-1")
            result = await asyncio.wait_for(second, timeout=1)
            print(
                {
                    "old_stop_confirmed": run_a.runtime_stop_assured,
                    "replacement_close_before_completion": close_calls_before_B_completion,
                    "B_error": result["isError"],
                    "B_meta": result.get("_meta"),
                }
            )
            assert close_calls_before_B_completion == 0 and not result["isError"]
        finally:
            release_ack.set()
            await FakeBackend.close(old)
            await FakeBackend.close(new)
            with suppress(Exception, asyncio.CancelledError):
                await coordinator.aclose()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(Exception, asyncio.CancelledError):
                        await task

    asyncio.run(scenario())


def test_G2_failed_pending_close_must_keep_pending_owner():
    async def scenario():
        pending = BrokenClient()
        adapter = adapter_for(pending=pending, state="STARTING")
        try:
            with pytest.raises(RuntimeError):
                await adapter.close()
            print(
                {
                    "state": adapter._state,
                    "pending_preserved": adapter._pending_start_client is pending,
                    "active_preserved": adapter._client is pending,
                    "alive": pending.alive,
                }
            )
            assert adapter._pending_start_client is pending or adapter._client is pending
        finally:
            dispose(adapter)

    asyncio.run(scenario())


def test_G2_second_close_must_not_claim_success_after_pending_reference_loss():
    async def scenario():
        pending = BrokenClient()
        adapter = adapter_for(pending=pending, state="STARTING")
        try:
            with pytest.raises(RuntimeError):
                await adapter.close()
            with suppress(Exception):
                await adapter.close()
            print(
                {
                    "state_after_second_close": adapter._state,
                    "sdk_close_calls": pending.calls,
                    "alive": pending.alive,
                }
            )
            assert adapter._state != "STOPPED", (
                "no successful close was performed on the still-alive pending client"
            )
        finally:
            dispose(adapter)

    asyncio.run(scenario())


@pytest.mark.parametrize("initial_state", ["DEGRADED", "STOP_FAILED"])
def test_G2_start_must_not_replace_client_when_old_close_is_unconfirmed(initial_state):
    async def scenario():
        old = BrokenClient()
        adapter = adapter_for(client=old, state=initial_state)
        started = []

        def spawn():
            started.append(True)
            adapter._client = NS(close=lambda: None)
            return RuntimeInfo(0, "injected runtime", "0.test")

        adapter._start_sync = spawn
        try:
            with suppress(Exception):
                await adapter.start_runtime()
            print(
                {
                    "initial": initial_state,
                    "old_close_calls": old.calls,
                    "new_starts": len(started),
                    "state": adapter._state,
                    "old_alive": old.alive,
                }
            )
            assert not started and adapter._client is old
        finally:
            dispose(adapter)

    asyncio.run(scenario())


def test_G2_cancelled_close_must_preserve_unconfirmed_owner():
    async def scenario():
        entered = threading.Event()
        release = threading.Event()

        class SlowBrokenClient(BrokenClient):
            def close(self):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    release.wait(timeout=2)
                raise OSError("injected close failure")

        client = SlowBrokenClient()
        adapter = adapter_for(client=client)
        task = asyncio.create_task(adapter.close())
        try:
            await eventually(entered.is_set)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            print(
                {
                    "cancel_state": adapter._state,
                    "client_retained": adapter._client is client,
                    "client_alive": client.alive,
                    "close_calls": client.calls,
                }
            )
            assert adapter._client is client and adapter._state != "STOPPED"
        finally:
            release.set()
            dispose(adapter)

    asyncio.run(scenario())


def test_G3_shutdown_must_surface_close_failure(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        await coordinator._get_backend()

        async def broken_close():
            raise OSError("injected close failure")

        backend.close = broken_close
        error = None
        try:
            try:
                await coordinator.aclose()
            except Exception as exc:
                error = exc
            print(
                {
                    "aclose_raised": repr(error),
                    "poisoned": coordinator._runtime_unrecoverable,
                    "backend_retained": coordinator._backend is backend,
                }
            )
            assert error is not None, (
                "shutdown caller cannot distinguish confirmed stop from failure"
            )
        finally:
            await FakeBackend.close(backend)

    asyncio.run(scenario())


def test_G3_sigterm_wrapper_must_exit_nonzero_when_sdk_close_fails(tmp_path):
    async def scenario():
        coordinator, backend, config, _ = setup(tmp_path)
        await coordinator._get_backend()

        async def broken_close():
            raise OSError("injected close failure")

        backend.close = broken_close
        path = ROOT / "server.py"
        tree = ast.parse(path.read_text())
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_graceful_then_exit"
        )
        ns = {
            "config": config,
            "coordinator": coordinator,
            "asyncio": asyncio,
            "os": NS(_exit=lambda code: exits.append(code)),
            "_logger": logging.getLogger("review.signal"),
        }
        exits = []
        module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
        exec(compile(module, str(path), "exec"), ns)
        try:
            await ns["_graceful_then_exit"]()
            print({"captured_exit_codes": exits, "poisoned": coordinator._runtime_unrecoverable})
            assert exits == [1], "signal wrapper reported success although close failed"
        finally:
            await FakeBackend.close(backend)

    asyncio.run(scenario())


@pytest.mark.parametrize("owner", ["active", "pending"])
def test_control_healthy_close_is_confirmed(owner):
    async def scenario():
        closed = []
        client = NS(close=lambda: closed.append(True))
        adapter = adapter_for(
            client=client if owner == "active" else None,
            pending=client if owner == "pending" else None,
        )
        try:
            await adapter.close()
            assert closed == [True]
            assert adapter._state == "STOPPED"
            assert adapter._client is None and adapter._pending_start_client is None
        finally:
            dispose(adapter)

    asyncio.run(scenario())


def test_control_healthy_shutdown_succeeds(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        await coordinator._get_backend()
        await coordinator.aclose()
        assert backend.close_calls == 1
        assert coordinator._backend is None
        assert not coordinator._runtime_unrecoverable

    asyncio.run(scenario())


def test_G2_coordinator_replacement_must_not_ignore_old_close_failure(tmp_path):
    async def scenario():
        from codex_app_mcp.backend.interface import RuntimeUnavailableError

        _, old, config, registry = setup(tmp_path)
        new = FakeBackend(Path(old.cwd))
        replacements = [old, new]
        coordinator = ExecutionCoordinator(
            lambda _: replacements.pop(0), config, bridge_cwd=old.cwd, registry=registry
        )

        async def unavailable_start():
            raise RuntimeUnavailableError(
                code="RUNTIME_DISCONNECTED", message="old backend cannot restart"
            )

        async def failed_close():
            old.close_calls += 1
            raise OSError("injected old-runtime close failure")

        old.start_runtime = unavailable_start
        old.close = failed_close
        error = None
        try:
            try:
                await coordinator._get_backend()
            except Exception as exc:
                error = exc
            print(
                {
                    "old_close_calls": old.close_calls,
                    "replacement_started": new.state == "READY",
                    "coordinator_poisoned": coordinator._runtime_unrecoverable,
                    "returned_error": repr(error),
                }
            )
            assert error is not None and new.state != "READY" and coordinator._backend is old
        finally:
            await FakeBackend.close(old)
            await FakeBackend.close(new)

    asyncio.run(scenario())
