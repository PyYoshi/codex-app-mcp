# Error contract

Errors use `isError: true`, a safe user-facing text message,
`structuredContent`, and `_meta.codex-app-mcp` fields including `code`,
`retryable`, `mayHaveSideEffects`, and known thread/turn identifiers.

Validation and policy errors include `VALIDATION_ERROR`,
`CONFLICTING_ARGUMENTS`, `WORKSPACE_DENIED`, `MODEL_NOT_ALLOWED`,
`MODEL_UNAVAILABLE`, `UNSUPPORTED_EFFORT`,
`UNSUPPORTED_APPROVAL_POLICY`, and `CONFIG_KEY_DENIED`. Capacity and limits
include `SERVER_BUSY`, `THREAD_BUSY`, `INPUT_LIMIT_EXCEEDED`,
`OUTPUT_LIMIT_EXCEEDED`, and `TURN_TIMEOUT`. Runtime failures include
`RUNTIME_MISMATCH`, `RUNTIME_DISCONNECTED`, `RPC_TIMEOUT`,
`UNSUPPORTED_SERVER_REQUEST`, `EXECUTION_STATE_UNKNOWN`,
`RUNTIME_STOP_UNCONFIRMED`, and `TURN_FAILED`.

`mayHaveSideEffects` is true once turn submission could have reached the
runtime, regardless of whether the bridge later loses confirmation. Fatal
runtime errors force stop/replacement before reuse; an unconfirmed stop prevents
reuse entirely. Internal diagnostics are sanitized and never copied directly
into the public envelope.
