# Architecture

```text
MCP client -> stdio server -> execution coordinator -> backend interface
                                      |                    |
                              policy + registry       Codex SDK
                                                           |
                                                   codex app-server
```

The server owns MCP framing and never writes diagnostics to stdout. Contracts
validate calls; policy checks workspace, sandbox, model, and effort; the
registry stores thread metadata and advisory locks. The coordinator owns turn
state, deadlines, cancellation, leases, and runtime shutdown. The SDK adapter
is the only module coupled to `openai-codex` and runs blocking SDK calls in
dedicated executors.

A run progresses through validation, thread start/resume, turn start, running,
optional cancellation, and terminal cleanup. A slot or lock is released only
after a terminal event, confirmed runtime stop, or proof that submission never
started. Unknown execution state is not treated as success and is never
automatically retried.

Runtime stop is tied to the exact backend instance owned by the run. Concurrent
stop requests share one result. An unconfirmed stop poisons the coordinator,
retains ownership and locks, and rejects further work. Shutdown failure or a
cleanup-budget overrun propagates as failure. Only final answer text and bounded
metadata are retained in memory.
