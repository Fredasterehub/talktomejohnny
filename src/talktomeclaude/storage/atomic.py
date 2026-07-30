"""Atomic JSON transactions with a bounded cross-process lock.

Windows uses a named kernel mutex derived from the canonical target path.  A
small POSIX flock implementation keeps the same contract available to Linux
development and CI without adding a dependency.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

_IS_WINDOWS = sys.platform == "win32"
_WINDOWS_REPLACE_RETRY_ENABLED = _IS_WINDOWS
_WINDOWS_REPLACE_MONOTONIC = time.monotonic
_WINDOWS_REPLACE_SLEEP = time.sleep
_NATIVE_PATH = type(Path())
_WINDOWS_REPLACE_RETRY_SECONDS = 0.5


class AtomicStorageError(RuntimeError):
    """Base class for durable-storage failures."""


class LockTimeoutError(AtomicStorageError):
    """The transaction could not acquire its lock before the deadline."""


class InvalidJsonError(AtomicStorageError):
    """The durable file is unreadable or does not contain a JSON object."""


def canonical_path(path: str | os.PathLike[str]) -> str:
    """Return a stable path identity, including Windows case folding."""
    value = os.path.realpath(os.path.abspath(os.fspath(path)))
    return os.path.normcase(value)


def lock_identity_for_path(
    path: str | os.PathLike[str], *, purpose: str = "atomic-json"
) -> str:
    """Derive a non-sensitive Windows mutex name from a canonical path."""
    material = f"{purpose}\0{canonical_path(path)}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    return rf"Local\TalkToMeClaude-{purpose}-{digest}"


class _WindowsNamedMutex(AbstractContextManager["_WindowsNamedMutex"]):
    WAIT_OBJECT_0 = 0x00000000
    WAIT_ABANDONED = 0x00000080
    WAIT_TIMEOUT = 0x00000102
    WAIT_FAILED = 0xFFFFFFFF

    def __init__(self, name: str, timeout: float) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        self._kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        self._kernel32.ReleaseMutex.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._name = name
        self._timeout = max(0.0, timeout)
        self._handle: int | None = None
        self.abandoned = False

    def __enter__(self) -> "_WindowsNamedMutex":
        handle = self._kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        self._handle = handle
        milliseconds = min(int(self._timeout * 1000 + 0.999), 0xFFFFFFFE)
        result = self._kernel32.WaitForSingleObject(handle, milliseconds)
        if result == self.WAIT_TIMEOUT:
            self._close()
            raise LockTimeoutError("storage lock acquisition timed out")
        if result == self.WAIT_FAILED:
            error = self._ctypes.get_last_error()
            self._close()
            raise self._ctypes.WinError(error)
        if result not in (self.WAIT_OBJECT_0, self.WAIT_ABANDONED):
            self._close()
            raise AtomicStorageError(f"unexpected mutex wait result: {result:#x}")
        self.abandoned = result == self.WAIT_ABANDONED
        return self

    def _close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is not None:
            if not self._kernel32.ReleaseMutex(self._handle):
                error = self._ctypes.get_last_error()
                self._close()
                raise self._ctypes.WinError(error)
            self._close()


_FALLBACK_LOCKS: dict[str, threading.Lock] = {}
_FALLBACK_LOCKS_GUARD = threading.Lock()


def _fcntl_module() -> Any:
    """Load the POSIX-only module without exposing conditional stubs to mypy."""
    return importlib.import_module("fcntl")


def _posix_lock_path(name: str) -> Path:
    """Return a stable lock path that never changes the durable-data directory."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        root = _NATIVE_PATH(runtime) / "talktomeclaude-locks"
    else:
        user = str(os.getuid()) if hasattr(os, "getuid") else "current"
        root = _NATIVE_PATH(tempfile.gettempdir()) / f"talktomeclaude-{user}-locks"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return root / f"{digest}.lock"


class _PosixNamedMutex(AbstractContextManager["_PosixNamedMutex"]):
    def __init__(self, name: str, timeout: float) -> None:
        self._name = name
        self._timeout = max(0.0, timeout)
        # Never unlink this file per operation: flock locks inodes, so replacing
        # or unlinking it could let two contenders lock different inodes.
        self._lock_path = _posix_lock_path(name)
        self._handle: Any = None
        self._thread_lock: threading.Lock | None = None
        self.abandoned = False

    def __enter__(self) -> "_PosixNamedMutex":
        fcntl = _fcntl_module()

        deadline = time.monotonic() + self._timeout
        with _FALLBACK_LOCKS_GUARD:
            lock = _FALLBACK_LOCKS.setdefault(self._name, threading.Lock())
        if not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise LockTimeoutError("storage lock acquisition timed out")
        self._thread_lock = lock
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._handle = self._lock_path.open("a+b")
            while True:
                try:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeoutError("storage lock acquisition timed out")
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        except BaseException:
            self._cleanup()
            raise

    def _cleanup(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._thread_lock is not None:
            self._thread_lock.release()
            self._thread_lock = None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is not None:
            fcntl = _fcntl_module()
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._cleanup()


def _mutex(name: str, timeout: float, target: Path) -> AbstractContextManager[Any]:
    # Some legacy tests patch os.name to exercise application branches.  The
    # storage backend is a property of the running interpreter, not mutable
    # application state, so select it from sys.platform.
    if _IS_WINDOWS:
        return _WindowsNamedMutex(name, timeout)
    return _PosixNamedMutex(name, timeout)


class AtomicJsonTransaction:
    """Reload, modify, and atomically replace one JSON object under a lock."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timeout: float = 5.0,
        purpose: str = "atomic-json",
        on_event: Callable[[str], None] | None = None,
        phase_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.path = _NATIVE_PATH(canonical_path(path))
        self.timeout = timeout
        self.lock_identity = lock_identity_for_path(self.path, purpose=purpose)
        self._on_event = on_event
        self._phase_hook = phase_hook

    def _emit(self, code: str) -> None:
        if self._on_event is not None:
            self._on_event(code)

    def _phase(self, name: str) -> None:
        if self._phase_hook is not None:
            self._phase_hook(name)

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidJsonError(f"JSON storage is unreadable ({exc})") from exc
        if not isinstance(value, dict):
            raise InvalidJsonError("JSON storage root must be an object")
        return value

    def read(self) -> dict[str, Any]:
        with _mutex(self.lock_identity, self.timeout, self.path) as mutex:
            if mutex.abandoned:
                self._emit("storage_lock_abandoned_recovered")
            return self._read_unlocked()

    def write(self, value: Mapping[str, Any]) -> dict[str, Any]:
        replacement = dict(value)
        return self.update(lambda _current: replacement, force=True)

    def update(
        self,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
        *,
        force: bool = False,
        create_if_missing: bool = True,
    ) -> dict[str, Any]:
        with _mutex(self.lock_identity, self.timeout, self.path) as mutex:
            if mutex.abandoned:
                self._emit("storage_lock_abandoned_recovered")
            existed = self.path.exists()
            if not existed and not create_if_missing:
                return {}
            current = self._read_unlocked()
            working = dict(current)
            result = mutator(working)
            updated = working if result is None else dict(result)
            if force or not existed or updated != current:
                self._write_unlocked(updated)
            self._phase("before_mutex_release")
            return updated

    def _write_unlocked(self, value: Mapping[str, Any]) -> None:
        try:
            encoded = (
                json.dumps(dict(value), indent=2, allow_nan=False) + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise AtomicStorageError(f"JSON storage value is not serializable ({exc})") from exc

        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = _NATIVE_PATH(temporary_name)
        primary_error: BaseException | None = None
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                self._phase("before_temp_flush")
                handle.flush()
                os.fsync(handle.fileno())
            self._phase("after_temp_flush")
            self._phase("before_replace")
            self._replace_with_retry(temporary)
            self._phase("after_replace")
            if self.path.read_bytes() != encoded:
                raise AtomicStorageError("atomic replacement verification failed")
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                if primary_error is None:
                    raise

    def _replace_with_retry(self, temporary: Path) -> None:
        """Tolerate short-lived Windows scanner locks without hiding real failures."""

        deadline: float | None = None
        delay = 0.005
        while True:
            try:
                os.replace(temporary, self.path)
                return
            except PermissionError:
                if not _WINDOWS_REPLACE_RETRY_ENABLED:
                    raise
                now = _WINDOWS_REPLACE_MONOTONIC()
                if deadline is None:
                    deadline = now + min(
                        max(0.0, self.timeout),
                        _WINDOWS_REPLACE_RETRY_SECONDS,
                    )
                remaining = deadline - now
                if remaining <= 0:
                    raise
                _WINDOWS_REPLACE_SLEEP(min(delay, remaining))
                delay = min(delay * 2, 0.05)
