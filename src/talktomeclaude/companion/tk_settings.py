"""Explicit-focus Tk surfaces for companion settings and review workflows."""

from __future__ import annotations

import importlib
import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from talktomeclaude import config
from talktomeclaude.companion.capture_delivery import TranscriptReview
from talktomeclaude.companion.hotkey import normalize_control_hotkey
from talktomeclaude.companion.settings import (
    AUTO_SUBMIT_WARNING,
    DEFAULT_PREVIEW_TEXT,
    VoiceImportKind,
    VoiceImportRequest,
    VoiceOption,
    VoiceSettingsService,
)
from talktomeclaude.diagnostics import DiagnosticStore


CONTROL_KEYBINDING_WARNING = (
    "Single-key shortcuts work globally and may trigger while typing in any app. "
    "Choose Change shortcut, then press any non-modifier key or key combination. "
    "The new shortcut applies as soon as Settings is saved."
)

_TK_MODIFIER_KEYSYMS = frozenset(
    {
        "alt_l",
        "alt_r",
        "control_l",
        "control_r",
        "shift_l",
        "shift_r",
        "super_l",
        "super_r",
        "win_l",
        "win_r",
    }
)
_TK_KEYSYM_TOKENS = {
    "space": "space",
    "tab": "tab",
    "return": "enter",
    "escape": "escape",
    "backspace": "backspace",
    "delete": "delete",
    "insert": "insert",
    "home": "home",
    "end": "end",
    "prior": "pageup",
    "next": "pagedown",
    "left": "left",
    "right": "right",
    "up": "up",
    "down": "down",
    "question": "?",
    "asciitilde": "~",
    "grave": "`",
    "slash": "/",
    "plus": "plus",
    "exclam": "!",
    "at": "@",
    "numbersign": "#",
    "dollar": "$",
    "percent": "%",
    "asciicircum": "^",
    "ampersand": "&",
    "asterisk": "*",
    "parenleft": "(",
    "parenright": ")",
    "underscore": "_",
    "equal": "=",
    "bracketleft": "[",
    "braceleft": "{",
    "bracketright": "]",
    "braceright": "}",
    "backslash": "\\",
    "bar": "|",
    "semicolon": ";",
    "colon": ":",
    "apostrophe": "'",
    "quotedbl": '"',
    "comma": ",",
    "less": "<",
    "period": ".",
    "greater": ">",
}


def _control_binding_from_tk_event(event: Any) -> str | None:
    """Translate one Tk key event into a validated, portable shortcut string."""

    keysym = str(getattr(event, "keysym", "")).strip().lower()
    if not keysym or keysym in _TK_MODIFIER_KEYSYMS:
        return None
    character = str(getattr(event, "char", ""))
    if character == "+":
        key = "plus"
    elif len(character) == 1 and character.isprintable() and not character.isspace():
        key = character
    elif keysym in _TK_KEYSYM_TOKENS:
        key = _TK_KEYSYM_TOKENS[keysym]
    elif len(keysym) == 1 and keysym.isascii() and keysym.isalnum():
        key = keysym
    elif keysym.startswith("f") and keysym[1:].isdigit():
        key = keysym
    else:
        raise ValueError(f"key {keysym!r} cannot be used as a global shortcut")
    state = int(getattr(event, "state", 0))
    modifiers = []
    if state & 0x0004:
        modifiers.append("ctrl")
    if state & 0x0008:
        modifiers.append("alt")
    if state & 0x0001:
        modifiers.append("shift")
    if state & 0x0040:
        modifiers.append("win")
    return normalize_control_hotkey("+".join((*modifiers, key)))


class DiagnosticReader(Protocol):
    def snapshot(self) -> tuple[dict[str, Any], bool]: ...

    def export(self, destination: str | Path) -> Path: ...


class VoiceSettingsReader(Protocol):
    def list_voices(self) -> tuple[VoiceOption, ...]: ...

    def select_voice(self, name: str) -> VoiceOption: ...

    def preview_voice(self, name: str, text: str = DEFAULT_PREVIEW_TEXT) -> None: ...

    def import_voice(
        self,
        request: VoiceImportRequest,
        *,
        cancelled: object = None,
        on_step: Any = None,
    ) -> object: ...


@dataclass(slots=True)
class TkSurfaceWindow:
    """A focused top-level and its stable integration/test handles."""

    window: Any
    controls: dict[str, Any]
    variables: dict[str, Any]


class _OperationMessageKind(str, Enum):
    PROGRESS = "progress"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class _OperationMessage:
    kind: _OperationMessageKind
    value: object = None


OperationWork = Callable[[threading.Event, Callable[[str], None]], object]


class _TkDaemonOperation:
    """Run one cancellable daemon task and touch Tk only from ``after`` polls."""

    def __init__(
        self,
        scheduler: Any,
        name: str,
        work: OperationWork,
        *,
        on_progress: Callable[[str], None],
        on_success: Callable[[object], None],
        on_error: Callable[[Exception], None],
        on_cancelled: Callable[[], None],
        on_tainted: Callable[[], None],
        on_settled: Callable[[], None],
        poll_milliseconds: int,
        close_timeout_seconds: float,
        monotonic: Callable[[], float],
    ) -> None:
        self._scheduler = scheduler
        self._name = name
        self._work = work
        self._on_progress = on_progress
        self._on_success = on_success
        self._on_error = on_error
        self._on_cancelled = on_cancelled
        self._on_tainted = on_tainted
        self._on_settled = on_settled
        self._poll_milliseconds = poll_milliseconds
        self._close_timeout_seconds = close_timeout_seconds
        self._monotonic = monotonic
        self._messages: queue.SimpleQueue[_OperationMessage] = queue.SimpleQueue()
        self._cancelled = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._close_deadline: float | None = None
        self._settled = False

    @property
    def closing(self) -> bool:
        return self._close_deadline is not None

    def start(self) -> None:
        try:
            self._thread.start()
        except Exception as exc:
            self._finish_error(exc)
            return
        self._schedule_poll(0)

    def cancel_and_close(self) -> None:
        if self._settled:
            return
        self._cancelled.set()
        if self._close_deadline is None:
            self._close_deadline = self._monotonic() + self._close_timeout_seconds

    def stop(self, timeout_seconds: float) -> bool:
        """Cancel and join without touching Tk after its root has closed."""

        self._cancelled.set()
        if self._thread is threading.current_thread():
            return False
        self._thread.join(max(0.0, timeout_seconds))
        return not self._thread.is_alive()

    def _run(self) -> None:
        def progress(message: str) -> None:
            if not self._cancelled.is_set():
                self._messages.put(
                    _OperationMessage(_OperationMessageKind.PROGRESS, message)
                )

        try:
            result = self._work(self._cancelled, progress)
        except Exception as exc:
            kind = (
                _OperationMessageKind.CANCELLED
                if self._cancelled.is_set()
                else _OperationMessageKind.ERROR
            )
            self._messages.put(_OperationMessage(kind, exc))
            return
        kind = (
            _OperationMessageKind.CANCELLED
            if self._cancelled.is_set()
            else _OperationMessageKind.SUCCESS
        )
        self._messages.put(_OperationMessage(kind, result))

    def _schedule_poll(self, milliseconds: int | None = None) -> None:
        delay = self._poll_milliseconds if milliseconds is None else milliseconds
        self._scheduler.after(delay, self._poll)

    def _poll(self) -> None:
        if self._settled:
            return
        terminal: _OperationMessage | None = None
        while True:
            try:
                message = self._messages.get_nowait()
            except queue.Empty:
                break
            if message.kind is _OperationMessageKind.PROGRESS:
                self._on_progress(str(message.value))
            else:
                terminal = message

        if terminal is not None:
            if self.closing or terminal.kind is _OperationMessageKind.CANCELLED:
                self._finish(self._on_cancelled)
            elif terminal.kind is _OperationMessageKind.SUCCESS:
                self._finish(lambda: self._on_success(terminal.value))
            else:
                error = terminal.value
                assert isinstance(error, Exception)
                self._finish(lambda: self._on_error(error))
            return

        if (
            self._close_deadline is not None
            and self._monotonic() >= self._close_deadline
        ):
            self._finish(self._on_tainted)
            return
        self._schedule_poll()

    def _finish(self, callback: Callable[[], None]) -> None:
        self._settled = True
        try:
            callback()
        finally:
            self._on_settled()

    def _finish_error(self, error: Exception) -> None:
        self._finish(lambda: self._on_error(error))


class TkCompanionSurfaces:
    """Direct-user-focus surfaces; background runtime updates never enter here."""

    def __init__(
        self,
        parent: Any,
        voices: VoiceSettingsService | VoiceSettingsReader,
        diagnostics: DiagnosticStore | DiagnosticReader,
        *,
        get_control_binding: Callable[[], str],
        set_control_binding: Callable[[str], object],
        get_auto_submit: Callable[[], bool],
        set_auto_submit: Callable[[bool], object],
        get_output_volume: Callable[[], int],
        set_output_volume: Callable[[int], object],
        get_recording_mode: Callable[[], str],
        set_recording_mode: Callable[[str], object],
        get_assistant_provider: Callable[[], str],
        set_assistant_provider: Callable[[str], object],
        tk_module: Any | None = None,
        file_dialogs: Any | None = None,
        messages: Any | None = None,
        operation_poll_milliseconds: int = 25,
        operation_close_timeout_seconds: float = 0.25,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if operation_poll_milliseconds < 1:
            raise ValueError("operation poll must be positive")
        if (
            not math.isfinite(operation_close_timeout_seconds)
            or operation_close_timeout_seconds <= 0
            or operation_close_timeout_seconds > 10
        ):
            raise ValueError("operation close timeout must be in (0, 10] seconds")
        self._parent = parent
        self._voices = voices
        self._diagnostics = diagnostics
        self._get_control_binding = get_control_binding
        self._set_control_binding = set_control_binding
        self._get_auto_submit = get_auto_submit
        self._set_auto_submit = set_auto_submit
        self._get_output_volume = get_output_volume
        self._set_output_volume = set_output_volume
        self._get_recording_mode = get_recording_mode
        self._set_recording_mode = set_recording_mode
        self._get_assistant_provider = get_assistant_provider
        self._set_assistant_provider = set_assistant_provider
        self._tk = tk_module or importlib.import_module("tkinter")
        self._file_dialogs = file_dialogs or importlib.import_module(
            "tkinter.filedialog"
        )
        self._messages = messages or importlib.import_module("tkinter.messagebox")
        self._operation_poll_milliseconds = operation_poll_milliseconds
        self._operation_close_timeout_seconds = operation_close_timeout_seconds
        self._monotonic = monotonic
        self._operations: set[_TkDaemonOperation] = set()
        self._voice_boundary_tainted = False
        self._voice_surface: TkSurfaceWindow | None = None
        self._voice_options: tuple[VoiceOption, ...] = ()

    def stop(self) -> bool:
        """Bound all active voice operations during application shutdown."""

        deadline = self._monotonic() + self._operation_close_timeout_seconds
        operations = tuple(self._operations)
        for operation in operations:
            operation.cancel_and_close()
        stopped = True
        for operation in operations:
            operation_stopped = operation.stop(
                max(0.0, deadline - self._monotonic())
            )
            stopped = operation_stopped and stopped
            if operation_stopped:
                self._operations.discard(operation)
        if not stopped:
            self._voice_boundary_tainted = True
        return stopped

    def open_settings(self) -> TkSurfaceWindow:
        surface = self._new_surface("TalkToMeJohnny settings", "680x560")
        window = surface.window
        frame = self._tk.Frame(window, padx=12, pady=12)
        frame.grid(row=0, column=0, sticky="nsew")

        auto_submit = self._tk.BooleanVar(value=bool(self._get_auto_submit()))
        current_mode = self._get_recording_mode()
        recording_mode = self._tk.StringVar(
            value=current_mode
            if current_mode in {"push-toggle", "push-to-talk"}
            else "push-toggle"
        )
        control_binding = self._tk.StringVar(value=str(self._get_control_binding()))
        surface.variables["control_binding"] = control_binding
        current_provider = self._get_assistant_provider()
        assistant_provider = self._tk.StringVar(
            value=(
                current_provider
                if current_provider in config.ASSISTANT_PROVIDERS
                else config.DEFAULT_ASSISTANT_PROVIDER
            )
        )
        output_volume = self._tk.IntVar(value=self._get_output_volume())
        output_volume_text = self._tk.StringVar(value=f"{int(output_volume.get())}%")
        surface.variables.update(
            {
                "auto_submit": auto_submit,
                "output_volume": output_volume,
                "output_volume_text": output_volume_text,
                "recording_mode": recording_mode,
                "assistant_provider": assistant_provider,
            }
        )
        auto_toggle = self._tk.Checkbutton(
            frame,
            text="Automatically press Enter after finish-toggle",
            variable=auto_submit,
        )
        auto_toggle.grid(row=0, column=0, columnspan=2, sticky="w")
        warning = self._tk.Label(
            frame,
            text=AUTO_SUBMIT_WARNING,
            justify="left",
            anchor="w",
            wraplength=520,
        )
        warning.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 12))
        self._tk.Label(frame, text="Spoken output", anchor="w").grid(
            row=2, column=0, columnspan=3, sticky="w"
        )
        self._tk.Label(frame, text="Volume", anchor="w").grid(
            row=3, column=0, sticky="w"
        )

        def update_volume_text(value: object) -> None:
            try:
                volume = int(float(value))
            except (TypeError, ValueError):
                volume = int(output_volume.get())
            output_volume_text.set(f"{volume}%")

        output_volume_scale = self._tk.Scale(
            frame,
            from_=0,
            to=100,
            orient="horizontal",
            resolution=1,
            showvalue=False,
            variable=output_volume,
            command=update_volume_text,
            length=320,
        )
        output_volume_scale.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(12, 0))
        volume_value = self._tk.Label(
            frame,
            textvariable=output_volume_text,
            anchor="e",
            width=5,
        )
        volume_value.grid(row=3, column=3, sticky="e", padx=(12, 0))
        volume_hint = self._tk.Label(
            frame,
            text=(
                "Applies to spoken replies and voice previews. "
                "Does not change microphone gain."
            ),
            justify="left",
            anchor="w",
            wraplength=640,
        )
        volume_hint.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(4, 14))
        self._tk.Label(frame, text="Recording control", anchor="w").grid(
            row=5, column=0, columnspan=2, sticky="w"
        )
        self._tk.Label(frame, text="Recording hotkey", anchor="w").grid(
            row=6, column=0, sticky="w"
        )
        control_binding_entry = self._tk.Entry(
            frame,
            textvariable=control_binding,
            width=30,
            state="readonly",
        )
        control_binding_entry.grid(row=6, column=1, sticky="ew", padx=(12, 12))
        capture_state: dict[str, object] = {
            "active": False,
            "binding_id": None,
        }

        def finish_capture() -> None:
            binding_id = capture_state["binding_id"]
            if isinstance(binding_id, str):
                window.unbind("<KeyPress>", binding_id)
            capture_state["active"] = False
            capture_state["binding_id"] = None
            change_binding.configure(text="Change shortcut")

        def capture_key(event: Any) -> str:
            try:
                captured = _control_binding_from_tk_event(event)
            except ValueError as exc:
                self._show_error("Shortcut was not changed", exc, parent=window)
                return "break"
            if captured is None:
                return "break"
            control_binding.set(captured)
            finish_capture()
            return "break"

        def toggle_capture() -> None:
            if bool(capture_state["active"]):
                finish_capture()
                return
            capture_state["active"] = True
            change_binding.configure(text="Press shortcut...")
            capture_state["binding_id"] = window.bind(
                "<KeyPress>",
                capture_key,
                add="+",
            )
            window.focus_force()

        change_binding = self._tk.Button(
            frame,
            text="Change shortcut",
            command=toggle_capture,
        )
        change_binding.grid(row=6, column=2, sticky="w")
        control_binding_warning = self._tk.Label(
            frame,
            text=CONTROL_KEYBINDING_WARNING,
            justify="left",
            anchor="w",
            wraplength=640,
        )
        control_binding_warning.grid(
            row=7,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(7, 16),
        )
        toggle = self._tk.Radiobutton(
            frame,
            text="Push-toggle (press once to start, once to finish)",
            variable=recording_mode,
            value="push-toggle",
        )
        toggle.grid(row=8, column=0, columnspan=3, sticky="w")
        hold = self._tk.Radiobutton(
            frame,
            text="Hold to talk (push-to-talk)",
            variable=recording_mode,
            value="push-to-talk",
        )
        hold.grid(row=9, column=0, columnspan=3, sticky="w")
        self._tk.Label(frame, text="Assistant provider", anchor="w").grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(16, 0)
        )
        both = self._tk.Radiobutton(
            frame,
            text="Claude Code and Codex CLI (recommended)",
            variable=assistant_provider,
            value="both",
        )
        both.grid(row=11, column=0, columnspan=3, sticky="w")
        claude = self._tk.Radiobutton(
            frame,
            text="Claude Code only",
            variable=assistant_provider,
            value="claude",
        )
        claude.grid(row=12, column=0, columnspan=3, sticky="w")
        codex = self._tk.Radiobutton(
            frame,
            text="Codex CLI only",
            variable=assistant_provider,
            value="codex",
        )
        codex.grid(row=13, column=0, columnspan=3, sticky="w")
        restart = self._tk.Label(
            frame,
            text=(
                "Restart required after changing assistant provider. "
                "The current companion session will keep its existing connection."
            ),
            justify="left",
            anchor="w",
            wraplength=640,
        )
        restart.grid(row=14, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        def close() -> None:
            finish_capture()
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", close)

        def save() -> None:
            finish_capture()
            try:
                self._set_control_binding(str(control_binding.get()))
                self._set_output_volume(int(output_volume.get()))
                self._set_recording_mode(str(recording_mode.get()))
                self._set_auto_submit(bool(auto_submit.get()))
                self._set_assistant_provider(str(assistant_provider.get()))
            except Exception as exc:
                self._show_error("Settings were not saved", exc, parent=window)
                return
            window.destroy()

        save_button = self._tk.Button(frame, text="Save", command=save)
        save_button.grid(row=15, column=0, sticky="e", pady=(20, 0))
        cancel = self._tk.Button(frame, text="Cancel", command=close)
        cancel.grid(row=15, column=1, sticky="w", pady=(20, 0))
        surface.controls.update(
            {
                "auto_submit": auto_toggle,
                "warning": warning,
                "output_volume": output_volume_scale,
                "output_volume_value": volume_value,
                "output_volume_help": volume_hint,
                "control_binding": control_binding_entry,
                "change_control_binding": change_binding,
                "control_binding_warning": control_binding_warning,
                "push_toggle": toggle,
                "push_to_talk": hold,
                "claude_provider": claude,
                "codex_provider": codex,
                "provider_restart": restart,
                "save": save_button,
                "cancel": cancel,
            }
        )
        return surface

    def open_voice(self) -> TkSurfaceWindow:
        surface = self._new_surface("Voice settings", "720x420")
        self._voice_surface = surface
        frame = self._tk.Frame(surface.window, padx=12, pady=12)
        frame.grid(row=0, column=0, sticky="nsew")
        self._tk.Label(
            frame,
            text="Voices (status is always written in text; color is not required)",
            anchor="w",
        ).grid(row=0, column=0, columnspan=4, sticky="ew")
        voice_list = self._tk.Listbox(frame, width=92, height=12, exportselection=False)
        voice_list.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(6, 8))
        surface.controls["voice_list"] = voice_list
        status = self._tk.StringVar(value="Ready")
        surface.variables["status"] = status
        status_label = self._tk.Label(frame, textvariable=status, anchor="w")
        status_label.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        operation_ref: list[_TkDaemonOperation | None] = [None]

        def selected_name() -> str | None:
            selection = voice_list.curselection()
            if not selection:
                self._messages.showerror(
                    "Choose a voice", "Select a voice first.", parent=surface.window
                )
                return None
            return self._voice_options[int(selection[0])].name

        def select() -> None:
            name = selected_name()
            if name is None:
                return
            try:
                self._voices.select_voice(name)
                self._refresh_voice_list(surface)
            except Exception as exc:
                self._show_error("Voice was not selected", exc, parent=surface.window)

        def preview() -> None:
            name = selected_name()
            if (
                name is None
                or operation_ref[0] is not None
                or self._voice_boundary_tainted
            ):
                return

            status.set(f"Previewing {name}…")
            for control in (select_button, preview_button, import_button):
                control.configure(state=self._tk.DISABLED)

            def work(
                cancelled: threading.Event, _progress: Callable[[str], None]
            ) -> None:
                if not cancelled.is_set():
                    self._voices.preview_voice(name)

            operation: _TkDaemonOperation

            def settled() -> None:
                self._operations.discard(operation)
                operation_ref[0] = None
                if not operation.closing:
                    for control in (select_button, preview_button, import_button):
                        control.configure(state=self._tk.NORMAL)

            operation = _TkDaemonOperation(
                surface.window,
                "ttc-tk-voice-preview",
                work,
                on_progress=status.set,
                on_success=lambda _result: status.set("Preview complete"),
                on_error=lambda exc: self._operation_error(
                    status, "Voice preview failed", exc, surface.window
                ),
                on_cancelled=surface.window.destroy,
                on_tainted=lambda: self._operation_tainted(
                    "Voice preview", surface.window
                ),
                on_settled=settled,
                poll_milliseconds=self._operation_poll_milliseconds,
                close_timeout_seconds=self._operation_close_timeout_seconds,
                monotonic=self._monotonic,
            )
            operation_ref[0] = operation
            self._operations.add(operation)
            operation.start()

        def close_voice() -> None:
            operation = operation_ref[0]
            if operation is None:
                surface.window.destroy()
                return
            status.set("Cancelling preview…")
            operation.cancel_and_close()

        def open_import() -> None:
            if operation_ref[0] is None and not self._voice_boundary_tainted:
                self.open_voice_import(on_complete=self._refresh_open_voice)

        select_button = self._tk.Button(frame, text="Select", command=select)
        select_button.grid(row=2, column=0, sticky="ew")
        preview_button = self._tk.Button(frame, text="Preview", command=preview)
        preview_button.grid(row=2, column=1, sticky="ew")
        import_button = self._tk.Button(
            frame,
            text="Import…",
            command=open_import,
        )
        import_button.grid(row=2, column=2, sticky="ew")
        close = self._tk.Button(frame, text="Close", command=close_voice)
        close.grid(row=2, column=3, sticky="ew")
        surface.window.protocol("WM_DELETE_WINDOW", close_voice)
        surface.controls.update(
            {
                "select": select_button,
                "preview": preview_button,
                "import": import_button,
                "close": close,
                "status": status_label,
            }
        )
        if self._voice_boundary_tainted:
            status.set("Voice operations are tainted; restart the companion")
            for control in (select_button, preview_button, import_button):
                control.configure(state=self._tk.DISABLED)
        self._refresh_voice_list(surface)
        return surface

    def open_voice_import(
        self, *, on_complete: Callable[[], object] | None = None
    ) -> TkSurfaceWindow:
        surface = self._new_surface("Import a voice", "620x390")
        frame = self._tk.Frame(surface.window, padx=12, pady=12)
        frame.grid(row=0, column=0, sticky="nsew")
        name = self._tk.StringVar(value="")
        kind = self._tk.StringVar(value=VoiceImportKind.CLONE.value)
        source = self._tk.StringVar(value="")
        voice_config = self._tk.StringVar(value="")
        preview = self._tk.BooleanVar(value=True)
        select = self._tk.BooleanVar(value=True)
        surface.variables.update(
            {
                "name": name,
                "kind": kind,
                "source": source,
                "config": voice_config,
                "preview": preview,
                "select": select,
            }
        )
        self._tk.Label(frame, text="Name", anchor="w").grid(row=0, column=0, sticky="w")
        name_entry = self._tk.Entry(frame, textvariable=name, width=48)
        name_entry.grid(row=0, column=1, columnspan=2, sticky="ew")
        clone = self._tk.Radiobutton(
            frame, text="Cloned voice", variable=kind, value=VoiceImportKind.CLONE.value
        )
        clone.grid(row=1, column=1, sticky="w")
        piper = self._tk.Radiobutton(
            frame, text="Piper model", variable=kind, value=VoiceImportKind.PIPER.value
        )
        piper.grid(row=1, column=2, sticky="w")
        self._tk.Label(frame, text="Audio/model", anchor="w").grid(
            row=2, column=0, sticky="w"
        )
        source_entry = self._tk.Entry(frame, textvariable=source, width=48)
        source_entry.grid(row=2, column=1, sticky="ew")

        def browse_source() -> None:
            chosen = self._file_dialogs.askopenfilename(parent=surface.window)
            if chosen:
                source.set(chosen)

        browse = self._tk.Button(frame, text="Browse…", command=browse_source)
        browse.grid(row=2, column=2, sticky="ew")
        self._tk.Label(frame, text="Piper config (optional)", anchor="w").grid(
            row=3, column=0, sticky="w"
        )
        config_entry = self._tk.Entry(frame, textvariable=voice_config, width=48)
        config_entry.grid(row=3, column=1, sticky="ew")

        def browse_config() -> None:
            chosen = self._file_dialogs.askopenfilename(parent=surface.window)
            if chosen:
                voice_config.set(chosen)

        config_browse = self._tk.Button(frame, text="Browse…", command=browse_config)
        config_browse.grid(row=3, column=2, sticky="ew")
        preview_toggle = self._tk.Checkbutton(
            frame, text="Preview immediately", variable=preview
        )
        preview_toggle.grid(row=4, column=1, sticky="w")
        select_toggle = self._tk.Checkbutton(
            frame, text="Select after import", variable=select
        )
        select_toggle.grid(row=5, column=1, sticky="w")
        status = self._tk.StringVar(value="Ready")
        surface.variables["status"] = status
        status_label = self._tk.Label(frame, textvariable=status, anchor="w")
        status_label.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        operation_ref: list[_TkDaemonOperation | None] = [None]
        completed = [False]

        def cancel_flow() -> None:
            operation = operation_ref[0]
            if operation is None:
                surface.window.destroy()
                return
            status.set("Cancelling import…")
            operation.cancel_and_close()

        def submit() -> None:
            if operation_ref[0] is not None or self._voice_boundary_tainted:
                return
            try:
                raw_config = str(voice_config.get()).strip()
                request = VoiceImportRequest(
                    kind=VoiceImportKind(str(kind.get())),
                    name=str(name.get()),
                    source_path=str(source.get()),
                    config_path=raw_config or None,
                    preview=bool(preview.get()),
                    select=bool(select.get()),
                    preview_text=DEFAULT_PREVIEW_TEXT,
                )
            except Exception as exc:
                self._show_error("Voice import failed", exc, parent=surface.window)
                return

            import_button.configure(state=self._tk.DISABLED)
            status.set("Starting import…")

            def work(
                cancelled: threading.Event, progress: Callable[[str], None]
            ) -> object:
                return self._voices.import_voice(
                    request,
                    cancelled=cancelled.is_set,
                    on_step=lambda step: progress(f"{step.value.capitalize()}…"),
                )

            operation: _TkDaemonOperation

            def succeeded(_result: object) -> None:
                status.set("Import complete")
                try:
                    if on_complete is not None:
                        on_complete()
                except Exception as exc:
                    self._operation_error(
                        status, "Voice list refresh failed", exc, surface.window
                    )
                    return
                completed[0] = True
                surface.window.destroy()

            def settled() -> None:
                self._operations.discard(operation)
                operation_ref[0] = None
                if not operation.closing and not completed[0]:
                    import_button.configure(state=self._tk.NORMAL)

            operation = _TkDaemonOperation(
                surface.window,
                "ttc-tk-voice-import",
                work,
                on_progress=status.set,
                on_success=succeeded,
                on_error=lambda exc: self._operation_error(
                    status, "Voice import failed", exc, surface.window
                ),
                on_cancelled=surface.window.destroy,
                on_tainted=lambda: self._operation_tainted(
                    "Voice import", surface.window
                ),
                on_settled=settled,
                poll_milliseconds=self._operation_poll_milliseconds,
                close_timeout_seconds=self._operation_close_timeout_seconds,
                monotonic=self._monotonic,
            )
            operation_ref[0] = operation
            self._operations.add(operation)
            operation.start()

        import_button = self._tk.Button(frame, text="Import", command=submit)
        import_button.grid(row=7, column=1, sticky="e", pady=(12, 0))
        cancel_button = self._tk.Button(frame, text="Cancel", command=cancel_flow)
        cancel_button.grid(row=7, column=2, sticky="w", pady=(12, 0))
        surface.window.protocol("WM_DELETE_WINDOW", cancel_flow)
        surface.controls.update(
            {
                "name": name_entry,
                "clone": clone,
                "piper": piper,
                "source": source_entry,
                "browse_source": browse,
                "config": config_entry,
                "browse_config": config_browse,
                "preview": preview_toggle,
                "select": select_toggle,
                "status": status_label,
                "submit": import_button,
                "cancel": cancel_button,
            }
        )
        if self._voice_boundary_tainted:
            status.set("Voice operations are tainted; restart the companion")
            import_button.configure(state=self._tk.DISABLED)
        return surface

    def open_review(
        self,
        review: TranscriptReview | str,
        *,
        on_confirm: Callable[[str], object],
        on_cancel: Callable[[], object],
    ) -> TkSurfaceWindow:
        surface = self._new_surface("Review transcript", "720x430")
        frame = self._tk.Frame(surface.window, padx=12, pady=12)
        frame.grid(row=0, column=0, sticky="nsew")
        text = review.text if isinstance(review, TranscriptReview) else review
        detail = "Edit the transcript before delivery."
        if isinstance(review, TranscriptReview) and review.reason:
            detail = f"Review required: {review.reason}"
        self._tk.Label(frame, text=detail, anchor="w").grid(
            row=0, column=0, columnspan=2, sticky="ew"
        )
        editor = self._tk.Text(frame, width=82, height=16, wrap="word")
        editor.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 8))
        editor.insert("1.0", text)

        def confirm() -> None:
            edited = str(editor.get("1.0", "end-1c"))
            try:
                on_confirm(edited)
            except Exception as exc:
                self._show_error(
                    "Transcript was not delivered", exc, parent=surface.window
                )
                return
            surface.window.destroy()

        def cancel() -> None:
            on_cancel()
            surface.window.destroy()

        confirm_button = self._tk.Button(
            frame, text="Confirm and send", command=confirm
        )
        confirm_button.grid(row=2, column=0, sticky="e")
        cancel_button = self._tk.Button(frame, text="Cancel", command=cancel)
        cancel_button.grid(row=2, column=1, sticky="w")
        surface.controls.update(
            {"editor": editor, "confirm": confirm_button, "cancel": cancel_button}
        )
        return surface

    def open_diagnostics(self) -> TkSurfaceWindow:
        surface = self._new_surface("Diagnostics", "680x420")
        frame = self._tk.Frame(surface.window, padx=12, pady=12)
        frame.grid(row=0, column=0, sticky="nsew")
        state, recovered = self._diagnostics.snapshot()
        events = state.get("events", [])
        summary = f"{len(events)} content-safe events"
        if recovered:
            summary += "; damaged storage was recovered"
        self._tk.Label(frame, text=summary, anchor="w").grid(
            row=0, column=0, columnspan=2, sticky="ew"
        )
        event_list = self._tk.Text(frame, width=76, height=15, wrap="none")
        event_list.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 8))
        lines = [
            f"#{event.get('sequence', '?')}  {event.get('kind', 'unknown')}"
            for event in events
            if isinstance(event, dict)
        ]
        event_list.insert("1.0", "\n".join(lines) or "No diagnostic events recorded.")
        event_list.configure(state=self._tk.DISABLED)

        def export() -> None:
            destination = self._file_dialogs.asksaveasfilename(
                parent=surface.window,
                title="Export diagnostics",
                defaultextension=".json",
                filetypes=(("JSON", "*.json"),),
            )
            if not destination:
                return
            try:
                output = self._diagnostics.export(destination)
            except Exception as exc:
                self._show_error(
                    "Diagnostics export failed", exc, parent=surface.window
                )
                return
            self._messages.showinfo(
                "Diagnostics exported", f"Saved to {output}", parent=surface.window
            )

        export_button = self._tk.Button(frame, text="Export…", command=export)
        export_button.grid(row=2, column=0, sticky="e")
        close = self._tk.Button(frame, text="Close", command=surface.window.destroy)
        close.grid(row=2, column=1, sticky="w")
        surface.controls.update(
            {"events": event_list, "export": export_button, "close": close}
        )
        return surface

    def _new_surface(self, title: str, geometry: str) -> TkSurfaceWindow:
        window = self._tk.Toplevel(self._parent)
        window.title(title)
        window.geometry(geometry)
        window.transient(self._parent)
        window.lift()
        window.focus_force()
        return TkSurfaceWindow(window, {}, {})

    def _refresh_open_voice(self) -> None:
        if self._voice_surface is not None:
            self._refresh_voice_list(self._voice_surface)

    def _refresh_voice_list(self, surface: TkSurfaceWindow) -> None:
        options = self._voices.list_voices()
        self._voice_options = options
        voice_list = surface.controls["voice_list"]
        voice_list.delete(0, self._tk.END)
        selected_index: int | None = None
        for index, option in enumerate(options):
            voice_list.insert(self._tk.END, self._voice_row(option))
            if option.selected:
                selected_index = index
        if selected_index is not None:
            voice_list.selection_set(selected_index)

    @staticmethod
    def _voice_row(option: VoiceOption) -> str:
        marker = "SELECTED; " if option.selected else ""
        status = option.availability.value.upper()
        if option.fault is not None:
            status = f"FAULT [{option.fault.code.value}]: {option.fault.message}"
        return f"{marker}{option.name} — {option.engine} — {status}"

    def _show_error(self, title: str, exc: Exception, *, parent: Any) -> None:
        self._messages.showerror(title, str(exc), parent=parent)

    def _operation_error(
        self, status: Any, title: str, exc: Exception, parent: Any
    ) -> None:
        status.set("Operation failed")
        self._show_error(title, exc, parent=parent)

    def _operation_tainted(self, label: str, parent: Any) -> None:
        self._voice_boundary_tainted = True
        self._messages.showerror(
            f"{label} did not stop",
            "Cancellation exceeded its bounded deadline. The daemon result is ignored; "
            "restart the companion before another voice change.",
            parent=parent,
        )
        parent.destroy()


__all__ = ["TkCompanionSurfaces", "TkSurfaceWindow"]
