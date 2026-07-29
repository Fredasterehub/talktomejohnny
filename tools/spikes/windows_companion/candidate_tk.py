"""Candidate A: standard-library Tk shell with explicit Win32 no-activate adapters."""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import sys
import threading
import tkinter as tk

from common import HotkeyWindow, JsonEmitter, monotonic_ns, read_ndjson


GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
GA_ROOT = 2
SW_SHOWNOACTIVATE = 4

CUES = {
    "idle": "(.)",
    "recording": "(~)",
    "transcribing": "(>)",
    "awaiting confirmation": "(?)",
    "delivering": "(>>)",
    "waiting for assistant": "(...)",
    "planning": "(::)",
    "speaking": "()))",
    "paused": "(||)",
    "stopping": "(x)",
    "disconnected": "(//)",
    "recoverable error": "(!)",
}


def apply_no_activate(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    get_style = user32.GetWindowLongPtrW
    set_style = user32.SetWindowLongPtrW
    get_style.restype = ctypes.c_ssize_t
    set_style.restype = ctypes.c_ssize_t
    style = get_style(hwnd, GWL_EXSTYLE)
    style = (style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
    set_style(hwnd, GWL_EXSTYLE, style)
    user32.SetWindowPos(
        hwnd,
        HWND_TOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vk", type=lambda value: int(value, 0), default=0x85)
    args = parser.parse_args()
    emitter = JsonEmitter()
    commands: queue.Queue[dict[str, object]] = queue.Queue()
    hotkey = HotkeyWindow(args.vk, lambda count: emitter.emit("hotkey", sequence=count, received_ns=monotonic_ns()))
    hotkey.start()
    threading.Thread(target=read_ndjson, args=(commands,), daemon=True).start()

    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.geometry("360x104+24+24")
    root.resizable(False, False)
    root.configure(background="#151922")
    state_var = tk.StringVar(value="(.) Idle")
    detail_var = tk.StringVar(value="Ready; global toggle registered")
    tk.Label(
        root,
        textvariable=state_var,
        anchor="w",
        font=("Segoe UI", 16, "bold"),
        foreground="#f3f6ff",
        background="#151922",
        padx=14,
        pady=10,
    ).pack(fill="x")
    tk.Label(
        root,
        textvariable=detail_var,
        anchor="w",
        font=("Segoe UI", 9),
        foreground="#c7cede",
        background="#151922",
        padx=14,
    ).pack(fill="x")
    root.update_idletasks()
    widget_hwnd = int(root.winfo_id())
    ctypes.windll.user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    ctypes.windll.user32.GetAncestor.restype = ctypes.c_void_p
    hwnd = int(ctypes.windll.user32.GetAncestor(widget_hwnd, GA_ROOT) or widget_hwnd)
    apply_no_activate(hwnd)
    ctypes.windll.user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    apply_no_activate(hwnd)
    root.title("TalkToMeJohnny Spike - (.) Idle")
    closing = False
    auxiliary: tk.Toplevel | None = None

    def set_state(state: str) -> tuple[str, str]:
        cue = CUES.get(state, "(?)")
        display = f"{cue} {state}"
        state_var.set(display)
        detail_var.set("State supplied by the versioned core probe")
        root.title(f"TalkToMeJohnny Spike - {display}")
        root.update_idletasks()
        return cue, display

    def show_auxiliary(surface: str) -> None:
        nonlocal auxiliary
        if auxiliary is not None and auxiliary.winfo_exists():
            auxiliary.destroy()
        auxiliary = tk.Toplevel(root)
        auxiliary.withdraw()
        auxiliary.overrideredirect(True)
        auxiliary.geometry("300x80+24+150")
        auxiliary.configure(background="#202635")
        tk.Label(auxiliary, text=f"{surface.replace('_', ' ').title()} ready", foreground="white", background="#202635", font=("Segoe UI", 13)).pack(fill="both", expand=True)
        auxiliary.update_idletasks()
        child = int(auxiliary.winfo_id())
        aux_hwnd = int(ctypes.windll.user32.GetAncestor(child, GA_ROOT) or child)
        apply_no_activate(aux_hwnd)
        ctypes.windll.user32.ShowWindow(aux_hwnd, SW_SHOWNOACTIVATE)
        apply_no_activate(aux_hwnd)
        auxiliary.title(f"TalkToMeJohnny {surface.replace('_', ' ')}")

    def request_close() -> None:
        nonlocal closing
        closing = True

    root.protocol("WM_DELETE_WINDOW", request_close)

    def poll_commands() -> None:
        nonlocal closing
        while True:
            try:
                message = commands.get_nowait()
            except queue.Empty:
                break
            kind = message.get("kind")
            if kind == "state":
                state = str(message.get("state"))
                cue, display = set_state(state)
                emitter.emit(
                    "state_ack",
                    seq=message.get("seq"),
                    state=state,
                    sent_ns=message.get("sent_ns"),
                    applied_ns=monotonic_ns(),
                    display_text=display,
                    cue=cue,
                    accessibility_name=f"TalkToMeJohnny {state}",
                )
            elif kind == "cycle":
                for state in ("recording", "idle", "planning", "speaking"):
                    set_state(state)
                emitter.emit("cycle_ack", seq=message.get("seq"), phases=["start", "stop", "state", "reply"], applied_ns=monotonic_ns())
            elif kind == "auxiliary":
                surface = str(message.get("surface"))
                show_auxiliary(surface)
                emitter.emit("auxiliary_ack", seq=message.get("seq"), surface=surface, opened=True, noactivate=True, applied_ns=monotonic_ns())
            elif kind == "shutdown":
                emitter.emit("shutdown_ack", requested_ns=message.get("sent_ns"), applied_ns=monotonic_ns())
                closing = True
            else:
                emitter.emit("protocol_error", error="unsupported_kind")
        if closing:
            hotkey.close()
            root.destroy()
        else:
            root.after(2, poll_commands)

    emitter.emit(
        "ready",
        candidate="A-tk-win32",
        pid=os.getpid(),
        hwnd=hwnd,
        runtime=f"Python {sys.version.split()[0]}; Tcl/Tk {tk.TclVersion}",
        transport="stdio-ndjson",
        virtual_key=args.vk,
    )
    root.after(2, poll_commands)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
