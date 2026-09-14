"""Turn notification aggregation and MCP result envelopes.

The reducer mirrors the SDK's own result aggregation (``_run.py``): the
final answer prefers ``final_answer``-phase assistant messages (last one
wins, scanning from the end) with phase-unspecified assistant messages as
fallback. Commentary, reasoning, progress, and command output never enter
the answer, and command output is not accumulated (CT-19).

Envelope builders produce plain dicts; ``server.py`` maps them onto
``mcp.types.CallToolResult``. The first text block stays human-readable and
the second serializes ``structuredContent`` for clients that do not expose
that field to the model. Diagnostics live under ``_meta``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import OUTPUT_LIMIT_EXCEEDED, TURN_FAILED, BridgeError

META_NAMESPACE = "codex-app-mcp"

#: The only statuses that prove the runtime ended the turn (F4). Anything
#: else arriving as turn/completed is treated as malformed/non-terminal.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})

# --- Reducer ------------------------------------------------------------------


@dataclass(slots=True)
class TurnOutcome:
    """Aggregated terminal state for one turn."""

    completed: bool = False
    status: str | None = None
    final_text: str | None = None
    turn_error: str | None = None
    last_error_notification: str | None = None
    last_retryable_error: bool | None = None
    events_seen: int = 0
    usage_seen: bool = False
    malformed_terminal: bool = False
    #: a turn/completed payload with a KNOWN terminal status (completed /
    #: failed / interrupted) and a readable turn structure was observed
    valid_terminal: bool = False

    def to_meta_status(self) -> str:
        return self.status or "unknown"


class TurnResultReducer:
    """Collects one turn's routed events into a :class:`TurnOutcome`."""

    def __init__(self) -> None:
        self._outcome = TurnOutcome()
        self._fallback_text: str | None = None

    @property
    def outcome(self) -> TurnOutcome:
        return self._outcome

    def observe(self, method: str, payload: Any) -> None:
        self._outcome.events_seen += 1
        if method == "item/completed":
            self._observe_item_completed(payload)
            return
        if method == "error":
            self._observe_error(payload)
            return
        if method == "thread/tokenUsage/updated":
            self._outcome.usage_seen = True
            return
        if method == "turn/completed":
            self._observe_turn_completed(payload)
            return
        # Everything else (deltas, started, plan, ...) is intentionally not
        # accumulated.

    # -- internals ------------------------------------------------------------

    def _observe_item_completed(self, payload: Any) -> None:
        item = getattr(payload, "item", None)
        root = getattr(item, "root", item) if item is not None else None
        if root is None:
            return
        if getattr(root, "type", None) != "agentMessage":
            # Command output, file changes, reasoning, etc. are not stored.
            return
        text = getattr(root, "text", None)
        if not isinstance(text, str):
            return
        phase = getattr(root, "phase", None)
        phase_value = getattr(phase, "value", phase)
        if phase_value == "final_answer":
            # Last final_answer wins (scan order end-overwrites).
            self._outcome.final_text = text
        elif phase_value is None:
            # SDK semantics: scanning from the end, the *last* phase-unspecified
            # assistant message is the fallback.
            self._fallback_text = text

    def _observe_error(self, payload: Any) -> None:
        error = getattr(payload, "error", None)
        message = getattr(error, "message", None) if error is not None else None
        self._outcome.last_error_notification = message if isinstance(message, str) else str(error)
        will_retry = getattr(payload, "will_retry", None)
        self._outcome.last_retryable_error = bool(will_retry) if will_retry is not None else None

    def _observe_turn_completed(self, payload: Any) -> None:
        turn = getattr(payload, "turn", None)
        status = getattr(turn, "status", None) if turn is not None else None
        status_value = getattr(status, "value", status)
        if (
            turn is None
            or not isinstance(status_value, str)
            or status_value not in _TERMINAL_STATUSES
        ):
            # A turn/completed-shaped event we cannot parse, or whose status
            # is unknown / non-terminal (e.g. inProgress), is never treated
            # as a completion or as evidence the runtime stopped the turn
            # (external reviews R8 / F4).
            self._outcome.malformed_terminal = True
            return
        self._outcome.valid_terminal = True
        status = getattr(turn, "status", None)
        status_value = getattr(status, "value", status)
        self._outcome.completed = True
        self._outcome.status = str(status_value) if status_value else "unknown"
        if self._outcome.final_text is None:
            self._outcome.final_text = self._fallback_text
        turn_error = getattr(turn, "error", None)
        if turn_error is not None:
            message = getattr(turn_error, "message", None)
            self._outcome.turn_error = message if isinstance(message, str) else str(turn_error)

    # -- completion --------------------------------------------------------

    def finalize(self) -> TurnOutcome:
        """Return the outcome; raise for failed turns per design 7.3."""
        outcome = self._outcome
        if not outcome.completed:
            return outcome
        if outcome.status == "failed":
            raise BridgeError(
                code=TURN_FAILED,
                message=(
                    "turn failed: "
                    f"{outcome.turn_error or outcome.last_error_notification or 'unknown error'}"
                ),
                retryable=False,
                may_have_side_effects=True,
            )
        return outcome


# --- Envelopes ------------------------------------------------------------------


def _namespace_meta(fields: dict[str, Any]) -> dict[str, Any]:
    return {"_meta": {META_NAMESPACE: fields}}


def _compatible_text_content(text: str, structured: dict[str, Any]) -> list[dict[str, str]]:
    """Return readable text plus the MCP structured-content fallback."""
    return [
        {"type": "text", "text": text},
        {"type": "text", "text": json.dumps(structured, ensure_ascii=False)},
    ]


def build_success_envelope(
    *,
    thread_id: str,
    text: str,
    turn_id: str | None,
    status: str,
    requested_model: str | None,
    requested_effort: str | None,
    max_result_bytes: int,
) -> dict[str, Any]:
    """Build a success result. Oversized output is rejected, never cut."""
    size = len(text.encode("utf-8"))
    if size > max_result_bytes:
        raise BridgeError(
            code=OUTPUT_LIMIT_EXCEEDED,
            message=(
                f"final answer is {size} bytes; the result limit is "
                f"{max_result_bytes} bytes. The turn has already run and side "
                "effects may exist"
            ),
            retryable=False,
            may_have_side_effects=True,
            thread_id=thread_id,
            turn_id=turn_id,
        )
    meta: dict[str, Any] = {
        "turnId": turn_id,
        "status": status,
        "requestedModel": requested_model,
        "requestedEffort": requested_effort,
    }
    structured = {"threadId": thread_id, "content": text}
    return {
        "content": _compatible_text_content(text, structured),
        "structuredContent": structured,
        "isError": False,
        **_namespace_meta(meta),
    }


def build_error_envelope(
    error: BridgeError,
    *,
    requested_model: str | None = None,
    requested_effort: str | None = None,
) -> dict[str, Any]:
    """Build an error result (thread ID only when established)."""
    structured: dict[str, Any] = {"content": error.message}
    if error.thread_id is not None:
        structured["threadId"] = error.thread_id
    meta: dict[str, Any] = {
        "code": error.code,
        "retryable": error.retryable,
        "mayHaveSideEffects": error.may_have_side_effects,
    }
    if error.turn_id is not None:
        meta["turnId"] = error.turn_id
    if requested_model is not None:
        meta["requestedModel"] = requested_model
    if requested_effort is not None:
        meta["requestedEffort"] = requested_effort
    return {
        "content": _compatible_text_content(error.message, structured),
        "structuredContent": structured,
        "isError": True,
        **_namespace_meta(meta),
    }
