"""stderr JSON logging (design section 10.3).

stdout is reserved for MCP. Diagnostics go to stderr as one JSON object per
line. Prompts, generated text, file contents, auth tokens, and raw SDK
stderr are never logged by this bridge — the log call sites only receive
IDs, settings provenance, durations, and result kinds.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

_MAX_FIELD = 400


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in {
                "args",
                "asctime",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
                "taskName",
            }:
                continue
            payload[key] = _safe_value(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)[: _MAX_FIELD * 4]
        return json.dumps(payload, ensure_ascii=False, default=str)


def _safe_value(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)
    return text[:_MAX_FIELD]


def setup_logging(level: str, fmt: str) -> None:
    """Configure root logging to stderr. Text mode for humans, JSON default."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
