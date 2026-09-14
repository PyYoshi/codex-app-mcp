"""Independent review regressions for the supplied ZIP.

Run against unmodified source with PYTHONPATH=<project>/src.
No Codex SDK, MCP SDK, network, authentication, or real model is used.
These assert the desired contracts and intentionally FAIL on the reviewed snapshot.
The async backend is a deterministic test double of backend/interface.py.
The lifespan check executes the original method extracted with AST (not a real MCP session).
"""

from __future__ import annotations

import ast
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import codex_app_mcp
from codex_app_mcp.backend.interface import (
    BackendEvent,
    ModelCatalogEntry,
    RuntimeInfo,
    ThreadSnapshot,
    TurnReceipt,
)
from codex_app_mcp.config import (
    BridgeConfig,
    DefaultsConfig,
    LimitsConfig,
    PolicyConfig,
    load_bridge_config,
)
from codex_app_mcp.contracts import CodexCall, CodexReplyCall
from codex_app_mcp.coordinator import ExecutionCoordinator
from codex_app_mcp.errors import BridgeError
from codex_app_mcp.registry import ThreadRegistry

MODEL = "test-model"
OTHER = "project-config-model"


def entry(name: str, default: bool = False):
    return ModelCatalogEntry(name, name, ("low", "medium", "high"), "medium", False, default)


class FakeBackend:
    """Deterministic implementation of the bridge-owned backend interface."""

    def __init__(self, cwd: Path, *, mode: str = "normal"):
        self.cwd = str(cwd)
        self.mode = mode
        self.generation = 1
        self.state = "STOPPED"
        self.last_unsupported_request = None
        self.on_global_event = None
        self.close_calls = 0
        self.list_calls = 0
        self.turn_calls = []
        self.thread_calls = []
        self.resume_calls = []
        self.interrupts = []
        self.running = set()
        self.queues = {}
        self.models = [entry(MODEL, True), entry(OTHER)]
        self.runtime_config_model = OTHER
        self.runtime_config_effort = "high"
        self.snapshot_model = MODEL
        self.snapshot_effort = "medium"
        self.resume_cwd = self.cwd
        self.catalog_entered = asyncio.Event()
        self.catalog_gate = None

    async def start_runtime(self):
        self.state = "READY"
        return RuntimeInfo(self.generation, "review-test-double", "not-a-real-runtime")

    async def close(self):
        self.close_calls += 1
        self.state = "STOPPED"
        self.running.clear()
        for queue in self.queues.values():
            queue.put_nowait(BridgeError(code="RUNTIME_DISCONNECTED", message="fake closed"))

    async def list_models(self, *, include_hidden=True):
        self.list_calls += 1
        self.catalog_entered.set()
        if self.catalog_gate is not None:
            await self.catalog_gate.wait()
        return self.models

    async def start_thread(self, request):
        self.thread_calls.append(request)
        self.snapshot_model = request.model or self.runtime_config_model
        self.snapshot_effort = request.effort or self.runtime_config_effort
        return ThreadSnapshot(
            f"thread-{len(self.thread_calls)}",
            request.cwd,
            self.snapshot_model,
            self.snapshot_effort,
            request.sandbox_mode,
            request.approval_policy,
        )

    async def read_thread(self, thread_id):
        return ThreadSnapshot(
            thread_id,
            self.cwd,
            self.snapshot_model,
            self.snapshot_effort,
            "unknown",
            None,
            status="idle",
        )

    async def resume_thread(self, thread_id, request):
        self.resume_calls.append(request)
        return ThreadSnapshot(
            thread_id,
            self.resume_cwd,
            self.snapshot_model,
            self.snapshot_effort,
            request.sandbox_mode,
            request.approval_policy,
        )

    def complete(self, thread_id, turn_id, status="completed"):
        q = self.queues[turn_id]
        q.put_nowait(
            BackendEvent(
                turn_id,
                "item/completed",
                NS(item=NS(type="agentMessage", text="test answer", phase="final_answer")),
            )
        )
        q.put_nowait(
            BackendEvent(
                turn_id, "turn/completed", NS(turn=NS(id=turn_id, status=status, error=None))
            )
        )

    async def start_turn(self, request):
        self.turn_calls.append(request)
        turn_id = f"turn-{len(self.turn_calls)}"
        self.queues[turn_id] = asyncio.Queue()
        self.running.add(turn_id)
        if self.mode == "start_timeout":
            self.state = "DEGRADED"
            raise BridgeError(code="RPC_TIMEOUT", message="ack lost")
        if self.mode == "disconnect":
            self.queues[turn_id].put_nowait(
                BridgeError(code="RUNTIME_DISCONNECTED", message="connection lost")
            )
        elif self.mode == "external_interrupt":
            self.complete(request.thread_id, turn_id, "interrupted")
        elif self.mode == "malformed_terminal":
            self.queues[turn_id].put_nowait(
                BackendEvent(turn_id, "turn/completed", NS(params={"unrecognized": True}))
            )
        elif self.mode != "ignore_interrupt":
            self.complete(request.thread_id, turn_id)
        return TurnReceipt(request.thread_id, turn_id)

    async def next_turn_event(self, turn_id):
        event = await self.queues[turn_id].get()
        if isinstance(event, BaseException):
            raise event
        if event.method == "turn/completed":
            self.running.discard(turn_id)
        return event

    async def interrupt_turn(self, thread_id, turn_id):
        self.interrupts.append((thread_id, turn_id))
        if self.mode == "disconnect":
            raise BridgeError(code="RUNTIME_DISCONNECTED", message="connection lost")
        # Deliberately acknowledge interrupt without completion: a required failure case.

    async def release_turn(self, turn_id):
        pass


def setup(tmp_path, backend=None, **defaults):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    backend = backend or FakeBackend(workspace)
    config = BridgeConfig(
        defaults=DefaultsConfig(model=MODEL, effort="medium", cwd=str(workspace), **defaults),
        policy=PolicyConfig(allowed_roots=(str(workspace),)),
        limits=LimitsConfig(
            turn_timeout_seconds=0.03,
            interrupt_grace_seconds=0.01,
            shutdown_grace_seconds=0.02,
            rpc_timeout_seconds=0.1,
        ),
    )
    registry = ThreadRegistry(codex_home=tmp_path / "codex-home")
    coordinator = ExecutionCoordinator(
        lambda _: backend, config, bridge_cwd=str(workspace), registry=registry
    )
    return coordinator, backend, config, registry


def run(coro):
    return asyncio.run(coro)


def test_R1_timeout_must_stop_runtime_before_return(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        backend.mode = "ignore_interrupt"
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert result["_meta"]["codex-app-mcp"]["code"] == "TURN_TIMEOUT"
            assert backend.close_calls >= 1 and not backend.running, (
                f"timeout returned with close_calls={backend.close_calls}, "
                f"running={backend.running}, slot_free={coordinator._active.terminal_event.is_set()}"
            )
        finally:
            await coordinator.aclose()

    run(scenario())


def test_R1_unknown_start_must_close_degraded_runtime(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        backend.mode = "start_timeout"
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert result["_meta"]["codex-app-mcp"]["code"] == "EXECUTION_STATE_UNKNOWN"
            assert backend.close_calls >= 1, (
                f"uncertain start returned with close_calls={backend.close_calls}, state={backend.state}"
            )
        finally:
            await coordinator.aclose()

    run(scenario())


def test_R2_reply_must_not_bypass_sandbox_allowlist(tmp_path):
    async def scenario():
        coordinator, backend, config, registry = setup(tmp_path, sandbox="workspace-write")
        path = tmp_path / "bridge.toml"
        path.write_text(
            f'''[defaults]\nmodel="{MODEL}"\neffort="medium"\nsandbox="workspace-write"\n[policy]\nallowed_roots=["{backend.cwd}"]\nallowed_sandboxes=["read-only"]\n'''
        )
        try:
            config = load_bridge_config(path, env={})
        except BridgeError:
            return  # rejecting inconsistent startup config is an acceptable fix
        coordinator = ExecutionCoordinator(
            lambda _: backend, config, bridge_cwd=backend.cwd, registry=registry
        )
        try:
            result = await coordinator.run_reply(CodexReplyCall(prompt="test", thread_id="saved"))
            assert result["isError"] and not backend.turn_calls, (
                f"forbidden sandbox accepted: result={result['isError']}, "
                f"sandbox={backend.resume_calls[-1].sandbox_mode}, turns={len(backend.turn_calls)}"
            )
        finally:
            await coordinator.aclose()

    run(scenario())


def test_R3_reply_must_hold_workspace_write_lock(tmp_path):
    async def scenario():
        coordinator, backend, _, registry = setup(tmp_path, sandbox="workspace-write")
        try:
            # A different file descriptor holding flock is sufficient to simulate
            # another cooperating bridge, without a subprocess or network.
            with registry.workspace_lock(backend.cwd):
                result = await coordinator.run_reply(
                    CodexReplyCall(prompt="test", thread_id="saved")
                )
            assert result["isError"] and not backend.turn_calls, (
                f"workspace lock already held but reply started {len(backend.turn_calls)} turn(s)"
            )
        finally:
            await coordinator.aclose()

    run(scenario())


def test_R4_new_call_without_model_must_honor_runtime_config(tmp_path):
    async def scenario():
        _, backend, config, registry = setup(tmp_path)
        config = replace(config, defaults=replace(config.defaults, model=None, effort=None))
        coordinator = ExecutionCoordinator(
            lambda _: backend, config, bridge_cwd=backend.cwd, registry=registry
        )
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert not result["isError"]
            assert backend.turn_calls[-1].model == OTHER, (
                f"runtime/project config={OTHER}, catalog default={MODEL}, "
                f"submitted={backend.turn_calls[-1].model}"
            )
        finally:
            await coordinator.aclose()

    run(scenario())


def test_R4_reply_unknown_effort_must_not_reapply_startup_default(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        backend.snapshot_effort = None
        try:
            result = await coordinator.run_reply(CodexReplyCall(prompt="test", thread_id="saved"))
            assert not result["isError"]
            assert backend.turn_calls[-1].effort is None, (
                f"thread effort unknown but bridge default injected: {backend.turn_calls[-1].effort}"
            )
        finally:
            await coordinator.aclose()

    run(scenario())


def test_R5_cancel_before_turn_submission_must_not_start_turn(tmp_path):
    async def scenario():
        coordinator, backend, config, registry = setup(tmp_path)
        config = replace(config, limits=replace(config.limits, interrupt_grace_seconds=0.2))
        coordinator = ExecutionCoordinator(
            lambda _: backend, config, bridge_cwd=backend.cwd, registry=registry
        )
        backend.catalog_gate = asyncio.Event()
        task = asyncio.create_task(coordinator.run_codex(CodexCall(prompt="test")))
        try:
            await backend.catalog_entered.wait()
            task.cancel()
            for _ in range(50):
                if coordinator._active.cancellation_requested:
                    break
                await asyncio.sleep(0)
            assert coordinator._active.cancellation_requested
            backend.catalog_gate.set()
            with suppress(asyncio.CancelledError):
                await task
            assert not backend.turn_calls, (
                f"already cancelled during catalog lookup, but started {len(backend.turn_calls)} turn(s)"
            )
        finally:
            backend.catalog_gate.set()
            await coordinator.aclose()

    run(scenario())


def test_R6_transport_loss_after_start_must_mark_possible_side_effects(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        backend.mode = "disconnect"
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            meta = result["_meta"]["codex-app-mcp"]
            assert meta["code"] == "RUNTIME_DISCONNECTED"
            assert meta["mayHaveSideEffects"] is True, (
                f"turn acknowledged, then transport lost, but metadata={meta}"
            )
        finally:
            await coordinator.aclose()

    run(scenario())


def test_R7_resume_confirmed_cwd_must_be_policy_checked(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        backend.resume_cwd = str(outside)
        try:
            result = await coordinator.run_reply(CodexReplyCall(prompt="test", thread_id="saved"))
            assert result["isError"] and not backend.turn_calls, (
                f"metadata cwd={backend.cwd}, resumed cwd={outside}, "
                f"turn started={len(backend.turn_calls)}"
            )
        finally:
            await coordinator.aclose()

    run(scenario())


def test_R8_interrupted_turn_must_not_be_success(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        backend.mode = "external_interrupt"
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert result["isError"] is True, (
                f"interrupted completion returned isError={result['isError']}, "
                f"status={result['_meta']['codex-app-mcp']['status']}"
            )
        finally:
            await coordinator.aclose()

    run(scenario())


def test_R8_unknown_terminal_payload_must_not_be_empty_success(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        backend.mode = "malformed_terminal"
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert result["isError"] is True, f"unparsed turn/completed returned success: {result}"
        finally:
            await coordinator.aclose()

    run(scenario())


def test_R9_lifespan_cancellation_must_close_coordinator():
    # Execute the exact uploaded method without importing unavailable MCP.
    # This checks Python context-manager cleanup, not MCP integration.
    source = Path(codex_app_mcp.__file__).parent / "server.py"
    tree = ast.parse(source.read_text())
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CodexAppMcpServer"
    )
    fn = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    module = ast.Module(body=[fn], type_ignores=[])
    env = {
        "asynccontextmanager": asynccontextmanager,
        "Any": object,
        "_logger": logging.getLogger("review"),
        "__version__": "review",
    }
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), env)

    async def scenario():
        calls = []

        async def close():
            calls.append(True)

        instance = NS(_config=None, _coordinator=NS(aclose=close))
        with suppress(asyncio.CancelledError):
            async with env["lifespan"](instance, None):
                raise asyncio.CancelledError
        assert calls, "CancelledError at lifespan yield skipped coordinator.aclose()"

    run(scenario())


def test_R10_replaced_backend_must_refresh_catalog(tmp_path):
    async def scenario():
        coordinator, first, _, _ = setup(tmp_path)
        second = FakeBackend(Path(first.cwd))
        second.models = [entry(OTHER, True)]
        await coordinator._get_catalog(first)
        await coordinator._get_catalog(second)
        assert second.list_calls == 1, (
            f"different backend instances both generation=1; second model/list calls={second.list_calls}"
        )

    run(scenario())


def test_R3_new_turn_must_hold_its_thread_lock(tmp_path):
    async def scenario():
        coordinator, backend, _, registry = setup(tmp_path)
        original = backend.start_turn
        lock_was_held = False

        async def observe_start(request):
            nonlocal lock_was_held
            try:
                with registry.thread_lock(request.thread_id):
                    pass
            except BridgeError as exc:
                assert exc.code == "THREAD_BUSY"
                lock_was_held = True
            return await original(request)

        backend.start_turn = observe_start
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert not result["isError"]
            assert lock_was_held, "new thread turn starts without holding its advisory thread lock"
        finally:
            await coordinator.aclose()

    run(scenario())


def test_control_normal_success(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        try:
            result = await coordinator.run_codex(CodexCall(prompt="test"))
            assert not result["isError"]
            assert result["structuredContent"]["content"] == "test answer"
            assert len(backend.turn_calls) == 1
        finally:
            await coordinator.aclose()

    run(scenario())


def test_control_new_call_enforces_sandbox_policy(tmp_path):
    async def scenario():
        _, backend, config, registry = setup(tmp_path)
        config = replace(config, policy=replace(config.policy, allowed_sandboxes=("read-only",)))
        coordinator = ExecutionCoordinator(
            lambda _: backend, config, bridge_cwd=backend.cwd, registry=registry
        )
        try:
            result = await coordinator.run_codex(
                CodexCall(prompt="test", sandbox="workspace-write")
            )
            assert result["isError"]
            assert not backend.turn_calls
        finally:
            await coordinator.aclose()

    run(scenario())


def test_control_reply_enforces_thread_lock(tmp_path):
    async def scenario():
        coordinator, backend, _, registry = setup(tmp_path)
        try:
            with registry.thread_lock("saved"):
                result = await coordinator.run_reply(
                    CodexReplyCall(prompt="test", thread_id="saved")
                )
            assert result["isError"]
            assert result["_meta"]["codex-app-mcp"]["code"] == "THREAD_BUSY"
            assert not backend.turn_calls
        finally:
            await coordinator.aclose()

    run(scenario())


def test_control_reply_explicit_effort_forwarded(tmp_path):
    async def scenario():
        coordinator, backend, _, _ = setup(tmp_path)
        try:
            result = await coordinator.run_reply(
                CodexReplyCall(prompt="test", thread_id="saved", effort="high")
            )
            assert not result["isError"]
            assert backend.turn_calls[-1].effort == "high"
        finally:
            await coordinator.aclose()

    run(scenario())
