"""CLI entry points: ``serve`` and ``doctor`` (design sections 10.4, 12)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from . import CHILD_ENV_FLAG, __version__
from .config import CliOverrides, load_bridge_config
from .errors import BridgeError
from .logs import setup_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-app-mcp",
        description=(
            "Local MCP bridge exposing Codex App Server threads/turns as the "
            "codex / codex-reply MCP tools (noninteractive v0.1 subset)."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server over stdio")
    serve.add_argument("--config", type=str, default=None, help="bridge.toml path")
    serve.add_argument("--model", type=str, default=None)
    serve.add_argument("--effort", type=str, default=None)
    serve.add_argument("--cwd", type=str, default=None)
    serve.add_argument(
        "--sandbox", type=str, default=None, choices=["read-only", "workspace-write"]
    )
    serve.add_argument("--approval-policy", type=str, default=None, choices=["never"])

    doctor = sub.add_parser("doctor", help="verify SDK/runtime/auth/config health")
    doctor.add_argument("--config", type=str, default=None)
    doctor.add_argument(
        "--skip-runtime",
        action="store_true",
        help="skip starting the runtime (version/auth-only checks)",
    )
    init = sub.add_parser("init", help="create a minimal fail-closed bridge.toml")
    destination = init.add_mutually_exclusive_group()
    destination.add_argument("--global", dest="global_config", action="store_true")
    destination.add_argument("--config", type=str, default=None)
    init.add_argument("--root", type=str, default=None, help="workspace root to allow")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Self-connection guard (design 10.4): a bridge launched from the Codex
    # side that itself consumes this bridge would recurse.
    if os.environ.get(CHILD_ENV_FLAG) == "1" and args.command == "serve":
        sys.stderr.write(
            f"refusing to start: {CHILD_ENV_FLAG}=1 indicates this bridge was "
            "launched inside a Codex runtime that may connect back to it "
            "(recursive self-connection guard)\n"
        )
        return 3

    if args.command == "serve":
        return asyncio.run(_serve(args))
    if args.command == "doctor":
        return asyncio.run(_doctor(args))
    if args.command == "init":
        return _init(args)
    parser.error(f"unknown command: {args.command}")
    return 2


async def _serve(args: argparse.Namespace) -> int:
    try:
        from .config import discover_bridge_config, require_operational_policy

        config_path = discover_bridge_config(args.config)
        if config_path is None:
            raise BridgeError(
                code="VALIDATION_ERROR",
                message=(
                    f"no bridge.toml found from {os.getcwd()!r} or the user config directory; "
                    "run 'codex-app-mcp init' or pass --config PATH"
                ),
            )
        config = load_bridge_config(
            config_path,
            CliOverrides(
                model=args.model,
                effort=args.effort,
                cwd=args.cwd,
                sandbox=args.sandbox,
                approval_policy=args.approval_policy,
            ),
        )
        require_operational_policy(config)
    except BridgeError as error:
        sys.stderr.write(f"configuration error [{error.code}]: {error.message}\n")
        return 2
    setup_logging(config.logging.level, config.logging.format)
    from .server import serve_stdio

    try:
        await serve_stdio(config)
    except Exception:  # noqa: BLE001 - G3: unconfirmed shutdown is a failure
        _logger = logging.getLogger("codex_app_mcp.cli")
        _logger.exception("shutdown could not be confirmed; exiting with failure")
        return 1
    return 0


async def _doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor

    report = await run_doctor(config_path=args.config, skip_runtime=args.skip_runtime)
    json.dump(report, sys.stderr, ensure_ascii=False, indent=2)
    sys.stderr.write("\n")
    return 0 if report["ok"] else 1


def _init(args: argparse.Namespace) -> int:
    from .config import find_workspace_root, user_config_path

    root = (
        Path(args.root).expanduser().resolve() if args.root is not None else find_workspace_root()
    )
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser().resolve()
    if not root.is_dir():
        sys.stderr.write(
            f"configuration error [VALIDATION_ERROR]: root is not a directory: {root}\n"
        )
        return 2
    if root == Path(root.anchor) or root == home:
        sys.stderr.write(
            "configuration error [VALIDATION_ERROR]: refusing to generate a broad "
            f"allowed_root for {root}; pass --root with a narrower workspace\n"
        )
        return 2

    if args.global_config:
        destination = user_config_path()
    elif args.config:
        destination = Path(args.config).expanduser().resolve()
    else:
        destination = root / "bridge.toml"

    if destination.exists():
        try:
            config = load_bridge_config(destination)
        except BridgeError as error:
            sys.stderr.write(
                f"configuration error [{error.code}]: existing {destination} was not changed: "
                f"{error.message}\n"
            )
            return 2
        if any(
            _contains(Path(item).expanduser().resolve(), root)
            for item in config.policy.allowed_roots
        ):
            sys.stdout.write(f"existing configuration already allows {root}: {destination}\n")
            return 0
        sys.stderr.write(
            f"existing configuration was not changed: {destination}\n"
            f"Add this workspace under [policy].allowed_roots:\n  {root}\n"
        )
        return 2

    destination.parent.mkdir(parents=True, exist_ok=True)
    contents = (
        "[policy]\n"
        f"allowed_roots = [{json.dumps(str(root), ensure_ascii=False)}]\n"
        'allowed_sandboxes = ["read-only", "workspace-write"]\n'
    )
    try:
        with destination.open("x", encoding="utf-8") as fh:
            fh.write(contents)
    except FileExistsError:
        sys.stderr.write(
            f"configuration appeared concurrently and was not changed: {destination}\n"
        )
        return 2
    sys.stdout.write(
        f"created {destination}\nallowed workspace: {root}\n"
        "This file may contain machine-local paths; exclude it from Git when appropriate.\n"
    )
    return 0


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
