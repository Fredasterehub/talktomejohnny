from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from talktomeclaude.companion.settings import (
    AUTO_SUBMIT_WARNING,
    VoiceAvailability,
    VoiceFault,
    VoiceFaultCode,
    VoiceFlowStep,
    VoiceImportRequest,
    VoiceOption,
    VoiceSource,
)
from talktomeclaude.companion.tk_settings import TkCompanionSurfaces
from talktomeclaude.diagnostics import DiagnosticStore


class _Variable:
    def __init__(self, value: object = "") -> None:
        self.value = value
        self.set_threads: list[int] = []

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.set_threads.append(threading.get_ident())
        self.value = value


class _Widget:
    def __init__(self, _parent: object = None, **options: Any) -> None:
        self.options = dict(options)
        self.grid_options: dict[str, object] = {}
        self.destroyed = False

    def grid(self, **options: object) -> None:
        self.grid_options = dict(options)

    def configure(self, **options: object) -> None:
        self.options.update(options)

    def destroy(self) -> None:
        self.destroyed = True

    def invoke(self) -> None:
        if self.options.get("state") == "disabled":
            return
        command = self.options.get("command")
        if callable(command):
            command()


class _Window(_Widget):
    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)
        self.calls: list[tuple[str, object]] = []
        self.protocols: dict[str, object] = {}
        self.after_callbacks: list[object] = []

    def title(self, value: str) -> None:
        self.calls.append(("title", value))

    def geometry(self, value: str) -> None:
        self.calls.append(("geometry", value))

    def transient(self, parent: object) -> None:
        self.calls.append(("transient", parent))

    def lift(self) -> None:
        self.calls.append(("lift", None))

    def focus_force(self) -> None:
        self.calls.append(("focus", None))

    def protocol(self, name: str, callback: object) -> None:
        self.protocols[name] = callback

    def after(self, _milliseconds: int, callback: object) -> str:
        self.after_callbacks.append(callback)
        return f"after-{len(self.after_callbacks)}"

    def run_pending(self) -> None:
        callbacks = tuple(self.after_callbacks)
        self.after_callbacks.clear()
        for callback in callbacks:
            if callable(callback):
                callback()


class _Listbox(_Widget):
    def __init__(self, parent: object = None, **options: Any) -> None:
        super().__init__(parent, **options)
        self.items: list[str] = []
        self.selected: tuple[int, ...] = ()

    def delete(self, _start: object, _end: object) -> None:
        self.items.clear()
        self.selected = ()

    def insert(self, _where: object, value: str) -> None:
        self.items.append(value)

    def curselection(self) -> tuple[int, ...]:
        return self.selected

    def selection_set(self, index: int) -> None:
        self.selected = (index,)


class _Text(_Widget):
    def __init__(self, parent: object = None, **options: Any) -> None:
        super().__init__(parent, **options)
        self.content = ""

    def insert(self, _where: object, value: str) -> None:
        self.content += value

    def get(self, _start: object, _end: object) -> str:
        return self.content


class _Tk:
    END = "end"
    NORMAL = "normal"
    DISABLED = "disabled"
    Frame = _Widget
    Label = _Widget
    Button = _Widget
    Checkbutton = _Widget
    Radiobutton = _Widget
    Entry = _Widget
    Listbox = _Listbox
    Text = _Text
    StringVar = _Variable
    BooleanVar = _Variable

    def __init__(self) -> None:
        self.windows: list[_Window] = []

    def Toplevel(self, parent: object) -> _Window:
        window = _Window(parent)
        self.windows.append(window)
        return window

    def run_pending(self) -> None:
        for window in tuple(self.windows):
            window.run_pending()


class _Dialogs:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.open_path = ""

    def askopenfilename(self, **_options: object) -> str:
        return self.open_path

    def asksaveasfilename(self, **_options: object) -> str:
        return str(self.destination)


class _Messages:
    def __init__(self) -> None:
        self.errors: list[tuple[str, str]] = []
        self.infos: list[tuple[str, str]] = []

    def showerror(self, title: str, message: str, **_options: object) -> None:
        self.errors.append((title, message))

    def showinfo(self, title: str, message: str, **_options: object) -> None:
        self.infos.append((title, message))


class _VoiceService:
    def __init__(self) -> None:
        self.options = [
            VoiceOption(
                "rick",
                "clone",
                VoiceSource.CLONED,
                True,
                VoiceAvailability.AVAILABLE,
            ),
            VoiceOption(
                "broken",
                "clone",
                VoiceSource.CLONED,
                False,
                VoiceAvailability.FAULT,
                VoiceFault(
                    VoiceFaultCode.MISSING_ASSET,
                    "The cloned voice reference audio is missing.",
                ),
            ),
        ]
        self.selected: list[str] = []
        self.previewed: list[str] = []
        self.imported: list[VoiceImportRequest] = []
        self.preview_threads: list[int] = []
        self.import_threads: list[int] = []
        self.preview_daemons: list[bool] = []
        self.import_daemons: list[bool] = []
        self.preview_started = threading.Event()
        self.preview_release: threading.Event | None = None
        self.import_started = threading.Event()
        self.import_release: threading.Event | None = None
        self.import_cancel_seen = threading.Event()
        self.import_finished = threading.Event()
        self.ignore_import_cancel = False

    def list_voices(self) -> tuple[VoiceOption, ...]:
        return tuple(self.options)

    def select_voice(self, name: str) -> VoiceOption:
        self.selected.append(name)
        self.options = [
            VoiceOption(
                option.name,
                option.engine,
                option.source,
                option.name == name,
                option.availability,
                option.fault,
            )
            for option in self.options
        ]
        return next(option for option in self.options if option.name == name)

    def preview_voice(self, name: str, _text: str = "") -> None:
        self.preview_threads.append(threading.get_ident())
        self.preview_daemons.append(threading.current_thread().daemon)
        self.preview_started.set()
        if self.preview_release is not None:
            self.preview_release.wait(1)
        self.previewed.append(name)

    def import_voice(
        self,
        request: VoiceImportRequest,
        *,
        cancelled: object = None,
        on_step: Any = None,
    ) -> object:
        self.import_threads.append(threading.get_ident())
        self.import_daemons.append(threading.current_thread().daemon)
        self.import_started.set()
        cancel_check = cancelled if callable(cancelled) else lambda: False
        try:
            while self.import_release is not None and not self.import_release.is_set():
                if cancel_check() and not self.ignore_import_cancel:
                    self.import_cancel_seen.set()
                    raise RuntimeError("cancelled")
                time.sleep(0.001)
            self.imported.append(request)
            if on_step is not None:
                for step in VoiceFlowStep:
                    on_step(step)
            self.options.append(
                VoiceOption(
                    request.name,
                    request.kind.value,
                    VoiceSource.CLONED,
                    request.select,
                    VoiceAvailability.AVAILABLE,
                )
            )
            return object()
        finally:
            self.import_finished.set()


def _surfaces(
    tmp_path: Path,
    *,
    operation_close_timeout_seconds: float = 0.25,
) -> tuple[
    TkCompanionSurfaces,
    _Tk,
    _VoiceService,
    _Dialogs,
    _Messages,
    dict[str, object],
]:
    tk = _Tk()
    voices = _VoiceService()
    dialogs = _Dialogs(tmp_path / "diagnostics-export.json")
    messages = _Messages()
    settings: dict[str, object] = {
        "auto_submit": True,
        "recording_mode": "push-toggle",
        "assistant_provider": "claude",
    }
    surface = TkCompanionSurfaces(
        object(),
        voices,
        DiagnosticStore(tmp_path / "diagnostics.json"),
        get_auto_submit=lambda: bool(settings["auto_submit"]),
        set_auto_submit=lambda value: settings.__setitem__("auto_submit", value),
        get_recording_mode=lambda: str(settings["recording_mode"]),
        set_recording_mode=lambda value: settings.__setitem__("recording_mode", value),
        get_assistant_provider=lambda: str(settings["assistant_provider"]),
        set_assistant_provider=lambda value: settings.__setitem__(
            "assistant_provider", value
        ),
        tk_module=tk,
        file_dialogs=dialogs,
        messages=messages,
        operation_close_timeout_seconds=operation_close_timeout_seconds,
    )
    return surface, tk, voices, dialogs, messages, settings


def _drain_until(
    tk: _Tk, predicate: Callable[[], bool], *, timeout_seconds: float = 1.0
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        tk.run_pending()
        if predicate():
            return
        time.sleep(0.002)
    tk.run_pending()
    assert predicate()


def test_settings_surface_focuses_only_when_opened_and_shows_exact_warning(
    tmp_path: Path,
) -> None:
    surfaces, tk, _voices, _dialogs, _messages, settings = _surfaces(tmp_path)
    assert tk.windows == []

    surface = surfaces.open_settings()

    assert ("focus", None) in surface.window.calls
    assert surface.controls["warning"].options["text"] == AUTO_SUBMIT_WARNING
    assert "Hold to talk" in surface.controls["push_to_talk"].options["text"]
    assert "Restart required" in surface.controls["provider_restart"].options["text"]
    surface.variables["auto_submit"].set(False)
    surface.variables["recording_mode"].set("push-to-talk")
    surface.variables["assistant_provider"].set("codex")
    surface.controls["save"].invoke()
    assert settings == {
        "auto_submit": False,
        "recording_mode": "push-to-talk",
        "assistant_provider": "codex",
    }
    assert surface.window.destroyed is True


def test_settings_surface_defaults_unknown_provider_to_both(tmp_path: Path) -> None:
    surfaces, _tk, _voices, _dialogs, _messages, settings = _surfaces(tmp_path)
    settings["assistant_provider"] = "unknown"

    surface = surfaces.open_settings()

    assert surface.variables["assistant_provider"].get() == "both"


def test_voice_surface_uses_non_color_status_and_service_actions(
    tmp_path: Path,
) -> None:
    surfaces, _tk, voices, _dialogs, _messages, _settings = _surfaces(tmp_path)
    surface = surfaces.open_voice()
    voice_list = surface.controls["voice_list"]

    assert "AVAILABLE" in voice_list.items[0]
    assert "SELECTED" in voice_list.items[0]
    assert "FAULT [missing-asset]" in voice_list.items[1]
    assert "reference audio is missing" in voice_list.items[1]
    assert "foreground" not in voice_list.options

    voices.preview_release = threading.Event()
    main_thread = threading.get_ident()
    voice_list.selection_set(1)
    surface.controls["preview"].invoke()
    assert voices.preview_started.wait(0.5)
    assert surface.variables["status"].get() == "Previewing broken…"
    assert surface.controls["select"].options["state"] == "disabled"
    assert len(voices.preview_threads) == 1
    assert voices.preview_threads[0] != main_thread
    assert voices.preview_daemons == [True]
    voices.preview_release.set()
    _drain_until(_tk, lambda: surface.variables["status"].get() == "Preview complete")
    surface.controls["select"].invoke()
    assert voices.previewed == ["broken"]
    assert voices.selected == ["broken"]


def test_guided_import_builds_request_and_refreshes_voice_list(tmp_path: Path) -> None:
    surfaces, tk, voices, dialogs, _messages, _settings = _surfaces(tmp_path)
    voice_surface = surfaces.open_voice()
    flow = surfaces.open_voice_import(on_complete=surfaces._refresh_open_voice)
    dialogs.open_path = str(tmp_path / "rick.wav")
    flow.controls["browse_source"].invoke()
    flow.variables["name"].set("narrator")
    flow.variables["preview"].set(False)
    flow.controls["submit"].invoke()
    assert voices.import_started.wait(0.5)
    assert voices.import_threads[0] != threading.get_ident()
    assert voices.import_daemons == [True]
    _drain_until(tk, lambda: flow.window.destroyed)

    assert len(voices.imported) == 1
    request = voices.imported[0]
    assert request.name == "narrator"
    assert request.source_path == str(tmp_path / "rick.wav")
    assert request.preview is False
    assert request.select is True
    assert flow.window.destroyed is True
    assert set(flow.variables["status"].set_threads) == {threading.get_ident()}
    assert any("narrator" in row for row in voice_surface.controls["voice_list"].items)


def test_import_cancel_uses_thread_safe_probe_and_closes_after_ack(
    tmp_path: Path,
) -> None:
    surfaces, tk, voices, _dialogs, messages, _settings = _surfaces(tmp_path)
    voices.import_release = threading.Event()
    flow = surfaces.open_voice_import()
    flow.variables["name"].set("temporary")
    flow.variables["source"].set(str(tmp_path / "source.wav"))

    flow.controls["submit"].invoke()
    assert voices.import_started.wait(0.5)
    flow.controls["cancel"].invoke()
    assert voices.import_cancel_seen.wait(0.5)
    _drain_until(tk, lambda: flow.window.destroyed)

    assert messages.errors == []
    assert voices.imported == []


def test_noncooperative_import_close_is_bounded_and_reports_taint(
    tmp_path: Path,
) -> None:
    surfaces, tk, voices, _dialogs, messages, _settings = _surfaces(
        tmp_path, operation_close_timeout_seconds=0.01
    )
    voices.import_release = threading.Event()
    voices.ignore_import_cancel = True
    flow = surfaces.open_voice_import()
    flow.variables["name"].set("temporary")
    flow.variables["source"].set(str(tmp_path / "source.wav"))

    flow.controls["submit"].invoke()
    assert voices.import_started.wait(0.5)
    flow.controls["cancel"].invoke()
    time.sleep(0.02)
    _drain_until(tk, lambda: flow.window.destroyed)

    assert messages.errors
    assert messages.errors[-1][0] == "Voice import did not stop"
    assert "bounded deadline" in messages.errors[-1][1]
    voices.import_release.set()
    assert voices.import_finished.wait(0.5)
    replacement = surfaces.open_voice()
    assert replacement.controls["preview"].options["state"] == "disabled"
    assert "tainted" in str(replacement.variables["status"].get())


def test_review_editor_confirms_edited_text_or_cancels(tmp_path: Path) -> None:
    surfaces, _tk, _voices, _dialogs, _messages, _settings = _surfaces(tmp_path)
    confirmed: list[str] = []
    cancelled: list[bool] = []
    surface = surfaces.open_review(
        "original transcript",
        on_confirm=confirmed.append,
        on_cancel=lambda: cancelled.append(True),
    )
    editor = surface.controls["editor"]
    editor.content = "corrected transcript"
    surface.controls["confirm"].invoke()
    assert confirmed == ["corrected transcript"]
    assert surface.window.destroyed is True

    cancelled_surface = surfaces.open_review(
        "private transcript",
        on_confirm=confirmed.append,
        on_cancel=lambda: cancelled.append(True),
    )
    cancelled_surface.controls["cancel"].invoke()
    assert cancelled == [True]
    assert cancelled_surface.window.destroyed is True


def test_diagnostics_surface_is_content_free_and_exports(tmp_path: Path) -> None:
    surfaces, _tk, _voices, dialogs, messages, _settings = _surfaces(tmp_path)
    store = surfaces._diagnostics
    assert isinstance(store, DiagnosticStore)
    secret = "PRIVATE TRANSCRIPT CONTENT"
    store.record("capture_result", transcript=secret, status="review-required")

    surface = surfaces.open_diagnostics()
    rendered = surface.controls["events"].content
    assert "capture_result" in rendered
    assert secret not in rendered
    assert surface.controls["events"].options["state"] == "disabled"

    surface.controls["export"].invoke()
    document = json.loads(dialogs.destination.read_text(encoding="utf-8"))
    assert secret not in repr(document)
    assert messages.infos and messages.errors == []


def load_tests(
    _loader: unittest.TestLoader,
    _tests: unittest.TestSuite,
    _pattern: str | None,
) -> unittest.TestSuite:
    """Expose the function-style Tk corpus to stdlib unittest discovery."""

    suite = unittest.TestSuite()
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue

        def run(test=function) -> None:
            if test.__code__.co_argcount == 0:
                test()
                return
            with tempfile.TemporaryDirectory() as temporary:
                test(Path(temporary))

        suite.addTest(unittest.FunctionTestCase(run, description=name))
    return suite
