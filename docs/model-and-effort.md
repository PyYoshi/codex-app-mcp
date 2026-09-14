# Model and reasoning effort

Startup defaults follow `CLI > environment > TOML > runtime default`. New
threads resolve call values, then bridge defaults, then runtime defaults.
Explicit models must satisfy the operator allowlist and runtime catalog; effort
must be supported by the model. There is no automatic fallback.

Runtime-confirmed settings are stored with thread metadata. Replies use explicit
overrides or inherit stored values; startup defaults are not reapplied. Resumed
threads are checked against current policy. Catalog caches are scoped to a
backend instance and generation and are invalidated when it stops or changes.
