"""Candidate C: explicit headless recovery controller."""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading

from common import HotkeyWindow, JsonEmitter, monotonic_ns, read_ndjson


CUES = {
    "idle": "[.]",
    "recording": "[~]",
    "transcribing": "[>]",
    "awaiting confirmation": "[?]",
    "delivering": "[>>]",
    "waiting for assistant": "[...]",
    "planning": "[::]",
    "speaking": "[))]",
    "paused": "[||]",
    "stopping": "[x]",
    "disconnected": "[//]",
    "recoverable error": "[!]",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vk", type=lambda value: int(value, 0), default=0x87)
    args = parser.parse_args()
    emitter = JsonEmitter()
    commands: queue.Queue[dict[str, object]] = queue.Queue()
    hotkey = HotkeyWindow(args.vk, lambda count: emitter.emit("hotkey", sequence=count, received_ns=monotonic_ns()))
    hotkey.start()
    threading.Thread(target=read_ndjson, args=(commands,), daemon=True).start()
    emitter.emit(
        "ready",
        candidate="C-headless",
        pid=os.getpid(),
        hwnd=0,
        runtime=sys.version.split()[0],
        transport="stdio-ndjson",
        virtual_key=args.vk,
    )
    while True:
        message = commands.get()
        kind = message.get("kind")
        if kind == "state":
            state = str(message.get("state"))
            cue = CUES.get(state, "[?]")
            emitter.emit(
                "state_ack",
                seq=message.get("seq"),
                state=state,
                sent_ns=message.get("sent_ns"),
                applied_ns=monotonic_ns(),
                display_text=f"{cue} {state}",
                cue=cue,
                accessibility_name=f"TalkToMeJohnny {state}",
            )
        elif kind == "cycle":
            emitter.emit("cycle_ack", seq=message.get("seq"), phases=["start", "stop", "state", "reply"], applied_ns=monotonic_ns())
        elif kind == "auxiliary":
            emitter.emit("auxiliary_ack", seq=message.get("seq"), surface=message.get("surface"), opened=False, noactivate=True, recovery_diagnostic=True, applied_ns=monotonic_ns())
        elif kind == "shutdown":
            emitter.emit("shutdown_ack", requested_ns=message.get("sent_ns"), applied_ns=monotonic_ns())
            break
        else:
            emitter.emit("protocol_error", error="unsupported_kind")
    hotkey.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
