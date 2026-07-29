from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from talktomeclaude.cli import main


def _hook_scripts(document: dict[str, object]) -> tuple[str, ...]:
    scripts: list[str] = []
    for groups in document.get("hooks", {}).values():
        for group in groups:
            for hook in group["hooks"]:
                command = hook["command"]
                if " -EncodedCommand " in command:
                    command = base64.b64decode(command.rsplit(" ", 1)[1]).decode(
                        "utf-16-le"
                    )
                scripts.append(command)
    return tuple(scripts)


class HookManagerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Path(self.temporary.name) / "settings.json"
        self.home = Path(self.temporary.name) / "home"
        self.runner = CliRunner()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "TALKTOMEJOHNNY_HOOK_EXECUTABLE": str(
                (self.home / "bin" / "talktomejohnny").resolve()
            ),
            **extra,
        }

    def test_install_is_idempotent_and_preserves_unrelated_hooks(self) -> None:
        self.settings.write_text(
            json.dumps(
                {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}
            ),
            encoding="utf-8",
        )
        for _attempt in range(2):
            result = self.runner.invoke(
                main,
                ["hook", "install", "--settings", str(self.settings)],
                env=self.env(),
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("installed", result.output)
        wire = self.settings.read_text(encoding="utf-8")
        document = json.loads(wire)
        scripts = _hook_scripts(document)
        self.assertEqual(
            sum("talktomeclaude.windows-companion.v1" in item for item in scripts),
            1,
        )
        self.assertEqual(
            sum(
                "talktomejohnny.session-control.claude.v1" in item
                for item in scripts
            ),
            2,
        )
        self.assertEqual(
            document["hooks"]["Stop"][0]["hooks"][0]["command"], "other"
        )
        self.assertEqual(len(document["hooks"]["UserPromptExpansion"]), 1)
        self.assertEqual(len(document["hooks"]["SessionEnd"]), 1)
        self.assertTrue(
            (self.home / ".claude" / "skills" / "talktomejohnny" / "SKILL.md").exists()
        )

    def test_status_reports_absent_without_creating_settings(self) -> None:
        result = self.runner.invoke(
            main,
            ["hook", "status", "--settings", str(self.settings)],
            env=self.env(),
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, "absent\n")
        self.assertFalse(self.settings.exists())

    def test_stream_uses_the_console_scripts_interpreter(self) -> None:
        config_root = Path(self.temporary.name) / "config"
        with mock.patch(
            "talktomeclaude.reply.remote.main", return_value=0
        ) as stream:
            result = self.runner.invoke(
                main,
                ["hook", "stream"],
                env={"TALKTOMECLAUDE_CONFIG_DIR": str(config_root)},
            )

        self.assertEqual(result.exit_code, 0, result.output)
        stream.assert_called_once_with(["stream"])
        state = json.loads(
            (config_root / "assistant-attachment.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("claude", state["live_leases"])

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

    def test_codex_install_adds_prompt_submit_and_session_end_hooks(self) -> None:
        result = self.runner.invoke(
            main,
            [
                "hook",
                "install",
                "--provider",
                "codex",
                "--settings",
                str(self.settings),
            ],
            env=self.env(),
        )

        self.assertEqual(result.exit_code, 0, result.output)
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(len(document["hooks"]["Stop"]), 1)
        self.assertEqual(len(document["hooks"]["UserPromptSubmit"]), 1)
        self.assertEqual(len(document["hooks"]["SessionEnd"]), 1)
        self.assertEqual(
            sum(
                "talktomejohnny.session-control.codex.v1" in item
                for item in _hook_scripts(document)
            ),
            2,
        )
        self.assertTrue(
            (self.home / ".agents" / "skills" / "talktomejohnny" / "SKILL.md").exists()
        )

    def test_session_command_blocks_only_the_exact_codex_control_prompt(self) -> None:
        from talktomeclaude.assistant import SessionAttachmentRegistry
        from talktomeclaude.assistant.hooks import CODEX_SESSION_CONTROL_MARKER

        state_path = Path(self.temporary.name) / "attachment.json"
        registry = SessionAttachmentRegistry(state_path)
        lease = registry.open_live_lease("codex", pid=202)
        environment = {"TALKTOMEJOHNNY_ATTACHMENT_STATE": str(state_path)}

        ordinary = self.runner.invoke(
            main,
            [
                "hook",
                "session",
                "--provider",
                "codex",
                "--owner-marker",
                CODEX_SESSION_CONTROL_MARKER,
            ],
            input=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "prompt": "ordinary prompt",
                }
            ),
            env=environment,
        )
        attached = self.runner.invoke(
            main,
            [
                "hook",
                "session",
                "--provider",
                "codex",
                "--owner-marker",
                CODEX_SESSION_CONTROL_MARKER,
            ],
            input=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "prompt": "$talktomejohnny on",
                }
            ),
            env=environment,
        )

        self.assertEqual(ordinary.exit_code, 0, ordinary.output)
        self.assertEqual(ordinary.output, "")
        self.assertEqual(attached.exit_code, 0, attached.output)
        self.assertEqual(json.loads(attached.output)["decision"], "block")
        self.assertTrue(registry.active("codex", "session-1", lease.lease_id))


if __name__ == "__main__":
    unittest.main()
