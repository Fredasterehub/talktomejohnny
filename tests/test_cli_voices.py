"""CLI tests for the voice-management surface: voices group, voice create, doctor."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from talktomeclaude import cli, registry
from talktomeclaude.cli import main


class VoiceCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(
            os.environ, {"TALKTOMECLAUDE_CONFIG_DIR": str(self.root)}, clear=False
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runner = CliRunner()

    def _piper(self, stem: str = "byo") -> Path:
        model = self.root / f"{stem}.onnx"
        model.write_bytes(b"onnx")
        model.with_name(model.name + ".json").write_text("{}")
        return model

    def _ref(self) -> Path:
        ref = self.root / "ref.wav"
        ref.write_bytes(b"RIFFfakewav")
        return ref

    def test_bare_voices_lists_bundled_with_licenses(self) -> None:
        result = self.runner.invoke(main, ["voices"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertGreaterEqual(result.output.lower().count("license"), 2)

    def test_voices_add_then_listed_then_remove(self) -> None:
        model = self._piper()
        add = self.runner.invoke(main, ["voices", "add", "byo", str(model)])
        self.assertEqual(add.exit_code, 0, add.output)
        listed = self.runner.invoke(main, ["voices"])
        self.assertIn("byo", listed.output)
        self.assertIsNotNone(registry.get("byo"))
        removed = self.runner.invoke(main, ["voices", "remove", "byo"])
        self.assertEqual(removed.exit_code, 0, removed.output)
        self.assertIsNone(registry.get("byo"))

    def test_voices_add_rejects_bad_name(self) -> None:
        model = self._piper()
        result = self.runner.invoke(main, ["voices", "add", "en_US-ljspeech-high", str(model)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("bundled", result.output)

    def test_doctor_reports_recommendation(self) -> None:
        result = self.runner.invoke(main, ["doctor"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Recommendation", result.output)

    def test_voice_create_registers_clone_without_engine(self) -> None:
        result = self.runner.invoke(
            main, ["voice", "create", "rick", "--reference", str(self._ref()), "--no-sample"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("created cloned voice", result.output)
        voice = registry.get("rick")
        self.assertIsNotNone(voice)
        self.assertEqual(voice.engine, "clone")

    def test_voice_create_requires_a_source(self) -> None:
        result = self.runner.invoke(main, ["voice", "create", "rick"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--reference", result.output)

    def test_config_barge_in_and_default_voice_round_trip(self) -> None:
        self.assertEqual(self.runner.invoke(main, ["config", "get", "barge-in"]).output.strip(), "off")
        self.runner.invoke(main, ["config", "set", "barge-in", "on"])
        self.assertEqual(self.runner.invoke(main, ["config", "get", "barge-in"]).output.strip(), "on")
        self.assertEqual(
            self.runner.invoke(main, ["config", "get", "default-voice"]).output.strip(), "auto"
        )
        self.runner.invoke(main, ["config", "set", "default-voice", "rick"])
        self.assertEqual(
            self.runner.invoke(main, ["config", "get", "default-voice"]).output.strip(), "rick"
        )

    def test_config_control_keybinding_accepts_printable_single_keys(self) -> None:
        default = self.runner.invoke(main, ["config", "get", "control-keybinding"])
        self.assertEqual(default.exit_code, 0, default.output)
        self.assertEqual(default.output.strip(), "ctrl+alt+space")

        changed = self.runner.invoke(
            main,
            ["config", "set", "control-keybinding", "?"],
        )
        self.assertEqual(changed.exit_code, 0, changed.output)
        self.assertEqual(
            self.runner.invoke(
                main,
                ["config", "get", "control-keybinding"],
            ).output.strip(),
            "?",
        )

        invalid = self.runner.invoke(
            main,
            ["config", "set", "control-keybinding", "ctrl+alt"],
        )
        self.assertNotEqual(invalid.exit_code, 0)
        self.assertIn("binding has no key", invalid.output)
        self.assertEqual(
            self.runner.invoke(
                main,
                ["config", "get", "control-keybinding"],
            ).output.strip(),
            "?",
        )

    def test_config_output_volume_round_trips_and_rejects_invalid_values(self) -> None:
        default = self.runner.invoke(main, ["config", "get", "output-volume"])
        self.assertEqual(default.exit_code, 0, default.output)
        self.assertEqual(default.output.strip(), "100")

        changed = self.runner.invoke(main, ["config", "set", "output-volume", "37"])
        self.assertEqual(changed.exit_code, 0, changed.output)
        self.assertEqual(
            self.runner.invoke(main, ["config", "get", "output-volume"]).output.strip(),
            "37",
        )

        invalid = self.runner.invoke(main, ["config", "set", "output-volume", "101"])
        self.assertNotEqual(invalid.exit_code, 0)
        self.assertIn("between 0 and 100", invalid.output)
        self.assertEqual(
            self.runner.invoke(main, ["config", "get", "output-volume"]).output.strip(),
            "37",
        )

    def test_speak_consumes_configured_default_voice(self) -> None:
        self.runner.invoke(main, ["config", "set", "default-voice", "en_US-ljspeech-high"])
        with mock.patch("talktomeclaude.cli.synthesize") as synth:
            synth.return_value = mock.Mock(name="en_US-ljspeech-high")
            self.runner.invoke(main, ["speak", "hi", "--out", str(self.root / "o.wav")])
        # speak(text, out_path, voice_name, on_status=...): the resolved default
        # is passed as the voice_name, not None.
        self.assertEqual(synth.call_args.args[2], "en_US-ljspeech-high")

    def test_dashboard_reply_consumes_configured_default_voice(self) -> None:
        self.runner.invoke(main, ["config", "set", "default-voice", "en_US-ljspeech-high"])

        with mock.patch.object(cli, "synthesize") as synth, mock.patch.object(
            cli, "_play_wav"
        ):
            cli._speak_reply("hi")

        self.assertEqual(synth.call_args.args[2], "en_US-ljspeech-high")

    def test_speak_with_uninstalled_clone_fails_cleanly(self) -> None:
        self.runner.invoke(main, ["voice", "create", "rick", "--reference", str(self._ref()), "--no-sample"])
        with mock.patch("talktomeclaude.clone.clone_available", return_value=False):
            result = self.runner.invoke(
                main, ["speak", "hello", "--voice", "rick", "--out", str(self.root / "o.wav")]
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("doctor", result.output)  # points the user at the install recipe


if __name__ == "__main__":
    unittest.main()
