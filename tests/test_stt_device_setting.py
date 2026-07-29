"""Tests that the persisted stt-device setting reaches every consumption path."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from talktomeclaude import cli, config


class _CliHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(
            os.environ, {"TALKTOMECLAUDE_CONFIG_DIR": self.tmp.name}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        self.runner = CliRunner()


class TranscribeDeviceTests(_CliHarness):
    def _audio(self) -> str:
        path = Path(self.tmp.name) / "clip.wav"
        path.write_bytes(b"RIFF")
        return str(path)

    def test_show_tier_uses_the_persisted_device(self) -> None:
        config.set_stt_device("cpu")
        tier = mock.Mock()
        tier.describe.return_value = "cpu tier"
        with mock.patch("talktomeclaude.stt.detect_tier", return_value=tier) as detect:
            result = self.runner.invoke(cli.main, ["transcribe", self._audio(), "--show-tier"])
        self.assertEqual(result.exit_code, 0, result.output)
        detect.assert_called_once_with("cpu", None)

    def test_explicit_device_overrides_the_setting(self) -> None:
        config.set_stt_device("cpu")
        tier = mock.Mock()
        tier.describe.return_value = "gpu tier"
        with mock.patch("talktomeclaude.stt.detect_tier", return_value=tier) as detect:
            result = self.runner.invoke(
                cli.main, ["transcribe", self._audio(), "--device", "cuda", "--show-tier"]
            )
        self.assertEqual(result.exit_code, 0, result.output)
        detect.assert_called_once_with("cuda", None)

    def test_transcription_threads_the_persisted_device(self) -> None:
        config.set_stt_device("cuda")
        with mock.patch(
            "talktomeclaude.stt.transcribe_file", return_value=("hi", mock.Mock())
        ) as transcribe:
            result = self.runner.invoke(cli.main, ["transcribe", self._audio()])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(transcribe.call_args.args[1], "cuda")


class ListenDeviceTests(_CliHarness):
    def test_listen_uses_the_persisted_device(self) -> None:
        config.set_stt_device("cuda")
        with mock.patch("talktomeclaude.listen.run_listen") as run:
            result = self.runner.invoke(cli.main, ["listen", "--once"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(run.call_args.kwargs["device"], "cuda")

    def test_explicit_device_overrides_the_setting(self) -> None:
        config.set_stt_device("cuda")
        with mock.patch("talktomeclaude.listen.run_listen") as run:
            result = self.runner.invoke(cli.main, ["listen", "--once", "--device", "cpu"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(run.call_args.kwargs["device"], "cpu")


class ConfigCliTests(_CliHarness):
    def test_stt_device_set_get_and_guard(self) -> None:
        result = self.runner.invoke(cli.main, ["config", "get", "stt-device"])
        self.assertEqual(result.output.strip(), "auto")
        result = self.runner.invoke(cli.main, ["config", "set", "stt-device", "cuda"])
        self.assertEqual(result.exit_code, 0, result.output)
        result = self.runner.invoke(cli.main, ["config", "get", "stt-device"])
        self.assertEqual(result.output.strip(), "cuda")
        result = self.runner.invoke(cli.main, ["config", "set", "stt-device", "tpu"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(config.stt_device(), "cuda")

    def test_namespace_policy_and_allowlist_round_trip(self) -> None:
        result = self.runner.invoke(cli.main, ["config", "get", "command-namespace-policy"])
        self.assertEqual(result.output.strip(), "ask-first-use")
        result = self.runner.invoke(
            cli.main, ["config", "set", "command-namespace-policy", "allowlist"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        result = self.runner.invoke(
            cli.main, ["config", "set", "command-namespace-allowlist", "kiln, gsd"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        result = self.runner.invoke(cli.main, ["config", "get", "command-namespace-allowlist"])
        self.assertEqual(result.output.strip(), "kiln, gsd")
        result = self.runner.invoke(
            cli.main, ["config", "set", "command-namespace-allowlist", "none"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        result = self.runner.invoke(cli.main, ["config", "get", "command-namespace-allowlist"])
        self.assertEqual(result.output.strip(), "none")
        result = self.runner.invoke(
            cli.main, ["config", "set", "command-namespace-policy", "deny-all"]
        )
        self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
