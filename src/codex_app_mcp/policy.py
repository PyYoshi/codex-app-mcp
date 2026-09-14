"""Operator policy enforcement and model catalog resolution (design 6.5, 10.2)."""

from __future__ import annotations

from pathlib import Path, PurePath

from .backend.interface import ModelCatalogEntry
from .config import BridgeConfig, PolicyConfig
from .errors import (
    MODEL_NOT_ALLOWED,
    UNSUPPORTED_APPROVAL_POLICY,
    UNSUPPORTED_EFFORT,
    WORKSPACE_DENIED,
    BridgeError,
)


class PolicyStore:
    """Enforces operator restrictions on workspaces, models, sandboxes."""

    def __init__(self, config: BridgeConfig) -> None:
        self._config = config

    @property
    def policy(self) -> PolicyConfig:
        return self._config.policy

    def resolve_cwd(self, cwd: str | None, *, bridge_cwd: str) -> str:
        """Resolve a call cwd to an absolute path.

        Relative paths resolve against the bridge startup directory; the
        prompt body itself is never trimmed or rewritten (design 5.1).
        """
        raw = cwd or self._config.defaults.cwd
        if raw is None:
            raw = bridge_cwd
        path = Path(raw)
        if not path.is_absolute():
            path = Path(bridge_cwd) / path
        return str(path.resolve())

    def check_workspace(self, absolute_cwd: str) -> None:
        """Fail-closed workspace check against allowed_roots."""
        source = self._config.source_path or "the active bridge configuration"
        if not self.policy.allowed_roots:
            raise BridgeError(
                code=WORKSPACE_DENIED,
                message=(
                    f"no allowed_roots configured in {source}; set [policy].allowed_roots "
                    "to the repository roots this bridge may use"
                ),
            )
        target = Path(absolute_cwd).resolve()
        for root in self.policy.allowed_roots:
            root_path = Path(root).resolve()
            try:
                target.relative_to(root_path)
                return
            except ValueError:
                continue
        raise BridgeError(
            code=WORKSPACE_DENIED,
            message=(
                f"working directory {absolute_cwd} is outside allowed_roots in {source}. "
                "Add this workspace under [policy].allowed_roots if it should be trusted: "
                f"{absolute_cwd}"
            ),
        )

    def check_sandbox(self, sandbox: str) -> None:
        allowed = self.policy.allowed_sandboxes
        if sandbox not in allowed:
            raise BridgeError(
                code=WORKSPACE_DENIED,
                message=(
                    f"sandbox {sandbox!r} is not allowed by operator policy "
                    f"(allowed: {list(allowed)})"
                ),
            )
        if sandbox == "danger-full-access":  # pragma: no cover - guarded by config
            raise BridgeError(
                code=WORKSPACE_DENIED,
                message="danger-full-access is never accepted in v0.1",
            )

    def check_approval_policy(self, approval_policy: str) -> None:
        if approval_policy != "never":
            raise BridgeError(
                code=UNSUPPORTED_APPROVAL_POLICY,
                message=(
                    f"approval-policy {approval_policy!r} is not supported in v0.1; only 'never'"
                ),
            )


class ModelCatalog:
    """Runtime-backed model catalog with allowlist enforcement.

    Design 6.5: efforts come from ``supportedReasoningEfforts``; no hardcoded
    low|medium|high; missing models trigger exactly one refresh by the caller.
    """

    def __init__(self, entries: list[ModelCatalogEntry]) -> None:
        self._entries = {entry.model_id: entry for entry in entries}

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def replace(self, entries: list[ModelCatalogEntry]) -> ModelCatalog:
        return ModelCatalog(entries)

    def entry(self, model_id: str) -> ModelCatalogEntry | None:
        return self._entries.get(model_id)

    def model_ids(self) -> list[str]:
        return sorted(self._entries)

    def default_model(self) -> str | None:
        for entry in self._entries.values():
            if entry.is_default:
                return entry.model_id
        return None

    def efforts_for(self, model_id: str) -> tuple[str, ...]:
        entry = self._entries.get(model_id)
        if entry is None:
            return ()
        return entry.supported_reasoning_efforts

    def validate_model(
        self,
        model: str,
        *,
        allowed_models: tuple[str, ...],
    ) -> ModelCatalogEntry:
        entry = self._entries.get(model)
        if entry is None:
            raise BridgeError(
                code="MODEL_UNAVAILABLE",
                message=(
                    f"model {model!r} is not in the runtime catalog; refusing to "
                    "fall back to another model"
                ),
            )
        if allowed_models and model not in allowed_models:
            raise BridgeError(
                code=MODEL_NOT_ALLOWED,
                message=(f"model {model!r} exists but is not in the operator's allowed_models"),
            )
        return entry

    def validate_effort(self, model: str, effort: str) -> None:
        supported = self.efforts_for(model)
        if supported and effort not in supported:
            raise BridgeError(
                code=UNSUPPORTED_EFFORT,
                message=(
                    f"effort {effort!r} is not supported by model {model!r} "
                    f"(supported: {list(supported)})"
                ),
            )
        if not supported:
            # Catalog did not declare efforts: keep the value but mark as
            # unverified — never silently coerce to low|medium|high.
            return


def is_relative_path(value: str) -> bool:
    return not PurePath(value).is_absolute()
