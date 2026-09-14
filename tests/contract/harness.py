"""Contract-test harness: scripted fake App Server + backend factory."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from codex_app_mcp.backend.codex_sdk import CodexSdkBackend

FAKE_SERVER_SCRIPT = Path(__file__).with_name("fake_app_server.py")

DEFAULT_MODELS = [
    {
        "id": "gpt-5.6-terra",
        "displayName": "Terra",
        "description": "fake terra model",
        "hidden": False,
        "isDefault": True,
        "model": "gpt-5.6-terra",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "low"},
            {"reasoningEffort": "medium", "description": "medium"},
            {"reasoningEffort": "high", "description": "high"},
        ],
    },
    {
        "id": "gpt-5.6-atlas",
        "displayName": "Atlas",
        "description": "fake atlas model",
        "hidden": True,
        "isDefault": False,
        "model": "gpt-5.6-atlas",
        "defaultReasoningEffort": "high",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "high", "description": "high"},
            {"reasoningEffort": "xhigh", "description": "xhigh"},
        ],
    },
]

FINAL_ANSWER_ITEM = {
    "type": "agentMessage",
    "id": "item-final",
    "text": "レビュー結果です。",
    "phase": "final_answer",
}
COMMENTARY_ITEM = {
    "type": "agentMessage",
    "id": "item-comment",
    "text": "途中のコメントです。",
    "phase": "commentary",
}
NO_PHASE_ITEM = {
    "type": "agentMessage",
    "id": "item-nophase",
    "text": "phase 未指定の応答です。",
    "phase": None,
}


@dataclass
class FakeRuntime:
    backend: CodexSdkBackend
    journal_path: Path

    def journal(self) -> list[dict]:
        events: list[dict] = []
        if not self.journal_path.exists():
            return events
        with open(self.journal_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def events(self, kind: str) -> list[dict]:
        return [e for e in self.journal() if e.get("event") == kind]

    async def wait_for(
        self,
        predicate: Callable[[list[dict]], bool],
        *,
        timeout: float = 5.0,
        interval: float = 0.02,
    ) -> list[dict]:
        deadline = time.monotonic() + timeout
        while True:
            events = self.journal()
            if predicate(events):
                return events
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"condition not met within {timeout}s; journal tail: "
                    f"{json.dumps(events[-12:], ensure_ascii=False)}"
                )
            await asyncio.sleep(interval)
