"""Tool input contracts: packaged tools.json validation plus semantic rules.

The low-level MCP server does not auto-validate tool arguments, so the
bridge validates explicitly (design sections 2.3, 5). Structural rules
mirror ``codex_app_mcp/schemas/tools.json``; cross-field rules (alias equality, duplicate
normalization, null rejection, whitespace-only prompts, no implicit trim)
are implemented here on top.

Validation cases are pinned directly in ``tests/unit/test_contracts.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import (
    CONFLICTING_ARGUMENTS,
    INPUT_LIMIT_EXCEEDED,
    VALIDATION_ERROR,
    BridgeError,
)

CODEX_TOOL = "codex"
CODEX_REPLY_TOOL = "codex-reply"

_ALLOWED_CODEX_FIELDS = frozenset(
    {
        "prompt",
        "model",
        "effort",
        "cwd",
        "approval-policy",
        "sandbox",
        "config",
        "base-instructions",
        "developer-instructions",
        "compact-prompt",
    }
)
_ALLOWED_REPLY_FIELDS = frozenset({"prompt", "threadId", "conversationId", "model", "effort"})
_ALLOWED_CONFIG_KEYS = frozenset({"model", "model_reasoning_effort", "compact_prompt"})


@dataclass(frozen=True, slots=True)
class CodexCall:
    """Normalized ``codex`` tool call (config duplicates already merged)."""

    prompt: str
    model: str | None = None
    effort: str | None = None
    cwd: str | None = None
    approval_policy: str | None = None
    sandbox: str | None = None
    base_instructions: str | None = None
    developer_instructions: str | None = None
    compact_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class CodexReplyCall:
    """Normalized ``codex-reply`` tool call (threadId alias resolved)."""

    prompt: str
    thread_id: str
    model: str | None = None
    effort: str | None = None


def _check_object(arguments: Any, tool: str) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise BridgeError(
            code=VALIDATION_ERROR,
            message=f"{tool}: arguments must be a JSON object",
        )
    return arguments


def _reject_nulls(arguments: dict[str, Any], tool: str) -> None:
    """JSON ``null`` is rejected: omission means inheritance (design 5.1)."""
    for key, value in arguments.items():
        if value is None:
            raise BridgeError(
                code=VALIDATION_ERROR,
                message=(
                    f"{tool}: argument {key!r} must not be null; omit it to "
                    "inherit, or pass an explicit value to reset"
                ),
            )


def _require_text(
    arguments: dict[str, Any],
    key: str,
    tool: str,
    *,
    required: bool,
    non_empty: bool = True,
) -> str | None:
    if key not in arguments:
        if required:
            raise BridgeError(
                code=VALIDATION_ERROR, message=f"{tool}: missing required argument {key!r}"
            )
        return None
    value = arguments[key]
    if not isinstance(value, str):
        raise BridgeError(
            code=VALIDATION_ERROR, message=f"{tool}: argument {key!r} must be a string"
        )
    if non_empty and not value:
        raise BridgeError(
            code=VALIDATION_ERROR, message=f"{tool}: argument {key!r} must not be empty"
        )
    return value


def _reject_unknown_fields(arguments: dict[str, Any], allowed: frozenset[str], tool: str) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise BridgeError(
            code=VALIDATION_ERROR,
            message=f"{tool}: unknown arguments {unknown} (schema is closed)",
        )


def _check_prompt(prompt: str, tool: str, max_input_bytes: int) -> None:
    if prompt.isspace():
        raise BridgeError(
            code=VALIDATION_ERROR,
            message=f"{tool}: prompt must not be whitespace-only",
        )
    size = len(prompt.encode("utf-8"))
    if size > max_input_bytes:
        raise BridgeError(
            code=INPUT_LIMIT_EXCEEDED,
            message=(f"{tool}: prompt is {size} bytes; the input limit is {max_input_bytes} bytes"),
        )


def _merge_config_duplicate(
    *,
    direct: str | None,
    config_value: str | None,
    direct_name: str,
    config_name: str,
    tool: str,
) -> str | None:
    """Merge same-meaning arguments; conflicting values are rejected."""
    if direct is None or config_value is None:
        return direct if direct is not None else config_value
    if direct != config_value:
        raise BridgeError(
            code=CONFLICTING_ARGUMENTS,
            message=(
                f"{tool}: {direct_name!r}={direct!r} conflicts with "
                f"config.{config_name}={config_value!r}; pass one consistent value"
            ),
        )
    return direct


def _config_value_text(key: str, value: object, tool: str) -> str:
    """Config values must be strings; compact_prompt may be empty (schema
    has no minLength for it), model/effort keys may not."""
    if not isinstance(value, str):
        raise BridgeError(
            code=VALIDATION_ERROR,
            message=f"{tool}: config.{key} must be a string",
        )
    if key != "compact_prompt" and not value:
        raise BridgeError(
            code=VALIDATION_ERROR,
            message=f"{tool}: config.{key} must be a non-empty string",
        )
    return value


def _check_total_input_size(parts: list[str], tool: str, max_input_bytes: int) -> None:
    """Design 9.2 limits the *total* input, not just the prompt."""
    total = sum(len(part.encode("utf-8")) for part in parts)
    if total > max_input_bytes:
        raise BridgeError(
            code=INPUT_LIMIT_EXCEEDED,
            message=(
                f"{tool}: total input is {total} bytes; the input limit is {max_input_bytes} bytes"
            ),
        )


def parse_codex_call(arguments: Any, *, max_input_bytes: int) -> CodexCall:
    """Validate and normalize a ``codex`` tool call."""
    tool = CODEX_TOOL
    args = _check_object(arguments, tool)
    _reject_nulls(args, tool)
    _reject_unknown_fields(args, _ALLOWED_CODEX_FIELDS, tool)

    prompt = _require_text(args, "prompt", tool, required=True)
    assert prompt is not None
    _check_prompt(prompt, tool, max_input_bytes)

    model = _require_text(args, "model", tool, required=False)
    effort = _require_text(args, "effort", tool, required=False)
    cwd = _require_text(args, "cwd", tool, required=False)

    approval_policy = _require_text(args, "approval-policy", tool, required=False)
    if approval_policy is not None and approval_policy != "never":
        from .errors import UNSUPPORTED_APPROVAL_POLICY

        raise BridgeError(
            code=UNSUPPORTED_APPROVAL_POLICY,
            message=(
                f"{tool}: approval-policy {approval_policy!r} is not supported in "
                "v0.1; only 'never'"
            ),
        )

    sandbox = _require_text(args, "sandbox", tool, required=False)
    if sandbox is not None and sandbox not in ("read-only", "workspace-write"):
        raise BridgeError(
            code=VALIDATION_ERROR,
            message=(f"{tool}: sandbox must be 'read-only' or 'workspace-write' (got {sandbox!r})"),
        )

    config_value = args.get("config")
    config: dict[str, str] = {}
    if config_value is not None:
        if not isinstance(config_value, dict):
            raise BridgeError(code=VALIDATION_ERROR, message=f"{tool}: 'config' must be an object")
        denied = sorted(set(config_value) - _ALLOWED_CONFIG_KEYS)
        if denied:
            from .errors import CONFIG_KEY_DENIED

            raise BridgeError(
                code=CONFIG_KEY_DENIED,
                message=(
                    f"{tool}: config keys {denied} are not allowed; permitted "
                    f"keys: {sorted(_ALLOWED_CONFIG_KEYS)}"
                ),
            )
        for key, value in config_value.items():
            config[key] = _config_value_text(key, value, tool)

    base_instructions = _require_text(
        args, "base-instructions", tool, required=False, non_empty=False
    )
    developer_instructions = _require_text(
        args, "developer-instructions", tool, required=False, non_empty=False
    )
    compact_prompt_direct = _require_text(
        args, "compact-prompt", tool, required=False, non_empty=False
    )

    # Normalize same-meaning duplicates (conflicts already rejected).
    merged_model = _merge_config_duplicate(
        direct=model,
        config_value=config.get("model"),
        direct_name="model",
        config_name="model",
        tool=tool,
    )
    merged_effort = _merge_config_duplicate(
        direct=effort,
        config_value=config.get("model_reasoning_effort"),
        direct_name="effort",
        config_name="model_reasoning_effort",
        tool=tool,
    )
    merged_compact = _merge_config_duplicate(
        direct=compact_prompt_direct,
        config_value=config.get("compact_prompt"),
        direct_name="compact-prompt",
        config_name="compact_prompt",
        tool=tool,
    )

    _check_total_input_size(
        [
            prompt,
            base_instructions or "",
            developer_instructions or "",
            merged_compact or "",
            *(config.values()),
        ],
        tool,
        max_input_bytes,
    )

    return CodexCall(
        prompt=prompt,
        model=merged_model,
        effort=merged_effort,
        cwd=cwd,
        approval_policy=approval_policy,
        sandbox=sandbox,
        base_instructions=base_instructions,
        developer_instructions=developer_instructions,
        compact_prompt=merged_compact,
    )


def parse_codex_reply_call(arguments: Any, *, max_input_bytes: int) -> CodexReplyCall:
    """Validate and normalize a ``codex-reply`` tool call."""
    tool = CODEX_REPLY_TOOL
    args = _check_object(arguments, tool)
    _reject_nulls(args, tool)
    _reject_unknown_fields(args, _ALLOWED_REPLY_FIELDS, tool)

    prompt = _require_text(args, "prompt", tool, required=True)
    assert prompt is not None
    _check_prompt(prompt, tool, max_input_bytes)

    thread_id = _require_text(args, "threadId", tool, required=False)
    conversation_id = _require_text(args, "conversationId", tool, required=False)
    if thread_id is None and conversation_id is None:
        raise BridgeError(
            code=VALIDATION_ERROR,
            message=f"{tool}: either threadId or conversationId is required",
        )
    if thread_id is not None and conversation_id is not None and thread_id != conversation_id:
        raise BridgeError(
            code=VALIDATION_ERROR,
            message=(
                f"{tool}: threadId and conversationId differ "
                f"({thread_id!r} vs {conversation_id!r}); they are aliases and "
                "must match"
            ),
        )
    effective_thread_id = thread_id if thread_id is not None else conversation_id
    assert effective_thread_id is not None

    model = _require_text(args, "model", tool, required=False)
    effort = _require_text(args, "effort", tool, required=False)

    return CodexReplyCall(
        prompt=prompt,
        thread_id=effective_thread_id,
        model=model,
        effort=effort,
    )
