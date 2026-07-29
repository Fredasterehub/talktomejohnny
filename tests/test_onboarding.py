"""Tests for the onboarding version bookkeeping and the first-run wizard screen."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner

from talktomeclaude import cli, config


class OnboardingConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ, {"TALKTOMECLAUDE_CONFIG_DIR": self.tmp.name}, clear=False
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_version_defaults_to_zero_and_round_trips(self) -> None:
        self.assertEqual(config.onboarding_version(), 0)
        config.set_onboarding_version(3)
        self.assertEqual(config.onboarding_version(), 3)

    def test_needed_compares_stored_against_current(self) -> None:
        config.set_onboarding_version(0)
        self.assertTrue(config.onboarding_needed(1))
        config.set_onboarding_version(5)
        self.assertFalse(config.onboarding_needed(1))
        self.assertTrue(config.onboarding_needed(6))

    def test_version_two_reopens_onboarding_for_version_one_operators(self) -> None:
        from talktomeclaude.onboarding import CURRENT_ONBOARDING_VERSION

        self.assertEqual(CURRENT_ONBOARDING_VERSION, 2)
        config.set_onboarding_version(1)  # completed the first release's wizard
        self.assertTrue(config.onboarding_needed(CURRENT_ONBOARDING_VERSION))


class SetupCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ, {"TALKTOMECLAUDE_CONFIG_DIR": self.tmp.name}, clear=False
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runner = CliRunner()

    def test_setup_help_documents_reset_and_force(self) -> None:
        result = self.runner.invoke(cli.main, ["setup", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("reset", result.output.lower())
        self.assertIn("force", result.output.lower())

    def test_subcommands_never_gated_by_onboarding(self) -> None:
        # A fresh install (onboarding version 0) must not block an unrelated
        # subcommand with the wizard — only the bare dashboard launch is gated.
        self.assertEqual(config.onboarding_version(), 0)
        result = self.runner.invoke(cli.main, ["config", "get", "recording-mode"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(config.onboarding_version(), 0)  # untouched

    def test_plain_setup_reruns_current_onboarding(self) -> None:
        from talktomeclaude import onboarding

        config.set_onboarding_version(onboarding.CURRENT_ONBOARDING_VERSION)
        with mock.patch.object(onboarding, "run_onboarding") as run:
            result = self.runner.invoke(cli.main, ["setup"])

        self.assertEqual(result.exit_code, 0, result.output)
        run.assert_called_once_with()


def _recommendation(feasible: bool):
    from talktomeclaude import advisor

    return advisor.Recommendation(
        stt_tier="CPU — test tier",
        clone_feasible=feasible,
        clone_reason="test",
        clone_recipe=("echo install-clone",) if feasible else (),
        notes=(),
    )


class _ScreenHarness(unittest.IsolatedAsyncioTestCase):
    """Isolated config dir + a bare host app for the onboarding screen."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(
            os.environ, {"TALKTOMECLAUDE_CONFIG_DIR": self.tmp.name}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)

    def _host(self, screen):
        from textual.app import App, ComposeResult

        result: dict = {}

        class _Host(App[None]):
            def compose(self) -> ComposeResult:
                return
                yield

            def on_mount(self) -> None:
                self.push_screen(screen, lambda value: result.setdefault("done", value))

        return _Host(), result


class OnboardingScreenTests(_ScreenHarness):
    async def test_is_a_textual_screen(self) -> None:
        from textual.screen import Screen

        from talktomeclaude.onboarding import OnboardingScreen

        self.assertTrue(issubclass(OnboardingScreen, Screen))

    async def test_defaults_fast_path_completes_immediately(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        app, result = self._host(OnboardingScreen())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("done", result)

    async def test_defaults_fast_path_preserves_existing_settings(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        config.set_recording_mode("push-toggle")
        config.set_claude_permissions("acceptEdits")
        config.set_default_voice("rick")
        config.set_remote("dev@example")
        app, result = self._host(OnboardingScreen())

        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

        self.assertIn("done", result)
        self.assertEqual(config.recording_mode(), "push-toggle")
        self.assertEqual(config.claude_permissions(), "acceptEdits")
        self.assertEqual(config.default_voice_name(), "rick")
        self.assertEqual(config.remote(), "dev@example")

    async def test_customize_walks_the_guided_sequence_and_persists(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("down", "enter")  # Customize step by step
                await pilot.pause()
                self.assertEqual(screen._step, "hardware")
                await pilot.press("enter")  # Continue
                await pilot.pause()
                self.assertEqual(screen._step, "stt-tier")
                await pilot.press("down", "enter")  # CUDA
                await pilot.pause()
                # clone-recipe is skipped: the advisor said cloning is infeasible
                self.assertEqual(screen._step, "claude")
                await pilot.press("enter")  # Local Claude Code
                await pilot.pause()
                self.assertEqual(screen._step, "voice")
                await pilot.press("enter")  # Auto (recommended)
                await pilot.pause()
                self.assertEqual(screen._step, "spoken")
                await pilot.press("s")  # skippable pane
                await pilot.pause()
                self.assertEqual(screen._step, "recording")
                await pilot.press("enter")  # push-to-talk default
                await pilot.pause()
                self.assertEqual(screen._step, "wake")
                await pilot.press("down", "enter")  # enable the wake word
                await pilot.pause()
                self.assertEqual(screen._step, "wake-phrase")
                await pilot.press("enter")  # keep the default phrase
                await pilot.pause()
                self.assertEqual(screen._step, "namespaces")
                await pilot.press("enter")  # ask on first use (safe default)
                await pilot.pause()
                self.assertEqual(screen._step, "permissions")
                await pilot.press("enter")  # off
                await pilot.pause()
                self.assertEqual(screen._step, "finish")
                self.assertEqual(config.onboarding_version(), 0)  # not yet
                await pilot.press("enter")  # Finish
                await pilot.pause()

        self.assertIs(result.get("done"), True)
        self.assertEqual(config.stt_device(), "cuda")
        self.assertEqual(config.recording_mode(), "push-to-talk")
        self.assertTrue(config.wake_word_enabled())
        self.assertEqual(config.command_namespace_policy(), "ask-first-use")
        self.assertEqual(config.claude_permissions(), "off")
        self.assertGreaterEqual(config.onboarding_version(), 2)
        self.assertIsNotNone(config.onboarding_completed_at())

    async def test_escape_fast_path_from_a_new_pane(self) -> None:
        from talktomeclaude.onboarding import (
            CURRENT_ONBOARDING_VERSION,
            OnboardingScreen,
        )

        screen = OnboardingScreen()
        app, result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("down", "enter", "enter")  # customize → stt-tier
                await pilot.pause()
                self.assertEqual(screen._step, "stt-tier")
                await pilot.press("escape")
                await pilot.pause()
        self.assertIs(result.get("done"), True)
        self.assertEqual(config.onboarding_version(), CURRENT_ONBOARDING_VERSION)


class SttTierPaneTests(_ScreenHarness):
    async def test_skip_leaves_the_setting_untouched_and_back_returns(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("down", "enter", "enter")
                await pilot.pause()
                self.assertEqual(screen._step, "stt-tier")
                await pilot.press("b")  # back
                await pilot.pause()
                self.assertEqual(screen._step, "hardware")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(screen._step, "stt-tier")
                await pilot.press("s")  # skip persists nothing
                await pilot.pause()
                self.assertEqual(screen._step, "claude")
        self.assertNotIn("stt-device", config.load())
        self.assertEqual(config.stt_device(), "auto")


class CloneRecipePaneTests(_ScreenHarness):
    async def _to_clone_recipe(self, pilot, screen) -> None:
        await pilot.pause()
        await pilot.press("down", "enter", "enter", "s")  # customize → hardware → stt skip
        await pilot.pause()
        self.assertEqual(screen._step, "clone-recipe")

    async def test_show_now_displays_the_recipe_text_and_persists(self) -> None:
        from textual.widgets import Static

        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(True)
        ), mock.patch(
            "talktomeclaude.advisor.clone_install_recipe",
            return_value=("echo one", "echo two"),
        ):
            async with app.run_test() as pilot:
                await self._to_clone_recipe(pilot, screen)
                await pilot.press("enter")  # Show the install recipe now
                await pilot.pause()
                self.assertEqual(screen._step, "clone-recipe-text")
                self.assertEqual(config.clone_recipe_choice(), "shown")
                text = str(screen.query_one("#ob-recipe-text", Static).content)
                self.assertIn("$ echo one", text)
                self.assertIn("$ echo two", text)
                await pilot.press("enter")  # Continue
                await pilot.pause()
                self.assertEqual(screen._step, "claude")

    async def test_later_persists_and_advances(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(True)
        ):
            async with app.run_test() as pilot:
                await self._to_clone_recipe(pilot, screen)
                await pilot.press("down", "enter")  # Later
                await pilot.pause()
                self.assertEqual(screen._step, "claude")
        self.assertEqual(config.clone_recipe_choice(), "later")

    async def test_skip_and_back_work(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(True)
        ):
            async with app.run_test() as pilot:
                await self._to_clone_recipe(pilot, screen)
                await pilot.press("b")
                await pilot.pause()
                self.assertEqual(screen._step, "stt-tier")
                await pilot.press("s")
                await pilot.pause()
                self.assertEqual(screen._step, "clone-recipe")
                await pilot.press("s")
                await pilot.pause()
                self.assertEqual(screen._step, "claude")
        self.assertNotIn("clone-recipe", config.load())


class NamespacesPaneTests(_ScreenHarness):
    async def _to_namespaces(self, pilot, screen) -> None:
        await pilot.pause()
        # customize, then skip every pane up to wake; wake's skip must land on
        # namespaces (the routing this milestone fixes), not permissions.
        await pilot.press("down", "enter", "s", "s", "s", "s", "s", "s", "s")
        await pilot.pause()
        self.assertEqual(screen._step, "namespaces")

    async def test_policy_persists_the_moment_selected(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
        ):
            async with app.run_test() as pilot:
                await self._to_namespaces(pilot, screen)
                await pilot.press("enter")  # Ask on first use (safe default)
                await pilot.pause()
                self.assertEqual(config.command_namespace_policy(), "ask-first-use")
                self.assertEqual(screen._step, "permissions")

    async def test_allowlist_branch_takes_a_comma_separated_list(self) -> None:
        from textual.widgets import Input

        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
        ):
            async with app.run_test() as pilot:
                await self._to_namespaces(pilot, screen)
                await pilot.press("down", "enter")  # Only an allowlist…
                await pilot.pause()
                self.assertEqual(screen._step, "namespaces-allowlist")
                self.assertEqual(config.command_namespace_policy(), "allowlist")
                screen.query_one(Input).value = " kiln , gsd "
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(screen._step, "permissions")
        self.assertEqual(config.command_namespace_allowlist(), ("kiln", "gsd"))

    async def test_allowlist_input_skip_routes_to_permissions(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
        ):
            async with app.run_test() as pilot:
                await self._to_namespaces(pilot, screen)
                await pilot.press("down", "enter")
                await pilot.pause()
                screen.action_skip()  # the focused Input owns printable keys
                await pilot.pause()
                self.assertEqual(screen._step, "permissions")
        self.assertEqual(config.command_namespace_allowlist(), ())

    async def test_skip_and_back_work(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
        ):
            async with app.run_test() as pilot:
                await self._to_namespaces(pilot, screen)
                await pilot.press("b")
                await pilot.pause()
                self.assertEqual(screen._step, "wake")
                await pilot.press("s")
                await pilot.pause()
                self.assertEqual(screen._step, "namespaces")
                await pilot.press("s")
                await pilot.pause()
                self.assertEqual(screen._step, "permissions")
        self.assertEqual(config.command_namespace_policy(), "ask-first-use")

    async def test_wake_phrase_submission_routes_through_namespaces(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("down", "enter", "s", "s", "s", "s", "s", "s")
                await pilot.pause()
                self.assertEqual(screen._step, "wake")
                await pilot.press("down", "enter")  # enable → wake-phrase
                await pilot.pause()
                self.assertEqual(screen._step, "wake-phrase")
                await pilot.press("enter")  # submit the phrase
                await pilot.pause()
                self.assertEqual(screen._step, "namespaces")

    async def test_wake_phrase_skip_routes_through_namespaces(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("down", "enter", "s", "s", "s", "s", "s", "s")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertEqual(screen._step, "wake-phrase")
                screen.action_skip()
                await pilot.pause()
                self.assertEqual(screen._step, "namespaces")


class AuditionTests(_ScreenHarness):
    async def test_voice_pane_p_auditions_the_highlighted_voice(self) -> None:
        from talktomeclaude.onboarding import _SAMPLE_TEXT, OnboardingScreen
        from talktomeclaude.tts import BUNDLED_VOICES

        calls: list[tuple[str, str | None]] = []
        screen = OnboardingScreen(audition=lambda text, name: calls.append((text, name)))
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("down", "enter", "enter", "s", "enter")
                await pilot.pause()
                self.assertEqual(screen._step, "voice")
                await pilot.press("p")  # Auto highlighted → the auto default
                await app.workers.wait_for_complete()
                await pilot.press("down", "p")  # first bundled voice
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.assertEqual(
            calls,
            [(_SAMPLE_TEXT, None), (_SAMPLE_TEXT, BUNDLED_VOICES[0].name)],
        )

    async def test_second_audition_is_ignored_while_one_is_in_flight(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        started = threading.Event()
        release = threading.Event()
        calls: list[tuple[str, str | None]] = []

        def helper(text: str, name: str | None) -> None:
            calls.append((text, name))
            started.set()
            release.wait(5)

        screen = OnboardingScreen(audition=helper)
        app, _result = self._host(screen)
        try:
            with mock.patch(
                "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
            ):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    await pilot.press("down", "enter", "enter", "s", "enter")
                    await pilot.pause()
                    self.assertEqual(screen._step, "voice")
                    await pilot.press("p")  # first audition begins and blocks
                    for _ in range(50):
                        if started.is_set():
                            break
                        await pilot.pause()
                    self.assertTrue(started.is_set())
                    self.assertTrue(screen._auditioning)
                    await pilot.press("p")  # the busy guard must ignore this one
                    await pilot.pause()
                    self.assertEqual(len(calls), 1)
                    release.set()
                    await app.workers.wait_for_complete()
        finally:
            release.set()
        self.assertEqual(len(calls), 1)

    async def test_p_is_inert_off_the_voice_pane(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        calls: list[tuple[str, str | None]] = []
        screen = OnboardingScreen(audition=lambda text, name: calls.append((text, name)))
        app, _result = self._host(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")  # welcome pane
            await app.workers.wait_for_complete()
            await pilot.pause()
        self.assertEqual(calls, [])

    async def test_finish_t_speaks_through_the_chosen_default(self) -> None:
        from talktomeclaude.onboarding import _TEST_TEXT, OnboardingScreen

        config.set_default_voice("rick")
        calls: list[tuple[str, str | None]] = []
        screen = OnboardingScreen(audition=lambda text, name: calls.append((text, name)))
        app, result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(False)
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press(
                    "down", "enter", "s", "s", "s", "s", "s", "s", "s", "s", "s"
                )
                await pilot.pause()
                self.assertEqual(screen._step, "finish")
                await pilot.press("t")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(calls, [(_TEST_TEXT, "rick")])
                await pilot.press("enter")  # Finish unchanged
                await pilot.pause()
        self.assertIs(result.get("done"), True)


def _fake_clone_screen():
    """Factory patched over clone_ui.CloneScreen; builds a real minimal Screen."""
    from textual.screen import Screen
    from textual.widgets import Static

    class _Fake(Screen[bool]):
        def __init__(self) -> None:
            super().__init__()
            self.created_voice = SimpleNamespace(name="my-clone")

        def compose(self):
            yield Static("fake clone screen")

    return _Fake()


class CloneFromOnboardingTests(_ScreenHarness):
    async def _to_clone_screen(self, pilot, screen) -> None:
        await pilot.pause()
        await pilot.press("down", "enter", "enter", "s", "s", "enter")
        await pilot.pause()
        self.assertEqual(screen._step, "voice")
        await pilot.press("end", "enter")  # Clone your own voice…
        await pilot.pause()
        self.assertEqual(screen._step, "voice")  # waits; never advances early

    async def test_successful_clone_becomes_the_default_voice(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(True)
        ), mock.patch("talktomeclaude.clone_ui.CloneScreen", _fake_clone_screen):
            async with app.run_test() as pilot:
                await self._to_clone_screen(pilot, screen)
                await app.screen.dismiss(True)
                await pilot.pause()
                self.assertEqual(screen._step, "spoken")
        self.assertEqual(config.default_voice_name(), "my-clone")

    async def test_cancelled_clone_preserves_the_prior_default(self) -> None:
        from talktomeclaude.onboarding import OnboardingScreen

        config.set_default_voice("rick")
        screen = OnboardingScreen()
        app, _result = self._host(screen)
        with mock.patch(
            "talktomeclaude.advisor.recommend", return_value=_recommendation(True)
        ), mock.patch("talktomeclaude.clone_ui.CloneScreen", _fake_clone_screen):
            async with app.run_test() as pilot:
                await self._to_clone_screen(pilot, screen)
                await app.screen.dismiss(False)
                await pilot.pause()
                # Cancelling never advances past the voice pane...
                self.assertEqual(screen._step, "voice")
        # ...and never clears the operator's existing default voice.
        self.assertEqual(config.default_voice_name(), "rick")


if __name__ == "__main__":
    unittest.main()
