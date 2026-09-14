"""Execution coordination: run lifecycle, cancellation, deadlines (design 7-9).

One coordinator owns at most one active turn (v0.1).

Lifecycle separation (external review R1): three distinct conditions are
tracked per run and never conflated —

- ``turn_terminal_confirmed``  the runtime delivered a parseable
  ``turn/completed`` for the run's turn;
- ``runtime_stop_assured``     the owning runtime was stopped because the
  terminal event could not be confirmed in bounds;
- ``terminal_event``           the execution task settled and the run slot
  plus its advisory locks may be released.

MCP handler cancellation never aborts SDK operations directly: each run
executes inside a supervisor task (:meth:`_supervise`). A cancellation that
arrives *before* the turn is submitted aborts the run without submitting
(review R5); once ``turn/start`` is in flight the supervisor collects the
late acknowledgement and interrupts that exact turn.

Executed-or-unknown turns are never resent (design 9.3).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from .backend.interface import (
    CodexBackend,
    ResumeThreadRequest,
    RuntimeUnavailableError,
    StartThreadRequest,
    StartTurnRequest,
    ThreadSnapshot,
)
from .config import BridgeConfig, LimitsConfig
from .contracts import CodexCall, CodexReplyCall
from .errors import (
    EXECUTION_STATE_UNKNOWN,
    RUNTIME_BUSY,
    RUNTIME_DISCONNECTED,
    RUNTIME_MISMATCH,
    RUNTIME_STOP_UNCONFIRMED,
    SERVER_BUSY_CODE,
    THREAD_BUSY,
    TURN_FAILED,
    TURN_INTERRUPTED,
    TURN_TIMEOUT,
    UNSUPPORTED_SERVER_REQUEST,
    BridgeError,
    internal_error,
)
from .policy import ModelCatalog, PolicyStore
from .registry import (
    THREAD_ACTIVE,
    THREAD_IDLE,
    THREAD_INTERRUPTING,
    THREAD_UNKNOWN,
    ThreadRegistry,
)
from .results import TurnResultReducer, build_error_envelope, build_success_envelope

_logger = logging.getLogger("codex_app_mcp.coordinator")

#: Errors that imply the runtime itself is no longer trustworthy: stop it
#: before answering the client.
_FATAL_RUNTIME_CODES = frozenset({UNSUPPORTED_SERVER_REQUEST, "RPC_TIMEOUT", RUNTIME_DISCONNECTED})

#: Transport-ish failures that make a start outcome unknowable.
_UNKNOWN_OUTCOME_CODES = frozenset({"RPC_TIMEOUT", RUNTIME_DISCONNECTED, RUNTIME_BUSY})

# Run states (design 8.2)
RUN_VALIDATING = "VALIDATING"
RUN_STARTING_THREAD = "STARTING_THREAD"
RUN_STARTING_TURN = "STARTING_TURN"
RUN_RUNNING = "RUNNING"
RUN_CANCELLING = "CANCELLING"
RUN_TERMINAL = "TERMINAL"

BackendFactory = Callable[[LimitsConfig], CodexBackend]
CoroutineLike = Coroutine[Any, Any, dict[str, Any]]


@dataclass(slots=True)
class ActiveRun:
    """Design 8.2 active run record, with the R1 lifecycle split."""

    session_id: str | None
    mcp_request_id: str | None
    tool: str
    state: str = RUN_VALIDATING
    thread_id: str | None = None
    turn_id: str | None = None
    runtime_generation: int = 0
    backend_ref: CodexBackend | None = None
    cancellation_requested: bool = False
    #: parseable turn/completed observed for this run's turn
    turn_terminal_confirmed: bool = False
    #: runtime stop confirmed (close succeeded) after an unconfirmed terminal
    runtime_stop_assured: bool = False
    #: turn/start has been *sent* (outcome may still be unknown) — F3
    turn_submission_started: bool = False
    #: advisory locks retained until process exit because safety could not
    #: be confirmed (stop failure with an unconfirmed terminal) — F2
    lease_retained: bool = False
    submitted_model: str | None = None
    submitted_effort: str | None = None
    requested_model: str | None = None
    requested_effort: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    deadline: float | None = None
    #: execution task settled — slot and locks may be released
    terminal_event: asyncio.Event = field(default_factory=asyncio.Event)
    #: runtime delivered a turn/completed-shaped event (parseable or not)
    turn_terminal_event: asyncio.Event = field(default_factory=asyncio.Event)
    result_uncertain: bool = False


class _RunLease:
    """Explicit advisory-lock holder for one run (F2/F3).

    Unlike a ``with`` block bound to the execution coroutine, release is a
    decision: locks are dropped only when the run is provably safe (terminal
    confirmed, runtime stop confirmed, or nothing was ever submitted).
    Retained leases keep their flock until process exit.
    """

    def __init__(self, registry: ThreadRegistry) -> None:
        self._registry = registry
        self._held: list[Any] = []

    def acquire_workspace(self, cwd: str) -> None:
        ctx = self._registry.workspace_lock(cwd)
        ctx.__enter__()
        self._held.append(ctx)

    def acquire_thread(self, thread_id: str) -> None:
        ctx = self._registry.thread_lock(thread_id)
        ctx.__enter__()
        self._held.append(ctx)

    def release(self) -> None:
        while self._held:
            self._held.pop().__exit__(None, None, None)


class ProgressSink:
    """Throttled standard-progress sender; None token disables sending."""

    def __init__(
        self,
        sender: Callable[[float, str], Awaitable[None]] | None,
        *,
        min_interval: float,
    ) -> None:
        self._sender = sender
        self._min_interval = min_interval
        self._last_sent = 0.0
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def ping(self, message: str) -> None:
        if self._stopped or self._sender is None:
            return
        now = time.monotonic()
        if now - self._last_sent < self._min_interval:
            return
        self._last_sent = now
        try:
            # Bounded: a client that stopped reading stdout must not stall
            # the turn loop past the deadline checks.
            await asyncio.wait_for(self._sender(now, message), timeout=2.0)
        except Exception:  # noqa: BLE001 - progress is best effort
            self._sender = None


class ExecutionCoordinator:
    """Serializes runs, owns the backend, and maps failures to envelopes."""

    def __init__(
        self,
        backend_factory: BackendFactory,
        config: BridgeConfig,
        *,
        bridge_cwd: str,
        registry: ThreadRegistry | None = None,
    ) -> None:
        self._backend_factory = backend_factory
        self._config = config
        self._policy = PolicyStore(config)
        self._bridge_cwd = bridge_cwd
        self._registry = registry or ThreadRegistry()
        self._backend: CodexBackend | None = None
        self._backend_lock = asyncio.Lock()
        # G1: stops are per-runtime, joinable, and remembered. In-flight
        # stops are shared tasks so cancellation/timeout/fatal paths all
        # converge on one result per backend instance.
        self._stop_tasks: dict[int, asyncio.Task[bool]] = {}
        # Backends whose stop was CONFIRMED. Strong references prevent id()
        # reuse from aliasing a fresh backend onto a dead identity.
        self._stopped_backends: dict[int, CodexBackend] = {}
        self._catalog: ModelCatalog | None = None
        # R10: the cache key pairs the backend instance identity with its
        # generation; a replacement backend never reuses a stale catalog even
        # when both report generation 1.
        self._catalog_key: tuple[int, int] | None = None
        self._active: ActiveRun | None = None
        self._closed = False
        # F2: once a runtime stop fails (or safety cannot be confirmed) the
        # bridge refuses new runs and retains orphaned leases until exit.
        self._runtime_unrecoverable = False
        self._retained_leases: list[_RunLease] = []

    # --- backend ownership ----------------------------------------------------

    async def _get_backend(self) -> CodexBackend:
        async with self._backend_lock:
            if self._backend is None:
                self._backend = self._backend_factory(self._config.limits)
            backend = self._backend
        try:
            await backend.start_runtime()
            return backend
        except RuntimeUnavailableError:
            if self._stop_unconfirmed(backend):
                return await self._fail_replacement(backend)
            return await self._replace_backend(backend)
        except BaseException as exc:
            if self._stop_unconfirmed(backend):
                return await self._fail_replacement(backend, cause=exc)
            raise

    def _stop_unconfirmed(self, backend: CodexBackend) -> bool:
        """The backend reports (or retain evidence of) an unconfirmed stop."""
        return getattr(backend, "state", None) == "STOP_FAILED"

    async def _fail_replacement(
        self, backend: CodexBackend, *, cause: BaseException | None = None
    ) -> CodexBackend:
        self._runtime_unrecoverable = True
        _logger.error("runtime stop is unconfirmed; refusing to replace or accept work")
        raise BridgeError(
            code=RUNTIME_STOP_UNCONFIRMED,
            message=(
                "a previous runtime stop could not be confirmed; the bridge "
                "refuses new work until the process is restarted"
            ),
            retryable=False,
            may_have_side_effects=False,
        ) from cause

    async def _replace_backend(self, old: CodexBackend) -> CodexBackend:
        """Swap a confirmed-stopped backend for a fresh instance (G2).

        The old runtime's close must succeed here; a failure is never
        swallowed — the coordinator is poisoned instead.
        """
        _logger.warning("replacing stopped runtime backend")
        if not await self._stop_backend(old):
            return await self._fail_replacement(old)
        async with self._backend_lock:
            self._backend = self._backend_factory(self._config.limits)
            backend = self._backend
        try:
            await backend.start_runtime()
        except BaseException as exc:
            if self._stop_unconfirmed(backend):
                return await self._fail_replacement(backend, cause=exc)
            raise
        return backend

    async def _stop_backend(self, target: CodexBackend | None = None) -> bool:
        """Stop a *specific* runtime; True only when the stop is confirmed.

        G1: the target defaults to the current backend but callers that owe
        a stop for a run pass ``run.backend_ref`` — an old run's cleanup can
        never stop a replacement runtime. Duplicate requests for the same
        backend join one in-flight stop task and share its result; a
        confirmed stop is remembered so late cleanup becomes a no-op.
        """
        backend = target if target is not None else self._backend
        if backend is None:
            return True
        key = id(backend)
        if key in self._stopped_backends:
            return True
        existing = self._stop_tasks.get(key)
        if existing is not None and not existing.done():
            # Join the same stop; shield so a cancelled caller cannot abort
            # another caller's close.
            return await asyncio.shield(existing)
        task = asyncio.create_task(self._close_backend(backend), name=f"stop-runtime-{key}")
        self._stop_tasks[key] = task

        def _forget(completed: asyncio.Task[bool]) -> None:
            if self._stop_tasks.get(key) is task:
                self._stop_tasks.pop(key, None)

        # Registry lifetime belongs to the shared operation, not to whichever
        # waiter happened to create it.  A cancelled waiter must leave the
        # still-running stop discoverable by later callers.
        task.add_done_callback(_forget)
        return await asyncio.shield(task)

    async def _close_backend(self, backend: CodexBackend) -> bool:
        try:
            await asyncio.wait_for(
                backend.close(), timeout=self._config.limits.shutdown_grace_seconds + 5
            )
        except BaseException as exc:  # noqa: BLE001 - any failure poisons
            self._runtime_unrecoverable = True
            _logger.error(
                "runtime stop failed; bridge refuses new runs until restart: %s",
                _sanitize(exc),
            )
            return False
        async with self._backend_lock:
            if self._backend is backend:
                self._backend = None
        self._stopped_backends[id(backend)] = backend
        self._invalidate_catalog()
        return True

    async def _backend_or_none(self) -> CodexBackend | None:
        async with self._backend_lock:
            return self._backend

    def _invalidate_catalog(self) -> None:
        self._catalog = None
        self._catalog_key = None

    # --- catalog ---------------------------------------------------------------

    async def _get_catalog(self, backend: CodexBackend, *, force: bool = False) -> ModelCatalog:
        key = (id(backend), backend.generation)
        if not force and self._catalog is not None and self._catalog_key == key:
            return self._catalog
        entries = await backend.list_models(include_hidden=True)
        self._catalog = ModelCatalog(entries)
        self._catalog_key = key
        return self._catalog

    async def _validate_model_with_refresh(self, backend: CodexBackend, model: str) -> None:
        """Validate a model against the catalog, refreshing exactly once."""
        allowed = self._config.policy.allowed_models
        catalog = await self._get_catalog(backend)
        try:
            catalog.validate_model(model, allowed_models=allowed)
        except BridgeError as exc:
            if exc.code != "MODEL_UNAVAILABLE":
                raise
            # One refresh, then fail — never fall back to another model.
            catalog = await self._get_catalog(backend, force=True)
            catalog.validate_model(model, allowed_models=allowed)

    # --- slot management --------------------------------------------------------

    def _reserve_slot(self, tool: str, session_id: str | None, request_id: str | None) -> ActiveRun:
        if self._closed:
            raise BridgeError(
                code=SERVER_BUSY_CODE,
                message="bridge is shutting down; not accepting new calls",
                retryable=False,
            )
        if self._runtime_unrecoverable:
            # F2: an unconfirmed runtime stop means the old turn may still be
            # running; accepting new work could duplicate executions.
            raise BridgeError(
                code=SERVER_BUSY_CODE,
                message=(
                    "a previous run could not be confirmed stopped; the bridge "
                    "refuses new calls until the process is restarted"
                ),
                retryable=False,
            )
        active = self._active
        if active is not None and not active.terminal_event.is_set():
            raise BridgeError(
                code=SERVER_BUSY_CODE,
                message=(
                    "another turn is active in this bridge process (max_active_turns=1 in v0.1)"
                ),
                retryable=True,
            )
        run = ActiveRun(session_id=session_id, mcp_request_id=request_id, tool=tool)
        self._active = run
        return run

    def _check_cancelled(self, run: ActiveRun) -> None:
        """R5: a cancelled request must not submit work that never started."""
        if run.cancellation_requested:
            raise asyncio.CancelledError

    def _release_slot_if_safe(self, run: ActiveRun) -> None:
        """F2: task completion alone is not evidence the run is safe.

        The slot (and with it the right to accept a new run in-process)
        frees only when the turn terminal was confirmed, the runtime stop
        was confirmed, or nothing was ever submitted. Unsafe runs keep the
        slot occupied; the poison flag rejects new work regardless.
        """
        if run.terminal_event.is_set():
            return
        if (
            run.turn_terminal_confirmed
            or run.runtime_stop_assured
            or not run.turn_submission_started
        ):
            run.terminal_event.set()

    def _settle_lease(self, run: ActiveRun, lease: _RunLease) -> None:
        """F2: release locks only when the run is provably safe.

        Safe means: the turn's terminal was confirmed, the runtime stop was
        confirmed, or no turn/start was ever sent (nothing is running).
        Otherwise the lease is retained until process exit and the bridge is
        poisoned — an unconfirmed stop must not unlock a possibly running
        execution for other callers.
        """
        if (
            run.turn_terminal_confirmed
            or run.runtime_stop_assured
            or not run.turn_submission_started
        ):
            lease.release()
        else:
            run.lease_retained = True
            self._retained_leases.append(lease)
            self._runtime_unrecoverable = True
            _logger.error(
                "run ended without a confirmed terminal or confirmed runtime "
                "stop; retaining advisory locks until process exit"
            )
        self._release_slot_if_safe(run)

    # --- public entry points ------------------------------------------------------

    async def run_codex(
        self,
        call: CodexCall,
        *,
        session_id: str | None = None,
        mcp_request_id: str | None = None,
        progress: ProgressSink | None = None,
    ) -> dict[str, Any]:
        try:
            run = self._reserve_slot("codex", session_id, mcp_request_id)
        except BridgeError as error:
            # Pre-flight rejections are returned as tool-level errors so the
            # coordinator API stays envelope-consistent for every caller.
            return build_error_envelope(
                error,
                requested_model=call.model,
                requested_effort=call.effort,
            )
        run.requested_model = call.model
        run.requested_effort = call.effort
        return await self._supervise(run, self._execute_codex(run, call, progress), progress)

    async def run_reply(
        self,
        call: CodexReplyCall,
        *,
        session_id: str | None = None,
        mcp_request_id: str | None = None,
        progress: ProgressSink | None = None,
    ) -> dict[str, Any]:
        # Design 8.3: while a turn is active, a call to the SAME thread is
        # THREAD_BUSY; other new calls are SERVER_BUSY.
        active = self._active
        if active is not None and not active.terminal_event.is_set():
            if active.thread_id == call.thread_id:
                return build_error_envelope(
                    BridgeError(
                        code=THREAD_BUSY,
                        message=(
                            f"thread {call.thread_id} has an active turn; v0.1 "
                            "does not steer running turns"
                        ),
                        retryable=True,
                        thread_id=call.thread_id,
                    ),
                    requested_model=call.model,
                    requested_effort=call.effort,
                )
        try:
            run = self._reserve_slot("codex-reply", session_id, mcp_request_id)
        except BridgeError as error:
            return build_error_envelope(
                error,
                requested_model=call.model,
                requested_effort=call.effort,
            )
        run.thread_id = call.thread_id
        run.requested_model = call.model
        run.requested_effort = call.effort
        return await self._supervise(run, self._execute_reply(run, call, progress), progress)

    async def _supervise(
        self,
        run: ActiveRun,
        execution: CoroutineLike,
        progress: ProgressSink | None,
    ) -> dict[str, Any]:
        task = asyncio.create_task(execution, name=f"run-{run.tool}")

        def _on_task_done(task: asyncio.Task[dict[str, Any]]) -> None:
            # G1: consume the outcome so a post-cancellation failure never
            # surfaces as "Task exception was never retrieved".
            if not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    _logger.debug("execution task ended with %r", exc)
            self._release_slot_if_safe(run)

        # F2: the done callback only *attempts* the safety-gated release —
        # the lease settlement inside the task decides whether the run is
        # provably safe, and an unsafe run keeps the slot occupied.
        task.add_done_callback(_on_task_done)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            run.cancellation_requested = True
            if progress is not None:
                progress.stop()
            limits = self._config.limits
            try:
                cleanup = asyncio.create_task(self._cancel_run(run), name=f"cancel-{run.tool}")
                await asyncio.shield(
                    asyncio.wait_for(
                        cleanup,
                        timeout=limits.interrupt_grace_seconds
                        + limits.shutdown_grace_seconds
                        + limits.rpc_timeout_seconds
                        + 5,
                    )
                )
            except asyncio.CancelledError, TimeoutError, Exception:  # noqa: BLE001
                _logger.exception("cancel cleanup did not complete in bounds")
            raise
        except BridgeError as exc:
            if run.thread_id is not None:
                exc.thread_id = exc.thread_id or run.thread_id
            if run.turn_id is not None:
                exc.turn_id = exc.turn_id or run.turn_id
                # R6: once a turn was acknowledged, every failure may have
                # side effects — the caller must never read "safe to resend".
                exc.may_have_side_effects = True
            _logger.warning(
                "run failed",
                extra={"code": exc.code, "thread_id": exc.thread_id, "turn_id": exc.turn_id},
            )
            # Design 10.1/9.2: untrustworthy runtimes stop before answering —
            # always the run's OWN runtime, never the current one (G1).
            if exc.code in _FATAL_RUNTIME_CODES:
                await self._stop_backend(run.backend_ref)
            return build_error_envelope(
                exc,
                requested_model=run.requested_model,
                requested_effort=run.requested_effort,
            )
        except Exception as exc:
            # Unexpected internal failure: isError result with honest side
            # effect semantics (R6) and sanitized diagnostics.
            _logger.error("run crashed: %s", _sanitize(exc))
            error = internal_error(
                "internal bridge failure; see bridge stderr logs",
                internal=_sanitize(exc),
            )
            if run.thread_id is not None:
                error.thread_id = run.thread_id
            if run.turn_id is not None:
                error.turn_id = run.turn_id
                error.may_have_side_effects = True
            return build_error_envelope(
                error,
                requested_model=run.requested_model,
                requested_effort=run.requested_effort,
            )

    # --- cancellation (design 9.1) --------------------------------------------------

    async def _cancel_run(self, run: ActiveRun, *, grace: float | None = None) -> None:
        if run.terminal_event.is_set():
            return
        # F3: a run whose turn/start was never sent has no terminal to wait
        # for and no runtime to stop — its cleanup completes immediately and
        # must never touch the backend a subsequent run may be using.
        if not run.turn_submission_started:
            return
        run.state = RUN_CANCELLING
        if run.thread_id is not None:
            self._registry.set_state(run.thread_id, THREAD_INTERRUPTING)
        limits = self._config.limits
        backend = run.backend_ref or await self._backend_or_none()
        if backend is not None and run.turn_id is not None:
            current = await self._backend_or_none()
            if current is not None and backend is not current:
                _logger.warning(
                    "owning runtime was replaced; skipping interrupt to avoid "
                    "cross-runtime delivery"
                )
            else:
                try:
                    await asyncio.wait_for(
                        backend.interrupt_turn(run.thread_id or "", run.turn_id),
                        timeout=limits.rpc_timeout_seconds,
                    )
                except (BridgeError, TimeoutError) as exc:
                    _logger.warning("interrupt during cancel failed: %r", exc)
        wait_grace = limits.interrupt_grace_seconds if grace is None else grace
        await self._wait_terminal_or_stop(run, wait_grace)

    def _run_settled_safe(self, run: ActiveRun) -> bool:
        """The run needs no further stop/interrupt work from anyone (G1)."""
        return (
            run.turn_terminal_confirmed
            or run.runtime_stop_assured
            or not run.turn_submission_started
            or run.terminal_event.is_set()
        )

    async def _wait_terminal_or_stop(self, run: ActiveRun, grace: float) -> None:
        """R1/G1: wait until the run is settled safe, then stop only the
        run's OWN runtime when it never settled.

        ``runtime_stop_assured`` set by the execution task's own failure
        path counts as settled — an old run's cleanup must never reach for
        the current (possibly replacement) backend.
        """
        deadline = time.monotonic() + grace
        while not self._run_settled_safe(run) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if self._run_settled_safe(run):
            return
        _logger.error(
            "run not settled within grace (%.2fs); stopping the run's runtime",
            grace,
        )
        backend = run.backend_ref
        if backend is not None and await self._stop_backend(backend):
            run.runtime_stop_assured = True
            self._release_slot_if_safe(run)

    # --- execution: new thread ---------------------------------------------------

    async def _execute_codex(
        self, run: ActiveRun, call: CodexCall, progress: ProgressSink | None
    ) -> dict[str, Any]:
        run.state = RUN_VALIDATING
        cwd = self._policy.resolve_cwd(call.cwd, bridge_cwd=self._bridge_cwd)
        self._policy.check_workspace(cwd)
        sandbox = call.sandbox or self._config.defaults.sandbox
        self._policy.check_sandbox(sandbox)
        approval = call.approval_policy or self._config.defaults.approval_policy
        self._policy.check_approval_policy(approval)
        self._check_cancelled(run)

        lease = _RunLease(self._registry)
        # R3: workspace-write executions hold the workspace lock for the
        # whole lease; the thread lock joins as soon as the thread id is
        # known, before the turn is submitted.
        if sandbox == "workspace-write":
            lease.acquire_workspace(cwd)
        try:
            backend = await self._get_backend()
            run.backend_ref = backend
            run.runtime_generation = backend.generation
            self._check_cancelled(run)

            # R4: new-thread resolution is call > bridge default; both may be
            # None, in which case the runtime resolves the cwd's own config.
            model = call.model if call.model is not None else self._config.defaults.model
            effort = call.effort if call.effort is not None else self._config.defaults.effort
            if model is not None:
                await self._validate_model_with_refresh(backend, model)
                if effort is not None:
                    catalog = await self._get_catalog(backend)
                    catalog.validate_effort(model, effort)
            self._check_cancelled(run)

            run.state = RUN_STARTING_THREAD
            snapshot = await self._start_thread_checked(
                backend,
                StartThreadRequest(
                    cwd=cwd,
                    model=model,
                    effort=effort,
                    sandbox_mode=sandbox,
                    approval_policy=approval,
                    base_instructions=call.base_instructions,
                    developer_instructions=call.developer_instructions,
                    config=_thread_config(call, effort),
                ),
                expected_sandbox=sandbox,
                expected_approval=approval,
                expected_cwd=cwd,
            )
            self._check_cancelled(run)
            run.thread_id = snapshot.thread_id
            lease.acquire_thread(snapshot.thread_id)

            # Phase 2 (R4/F1): the runtime-confirmed settings are
            # authoritative and are ALWAYS validated against the catalog and
            # the operator allowlist before the turn is submitted.
            effective_model, effective_effort = await self._finalize_new_settings(
                backend, snapshot, requested_model=model, requested_effort=effort
            )
            run.submitted_model, run.submitted_effort = effective_model, effective_effort
            self._registry.upsert(_record_from_snapshot(snapshot, state=THREAD_ACTIVE))
            self._registry.update_settings(
                snapshot.thread_id,
                model=effective_model,
                effort=effective_effort,
            )
            try:
                return await self._run_turn(
                    run, backend, call.prompt, effective_model, effective_effort, progress
                )
            finally:
                self._registry.set_state(
                    snapshot.thread_id,
                    THREAD_UNKNOWN if run.result_uncertain else THREAD_IDLE,
                )
        finally:
            self._settle_lease(run, lease)

    async def _finalize_new_settings(
        self,
        backend: CodexBackend,
        snapshot: ThreadSnapshot,
        *,
        requested_model: str | None,
        requested_effort: str | None,
    ) -> tuple[str, str | None]:
        """Validate the runtime-confirmed settings for a new thread.

        F1: the effective model is validated *unconditionally* — the catalog
        is fetched (refreshing once when needed) and the operator allowlist
        is enforced even when the model came from the runtime's per-cwd
        config. A missing catalog or an unknown model is a rejection, never
        a validation skip.
        """
        effective_model = snapshot.model
        if not effective_model:
            raise BridgeError(
                code=RUNTIME_MISMATCH,
                message="runtime did not confirm a model for the new thread",
                retryable=False,
                may_have_side_effects=False,
                thread_id=snapshot.thread_id,
            )
        if requested_model is not None and effective_model != requested_model:
            raise BridgeError(
                code=RUNTIME_MISMATCH,
                message=(
                    "runtime confirmed a different model than requested "
                    f"(requested {requested_model!r}, got {effective_model!r})"
                ),
                retryable=False,
                may_have_side_effects=False,
                thread_id=snapshot.thread_id,
            )
        await self._validate_model_with_refresh(backend, effective_model)
        # effort: requested > bridge default > runtime-confirmed value
        effective_effort = (
            requested_effort
            if requested_effort is not None
            else (
                self._config.defaults.effort
                if self._config.defaults.effort is not None
                else snapshot.reasoning_effort
            )
        )
        if effective_effort is not None:
            catalog = await self._get_catalog(backend)
            catalog.validate_effort(effective_model, effective_effort)
        return effective_model, effective_effort

    # --- execution: continuation ----------------------------------------------------

    async def _execute_reply(
        self, run: ActiveRun, call: CodexReplyCall, progress: ProgressSink | None
    ) -> dict[str, Any]:
        thread_id = call.thread_id
        record = self._registry.get(thread_id)
        if record is not None and record.state == THREAD_ACTIVE:
            raise BridgeError(
                code=THREAD_BUSY,
                message=f"thread {thread_id} already has an active turn; v0.1 does not steer",
                retryable=True,
                thread_id=thread_id,
            )
        lease = _RunLease(self._registry)
        # R3: reply holds the thread lock for the whole lease; a
        # workspace-write resume also holds the workspace lock on the
        # confirmed cwd before the turn is submitted.
        lease.acquire_thread(thread_id)
        try:
            record = self._registry.ensure(thread_id)
            if record.state == THREAD_ACTIVE:
                raise BridgeError(
                    code=THREAD_BUSY,
                    message=f"thread {thread_id} is active in this process",
                    retryable=True,
                    thread_id=thread_id,
                )
            self._check_cancelled(run)
            # R2: the same execution policy as new calls — sandbox and
            # approval come from startup defaults and must pass the allowlists.
            sandbox = self._config.defaults.sandbox
            approval = self._config.defaults.approval_policy
            self._policy.check_sandbox(sandbox)
            self._policy.check_approval_policy(approval)
            backend = await self._get_backend()
            run.backend_ref = backend
            run.runtime_generation = backend.generation
            self._check_cancelled(run)

            # F5: the cwd adopted *before* resuming — from thread metadata
            # for unknown threads, from the cached record for known ones —
            # is the reference the resume response must confirm, and the
            # key any workspace lock is held on.
            expected_cwd: str | None = record.cwd
            if record.state == THREAD_UNKNOWN:
                # Unloaded thread: read metadata and cwd first (design 7.2).
                meta = await backend.read_thread(thread_id)
                expected_cwd = meta.cwd
                record.cwd = meta.cwd
                record.model = meta.model
                record.effort = meta.reasoning_effort
                if meta.cwd:
                    self._policy.check_workspace(meta.cwd)
                self._check_cancelled(run)

            if sandbox == "workspace-write":
                lock_cwd = expected_cwd or self._config.defaults.cwd
                if not lock_cwd:
                    raise BridgeError(
                        code=RUNTIME_MISMATCH,
                        message=(
                            "cannot determine the workspace for a write "
                            "execution; refusing to resume"
                        ),
                        retryable=False,
                        thread_id=thread_id,
                    )
                lease.acquire_workspace(lock_cwd)

            snapshot = await self._resume_thread_checked(
                backend,
                thread_id,
                ResumeThreadRequest(
                    sandbox_mode=sandbox,
                    approval_policy=approval,
                ),
                expected_sandbox=sandbox,
            )
            self._check_cancelled(run)
            # R7/F5: the runtime-confirmed cwd is policy-checked and must
            # match the pre-resume reference for known AND unknown threads.
            self._confirm_resume_cwd(snapshot, expected_cwd=expected_cwd)
            record.cwd = snapshot.cwd
            record.sandbox_mode = snapshot.sandbox_mode
            # Design 6.4: the resume response is the basis for model/effort —
            # never the bridge startup defaults.
            if snapshot.model is not None:
                record.model = snapshot.model
            if snapshot.reasoning_effort is not None:
                record.effort = snapshot.reasoning_effort

            # R4 (continuation): call explicit > confirmed thread state.
            # Unknown stays unknown (omitted), never filled with startup
            # defaults.
            model = call.model if call.model is not None else record.model
            effort = call.effort if call.effort is not None else record.effort
            if model is not None:
                await self._validate_model_with_refresh(backend, model)
                if effort is not None:
                    catalog = await self._get_catalog(backend)
                    catalog.validate_effort(model, effort)
            self._check_cancelled(run)
            run.submitted_model, run.submitted_effort = model, effort
            self._registry.update_settings(
                thread_id, model=model, effort=effort, sandbox_mode=snapshot.sandbox_mode
            )
            record.state = THREAD_ACTIVE
            try:
                return await self._run_turn(run, backend, call.prompt, model, effort, progress)
            finally:
                self._registry.set_state(
                    thread_id, THREAD_UNKNOWN if run.result_uncertain else THREAD_IDLE
                )
        finally:
            self._settle_lease(run, lease)

    def _confirm_resume_cwd(self, snapshot: ThreadSnapshot, *, expected_cwd: str | None) -> None:
        if not snapshot.cwd:
            raise BridgeError(
                code=RUNTIME_MISMATCH,
                message="runtime did not confirm a working directory on resume",
                retryable=False,
                thread_id=snapshot.thread_id,
            )
        self._policy.check_workspace(snapshot.cwd)
        if expected_cwd is not None:
            from pathlib import Path

            if Path(snapshot.cwd).resolve() != Path(expected_cwd).resolve():
                raise BridgeError(
                    code=RUNTIME_MISMATCH,
                    message=(
                        "resume confirmed a different working directory than "
                        f"the thread's recorded one (recorded {expected_cwd!r}, "
                        f"resumed {snapshot.cwd!r}); refusing to continue"
                    ),
                    retryable=False,
                    thread_id=snapshot.thread_id,
                )

    # --- turn execution shared tail ---------------------------------------------------

    async def _run_turn(
        self,
        run: ActiveRun,
        backend: CodexBackend,
        prompt: str,
        model: str | None,
        effort: str | None,
        progress: ProgressSink | None,
    ) -> dict[str, Any]:
        limits = self._config.limits
        run.state = RUN_STARTING_TURN
        run.deadline = time.monotonic() + limits.turn_timeout_seconds
        self._check_cancelled(run)
        # F3: mark the submission as *sent* — from here on, the run owns a
        # possibly-started turn and its cleanup must await a terminal or a
        # confirmed stop. Before this point, cancellation never owes a stop.
        run.turn_submission_started = True
        try:
            receipt = await backend.start_turn(
                StartTurnRequest(
                    thread_id=run.thread_id or "",
                    prompt=prompt,
                    model=model,
                    effort=effort,
                )
            )
        except BridgeError as exc:
            # Start outcome unknown: the turn may or may not exist — never
            # resend, and keep the stop obligation (R1).
            if exc.code in _UNKNOWN_OUTCOME_CODES:
                run.result_uncertain = True
                # F2/G1: assured only when the stop of THIS run's runtime is
                # confirmed; a failed stop leaves the lease retained and the
                # bridge poisoned.
                if await self._stop_backend(backend):
                    run.runtime_stop_assured = True
                raise BridgeError(
                    code=EXECUTION_STATE_UNKNOWN,
                    message=(
                        "turn start result is unknown (transport or timeout "
                        "failure); the turn is not resent automatically and the "
                        "runtime was stopped"
                    ),
                    retryable=False,
                    may_have_side_effects=True,
                    thread_id=run.thread_id,
                    internal=exc.internal,
                ) from exc
            raise
        run.turn_id = receipt.turn_id
        run.state = RUN_RUNNING
        if run.cancellation_requested:
            # Start acknowledgement arrived after cancellation: interrupt the
            # late-known turn instead of abandoning it (design 9.1 / CT-14).
            try:
                await backend.interrupt_turn(run.thread_id or "", run.turn_id)
            except BridgeError as exc:
                _logger.warning("late interrupt failed: %s", exc.code)

        reducer = TurnResultReducer()
        # One persistent event pump per run: no per-iteration wait_for, so a
        # turn timeout never abandons extra notification waiters.
        pump = asyncio.create_task(
            self._pump_turn_events(run, backend, reducer, progress),
            name=f"pump-{run.turn_id}",
        )
        try:
            try:
                await asyncio.wait_for(
                    asyncio.shield(pump),
                    timeout=max(0.0, (run.deadline or time.monotonic()) - time.monotonic()),
                )
            except TimeoutError:
                run.result_uncertain = True
                raise BridgeError(
                    code=TURN_TIMEOUT,
                    message=(
                        f"turn exceeded {limits.turn_timeout_seconds:.0f}s; "
                        "interrupt initiated; side effects may exist"
                    ),
                    retryable=False,
                    may_have_side_effects=True,
                    thread_id=run.thread_id,
                    turn_id=run.turn_id,
                ) from None
        except BridgeError:
            # R1: confirm the turn terminal (the interrupt should produce it)
            # or stop the owning runtime before answering.
            await self._settle_after_failure(run, backend)
            raise
        finally:
            pump.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

        outcome = reducer.finalize()
        if not run.turn_terminal_confirmed or outcome.status != "completed":
            if run.cancellation_requested and outcome.status == "interrupted":
                # Cancelled request: no result is sent for it.
                raise asyncio.CancelledError
            # R8/F4: the pump only returns on a *valid* terminal, so reaching
            # here without a confirmed completed status means interrupted
            # (without our cancellation) or failed — never a success.
            if outcome.status == "interrupted":
                raise BridgeError(
                    code=TURN_INTERRUPTED,
                    message=(
                        "turn was interrupted before completing; no result is "
                        "available (side effects may exist)"
                    ),
                    retryable=False,
                    may_have_side_effects=True,
                    thread_id=run.thread_id,
                    turn_id=run.turn_id,
                )
            raise BridgeError(
                code=TURN_FAILED,
                message=(
                    "turn ended without a confirmed completion; treating the "
                    "turn as failed (side effects may exist)"
                ),
                retryable=False,
                may_have_side_effects=True,
                thread_id=run.thread_id,
                turn_id=run.turn_id,
            )
        text = outcome.final_text if outcome.final_text is not None else ""
        envelope = build_success_envelope(
            thread_id=run.thread_id or "",
            text=text,
            turn_id=run.turn_id,
            status=outcome.to_meta_status(),
            requested_model=run.requested_model,
            requested_effort=run.requested_effort,
            max_result_bytes=limits.max_result_bytes,
        )
        _logger.info(
            "turn completed",
            extra={
                "thread_id": run.thread_id,
                "turn_id": run.turn_id,
                "status": outcome.status,
                "duration_s": round(time.monotonic() - run.started_at, 3),
            },
        )
        return envelope

    async def _settle_after_failure(self, run: ActiveRun, backend: CodexBackend) -> None:
        """R1: after a turn-level failure, interrupt and confirm the terminal
        event, or stop the owning runtime."""
        if run.turn_id is None:
            return
        if run.backend_ref is not None and run.backend_ref is not backend:
            _logger.warning("runtime replaced since the turn started; skipping interrupt")
            return
        try:
            await asyncio.wait_for(
                backend.interrupt_turn(run.thread_id or "", run.turn_id),
                timeout=self._config.limits.rpc_timeout_seconds,
            )
        except (BridgeError, TimeoutError) as exc:
            # F2: a hung interrupt surfaces as a raw TimeoutError here — it
            # must fall through to the stop obligation, not escape it.
            _logger.warning("post-failure interrupt failed: %r", exc)
        # We are inside the execution task; the pump sets turn_terminal_event.
        grace = self._config.limits.interrupt_grace_seconds
        deadline = time.monotonic() + grace
        while not run.turn_terminal_event.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if not self._run_settled_safe(run):
            _logger.error(
                "turn terminal not confirmed within grace (%.2fs); stopping the run's runtime",
                grace,
            )
            if await self._stop_backend(run.backend_ref):
                run.runtime_stop_assured = True

    async def _pump_turn_events(
        self,
        run: ActiveRun,
        backend: CodexBackend,
        reducer: TurnResultReducer,
        progress: ProgressSink | None,
    ) -> None:
        """Consume routed events for the run's turn until turn/completed.

        Runs as one persistent task so timeouts interrupt nothing: the pump
        ends when the terminal event (possibly produced by the interrupt)
        arrives, or when the runtime dies.
        """
        assert run.turn_id is not None
        while True:
            event = await backend.next_turn_event(run.turn_id)
            reducer.observe(event.method, event.payload)
            if progress is not None:
                await progress.ping(f"event:{event.method}")
            if event.method == "turn/completed" and reducer.outcome.valid_terminal:
                # F4: only a parseable terminal with a known status confirms
                # the end and releases the notification route. Malformed or
                # non-terminal (e.g. inProgress) payloads leave the run
                # unconfirmed; the deadline/stop path then takes over.
                run.turn_terminal_event.set()
                run.turn_terminal_confirmed = True
                await backend.release_turn(run.turn_id)
                return

    # --- thread start/resume with confirmation checks -------------------------------

    async def _start_thread_checked(
        self,
        backend: CodexBackend,
        request: StartThreadRequest,
        *,
        expected_sandbox: str,
        expected_approval: str,
        expected_cwd: str | None = None,
    ) -> ThreadSnapshot:
        try:
            snapshot = await backend.start_thread(request)
        except BridgeError as exc:
            if exc.code in _UNKNOWN_OUTCOME_CODES:
                # R1: unknown start ⇒ stop this run's runtime before
                # answering (the result is not claimed assured on failure).
                await self._stop_backend(backend)
                raise BridgeError(
                    code=EXECUTION_STATE_UNKNOWN,
                    message=(
                        "thread start result is unknown; not resending the same "
                        "start and a runtime stop was attempted"
                    ),
                    retryable=False,
                    may_have_side_effects=False,
                    internal=exc.internal,
                ) from exc
            raise
        self._confirm_snapshot(
            snapshot,
            expected_sandbox=expected_sandbox,
            expected_approval=expected_approval,
            expected_cwd=expected_cwd,
        )
        return snapshot

    async def _resume_thread_checked(
        self,
        backend: CodexBackend,
        thread_id: str,
        request: ResumeThreadRequest,
        *,
        expected_sandbox: str,
    ) -> ThreadSnapshot:
        try:
            snapshot = await backend.resume_thread(thread_id, request)
        except BridgeError as exc:
            if exc.code in _UNKNOWN_OUTCOME_CODES:
                await self._stop_backend()
                raise BridgeError(
                    code=EXECUTION_STATE_UNKNOWN,
                    message=(
                        "thread resume result is unknown; not resending and the runtime was stopped"
                    ),
                    retryable=False,
                    may_have_side_effects=False,
                    thread_id=thread_id,
                    internal=exc.internal,
                ) from exc
            raise
        # Design 7.2: the runtime must confirm exactly the sandbox the
        # bridge requested; membership in allowed_sandboxes alone would let
        # a workspace-write-persisted thread keep elevated permissions.
        if snapshot.sandbox_mode != expected_sandbox or (
            snapshot.approval_policy is not None and snapshot.approval_policy != "never"
        ):
            raise BridgeError(
                code=RUNTIME_MISMATCH,
                message=(
                    "resumed thread cannot run under the bridge's safety settings "
                    f"(requested sandbox={expected_sandbox!r}, got "
                    f"{snapshot.sandbox_mode!r}, approval={snapshot.approval_policy!r}); "
                    "refusing to start a turn"
                ),
                retryable=False,
                may_have_side_effects=False,
                thread_id=thread_id,
            )
        return snapshot

    def _confirm_snapshot(
        self,
        snapshot: ThreadSnapshot,
        *,
        expected_sandbox: str,
        expected_approval: str,
        expected_cwd: str | None = None,
    ) -> None:
        if expected_cwd is not None and snapshot.cwd:
            from pathlib import Path

            if Path(snapshot.cwd).resolve() != Path(expected_cwd).resolve():
                raise BridgeError(
                    code=RUNTIME_MISMATCH,
                    message=(
                        "runtime confirmed a different working directory than "
                        f"requested (requested {expected_cwd!r}, got {snapshot.cwd!r})"
                    ),
                    retryable=False,
                    may_have_side_effects=False,
                    thread_id=snapshot.thread_id,
                )
        if snapshot.sandbox_mode != expected_sandbox:
            raise BridgeError(
                code=RUNTIME_MISMATCH,
                message=(
                    "runtime confirmed a different sandbox than requested "
                    f"(requested {expected_sandbox!r}, got {snapshot.sandbox_mode!r})"
                ),
                retryable=False,
                may_have_side_effects=False,
                thread_id=snapshot.thread_id,
            )
        if snapshot.approval_policy is not None and snapshot.approval_policy != expected_approval:
            raise BridgeError(
                code=RUNTIME_MISMATCH,
                message=(
                    "runtime confirmed a different approval policy than requested "
                    f"(requested {expected_approval!r}, got {snapshot.approval_policy!r})"
                ),
                retryable=False,
                may_have_side_effects=False,
                thread_id=snapshot.thread_id,
            )

    # --- shutdown -----------------------------------------------------------------

    async def aclose(self) -> None:
        """Stop accepting calls, interrupt the active turn, close the runtime.

        Bounded by ``shutdown_grace + rpc_timeout + 5`` seconds and shielded
        so a cancellation racing the MCP teardown cannot skip runtime close.
        G3: an unconfirmed stop or a budget overrun is raised to the caller
        (never reported as a healthy shutdown).
        """
        self._closed = True
        limits = self._config.limits
        budget = limits.shutdown_grace_seconds + limits.rpc_timeout_seconds + 5
        task = asyncio.create_task(self._aclose_inner(), name="bridge-shutdown")
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=budget)
        except TimeoutError:
            _logger.error("shutdown cleanup exceeded budget; forcing stop")
            task.cancel()
            raise RuntimeError(
                "shutdown cleanup exceeded its budget; runtime state unconfirmed"
            ) from None
        except asyncio.CancelledError:
            # Teardown racing loop shutdown: give the bounded cleanup a short
            # chance to finish, then let the cancellation propagate — never
            # report success for an unfinished shutdown (G3).
            try:
                await asyncio.shield(asyncio.wait_for(task, timeout=3.0))
            except TimeoutError, asyncio.CancelledError, Exception:  # noqa: BLE001
                pass
            raise

    async def _aclose_inner(self) -> None:
        limits = self._config.limits
        run = self._active
        if run is not None and not run.terminal_event.is_set():
            run.cancellation_requested = True
            try:
                await self._cancel_run_and_wait(run, grace=limits.shutdown_grace_seconds)
            except Exception:  # noqa: BLE001
                _logger.exception("shutdown cancel failed")
        if not await self._stop_backend():
            # G3: the stop is unconfirmed — surface it to the shutdown caller
            # (SIGTERM path exits non-zero; lifespan teardown records it).
            raise RuntimeError("runtime stop could not be confirmed during shutdown")

    async def _cancel_run_and_wait(self, run: ActiveRun, *, grace: float) -> None:
        """Cancellation/shutdown cleanup: interrupt, then confirm the turn
        terminal or stop the runtime (R1)."""
        if not run.turn_submission_started:
            # F3: nothing was submitted — no terminal will ever arrive.
            return
        if run.thread_id is not None:
            self._registry.set_state(run.thread_id, THREAD_INTERRUPTING)
        limits = self._config.limits
        backend = run.backend_ref or await self._backend_or_none()
        if backend is not None and run.turn_id is not None:
            current = await self._backend_or_none()
            if current is not None and backend is not current:
                _logger.warning(
                    "owning runtime was replaced; skipping interrupt to avoid "
                    "cross-runtime delivery"
                )
            else:
                try:
                    await asyncio.wait_for(
                        backend.interrupt_turn(run.thread_id or "", run.turn_id),
                        timeout=limits.rpc_timeout_seconds,
                    )
                except (BridgeError, TimeoutError) as exc:
                    _logger.warning("interrupt during cancel failed: %r", exc)
        await self._wait_terminal_or_stop(run, grace)


def _sanitize(exc: BaseException) -> str:
    """Exception class + short message only (reprs can embed input values)."""
    text = str(exc).splitlines()[0][:200] if str(exc) else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# --- helpers ----------------------------------------------------------------------


def _thread_config(call: CodexCall, effort: str | None) -> dict[str, str] | None:
    """Design 6.2: optionally mirror effort into thread config."""
    config: dict[str, str] = {}
    if call.compact_prompt is not None:
        config["compact_prompt"] = call.compact_prompt
    if effort is not None:
        config["model_reasoning_effort"] = effort
    return config or None


def _record_from_snapshot(snapshot: ThreadSnapshot, *, state: str):
    from .registry import ThreadRecord

    return ThreadRecord(
        snapshot.thread_id,
        state=state,
        cwd=snapshot.cwd,
        model=snapshot.model,
        effort=snapshot.reasoning_effort,
        sandbox_mode=snapshot.sandbox_mode,
    )
