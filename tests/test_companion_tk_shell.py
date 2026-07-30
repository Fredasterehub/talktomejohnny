from __future__ import annotations

import unittest
from typing import Any

from talktomeclaude.companion.contracts import (
    CompanionIntent,
    CompanionSnapshot,
    IntentKind,
)
from talktomeclaude.companion.tk_shell import (
    TkCompanionShell,
    WindowsNonActivatingPolicy,
)
from talktomeclaude.core import RuntimePhase, RuntimeState


class _Variable:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class _Widget:
    def __init__(self, _parent: object = None, **options: Any) -> None:
        self.options = options
        self.grid_options: dict[str, object] = {}
        self.place_options: dict[str, object] = {}

    def grid(self, **options: object) -> None:
        self.grid_options = dict(options)

    def configure(self, **options: object) -> None:
        self.options.update(options)

    def place(self, **options: object) -> None:
        self.place_options = dict(options)

    def invoke(self) -> None:
        command = self.options.get("command")
        if callable(command):
            command()

    def grid_columnconfigure(self, _column: int, **_options: object) -> None:
        return None


class _Canvas(_Widget):
    def __init__(self, _parent: object = None, **options: Any) -> None:
        super().__init__(_parent, **options)
        self.items: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def delete(self, _target: object) -> None:
        self.items.clear()

    def _create(self, kind: str, *args: object, **options: object) -> int:
        self.items.append((kind, args, dict(options)))
        return len(self.items)

    def create_rectangle(self, *args: object, **options: object) -> int:
        return self._create("rectangle", *args, **options)

    def create_polygon(self, *args: object, **options: object) -> int:
        return self._create("polygon", *args, **options)

    def create_text(self, *args: object, **options: object) -> int:
        return self._create("text", *args, **options)

    def create_line(self, *args: object, **options: object) -> int:
        return self._create("line", *args, **options)


class _Root(_Widget):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, object]] = []
        self.protocols: dict[str, object] = {}
        self.after_callbacks: dict[str, object] = {}
        self.destroyed = False
        self.focus_calls = 0

    def withdraw(self) -> None:
        self.calls.append(("withdraw", None))

    def title(self, value: str) -> None:
        self.calls.append(("title", value))

    def geometry(self, value: str) -> None:
        self.calls.append(("geometry", value))

    def resizable(self, width: bool, height: bool) -> None:
        self.calls.append(("resizable", (width, height)))

    def protocol(self, name: str, callback: object) -> None:
        self.protocols[name] = callback

    def update_idletasks(self) -> None:
        self.calls.append(("update", None))

    def winfo_id(self) -> int:
        return 41

    def after(self, _milliseconds: int, callback: object) -> str:
        handle = f"after-{len(self.after_callbacks) + 1}"
        self.after_callbacks[handle] = callback
        return handle

    def after_cancel(self, handle: object) -> None:
        self.calls.append(("after_cancel", handle))
        self.after_callbacks.pop(str(handle), None)

    def destroy(self) -> None:
        self.destroyed = True

    def mainloop(self) -> None:
        self.calls.append(("mainloop", None))

    def focus_force(self) -> None:
        self.focus_calls += 1

    def lift(self) -> None:
        self.focus_calls += 1


class _Tk:
    NORMAL = "normal"
    DISABLED = "disabled"
    Frame = _Widget
    Label = _Widget
    Button = _Widget
    Canvas = _Canvas
    StringVar = _Variable

    def __init__(self) -> None:
        self.root = _Root()

    def Tk(self) -> _Root:
        return self.root


class _Policy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def apply(self, widget_handle: int) -> int:
        self.calls.append(("apply", widget_handle))
        return 99

    def show_without_activation(self, window_handle: int) -> None:
        self.calls.append(("show", window_handle))

    def release(self, window_handle: int) -> None:
        self.calls.append(("release", window_handle))


class TkCompanionShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tk = _Tk()
        self.policy = _Policy()
        self.intents: list[CompanionIntent] = []
        self.muted = False

        def dispatch(intent: CompanionIntent) -> CompanionSnapshot:
            self.intents.append(intent)
            if intent.kind is IntentKind.START_RECORDING:
                return CompanionSnapshot(RuntimeState(RuntimePhase.RECORDING))
            if intent.kind is IntentKind.FINISH_RECORDING:
                return CompanionSnapshot(RuntimeState(RuntimePhase.TRANSCRIBING))
            if intent.kind is IntentKind.TOGGLE_OUTPUT_MUTE:
                self.muted = not self.muted
            return CompanionSnapshot(RuntimeState(), output_muted=self.muted)

        self.shell = TkCompanionShell(
            dispatch,
            CompanionSnapshot(RuntimeState(), "ready detail"),
            tk_module=self.tk,
            window_policy=self.policy,
            poll_milliseconds=10,
        )

    def tearDown(self) -> None:
        self.shell.close()

    def test_launch_is_compact_semantic_and_native_nonactivating(self) -> None:
        self.assertIn(("geometry", "520x326"), self.tk.root.calls)
        self.assertIn(("resizable", (False, False)), self.tk.root.calls)
        self.assertEqual(self.policy.calls[:2], [("apply", 41), ("show", 99)])
        self.assertEqual(self.shell._cue.get(), "IDLE")
        self.assertEqual(self.shell._status.get(), "Companion ready")
        self.assertEqual(self.shell._detail.get(), "ready detail")
        self.assertEqual(self.shell._microphone_level.get(), "MIC · STANDBY")
        self.assertEqual(len(self.shell._semantic_labels), 5)
        self.assertTrue(
            all(label.place_options for label in self.shell._semantic_labels)
        )
        canvas_text = {
            str(options.get("text"))
            for kind, _args, options in self.shell._canvas.items
            if kind == "text"
        }
        self.assertIn("TALKTOMEJOHNNY", canvas_text)
        self.assertEqual(self.shell._output_status.get(), "OUTPUT · 100%")
        self.assertEqual(self.tk.root.focus_calls, 0)

    def test_workflow_mute_and_surface_controls_dispatch_focus_permissions(self) -> None:
        self.shell.buttons[IntentKind.START_RECORDING].invoke()
        self.assertEqual(self.intents[-1], CompanionIntent(IntentKind.START_RECORDING))
        self.assertEqual(
            self.shell.buttons[IntentKind.FINISH_RECORDING].options["state"],
            self.tk.NORMAL,
        )
        self.shell.buttons[IntentKind.FINISH_RECORDING].invoke()
        self.shell.buttons[IntentKind.TOGGLE_OUTPUT_MUTE].invoke()
        self.assertEqual(self.shell._mute_text.get(), "UNMUTE")

        for kind in (
            IntentKind.OPEN_SETTINGS,
            IntentKind.OPEN_VOICE,
            IntentKind.OPEN_REVIEW,
            IntentKind.OPEN_DIAGNOSTICS,
        ):
            self.shell.buttons[kind].invoke()
            self.assertEqual(self.intents[-1], CompanionIntent(kind, allow_focus=True))
        self.assertTrue(
            all(
                not intent.allow_focus
                for intent in self.intents
                if intent.kind
                not in {
                    IntentKind.OPEN_SETTINGS,
                    IntentKind.OPEN_VOICE,
                    IntentKind.OPEN_REVIEW,
                    IntentKind.OPEN_DIAGNOSTICS,
                }
            )
        )

    def test_unsolicited_updates_change_text_and_controls_without_focus(self) -> None:
        self.shell.publish(
            CompanionSnapshot(
                runtime=RuntimeState(RuntimePhase.RECORDING),
                detail="microphone active",
                output_muted=False,
                output_volume=64,
                microphone_level=0.42,
            )
        )

        self.shell._drain_updates()

        self.assertEqual(self.shell._cue.get(), "RECORDING")
        self.assertEqual(self.shell._status.get(), "Recording")
        self.assertEqual(self.shell._detail.get(), "microphone active")
        self.assertEqual(self.shell._microphone_level.get(), "MIC · LIVE 42%")
        self.assertEqual(self.shell._output_status.get(), "OUTPUT · 64%")
        self.assertTrue(
            any(
                kind == "line" and options.get("tags") == ("waveform",)
                for kind, _args, options in self.shell._canvas.items
            )
        )
        self.assertEqual(self.tk.root.focus_calls, 0)
        self.assertEqual(self.policy.calls.count(("show", 99)), 1)

    def test_recording_silence_is_labeled_no_signal_not_hardware_muted(self) -> None:
        self.shell.publish(
            CompanionSnapshot(
                runtime=RuntimeState(RuntimePhase.RECORDING),
                microphone_level=0.0,
            )
        )

        self.shell._drain_updates()

        self.assertEqual(self.shell._microphone_level.get(), "MIC · NO SIGNAL")
        self.assertNotEqual(self.shell._microphone_level.get(), "MIC · MUTED")

    def test_muted_output_canvas_text_preserves_volume_percent(self) -> None:
        self.shell.publish(
            CompanionSnapshot(
                runtime=RuntimeState(RuntimePhase.IDLE),
                output_muted=True,
                output_volume=37,
            )
        )

        self.shell._drain_updates()

        self.assertEqual(self.shell._output_status.get(), "OUTPUT · MUTED (37%)")

    def test_paused_snapshot_uses_paused_matrix_state_text(self) -> None:
        self.shell.publish(
            CompanionSnapshot(
                runtime=RuntimeState(RuntimePhase.PAUSED),
                detail="reply paused",
            )
        )

        self.shell._drain_updates()

        self.assertEqual(self.shell._cue.get(), "PAUSED")
        self.assertEqual(self.shell._status.get(), "Speech paused")
        self.assertEqual(self.shell._semantic_labels[0].options["textvariable"], self.shell._cue)

    def test_recoverable_error_snapshot_uses_error_matrix_state_text(self) -> None:
        self.shell.publish(
            CompanionSnapshot(
                runtime=RuntimeState(
                    RuntimePhase.RECOVERABLE_ERROR,
                    resume_phase=RuntimePhase.IDLE,
                    error_code="capture_failed",
                ),
                detail="capture needs attention",
            )
        )

        self.shell._drain_updates()

        self.assertEqual(self.shell._cue.get(), "ERROR")
        self.assertEqual(self.shell._status.get(), "Companion needs attention")
        self.assertEqual(self.shell._semantic_labels[0].options["textvariable"], self.shell._cue)

    def test_dispatch_failure_updates_visible_native_detail(self) -> None:
        def fail(_intent: CompanionIntent) -> CompanionSnapshot:
            raise RuntimeError("private failure detail")

        self.shell._dispatch = fail
        self.shell.buttons[IntentKind.START_RECORDING].invoke()

        self.assertEqual(self.shell._detail.get(), "Action unavailable")
        self.assertEqual(
            self.shell._semantic_labels[2].options["textvariable"],
            self.shell._detail,
        )

    def test_quit_returns_to_application_owned_shutdown_without_dispatch(self) -> None:
        self.shell.buttons[IntentKind.QUIT].invoke()
        self.shell.close()

        self.assertNotIn(IntentKind.QUIT, [intent.kind for intent in self.intents])
        self.assertTrue(self.tk.root.destroyed)
        self.assertTrue(any(call[0] == "after_cancel" for call in self.tk.root.calls))
        self.assertEqual(self.policy.calls[-1], ("release", 99))

    def test_workflow_intent_cannot_be_constructed_with_focus_permission(self) -> None:
        with self.assertRaises(ValueError):
            CompanionIntent(IntentKind.START_RECORDING, allow_focus=True)


class WindowsPolicyTests(unittest.TestCase):
    def test_policy_sets_noactivate_toolwindow_and_shows_without_activation(self) -> None:
        class Api:
            def __init__(self) -> None:
                self.style: tuple[int, int] | None = None
                self.calls: list[tuple[str, int]] = []

            def top_level(self, widget_handle: int) -> int:
                self.calls.append(("top", widget_handle))
                return 73

            def extended_style(self, window_handle: int) -> int:
                self.calls.append(("style", window_handle))
                return 0x20

            def set_extended_style(self, window_handle: int, style: int) -> None:
                self.style = (window_handle, style)

            def set_topmost_without_activation(self, window_handle: int) -> None:
                self.calls.append(("topmost", window_handle))

            def show_without_activation(self, window_handle: int) -> None:
                self.calls.append(("show", window_handle))

        api = Api()
        policy = WindowsNonActivatingPolicy(api)

        handle = policy.apply(41)
        policy.show_without_activation(handle)

        self.assertEqual(handle, 73)
        assert api.style is not None
        self.assertTrue(api.style[1] & policy.WS_EX_NOACTIVATE)
        self.assertTrue(api.style[1] & policy.WS_EX_TOOLWINDOW)
        self.assertEqual(api.calls[-2:], [("topmost", 73), ("show", 73)])


if __name__ == "__main__":
    unittest.main()
