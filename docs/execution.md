# Execution, cancellation, and timeouts

New calls validate input and policy, acquire the single process slot and needed
thread/workspace locks, start the runtime lazily, resolve model settings, start
a thread and turn, then reduce events to a result. Replies additionally load and
validate stored/runtime thread metadata before starting a turn.

Cancellation interrupts only the turn and backend owned by that run. If
submission has not started, cleanup completes without touching the runtime. If
submission may have occurred, the supervisor continues long enough to obtain a
turn ID, interrupt it, and establish a safe terminal condition. A cancelled MCP
request receives no late result.

Turn timeout triggers interrupt and bounded cleanup. Startup, RPC, transport,
unsupported server-request, and unknown-state failures stop the affected runtime
before a response when possible. If stop cannot be confirmed, the bridge becomes
unavailable rather than releasing unsafe ownership. No uncertain turn is
resubmitted automatically. Events from other runtime generations or turn IDs
cannot complete the current run.
