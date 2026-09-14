"""The sole dependency point on the official ``openai_codex`` SDK.

Design sections 2.2, 4, 8.1, 10.1:

- Uses the low-level ``CodexClient`` with an immediate-decline approval
  handler (the SDK default accepts approvals — never used here).
- Unknown server requests raise :class:`UnsupportedServerRequestError` inside
  the SDK reader thread; the SDK fails pending waiters and this backend
  marks itself DEGRADED so the supervisor can close the runtime.
- Synchronous SDK calls are offloaded to two dedicated executors: a
  notification executor (turn event pump + global drain) and a control
  executor (RPCs, close). A blocked notification wait can therefore never
  prevent ``turn/interrupt`` or ``close``.
- The SDK's ``initialize()`` sends the ``initialized`` notification itself;
  this bridge never sends it again.
- No SDK private attributes are touched.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from openai_codex.client import CodexClient as _SdkCodexClient
from openai_codex.client import CodexConfig as _SdkCodexConfig
from openai_codex.errors import (
    CodexError,
    JsonRpcError,
    ServerBusyError,
    TransportClosedError,
)
from openai_codex.generated.v2_all import (
    AskForApproval,
    AskForApprovalValue,
    ModelListResponse,
    ReasoningEffort,
    SandboxMode,
    SandboxPolicy,
    ThreadReadResponse,
    ThreadResumeParams,
    ThreadResumeResponse,
    ThreadStartParams,
    ThreadStartResponse,
    TurnStartParams,
    TurnStartResponse,
)
from openai_codex.models import JsonObject, Notification

from .. import CHILD_ENV_FLAG
from ..errors import (
    RPC_TIMEOUT,
    RUNTIME_BUSY,
    RUNTIME_DISCONNECTED,
    STARTUP_TIMEOUT,
    UNSUPPORTED_SERVER_REQUEST,
    BridgeError,
)
from .interface import (
    RUNTIME_DEGRADED,
    RUNTIME_READY,
    RUNTIME_STARTING,
    RUNTIME_STOP_FAILED,
    RUNTIME_STOPPED,
    RUNTIME_STOPPING,
    BackendEvent,
    BackendOptions,
    CodexBackend,
    ModelCatalogEntry,
    ResumeThreadRequest,
    RuntimeInfo,
    RuntimeUnavailableError,
    StartThreadRequest,
    StartTurnRequest,
    ThreadSnapshot,
    TurnReceipt,
    UnsupportedServerRequestError,
)

_logger = logging.getLogger("codex_app_mcp.backend")

_DECLINE_COMMAND = {"decision": "decline"}
_DECLINE_FILE_CHANGE = {"decision": "decline"}

GlobalEventCallback = Callable[[str, object], None]


def _sandbox_mode(policy: SandboxPolicy | None) -> str:
    """Map a structured sandbox policy to the bridge-level mode string."""
    if policy is None:
        return "unknown"
    type_name = getattr(getattr(policy, "root", policy), "type", None)
    return {
        "readOnly": "read-only",
        "workspaceWrite": "workspace-write",
        "dangerFullAccess": "danger-full-access",
    }.get(str(type_name), "unknown")


def _sanitize_internal(exc: BaseException) -> str:
    """Truncate diagnostics and strip raw SDK stderr tails (design 10.3)."""
    text = repr(exc)
    for marker in ("stderr_tail=", "stderr tail="):
        idx = text.find(marker)
        if idx >= 0:
            text = text[:idx] + "<sdk-stderr-omitted>'"
    return text[:600]


def _approval_policy_text(value: AskForApproval | None) -> str | None:
    if value is None:
        return None
    root = getattr(value, "root", value)
    return getattr(root, "value", str(root))


def _absolute_path_text(value: object) -> str:
    """AbsolutePathBuf is a RootModel; extract the underlying path string."""
    return str(getattr(value, "root", value))


def _snapshot_from_start(response: ThreadStartResponse | ThreadResumeResponse) -> ThreadSnapshot:
    return ThreadSnapshot(
        thread_id=response.thread.id,
        cwd=_absolute_path_text(response.cwd),
        model=response.model,
        reasoning_effort=(
            response.reasoning_effort.value if response.reasoning_effort is not None else None
        ),
        sandbox_mode=_sandbox_mode(response.sandbox),
        approval_policy=_approval_policy_text(response.approval_policy),
        ephemeral=response.thread.ephemeral,
    )


def _snapshot_from_read(response: ThreadReadResponse) -> ThreadSnapshot:
    thread = response.thread
    status = getattr(getattr(thread.status, "root", thread.status), "type", None)
    return ThreadSnapshot(
        thread_id=thread.id,
        cwd=_absolute_path_text(thread.cwd),
        model=thread.model,
        reasoning_effort=thread.reasoning_effort.value if thread.reasoning_effort else None,
        sandbox_mode="unknown",
        approval_policy=None,
        ephemeral=thread.ephemeral,
        status=str(status) if status else None,
    )


def _ask_for_approval(value: str | None) -> AskForApproval | None:
    if value is None:
        return None
    return AskForApproval(AskForApprovalValue(value))


class CodexSdkBackend(CodexBackend):
    """Owns one App Server runtime process driven through ``CodexClient``."""

    def __init__(self, options: BackendOptions | None = None) -> None:
        self._options = options or BackendOptions()
        self._client: _SdkCodexClient | None = None
        self._generation = 0
        self._state = RUNTIME_STOPPED
        self._runtime_info: RuntimeInfo | None = None
        self._unsupported: UnsupportedServerRequestError | None = None
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._adopt_lock = threading.Lock()
        self._start_aborted = False
        self._pending_start_client: _SdkCodexClient | None = None
        self._control_exec = ThreadPoolExecutor(max_workers=2, thread_name_prefix="codex-ctl")
        self._notif_exec = ThreadPoolExecutor(max_workers=2, thread_name_prefix="codex-evt")
        self._drain_task: asyncio.Task[None] | None = None
        self._global_event_callback: GlobalEventCallback | None = None
        self._closed_executors = False

    # --- Approval handler (runs inside the SDK reader thread) --------------

    def _deny_approval_handler(self, method: str, params: JsonObject | None) -> JsonObject:
        if method == "item/commandExecution/requestApproval":
            _logger.info(
                "declining command execution approval request (thread=%s)",
                (params or {}).get("threadId"),
            )
            return dict(_DECLINE_COMMAND)
        if method == "item/fileChange/requestApproval":
            _logger.info(
                "declining file change approval request (thread=%s)",
                (params or {}).get("threadId"),
            )
            return dict(_DECLINE_FILE_CHANGE)
        exc = UnsupportedServerRequestError(method)
        self._unsupported = exc
        self._state = RUNTIME_DEGRADED
        _logger.error("unsupported server request; runtime degraded: %s", method)
        raise exc

    # --- Lifecycle -----------------------------------------------------------

    async def start_runtime(self) -> RuntimeInfo:
        async with self._start_lock:
            if self._client is not None and self._state == RUNTIME_READY:
                assert self._runtime_info is not None
                return self._runtime_info
            if self._state == RUNTIME_STOP_FAILED:
                # G2: a previously failed close left the stop unconfirmed;
                # this backend instance must never start another runtime.
                raise RuntimeUnavailableError(
                    code=RUNTIME_DISCONNECTED,
                    message=(
                        "runtime stop previously failed; stop is unconfirmed "
                        "and this backend is unusable"
                    ),
                )
            if self._state == RUNTIME_STOPPING or self._closed_executors:
                raise RuntimeUnavailableError(
                    code=RUNTIME_DISCONNECTED,
                    message="backend has been closed; runtime cannot restart in-process",
                )
            self._unsupported = None
            loop = asyncio.get_running_loop()
            # A degraded-but-alive runtime (unsupported request, rpc
            # timeout) must be closed before a replacement starts, otherwise
            # the old process leaks (review B-2). G2: a *failed* stale close
            # must fail-closed — never start a new runtime while the old
            # one's stop is unconfirmed.
            stale = self._client if self._state == RUNTIME_DEGRADED else None
            if stale is not None:
                try:
                    await loop.run_in_executor(self._control_exec, stale.close)
                except Exception as exc:  # noqa: BLE001
                    self._client = stale  # keep ownership for diagnosis
                    self._state = RUNTIME_STOP_FAILED
                    _logger.error(
                        "stale runtime close failed; refusing to start a "
                        "replacement (stop unconfirmed): %r",
                        exc,
                    )
                    raise RuntimeError(
                        f"stale runtime close failed; refusing to start a replacement: {exc!r}"
                    ) from exc
                self._client = None
            self._state = RUNTIME_STARTING
            try:
                info = await asyncio.wait_for(
                    loop.run_in_executor(self._control_exec, self._start_sync),
                    timeout=self._options.startup_timeout,
                )
            except TimeoutError:
                if self._state != RUNTIME_STOP_FAILED:
                    self._state = RUNTIME_DEGRADED
                # close() terminates the half-started child (registered in
                # the pending box) and unblocks the stuck startup worker.
                # G2: a failing close here must surface, not be swallowed —
                # but it must not mask the timeout classification.
                try:
                    await self.close()
                except BaseException:  # noqa: BLE001
                    _logger.exception("close after startup timeout failed")
                raise BridgeError(
                    code=STARTUP_TIMEOUT,
                    message=(
                        f"runtime startup did not complete within "
                        f"{self._options.startup_timeout:.0f}s"
                    ),
                    internal="startup timeout while spawning/initializing app-server",
                ) from None
            except asyncio.CancelledError:
                # Handler cancelled mid-start: stop the half-started runtime.
                # Cancellation itself must keep propagating even if the
                # cleanup close fails (G2 ownership handled inside close()).
                if self._state != RUNTIME_STOP_FAILED:
                    self._state = RUNTIME_DEGRADED
                try:
                    await self.close()
                except BaseException:  # noqa: BLE001
                    _logger.exception("close after startup cancellation failed")
                raise
            except BaseException as exc:
                # A startup worker may already have attempted the one safe
                # close for its client.  Preserve that authoritative result:
                # the pinned SDK detaches its process before close can fail,
                # so retrying cannot prove that the child stopped.
                if self._state != RUNTIME_STOP_FAILED:
                    self._state = RUNTIME_DEGRADED
                close_error: BaseException | None = None
                try:
                    await self.close()
                except BaseException as inner:  # noqa: BLE001
                    close_error = inner
                if close_error is not None or self._state == RUNTIME_STOP_FAILED:
                    raise RuntimeError(
                        "runtime cleanup after startup failure could not be "
                        f"confirmed: {close_error!r}"
                    ) from exc
                raise self._map_error(exc, default_code=RUNTIME_DISCONNECTED) from exc
            self._state = RUNTIME_READY
            self._generation += 1
            from dataclasses import replace as _dc_replace

            info = _dc_replace(info, generation=self._generation)
            self._runtime_info = info
            self._start_global_drain()
            return info

    def _start_sync(self) -> RuntimeInfo:
        options = self._options
        env: dict[str, str] = {CHILD_ENV_FLAG: "1"}
        env.update({k: str(v) for k, v in options.child_env.items()})
        config = _SdkCodexConfig(
            codex_bin=options.codex_bin,
            launch_args_override=options.launch_args_override,
            cwd=options.cwd,
            env=env,
            client_name=options.client_name,
            client_title=options.client_title,
        )
        client = _SdkCodexClient(config, approval_handler=self._deny_approval_handler)
        try:
            client.start()
            with self._adopt_lock:
                # Register the spawned process immediately so a racing
                # close()/startup-timeout can terminate it even before the
                # handshake completes (blocked initialize() never returns).
                if self._start_aborted:
                    raise RuntimeError("startup aborted before spawn")
                self._pending_start_client = client
            init = client.initialize()
        except BaseException:
            # Publish the close operation before waiting for it.  A racing
            # timeout/cancellation path in the event loop will join this
            # exact concurrent future instead of invoking SDK close again.
            with self._adopt_lock:
                close_futures = getattr(self, "_client_close_futures", None)
                if close_futures is None:
                    close_futures = {}
                    self._client_close_futures = close_futures
                entry = close_futures.get(id(client))
                if entry is None or entry[0] is not client:
                    close_source = self._control_exec.submit(client.close)
                    close_futures[id(client)] = (client, close_source)
                else:
                    close_source = entry[1]
            try:
                close_source.result()
            except Exception:  # noqa: BLE001
                # G2: the spawned client could not be stopped — the stop is
                # unconfirmed. Flag it so the async side (close()/start_
                # runtime) fails closed instead of leaking a new runtime.
                # Keep the pending owner until a successful first close has
                # actually confirmed the stop.  A later SDK close may be a
                # no-op after the process reference was detached.
                self._state = "STOP_FAILED"
                _logger.error("close of failed startup client did not confirm; marking STOP_FAILED")
            else:
                with self._adopt_lock:
                    if self._pending_start_client is client:
                        self._pending_start_client = None
                    close_futures.pop(id(client), None)
            raise
        # Adopt the client only if close() has not raced ahead of us; the
        # adopt lock makes the abort check and assignment atomic.
        with self._adopt_lock:
            if self._start_aborted:
                close_futures = getattr(self, "_client_close_futures", None)
                if close_futures is None:
                    close_futures = {}
                    self._client_close_futures = close_futures
                entry = close_futures.get(id(client))
                if entry is None or entry[0] is not client:
                    close_source = self._control_exec.submit(client.close)
                    close_futures[id(client)] = (client, close_source)
                else:
                    close_source = entry[1]
                try:
                    close_source.result()
                except Exception:  # noqa: BLE001
                    self._state = "STOP_FAILED"
                    _logger.error(
                        "close of aborted startup client did not confirm; marking STOP_FAILED"
                    )
                else:
                    if self._pending_start_client is client:
                        self._pending_start_client = None
                    close_futures.pop(id(client), None)
                raise RuntimeError("startup aborted before adoption")
            if self._pending_start_client is client:
                self._pending_start_client = None
            self._client = client
        server = init.serverInfo
        return RuntimeInfo(
            generation=0,  # replaced by start_runtime after adoption
            server_name=server.name if server else None,
            server_version=server.version if server else None,
        )

    async def close(self) -> None:
        """Stop the runtime; raise when the stop cannot be confirmed.

        F2/G2: a failed close must never be reported as ``RUNTIME_STOPPED``.
        Per-client outcomes are tracked separately (active vs pending) and
        unconfirmed references are retained for diagnosis. STOP_FAILED is
        sticky: a second close cannot turn an unconfirmed stop into success
        (the pinned SDK client drops its process reference before failing,
        so a later exception-free close proves nothing).
        """
        async with self._close_lock:
            if self._state == "STOP_FAILED":
                raise RuntimeError(
                    "runtime close previously failed; stop is unconfirmed and "
                    "this backend is unusable"
                )
            self._state = RUNTIME_STOPPING
            if self._drain_task is not None and not self._drain_task.done():
                self._drain_task.cancel()
            # The adopt lock is held by the other side only for a handful of
            # instructions; taking it here closes the startup race window.
            with self._adopt_lock:
                self._start_aborted = True
                client, self._client = self._client, None
                pending, self._pending_start_client = self._pending_start_client, None
            loop = asyncio.get_running_loop()
            # Concurrent futures outlive an asyncio waiter.  Keep exactly one
            # SDK close attempt per client so cancellation cannot trigger a
            # second, potentially no-op close and misclassify it as success.
            close_futures = getattr(self, "_client_close_futures", None)
            if close_futures is None:
                close_futures = {}
                self._client_close_futures = close_futures

            async def _close_once(target: _SdkCodexClient) -> None:
                entry = close_futures.get(id(target))
                if entry is None or entry[0] is not target:
                    source = self._control_exec.submit(target.close)
                    close_futures[id(target)] = (target, source)
                else:
                    source = entry[1]
                future = asyncio.wrap_future(source, loop=loop)
                # Retrieve a late exception even if the initiating waiter
                # was cancelled.  Later close callers wrap and receive the
                # same stored concurrent result.
                future.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
                await asyncio.shield(future)

            pending_confirmed = False
            active_confirmed = False
            failures: list[BaseException] = []

            def _restore_unconfirmed() -> None:
                """Give back references whose stop is not confirmed (G2)."""
                with self._adopt_lock:
                    if pending is not None and not pending_confirmed:
                        self._pending_start_client = pending
                    if client is not None and not active_confirmed:
                        self._client = client

            if pending is not None:
                # A half-started runtime (initialize() never returned) must
                # die here, otherwise its worker blocks forever and the child
                # process leaks. Killing the process unblocks the SDK waiter.
                try:
                    await _close_once(pending)
                except asyncio.CancelledError:
                    # The worker continues and its stored result remains the
                    # only admissible stop evidence for later waiters.
                    _restore_unconfirmed()
                    raise
                except Exception as exc:  # noqa: BLE001
                    failures.append(exc)
                else:
                    pending_confirmed = True
            if client is not None:
                try:
                    await _close_once(client)
                except asyncio.CancelledError:
                    _restore_unconfirmed()
                    raise
                except Exception as exc:  # noqa: BLE001
                    failures.append(exc)
                else:
                    active_confirmed = True
            if failures:
                # Keep every unconfirmed reference and surface the failure
                # instead of transitioning to STOPPED (G2: pending and
                # active outcomes are tracked independently).
                _restore_unconfirmed()
                self._state = "STOP_FAILED"
                _logger.error(
                    "runtime close failed (%d failure(s)); state=STOP_FAILED, "
                    "unconfirmed client references retained",
                    len(failures),
                )
                raise RuntimeError(
                    f"runtime close failed; stop not confirmed: {failures[0]!r}"
                ) from failures[0]
            for stopped in (pending, client):
                if stopped is not None:
                    close_futures.pop(id(stopped), None)
            if not self._closed_executors:
                self._closed_executors = True
                self._notif_exec.shutdown(wait=False)
                self._control_exec.shutdown(wait=False)
            self._state = RUNTIME_STOPPED

    # --- Liveness / state ----------------------------------------------------

    def _require_ready(self) -> _SdkCodexClient:
        if self._unsupported is not None:
            raise BridgeError(
                code=UNSUPPORTED_SERVER_REQUEST,
                message=(
                    "runtime received a server request this bridge cannot answer "
                    f"({self._unsupported.method}); runtime stopped"
                ),
                retryable=False,
                may_have_side_effects=True,
                internal=repr(self._unsupported),
            )
        client = self._client
        if client is None or self._state != RUNTIME_READY:
            raise RuntimeUnavailableError(
                code=RUNTIME_DISCONNECTED,
                message="codex runtime is not running",
            )
        return client

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_unsupported_request(self) -> UnsupportedServerRequestError | None:
        return self._unsupported

    @property
    def on_global_event(self) -> GlobalEventCallback | None:
        return self._global_event_callback

    @on_global_event.setter
    def on_global_event(self, callback: GlobalEventCallback | None) -> None:
        self._global_event_callback = callback

    # --- RPC plumbing ---------------------------------------------------------

    async def _call_rpc(
        self,
        method_name: str,
        /,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        client = self._require_ready()
        fn = getattr(client, method_name)
        loop = asyncio.get_running_loop()
        rpc_timeout = self._options.rpc_timeout if timeout is None else timeout
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._control_exec, partial(fn, *args, **kwargs)),
                timeout=rpc_timeout,
            )
        except TimeoutError:
            self._state = RUNTIME_DEGRADED
            raise BridgeError(
                code=RPC_TIMEOUT,
                message=f"runtime RPC timed out after {rpc_timeout:.0f}s",
                retryable=False,
                may_have_side_effects=False,
                internal=f"rpc timeout: {method_name}",
            ) from None
        except BridgeError:
            raise
        except BaseException as exc:
            raise self._map_error(exc, default_code=RUNTIME_DISCONNECTED) from exc

    def _map_error(self, exc: BaseException, *, default_code: str) -> BridgeError:
        if isinstance(exc, BridgeError):
            return exc
        if isinstance(exc, UnsupportedServerRequestError):
            return BridgeError(
                code=UNSUPPORTED_SERVER_REQUEST,
                message=(
                    "runtime sent a server request this bridge cannot answer "
                    f"({exc.method}); runtime stopped"
                ),
                retryable=False,
                may_have_side_effects=True,
                internal=_sanitize_internal(exc),
            )
        if isinstance(exc, TransportClosedError):
            self._state = RUNTIME_DEGRADED
            return BridgeError(
                code=RUNTIME_DISCONNECTED,
                message="codex runtime connection closed unexpectedly",
                retryable=False,
                internal=_sanitize_internal(exc),
            )
        if isinstance(exc, ServerBusyError):
            return BridgeError(
                code=RUNTIME_BUSY,
                message="codex runtime reported overload",
                retryable=True,
                internal=_sanitize_internal(exc),
            )
        if isinstance(exc, JsonRpcError):
            return BridgeError(
                code=default_code,
                message=f"codex runtime rejected the request: {exc.message}",
                retryable=False,
                internal=_sanitize_internal(exc),
            )
        if isinstance(exc, CodexError):
            return BridgeError(
                code=default_code,
                message=f"codex SDK error: {exc}",
                retryable=False,
                internal=_sanitize_internal(exc),
            )
        return BridgeError(
            code=default_code,
            message="unexpected backend failure",
            retryable=False,
            internal=_sanitize_internal(exc),
        )

    # --- Operations ------------------------------------------------------------

    async def list_models(self, *, include_hidden: bool = True) -> list[ModelCatalogEntry]:
        entries: dict[str, ModelCatalogEntry] = {}
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"includeHidden": include_hidden}
            if cursor is not None:
                params["cursor"] = cursor
            response: ModelListResponse = await self._call_rpc(
                "request",
                "model/list",
                params,
                response_model=ModelListResponse,
            )
            for model in response.data:
                efforts = tuple(
                    option.reasoning_effort.value
                    for option in model.supported_reasoning_efforts or ()
                )
                entries[model.id] = ModelCatalogEntry(
                    model_id=model.id,
                    display_name=model.display_name,
                    supported_reasoning_efforts=efforts,
                    default_reasoning_effort=(
                        model.default_reasoning_effort.value
                        if model.default_reasoning_effort
                        else None
                    ),
                    hidden=model.hidden,
                    is_default=model.is_default,
                )
            cursor = response.next_cursor
            if not cursor:
                return list(entries.values())

    async def start_thread(self, request: StartThreadRequest) -> ThreadSnapshot:
        params = ThreadStartParams(
            cwd=request.cwd,
            model=request.model,
            sandbox=SandboxMode(request.sandbox_mode),
            approval_policy=_ask_for_approval(request.approval_policy),
            base_instructions=request.base_instructions,
            developer_instructions=request.developer_instructions,
            config=dict(request.config) if request.config else None,
        )
        response: ThreadStartResponse = await self._call_rpc("thread_start", params)
        return _snapshot_from_start(response)

    async def resume_thread(self, thread_id: str, request: ResumeThreadRequest) -> ThreadSnapshot:
        params = ThreadResumeParams(
            thread_id=thread_id,
            model=request.model,
            approval_policy=_ask_for_approval(request.approval_policy),
            # Re-apply the bridge sandbox: persisted threads must not keep
            # running with elevated permissions (design 7.2).
            sandbox=(SandboxMode(request.sandbox_mode) if request.sandbox_mode else None),
        )
        if request.effort is not None:
            params.config = {"model_reasoning_effort": request.effort}
        response: ThreadResumeResponse = await self._call_rpc("thread_resume", thread_id, params)
        return _snapshot_from_start(response)

    async def read_thread(self, thread_id: str) -> ThreadSnapshot:
        response: ThreadReadResponse = await self._call_rpc("thread_read", thread_id, False)
        return _snapshot_from_read(response)

    async def start_turn(self, request: StartTurnRequest) -> TurnReceipt:
        params = TurnStartParams(
            thread_id=request.thread_id,
            input=[{"type": "text", "text": request.prompt}],
            model=request.model,
            effort=ReasoningEffort(request.effort) if request.effort else None,
        )
        response: TurnStartResponse = await self._call_rpc(
            "turn_start",
            request.thread_id,
            request.prompt,
            params,
        )
        return TurnReceipt(thread_id=request.thread_id, turn_id=response.turn.id)

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self._call_rpc("turn_interrupt", thread_id, turn_id)

    async def release_turn(self, turn_id: str) -> None:
        """Release SDK-side turn routing state after a terminal event.

        Keeps the router's per-turn state from growing across a long-lived
        serve process (review m-4). Best effort; never raises.
        """
        client = self._client
        if client is None:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                self._notif_exec, client.unregister_turn_notifications, turn_id
            )
        except Exception:  # noqa: BLE001 - release is best effort
            _logger.debug("turn notification release failed for %s", turn_id)

    async def next_turn_event(self, turn_id: str) -> BackendEvent:
        client = self._require_ready()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Notification] = loop.run_in_executor(  # type: ignore[assignment]
            self._notif_exec, client.next_turn_notification, turn_id
        )
        try:
            notification = await future
        except BridgeError:
            raise
        except asyncio.CancelledError:
            # Outer-cancellation (e.g. wait_for timeout): re-raise as-is; the
            # executor thread keeps its single waiter alive and the mapping
            # below must not fabricate a transport failure.
            raise
        except BaseException as exc:
            raise self._map_error(exc, default_code=RUNTIME_DISCONNECTED) from exc
        return BackendEvent(
            turn_id=turn_id, method=notification.method, payload=notification.payload
        )

    # --- Global notification drain ---------------------------------------------

    def _start_global_drain(self) -> None:
        if self._drain_task is not None and not self._drain_task.done():
            return
        self._drain_task = asyncio.create_task(self._drain_globals(), name="codex-global-drain")

    async def _drain_globals(self) -> None:
        """Consume global notifications so they cannot accumulate (design 8.1)."""
        loop = asyncio.get_running_loop()
        while True:
            client = self._client
            if client is None:
                return
            try:
                future = loop.run_in_executor(self._notif_exec, client.next_notification)
                notification = await future
            except asyncio.CancelledError:
                return
            except UnsupportedServerRequestError as exc:
                self._unsupported = exc
                self._state = RUNTIME_DEGRADED
                _logger.error("runtime degraded by unsupported server request: %s", exc.method)
                return
            except (TransportClosedError, CodexError) as exc:
                # Transport closed or reader dead: the runtime is unusable,
                # so the next operation fails fast instead of hanging.
                if self._state == RUNTIME_READY:
                    self._state = RUNTIME_DEGRADED
                _logger.debug("global notification drain ended: %r", exc)
                return
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, asyncio.CancelledError):
                    return
                # Bridge bug, not runtime death: log loudly, keep state.
                _logger.exception("global notification drain crashed")
                return
            callback = self._global_event_callback
            method = notification.method
            if callback is not None:
                try:
                    callback(method, notification.payload)
                except Exception:  # noqa: BLE001 - callbacks must not kill the drain
                    _logger.exception("global event callback failed")
            else:
                _logger.debug("global notification dropped: %s", method)
