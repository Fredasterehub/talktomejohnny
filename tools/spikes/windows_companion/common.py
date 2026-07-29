"""Standard-library helpers shared by the isolated Windows shell candidates."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Callable


PROTOCOL_VERSION = 1
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
HWND_MESSAGE = wintypes.HWND(-3)


class JsonEmitter:
    """Serialize one versioned NDJSON message at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def emit(self, kind: str, **fields: Any) -> None:
        if fields.get("version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            raise ValueError("unsupported outgoing protocol version")
        payload = {**fields, "version": PROTOCOL_VERSION, "kind": kind}
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            sys.stdout.write(data + "\n")
            sys.stdout.flush()


def read_ndjson(target: queue.Queue[dict[str, Any]]) -> None:
    """Read commands without touching a candidate UI from the reader thread."""

    for raw in sys.stdin:
        try:
            message = json.loads(raw)
            if message.get("version") != PROTOCOL_VERSION:
                raise ValueError("unsupported protocol version")
            target.put(message)
        except Exception as exc:  # spike diagnostics stay content-free
            target.put({"version": PROTOCOL_VERSION, "kind": "protocol_error", "error": type(exc).__name__})


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class HotkeyWindow:
    """Own RegisterHotKey on a dedicated Win32 message-window thread."""

    def __init__(self, virtual_key: int, callback: Callable[[int], None]) -> None:
        self.virtual_key = virtual_key
        self.callback = callback
        self._ready = threading.Event()
        self._error: str | None = None
        self._thread_id = 0
        self._hwnd = 0
        self._count = 0
        self._wndproc: WNDPROC | None = None
        self._thread = threading.Thread(target=self._run, name="spike-hotkey", daemon=True)

    def start(self, timeout: float = 5.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("hotkey message window did not start")
        if self._error:
            raise RuntimeError(self._error)

    def close(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        self._thread_id = kernel32.GetCurrentThreadId()
        class_name = f"TalkToMeJohnnySpikeHotkey_{os.getpid()}_{self.virtual_key}"

        @WNDPROC
        def wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_HOTKEY:
                self._count += 1
                self.callback(self._count)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = wndproc
        wc = WNDCLASSW()
        wc.lpfnWndProc = wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            self._error = f"RegisterClassW:{ctypes.get_last_error()}"
            self._ready.set()
            return
        self._hwnd = user32.CreateWindowExW(
            0, class_name, class_name, 0, 0, 0, 0, 0, HWND_MESSAGE, 0, wc.hInstance, None
        )
        if not self._hwnd:
            self._error = f"CreateWindowExW:{ctypes.get_last_error()}"
            self._ready.set()
            return
        modifiers = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
        if not user32.RegisterHotKey(self._hwnd, 1, modifiers, self.virtual_key):
            self._error = f"RegisterHotKey:{ctypes.get_last_error()}"
            self._ready.set()
            return
        self._ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        user32.UnregisterHotKey(self._hwnd, 1)
        user32.DestroyWindow(self._hwnd)
        user32.UnregisterClassW(class_name, wc.hInstance)


def monotonic_ns() -> int:
    return time.perf_counter_ns()
