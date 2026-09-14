"""MCP server wiring: low-level v2 Server, tools/list, call_tool (design 4, 5).

Protocol/envelope problems and tool-level failures are kept apart: known
tools always return a ``CallToolResult`` built by the bridge (never rely on
exception auto-conversion); unknown tools and malformed envelopes are
protocol errors handled by the SDK (design section 11).
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import mcp.types as mcp_types

from . import __version__
from .config import BridgeConfig
from .contracts import CODEX_REPLY_TOOL, CODEX_TOOL, parse_codex_call, parse_codex_reply_call
from .coordinator import ExecutionCoordinator, ProgressSink
from .errors import BridgeError, internal_error
from .results import build_error_envelope

_logger = logging.getLogger("codex_app_mcp.server")

TOOLS_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "tools.json"


def load_tool_definitions(path: Path = TOOLS_SCHEMA_PATH) -> list[mcp_types.Tool]:
    """Load tools/list definitions from the authoritative packaged schema."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    tools: list[mcp_types.Tool] = []
    for entry in data["tools"]:
        annotations = entry.get("annotations")
        tools.append(
            mcp_types.Tool(
                name=entry["name"],
                description=entry.get("description"),
                input_schema=entry["inputSchema"],
                output_schema=entry.get("outputSchema"),
                annotations=(mcp_types.ToolAnnotations(**annotations) if annotations else None),
            )
        )
    return tools


class CodexAppMcpServer:
    """Owns the coordinator and adapts it to the low-level MCP server."""

    def __init__(self, config: BridgeConfig, coordinator: ExecutionCoordinator) -> None:
        self._config = config
        self._coordinator = coordinator
        self._tools = load_tool_definitions()

    @property
    def coordinator(self) -> ExecutionCoordinator:
        return self._coordinator

    @asynccontextmanager
    async def lifespan(self, server: Any):
        _logger.info("bridge starting", extra={"version": __version__})
        try:
            yield {"config": self._config}
        finally:
            # R9: cancellation or exceptions propagating through the lifespan
            # must still run the bounded, shielded coordinator cleanup.
            _logger.info("bridge lifespan exiting; shutting coordinator down")
            await self._coordinator.aclose()

    # --- handlers -----------------------------------------------------------

    async def on_list_tools(
        self, ctx: Any, params: mcp_types.PaginatedRequestParams | None
    ) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=list(self._tools))

    async def on_call_tool(
        self, ctx: Any, params: mcp_types.CallToolRequestParams
    ) -> mcp_types.CallToolResult:
        from mcp.shared.exceptions import MCPError

        name = params.name
        if name not in (CODEX_TOOL, CODEX_REPLY_TOOL):
            # Unknown tool: protocol-level error, not a tool result
            # (design section 11).
            raise MCPError(code=mcp_types.INVALID_PARAMS, message=f"unknown tool: {name}")
        arguments = params.arguments
        progress = self._make_progress_sink(ctx)
        request_id = getattr(ctx, "request_id", None)
        session_id = _session_id(ctx)
        requested: dict[str, Any] = {}
        try:
            if name == CODEX_TOOL:
                call = parse_codex_call(
                    arguments, max_input_bytes=self._config.limits.max_input_bytes
                )
                requested = {
                    "requested_model": call.model,
                    "requested_effort": call.effort,
                }
                envelope = await self._coordinator.run_codex(
                    call,
                    session_id=session_id,
                    mcp_request_id=str(request_id) if request_id is not None else None,
                    progress=progress,
                )
            else:
                call = parse_codex_reply_call(
                    arguments, max_input_bytes=self._config.limits.max_input_bytes
                )
                requested = {
                    "requested_model": call.model,
                    "requested_effort": call.effort,
                }
                envelope = await self._coordinator.run_reply(
                    call,
                    session_id=session_id,
                    mcp_request_id=str(request_id) if request_id is not None else None,
                    progress=progress,
                )
        except BridgeError as error:
            try:
                _logger.info(
                    "tool call rejected",
                    extra={"tool": name, **error.log_fields()},
                )
            except Exception:  # noqa: BLE001 - logging must never break replies
                pass
            return _to_call_tool_result(build_error_envelope(error, **requested))
        except Exception as exc:
            # Unexpected internal failure: isError result. Diagnostics stay
            # in stderr logs; exception text is sanitized so prompt
            # fragments cannot leak through reprs (review m-11).
            _logger.error("tool call crashed: %s", _sanitize_exception(exc))
            error = internal_error(
                "internal bridge failure; see bridge stderr logs",
                internal=_sanitize_exception(exc),
            )
            return _to_call_tool_result(build_error_envelope(error, **requested))
        return _to_call_tool_result(envelope)

    # --- progress -------------------------------------------------------------

    def _make_progress_sink(self, ctx: Any) -> ProgressSink | None:
        """Standard progress only when the client asked for it (FR-07)."""
        meta = getattr(ctx, "meta", None)
        progress_token = None
        if isinstance(meta, dict):
            progress_token = meta.get("progressToken")
        elif meta is not None:
            progress_token = getattr(meta, "progress_token", None)
        if progress_token is None:
            return None
        session = getattr(ctx, "session", None)

        async def sender(progress: float, message: str) -> None:
            if session is None:
                return
            await session.send_progress_notification(
                progress_token=progress_token, progress=progress, message=message
            )

        return ProgressSink(sender, min_interval=self._config.limits.progress_min_interval_seconds)


def _sanitize_exception(exc: BaseException) -> str:
    """Exception class + short message only: exception reprs can embed
    argument values (pydantic shows input_value), so full reprs must not
    reach logs by default (design section 10.3)."""
    text = str(exc).splitlines()[0][:200] if str(exc) else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _session_id(ctx: Any) -> str | None:
    session = getattr(ctx, "session", None)
    getter = getattr(session, "session_id", None)
    if callable(getter):
        try:
            value = getter()
            return str(value) if value else None
        except Exception:  # noqa: BLE001
            return None
    return None


def _to_call_tool_result(envelope: dict[str, Any]) -> mcp_types.CallToolResult:
    content = [
        mcp_types.TextContent(type="text", text=block["text"])
        for block in envelope.get("content", [])
    ]
    return mcp_types.CallToolResult(
        _meta=envelope.get("_meta"),
        content=content,
        structured_content=envelope.get("structuredContent"),
        is_error=envelope.get("isError", False),
    )


def build_lowlevel_server(app: CodexAppMcpServer) -> Any:
    """Construct the official low-level v2 Server bound to our handlers."""
    from mcp.server.lowlevel import Server

    return Server(
        "codex-app-mcp",
        version=__version__,
        lifespan=app.lifespan,
        on_list_tools=app.on_list_tools,
        on_call_tool=app.on_call_tool,
    )


async def serve_stdio(config: BridgeConfig) -> None:
    """Run the bridge over stdio until EOF or SIGTERM (design section 12).

    The stdio transport's reader only ends on stdin EOF, so a SIGTERM-driven
    cancellation could hang inside the transport. Instead, the signal starts
    a bounded graceful shutdown (stop accepting, interrupt the active turn,
    close the runtime) and then terminates the process explicitly.
    """
    import os
    import signal

    from mcp.server.stdio import stdio_server

    from .backend.codex_sdk import CodexSdkBackend
    from .backend.interface import BackendOptions
    from .registry import ThreadRegistry

    # Test-only launch injection (contract tests drive the serve subprocess
    # against the fake App Server). Doctor flags it when set; production
    # setups never define it.
    launch_override: tuple[str, ...] | None = None
    raw_launch = os.environ.get("CODEX_APP_MCP_TEST_LAUNCH_ARGS")
    if raw_launch:
        try:
            parsed_launch = json.loads(raw_launch)
        except json.JSONDecodeError as exc:
            raise BridgeError(
                code="VALIDATION_ERROR",
                message=f"CODEX_APP_MCP_TEST_LAUNCH_ARGS is not valid JSON: {exc}",
            ) from exc
        if not isinstance(parsed_launch, list) or not all(
            isinstance(item, str) for item in parsed_launch
        ):
            raise BridgeError(
                code="VALIDATION_ERROR",
                message="CODEX_APP_MCP_TEST_LAUNCH_ARGS must be a JSON array of strings",
            )
        launch_override = tuple(parsed_launch)

    def backend_factory(limits) -> CodexSdkBackend:  # noqa: ANN001
        return CodexSdkBackend(
            BackendOptions(
                startup_timeout=limits.startup_timeout_seconds,
                rpc_timeout=limits.rpc_timeout_seconds,
                child_env={},
                cwd=None,
                launch_args_override=launch_override,
            )
        )

    coordinator = ExecutionCoordinator(
        backend_factory,
        config,
        bridge_cwd=str(Path.cwd()),
        registry=ThreadRegistry(),
    )
    app = CodexAppMcpServer(config, coordinator)
    server = build_lowlevel_server(app)
    init_options = server.create_initialization_options(
        notification_options=None, experimental_capabilities={}
    )

    shutdown_started = False

    async def _graceful_then_exit() -> None:
        limits = config.limits
        # Same budget formula as coordinator.aclose() so both layers agree on
        # how long shutdown may take (R9 budget consistency).
        budget = limits.shutdown_grace_seconds + limits.rpc_timeout_seconds + 5
        exit_code = 0
        try:
            await asyncio.wait_for(coordinator.aclose(), timeout=budget)
        except BaseException:  # noqa: BLE001 - shutdown must always exit
            exit_code = 1
            _logger.exception("graceful shutdown failed; exiting with failure status")
        # stdout is the MCP wire and fd state is transport-owned; a plain
        # exit() would block on the stdio reader. aclose() has either closed
        # the runtime or exhausted its bounded cleanup by this point.
        os._exit(exit_code)

    def _on_terminate_signal() -> None:
        nonlocal shutdown_started
        if shutdown_started:
            return
        shutdown_started = True
        asyncio.create_task(_graceful_then_exit(), name="sigterm-shutdown")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, _on_terminate_signal)
        except NotImplementedError, ValueError:  # pragma: no cover
            pass

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options, raise_exceptions=False)
