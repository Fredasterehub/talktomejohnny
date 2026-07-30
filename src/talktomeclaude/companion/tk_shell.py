"""Compact non-activating Tk shell for the Windows companion."""

from __future__ import annotations

import ctypes
import importlib
import queue
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

from talktomeclaude.companion.contracts import (
    CompanionIntent,
    CompanionSnapshot,
    IntentKind,
)
from talktomeclaude.companion.tk_matrix import MatrixDeck, matrix_visual, microphone_label
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
        self.root.geometry("520x326")
        self.root.resizable(False, False)
        self.root.configure(background="#010403")
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self._cue = self._tk.StringVar(value="")
        self._status = self._tk.StringVar(value="")
        self._detail = self._tk.StringVar(value="")
        self._mute_text = self._tk.StringVar(value="MUTE")
        self._microphone_level = self._tk.StringVar(value="")
        self._output_status = self._tk.StringVar(value="")
        self.buttons: dict[IntentKind, Any] = {}
        self._build()
        self._apply_snapshot(initial_snapshot)
        self.root.update_idletasks()
        self._window_handle = self._policy.apply(int(self.root.winfo_id()))
        self._policy.show_without_activation(self._window_handle)
        self._schedule_updates()

    def _build(self) -> None:
        frame = self._tk.Frame(self.root, padx=0, pady=0, background="#010403")
        frame.grid(row=0, column=0, sticky="nsew")
        self._canvas = self._tk.Canvas(
            frame,
            width=MatrixDeck.WIDTH,
            height=MatrixDeck.HEIGHT,
            background="#010403",
            highlightthickness=0,
            borderwidth=0,
        )
        self._canvas.grid(row=0, column=0, columnspan=8, sticky="nsew")
        self._deck = MatrixDeck(self._canvas)
        semantic_labels = (
            (self._cue, 252, 16, 242, 22, ("Bahnschrift SemiBold", 13, "bold")),
            (self._status, 252, 40, 242, 20, ("Segoe UI", 9)),
            (self._detail, 252, 62, 242, 20, ("Segoe UI", 8)),
            (self._microphone_level, 244, 136, 162, 18, ("Cascadia Mono", 8)),
            (self._output_status, 244, 160, 162, 18, ("Cascadia Mono", 8)),
        )
        self._semantic_labels = []
        for variable, x, y, width, height, font in semantic_labels:
            label = self._tk.Label(
                frame,
                textvariable=variable,
                anchor="w",
                background="#06160e",
                foreground="#bfe8cf",
                borderwidth=0,
                highlightthickness=0,
                font=font,
            )
            label.place(x=x, y=y, width=width, height=height)
            self._semantic_labels.append(label)
        controls = (
            (IntentKind.START_RECORDING, "START", False),
            (IntentKind.FINISH_RECORDING, "FINISH", False),
            (IntentKind.TOGGLE_OUTPUT_MUTE, self._mute_text, False),
            (IntentKind.OPEN_SETTINGS, "SETTINGS", True),
            (IntentKind.OPEN_VOICE, "VOICE", True),
            (IntentKind.OPEN_REVIEW, "REVIEW", True),
            (IntentKind.OPEN_DIAGNOSTICS, "DIAGNOSTICS", True),
            (IntentKind.QUIT, "QUIT", False),
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
            button.configure(
                background="#06180f",
                foreground="#d5fbe2",
                activebackground="#123824",
                activeforeground="#effff5",
                disabledforeground="#638d72",
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
                padx=5,
                pady=3,
                font=("Segoe UI", 7),
            )
            button.grid(
                row=1,
                column=index,
                sticky="nsew",
                padx=0,
                pady=0,
            )
            self.buttons[kind] = button
        if hasattr(frame, "grid_columnconfigure"):
            for column in range(8):
                frame.grid_columnconfigure(column, weight=1)

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
        self._deck.tick()
        self._schedule_updates()

    def _apply_snapshot(self, snapshot: CompanionSnapshot) -> None:
        self._snapshot = snapshot
        view = to_view_model(snapshot)
        self._cue.set(view.cue)
        self._status.set(view.status)
        self._detail.set(view.detail)
        self._mute_text.set("UNMUTE" if snapshot.output_muted else "MUTE")
        self._microphone_level.set(microphone_label(snapshot))
        self._output_status.set(
            f"OUTPUT · MUTED ({snapshot.output_volume}%)"
            if snapshot.output_muted
            else f"OUTPUT · {snapshot.output_volume}%"
        )
        visual = matrix_visual(snapshot.runtime.phase)
        self._semantic_labels[0].configure(foreground=visual.accent)
        self._semantic_labels[1].configure(foreground="#a7d8bb")
        self._semantic_labels[2].configure(foreground="#70a985")
        self._deck.set_snapshot(snapshot)
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
            self._apply_snapshot(replace(self._snapshot, detail="Action unavailable"))
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
