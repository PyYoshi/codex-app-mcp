"""Unit tests: config resolution, policy enforcement, model resolution."""

from __future__ import annotations

import pytest

from codex_app_mcp.backend.interface import ModelCatalogEntry
from codex_app_mcp.config import (
    CliOverrides,
    DefaultsConfig,
    LimitsConfig,
    PolicyConfig,
    discover_bridge_config,
    find_workspace_root,
    load_bridge_config,
    user_config_path,
)
from codex_app_mcp.doctor import _is_supported_python
from codex_app_mcp.errors import (
    CONFIG_KEY_DENIED,
    MODEL_NOT_ALLOWED,
    UNSUPPORTED_EFFORT,
    VALIDATION_ERROR,
    WORKSPACE_DENIED,
    BridgeError,
)
from codex_app_mcp.policy import ModelCatalog, PolicyStore


def _config(**kw) -> object:
    from codex_app_mcp.config import BridgeConfig, LoggingConfig, RuntimeConfig

    return BridgeConfig(
        runtime=RuntimeConfig(),
        defaults=kw.pop("defaults", DefaultsConfig()),
        policy=kw.pop(
            "policy",
            PolicyConfig(allowed_roots=("/repo",), allowed_models=("gpt-5.6-terra",)),
        ),
        limits=kw.pop("limits", LimitsConfig()),
        logging=LoggingConfig(),
    )


# --- config --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "supported"),
    [((3, 13, 15), False), ((3, 14, 0), True), ((3, 14, 99), True), ((3, 15, 0), False)],
)
def test_doctor_python_support_matches_package_metadata(version, supported):
    assert _is_supported_python(version) is supported


def test_config_builtin_defaults():
    config = load_bridge_config(None)
    assert config.defaults.sandbox == "read-only"
    assert config.defaults.approval_policy == "never"
    assert config.limits.turn_timeout_seconds == 900
    assert config.limits.max_input_bytes == 1024 * 1024
    assert config.limits.max_result_bytes == 1024 * 1024
    assert config.limits.interrupt_grace_seconds == 10
    assert config.provenance() == {}


def test_config_priority_cli_env_toml(tmp_path):
    toml = tmp_path / "b.toml"
    toml.write_text(
        """
[defaults]
model = "toml-model"
effort = "low"
cwd = "/toml"
sandbox = "read-only"

[policy]
allowed_roots = ["/repo"]
allowed_models = ["toml-model"]
"""
    )
    config = load_bridge_config(
        toml,
        CliOverrides(model="cli-model"),
        env={"CODEX_APP_MCP_EFFORT": "high"},
    )
    assert config.defaults.model == "cli-model"
    assert config.defaults.effort == "high"
    assert config.defaults.cwd == "/toml"
    prov = config.provenance()
    assert prov["defaults.model"] == "cli"
    assert prov["defaults.effort"] == "env"
    assert prov["defaults.cwd"] == "toml"


def test_config_rejects_bad_values(tmp_path):
    toml = tmp_path / "b.toml"
    toml.write_text("[defaults]\nsandbox = 'danger-full-access'\n")
    with pytest.raises(BridgeError) as excinfo:
        load_bridge_config(toml)
    assert excinfo.value.code == CONFIG_KEY_DENIED

    toml.write_text("[limits]\nmax_active_turns = 4\n")
    with pytest.raises(BridgeError) as excinfo:
        load_bridge_config(toml)
    assert excinfo.value.code == VALIDATION_ERROR

    toml.write_text("[defaults]\napproval_policy = 'on-request'\n")
    with pytest.raises(BridgeError) as excinfo:
        load_bridge_config(toml)
    assert excinfo.value.code == CONFIG_KEY_DENIED

    toml.write_text("[unknown_section]\nx = 1\n")
    with pytest.raises(BridgeError) as excinfo:
        load_bridge_config(toml)
    assert excinfo.value.code == VALIDATION_ERROR


def test_config_rejects_unknown_keys_in_known_sections(tmp_path):
    """Review MINOR-1: a typo'd key must fail, not silently use defaults."""
    toml = tmp_path / "b.toml"
    toml.write_text("[limits]\nturn_timeout_secondz = 5\n")
    with pytest.raises(BridgeError) as excinfo:
        load_bridge_config(toml)
    assert excinfo.value.code == VALIDATION_ERROR
    assert "turn_timeout_secondz" in excinfo.value.message

    toml.write_text('[defaults]\nmodl = "x"\n')
    with pytest.raises(BridgeError) as excinfo:
        load_bridge_config(toml)
    assert excinfo.value.code == VALIDATION_ERROR


def test_config_example_file_loads():
    config = load_bridge_config("bridge.example.toml")
    assert config.defaults.model == "gpt-5.6-terra"
    assert config.policy.allowed_roots == ("/absolute/path/to/repository",)
    assert config.limits.max_active_turns == 1


def test_config_discovery_prefers_explicit_then_nearest_then_user(tmp_path):
    home = tmp_path / "home"
    xdg = home / "xdg"
    nested = tmp_path / "repo" / "sub" / "dir"
    nested.mkdir(parents=True)
    user = xdg / "codex-app-mcp" / "bridge.toml"
    user.parent.mkdir(parents=True)
    user.write_text("[policy]\nallowed_roots = ['/user']\n")
    parent = nested.parent / "bridge.toml"
    parent.write_text("[policy]\nallowed_roots = ['/parent']\n")
    nearest = nested / "bridge.toml"
    nearest.write_text("[policy]\nallowed_roots = ['/nearest']\n")
    explicit = tmp_path / "explicit.toml"

    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}
    assert discover_bridge_config(explicit, start_dir=nested, env=env) == explicit
    assert discover_bridge_config(None, start_dir=nested, env=env) == nearest
    nearest.unlink()
    assert discover_bridge_config(None, start_dir=nested, env=env) == parent
    parent.unlink()
    assert discover_bridge_config(None, start_dir=nested, env=env) == user


def test_user_config_falls_back_when_xdg_is_relative(tmp_path):
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": "relative"}
    assert user_config_path(env=env) == tmp_path / ".config/codex-app-mcp/bridge.toml"


def test_workspace_root_uses_nearest_git_marker(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: elsewhere\n")
    assert find_workspace_root(nested) == repo


# --- policy: workspace -----------------------------------------------------------


def test_workspace_checks(tmp_path):
    # Explicit roots; containment only, existence not required.
    store = PolicyStore(_config(policy=PolicyConfig(allowed_roots=(str(tmp_path),))))
    store.check_workspace(str(tmp_path))
    with pytest.raises(BridgeError) as excinfo:
        store.check_workspace(str(tmp_path.parent))
    assert excinfo.value.code == WORKSPACE_DENIED


def test_workspace_path_containment(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    store = PolicyStore(_config(policy=PolicyConfig(allowed_roots=(str(root),))))
    store.check_workspace(str(root / "sub" / "dir"))  # does not need to exist
    with pytest.raises(BridgeError) as excinfo:
        store.check_workspace(str(tmp_path / "other"))
    assert excinfo.value.code == WORKSPACE_DENIED


def test_workspace_empty_roots_fail_closed(tmp_path):
    store = PolicyStore(_config(policy=PolicyConfig(allowed_roots=())))
    with pytest.raises(BridgeError) as excinfo:
        store.check_workspace(str(tmp_path))
    assert excinfo.value.code == WORKSPACE_DENIED


def test_cwd_relative_resolves_against_bridge_cwd(tmp_path):
    store = PolicyStore(
        _config(
            defaults=DefaultsConfig(cwd=None),
            policy=PolicyConfig(allowed_roots=(str(tmp_path),)),
        )
    )
    resolved = store.resolve_cwd("sub/dir", bridge_cwd=str(tmp_path))
    assert resolved.startswith(str(tmp_path))
    assert resolved.endswith("sub/dir")


def test_sandbox_allowlist():
    store = PolicyStore(
        _config(policy=PolicyConfig(allowed_roots=("/r",), allowed_sandboxes=("read-only",)))
    )
    store.check_sandbox("read-only")
    with pytest.raises(BridgeError) as excinfo:
        store.check_sandbox("workspace-write")
    assert excinfo.value.code == WORKSPACE_DENIED


# --- model catalog -----------------------------------------------------------------


def _entry(model_id: str, efforts=("low", "medium", "high"), default=False):
    return ModelCatalogEntry(
        model_id=model_id,
        display_name=model_id,
        supported_reasoning_efforts=efforts,
        default_reasoning_effort=efforts[1] if efforts else None,
        hidden=False,
        is_default=default,
    )


CATALOG = ModelCatalog([_entry("gpt-5.6-terra", default=True), _entry("atlas", ("high", "xhigh"))])


def test_model_unavailable_no_fallback():
    with pytest.raises(BridgeError) as excinfo:
        CATALOG.validate_model("nope", allowed_models=())
    assert excinfo.value.code == "MODEL_UNAVAILABLE"


def test_model_not_allowed_even_in_catalog():
    with pytest.raises(BridgeError) as excinfo:
        CATALOG.validate_model("atlas", allowed_models=("gpt-5.6-terra",))
    assert excinfo.value.code == MODEL_NOT_ALLOWED


def test_effort_validated_against_catalog_not_hardcoded():
    with pytest.raises(BridgeError) as excinfo:
        CATALOG.validate_effort("gpt-5.6-terra", "xhigh")
    assert excinfo.value.code == UNSUPPORTED_EFFORT
    CATALOG.validate_effort("atlas", "xhigh")  # catalog-driven, not hardcoded


def test_resolution_priority_is_coordinator_contract():
    """ModelCatalog.resolve() was removed: its single priority ladder could
    not express the R4 split (new threads must fall back to the runtime's
    per-cwd config; continuations must never re-apply startup defaults).
    The priority contract is now verified end-to-end by CT-04/05/06/07
    (tests/contract/test_gate_b.py) and the external review regressions
    test_R4_* (tests/regression/test_external_review.py).
    """
    catalog = ModelCatalog(
        [_entry("gpt-5.6-terra", default=True), _entry("atlas", ("high", "xhigh"))]
    )
    assert catalog.default_model() == "gpt-5.6-terra"
    catalog.validate_model("atlas", allowed_models=())
    catalog.validate_effort("atlas", "xhigh")


def test_model_change_with_inherited_effort_validation():
    """Continuation safety: switching models keeps the inherited effort and
    must reject unsupported combinations (moved from resolve() into the
    coordinator, still enforced via ModelCatalog.validate_effort)."""
    catalog = ModelCatalog([_entry("gpt-5.6-terra"), _entry("atlas", ("high", "xhigh"))])
    inherited_effort = "xhigh"  # supported by atlas only
    with pytest.raises(BridgeError) as excinfo:
        catalog.validate_effort("gpt-5.6-terra", inherited_effort)
    assert excinfo.value.code == UNSUPPORTED_EFFORT
