"""Additional acceptance coverage for the v2 review findings.

Complements tests/regression/test_followup_review.py (kept verbatim) with
acceptance criteria stated in REVIEW.md that the supplied probes only imply:

- F2: an unconfirmed stop must refuse subsequent runs (same backend).
- F5: the requested-cwd vs confirmed-cwd consistency also applies to NEW
  threads, and the workspace lock key matches the confirmed cwd.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from codex_app_mcp.backend.interface import StartThreadRequest, ThreadSnapshot
from codex_app_mcp.contracts import CodexCall, CodexReplyCall

from .previous_review_support import setup


async def _finish(coordinator, backend):
    await type(backend).close(backend)
    with suppress(Exception, asyncio.CancelledError):
        await coordinator.aclose()


def test_f2_unconfirmed_stop_refuses_new_runs(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        backend.mode = "ignore_interrupt"

        real_close = backend.close
        closed = 0

        async def broken_close():
            nonlocal closed
            closed += 1
            raise OSError("stop failed")

        backend.close = broken_close
        try:
            first = await coordinator.run_codex(CodexCall(prompt="will time out"))
            assert first["isError"]
            assert closed >= 1, "stop must have been attempted"
            run = coordinator._active
            assert not run.runtime_stop_assured
            assert run.lease_retained

            # F2 acceptance: no new run while the stop is unconfirmed.
            second = await coordinator.run_codex(CodexCall(prompt="after failure"))
            assert second["isError"]
            assert second["_meta"]["codex-app-mcp"]["code"] == "SERVER_BUSY"
            assert second["_meta"]["codex-app-mcp"]["retryable"] is False
        finally:
            backend.close = real_close
            await _finish(coordinator, backend)

    asyncio.run(scenario())


def test_f5_new_thread_confirmed_cwd_must_match_request(tmp_path):
    async def scenario():
        _, backend, config, registry = setup(tmp_path, sandbox="workspace-write")
        requested = backend.cwd
        confirmed = str(tmp_path / "elsewhere")

        real_start = backend.start_thread

        async def redirected_start(request: StartThreadRequest) -> ThreadSnapshot:
            snapshot = await real_start(request)
            # The runtime confirms a different cwd than the bridge requested.
            return ThreadSnapshot(
                snapshot.thread_id,
                confirmed,
                snapshot.model,
                snapshot.reasoning_effort,
                snapshot.sandbox_mode,
                snapshot.approval_policy,
            )

        backend.start_thread = redirected_start
        coordinator = setup(tmp_path, backend=backend, sandbox="workspace-write")[0]
        try:
            result = await coordinator.run_codex(CodexCall(prompt="new thread"))
            assert result["isError"], "confirmed cwd diverged from the request"
            meta = result["_meta"]["codex-app-mcp"]
            assert meta["code"] == "RUNTIME_MISMATCH"
            assert not backend.turn_calls
            # The workspace lock was taken on the *requested* cwd (the only
            # one known pre-start) and released with the failed run.
            with registry.workspace_lock(requested):
                pass
        finally:
            await _finish(coordinator, backend)

    asyncio.run(scenario())


def test_f5_lock_key_matches_confirmed_cwd_on_reply(tmp_path):
    """Happy-path: a reply's held workspace lock is on the confirmed cwd,
    so another bridge holding that exact workspace blocks the reply."""

    async def scenario():
        _, backend, config, registry = setup(tmp_path, sandbox="workspace-write")
        coordinator = setup(tmp_path, backend=backend, sandbox="workspace-write")[0]
        try:
            first = await coordinator.run_codex(CodexCall(prompt="create"))
            assert not first["isError"]
            thread_id = first["structuredContent"]["threadId"]
            # Same backend.cwd both cached and confirmed: locking it must
            # block the write reply (lock key == confirmed cwd).
            with registry.workspace_lock(backend.cwd):
                result = await coordinator.run_reply(
                    CodexReplyCall(prompt="reply", thread_id=thread_id)
                )
            assert result["isError"]
            assert result["_meta"]["codex-app-mcp"]["code"] == "SERVER_BUSY"
            assert len(backend.turn_calls) == 1  # only the creating turn
        finally:
            await _finish(coordinator, backend)

    asyncio.run(scenario())


def test_f4_reply_malformed_terminal_keeps_stop_obligation(tmp_path):
    """F4 on the reply path: an unreadable terminal must not release the
    lease without a stop (mirrors the new-thread case)."""

    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path, sandbox="workspace-write")
        first = await coordinator.run_codex(CodexCall(prompt="create"))
        thread_id = first["structuredContent"]["threadId"]

        backend.mode = "malformed_terminal"
        backend.turn_calls.clear()
        result = await coordinator.run_reply(CodexReplyCall(prompt="x", thread_id=thread_id))
        meta = result["_meta"]["codex-app-mcp"]
        assert result["isError"]
        assert meta["code"] in {"TURN_TIMEOUT", "TURN_FAILED"}
        # The stop obligation ran: close was called or the lease is retained.
        run = coordinator._active
        assert run.runtime_stop_assured or run.lease_retained or backend.close_calls >= 1
        assert not backend.running, "runtime must not stay running unconfirmed"

    asyncio.run(scenario())
