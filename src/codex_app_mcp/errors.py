"""Error taxonomy and safe external messages (design section 11).

Every tool-level failure is represented as :class:`BridgeError` carrying a
stable machine-readable ``code``, a user-facing message safe to return to the
MCP client, and explicit ``retryable`` / ``may_have_side_effects`` flags.

Internal diagnostics (SDK exception text, stack details) are recorded via
``internal`` and logged to stderr; they are never returned verbatim to the
MCP client.
"""

from __future__ import annotations

import dataclasses
from typing import Any

# --- Stable error codes (design section 11) ---------------------------------

VALIDATION_ERROR = "VALIDATION_ERROR"
CONFLICTING_ARGUMENTS = "CONFLICTING_ARGUMENTS"
CONFIG_KEY_DENIED = "CONFIG_KEY_DENIED"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
UNSUPPORTED_EFFORT = "UNSUPPORTED_EFFORT"
MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
WORKSPACE_DENIED = "WORKSPACE_DENIED"
UNSUPPORTED_APPROVAL_POLICY = "UNSUPPORTED_APPROVAL_POLICY"
THREAD_BUSY = "THREAD_BUSY"
SERVER_BUSY_CODE = "SERVER_BUSY"
UNSUPPORTED_SERVER_REQUEST = "UNSUPPORTED_SERVER_REQUEST"
TURN_FAILED = "TURN_FAILED"
TURN_TIMEOUT = "TURN_TIMEOUT"
TURN_INTERRUPTED = "TURN_INTERRUPTED"
INTERRUPT_FAILED = "INTERRUPT_FAILED"
OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
INPUT_LIMIT_EXCEEDED = "INPUT_LIMIT_EXCEEDED"
RUNTIME_DISCONNECTED = "RUNTIME_DISCONNECTED"
RUNTIME_MISMATCH = "RUNTIME_MISMATCH"
RUNTIME_STOP_UNCONFIRMED = "RUNTIME_STOP_UNCONFIRMED"
RUNTIME_BUSY = "RUNTIME_BUSY"
EXECUTION_STATE_UNKNOWN = "EXECUTION_STATE_UNKNOWN"
STARTUP_TIMEOUT = "STARTUP_TIMEOUT"
RPC_TIMEOUT = "RPC_TIMEOUT"
INTERNAL_ERROR = "INTERNAL_ERROR"

#: Errors that must never be auto-retried because a turn may have started.
#: (Kept as an explicit allow-list for errors.md and future gating.)
NON_RETRYABLE_WITH_SIDE_EFFECTS = frozenset(
    {
        TURN_FAILED,
        TURN_TIMEOUT,
        TURN_INTERRUPTED,
        INTERRUPT_FAILED,
        OUTPUT_LIMIT_EXCEEDED,
        RUNTIME_DISCONNECTED,
        EXECUTION_STATE_UNKNOWN,
        STARTUP_TIMEOUT,
        RPC_TIMEOUT,
        INTERNAL_ERROR,
    }
)


@dataclasses.dataclass(kw_only=True)
class BridgeError(Exception):
    """A tool-level failure with a stable external contract.

    Attributes:
        code: Stable machine-readable error code (constants above).
        message: User-facing message; safe to return to the MCP client.
        retryable: Whether a *new* call (not a blind turn resend) may succeed.
            Never means "resend the same turn unconditionally".
        may_have_side_effects: True when the turn may have started or files
            may already have been modified before the failure.
        thread_id: Thread ID when already established (never fabricated).
        turn_id: Turn ID when already established (never fabricated).
        internal: Internal diagnostics for stderr logging only.
    """

    code: str
    message: str
    retryable: bool = False
    may_have_side_effects: bool = False
    thread_id: str | None = None
    turn_id: str | None = None
    internal: str | None = None

    def __str__(self) -> str:  # pragma: no cover - debug convenience
        return f"[{self.code}] {self.message}"

    def log_fields(self) -> dict[str, Any]:
        """Fields safe and useful for stderr diagnostics logging.

        Keys avoid logging-module reserved names (``message`` would raise
        "Attempt to overwrite 'message' in LogRecord").
        """
        return {
            "code": self.code,
            "error_message": self.message,
            "retryable": self.retryable,
            "may_have_side_effects": self.may_have_side_effects,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "internal": self.internal,
        }


def internal_error(message: str, *, internal: str | None = None) -> BridgeError:
    return BridgeError(
        code=INTERNAL_ERROR,
        message=message,
        retryable=False,
        may_have_side_effects=False,
        internal=internal,
    )
