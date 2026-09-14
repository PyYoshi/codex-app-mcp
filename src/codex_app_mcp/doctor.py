"""``doctor`` diagnostics.

Checks SDK/runtime versions, authentication presence (never contents),
self-connection suspicion, cwd/policy sanity, and the runtime model catalog.
No inference is run and no secrets are displayed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import CHILD_ENV_FLAG
from .config import (
    BridgeConfig,
    discover_bridge_config,
    load_bridge_config,
    require_operational_policy,
)
from .errors import BridgeError

_OK, WARN, FAIL = "ok", "warning", "fail"


def _is_supported_python(version_info: tuple[int, ...] = sys.version_info) -> bool:
    """Return whether the interpreter is in the supported 3.14 major line."""
    return (3, 14) <= version_info[:2] < (3, 15)


def _check(ok: bool, detail: str, *, level_on_fail: str = FAIL) -> dict[str, str]:
    return {"status": _OK if ok else level_on_fail, "detail": detail}


def _contains_bridge_reference(value: object) -> bool:
    if isinstance(value, str):
        return re.search(r"codex[-_]app[-_]mcp", value) is not None
    if isinstance(value, dict):
        return any(_contains_bridge_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_bridge_reference(item) for item in value)
    return False


def _codex_mcp_references_bridge(config_toml: Path) -> bool:
    """Inspect MCP server definitions without matching unrelated project paths."""
    with config_toml.open("rb") as stream:
        config = tomllib.load(stream)
    return _contains_bridge_reference(config.get("mcp_servers", {}))


async def run_doctor(
    *, config_path: str | None = None, skip_runtime: bool = False
) -> dict[str, Any]:
    report: dict[str, Any] = {"bridge_version": _bridge_version(), "checks": []}
    checks: list[dict[str, str]] = report["checks"]

    # --- Python / SDK --------------------------------------------------------
    checks.append(_check(_is_supported_python(), f"python {sys.version.split()[0]}"))
    try:
        import openai_codex

        checks.append(_check(True, f"openai-codex SDK {openai_codex.__version__}"))
        sdk_version = openai_codex.__version__
    except Exception as exc:  # noqa: BLE001
        checks.append(_check(False, f"openai-codex SDK import failed: {exc!r}"))
        report["ok"] = False
        return report
    try:
        import mcp

        mcp_version = getattr(mcp, "__version__", None)
        from importlib.metadata import version as pkg_version

        mcp_version = mcp_version or pkg_version("mcp")
        checks.append(_check(True, f"mcp SDK {mcp_version}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check(False, f"mcp SDK import failed: {exc!r}"))
        report["ok"] = False
        return report

    # --- Configuration -------------------------------------------------------
    config: BridgeConfig | None = None
    try:
        selected = discover_bridge_config(config_path)
        if selected is None:
            raise BridgeError(
                code="VALIDATION_ERROR",
                message=(
                    f"no bridge.toml found from {Path.cwd()} or the user config directory; "
                    "run 'codex-app-mcp init' or pass --config PATH"
                ),
            )
        config = load_bridge_config(selected)
        require_operational_policy(config)
        checks.append(_check(True, f"configuration loaded: {selected}"))
        checks.append(_check(True, "policy.allowed_roots configured"))
        default_cwd = config.defaults.cwd
        if default_cwd:
            checks.append(
                _check(
                    Path(default_cwd).is_dir(),
                    f"defaults.cwd exists: {default_cwd}",
                    level_on_fail=WARN,
                )
            )
        if config.defaults.model:
            checks.append({"status": _OK, "detail": f"default model: {config.defaults.model}"})
        if config.defaults.effort:
            checks.append({"status": _OK, "detail": f"default effort: {config.defaults.effort}"})
    except BridgeError as error:
        checks.append(_check(False, f"config error [{error.code}]: {error.message}"))
        report["ok"] = False
        return report

    # --- Self-connection suspicion --------------------------------------------
    if os.environ.get("CODEX_APP_MCP_TEST_LAUNCH_ARGS"):
        checks.append(
            _check(
                False,
                "CODEX_APP_MCP_TEST_LAUNCH_ARGS is set: this environment injects a "
                "custom runtime launch command (test hook); unset it for "
                "production use.",
            )
        )
    if os.environ.get(CHILD_ENV_FLAG) == "1":
        checks.append(
            _check(
                False,
                f"{CHILD_ENV_FLAG}=1 is set in this environment: a serve started "
                "here would stop itself (recursive self-connection guard).",
            )
        )
    else:
        checks.append({"status": _OK, "detail": "no self-connection flag present"})
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    config_toml = codex_home / "config.toml"
    if config_toml.is_file():
        try:
            if _codex_mcp_references_bridge(config_toml):
                checks.append(
                    _check(
                        False,
                        "Codex config references codex-app-mcp: check for recursive "
                        "MCP self-connection before enabling it as a Codex-side "
                        "MCP server.",
                    )
                )
            else:
                checks.append({"status": _OK, "detail": "no self-reference in Codex config"})
        except (OSError, tomllib.TOMLDecodeError) as exc:
            checks.append(_check(False, f"cannot read {config_toml}: {exc}", level_on_fail=WARN))

    # --- Authentication presence (contents never displayed) --------------------
    auth_file = codex_home / "auth.json"
    checks.append(
        _check(
            auth_file.is_file(),
            f"auth file present: {auth_file}"
            if auth_file.is_file()
            else f"no auth file at {auth_file}; run codex login before using the bridge",
            level_on_fail=WARN,
        )
    )

    # --- Runtime ---------------------------------------------------------------
    if skip_runtime:
        checks.append({"status": _OK, "detail": "runtime check skipped (--skip-runtime)"})
    else:
        await _check_runtime(checks, config, sdk_version)

    report["ok"] = all(check["status"] != FAIL for check in checks)
    report["warnings"] = sum(1 for check in checks if check["status"] == WARN)
    return report


def _bridge_version() -> str:
    from . import __version__

    return __version__


async def _check_runtime(
    checks: list[dict[str, str]], config: BridgeConfig, sdk_version: str
) -> None:
    from .backend.codex_sdk import CodexSdkBackend
    from .backend.interface import BackendOptions

    backend = CodexSdkBackend(
        BackendOptions(
            startup_timeout=config.limits.startup_timeout_seconds,
            rpc_timeout=config.limits.rpc_timeout_seconds,
        )
    )
    try:
        try:
            info = await backend.start_runtime()
        except BridgeError as error:
            checks.append(_check(False, f"runtime startup failed [{error.code}]"))
            return
        checks.append(
            {
                "status": _OK,
                "detail": (
                    f"runtime started: {info.server_name or 'codex app-server'} "
                    f"{info.server_version or 'unknown'} (SDK {sdk_version})"
                ),
            }
        )
        try:
            entries = await asyncio.wait_for(
                backend.list_models(include_hidden=True),
                timeout=config.limits.rpc_timeout_seconds,
            )
        except BridgeError as error:
            checks.append(_check(False, f"model/list failed [{error.code}]"))
            return
        model_ids = sorted(entry.model_id for entry in entries)
        checks.append(
            {
                "status": _OK,
                "detail": f"catalog models ({len(model_ids)}): {', '.join(model_ids[:12])}"
                + (" ..." if len(model_ids) > 12 else ""),
            }
        )
        default_model = config.defaults.model
        if default_model:
            match = next((e for e in entries if e.model_id == default_model), None)
            if match is None:
                checks.append(
                    _check(
                        False,
                        f"configured default model {default_model!r} not in catalog",
                    )
                )
            else:
                checks.append(
                    {
                        "status": _OK,
                        "detail": (
                            f"default model {default_model!r} available; efforts: "
                            f"{list(match.supported_reasoning_efforts) or 'undeclared'}"
                        ),
                    }
                )
                default_effort = config.defaults.effort
                if default_effort and match.supported_reasoning_efforts:
                    if default_effort not in match.supported_reasoning_efforts:
                        checks.append(
                            _check(
                                False,
                                f"default effort {default_effort!r} not supported by "
                                f"{default_model!r}",
                            )
                        )
    finally:
        try:
            await asyncio.wait_for(
                backend.close(), timeout=config.limits.shutdown_grace_seconds + 5
            )
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":  # pragma: no cover
    report = asyncio.run(run_doctor())
    print(json.dumps(report, ensure_ascii=False, indent=2))
