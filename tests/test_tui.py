"""Tests for the Textual voice dashboard."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner

from talktomeclaude import cli, listen, tui
from talktomeclaude.tui import QueueKeys, TalkToMeApp


class _ConfigIsolation(unittest.IsolatedAsyncioTestCase):
    """Point config at a throwaway XDG dir so set_* never touches real state."""

    def setUp(self) -> None:
        self._cfg = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": self._cfg.name}, clear=False
        )
        self._env.start()
        os.environ.pop("TALKTOMECLAUDE_REDUCED_MOTION", None)

    def tearDown(self) -> None:
        self._env.stop()
        self._cfg.cleanup()


class TalkToMeAppTests(_ConfigIsolation):
    async def test_boots_ready_with_core_widgets(self) -> None:
        app = TalkToMeApp(lambda _text: None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.phase, "ready")
            self.assertIn("ttmj", app.available_themes)
            self.assertEqual(app.get_css_variables()["ttmj-ochre"], "#8a5e0f")
            self.assertIsNotNone(app.query_one("#header"))
            self.assertIsNotNone(app.query_one("#dialogue", tui.RichLog))
            band = app.query_one("#band", tui.HeaderBand)
            self.assertIn("TALK TO ME, CLAUDE", band.render().plain)

    async def test_wake_chip_present(self) -> None:
        app = TalkToMeApp(lambda _text: None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            chip = app.query_one("#wake-chip", tui.Static)
            self.assertIn("WAKE", chip.render().plain)

    async def test_wake_chip_never_claims_on_without_a_detector_model(self) -> None:
        from talktomeclaude import config

        config.set_wake_word(True)  # opted in, but no wake-model configured
        app = TalkToMeApp(lambda _text: None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            chip = app.query_one("#wake-chip", tui.Static)
            self.assertEqual(chip.render().plain, "WAKE MANUAL")

    async def test_unreadable_config_marks_wake_unavailable(self) -> None:
        from talktomeclaude import config

        config.config_path().parent.mkdir(parents=True, exist_ok=True)
        config.config_path().write_text("{ broken", encoding="utf-8")
        app = TalkToMeApp(lambda _text: None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            chip = app.query_one("#wake-chip", tui.Static)
            self.assertEqual(chip.render().plain, "WAKE UNAVAILABLE")

    async def test_wake_degraded_status_flips_the_chip_and_surfaces_why(self) -> None:
        from talktomeclaude import config

        config.set_wake_word(True)
        config.set_wake_model_path("/models/yo-claude.onnx")
        app = TalkToMeApp(lambda _text: None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            chip = app.query_one("#wake-chip", tui.Static)
            self.assertEqual(chip.render().plain, "WAKE ON")
            warning = (
                "wake word unavailable: corrupt detector; "
                "manual push-to-talk required"
            )
            app.post_message(tui.Status(warning))
            await pilot.pause()
            self.assertEqual(chip.render().plain, "WAKE UNAVAILABLE")
            self.assertIn("manual push-to-talk", app.notice)

    async def test_reduced_motion_disables_animation(self) -> None:
        with mock.patch.dict(os.environ, {"TALKTOMECLAUDE_REDUCED_MOTION": "1"}):
            app = TalkToMeApp(lambda _text: None)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                self.assertEqual(app.animation_level, "none")

    async def test_compact_class_on_small_terminal(self) -> None:
        app = TalkToMeApp(lambda _text: None)
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            self.assertTrue(app.screen.has_class("-compact"))
            band = app.query_one("#band", tui.HeaderBand)
            self.assertIn("TALK TO ME, CLAUDE", band.render().plain)

    async def test_mode_key_cycles_and_persists(self) -> None:
        from talktomeclaude import config

        config.set_recording_mode("always-on")
        app = TalkToMeApp(lambda _text: None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            start = app.mode
            await pilot.press("m")
            await pilot.pause()
            self.assertNotEqual(app.mode, start)
            self.assertEqual(config.recording_mode(), app.mode)

    async def test_phase_message_updates_pill_and_status(self) -> None:
        app = TalkToMeApp(lambda _text: None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.post_message(tui.Phase("recording"))
            await pilot.pause()
            self.assertEqual(app.phase, "recording")
            self.assertTrue(app.query_one("#phase").has_class("-recording"))
            self.assertTrue(app.query_one("#status").has_class("-recording"))


class VoiceBridgeTests(_ConfigIsolation):
    async def test_space_spawns_worker_with_bridge_args(self) -> None:
        seen: dict = {}

        def fake(**kwargs):
            seen.update(kwargs)

        with mock.patch.object(tui, "run_listen", fake):
            app = TalkToMeApp(lambda _text: None)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("space")
                await app.workers.wait_for_complete()
                await pilot.pause()
                mode = app.mode
        self.assertIsInstance(seen.get("keys"), QueueKeys)
        self.assertIsNotNone(seen.get("stop_event"))
        self.assertEqual(seen.get("trigger_key"), " ")
        self.assertEqual(seen.get("mode"), mode)

    async def test_callbacks_update_widgets(self) -> None:
        def fake(**kwargs):
            kwargs["on_phase"]("recording")
            kwargs["on_level"](0.5)
            kwargs["echo"]("you: hi there")
            kwargs["echo"]("claude: yo back")
            kwargs["status"]("stt tier: small.en")

        with mock.patch.object(tui, "run_listen", fake):
            app = TalkToMeApp(lambda _text: None)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("space")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.level, 0.5)
                self.assertEqual(app.tier, "small.en")
                lines = " ".join(
                    strip.text for strip in app.query_one("#dialogue", tui.RichLog).lines
                )
                self.assertIn("hi there", lines)
                self.assertIn("yo back", lines)
                self.assertEqual(app.phase, "ready")  # reset after the worker returns

    async def test_worker_error_sets_error_phase_without_exit(self) -> None:
        def fake(**_kwargs):
            raise listen.ListenError("microphone unavailable")

        with mock.patch.object(tui, "run_listen", fake):
            app = TalkToMeApp(lambda _text: None)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("space")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.phase, "error")
                self.assertIn("microphone unavailable", app.notice)
                self.assertFalse(app._voice_running)

    async def test_mirror_events_render_in_session_panel(self) -> None:
        def fake(**kwargs):
            kwargs["on_event"]({"type": "system", "subtype": "init", "model": "opus", "tools": []})
            kwargs["on_event"]({
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "TODO"}}]},
            })

        with mock.patch.object(tui, "run_listen", fake):
            app = TalkToMeApp(lambda _text: None)
            async with app.run_test(size=(120, 34)) as pilot:
                await pilot.pause()
                await pilot.press("space")
                await app.workers.wait_for_complete()
                await pilot.pause()
                lines = " ".join(
                    strip.text for strip in app.query_one("#session", tui.RichLog).lines
                )
                self.assertIn("Grep", lines)
                self.assertIn("session · opus", lines)

    async def test_bridge_threads_permission_and_records_session(self) -> None:
        from talktomeclaude import config

        config.set_claude_permissions("skip")
        seen: dict = {}

        def fake(**kwargs):
            seen.update(kwargs)
            kwargs["on_session"]("session-abc-123")

        with mock.patch.object(tui, "run_listen", fake):
            app = TalkToMeApp(lambda _text: None)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("space")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.current_session_id, "session-abc-123")
        self.assertEqual(seen.get("permission"), "skip")

    async def test_bridge_threads_the_persisted_stt_device(self) -> None:
        from talktomeclaude import config

        config.set_stt_device("cpu")
        seen: dict = {}

        def fake(**kwargs):
            seen.update(kwargs)

        with mock.patch.object(tui, "run_listen", fake):
            app = TalkToMeApp(lambda _text: None)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("space")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.assertEqual(seen.get("device"), "cpu")

    async def test_wake_key_toggles_and_persists(self) -> None:
        from talktomeclaude import config

        app = TalkToMeApp(lambda _text: None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            start = app.wake_enabled
            await pilot.press("w")
            await pilot.pause()
            self.assertNotEqual(app.wake_enabled, start)
            self.assertEqual(config.wake_word_enabled(), app.wake_enabled)

    async def test_wake_toggle_stays_manual_after_a_mid_session_degradation(self) -> None:
        from talktomeclaude import config

        config.set_wake_word(True)
        config.set_wake_model_path("/models/yo-claude.onnx")
        app = TalkToMeApp(lambda _text: None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            chip = app.query_one("#wake-chip", tui.Static)
            self.assertEqual(chip.render().plain, "WAKE ON")
            # A running session whose detector fails mid-session sticks to manual.
            app._voice_running = True
            app.post_message(
                tui.Status(
                    "wake word manual fallback: detector died; push-to-talk required"
                )
            )
            await pilot.pause()
            self.assertEqual(chip.render().plain, "WAKE MANUAL")
            # Toggling wake off then on must not falsely reclaim readiness while
            # the live session is still manual.
            await pilot.press("w")  # off
            await pilot.pause()
            await pilot.press("w")  # on again, mid-session
            await pilot.pause()
            self.assertEqual(chip.render().plain, "WAKE MANUAL")
            self.assertIn("manual", app.notice.lower())
            app._voice_running = False

    async def test_escape_stops_a_running_session(self) -> None:
        def fake(**kwargs):
            kwargs["keys"].read_key(None)  # blocks until stop() raises

        with mock.patch.object(tui, "run_listen", fake):
            app = TalkToMeApp(lambda _text: None)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("space")
                await pilot.pause()
                self.assertTrue(app._voice_running)
                await pilot.press("escape")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertFalse(app._voice_running)
                self.assertEqual(app.phase, "ready")


class QueueKeysTests(unittest.TestCase):
    def test_contract_fifo_drain_and_context(self) -> None:
        keys = QueueKeys()
        self.assertIsNone(keys.read_key(0.01))
        keys.push("a")
        keys.push("b")
        self.assertEqual(keys.read_key(0), "a")
        self.assertEqual(keys.read_key(0), "b")
        keys.push("x")
        keys.drain()
        self.assertIsNone(keys.read_key(0))
        self.assertIsNone(keys.is_pressed(" "))
        with keys as ctx:
            self.assertIs(ctx, keys)

    def test_blocking_read_unblocks_when_fed(self) -> None:
        keys = QueueKeys()

        def feed() -> None:
            time.sleep(0.05)
            keys.push(" ")

        threading.Thread(target=feed, daemon=True).start()
        self.assertEqual(keys.read_key(2.0), " ")

    def test_stop_raises_for_every_reader(self) -> None:
        keys = QueueKeys()
        keys.stop()
        with self.assertRaises(KeyboardInterrupt):
            keys.read_key(None)
        with self.assertRaises(KeyboardInterrupt):
            keys.read_key(0)  # sentinel is re-armed for the next reader
        other = QueueKeys()
        other.stop()
        with self.assertRaises(KeyboardInterrupt):
            other.drain()


class InjectedKeysTests(unittest.TestCase):
    def test_injected_keys_bypasses_rawkeys(self) -> None:
        class OneShot:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def drain(self) -> None:
                pass

            def is_pressed(self, _key):
                return None

            def read_key(self, _timeout):
                raise KeyboardInterrupt

        with mock.patch.object(
            listen, "_RawKeys", side_effect=AssertionError("must not open a raw reader")
        ), mock.patch.object(listen, "UtteranceTranscriber", return_value=mock.Mock()):
            with self.assertRaises(KeyboardInterrupt):
                listen.run_listen(
                    mode="push-toggle",
                    session_id=None,
                    tmux_pane=None,
                    device="auto",
                    model=None,
                    once=True,
                    echo=lambda _m: None,
                    speak=lambda _m: None,
                    status=lambda _m: None,
                    keys=OneShot(),
                )

    def test_stop_event_returns_before_capture(self) -> None:
        event = threading.Event()
        event.set()
        captured = mock.Mock(side_effect=AssertionError("must not capture when stopping"))
        with mock.patch.object(listen, "UtteranceTranscriber", return_value=mock.Mock()), \
                mock.patch.object(listen, "_record_always_on", captured):
            listen.run_listen(
                mode="always-on",
                session_id=None,
                tmux_pane=None,
                device="auto",
                model=None,
                once=True,
                echo=lambda _m: None,
                speak=lambda _m: None,
                status=lambda _m: None,
                stop_event=event,
            )
        captured.assert_not_called()


class MirrorFormatTests(unittest.TestCase):
    def test_init_and_result_markers(self) -> None:
        self.assertEqual(
            tui._mirror_lines({"type": "system", "subtype": "init", "model": "opus", "tools": ["a", "b"]}),
            [("mark", "● session · opus · 2 tools")],
        )
        done = tui._mirror_lines({"type": "result", "is_error": False, "duration_ms": 8423})
        self.assertEqual(done, [("mark", "● done · 8423 ms")])

    def test_tool_use_and_tool_result(self) -> None:
        used = tui._mirror_lines({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]},
        })
        self.assertEqual(used, [("tool", "▸ Bash  ls -la")])
        out = tui._mirror_lines({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "output here"}]},
        })
        self.assertEqual(out, [("out", "  ↳ output here")])

    def test_unknown_event_and_truncation(self) -> None:
        self.assertEqual(tui._mirror_lines({"type": "rate_limit_event"}), [])
        used = tui._mirror_lines({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "x" * 500}}]},
        })
        self.assertLessEqual(len(used[0][1]), 130)
        self.assertTrue(used[0][1].endswith("…"))


class _FakeProc:
    def __init__(self, lines, returncode=0, stderr=""):
        self.stdout = iter(lines)
        self.stderr = iter([stderr] if stderr else [])
        self.returncode = None
        self._rc = returncode

    def wait(self, timeout=None):
        self.returncode = self._rc

    def terminate(self):
        self.returncode = self._rc

    def kill(self):
        self.returncode = self._rc


class StreamConsumeTests(unittest.TestCase):
    def _lines(self, *events):
        return [json.dumps(event) + "\n" for event in events]

    def test_extracts_result_and_forwards_events(self) -> None:
        lines = self._lines(
            {"type": "system", "subtype": "init", "session_id": "sess1", "model": "m", "tools": []},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        )
        lines += ["\n", "not json\n"]  # blank + malformed tolerated
        lines += self._lines(
            {"type": "result", "subtype": "success", "result": "all done", "session_id": "sess1", "duration_ms": 10}
        )
        seen = []
        with mock.patch.object(listen.subprocess, "Popen", return_value=_FakeProc(lines)):
            result, session = listen._consume_stream(["claude"], seen.append, None, None)
        self.assertEqual(result, "all done")
        self.assertEqual(session, "sess1")
        self.assertEqual([event["type"] for event in seen], ["system", "assistant", "result"])

    def test_raises_on_nonzero_exit_with_stderr(self) -> None:
        with mock.patch.object(
            listen.subprocess, "Popen", return_value=_FakeProc(["\n"], returncode=1, stderr="kaboom")
        ):
            with self.assertRaisesRegex(listen.ListenError, "kaboom"):
                listen._consume_stream(["claude"], lambda _event: None, None, None)

    def test_raises_when_stream_has_no_result(self) -> None:
        lines = self._lines({"type": "assistant", "message": {"content": []}})
        with mock.patch.object(listen.subprocess, "Popen", return_value=_FakeProc(lines)):
            with self.assertRaisesRegex(listen.ListenError, "without a result"):
                listen._consume_stream(["claude"], lambda _event: None, None, None)

    def test_error_result_raises_instead_of_speaking(self) -> None:
        lines = self._lines(
            {"type": "result", "is_error": True, "result": "API overloaded", "session_id": "s"}
        )
        with mock.patch.object(listen.subprocess, "Popen", return_value=_FakeProc(lines)):
            with self.assertRaisesRegex(listen.ListenError, "API overloaded"):
                listen._consume_stream(["claude"], lambda _event: None, None, None)

    def test_stop_event_aborts_mid_stream(self) -> None:
        event = threading.Event()
        event.set()
        lines = self._lines(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "result", "result": "done", "session_id": "s"},
        )
        proc = _FakeProc(lines)
        with mock.patch.object(listen.subprocess, "Popen", return_value=proc):
            result, _session = listen._consume_stream(
                ["claude"], lambda _event: None, None, None, event
            )
        self.assertEqual(result, "")
        self.assertIsNotNone(proc.returncode)  # child was reaped, not orphaned


class RemoteProjectTests(unittest.TestCase):
    def test_project_discovery_returns_sorted_unique_paths(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="/srv/projects/zeta\n/srv/projects/Alpha\n/srv/projects/zeta\n",
            stderr="",
        )

        with mock.patch.object(tui, "_ssh_base", return_value=["ssh", "dev@example"]), mock.patch.object(
            tui, "_remote_shell_command", side_effect=lambda command: command
        ), mock.patch.object(tui.subprocess, "run", return_value=completed) as run:
            projects = tui.discover_remote_projects(
                "user@example-host", "/srv/projects"
            )

        self.assertEqual(
            projects, ["/srv/projects/Alpha", "/srv/projects/zeta"]
        )
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["ssh", "dev@example"])
        self.assertIn("find -- /srv/projects", command[-1])
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_project_discovery_reports_ssh_failure(self) -> None:
        completed = SimpleNamespace(returncode=255, stdout="", stderr="connection failed")

        with mock.patch.object(tui.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(tui.TUIError, "connection failed"):
                tui.discover_remote_projects("dev@example")

    def test_directory_check_uses_test_d_without_end_of_options(self) -> None:
        # `test -d -- PATH` is a bash error (no `--`); the check must omit it.
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(tui, "_ssh_base", return_value=["ssh", "dev@example"]), mock.patch.object(
            tui, "_remote_shell_command", side_effect=lambda command: command
        ), mock.patch.object(tui.subprocess, "run", return_value=completed) as run:
            self.assertTrue(
                tui.remote_directory_exists(
                    "user@example-host", "/srv/projects/example-project"
                )
            )

        inner = run.call_args.args[0][-1]
        self.assertIn("test -d ", inner)
        self.assertNotIn("-d -- ", inner)
        self.assertIn("/srv/projects/example-project", inner)


class LiveSignalTests(unittest.TestCase):
    def test_space_toggle_reports_recording_and_audio_level(self) -> None:
        keys = mock.Mock()
        keys.read_key.side_effect = [" ", " "]
        block = mock.Mock()
        block.copy.return_value = block
        stream = mock.MagicMock()
        stream.__enter__.return_value.read.return_value = (block, False)
        sounddevice = mock.Mock()
        sounddevice.InputStream.return_value = stream
        levels: list[float] = []
        recording = mock.Mock()

        with mock.patch.object(listen, "_sounddevice", return_value=sounddevice), mock.patch.object(
            listen, "_rms", return_value=0.125
        ), mock.patch.object(listen, "_finish", return_value="audio"):
            result = listen._record_push_toggle(
                keys,
                trigger_key=" ",
                on_level=levels.append,
                on_recording=recording,
            )

        self.assertEqual(result, "audio")
        self.assertEqual(levels, [0.125])
        recording.assert_called_once_with()

    def test_trigger_wait_ignores_non_matching_keys(self) -> None:
        keys = mock.Mock()
        keys.read_key.side_effect = ["p", " "]

        self.assertEqual(listen._wait_for_trigger(keys, " "), " ")

    def test_immediate_toggle_uses_dashboard_space_as_start(self) -> None:
        keys = mock.Mock()
        keys.read_key.return_value = " "
        block = mock.Mock()
        block.copy.return_value = block
        stream = mock.MagicMock()
        stream.__enter__.return_value.read.return_value = (block, False)
        sounddevice = mock.Mock()
        sounddevice.InputStream.return_value = stream

        with mock.patch.object(listen, "_sounddevice", return_value=sounddevice), mock.patch.object(
            listen, "_rms", return_value=0.1
        ), mock.patch.object(listen, "_finish", return_value="audio"):
            result = listen._record_push_toggle(
                keys,
                trigger_key=" ",
                start_immediately=True,
            )

        self.assertEqual(result, "audio")
        keys.read_key.assert_called_once_with(0)


class DashboardCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(
            os.environ, {"TALKTOMECLAUDE_CONFIG_DIR": self.tmp.name}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        self.runner = CliRunner()

    def test_no_arguments_runs_onboarding_then_launches_dashboard(self) -> None:
        with mock.patch("talktomeclaude.onboarding.run_onboarding") as onboard, \
                mock.patch.object(cli, "_launch_dashboard") as launch:
            result = self.runner.invoke(cli.main, [])

        self.assertEqual(result.exit_code, 0, result.output)
        onboard.assert_called_once_with()
        launch.assert_called_once_with()

    def test_completed_onboarding_goes_straight_to_the_dashboard(self) -> None:
        from talktomeclaude import config
        from talktomeclaude.onboarding import CURRENT_ONBOARDING_VERSION

        config.set_onboarding_version(CURRENT_ONBOARDING_VERSION)
        with mock.patch("talktomeclaude.onboarding.run_onboarding") as onboard, \
                mock.patch.object(cli, "_launch_dashboard") as launch:
            result = self.runner.invoke(cli.main, [])

        self.assertEqual(result.exit_code, 0, result.output)
        onboard.assert_not_called()
        launch.assert_called_once_with()

    def test_ui_command_launches_dashboard(self) -> None:
        with mock.patch.object(cli, "_launch_dashboard") as launch:
            result = self.runner.invoke(cli.main, ["ui"])

        self.assertEqual(result.exit_code, 0, result.output)
        launch.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
