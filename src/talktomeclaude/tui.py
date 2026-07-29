"""Textual dashboard for the local TalkToMeJohnny voice loop,
rendered in the terminal (near-black ground, amber accent, cream ink)."""

from __future__ import annotations

import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from pathlib import PurePosixPath
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Footer, Input, OptionList, RichLog, Sparkline, Static
from textual.worker import Worker, WorkerState

from talktomeclaude import config
from talktomeclaude.listen import (
    _remote_shell_command,
    _ssh_base,
    run_listen,
)

# Kept at module scope so `mock.patch.object(tui, "run_listen"/"subprocess"/...)`
# in the tests still resolves to the objects the code actually calls.
__all__ = [
    "TUIError",
    "QueueKeys",
    "TalkToMeApp",
    "run_dashboard",
    "discover_remote_projects",
    "remote_directory_exists",
    "RichLog",
]

_MODE_LABELS = {
    "always-on": "Always on",
    "push-to-talk": "Push to talk",
    "push-toggle": "Space toggle",
}
_PHASE_LABELS = {
    "ready": "READY",
    "starting": "STARTING",
    "recording": "RECORDING",
    "transcribing": "TRANSCRIBING",
    "thinking": "ASSISTANT WORKING",
    "speaking": "SPEAKING",
    "error": "NEEDS ATTENTION",
}
_PHASE_NOTICES = {
    "ready": "Press Space to record; press Space again to send",
    "starting": "Loading the local speech model",
    "recording": "Listening to your microphone",
    "transcribing": "Turning speech into text locally",
    "thinking": "The assistant is working in the selected project",
    "speaking": "Playing the assistant reply",
}

# ── brand ────────────────────────────────────────────────────────────────────
_INK = "#171310"
_CREAM = "#ede6d6"
_AMBER = "#e6b22e"
_AMBER_HOT = "#f2d06b"
_OCHRE = "#8a5e0f"
_GRAY = "#b9b09c"

_TTMJ_VARS = {
    "ttmj-ochre": _OCHRE,
    "ttmj-gray": _GRAY,
    "ttmj-gray-dim": "#8f846b",
    "ttmj-border": "#42382a",
    "vu-idle": _OCHRE,
    "vu-hot": _AMBER_HOT,
    "phase-recording": "#c14a2b",
    "phase-transcribing": "#c8901c",
    "phase-thinking": _AMBER,
    "phase-speaking": "#8fae5a",
    "phase-error": "#c14a2b",
}

TTMJ_THEME = Theme(
    name="ttmj",
    primary=_AMBER,
    secondary=_OCHRE,
    accent=_AMBER,
    foreground=_CREAM,
    background=_INK,
    surface="#211b15",
    panel="#2a2119",
    success="#8fae5a",
    warning=_AMBER,
    error="#c14a2b",
    dark=True,
    luminosity_spread=0.12,
    variables=dict(_TTMJ_VARS),
)

# ── the wordmark — a voice waveform, then the name (two-tone, poster identity) ─
_WAVE = "▁▂▃▅▇▅▃▂▁"


def _header_full() -> Text:
    text = Text(no_wrap=True)
    text.append(_WAVE, style=f"bold {_AMBER}")
    text.append("   ")
    text.append("TALK TO ME, ", style=f"bold {_CREAM}")
    text.append("JOHNNY", style=f"bold {_AMBER}")
    text.append("\n")
    text.append("local voice companion", style=_GRAY)
    text.append("   ·   it's a long road", style=f"italic {_OCHRE}")
    return text


def _header_compact() -> Text:
    text = Text(no_wrap=True)
    text.append("▂▄▆ ", style=f"bold {_AMBER}")
    text.append("TALK TO ME, ", style=f"bold {_CREAM}")
    text.append("JOHNNY", style=f"bold {_AMBER}")
    text.append("  ·  local voice companion", style=_GRAY)
    return text


# ── inline poster stylesheet (theme variables only → NO_COLOR degrades free) ──
_TTMJ_CSS = """
Screen { background: $background; color: $foreground; layout: vertical; }

#header { height: auto; padding: 1 2 0 2; }
#band { height: auto; }

/* Status strip: a phase dot + label, then quiet dim metadata — no colour blocks. */
#statusbar { height: 1; margin-top: 1; }
#phase { width: auto; text-style: bold; color: $ttmj-gray; margin: 0 3 0 0; }
#phase.-ready { color: $ttmj-gray; }
#phase.-starting { color: $ttmj-ochre; }
#phase.-recording { color: $phase-recording; }
#phase.-transcribing { color: $phase-transcribing; }
#phase.-thinking { color: $phase-thinking; }
#phase.-speaking { color: $phase-speaking; }
#phase.-error { color: $phase-error; }

.chip { width: auto; color: $ttmj-gray-dim; margin: 0 3 0 0; }

/* Ambient VU: a thin borderless meter, muted at rest, warming to ochre on sound. */
#vu { width: 1fr; height: 1; margin: 1 2 0 2; }
#vu > .sparkline--max-color { color: $ttmj-ochre; }
#vu > .sparkline--min-color { color: $ttmj-border; }

#status { height: 1; padding: 0 2; margin-top: 1; color: $ttmj-gray-dim; text-style: italic; }
#status.-recording { color: $phase-recording; text-style: bold; }
#status.-transcribing { color: $phase-transcribing; text-style: none; }
#status.-thinking { color: $phase-thinking; text-style: none; }
#status.-speaking { color: $phase-speaking; text-style: none; }
#status.-error { color: $phase-error; text-style: bold; }

#body { height: 1fr; padding: 1 2; }
.panel {
    background: $background; border: round $ttmj-border;
    border-title-color: $ttmj-gray-dim; border-title-align: left;
    padding: 0 1;
    scrollbar-size: 1 1;
    scrollbar-background: $background;
    scrollbar-background-hover: $background;
    scrollbar-background-active: $background;
    scrollbar-color: $ttmj-border;
    scrollbar-color-hover: $ttmj-ochre;
    scrollbar-color-active: $accent;
}
.panel:focus-within { border: round $accent; border-title-color: $accent; }
#dialogue { width: 1fr; margin-right: 1; overflow-x: hidden; }
#session { width: 1fr; overflow-x: hidden; }

/* In compact the band renders a single wordmark line (HeaderBand.compact); it
   stays visible so the brand identity survives on small terminals. The body
   also stacks so both panels keep a usable width. */
.-compact #body { layout: vertical; }
.-compact #dialogue, .-compact #session { width: 1fr; height: 1fr; margin-right: 0; }

ModalScreen { align: center middle; }
.modal {
    width: 70%; max-width: 80; height: auto; max-height: 80%;
    background: $surface; border: round $ttmj-border; padding: 1 2;
}
.modal-help { color: $foreground; padding: 0 1; height: auto; }
.modal-keys { color: $ttmj-gray-dim; padding: 1 1 0 1; height: auto; }
#picker { height: auto; max-height: 16; background: $surface; }
#prompt-input { margin: 1 0; }

Footer { background: $panel; color: $ttmj-gray-dim; }
Footer > .footer-key--key { color: $accent; text-style: bold; }
Footer > .footer-key--description { color: $ttmj-gray; }
"""


class TUIError(RuntimeError):
    """Raised when the dashboard cannot start or complete an action."""


class QueueKeys:
    """A key source that duck-types :class:`listen._RawKeys` but is fed from
    Textual's own event loop instead of raw stdin, so Textual keeps sole
    ownership of the terminal. :meth:`stop` unblocks a waiting reader by raising
    ``KeyboardInterrupt`` — the signal ``run_listen`` already treats as a clean
    shutdown."""

    _STOP = object()

    def __init__(self) -> None:
        self._queue: "queue.Queue[object]" = queue.Queue()

    def push(self, char: str) -> None:
        self._queue.put(char)

    def stop(self) -> None:
        self._queue.put(self._STOP)

    def _unwrap(self, item: object) -> str:
        if item is self._STOP:
            self._queue.put(self._STOP)  # keep other blocked readers unblocked
            raise KeyboardInterrupt
        return item  # type: ignore[return-value]

    def read_key(self, timeout: float | None) -> str | None:
        try:
            if timeout is None:
                item = self._queue.get()
            elif timeout <= 0:
                item = self._queue.get_nowait()
            else:
                item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        return self._unwrap(item)

    def drain(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is self._STOP:
                self._queue.put(self._STOP)
                raise KeyboardInterrupt

    def is_pressed(self, key: str) -> bool | None:
        # No key-up events from Textual: None routes push-to-talk through
        # listen.py's repeat-gap fallback.
        return None

    def __enter__(self) -> "QueueKeys":
        return self

    def __exit__(self, *_exc_info) -> bool:
        return False


def discover_remote_projects(
    remote: str, root: str = "/srv/projects"
) -> list[str]:
    inner = (
        f"find -- {shlex.quote(root)} -mindepth 1 -maxdepth 1 -type d "
        "-not -name '.*' -print"
    )
    command = _ssh_base(remote) + [_remote_shell_command(inner)]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"SSH exited {result.returncode}"
        raise TUIError(f"Could not list {root}: {detail}")
    return sorted(
        {line.strip() for line in result.stdout.splitlines() if line.strip()},
        key=str.casefold,
    )


def remote_directory_exists(remote: str, path: str) -> bool:
    # `test` has no `--` end-of-options; the quoted absolute path is the operand.
    inner = f"test -d {shlex.quote(path)}"
    command = _ssh_base(remote) + [_remote_shell_command(inner)]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).returncode == 0


class HeaderBand(Static):
    """The poster wordmark; swaps to a single line on small terminals."""

    compact: reactive[bool] = reactive(False)

    def render(self) -> Text:
        return _header_compact() if self.compact else _header_full()

    def watch_compact(self, _compact: bool) -> None:
        self.refresh(layout=True)


class TextPromptScreen(ModalScreen[str]):
    """A single bordered field — SSH target or a remote project path."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, label: str, initial: str = "") -> None:
        super().__init__()
        self._label = label
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box", classes="panel modal"):
            yield Static(self._label, classes="modal-help")
            yield Input(value=self._initial, id="prompt-input")
            yield Static("Enter Save   ·   Esc Cancel", classes="modal-keys")

    def on_mount(self) -> None:
        self.query_one("#prompt-box").border_title = self._label
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class PickProject(ModalScreen[str]):
    """Choose the remote working directory for Claude Code."""

    BINDINGS = [
        Binding("escape", "cancel", "Back", show=False),
        Binding("e", "enter_path", "Enter path", show=False),
    ]

    def __init__(self, projects: list[str], current: str | None) -> None:
        super().__init__()
        self._projects = projects
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box", classes="panel modal"):
            yield Static("Choose where remote Claude Code should work", classes="modal-help")
            yield OptionList(*self._projects, id="picker")
            yield Static(
                "Up/Down Move   ·   Enter Select   ·   E Enter path   ·   Esc Back",
                classes="modal-keys",
            )

    def on_mount(self) -> None:
        self.query_one("#picker-box").border_title = "PROJECTS"
        picker = self.query_one("#picker", OptionList)
        if self._current in self._projects:
            picker.highlighted = self._projects.index(self._current)
        picker.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._projects[event.option_index])

    def action_enter_path(self) -> None:
        def _entered(path: str | None) -> None:
            if path:
                self.dismiss(path)

        self.app.push_screen(
            TextPromptScreen(
                "Remote project path", self._current or "/srv/projects/"
            ),
            _entered,
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


# Thread → UI messages. Defined at MODULE scope so their handler names are the
# short on_echo / on_status / on_phase / on_level / on_progress (a message nested
# in the App would namespace to on_talk_to_me_app_echo and never be dispatched).
class Echo(Message):
    def __init__(self, line: str) -> None:
        self.line = line
        super().__init__()


class Status(Message):
    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class Phase(Message):
    def __init__(self, phase: str) -> None:
        self.phase = phase
        super().__init__()


class Level(Message):
    def __init__(self, value: float) -> None:
        self.value = value
        super().__init__()


class Progress(Message):
    pass


class Mirror(Message):
    def __init__(self, event: dict) -> None:
        self.event = event
        super().__init__()


class SessionChanged(Message):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__()


# ── live session-mirror formatting (stream-json events → styled lines) ────────
_MIRROR_STYLES = {
    "mark": _OCHRE,
    "tool": f"bold {_AMBER}",
    "thinking": _GRAY,
    "text": _CREAM,
    "out": "#8f8269",
}


def _truncate(value: object, limit: int = 110) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _brief_input(inp: object) -> str:
    if isinstance(inp, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
            if inp.get(key):
                return _truncate(inp[key])
        if inp:
            return _truncate(", ".join(f"{k}={v}" for k, v in inp.items()))
    return ""


def _brief_result(content: object) -> str:
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        content = " ".join(parts)
    return _truncate(content)


def _content_blocks(event: dict) -> list[dict]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _mirror_lines(event: dict) -> list[tuple[str, str]]:
    """Turn one claude stream-json event into styled mirror lines. Unknown or
    malformed events yield nothing (tolerated, not fatal); every shape is
    coerced defensively. Tool I/O is truncated so a huge file dump can't flood
    the panel."""
    if not isinstance(event, dict):
        return []
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        model = event.get("model") or "claude"
        tools = event.get("tools")
        count = len(tools) if isinstance(tools, list) else 0
        return [("mark", f"● session · {model} · {count} tools")]
    if etype == "assistant":
        lines: list[tuple[str, str]] = []
        for block in _content_blocks(event):
            btype = block.get("type")
            if btype == "tool_use":
                lines.append(("tool", f"▸ {block.get('name') or 'tool'}  {_brief_input(block.get('input'))}"))
            elif btype == "thinking":
                lines.append(("thinking", "· thinking…"))
            elif btype == "text":
                text = block.get("text")
                text = text.strip() if isinstance(text, str) else ""
                if text:
                    lines.append(("text", _truncate(text, 200)))
        return lines
    if etype == "user":
        return [
            ("out", "  ↳ " + _brief_result(block.get("content")))
            for block in _content_blocks(event)
            if block.get("type") == "tool_result"
        ]
    if etype == "result":
        marker = "error" if event.get("is_error") else "done"
        duration = event.get("duration_ms")
        millis = int(duration) if isinstance(duration, (int, float)) else 0
        return [("mark", f"● {marker} · {millis} ms")]
    return []


class TalkToMeApp(App[None]):
    """The interactive launcher and live voice-session dashboard."""

    CSS = _TTMJ_CSS
    TITLE = "TalkToMeJohnny"

    BINDINGS = [
        Binding("space", "talk", "Talk"),
        Binding("escape", "stop", "Stop", show=False),
        Binding("p", "pick_project", "Project"),
        Binding("m", "mode", "Mode"),
        Binding("v", "voice", "Voice"),
        Binding("w", "wake", "Wake"),
        Binding("c", "clone", "Clone"),
        Binding("r", "remote", "Remote"),
        Binding("q", "quit", "Quit"),
    ]

    phase: reactive[str] = reactive("ready")
    level: reactive[float] = reactive(0.0)
    notice: reactive[str] = reactive("Press Space to start a voice session")
    tier: reactive[str] = reactive("")
    mode: reactive[str] = reactive("always-on")
    voice_enabled: reactive[bool] = reactive(True)
    wake_enabled: reactive[bool] = reactive(False)
    # Availability stays separate from opt-in: no model means manual capture;
    # a failed detector or unreadable setting means wake is unavailable.
    wake_ready: reactive[bool] = reactive(True)
    wake_unavailable: reactive[bool] = reactive(False)
    remote: reactive[str | None] = reactive(None)
    remote_cwd: reactive[str | None] = reactive(None)

    _ui_ready = False

    def __init__(self, speak: Callable[[str], None]) -> None:
        super().__init__()
        self._speak_cb = speak
        self._voice_running = False
        self._keys: QueueKeys | None = None
        self._stop_event: threading.Event | None = None
        self._worker: Worker | None = None
        self._levels = [0.0] * 48
        self._compact: bool | None = None
        self._thinking_started = 0.0
        self.current_session_id: str | None = None
        # Sticky: a running session that fell back to manual wake can't recover
        # for its lifetime, so re-enabling wake must not reclaim readiness.
        self._wake_degraded_session = False

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return {**super().get_theme_variable_defaults(), **_TTMJ_VARS}

    def compose(self) -> ComposeResult:
        with Container(id="header"):
            yield HeaderBand(id="band")
            with Horizontal(id="statusbar"):
                yield Static("● READY", id="phase")
                yield Static("", id="mode-chip", classes="chip")
                yield Static("", id="voice-chip", classes="chip")
                yield Static("", id="wake-chip", classes="chip")
                yield Static("", id="source-chip", classes="chip")
                yield Static("", id="tier-chip", classes="chip")
        yield Sparkline(list(self._levels), summary_function=max, id="vu")
        yield Static("", id="status", classes="status")
        with Horizontal(id="body"):
            yield RichLog(id="dialogue", classes="panel", markup=False, wrap=True,
                          min_width=20, max_lines=500, auto_scroll=True)
            yield RichLog(id="session", classes="panel", markup=False, wrap=True,
                          min_width=20, max_lines=1000, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(TTMJ_THEME)
        self.theme = "ttmj"
        if (
            os.environ.get("TALKTOMEJOHNNY_REDUCED_MOTION") == "1"
            or os.environ.get("TALKTOMECLAUDE_REDUCED_MOTION") == "1"
        ):
            self.animation_level = "none"
        self.mode = config.recording_mode()
        self.voice_enabled = config.voice_assist_enabled()
        self.wake_enabled, self.wake_unavailable = config.wake_word_state()
        self.wake_ready = (
            not self.wake_unavailable and config.wake_model_path() is not None
        )
        self.remote = config.remote()
        self.remote_cwd = config.remote_cwd()
        self.query_one("#dialogue").border_title = "DIALOGUE"
        session = self.query_one("#session", RichLog)
        session.border_title = "SESSION"
        session.write(Text("Assistant activity — tools, edits, thinking — appears here.",
                           style=_GRAY))
        self._apply_compact(self.size.width, self.size.height)
        self._ui_ready = True
        self._paint_all()

    # ── painting ────────────────────────────────────────────────────────────
    def _paint_all(self) -> None:
        self._paint_phase(self.phase)
        self._paint_chips()
        self._paint_status()
        self._paint_tier()

    def _paint_phase(self, phase: str) -> None:
        label = _PHASE_LABELS.get(phase, phase.upper())
        pill = self.query_one("#phase", Static)
        status = self.query_one("#status", Static)
        pill.update(f"● {label}")
        for known in _PHASE_LABELS:
            pill.remove_class(f"-{known}")
            status.remove_class(f"-{known}")
        pill.add_class(f"-{phase}")
        status.add_class(f"-{phase}")

    def _paint_chips(self) -> None:
        self.query_one("#mode-chip", Static).update(
            _MODE_LABELS.get(self.mode, self.mode).upper()
        )
        self.query_one("#voice-chip", Static).update(
            "VOICE ON" if self.voice_enabled else "VOICE MUTED"
        )
        src = "LOCAL" if self.remote is None else self.remote.upper()
        self.query_one("#source-chip", Static).update(src)
        if not self.wake_enabled:
            wake_label = "WAKE OFF"
        elif self.wake_unavailable:
            wake_label = "WAKE UNAVAILABLE"
        elif self.wake_ready:
            wake_label = "WAKE ON"
        else:
            wake_label = "WAKE MANUAL"
        self.query_one("#wake-chip", Static).update(wake_label)

    def _paint_status(self) -> None:
        self.query_one("#status", Static).update(self.notice)

    def _paint_tier(self) -> None:
        self.query_one("#tier-chip", Static).update(
            f"STT {self.tier}".upper() if self.tier else ""
        )

    def _source(self) -> str:
        if self.remote is None:
            return "Local Claude Code"
        return f"{self.remote}:{self.remote_cwd or '~'}"

    # ── reactive watchers ─────────────────────────────────────────────────────
    def watch_phase(self, phase: str) -> None:
        if not self._ui_ready:
            return
        self._paint_phase(phase)
        if phase == "thinking":
            self._thinking_started = time.monotonic()
        if phase in _PHASE_NOTICES:
            self.notice = _PHASE_NOTICES[phase]

    def watch_level(self, value: float) -> None:
        if not self._ui_ready:
            return
        self._levels = [*self._levels[-47:], max(0.0, min(1.0, value * 16.0))]
        self.query_one("#vu", Sparkline).data = self._levels

    def watch_notice(self, _notice: str) -> None:
        if self._ui_ready:
            self._paint_status()

    def watch_tier(self, _tier: str) -> None:
        if self._ui_ready:
            self._paint_tier()

    def watch_mode(self, _mode: str) -> None:
        if self._ui_ready:
            self._paint_chips()

    def watch_voice_enabled(self, _enabled: bool) -> None:
        if self._ui_ready:
            self._paint_chips()

    def watch_wake_enabled(self, _enabled: bool) -> None:
        if self._ui_ready:
            self._paint_chips()

    def watch_wake_ready(self, _ready: bool) -> None:
        if self._ui_ready:
            self._paint_chips()

    def watch_wake_unavailable(self, _unavailable: bool) -> None:
        if self._ui_ready:
            self._paint_chips()

    def watch_remote(self, _remote: str | None) -> None:
        if self._ui_ready:
            self._paint_chips()

    def watch_remote_cwd(self, _cwd: str | None) -> None:
        if self._ui_ready:
            self._paint_chips()

    # ── layout ────────────────────────────────────────────────────────────────
    def on_resize(self, event) -> None:
        self._apply_compact(event.size.width, event.size.height)

    def _apply_compact(self, width: int, height: int) -> None:
        compact = width < 72 or height < 20
        if compact == self._compact:
            return
        self._compact = compact
        self.screen.set_class(compact, "-compact")
        self.query_one("#band", HeaderBand).compact = compact

    # ── voice session ─────────────────────────────────────────────────────────
    def action_talk(self) -> None:
        if self._voice_running:
            return
        self._voice_running = True
        self._wake_degraded_session = False
        self._keys = QueueKeys()
        self._stop_event = threading.Event()
        self.notice = "Starting a voice session…"
        self._worker = self._voice_worker()

    def action_stop(self) -> None:
        if self._voice_running:
            self._request_stop()
            self.notice = "Stopping the voice session…"

    def _request_stop(self) -> None:
        # Cooperative stop only. We deliberately do NOT call worker.cancel():
        # cancel flips Textual's async wrapper to CANCELLED immediately while the
        # OS thread stays blocked in run_listen, which would reset the UI (and
        # allow a second overlapping session) before the thread has really left.
        # stop_event + keys.stop() make the thread unwind on its own, after which
        # the worker reaches SUCCESS and on_worker_state_changed resets cleanly.
        if self._stop_event is not None:
            self._stop_event.set()
        if self._keys is not None:
            self._keys.stop()

    def on_unmount(self) -> None:
        # Never leave the blocking run_listen thread orphaned at exit; the
        # cooperative signals let it unwind so the executor join does not hang.
        self._request_stop()

    def _speak(self, text: str) -> None:
        if self.voice_enabled:
            self._speak_cb(text)

    def _voice_worker(self) -> Worker:
        return self.run_worker(
            self._run_voice,
            group="voice",
            name="voice",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _run_voice(self) -> None:
        try:
            run_listen(
                mode=self.mode,
                session_id=None,
                tmux_pane=None,
                device=config.stt_device(),
                model=None,
                once=False,
                echo=lambda line: self.post_message(Echo(line)),
                speak=self._speak,
                status=lambda text: self.post_message(Status(text)),
                remote=self.remote,
                remote_cwd=self.remote_cwd,
                on_level=lambda value: self.post_message(Level(value)),
                on_phase=lambda phase: self.post_message(Phase(phase)),
                on_progress=lambda: self.post_message(Progress()),
                on_event=lambda event: self.post_message(Mirror(event)),
                on_session=lambda session_id: self.post_message(SessionChanged(session_id)),
                trigger_key=" ",
                start_recording=self.mode == "push-toggle",
                keys=self._keys,
                stop_event=self._stop_event,
                permission=config.claude_permissions(),
            )
        except KeyboardInterrupt:
            return

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "voice":
            return
        if event.state == WorkerState.ERROR:
            self._reset_voice()
            self.phase = "error"
            self.notice = str(event.worker.error) or "The voice session failed"
        elif event.state in (WorkerState.SUCCESS, WorkerState.CANCELLED):
            self._reset_voice()
            self.phase = "ready"
            self.notice = "Voice session ended — press Space to start again"

    def _reset_voice(self) -> None:
        self._voice_running = False
        self._keys = None
        self._stop_event = None
        self._worker = None

    # ── key routing: feed the recorder while a session runs ──────────────────
    def on_key(self, event) -> None:
        if not self._voice_running:
            return
        # Escape stops; W stays live so wake gating can be toggled mid-session
        # (the listen loop reads the setting before each hands-free capture).
        if event.key in ("escape", "ctrl+c", "w"):
            return
        if self._keys is not None:
            self._keys.push(event.character or event.key)
        event.prevent_default()
        event.stop()

    # ── message handlers (UI thread) ─────────────────────────────────────────
    def on_echo(self, message: Echo) -> None:
        log = self.query_one("#dialogue", RichLog)
        line = message.line
        if line.startswith("you:"):
            log.write(
                Text.assemble(("you", f"bold {_AMBER}"), "   ",
                              (line[4:].strip(), _CREAM))
            )
        elif line.startswith("claude:"):
            log.write(
                Text.assemble(("claude", f"bold {_CREAM}"), "   ",
                              (line[7:].strip(), _GRAY))
            )

    def on_status(self, message: Status) -> None:
        text = message.text
        lowered = text.lower()
        if text.startswith("stt tier:"):
            self.tier = text.removeprefix("stt tier:").strip()
        elif (
            "degraded" in lowered
            or text.startswith("error:")
            or lowered.startswith("wake word manual fallback")
            or lowered.startswith("wake word unavailable")
        ):
            if lowered.startswith("wake word manual fallback"):
                self.wake_ready = False
                self.wake_unavailable = False
                self._wake_degraded_session = True
            elif "wake word" in lowered:
                self.wake_ready = False
                self.wake_unavailable = True
                self._wake_degraded_session = True
            self.notice = text.removeprefix("error:").strip()

    def on_phase(self, message: Phase) -> None:
        self.phase = message.phase

    def on_level(self, message: Level) -> None:
        self.level = message.value

    def on_progress(self, message: Progress) -> None:
        elapsed = max(0, int(time.monotonic() - self._thinking_started))
        self.notice = f"Assistant is working ({elapsed}s)"

    def on_mirror(self, message: Mirror) -> None:
        log = self.query_one("#session", RichLog)
        for style_key, text in _mirror_lines(message.event):
            log.write(Text(text, style=_MIRROR_STYLES.get(style_key, _CREAM)))

    def on_session_changed(self, message: SessionChanged) -> None:
        # The live session id every voice-fired command dispatches into.
        self.current_session_id = message.session_id
        self.query_one("#session").border_title = f"SESSION {message.session_id[:8]}"

    # ── actions: settings & navigation ───────────────────────────────────────
    def action_mode(self) -> None:
        if self._voice_running:
            return
        modes = list(config.RECORDING_MODES)
        self.mode = modes[(modes.index(self.mode) + 1) % len(modes)]
        config.set_recording_mode(self.mode)
        self.notice = f"Recording mode: {_MODE_LABELS[self.mode]}"

    def action_voice(self) -> None:
        self.voice_enabled = not self.voice_enabled
        config.set_voice_assist(self.voice_enabled)
        self.notice = "Spoken replies enabled" if self.voice_enabled else "Spoken replies muted"

    def action_wake(self) -> None:
        self.wake_enabled = not self.wake_enabled
        config.set_wake_word(self.wake_enabled)
        self.wake_unavailable = False
        if not self.wake_enabled:
            self.wake_ready = config.wake_model_path() is not None
            self.notice = "Wake word off — hands-free capture starts immediately"
            return
        if self._voice_running and self._wake_degraded_session:
            # The live listen loop already fell back to manual for this session
            # and cannot recover until it restarts — do not claim readiness now.
            self.wake_ready = False
            self.notice = "Wake word stays manual until the session restarts"
            return
        self.wake_ready = config.wake_model_path() is not None
        if self.wake_ready:
            self.notice = "Wake word on — hands-free capture waits for the wake phrase"
        else:
            self.notice = (
                "Wake word has no detector model — manual push-to-talk required "
                "(config set wake-model /path/to/model.onnx)"
            )

    def action_clone(self) -> None:
        if self._voice_running:
            return
        from talktomeclaude.clone_ui import CloneScreen

        def _done(created: bool | None) -> None:
            self.notice = (
                "Voice created — select it with `config set default-voice NAME`"
                if created
                else "Voice cloning cancelled"
            )

        self.push_screen(CloneScreen(), _done)

    def action_remote(self) -> None:
        if self._voice_running:
            return

        def _saved(target: str | None) -> None:
            if target is None:
                return
            self.remote = target or None
            config.set_remote(target or None)
            self.notice = "Remote target saved" if target else "Using local Claude Code"

        self.push_screen(TextPromptScreen("SSH target (user@host)", self.remote or ""), _saved)

    def action_pick_project(self) -> None:
        if self._voice_running:
            return
        if self.remote is None:
            self.notice = "Set an SSH target with R before choosing a remote project"
            return
        self.notice = "Loading remote project directories…"
        self._discover_projects()

    def _discover_projects(self) -> Worker:
        return self.run_worker(
            self._run_discovery, group="discover", thread=True, exclusive=True,
            exit_on_error=False,
        )

    def _run_discovery(self) -> None:
        root = (
            str(PurePosixPath(self.remote_cwd).parent)
            if self.remote_cwd
            else "/srv/projects"
        )
        try:
            projects = discover_remote_projects(self.remote, root)
        except TUIError as exc:
            self.post_message(Status(f"error: {exc}"))
            return
        self.call_from_thread(self._open_picker, projects)

    def _open_picker(self, projects: list[str]) -> None:
        def _chosen(path: str | None) -> None:
            if path is not None:
                self._apply_project(path)

        self.push_screen(PickProject(projects, self.remote_cwd), _chosen)

    def _apply_project(self, path: str) -> None:
        # Validate over SSH on a worker thread, never on the UI thread.
        self.notice = f"Checking {path}…"
        self.run_worker(
            lambda: self._validate_project(path), group="discover",
            thread=True, exclusive=True, exit_on_error=False,
        )

    def _validate_project(self, path: str) -> None:
        self.call_from_thread(
            self._project_checked, path, remote_directory_exists(self.remote, path)
        )

    def _project_checked(self, path: str, exists: bool) -> None:
        if not exists:
            self.phase = "error"
            self.notice = f"Remote directory does not exist: {path}"
            return
        self.remote_cwd = path
        config.set_remote_cwd(path)
        self.notice = f"Project selected: {path}"


def run_dashboard(speak: Callable[[str], None]) -> None:
    """Run the interactive launcher and live voice-session dashboard."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise TUIError("the dashboard needs an interactive terminal")
    TalkToMeApp(speak).run()
