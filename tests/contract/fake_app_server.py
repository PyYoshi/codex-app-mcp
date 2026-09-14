"""Scripted fake Codex App Server for contract tests.

Launched as a subprocess through ``CodexConfig(launch_args_override=...)``
so the real ``CodexClient`` speaks JSON-RPC with it over stdio. Behavior is
driven by a scenario JSON file; every received/sent message is appended to a
JSONL journal for assertions.

Scenario schema (all fields optional)::

    {
      "initialize": {"serverInfo": {"name": ..., "version": ...}},
      "modelPages": [{"data": [<Model>...], "nextCursor": "c1"|null}, ...],
      "models": [<Model>...],              # shorthand for one page
      "threadStart": {"model": ..., "reasoningEffort": ..., "sandbox": {...},
                      "approvalPolicy": ...},   # response overrides
      "threadResume": {...same...},
      "threadRead": {"model": ..., "cwd": ..., "status": "idle"},
      "turns": [ {"steps": [...]} ],       # matched by turn/start order
      "interrupt": {"notifyCompleted": true, "status": "interrupted"}
    }

Turn steps (executed sequentially in a worker thread when ``turn/start``
arrives; placeholders ``{threadId}``/``{turnId}`` are substituted):

- {"action": "sleep", "ms": 200}
- {"action": "response", "turnStatus": "inProgress"}   # send the RPC result
- {"action": "turnStarted"}
- {"action": "itemCompleted", "item": {...ThreadItem...}}
- {"action": "errorNotify", "message": "...", "willRetry": true}
- {"action": "turnCompleted", "status": "completed"|"failed"|"interrupted",
   "errorMessage": "..."}
- {"action": "approvalRequest", "kind": "commandExecution"|"fileChange",
   "wait": true}
- {"action": "unknownRequest", "method": "...", "wait": true}
- {"action": "crash", "code": 1}
- {"action": "hang"}                                     # never respond

The fake validates outgoing responses against the SDK's generated models
before writing them, so shape drift fails loudly.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid


def journal(fh, event: str, **fields) -> None:
    fh.write(json.dumps({"ts": time.time(), "event": event, **fields}) + "\n")
    fh.flush()


class FakeAppServer:
    def __init__(self, scenario: dict, journal_path: str) -> None:
        self.scenario = scenario
        self.journal_path = journal_path
        self.jfh = open(journal_path, "w", encoding="utf-8")
        self.write_lock = threading.Lock()
        self.thread_counter = 0
        self.turn_counter = 0
        self.request_counter = 0
        self.pending_server_requests: dict[str, dict] = {}
        # (request_id -> {"kind": ..., "params": ..., "response": ...})
        self.turn_scripts: list[dict] = list(scenario.get("turns", []))
        self.lock = threading.Lock()
        self.interrupted_turns: set[str] = set()
        self.thread_cwds: dict[str, str] = {}
        self.thread_models: dict[str, str | None] = {}
        self.thread_efforts: dict[str, str | None] = {}

    # --- output ---------------------------------------------------------

    def send(self, message: dict) -> None:
        line = json.dumps(message, separators=(",", ":"))
        with self.write_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def notify(self, method: str, params: dict) -> None:
        self.send({"method": method, "params": params})
        journal(self.jfh, "sent_notify", method=method, params=params)

    def server_request(self, method: str, params: dict) -> str:
        self.request_counter += 1
        request_id = f"fake-req-{self.request_counter}"
        self.pending_server_requests[request_id] = {
            "method": method,
            "params": params,
            "response": None,
            "responded": threading.Event(),
        }
        self.send({"id": request_id, "method": method, "params": params})
        journal(self.jfh, "sent_request", id=request_id, method=method, params=params)
        return request_id

    # --- model-validated responses ---------------------------------------

    def _thread_obj(self, thread_id: str, cwd: str, overrides: dict) -> dict:
        now = int(time.time())
        return {
            "id": thread_id,
            "createdAt": now,
            "updatedAt": now,
            "cwd": overrides.get("cwd", cwd),
            "ephemeral": False,
            "cliVersion": "0.154.0-fake",
            "modelProvider": "openai",
            "preview": "fake thread",
            "sessionId": f"session-{thread_id}",
            "source": "appServer",
            "status": {"type": "idle"},
            "turns": [],
            "model": overrides.get("model"),
            "reasoningEffort": overrides.get("reasoningEffort"),
        }

    def _thread_response(self, thread_id: str, cwd: str, overrides: dict, kind: str) -> dict:
        from openai_codex.generated.v2_all import (
            ThreadReadResponse,
            ThreadResumeResponse,
            ThreadStartResponse,
        )

        response = {
            "thread": self._thread_obj(thread_id, cwd, overrides),
            "cwd": overrides.get("cwd", cwd),
            "model": overrides.get("model", "gpt-5.6-terra"),
            "modelProvider": "openai",
            "reasoningEffort": overrides.get("reasoningEffort"),
            "approvalPolicy": overrides.get("approvalPolicy", "never"),
            "approvalsReviewer": overrides.get("approvalsReviewer", "user"),
            "sandbox": overrides.get("sandbox", {"type": "readOnly"}),
        }
        if kind == "start":
            ThreadStartResponse.model_validate(response)
        elif kind == "resume":
            ThreadResumeResponse.model_validate(response)
        else:
            ThreadReadResponse.model_validate(response)
        return response

    # --- handlers ---------------------------------------------------------

    def handle_initialize(self, request_id: str, params: dict) -> None:
        from openai_codex.models import InitializeResponse

        if self.scenario.get("hangInitialize"):
            # Never respond: exercises the bridge's startup-timeout cleanup.
            journal(self.jfh, "hang_initialize", id=request_id)
            return
        result = {
            "serverInfo": self.scenario.get(
                "initialize",
                {"name": "fake-app-server", "version": "0.154.0"},
            ),
            "userAgent": "codex-fake/0.154.0",
            "platformFamily": "linux",
        }
        InitializeResponse.model_validate(result)
        self.send({"id": request_id, "result": result})
        journal(self.jfh, "sent_response", id=request_id)

    def handle_model_list(self, request_id: str, params: dict) -> None:
        from openai_codex.generated.v2_all import ModelListResponse

        pages = self.scenario.get("modelPages")
        if pages is None:
            pages = [{"data": self.scenario.get("models", []), "nextCursor": None}]
        cursor = params.get("cursor")
        if cursor is None:
            page = pages[0]
        else:
            page = next((p for p in pages[1:] if p.get("cursorMatch", cursor) == cursor), pages[-1])
        result = {"data": page.get("data", []), "nextCursor": page.get("nextCursor")}
        ModelListResponse.model_validate(result)
        self.send({"id": request_id, "result": result})
        journal(self.jfh, "sent_response", id=request_id, result=result)

    def handle_thread_start(self, request_id: str, params: dict) -> None:
        with self.lock:
            self.thread_counter += 1
            thread_id = f"thread-{self.thread_counter}"
        cwd = params.get("cwd") or os.getcwd()
        self.thread_cwds[thread_id] = cwd
        self.last_thread_cwd = cwd
        self.last_thread_params = params
        overrides = dict(self.scenario.get("threadStart", {}))
        # Echo explicitly requested settings (real runtime confirms them);
        # scenario values act as forced values for mismatch tests.
        overrides.setdefault("model", params.get("model") or "gpt-5.6-terra")
        requested_effort = (params.get("config") or {}).get("model_reasoning_effort")
        overrides.setdefault("reasoningEffort", requested_effort)
        result = self._thread_response(thread_id, cwd, overrides, "start")
        self.thread_models[thread_id] = result.get("model")
        self.thread_efforts[thread_id] = result.get("reasoningEffort")
        self.send({"id": request_id, "result": result})
        self.current_thread = thread_id
        journal(self.jfh, "sent_response", id=request_id, threadId=thread_id)

    def handle_thread_resume(self, request_id: str, params: dict) -> None:
        thread_id = params.get("threadId", "thread-unknown")
        self.current_thread = thread_id
        self.last_resume_params = params
        overrides = dict(self.scenario.get("threadResume", {}))
        # Honor the requested sandbox unless the scenario forces a different
        # one (mismatch tests). SandboxMode string -> SandboxPolicy object.
        requested_sandbox = params.get("sandbox")
        sandbox_map = {
            "read-only": {"type": "readOnly"},
            "workspace-write": {"type": "workspaceWrite"},
            "danger-full-access": {"type": "dangerFullAccess"},
        }
        force = overrides.pop("forceSandbox", None)
        if force is not None:
            overrides["sandbox"] = force
        elif requested_sandbox in sandbox_map:
            overrides["sandbox"] = sandbox_map[requested_sandbox]
        persisted_cwd = self.thread_cwds.get(thread_id)
        # Echo an explicitly resumed model; otherwise keep the persisted one.
        overrides.setdefault("model", params.get("model") or self.thread_models.get(thread_id))
        requested_effort = (params.get("config") or {}).get("model_reasoning_effort")
        overrides.setdefault("reasoningEffort", requested_effort)
        result = self._thread_response(
            thread_id, params.get("cwd") or persisted_cwd or "/tmp", overrides, "resume"
        )
        self.thread_models[thread_id] = result.get("model")
        self.thread_efforts[thread_id] = result.get("reasoningEffort")
        self.send({"id": request_id, "result": result})
        journal(self.jfh, "sent_response", id=request_id, threadId=thread_id)

    def handle_thread_read(self, request_id: str, params: dict) -> None:
        thread_id = params.get("threadId", "thread-unknown")
        overrides = dict(self.scenario.get("threadRead", {}))
        persisted_cwd = self.thread_cwds.get(thread_id)
        resolved_cwd = overrides.get("cwd", persisted_cwd or "/tmp")
        overrides.setdefault("cwd", resolved_cwd)
        result = self._thread_response(thread_id, resolved_cwd, overrides, "read")
        # A read of a persisted thread also surfaces its persisted settings,
        # seeding this (fresh) fake process like the real runtime would.
        self.thread_cwds[thread_id] = result.get("cwd")
        if result.get("model") is not None:
            self.thread_models[thread_id] = result.get("model")
        self.thread_efforts[thread_id] = result.get("reasoningEffort")
        self.send({"id": request_id, "result": result})
        journal(self.jfh, "sent_response", id=request_id, threadId=thread_id)

    def handle_turn_start(self, request_id: str, params: dict) -> None:
        with self.lock:
            self.turn_counter += 1
            turn_id = f"turn-{self.turn_counter}"
        thread_id = params.get("threadId") or getattr(self, "current_thread", "thread-1")
        script = (
            self.turn_scripts[min(self.turn_counter - 1, len(self.turn_scripts) - 1)]
            if self.turn_scripts
            else {}
        )
        # Prompt-matched scripts take precedence over positional selection:
        # fresh processes restart turn numbering, so recovery tests match by
        # prompt text instead.
        prompt_text = ""
        input_items = params.get("input") or []
        if input_items and isinstance(input_items[0], dict):
            prompt_text = str(input_items[0].get("text", ""))
        for candidate in self.scenario.get("turnsByPrompt", []):
            if candidate.get("match") and candidate["match"] in prompt_text:
                script = candidate
                break
        steps = script.get("steps", [{"action": "response"}, {"action": "turnCompleted"}])
        threading.Thread(
            target=self.run_turn_steps,
            args=(request_id, thread_id, turn_id, steps),
            daemon=True,
        ).start()

    def handle_turn_interrupt(self, request_id: str, params: dict) -> None:
        turn_id = params.get("turnId", "")
        self.interrupted_turns.add(turn_id)
        self.send({"id": request_id, "result": {}})
        journal(self.jfh, "sent_response", id=request_id, interrupted=turn_id)
        cfg = self.scenario.get("interrupt", {"notifyCompleted": True})
        if cfg.get("notifyCompleted", True):

            def delayed() -> None:
                time.sleep(cfg.get("delayMs", 0) / 1000.0)
                self.notify_turn_completed(
                    thread_id=params.get("threadId", ""),
                    turn_id=turn_id,
                    status=cfg.get("status", "interrupted"),
                )

            threading.Thread(target=delayed, daemon=True).start()

    # --- turn script execution ---------------------------------------------

    def run_turn_steps(self, request_id: str, thread_id: str, turn_id: str, steps: list) -> None:
        try:
            for step in steps:
                action = step.get("action")
                if action == "sleep":
                    time.sleep(step.get("ms", 0) / 1000.0)
                elif action == "response":
                    from openai_codex.generated.v2_all import TurnStartResponse

                    status = step.get("turnStatus", "inProgress")
                    result = {"turn": {"id": turn_id, "status": status, "items": []}}
                    TurnStartResponse.model_validate(result)
                    self.send({"id": request_id, "result": result})
                    journal(self.jfh, "sent_response", id=request_id, turnId=turn_id)
                elif action == "turnStarted":
                    self.notify(
                        "turn/started",
                        {
                            "threadId": thread_id,
                            "turn": {"id": turn_id, "status": "inProgress", "items": []},
                        },
                    )
                elif action == "itemStarted":
                    self.notify(
                        "item/started",
                        {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "startedAtMs": int(time.time() * 1000),
                            "item": step["item"],
                        },
                    )
                elif action == "itemCompleted":
                    from openai_codex.generated.v2_all import ItemCompletedNotification

                    payload = {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "completedAtMs": int(time.time() * 1000),
                        "item": step["item"],
                    }
                    ItemCompletedNotification.model_validate(payload)
                    self.notify("item/completed", payload)
                elif action == "errorNotify":
                    self.notify(
                        "error",
                        {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "willRetry": step.get("willRetry", False),
                            "error": {"message": step.get("message", "fake error")},
                        },
                    )
                elif action == "turnCompleted":
                    self.notify_turn_completed(
                        thread_id,
                        turn_id,
                        step.get("status", "completed"),
                        step.get("errorMessage"),
                    )
                elif action == "approvalRequest":
                    kind = step.get("kind", "commandExecution")
                    method = f"item/{kind}/requestApproval"
                    rid = self.server_request(
                        method,
                        {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "callId": f"call-{turn_id}",
                            **step.get("params", {}),
                        },
                    )
                    if step.get("wait", True):
                        entry = self.pending_server_requests[rid]
                        entry["responded"].wait(timeout=10)
                elif action == "unknownRequest":
                    rid = self.server_request(
                        step["method"], {"threadId": thread_id, "turnId": turn_id}
                    )
                    if step.get("wait", False):
                        self.pending_server_requests[rid]["responded"].wait(timeout=10)
                elif action == "crash":
                    journal(self.jfh, "crash", code=step.get("code", 1))
                    os._exit(step.get("code", 1))
                elif action == "hang":
                    journal(self.jfh, "hang", id=request_id)
                    return  # never respond; keep process alive
                else:
                    raise ValueError(f"unknown step action: {action!r}")
        except BaseException as exc:  # pragma: no cover - harness guard
            journal(self.jfh, "script_error", error=repr(exc))
            raise

    def notify_turn_completed(
        self, thread_id: str, turn_id: str, status: str, error_message: str | None = None
    ) -> None:
        from openai_codex.generated.v2_all import TurnCompletedNotification

        turn: dict = {"id": turn_id, "status": status, "items": []}
        if error_message:
            turn["error"] = {"message": error_message}
        TurnCompletedNotification.model_validate({"threadId": thread_id, "turn": turn})
        self.notify("turn/completed", {"threadId": thread_id, "turn": turn})

    # --- main loop -----------------------------------------------------------

    def run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            method = message.get("method")
            request_id = message.get("id")
            params = message.get("params") or {}
            if request_id is not None and method is not None:
                # A response to one of our server requests?
                if method not in (
                    "initialize",
                    "model/list",
                    "thread/start",
                    "thread/resume",
                    "thread/read",
                    "turn/start",
                    "turn/interrupt",
                    "account/read",
                ):
                    journal(self.jfh, "unknown_request_method", id=request_id, method=method)
                    self.send(
                        {"id": request_id, "error": {"code": -32601, "message": "method not found"}}
                    )
                    continue
                journal(self.jfh, "request", id=request_id, method=method, params=params)
                if method == "initialize":
                    self.handle_initialize(request_id, params)
                elif method == "model/list":
                    self.handle_model_list(request_id, params)
                elif method == "thread/start":
                    self.handle_thread_start(request_id, params)
                elif method == "thread/resume":
                    self.handle_thread_resume(request_id, params)
                elif method == "thread/read":
                    self.handle_thread_read(request_id, params)
                elif method == "turn/start":
                    self.handle_turn_start(request_id, params)
                elif method == "turn/interrupt":
                    self.handle_turn_interrupt(request_id, params)
                else:
                    self.send(
                        {"id": request_id, "error": {"code": -32601, "message": "method not found"}}
                    )
                continue
            # No id: notification, or a response to a server request we sent.
            if method is not None:
                journal(self.jfh, "notification", method=method, params=params)
                continue
            responded_id = message.get("id") if "id" in message else None
            if responded_id is None:
                continue
            entry = self.pending_server_requests.get(str(responded_id))
            if entry is None:
                continue
            entry["response"] = message.get("result", message.get("error"))
            entry["responded"].set()
            journal(
                self.jfh,
                "client_response",
                id=str(responded_id),
                method=entry["method"],
                result=entry["response"],
            )


def main() -> None:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: fake_app_server.py <scenario.json> <journal.jsonl>\n")
        sys.exit(2)
    with open(sys.argv[1], encoding="utf-8") as fh:
        scenario = json.load(fh)
    # Each run gets a unique instance marker so tests can correlate runs.
    marker = os.environ.get("FAKE_APP_SERVER_MARKER") or str(uuid.uuid4())
    server = FakeAppServer(scenario, sys.argv[2])
    journal(server.jfh, "started", marker=marker, pid=os.getpid())
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    journal(server.jfh, "stdin_closed")


if __name__ == "__main__":
    main()
