"""Tests for the barge-in and default-voice configuration settings."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from talktomeclaude import config


class ConfigSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ, {"TALKTOMECLAUDE_CONFIG_DIR": self.tmp.name}, clear=False
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_barge_in_defaults_off_and_round_trips(self) -> None:
        self.assertFalse(config.barge_in_enabled())
        config.set_barge_in(True)
        self.assertTrue(config.barge_in_enabled())
        config.set_barge_in(False)
        self.assertFalse(config.barge_in_enabled())

    def test_default_voice_none_until_set_then_clears(self) -> None:
        self.assertIsNone(config.default_voice_name())
        config.set_default_voice("  rick  ")
        self.assertEqual(config.default_voice_name(), "rick")  # trimmed
        config.set_default_voice(None)
        self.assertIsNone(config.default_voice_name())
        config.set_default_voice("gimli")
        config.set_default_voice("")  # empty clears to auto-select
        self.assertIsNone(config.default_voice_name())

    def test_settings_are_independent(self) -> None:
        config.set_default_voice("rick")
        config.set_barge_in(True)
        self.assertEqual(config.default_voice_name(), "rick")
        self.assertTrue(config.barge_in_enabled())
        # Existing settings remain untouched.
        self.assertTrue(config.voice_assist_enabled())

    def test_wake_word_defaults_off_with_default_phrase(self) -> None:
        self.assertFalse(config.wake_word_enabled())
        self.assertEqual(config.wake_phrase(), "hey johnny")

    def test_wake_word_toggle_round_trips(self) -> None:
        config.set_wake_word(True)
        self.assertTrue(config.wake_word_enabled())
        config.set_wake_word(False)
        self.assertFalse(config.wake_word_enabled())

    def test_wake_phrase_round_trips(self) -> None:
        config.set_wake_phrase("hey claude")
        self.assertEqual(config.wake_phrase(), "hey claude")
        config.set_wake_phrase("yo claude")
        self.assertEqual(config.wake_phrase(), "yo claude")

    def test_stt_device_defaults_auto_guards_and_round_trips(self) -> None:
        self.assertEqual(config.stt_device(), "auto")
        config.set_stt_device("cuda")
        self.assertEqual(config.stt_device(), "cuda")
        config.set_stt_device("cpu")
        self.assertEqual(config.stt_device(), "cpu")
        with self.assertRaises(ValueError):
            config.set_stt_device("tpu")
        self.assertEqual(config.stt_device(), "cpu")  # rejected write changed nothing

    def test_stt_device_ignores_a_corrupt_stored_value(self) -> None:
        config.set_value("stt-device", "quantum")
        self.assertEqual(config.stt_device(), "auto")

    def test_command_namespace_policy_defaults_guards_and_round_trips(self) -> None:
        self.assertEqual(config.command_namespace_policy(), "ask-first-use")
        config.set_command_namespace_policy("ask-first-use")
        self.assertEqual(config.command_namespace_policy(), "ask-first-use")
        config.set_command_namespace_policy("allowlist")
        self.assertEqual(config.command_namespace_policy(), "allowlist")
        with self.assertRaises(ValueError):
            config.set_command_namespace_policy("deny-all")
        self.assertEqual(config.command_namespace_policy(), "allowlist")

    def test_command_namespace_allowlist_parses_trims_and_clears(self) -> None:
        self.assertEqual(config.command_namespace_allowlist(), ())
        config.set_command_namespace_allowlist(" kiln , gsd ,, ")
        self.assertEqual(config.command_namespace_allowlist(), ("kiln", "gsd"))
        config.set_command_namespace_allowlist(None)
        self.assertEqual(config.command_namespace_allowlist(), ())
        config.set_command_namespace_allowlist("solo")
        config.set_command_namespace_allowlist("")  # empty clears
        self.assertEqual(config.command_namespace_allowlist(), ())

    def test_clone_recipe_choice_defaults_later_and_guards(self) -> None:
        self.assertEqual(config.clone_recipe_choice(), "later")
        config.set_clone_recipe_choice("shown")
        self.assertEqual(config.clone_recipe_choice(), "shown")
        with self.assertRaises(ValueError):
            config.set_clone_recipe_choice("maybe")

    def test_assist_state_written_outside_is_read_inside_the_plugin_env(self) -> None:
        # LAW assist-mute: `assist off` from a normal shell must be the exact
        # state the Stop hook reads while Claude Code sets CLAUDE_PLUGIN_DATA.
        config.set_voice_assist(False)
        outside_path = config.config_path()
        with tempfile.TemporaryDirectory() as plugin_data:
            with mock.patch.dict(
                os.environ, {"CLAUDE_PLUGIN_DATA": plugin_data}, clear=False
            ):
                self.assertEqual(config.config_path(), outside_path)
                self.assertFalse(config.voice_assist_enabled())
        config.set_voice_assist(True)
        self.assertTrue(config.voice_assist_enabled())

    def test_companion_defaults_toggle_without_changing_legacy_default(self) -> None:
        self.assertEqual(config.recording_mode(), "push-to-talk")
        self.assertEqual(config.companion_recording_mode(), "push-toggle")
        config.set_recording_mode("push-to-talk")
        self.assertEqual(config.companion_recording_mode(), "push-to-talk")

    def test_assistant_auto_submit_defaults_on_and_round_trips(self) -> None:
        self.assertTrue(config.assistant_auto_submit_enabled())
        config.set_assistant_auto_submit(False)
        self.assertFalse(config.assistant_auto_submit_enabled())
        config.set_assistant_auto_submit(True)
        self.assertTrue(config.assistant_auto_submit_enabled())

    def test_assistant_provider_defaults_to_both_and_round_trips(self) -> None:
        self.assertEqual(config.assistant_provider(), "both")
        config.set_assistant_provider("codex")
        self.assertEqual(config.assistant_provider(), "codex")
        self.assertEqual(config.load()["assistant-provider"], "codex")
        config.set_assistant_provider("claude")
        self.assertEqual(config.assistant_provider(), "claude")
        config.set_assistant_provider("both")
        self.assertEqual(config.assistant_provider(), "both")

    def test_assistant_provider_rejects_unknown_value_without_mutation(self) -> None:
        config.set_assistant_provider("codex")
        before = config.load()

        with self.assertRaises(ValueError):
            config.set_assistant_provider("other")

        self.assertEqual(config.load(), before)

    def test_assistant_provider_ignores_corrupt_stored_value(self) -> None:
        config.set_value("assistant-provider", "other")
        self.assertEqual(config.assistant_provider(), "both")

    def test_control_keybinding_defaults_normalizes_and_round_trips(self) -> None:
        self.assertEqual(config.control_hotkey(), "ctrl+alt+space")
        config.set_control_keybinding(" Control + ? ")
        self.assertEqual(config.control_hotkey(), "ctrl+?")
        self.assertEqual(config.load()["control-keybinding"], "ctrl+?")
        config.set_control_keybinding("~")
        self.assertEqual(config.control_hotkey(), "~")

    def test_invalid_control_keybinding_does_not_mutate_settings(self) -> None:
        config.set_control_keybinding("~")
        before = config.load()

        with self.assertRaises(ValueError):
            config.set_control_keybinding("ctrl+alt")

        self.assertEqual(config.load(), before)

    def test_corrupt_control_keybinding_falls_back_without_rewriting_it(self) -> None:
        config.set_value("control-keybinding", "ctrl+alt")
        self.assertEqual(config.control_hotkey(), "ctrl+alt+space")
        self.assertEqual(config.load()["control-keybinding"], "ctrl+alt")


if __name__ == "__main__":
    unittest.main()
