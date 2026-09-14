"""Bridge-owned asynchronous backend interface (design sections 4 and 13).

This is the *bridge's own* interface, deliberately decoupled from the Codex
SDK surface. ``backend/codex_sdk.py`` is the only module permitted to import
``openai_codex``. The operations are limited to: start-runtime, models,
start-thread, resume-thread, read-thread, start-turn, events, interrupt,
close.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..errors import BridgeError

# --- Runtime states (design section 8.2) ------------------------------------

RUNTIME_STOPPED = "STOPPED"
#: A close attempt failed; the runtime's stopped state is UNCONFIRMED and
#: the backend instance must not be reused (review G2).
RUNTIME_STOP_FAILED = "STOP_FAILED"
RUNTIME_STARTING = "STARTING"
RUNTIME_READY = "READY"
RUNTIME_DEGRADED = "DEGRADED"
RUNTIME_STOPPING = "STOPPING"

# --- Sandbox / approval modes ------------------------------------------------

SANDBOX_READ_ONLY = "read-only"
SANDBOX_WORKSPACE_WRITE = "workspace-write"
SANDBOX_DANGER_FULL_ACCESS = "danger-full-access"
SANDBOX_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BackendOptions:
    """Options for the SDK backend adapter.

    ``launch_args_override`` and ``codex_bin`` exist for verified injection
    points (contract tests, doctor); the default resolves the runtime pinned
    by the SDK distribution.
    """

    startup_timeout: float = 30.0
    rpc_timeout: float = 30.0
    client_name: str = "codex-app-mcp"
    client_title: str = "Codex App MCP Bridge"
    codex_bin: str | None = None
    launch_args_override: tuple[str, ...] | None = None
    child_env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeInfo:
    """Result of a successful runtime start."""

    generation: int
    server_name: str | None
    server_version: str | None


@dataclass(frozen=True, slots=True)
class ModelCatalogEntry:
    """One model from the runtime catalog (paginated fetch merges pages)."""

    model_id: str
    display_name: str
    supported_reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str | None
    hidden: bool
    is_default: bool


@dataclass(frozen=True, slots=True)
class ThreadSnapshot:
    """Confirmed thread settings as reported by the runtime."""

    thread_id: str
    cwd: str
    model: str | None
    reasoning_effort: str | None
    sandbox_mode: str
    approval_policy: str | None
    ephemeral: bool | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class StartThreadRequest:
    """Parameters for ``thread/start``."""

    cwd: str
    model: str | None
    effort: str | None
    sandbox_mode: str
    approval_policy: str
    base_instructions: str | None = None
    developer_instructions: str | None = None
    config: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ResumeThreadRequest:
    """Override settings applied while resuming a saved thread.

    ``model``/``effort`` are ``None`` to inherit the thread's persisted
    settings; sandbox and approval policy are always re-applied by the
    bridge and must not stay elevated.
    """

    model: str | None = None
    effort: str | None = None
    sandbox_mode: str | None = None
    approval_policy: str | None = None


@dataclass(frozen=True, slots=True)
class StartTurnRequest:
    """Parameters for ``turn/start``."""

    thread_id: str
    prompt: str
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True, slots=True)
class TurnReceipt:
    """Acknowledgement of ``turn/start`` — not a completion result."""

    thread_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class BackendEvent:
    """One notification routed for a turn (payload is SDK-typed or mapping)."""

    turn_id: str
    method: str
    payload: object


class UnsupportedServerRequestError(Exception):
    """Raised by the approval handler for requests the bridge cannot answer.

    Raised inside the SDK reader thread so the SDK fails pending calls and
    the supervisor can stop the runtime (design section 10.1).
    """

    def __init__(self, method: str) -> None:
        super().__init__(f"unsupported server request: {method}")
        self.method = method


class RuntimeUnavailableError(BridgeError):
    """The runtime is not running (stopped or never started)."""


class CodexBackend(ABC):
    """Asynchronous interface over one owned App Server runtime.

    One backend instance owns one runtime process (design section 4.2).
    All coroutine methods are safe to call from the event loop; blocking SDK
    calls are offloaded to dedicated executors.
    """

    @abstractmethod
    async def start_runtime(self) -> RuntimeInfo:
        """Start (or verify) the runtime and initialize the protocol.

        Raises :class:`BridgeError` with ``STARTUP_TIMEOUT`` when startup
        does not complete within the configured timeout; the child process
        is stopped before raising.
        """

    @abstractmethod
    async def close(self) -> None:
        """Stop the runtime and release workers. Idempotent."""

    @abstractmethod
    async def list_models(self, *, include_hidden: bool = True) -> list[ModelCatalogEntry]:
        """Return the model catalog, following pagination."""

    @abstractmethod
    async def start_thread(self, request: StartThreadRequest) -> ThreadSnapshot:
        """Start a new thread and return the runtime-confirmed settings."""

    @abstractmethod
    async def resume_thread(self, thread_id: str, request: ResumeThreadRequest) -> ThreadSnapshot:
        """Resume a saved thread and return the runtime-confirmed settings."""

    @abstractmethod
    async def read_thread(self, thread_id: str) -> ThreadSnapshot:
        """Read thread metadata (cwd, model, status) without resuming."""

    @abstractmethod
    async def start_turn(self, request: StartTurnRequest) -> TurnReceipt:
        """Start one turn. The acknowledgement is not a completion."""

    @abstractmethod
    async def next_turn_event(self, turn_id: str) -> BackendEvent:
        """Wait for the next routed event for ``turn_id``.

        Returns terminal events (``turn/completed``) without consuming
        further events. Raises when the runtime dies or the route closes.
        """

    @abstractmethod
    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        """Interrupt the given turn (best effort; bounded by rpc timeout)."""

    @abstractmethod
    async def release_turn(self, turn_id: str) -> None:
        """Release SDK-side notification routing for a finished turn."""

    @property
    @abstractmethod
    def generation(self) -> int:
        """Monotonic runtime generation; bumps on every successful start."""

    @property
    @abstractmethod
    def state(self) -> str:
        """One of RUNTIME_* constants."""

    @property
    @abstractmethod
    def last_unsupported_request(self) -> UnsupportedServerRequestError | None:
        """The unsupported server request that degraded the runtime, if any."""

    @property
    @abstractmethod
    def on_global_event(self) -> object:
        """Callback registry hook for global notifications (diagnostics)."""
