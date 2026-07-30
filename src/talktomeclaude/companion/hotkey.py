"""Thread-owned Win32 global-hotkey pump for the non-activating Tk shell."""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from collections.abc import Callable
from typing import Protocol

from talktomeclaude.platform.windows.hotkeys import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    CtypesHotkeyFacade,
    GlobalHotkeyAdapter,
    HotkeyFacade,
)


WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_PAUSE = 0x13
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_SNAPSHOT = 0x2C
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_OEM_1 = 0xBA
VK_OEM_PLUS = 0xBB
VK_OEM_COMMA = 0xBC
VK_OEM_MINUS = 0xBD
VK_OEM_PERIOD = 0xBE
VK_OEM_2 = 0xBF
VK_OEM_3 = 0xC0
VK_OEM_4 = 0xDB
VK_OEM_5 = 0xDC
VK_OEM_6 = 0xDD
VK_OEM_7 = 0xDE


_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "super": MOD_WIN,
}


_SPECIAL_KEYS = {
    "space": VK_SPACE,
    "spacebar": VK_SPACE,
    "tab": VK_TAB,
    "backspace": VK_BACK,
    "enter": VK_RETURN,
    "return": VK_RETURN,
    "esc": VK_ESCAPE,
    "escape": VK_ESCAPE,
    "home": VK_HOME,
    "end": VK_END,
    "pageup": VK_PRIOR,
    "pgup": VK_PRIOR,
    "pagedown": VK_NEXT,
    "pgdn": VK_NEXT,
    "left": VK_LEFT,
    "right": VK_RIGHT,
    "up": VK_UP,
    "down": VK_DOWN,
    "del": VK_DELETE,
    "delete": VK_DELETE,
    "insert": VK_INSERT,
    "ins": VK_INSERT,
    "pause": VK_PAUSE,
    "printscreen": VK_SNAPSHOT,
}


_SPECIAL_CANONICAL = {
    "spacebar": "space",
    "return": "enter",
    "esc": "escape",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "del": "delete",
    "ins": "insert",
}


_PRINTABLE_KEYS: dict[str, tuple[int, int, str]] = {
    "`": (VK_OEM_3, 0, "`"),
    "grave": (VK_OEM_3, 0, "`"),
    "~": (VK_OEM_3, MOD_SHIFT, "~"),
    "tilde": (VK_OEM_3, MOD_SHIFT, "~"),
    "-": (VK_OEM_MINUS, 0, "-"),
    "_": (VK_OEM_MINUS, MOD_SHIFT, "_"),
    "=": (VK_OEM_PLUS, 0, "="),
    "plus": (VK_OEM_PLUS, MOD_SHIFT, "plus"),
    "[": (VK_OEM_4, 0, "["),
    "{": (VK_OEM_4, MOD_SHIFT, "{"),
    "]": (VK_OEM_6, 0, "]"),
    "}": (VK_OEM_6, MOD_SHIFT, "}"),
    "\\": (VK_OEM_5, 0, "\\"),
    "|": (VK_OEM_5, MOD_SHIFT, "|"),
    ";": (VK_OEM_1, 0, ";"),
    ":": (VK_OEM_1, MOD_SHIFT, ":"),
    "'": (VK_OEM_7, 0, "'"),
    '"': (VK_OEM_7, MOD_SHIFT, '"'),
    ",": (VK_OEM_COMMA, 0, ","),
    "<": (VK_OEM_COMMA, MOD_SHIFT, "<"),
    ".": (VK_OEM_PERIOD, 0, "."),
    ">": (VK_OEM_PERIOD, MOD_SHIFT, ">"),
    "/": (VK_OEM_2, 0, "/"),
    "slash": (VK_OEM_2, 0, "/"),
    "?": (VK_OEM_2, MOD_SHIFT, "?"),
    "question": (VK_OEM_2, MOD_SHIFT, "?"),
    "!": (ord("1"), MOD_SHIFT, "!"),
    "@": (ord("2"), MOD_SHIFT, "@"),
    "#": (ord("3"), MOD_SHIFT, "#"),
    "$": (ord("4"), MOD_SHIFT, "$"),
    "%": (ord("5"), MOD_SHIFT, "%"),
    "^": (ord("6"), MOD_SHIFT, "^"),
    "&": (ord("7"), MOD_SHIFT, "&"),
    "*": (ord("8"), MOD_SHIFT, "*"),
    "(": (ord("9"), MOD_SHIFT, "("),
    ")": (ord("0"), MOD_SHIFT, ")"),
}


_CANONICAL_MODIFIERS = (
    ("ctrl", MOD_CONTROL),
    ("alt", MOD_ALT),
    ("shift", MOD_SHIFT),
    ("win", MOD_WIN),
)


def _parse_function_key(token: str) -> int:
    if token.startswith("f") and len(token) > 1 and token[1:].isdigit():
        number = int(token[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    raise ValueError(f"unknown virtual key {token!r}")


def _parsed_control_hotkey(binding: str) -> tuple[int, int, str]:
    if not isinstance(binding, str) or not binding.strip():
        raise ValueError("control-keybinding cannot be empty")
    cleaned = binding.strip().lower()
    tokens = ["plus"] if cleaned == "+" else [
        part.strip() for part in cleaned.split("+")
    ]
    if not tokens or not tokens[0]:
        raise ValueError(f"invalid control-keybinding {binding!r}")

    modifiers = 0
    virtual_key: int | None = None
    canonical_key: str | None = None
    implied_modifiers = 0
    for token in tokens:
        if not token:
            raise ValueError(f"invalid control-keybinding {binding!r}")
        if token in _MODIFIERS:
            modifiers |= _MODIFIERS[token]
            continue
        if token in _SPECIAL_KEYS:
            if virtual_key is not None:
                raise ValueError(f"multiple keys in binding {binding!r}")
            virtual_key = _SPECIAL_KEYS[token]
            canonical_key = _SPECIAL_CANONICAL.get(token, token)
            continue
        if token.startswith("f") and len(token) > 1 and token[1:].isdigit():
            if virtual_key is not None:
                raise ValueError(f"multiple keys in binding {binding!r}")
            virtual_key = _parse_function_key(token)
            canonical_key = token
            continue
        printable = _PRINTABLE_KEYS.get(token)
        if printable is not None:
            if virtual_key is not None:
                raise ValueError(f"multiple keys in binding {binding!r}")
            virtual_key, implied_modifiers, canonical_key = printable
            modifiers |= implied_modifiers
            continue
        if len(token) == 1 and token.isascii() and token.isalnum():
            if virtual_key is not None:
                raise ValueError(f"multiple keys in binding {binding!r}")
            virtual_key = ord(token.upper())
            canonical_key = token
            continue
        raise ValueError(f"unknown control-key token {token!r} in {binding!r}")
    if virtual_key is None or canonical_key is None:
        raise ValueError(f"binding has no key in {binding!r}")
    displayed_modifiers = modifiers & ~implied_modifiers
    prefix = [
        name for name, mask in _CANONICAL_MODIFIERS if displayed_modifiers & mask
    ]
    return modifiers, virtual_key, "+".join((*prefix, canonical_key))


def parse_control_hotkey(binding: str) -> tuple[int, int]:
    """Parse a keyboard shortcut into Win32 ``RegisterHotKey`` arguments."""

    modifiers, virtual_key, _canonical = _parsed_control_hotkey(binding)
    return modifiers, virtual_key


def normalize_control_hotkey(binding: str) -> str:
    """Return the stable, user-facing spelling of a valid control shortcut."""

    return _parsed_control_hotkey(binding)[2]


class MessagePump(Protocol):
    def prepare(self) -> int: ...

    def next_message(self) -> tuple[int, int] | None: ...

    def post_quit(self, thread_id: int) -> bool: ...


class KeyState(Protocol):
    def is_pressed(self, virtual_key: int) -> bool: ...


class _CtypesKeyState:
    def __init__(self) -> None:
        from ctypes import wintypes

        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetAsyncKeyState.argtypes = (wintypes.INT,)
        self._user32.GetAsyncKeyState.restype = wintypes.SHORT

    def is_pressed(self, virtual_key: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(virtual_key) & 0x8000)


class _CtypesMessagePump:
    def __init__(self) -> None:
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", POINT),
            ]

        self._message_type = MSG
        self._message = MSG()
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32.PeekMessageW.argtypes = (
            ctypes.POINTER(MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.PeekMessageW.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = (
            ctypes.POINTER(MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.GetMessageW.restype = ctypes.c_int
        self._user32.PostThreadMessageW.argtypes = (
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self._user32.PostThreadMessageW.restype = wintypes.BOOL
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def prepare(self) -> int:
        # Peek creates this thread's message queue without removing anything.
        self._user32.PeekMessageW(
            ctypes.byref(self._message), None, 0, 0, PM_NOREMOVE
        )
        return int(self._kernel32.GetCurrentThreadId())

    def next_message(self) -> tuple[int, int] | None:
        result = self._user32.GetMessageW(
            ctypes.byref(self._message), None, 0, 0
        )
        if result == 0:
            return None
        if result < 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(self._message.message), int(self._message.wParam)

    def post_quit(self, thread_id: int) -> bool:
        return bool(self._user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0))


class ThreadHotkeyListener:
    """Register and pump one no-repeat hotkey without touching Tk's thread."""

    def __init__(
        self,
        callback: Callable[[], bool | None],
        *,
        release_callback: Callable[[], None] | None = None,
        modifiers: int = MOD_CONTROL | MOD_ALT,
        virtual_key: int = 0x20,
        hotkey_id: int = 1,
        startup_deadline_seconds: float = 2.0,
        shutdown_deadline_seconds: float = 2.0,
        release_poll_seconds: float = 0.01,
        pump_factory: Callable[[], MessagePump] = _CtypesMessagePump,
        facade_factory: Callable[[], HotkeyFacade] = CtypesHotkeyFacade,
        key_state_factory: Callable[[], KeyState] = _CtypesKeyState,
    ) -> None:
        if (
            startup_deadline_seconds <= 0
            or shutdown_deadline_seconds <= 0
            or release_poll_seconds <= 0
        ):
            raise ValueError("hotkey lifecycle deadlines must be positive")
        self._callback = callback
        self._release_callback = release_callback
        self._modifiers = modifiers
        self._virtual_key = virtual_key
        self._hotkey_id = hotkey_id
        self._startup_deadline = startup_deadline_seconds
        self._shutdown_deadline = shutdown_deadline_seconds
        self._release_poll_seconds = release_poll_seconds
        self._pump_factory = pump_factory
        self._facade_factory = facade_factory
        self._key_state_factory = key_state_factory
        self._intents: queue.SimpleQueue[int | None] = queue.SimpleQueue()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._stop_requested = threading.Event()
        self._guard = threading.Lock()
        self._pump: MessagePump | None = None
        self._thread_id: int | None = None
        self._error: BaseException | None = None
        self._owner: threading.Thread | None = None
        self._dispatcher: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        with self._guard:
            if self._started:
                return
            self._started = True
            self._dispatcher = threading.Thread(
                target=self._dispatch,
                name="ttc-hotkey-intent-dispatch",
                daemon=True,
            )
            self._owner = threading.Thread(
                target=self._run,
                name="ttc-win32-hotkey-pump",
                daemon=True,
            )
            self._dispatcher.start()
            self._owner.start()
        if not self._ready.wait(self._startup_deadline):
            self.stop()
            raise TimeoutError("global hotkey registration timed out")
        if self._error is not None:
            error = self._error
            self.stop()
            raise RuntimeError("global hotkey registration failed") from error

    def _run(self) -> None:
        adapter: GlobalHotkeyAdapter | None = None
        try:
            pump = self._pump_factory()
            self._pump = pump
            self._thread_id = pump.prepare()
            adapter = GlobalHotkeyAdapter(
                self._facade_factory(), hwnd=0, intent_queue=self._intents
            )
            adapter.register(self._hotkey_id, self._modifiers, self._virtual_key)
            self._ready.set()
            while True:
                message = pump.next_message()
                if message is None:
                    break
                adapter.dispatch_message(*message)
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception as exc:
                    if self._error is None:
                        self._error = exc
            self._intents.put(None)
            self._stopped.set()

    def _dispatch(self) -> None:
        key_state: KeyState | None = None
        while True:
            intent = self._intents.get()
            if intent is None or self._stop_requested.is_set():
                return
            if intent != self._hotkey_id:
                continue
            try:
                monitor_release = self._callback() is True
                if monitor_release and self._release_callback is not None:
                    key_state = key_state or self._key_state_factory()
                    while key_state.is_pressed(self._virtual_key):
                        if self._stop_requested.wait(self._release_poll_seconds):
                            return
                    if not self._stop_requested.is_set():
                        self._release_callback()
            except Exception:
                pass

    def stop(self) -> bool:
        self._stop_requested.set()
        self._intents.put(None)
        with self._guard:
            if not self._started:
                return True
            pump = self._pump
            thread_id = self._thread_id
        if pump is not None and thread_id is not None:
            pump.post_quit(thread_id)
        with self._guard:
            owner = self._owner
            dispatcher = self._dispatcher
        deadline = time.monotonic() + self._shutdown_deadline
        if owner is not None:
            owner.join(max(0.0, deadline - time.monotonic()))
        if dispatcher is not None:
            dispatcher.join(max(0.0, deadline - time.monotonic()))
        return bool(
            self._stopped.is_set()
            and (owner is None or not owner.is_alive())
            and (dispatcher is None or not dispatcher.is_alive())
        )


class ReconfigurableHotkeyListener:
    """Keep the current global hotkey live while validating a replacement."""

    def __init__(
        self,
        callback: Callable[[], bool | None],
        *,
        release_callback: Callable[[], None] | None = None,
        modifiers: int = MOD_CONTROL | MOD_ALT,
        virtual_key: int = VK_SPACE,
        listener_factory: Callable[..., ThreadHotkeyListener] = ThreadHotkeyListener,
    ) -> None:
        self._callback = callback
        self._release_callback = release_callback
        self._binding = self._validated_binding(modifiers, virtual_key)
        self._listener_factory = listener_factory
        self._active: ThreadHotkeyListener | None = None
        self._started = False
        self._guard = threading.RLock()

    @staticmethod
    def _validated_binding(modifiers: int, virtual_key: int) -> tuple[int, int]:
        if type(modifiers) is not int or modifiers < 0:
            raise ValueError("hotkey modifiers must be a non-negative integer")
        if type(virtual_key) is not int or not 1 <= virtual_key <= 0xFF:
            raise ValueError("hotkey virtual key must be in [1, 255]")
        return modifiers, virtual_key

    @property
    def binding(self) -> tuple[int, int]:
        with self._guard:
            return self._binding

    def _new_listener(self, binding: tuple[int, int]) -> ThreadHotkeyListener:
        modifiers, virtual_key = binding
        return self._listener_factory(
            self._callback,
            release_callback=self._release_callback,
            modifiers=modifiers,
            virtual_key=virtual_key,
        )

    def start(self) -> None:
        with self._guard:
            if self._started:
                return
            candidate = self._new_listener(self._binding)
            candidate.start()
            self._active = candidate
            self._started = True

    def rebind(self, modifiers: int, virtual_key: int) -> None:
        replacement = self._validated_binding(modifiers, virtual_key)
        with self._guard:
            if replacement == self._binding:
                return
            if not self._started:
                self._binding = replacement
                return
            previous = self._active
            if previous is None:
                raise RuntimeError("active global hotkey is unavailable")
            candidate = self._new_listener(replacement)
            candidate.start()
            try:
                previous_stopped = previous.stop()
            except BaseException:
                candidate.stop()
                raise
            if not previous_stopped:
                candidate.stop()
                raise RuntimeError("previous global hotkey did not stop cleanly")
            self._active = candidate
            self._binding = replacement

    def stop(self) -> bool:
        with self._guard:
            if not self._started:
                return True
            active = self._active
            if active is None:
                return False
            stopped = active.stop()
            if stopped:
                self._active = None
                self._started = False
            return stopped


__all__ = [
    "ReconfigurableHotkeyListener",
    "ThreadHotkeyListener",
    "normalize_control_hotkey",
    "parse_control_hotkey",
]
