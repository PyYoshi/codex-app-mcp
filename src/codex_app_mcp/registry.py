"""Thread registry and advisory locks (design sections 8.2, 8.3).

Bridge memory holds only: current runtime settings, the active run, thread
metadata, and the model catalog cache. Conversation bodies are never
duplicated here — they stay in the Codex runtime.

Advisory locks use ``flock`` on files derived from the normalized
CODEX_HOME plus thread ID (and workspace for write executions). These stop
cooperating bridge processes only — Codex CLI or editors bypass them.
"""

from __future__ import annotations

import errno
import hashlib
import os
import threading
from pathlib import Path

from .errors import SERVER_BUSY_CODE, THREAD_BUSY, BridgeError

THREAD_IDLE = "IDLE"
THREAD_ACTIVE = "ACTIVE"
THREAD_INTERRUPTING = "INTERRUPTING"
THREAD_UNKNOWN = "UNKNOWN"


class ThreadRecord:
    """In-memory metadata for one (possibly saved) thread."""

    __slots__ = ("thread_id", "state", "cwd", "model", "effort", "sandbox_mode", "last_turn_id")

    def __init__(
        self,
        thread_id: str,
        *,
        state: str = THREAD_UNKNOWN,
        cwd: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        sandbox_mode: str | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.state = state
        self.cwd = cwd
        self.model = model
        self.effort = effort
        self.sandbox_mode = sandbox_mode
        self.last_turn_id: str | None = None


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def _lock_path(base: Path, kind: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return base / "codex-app-mcp-locks" / f"{kind}-{digest}.lock"


class AdvisoryLockError(BridgeError):
    """Another cooperating process holds the lock."""


class ThreadRegistry:
    """In-memory thread metadata plus flock-based advisory locks."""

    def __init__(self, codex_home: Path | None = None) -> None:
        self._threads: dict[str, ThreadRecord] = {}
        self._guard = threading.Lock()
        self._codex_home = Path(codex_home) if codex_home is not None else default_codex_home()

    # --- metadata ----------------------------------------------------------

    def get(self, thread_id: str) -> ThreadRecord | None:
        with self._guard:
            return self._threads.get(thread_id)

    def upsert(self, record: ThreadRecord) -> ThreadRecord:
        with self._guard:
            self._threads[record.thread_id] = record
            return record

    def ensure(self, thread_id: str) -> ThreadRecord:
        with self._guard:
            record = self._threads.get(thread_id)
            if record is None:
                record = ThreadRecord(thread_id, state=THREAD_UNKNOWN)
                self._threads[thread_id] = record
            return record

    def set_state(self, thread_id: str, state: str) -> None:
        record = self.ensure(thread_id)
        with self._guard:
            record.state = state

    def state(self, thread_id: str) -> str:
        with self._guard:
            record = self._threads.get(thread_id)
            return record.state if record else THREAD_UNKNOWN

    def update_settings(
        self,
        thread_id: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        cwd: str | None = None,
        sandbox_mode: str | None = None,
    ) -> None:
        """Update thread settings; explicit ``None`` args are ignored."""
        record = self.ensure(thread_id)
        with self._guard:
            if model is not None:
                record.model = model
            if effort is not None:
                record.effort = effort
            if cwd is not None:
                record.cwd = cwd
            if sandbox_mode is not None:
                record.sandbox_mode = sandbox_mode

    # --- advisory locks ------------------------------------------------------

    @property
    def lock_dir(self) -> Path:
        return self._codex_home / "codex-app-mcp-locks"

    def thread_lock(self, thread_id: str) -> _AdvisoryLock:
        key = f"{self._codex_home.resolve()}:{thread_id}"
        return _AdvisoryLock(_lock_path(self._codex_home, "thread", key), THREAD_BUSY, thread_id)

    def workspace_lock(self, workspace: str) -> _AdvisoryLock:
        key = f"{self._codex_home.resolve()}:workspace:{Path(workspace).resolve()}"
        return _AdvisoryLock(_lock_path(self._codex_home, "ws", key), SERVER_BUSY_CODE, None)


class _AdvisoryLock:
    """Class-based flock context manager.

    The coordinator's run lease drives ``__enter__``/``__exit__`` manually,
    and a deliberately retained lease (stop could not be confirmed, F2) must
    not rely on generator finalization — so the lock is a plain class.
    """

    def __init__(self, path: Path, busy_code: str, thread_id: str | None) -> None:
        self._path = path
        self._busy_code = busy_code
        self._thread_id = thread_id
        self._fd: int | None = None

    def __enter__(self) -> _AdvisoryLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AdvisoryLockError(
                    code=self._busy_code,
                    message=(
                        "another cooperating bridge process holds the lock "
                        f"for this {'thread' if self._thread_id else 'workspace'}"
                    ),
                    retryable=True,
                    thread_id=self._thread_id,
                ) from exc
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
