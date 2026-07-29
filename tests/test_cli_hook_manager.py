from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from talktomeclaude.cli import main


class HookManagerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Path(self.temporary.name) / "settings.json"
        self.runner = CliRunner()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_install_is_idempotent_and_preserves_unrelated_hooks(self) -> None:
        self.settings.write_text(
            json.dumps(
                {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}
            ),
            encoding="utf-8",
        )
        for _attempt in range(2):
            result = self.runner.invoke(
                main, ["hook", "install", "--settings", str(self.settings)]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("installed", result.output)
        wire = self.settings.read_text(encoding="utf-8")
        self.assertEqual(wire.count("talktomeclaude.windows-companion.v1"), 1)
        document = json.loads(wire)
        commands = [
            command["command"]
            for rule in document["hooks"]["Stop"]
            for command in rule["hooks"]
        ]
        self.assertIn("other", commands)

    def test_status_reports_absent_without_creating_settings(self) -> None:
        result = self.runner.invoke(
            main, ["hook", "status", "--settings", str(self.settings)]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, "absent\n")
        self.assertFalse(self.settings.exists())

    def test_stream_uses_the_console_scripts_interpreter(self) -> None:
        with mock.patch(
            "talktomeclaude.reply.remote.main", return_value=0
        ) as stream:
            result = self.runner.invoke(main, ["hook", "stream"])

        self.assertEqual(result.exit_code, 0, result.output)
        stream.assert_called_once_with(["stream"])

    def test_codex_stream_uses_the_isolated_provider_spool(self) -> None:
        config_root = Path(self.temporary.name) / "config"
        with mock.patch(
            "talktomeclaude.reply.remote.main", return_value=0
        ) as stream:
            result = self.runner.invoke(
                main,
                ["hook", "stream", "--provider", "codex"],
                env={"TALKTOMECLAUDE_CONFIG_DIR": str(config_root)},
            )

        self.assertEqual(result.exit_code, 0, result.output)
        stream.assert_called_once_with(
            [
                "stream",
                "--spool-root",
                str(config_root / "reply-spool-codex"),
            ]
        )


if __name__ == "__main__":
    unittest.main()
