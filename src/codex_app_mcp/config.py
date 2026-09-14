"""Bridge configuration resolution (design sections 6.1, 9.2, 12).

Priority (highest wins):

    CLI arguments > CODEX_APP_MCP_* environment > bridge.toml > built-in defaults

This governs *startup configuration only*; it never re-applies to running
threads on every turn.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import CONFIG_KEY_DENIED, VALIDATION_ERROR, BridgeError

ENV_PREFIX = "CODEX_APP_MCP_"

SUPPORTED_RUNTIME_MODES = ("bundled",)
SUPPORTED_SANDBOX_MODES = ("read-only", "workspace-write")
SUPPORTED_APPROVAL_POLICIES = ("never",)
SUPPORTED_LOG_FORMATS = ("json", "text")

_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    mode: str = "bundled"


@dataclass(frozen=True, slots=True)
class DefaultsConfig:
    model: str | None = None
    effort: str | None = None
    cwd: str | None = None
    sandbox: str = "read-only"
    approval_policy: str = "never"


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Operator restrictions.

    ``allowed_roots`` restricts cwd / thread acceptance — it is NOT a file
    read sandbox (design section 10.2). Empty ``allowed_models`` means no
    restriction beyond the runtime catalog; empty ``allowed_roots`` denies
    every workspace (fail-closed).
    """

    allowed_roots: tuple[str, ...] = ()
    allowed_models: tuple[str, ...] = ()
    allowed_sandboxes: tuple[str, ...] = ("read-only", "workspace-write")


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    max_active_turns: int = 1
    startup_timeout_seconds: float = 30.0
    rpc_timeout_seconds: float = 30.0
    turn_timeout_seconds: float = 900.0
    interrupt_grace_seconds: float = 10.0
    shutdown_grace_seconds: float = 5.0
    max_input_bytes: int = _MIB
    max_result_bytes: int = _MIB
    progress_min_interval_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    source_path: str | None = field(default=None, compare=False)

    def provenance(self) -> dict[str, str]:
        """Where each effective setting came from, for diagnostics."""
        return dict(self._provenance)

    # provenance is stored separately to keep the frozen dataclass hashable
    _provenance: dict[str, str] = field(default_factory=dict, compare=False)


def _err(message: str) -> BridgeError:
    return BridgeError(code=VALIDATION_ERROR, message=message)


def _join_list(value: object, section: str, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _err(f"[{section}] {key} must be an array of strings")
    return tuple(value)


_SECTION_KEYS = {
    "runtime": {"mode"},
    "defaults": {"model", "effort", "cwd", "sandbox", "approval_policy"},
    "policy": {"allowed_roots", "allowed_models", "allowed_sandboxes"},
    "limits": {
        "max_active_turns",
        "startup_timeout_seconds",
        "rpc_timeout_seconds",
        "turn_timeout_seconds",
        "interrupt_grace_seconds",
        "shutdown_grace_seconds",
        "max_input_bytes",
        "max_result_bytes",
        "progress_min_interval_seconds",
    },
    "logging": {"level", "format"},
}


def _reject_unknown_keys(data: Mapping[str, object], section: str) -> Mapping[str, object]:
    """Unknown keys inside known sections are rejected, not ignored.

    A typo like ``turn_timeout_secondz`` silently falling back to the 900s
    default is exactly the "unsupported setting silently ignored" class the
    project configuration contract forbids (docs/configuration.md).
    """
    raw = data.get(section, {})
    if not isinstance(raw, Mapping):
        raise _err(f"[{section}] must be a table")
    unknown = sorted(set(raw) - _SECTION_KEYS[section])
    if unknown:
        raise _err(
            f"[{section}] has unknown keys {unknown} (allowed: {sorted(_SECTION_KEYS[section])})"
        )
    return raw


def _parse_toml_section(
    data: Mapping[str, object],
) -> tuple[
    RuntimeConfig,
    DefaultsConfig,
    PolicyConfig,
    LimitsConfig,
    LoggingConfig,
    dict[str, str],
]:
    provenance: dict[str, str] = {}

    runtime_raw = _reject_unknown_keys(data, "runtime")
    mode = runtime_raw.get("mode", "bundled")
    if mode not in SUPPORTED_RUNTIME_MODES:
        raise BridgeError(
            code=CONFIG_KEY_DENIED,
            message=f"unsupported runtime mode: {mode!r} (supported: {SUPPORTED_RUNTIME_MODES})",
        )
    runtime = RuntimeConfig(mode=str(mode))

    defaults_raw = _reject_unknown_keys(data, "defaults")

    def _opt_str(section: str, key: str) -> str | None:
        value = section_raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise _err(f"[{section}] {key} must be a non-empty string")
        return value

    section_raw = defaults_raw
    model = _opt_str("defaults", "model")
    effort = _opt_str("defaults", "effort")
    cwd = _opt_str("defaults", "cwd")
    sandbox = defaults_raw.get("sandbox", "read-only")
    if sandbox not in SUPPORTED_SANDBOX_MODES:
        raise BridgeError(
            code=CONFIG_KEY_DENIED,
            message=(
                f"[defaults] sandbox {sandbox!r} is not supported in v0.1 "
                f"(supported: {SUPPORTED_SANDBOX_MODES})"
            ),
        )
    approval_policy = defaults_raw.get("approval_policy", "never")
    if approval_policy not in SUPPORTED_APPROVAL_POLICIES:
        raise BridgeError(
            code=CONFIG_KEY_DENIED,
            message=(
                f"[defaults] approval_policy {approval_policy!r} is not supported "
                f"(supported: {SUPPORTED_APPROVAL_POLICIES})"
            ),
        )
    defaults = DefaultsConfig(
        model=model,
        effort=effort,
        cwd=cwd,
        sandbox=str(sandbox),
        approval_policy=str(approval_policy),
    )
    for key, value in (
        ("model", model),
        ("effort", effort),
        ("cwd", cwd),
        ("sandbox", str(sandbox)),
        ("approval_policy", str(approval_policy)),
    ):
        if value is not None:
            provenance[f"defaults.{key}"] = "toml"

    policy_raw = _reject_unknown_keys(data, "policy")
    allowed_sandboxes = _join_list(
        policy_raw.get("allowed_sandboxes"), "policy", "allowed_sandboxes"
    ) or ("read-only", "workspace-write")
    for sandbox_mode in allowed_sandboxes:
        if sandbox_mode not in SUPPORTED_SANDBOX_MODES:
            raise BridgeError(
                code=CONFIG_KEY_DENIED,
                message=(f"[policy] sandbox mode {sandbox_mode!r} cannot be allowed in v0.1"),
            )
    policy = PolicyConfig(
        allowed_roots=_join_list(policy_raw.get("allowed_roots"), "policy", "allowed_roots"),
        allowed_models=_join_list(policy_raw.get("allowed_models"), "policy", "allowed_models"),
        allowed_sandboxes=allowed_sandboxes,
    )

    limits_raw = _reject_unknown_keys(data, "limits")

    def _num(key: str, default: float) -> float:
        value = limits_raw.get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise _err(f"[limits] {key} must be a positive number")
        return float(value)

    max_active_turns = limits_raw.get("max_active_turns", 1)
    if max_active_turns != 1:
        raise _err(
            "[limits] max_active_turns must be 1 in v0.1; concurrent turns are a later design"
        )
    limits = LimitsConfig(
        max_active_turns=1,
        startup_timeout_seconds=_num("startup_timeout_seconds", 30.0),
        rpc_timeout_seconds=_num("rpc_timeout_seconds", 30.0),
        turn_timeout_seconds=_num("turn_timeout_seconds", 900.0),
        interrupt_grace_seconds=_num("interrupt_grace_seconds", 10.0),
        shutdown_grace_seconds=_num("shutdown_grace_seconds", 5.0),
        max_input_bytes=int(_num("max_input_bytes", _MIB)),
        max_result_bytes=int(_num("max_result_bytes", _MIB)),
        progress_min_interval_seconds=_num("progress_min_interval_seconds", 1.0),
    )

    logging_raw = _reject_unknown_keys(data, "logging")
    level = str(logging_raw.get("level", "INFO")).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise _err(f"[logging] level {level!r} is not a valid Python log level")
    fmt = logging_raw.get("format", "json")
    if fmt not in SUPPORTED_LOG_FORMATS:
        raise _err(f"[logging] format must be one of {SUPPORTED_LOG_FORMATS}")
    logging_config = LoggingConfig(level=level, format=str(fmt))

    return runtime, defaults, policy, limits, logging_config, provenance


@dataclass(frozen=True, slots=True)
class CliOverrides:
    """Startup CLI overrides for ``serve`` (design section 12)."""

    model: str | None = None
    effort: str | None = None
    cwd: str | None = None
    sandbox: str | None = None
    approval_policy: str | None = None


def load_bridge_config(
    path: str | Path | None,
    cli: CliOverrides | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> BridgeConfig:
    """Resolve the effective bridge configuration.

    ``env`` defaults to ``os.environ``. Unknown TOML top-level sections are
    rejected rather than silently ignored.
    """
    env = os.environ if env is None else env
    data: dict[str, object] = {}
    source = "defaults"
    if path is not None:
        config_path = Path(path)
        if not config_path.is_file():
            raise _err(f"bridge config file not found: {config_path}")
        try:
            with open(config_path, "rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise _err(f"invalid TOML in {config_path}: {exc}") from exc
        source = "toml"
    allowed_sections = {"runtime", "defaults", "policy", "limits", "logging"}
    unknown = set(data) - allowed_sections
    if unknown:
        raise _err(
            f"unknown bridge config sections: {sorted(unknown)} "
            f"(allowed: {sorted(allowed_sections)})"
        )

    runtime, defaults, policy, limits, logging_config, provenance = _parse_toml_section(data)
    if source == "defaults":
        provenance = {}

    # Environment overrides (bridge-specific variables only).
    def _env(name: str) -> str | None:
        value = env.get(f"{ENV_PREFIX}{name}")
        return value if value else None

    env_model = _env("MODEL")
    env_effort = _env("EFFORT")
    env_cwd = _env("CWD")
    env_sandbox = _env("SANDBOX")
    env_approval = _env("APPROVAL_POLICY")
    if env_model:
        defaults = replace(defaults, model=env_model)
        provenance["defaults.model"] = "env"
    if env_effort:
        defaults = replace(defaults, effort=env_effort)
        provenance["defaults.effort"] = "env"
    if env_cwd:
        defaults = replace(defaults, cwd=env_cwd)
        provenance["defaults.cwd"] = "env"
    if env_sandbox:
        if env_sandbox not in SUPPORTED_SANDBOX_MODES:
            raise BridgeError(
                code=CONFIG_KEY_DENIED,
                message=f"{ENV_PREFIX}SANDBOX={env_sandbox!r} is not supported in v0.1",
            )
        defaults = replace(defaults, sandbox=env_sandbox)
        provenance["defaults.sandbox"] = "env"
    if env_approval:
        if env_approval not in SUPPORTED_APPROVAL_POLICIES:
            raise BridgeError(
                code=CONFIG_KEY_DENIED,
                message=f"{ENV_PREFIX}APPROVAL_POLICY={env_approval!r} is not supported",
            )
        defaults = replace(defaults, approval_policy=env_approval)
        provenance["defaults.approval_policy"] = "env"

    # CLI overrides (highest priority).
    if cli is not None:
        if cli.model:
            defaults = replace(defaults, model=cli.model)
            provenance["defaults.model"] = "cli"
        if cli.effort:
            defaults = replace(defaults, effort=cli.effort)
            provenance["defaults.effort"] = "cli"
        if cli.cwd:
            defaults = replace(defaults, cwd=cli.cwd)
            provenance["defaults.cwd"] = "cli"
        if cli.sandbox:
            if cli.sandbox not in SUPPORTED_SANDBOX_MODES:
                raise BridgeError(
                    code=CONFIG_KEY_DENIED,
                    message=f"--sandbox {cli.sandbox!r} is not supported in v0.1",
                )
            defaults = replace(defaults, sandbox=cli.sandbox)
            provenance["defaults.sandbox"] = "cli"
        if cli.approval_policy:
            if cli.approval_policy not in SUPPORTED_APPROVAL_POLICIES:
                raise BridgeError(
                    code=CONFIG_KEY_DENIED,
                    message=f"--approval-policy {cli.approval_policy!r} is not supported",
                )
            defaults = replace(defaults, approval_policy=cli.approval_policy)
            provenance["defaults.approval_policy"] = "cli"

    final = BridgeConfig(
        runtime=runtime,
        defaults=defaults,
        policy=policy,
        limits=limits,
        logging=logging_config,
        source_path=str(Path(path).resolve()) if path is not None else None,
        _provenance=provenance,
    )
    _validate_startup_consistency(final)
    return final


def user_config_path(*, env: Mapping[str, str] | None = None) -> Path:
    """Return the XDG user configuration path."""
    env = os.environ if env is None else env
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg and Path(xdg).is_absolute():
        base = Path(xdg)
    else:
        home = env.get("HOME")
        base = (Path(home) if home else Path.home()) / ".config"
    return base / "codex-app-mcp" / "bridge.toml"


def discover_bridge_config(
    explicit: str | Path | None,
    *,
    start_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Find one config: explicit, nearest ancestor, then the XDG user file."""
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    current = Path.cwd() if start_dir is None else Path(start_dir)
    current = current.expanduser().resolve()
    for directory in (current, *current.parents):
        candidate = directory / "bridge.toml"
        if candidate.is_file():
            return candidate
    candidate = user_config_path(env=env)
    return candidate if candidate.is_file() else None


def find_workspace_root(start_dir: str | Path | None = None) -> Path:
    """Return the nearest Git worktree root, or the resolved start directory."""
    current = Path.cwd() if start_dir is None else Path(start_dir)
    current = current.expanduser().resolve()
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return directory
    return current


def require_operational_policy(config: BridgeConfig) -> None:
    """Reject a serve configuration that cannot authorize any workspace."""
    if config.policy.allowed_roots:
        return
    source = config.source_path or "the selected bridge configuration"
    raise _err(
        f"policy.allowed_roots is empty in {source}; every workspace would be denied. "
        "Add [policy].allowed_roots or run 'codex-app-mcp init'"
    )


def _validate_startup_consistency(config: BridgeConfig) -> None:
    """Reject inconsistent startup settings at load time (review R2).

    Per-call policy checks remain as defense in depth, but an operator
    configuration whose defaults exceed its own allowlist must not load.
    """
    if config.defaults.sandbox not in config.policy.allowed_sandboxes:
        raise BridgeError(
            code=CONFIG_KEY_DENIED,
            message=(
                f"[defaults] sandbox {config.defaults.sandbox!r} is not in "
                "[policy].allowed_sandboxes; startup defaults must stay within "
                "the operator allowlist"
            ),
        )
    if config.defaults.approval_policy != "never":
        raise BridgeError(
            code=CONFIG_KEY_DENIED,
            message=(
                f"[defaults] approval_policy {config.defaults.approval_policy!r} "
                "is not supported (only 'never')"
            ),
        )
