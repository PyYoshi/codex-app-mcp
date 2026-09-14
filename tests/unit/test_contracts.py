"""Unit tests for structural and cross-field tool contracts."""

from __future__ import annotations

import pytest

from codex_app_mcp.contracts import parse_codex_call, parse_codex_reply_call
from codex_app_mcp.errors import (
    CONFIG_KEY_DENIED,
    CONFLICTING_ARGUMENTS,
    UNSUPPORTED_APPROVAL_POLICY,
    VALIDATION_ERROR,
    BridgeError,
)

MAX_INPUT = 1024 * 1024


def _expect_error(fn, code: str) -> BridgeError:
    with pytest.raises(BridgeError) as excinfo:
        fn()
    assert excinfo.value.code == code, excinfo.value
    return excinfo.value


# --- codex --------------------------------------------------------------------


def test_new_minimal_valid():
    call = parse_codex_call({"prompt": "Summarize the repo."}, max_input_bytes=MAX_INPUT)
    assert call.prompt == "Summarize the repo."
    assert call.model is None and call.effort is None


def test_new_explicit_settings_valid():
    call = parse_codex_call(
        {
            "prompt": "p",
            "model": "gpt-5.6-terra",
            "effort": "high",
            "cwd": "/tmp",
            "sandbox": "workspace-write",
            "approval-policy": "never",
        },
        max_input_bytes=MAX_INPUT,
    )
    assert call.model == "gpt-5.6-terra"
    assert call.sandbox == "workspace-write"
    assert call.approval_policy == "never"


def test_new_legacy_config_valid():
    call = parse_codex_call(
        {"prompt": "p", "config": {"model_reasoning_effort": "high"}},
        max_input_bytes=MAX_INPUT,
    )
    assert call.effort == "high"


def test_new_unknown_field_invalid():
    _expect_error(
        lambda: parse_codex_call({"prompt": "p", "bogus": 1}, max_input_bytes=MAX_INPUT),
        VALIDATION_ERROR,
    )


def test_new_disallowed_config_invalid():
    _expect_error(
        lambda: parse_codex_call(
            {"prompt": "p", "config": {"sandbox": "workspace-write"}},
            max_input_bytes=MAX_INPUT,
        ),
        CONFIG_KEY_DENIED,
    )


def test_new_unsupported_approval_invalid():
    _expect_error(
        lambda: parse_codex_call(
            {"prompt": "p", "approval-policy": "on-request"},
            max_input_bytes=MAX_INPUT,
        ),
        UNSUPPORTED_APPROVAL_POLICY,
    )


def test_new_unrestricted_sandbox_invalid():
    _expect_error(
        lambda: parse_codex_call(
            {"prompt": "p", "sandbox": "danger-full-access"},
            max_input_bytes=MAX_INPUT,
        ),
        VALIDATION_ERROR,
    )


def test_new_null_effort_invalid():
    _expect_error(
        lambda: parse_codex_call({"prompt": "p", "effort": None}, max_input_bytes=MAX_INPUT),
        VALIDATION_ERROR,
    )


def test_new_empty_prompt_invalid():
    _expect_error(
        lambda: parse_codex_call({"prompt": ""}, max_input_bytes=MAX_INPUT),
        VALIDATION_ERROR,
    )
    _expect_error(
        lambda: parse_codex_call({"prompt": "   \n\t"}, max_input_bytes=MAX_INPUT),
        VALIDATION_ERROR,
    )


def test_new_conflicting_model_arguments():
    _expect_error(
        lambda: parse_codex_call(
            {"prompt": "p", "model": "a", "config": {"model": "b"}},
            max_input_bytes=MAX_INPUT,
        ),
        CONFLICTING_ARGUMENTS,
    )
    # Same value is normalized into one.
    call = parse_codex_call(
        {"prompt": "p", "model": "a", "config": {"model": "a"}},
        max_input_bytes=MAX_INPUT,
    )
    assert call.model == "a"


def test_new_conflicting_effort_and_compact():
    _expect_error(
        lambda: parse_codex_call(
            {"prompt": "p", "effort": "high", "config": {"model_reasoning_effort": "low"}},
            max_input_bytes=MAX_INPUT,
        ),
        CONFLICTING_ARGUMENTS,
    )
    _expect_error(
        lambda: parse_codex_call(
            {"prompt": "p", "compact-prompt": "x", "config": {"compact_prompt": "y"}},
            max_input_bytes=MAX_INPUT,
        ),
        CONFLICTING_ARGUMENTS,
    )


def test_new_prompt_not_trimmed_and_size_limit():
    call = parse_codex_call({"prompt": "  keep my spaces  "}, max_input_bytes=MAX_INPUT)
    assert call.prompt == "  keep my spaces  "
    _expect_error(
        lambda: parse_codex_call({"prompt": "x" * 11}, max_input_bytes=10),
        "INPUT_LIMIT_EXCEEDED",
    )


def test_new_total_input_size_includes_instructions():
    """Design 9.2 limits the *total* input, not just the prompt."""
    huge = "x" * 60
    with pytest.raises(BridgeError) as excinfo:
        parse_codex_call(
            {"prompt": "p", "base-instructions": huge, "developer-instructions": huge},
            max_input_bytes=100,
        )
    assert excinfo.value.code == "INPUT_LIMIT_EXCEEDED"
    # Under the limit with both instructions is fine.
    call = parse_codex_call(
        {"prompt": "p", "base-instructions": "x", "developer-instructions": "y"},
        max_input_bytes=100,
    )
    assert call.base_instructions == "x"


def test_new_compact_prompt_may_be_empty():
    """The schema has no minLength for compact_prompt: empty is valid."""
    call = parse_codex_call({"prompt": "p", "compact-prompt": ""}, max_input_bytes=MAX_INPUT)
    assert call.compact_prompt == ""
    via_config = parse_codex_call(
        {"prompt": "p", "config": {"compact_prompt": ""}}, max_input_bytes=MAX_INPUT
    )
    assert via_config.compact_prompt == ""
    # Model keys are still required to be non-empty.
    _expect_error(
        lambda: parse_codex_call(
            {"prompt": "p", "config": {"model": ""}}, max_input_bytes=MAX_INPUT
        ),
        VALIDATION_ERROR,
    )


def test_new_arguments_not_object():
    _expect_error(lambda: parse_codex_call(["p"], max_input_bytes=MAX_INPUT), VALIDATION_ERROR)
    _expect_error(lambda: parse_codex_call("p", max_input_bytes=MAX_INPUT), VALIDATION_ERROR)


# --- codex-reply --------------------------------------------------------------


def test_reply_modern_id_valid():
    call = parse_codex_reply_call({"prompt": "next", "threadId": "th-1"}, max_input_bytes=MAX_INPUT)
    assert call.thread_id == "th-1"


def test_reply_legacy_id_valid():
    call = parse_codex_reply_call(
        {"prompt": "next", "conversationId": "th-2"}, max_input_bytes=MAX_INPUT
    )
    assert call.thread_id == "th-2"


def test_reply_matching_aliases_valid():
    call = parse_codex_reply_call(
        {"prompt": "next", "threadId": "th-3", "conversationId": "th-3"},
        max_input_bytes=MAX_INPUT,
    )
    assert call.thread_id == "th-3"


def test_reply_mismatched_aliases_rejected():
    _expect_error(
        lambda: parse_codex_reply_call(
            {"prompt": "next", "threadId": "a", "conversationId": "b"},
            max_input_bytes=MAX_INPUT,
        ),
        VALIDATION_ERROR,
    )


def test_reply_overrides_valid():
    call = parse_codex_reply_call(
        {"prompt": "next", "threadId": "th", "model": "m", "effort": "high"},
        max_input_bytes=MAX_INPUT,
    )
    assert call.model == "m" and call.effort == "high"


def test_reply_missing_id_rejected():
    _expect_error(
        lambda: parse_codex_reply_call({"prompt": "next"}, max_input_bytes=MAX_INPUT),
        VALIDATION_ERROR,
    )


def test_reply_disallowed_cwd_rejected():
    _expect_error(
        lambda: parse_codex_reply_call(
            {"prompt": "next", "threadId": "th", "cwd": "/elsewhere"},
            max_input_bytes=MAX_INPUT,
        ),
        VALIDATION_ERROR,
    )


def test_reply_null_thread_rejected():
    _expect_error(
        lambda: parse_codex_reply_call(
            {"prompt": "n", "threadId": None}, max_input_bytes=MAX_INPUT
        ),
        VALIDATION_ERROR,
    )
