"""Shared test fixtures: scripted fake App Server runtimes and bridges."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from codex_app_mcp.backend.codex_sdk import CodexSdkBackend
from codex_app_mcp.backend.interface import BackendOptions
from codex_app_mcp.config import (
    BridgeConfig,
    DefaultsConfig,
    LimitsConfig,
    LoggingConfig,
    PolicyConfig,
    RuntimeConfig,
)
from codex_app_mcp.coordinator import ExecutionCoordinator
from codex_app_mcp.registry import ThreadRegistry
from codex_app_mcp.server import CodexAppMcpServer, build_lowlevel_server
from tests.contract.harness import DEFAULT_MODELS, FAKE_SERVER_SCRIPT, FakeRuntime

__all__ = ["make_runtime", "make_bridge"]


@pytest.fixture
async def make_runtime(tmp_path: Path):
    created: list[CodexSdkBackend] = []

    async def _make(
        scenario: dict,
        *,
        rpc_timeout: float = 10.0,
        startup_timeout: float = 10.0,
        start: bool = True,
    ) -> FakeRuntime:
        scenario_path = tmp_path / f"scenario-{time.monotonic_ns()}.json"
        journal_path = tmp_path / f"journal-{time.monotonic_ns()}.jsonl"
        scenario.setdefault("models", DEFAULT_MODELS)
        scenario_path.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
        backend = CodexSdkBackend(
            BackendOptions(
                startup_timeout=startup_timeout,
                rpc_timeout=rpc_timeout,
                launch_args_override=(
                    sys.executable,
                    str(FAKE_SERVER_SCRIPT),
                    str(scenario_path),
                    str(journal_path),
                ),
            )
        )
        created.append(backend)
        runtime = FakeRuntime(backend=backend, journal_path=journal_path)
        if start:
            await backend.start_runtime()
        return runtime

    yield _make

    for backend in created:
        try:
            await asyncio.wait_for(backend.close(), timeout=10.0)
        except Exception:  # noqa: BLE001 - teardown must not mask failures
            pass


class Bridge:
    """An in-process bridge (low-level server + client session) for tests."""

    def __init__(
        self,
        *,
        config: BridgeConfig,
        client: ClientSession,
        coordinator: ExecutionCoordinator,
        journal_path: Path,
        workspace: Path,
    ) -> None:
        self.config = config
        self.client = client
        self.coordinator = coordinator
        self.journal_path = journal_path
        self.workspace = workspace

    def journal(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if not self.journal_path.exists():
            return events
        with open(self.journal_path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    events.append(json.loads(line))
        return events

    def requests(self, method: str) -> list[dict[str, Any]]:
        return [
            e for e in self.journal() if e.get("event") == "request" and e.get("method") == method
        ]

    async def call(self, name: str, arguments: dict[str, Any] | None = None):
        return await self.client.call_tool(name, arguments or {})

    async def wait_for_turns(self, count: int, *, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.requests("turn/start")) >= count:
                return
            await asyncio.sleep(0.02)
        raise TimeoutError(f"only {len(self.requests('turn/start'))}/{count} turns started")


def _test_config(workspace: Path, **overrides: Any) -> BridgeConfig:
    defaults = overrides.pop(
        "defaults",
        DefaultsConfig(
            model="gpt-5.6-terra",
            effort="medium",
            cwd=str(workspace),
            sandbox="read-only",
        ),
    )
    policy = overrides.pop(
        "policy",
        PolicyConfig(
            allowed_roots=(str(workspace),),
            allowed_models=(),
            allowed_sandboxes=("read-only", "workspace-write"),
        ),
    )
    limits = overrides.pop(
        "limits",
        LimitsConfig(
            startup_timeout_seconds=10.0,
            rpc_timeout_seconds=10.0,
            turn_timeout_seconds=30.0,
            interrupt_grace_seconds=5.0,
            shutdown_grace_seconds=5.0,
        ),
    )
    return BridgeConfig(
        runtime=RuntimeConfig(),
        defaults=defaults,
        policy=policy,
        limits=limits,
        logging=LoggingConfig(level="WARNING"),
    )


@pytest.fixture
async def make_bridge(tmp_path: Path):
    @asynccontextmanager
    async def _make(
        scenario: dict, *, config: BridgeConfig | None = None, keep_coordinator: bool = False
    ):
        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        effective = config or _test_config(workspace)
        scenario_path = tmp_path / f"scenario-{time.monotonic_ns()}.json"
        journal_path = tmp_path / f"journal-{time.monotonic_ns()}.jsonl"
        scenario.setdefault("models", DEFAULT_MODELS)
        scenario_path.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")

        def backend_factory(limits: LimitsConfig) -> CodexSdkBackend:
            return CodexSdkBackend(
                BackendOptions(
                    startup_timeout=limits.startup_timeout_seconds,
                    rpc_timeout=limits.rpc_timeout_seconds,
                    launch_args_override=(
                        sys.executable,
                        str(FAKE_SERVER_SCRIPT),
                        str(scenario_path),
                        str(journal_path),
                    ),
                )
            )

        coordinator = ExecutionCoordinator(
            backend_factory,
            effective,
            bridge_cwd=str(tmp_path),
            registry=ThreadRegistry(codex_home=tmp_path / "codex-home"),
        )
        app = CodexAppMcpServer(effective, coordinator)
        server = build_lowlevel_server(app)
        async with create_client_server_memory_streams() as (
            client_streams,
            server_streams,
        ):
            init_options = server.create_initialization_options()
            server_task = asyncio.create_task(
                server.run(
                    server_streams[0], server_streams[1], init_options, raise_exceptions=False
                )
            )
            try:
                async with ClientSession(client_streams[0], client_streams[1]) as session:
                    await session.initialize()
                    yield Bridge(
                        config=effective,
                        client=session,
                        coordinator=coordinator,
                        journal_path=journal_path,
                        workspace=workspace,
                    )
            finally:
                server_task.cancel()
                try:
                    await asyncio.wait_for(server_task, timeout=10.0)
                except asyncio.CancelledError, TimeoutError:
                    pass
                if not keep_coordinator:
                    try:
                        await asyncio.wait_for(coordinator.aclose(), timeout=10.0)
                    except Exception:  # noqa: BLE001
                        pass

    yield _make
