"""Permanent regressions for external review v4 lifecycle findings H1-H3."""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from types import SimpleNamespace as NS

import pytest

from codex_app_mcp.backend import codex_sdk
from codex_app_mcp.backend.codex_sdk import CodexSdkBackend
from codex_app_mcp.backend.interface import BackendOptions

from .previous_review_support import setup


class DroppingClient:
    """A pinned-SDK close that detaches its process before failing."""

    def __init__(self, *, initialize_fails: bool = True, close_fails: bool = True) -> None:
        self.alive = False
        self.owns_proc = False
        self.calls = 0
        self.initialize_fails = initialize_fails
        self.close_fails = close_fails

    def start(self) -> None:
        self.alive = True
        self.owns_proc = True

    def initialize(self) -> NS:
        if self.initialize_fails:
            raise OSError("injected initialize failure")
        return NS(serverInfo=NS(name="fake", version="0.test"))

    def close(self) -> None:
        self.calls += 1
        if not self.owns_proc:
            return
        self.owns_proc = False
        if self.close_fails:
            raise OSError("injected close failure after process detach")
        self.alive = False


def _dispose(backend: CodexSdkBackend) -> None:
    backend._control_exec.shutdown(wait=True)
    backend._notif_exec.shutdown(wait=True)


@pytest.mark.parametrize("abort_before_adoption", [False, True])
async def test_h1_startup_cleanup_failure_is_sticky_and_retains_owner(
    monkeypatch: pytest.MonkeyPatch, abort_before_adoption: bool
) -> None:
    client = DroppingClient(initialize_fails=not abort_before_adoption)
    backend = CodexSdkBackend(BackendOptions(startup_timeout=0.3))
    monkeypatch.setattr(codex_sdk, "_SdkCodexConfig", lambda **kwargs: NS(**kwargs))
    monkeypatch.setattr(codex_sdk, "_SdkCodexClient", lambda *args, **kwargs: client)
    if abort_before_adoption:
        original_initialize = client.initialize

        def initialize_then_abort() -> NS:
            backend._start_aborted = True
            return original_initialize()

        client.initialize = initialize_then_abort  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await backend.start_runtime()
        assert backend.state == "STOP_FAILED"
        assert backend._pending_start_client is client
        assert client.calls == 1
    finally:
        _dispose(backend)


@pytest.mark.parametrize("owner", ["active", "pending"])
async def test_h2_cancelled_waiter_rejoins_first_close_result(owner: str) -> None:
    entered = threading.Event()
    release = threading.Event()

    class SlowDroppingClient(DroppingClient):
        def close(self) -> None:
            self.calls += 1
            if not self.owns_proc:
                return
            self.owns_proc = False
            entered.set()
            release.wait(timeout=2)
            raise OSError("first close failed after process detach")

    client = SlowDroppingClient()
    client.start()
    backend = CodexSdkBackend()
    backend._state = "READY" if owner == "active" else "STARTING"
    backend._client = client if owner == "active" else None
    backend._pending_start_client = client if owner == "pending" else None
    first = asyncio.create_task(backend.close())
    try:
        await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        with suppress(asyncio.CancelledError):
            await first
        release.set()
        with pytest.raises(RuntimeError):
            await backend.close()
        assert backend.state == "STOP_FAILED"
        assert client.calls == 1
        assert backend._client is client or backend._pending_start_client is client
    finally:
        release.set()
        _dispose(backend)


async def test_h3_cancelled_creator_keeps_shared_stop_registered(tmp_path) -> None:
    coordinator, backend, _, _ = setup(tmp_path)
    await coordinator._get_backend()
    gate = asyncio.Event()
    entered = asyncio.Event()
    calls = 0
    original_close = backend.close

    async def slow_close() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await gate.wait()
        await original_close()

    backend.close = slow_close
    first = asyncio.create_task(coordinator._stop_backend(backend))
    second = None
    try:
        await entered.wait()
        first.cancel()
        with suppress(asyncio.CancelledError):
            await first
        second = asyncio.create_task(coordinator._stop_backend(backend))
        await asyncio.sleep(0.02)
        assert calls == 1
        assert len(coordinator._stop_tasks) == 1
    finally:
        gate.set()
        if second is not None:
            await second
        await coordinator.aclose()
