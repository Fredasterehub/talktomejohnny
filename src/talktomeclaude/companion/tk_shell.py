"""Compact non-activating Tk shell for the Windows companion."""

from __future__ import annotations

import ctypes
import importlib
import queue
from collections.abc import Callable
from typing import Any, Protocol

from talktomeclaude.companion.contracts import (
    CompanionIntent,
    CompanionSnapshot,
    IntentKind,
)
from talktomeclaude.companion.viewmodel import to_view_model


class WindowPolicy(Protocol):
    def apply(self, widget_handle: int) -> int: ...

    def show_without_activation(self, window_handle: int) -> None: ...

    def release(self, window_handle: int) -> None: ...


class Win32WindowApi(Protocol):
    def top_level(self, widget_handle: int) -> int: ...

    def extended_style(self, window_handle: int) -> int: ...

    def set_extended_style(self, window_handle: int, style: int) -> None: ...

    def set_topmost_without_activation(self, window_handle: int) -> None: ...

    def show_without_activation(self, window_handle: int) -> None: ...


class _CtypesWin32WindowApi:
    GWL_EXSTYLE = -20
    SW_SHOWNOACTIVATE = 4
    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020

    def __init__(self) -> None:
        from ctypes import wintypes

        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetParent.argtypes = (wintypes.HWND,)
        self._user32.GetParent.restype = wintypes.HWND
        self._user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
        self._user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self._user32.SetWindowLongPtrW.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_ssize_t,
        )
        self._user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        self._user32.SetWindowPos.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        self._user32.SetWindowPos.restype = wintypes.BOOL
        self._user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self._user32.ShowWindow.restype = wintypes.BOOL

    def top_level(self, widget_handle: int) -> int:
        parent = self._user32.GetParent(widget_handle)
        return int(parent or widget_handle)

    def extended_style(self, window_handle: int) -> int:
        return int(self._user32.GetWindowLongPtrW(window_handle, self.GWL_EXSTYLE))

    def set_extended_style(self, window_handle: int, style: int) -> None:
        ctypes.set_last_error(0)
        prior = self._user32.SetWindowLongPtrW(
            window_handle, self.GWL_EXSTYLE, style
        )
        if prior == 0 and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())

    def set_topmost_without_activation(self, window_handle: int) -> None:
        flags = (
            self.SWP_NOSIZE
            | self.SWP_NOMOVE
            | self.SWP_NOACTIVATE
            | self.SWP_FRAMECHANGED
        )
        if not self._user32.SetWindowPos(
            window_handle, self.HWND_TOPMOST, 0, 0, 0, 0, flags
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def show_without_activation(self, window_handle: int) -> None:
        self._user32.ShowWindow(window_handle, self.SW_SHOWNOACTIVATE)


class WindowsNonActivatingPolicy:
    """Apply native no-activate/tool-window semantics before first show."""

    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000

    def __init__(self, api: Win32WindowApi | None = None) -> None:
        self._api = api or _CtypesWin32WindowApi()

    def apply(self, widget_handle: int) -> int:
        handle = self._api.top_level(widget_handle)
        style = self._api.extended_style(handle)
        self._api.set_extended_style(
            handle, style | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE
        )
        self._api.set_topmost_without_activation(handle)
        return handle

    def show_without_activation(self, window_handle: int) -> None:
        self._api.show_without_activation(window_handle)

    def release(self, window_handle: int) -> None:
        del window_handle


DispatchIntent = Callable[[CompanionIntent], CompanionSnapshot | None]


class TkCompanionShell:
    """Tk presentation that never focuses itself for runtime updates."""

    POLL_MILLISECONDS = 50

    def __init__(
        self,
        dispatch: DispatchIntent,
        initial_snapshot: CompanionSnapshot,
        *,
        tk_module: Any | None = None,
        window_policy: WindowPolicy | None = None,
        poll_milliseconds: int = POLL_MILLISECONDS,
    ) -> None:
        if poll_milliseconds < 1:
            raise ValueError("Tk update poll must be positive")
        self._dispatch = dispatch
        self._tk = tk_module or importlib.import_module("tkinter")
        self._policy = window_policy or WindowsNonActivatingPolicy()
        self._poll_milliseconds = poll_milliseconds
        self._updates: queue.SimpleQueue[CompanionSnapshot] = queue.SimpleQueue()
        self._closed = False
        self._window_handle: int | None = None
        self._after_handle: object | None = None
        self._snapshot = initial_snapshot
        self.root = self._tk.Tk()
        self.root.withdraw()
        self.root.title("TalkToMeJohnny")
        self.root.geometry("360x190")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self._cue = self._tk.StringVar(value="")
        self._status = self._tk.StringVar(value="")
        self._detail = self._tk.StringVar(value="")
        self._mute_text = self._tk.StringVar(value="Mute output")
        self.buttons: dict[IntentKind, Any] = {}
        self._build()
        self._apply_snapshot(initial_snapshot)
        self.root.update_idletasks()
        self._window_handle = self._policy.apply(int(self.root.winfo_id()))
        self._policy.show_without_activation(self._window_handle)
        self._schedule_updates()

    def _build(self) -> None:
        frame = self._tk.Frame(self.root, padx=8, pady=8)
        frame.grid(row=0, column=0, sticky="nsew")
        self._tk.Label(frame, textvariable=self._cue, anchor="w").grid(
            row=0, column=0, columnspan=4, sticky="ew"
        )
        self._tk.Label(frame, textvariable=self._status, anchor="w").grid(
            row=1, column=0, columnspan=4, sticky="ew"
        )
        self._tk.Label(frame, textvariable=self._detail, anchor="w").grid(
            row=2, column=0, columnspan=4, sticky="ew"
        )
        controls = (
            (IntentKind.START_RECORDING, "Start", False),
            (IntentKind.FINISH_RECORDING, "Finish", False),
            (IntentKind.TOGGLE_OUTPUT_MUTE, self._mute_text, False),
            (IntentKind.OPEN_SETTINGS, "Settings", True),
            (IntentKind.OPEN_VOICE, "Voice", True),
            (IntentKind.OPEN_REVIEW, "Review", True),
            (IntentKind.OPEN_DIAGNOSTICS, "Diagnostics", True),
            (IntentKind.QUIT, "Quit", False),
        )
        for index, (kind, label, allow_focus) in enumerate(controls):
            label_option = (
                {"textvariable": label}
                if hasattr(label, "get")
                else {"text": label}
            )
            command = (
                self._quit
                if kind is IntentKind.QUIT
                else self._intent_command(kind, allow_focus=allow_focus)
            )
            button = self._tk.Button(frame, command=command, **label_option)
            button.grid(row=3 + index // 4, column=index % 4, sticky="ew")
            self.buttons[kind] = button

    def _intent_command(
        self, kind: IntentKind, *, allow_focus: bool
    ) -> Callable[[], None]:
        def dispatch() -> None:
            self._intent(kind, allow_focus=allow_focus)

        return dispatch

    def _schedule_updates(self) -> None:
        if not self._closed:
            self._after_handle = self.root.after(
                self._poll_milliseconds, self._drain_updates
            )

    def publish(self, snapshot: CompanionSnapshot) -> None:
        """Queue an unsolicited state update without touching window focus."""

        if not self._closed:
            self._updates.put(snapshot)

    def _drain_updates(self) -> None:
        if self._closed:
            return
        latest: CompanionSnapshot | None = None
        while True:
            try:
                latest = self._updates.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self._apply_snapshot(latest)
        self._schedule_updates()

    def _apply_snapshot(self, snapshot: CompanionSnapshot) -> None:
        self._snapshot = snapshot
        view = to_view_model(snapshot)
        self._cue.set(f"[{view.cue}]")
        self._status.set(view.status)
        self._detail.set(view.detail)
        self._mute_text.set("Unmute output" if snapshot.output_muted else "Mute output")
        self.buttons[IntentKind.START_RECORDING].configure(
            state=self._tk.NORMAL if view.can_start_recording else self._tk.DISABLED
        )
        self.buttons[IntentKind.FINISH_RECORDING].configure(
            state=self._tk.NORMAL if view.can_finish_recording else self._tk.DISABLED
        )
        self.buttons[IntentKind.OPEN_REVIEW].configure(
            state=(
                self._tk.NORMAL
                if view.phase.value == "awaiting_confirmation"
                else self._tk.DISABLED
            )
        )

    def _intent(self, kind: IntentKind, *, allow_focus: bool = False) -> None:
        if self._closed:
            return
        try:
            snapshot = self._dispatch(CompanionIntent(kind, allow_focus=allow_focus))
        except Exception:
            self._detail.set("Action unavailable")
            return
        if snapshot is not None:
            self._apply_snapshot(snapshot)

    def _quit(self) -> None:
        if self._closed:
            return
        # The application owns one absolute shutdown deadline after mainloop
        # returns.  Dispatching QUIT here would run controller teardown first
        # and then start a second hotkey/application deadline.
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._after_handle is not None:
            try:
                self.root.after_cancel(self._after_handle)
            except Exception:
                pass
            self._after_handle = None
        if self._window_handle is not None:
            try:
                self._policy.release(self._window_handle)
            except Exception:
                pass
            self._window_handle = None
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
