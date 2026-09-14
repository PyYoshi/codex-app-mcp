# MCP tool contracts

The packaged source of truth is `src/codex_app_mcp/schemas/tools.json`.

## `codex`

`prompt` is required and non-blank; whitespace is preserved. Optional fields
cover model, effort, cwd, sandbox, the fixed approval policy, and compatibility
instructions/config. Null and unknown fields are rejected. Compatibility config
allows only `model`, `model_reasoning_effort`, and `compact_prompt`;
duplicate values must agree. Input is bounded by `max_input_bytes`.

## `codex-reply`

`prompt` and `threadId` are required. `conversationId` is an alias and must
match when both are supplied. Model and effort overrides persist. Reply cannot
change cwd, sandbox, or instructions. An active thread returns `THREAD_BUSY`.

Success returns final text plus `structuredContent` with `threadId` and
`content`. A JSON text fallback carries the same data for clients that hide
structured content. Oversized output fails rather than being truncated. Only
final agent messages are retained; commands, reasoning, plans, and file content
are not accumulated. Progress requires a progress token and is throttled.
