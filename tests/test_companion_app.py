from __future__ import annotations

import threading
import time
import unittest
from collections.abc import Callable
from unittest import mock

from talktomeclaude.companion.app import (
    CompanionStartupError,
    DesktopCompanionApplication,
    PersistentTranscriberFactory,
    _REMOTE_REPLY_COMMAND,
    _reply_inbox_path,
    _reply_spool_path,
    _remote_reply_command,
    _route_hotkey_press,
    _route_hotkey_release,
    _safe_stt_status,
    _selected_voice,
    ensure_companion_hook,
)
from talktomeclaude.capture import CaptureMode
from talktomeclaude.companion.contracts import (
    CompanionIntent,
    CompanionSnapshot,
    IntentKind,
)
from talktomeclaude.core import RuntimePhase, RuntimeState


class PersistentTranscriberFactoryTests(unittest.TestCase):
    def test_warms_once_and_refreshes_cancellation_probe_per_turn(self) -> None:
        class Transcriber:
            def __init__(
                self,
                device: str,
                model: str | None,
                *,
                on_status: object,
                cancelled: Callable[[], bool],
            ) -> None:
                self.device = device
                self.model = model
                self.cancelled = cancelled
                created.append(self)

        created: list[Transcriber] = []

        first_cancelled = False
        second_cancelled = True
        factory = PersistentTranscriberFactory(
            "cuda", "large-v3", transcriber_type=Transcriber
        )
        first = factory(lambda: first_cancelled)
        second = factory(lambda: second_cancelled)

        self.assertIs(first, second)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].cancelled())

    def test_third_party_stt_status_is_reduced_to_content_free_codes(self) -> None:
        self.assertEqual(_safe_stt_status("CUDA model ready"), "cuda")
        self.assertEqual(
            _safe_stt_status("private transcript at C:/Users/Fred/file"),
            "updated",
        )


class DesktopCompanionApplicationTests(unittest.TestCase):
    def test_run_owns_start_subscription_hotkey_and_idempotent_cleanup(self) -> None:
        calls: list[tuple[str, object]] = []

        class Controller:
            def subscribe(
                self, listener: Callable[[CompanionSnapshot], None]
            ) -> Callable[[], None]:
                calls.append(("subscribe", listener))

                def unsubscribe() -> None:
                    calls.append(("unsubscribe", None))

                return unsubscribe

            def start_background(self) -> None:
                calls.append(("background", None))

            def dispatch(self, intent: CompanionIntent) -> CompanionSnapshot:
                calls.append(("dispatch", intent.kind))
                return CompanionSnapshot(RuntimeState())

        class Shell:
            def publish(self, _snapshot: CompanionSnapshot) -> None:
                return None

            def run(self) -> None:
                calls.append(("shell_run", None))

            def close(self) -> None:
                calls.append(("shell_close", None))

        class Hotkey:
            def start(self) -> None:
                calls.append(("hotkey_start", None))

            def stop(self) -> bool:
                calls.append(("hotkey_stop", None))
                return True

        application = DesktopCompanionApplication(Controller(), Shell(), Hotkey())

        self.assertEqual(application.run(), 0)
        names = [name for name, _value in calls]
        self.assertLess(names.index("background"), names.index("hotkey_start"))
        self.assertLess(names.index("hotkey_start"), names.index("shell_run"))
        self.assertLess(names.index("shell_run"), names.index("hotkey_stop"))
        self.assertIn(("dispatch", IntentKind.QUIT), calls)
        self.assertEqual(names[-2:], ["unsubscribe", "shell_close"])

    def test_one_absolute_deadline_rejects_live_hotkey_and_controller(self) -> None:
        release = threading.Event()

        class Controller:
            shutdown_clean = True

            def subscribe(
                self, _listener: Callable[[CompanionSnapshot], None]
            ) -> Callable[[], None]:
                return lambda: None

            def start_background(self) -> None:
                return None

            def dispatch(self, _intent: CompanionIntent) -> CompanionSnapshot:
                release.wait(1)
                return CompanionSnapshot(RuntimeState())

        class Shell:
            def publish(self, _snapshot: CompanionSnapshot) -> None:
                return None

            def run(self) -> None:
                return None

            def close(self) -> None:
                return None

        class Hotkey:
            def start(self) -> None:
                return None

            def stop(self) -> bool:
                release.wait(1)
                return True

        application = DesktopCompanionApplication(
            Controller(),
            Shell(),
            Hotkey(),
            shutdown_deadline_seconds=0.05,
        )
        started = time.monotonic()

        with self.assertRaisesRegex(RuntimeError, "shutdown deadline"):
            application.run()

        self.assertLess(time.monotonic() - started, 0.2)
        release.set()


class HotkeyRouterTests(unittest.TestCase):
    def test_toggle_routes_alternating_key_downs_without_release_monitor(self) -> None:
        controller = mock.Mock()
        controller.capture_mode = CaptureMode.PUSH_TOGGLE
        controller.snapshot = CompanionSnapshot(RuntimeState(RuntimePhase.IDLE))

        self.assertFalse(_route_hotkey_press(controller))
        self.assertEqual(
            controller.dispatch.call_args.args[0].kind,
            IntentKind.START_RECORDING,
        )

        controller.snapshot = CompanionSnapshot(
            RuntimeState(RuntimePhase.RECORDING)
        )
        self.assertFalse(_route_hotkey_press(controller))
        self.assertEqual(
            controller.dispatch.call_args.args[0].kind,
            IntentKind.FINISH_RECORDING,
        )

    def test_hold_routes_press_then_primary_key_release_exactly_once(self) -> None:
        controller = mock.Mock()
        controller.capture_mode = CaptureMode.HOLD_TO_TALK
        controller.snapshot = CompanionSnapshot(RuntimeState(RuntimePhase.IDLE))

        self.assertTrue(_route_hotkey_press(controller))
        controller.snapshot = CompanionSnapshot(
            RuntimeState(RuntimePhase.RECORDING)
        )
        _route_hotkey_release(controller)

        self.assertEqual(controller.dispatch.call_count, 2)
        self.assertEqual(
            [call.args[0].kind for call in controller.dispatch.call_args_list],
            [IntentKind.START_RECORDING, IntentKind.FINISH_RECORDING],
        )

    def test_hold_release_after_cancel_is_ignored(self) -> None:
        controller = mock.Mock()
        controller.capture_mode = CaptureMode.HOLD_TO_TALK
        controller.snapshot = CompanionSnapshot(RuntimeState(RuntimePhase.IDLE))

        _route_hotkey_release(controller)

        controller.dispatch.assert_not_called()


class SelectedVoiceTests(unittest.TestCase):
    def test_selected_unavailable_voice_never_substitutes_fallback(self) -> None:
        voice = mock.Mock(name="voice", spec=["name"])
        voice.name = "rick"
        with mock.patch(
            "talktomeclaude.companion.app.config.default_voice_name",
            return_value="rick",
        ), mock.patch(
            "talktomeclaude.companion.app.get_voice", return_value=voice
        ) as get, mock.patch(
            "talktomeclaude.companion.app.default_voice"
        ) as fallback, mock.patch(
            "talktomeclaude.companion.app.is_available", return_value=False
        ):
            with self.assertRaises(CompanionStartupError):
                _selected_voice()

        get.assert_called_once_with("rick")
        fallback.assert_not_called()


class HookBootstrapTests(unittest.TestCase):
    def test_local_hook_install_is_idempotent_and_owned(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "settings.json"
            ensure_companion_hook(None, local_settings_path=settings)
            ensure_companion_hook(None, local_settings_path=settings)
            wire = settings.read_text(encoding="utf-8")
        self.assertEqual(wire.count("talktomeclaude.windows-companion.v1"), 1)

    def test_remote_hook_uses_bounded_noninteractive_ssh(self) -> None:
        completed = mock.Mock(returncode=0)
        runner = mock.Mock(return_value=completed)
        ensure_companion_hook("dev@example", runner=runner)
        command = runner.call_args.args[0]
        self.assertEqual(command[:3], ["ssh", "-T", "-o"])
        self.assertEqual(command[-2:], ["dev@example", "talktomeclaude hook install"])
        self.assertEqual(runner.call_args.kwargs["timeout"], 15)

    def test_local_codex_hook_install_is_idempotent_and_owned(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "hooks.json"
            ensure_companion_hook(
                None,
                provider="codex",
                local_settings_path=settings,
            )
            ensure_companion_hook(
                None,
                provider="codex",
                local_settings_path=settings,
            )
            wire = settings.read_text(encoding="utf-8")
        self.assertEqual(
            wire.count("talktomeclaude.windows-companion.codex.v1"), 1
        )

    def test_remote_codex_hook_is_scoped_to_configured_project(self) -> None:
        completed = mock.Mock(returncode=0)
        runner = mock.Mock(return_value=completed)

        ensure_companion_hook(
            "dev@example",
            provider="codex",
            remote_cwd="/DEV/ghostundo",
            runner=runner,
        )

        command = runner.call_args.args[0]
        self.assertEqual(command[:3], ["ssh", "-T", "-o"])
        self.assertEqual(command[-2], "dev@example")
        self.assertEqual(
            command[-1],
            "talktomeclaude hook install --provider codex --settings "
            "/DEV/ghostundo/.codex/hooks.json",
        )
        self.assertEqual(runner.call_args.kwargs["timeout"], 15)

    def test_remote_codex_hook_requires_a_configured_project(self) -> None:
        runner = mock.Mock()

        with self.assertRaisesRegex(CompanionStartupError, "remote-cwd"):
            ensure_companion_hook(
                "dev@example",
                provider="codex",
                remote_cwd=None,
                runner=runner,
            )

        runner.assert_not_called()

    def test_remote_reply_stream_uses_the_installed_console_interpreter(self) -> None:
        self.assertEqual(
            _REMOTE_REPLY_COMMAND,
            ("talktomeclaude", "hook", "stream"),
        )
        self.assertEqual(
            _remote_reply_command("codex"),
            ("talktomeclaude", "hook", "stream", "--provider", "codex"),
        )

    def test_codex_uses_isolated_local_durable_state(self) -> None:
        from pathlib import Path

        root = Path("state-root")
        self.assertEqual(_reply_spool_path(root, "claude"), root / "reply-spool")
        self.assertEqual(
            _reply_inbox_path(root, "claude"),
            root / "reply-inbox",
        )
        self.assertEqual(
            _reply_spool_path(root, "codex"),
            root / "reply-spool-codex",
        )
        self.assertEqual(
            _reply_inbox_path(root, "codex"),
            root / "reply-inbox-codex",
        )


if __name__ == "__main__":
    unittest.main()
