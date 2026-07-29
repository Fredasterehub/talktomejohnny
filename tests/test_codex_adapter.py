from __future__ import annotations

import base64
import re
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from talktomeclaude.assistant import (
    CODEX_OWNED_HOOK_MARKER,
    SessionAttachmentRegistry,
)

from talktomeclaude.assistant.codex import (
    CodexStopErrorCode,
    CodexStopPayloadError,
    CodexStopTransportError,
    codex_stop_event_id,
    translate_stop_event,
    transport_stop_event,
)
from talktomeclaude.reply import ReplyEvent, ReplySpool
from talktomeclaude.cli import main


def payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "hook_event_name": "Stop",
        "session_id": "session-1",
        "turn_id": "turn-7",
        "last_assistant_message": "Cafe\u0301 \u2615 \U0001f44b\nsecond line",
    }
    value.update(changes)
    return value


class CodexStopAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.spool_root = Path(self.temporary.name) / "spool"

    def test_event_id_is_deterministic_and_reply_safe(self) -> None:
        first = codex_stop_event_id("session-1", "turn-7", "answer")
        second = codex_stop_event_id("session-1", "turn-7", "answer")
        changed = codex_stop_event_id("session-1", "turn-8", "answer")

        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertRegex(first, re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"))

    def test_translate_preserves_exact_unicode_and_canonical_digest(self) -> None:
        event = translate_stop_event(payload())

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.answer, "Cafe\u0301 \u2615 \U0001f44b\nsecond line")
        self.assertEqual(
            event.event_id,
            codex_stop_event_id(
                "session-1",
                "turn-7",
                "Cafe\u0301 \u2615 \U0001f44b\nsecond line",
            ),
        )
        self.assertEqual(event, ReplyEvent.from_bytes(event.to_bytes()))

    def test_transport_spools_idempotently_for_repeated_same_turn(self) -> None:
        first = transport_stop_event(payload(), spool_root=self.spool_root)
        second = transport_stop_event(payload(), spool_root=self.spool_root)

        pending = ReplySpool(self.spool_root).pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(first.event, second.event)
        self.assertEqual(pending[0].event.answer, "Cafe\u0301 \u2615 \U0001f44b\nsecond line")

    def test_repeated_stop_after_continuation_retains_the_later_answer(self) -> None:
        transport_stop_event(
            payload(last_assistant_message="first stop"),
            spool_root=self.spool_root,
        )
        transport_stop_event(
            payload(
                stop_hook_active=True,
                last_assistant_message="later final stop",
            ),
            spool_root=self.spool_root,
        )

        pending = ReplySpool(self.spool_root).pending()
        self.assertEqual(len(pending), 2)
        self.assertEqual(
            [record.event.answer for record in pending],
            ["first stop", "later final stop"],
        )

    def test_null_authoritative_message_is_a_clean_noop(self) -> None:
        result = transport_stop_event(
            payload(last_assistant_message=None),
            spool_root=self.spool_root,
        )

        self.assertIsNone(result)
        self.assertFalse(self.spool_root.exists())

    def test_invalid_payloads_are_rejected_without_answer_leakage(self) -> None:
        secret = "SECRET-\U0001f44b"
        cases = [
            (CodexStopErrorCode.INVALID_ROOT, []),
            (
                CodexStopErrorCode.INVALID_EVENT_NAME,
                payload(hook_event_name="PostToolUse", last_assistant_message=secret),
            ),
            (
                CodexStopErrorCode.INVALID_SESSION,
                payload(session_id="bad\nsession", last_assistant_message=secret),
            ),
            (
                CodexStopErrorCode.INVALID_TURN_ID,
                payload(turn_id=True, last_assistant_message=secret),
            ),
            (
                CodexStopErrorCode.INVALID_ANSWER,
                payload(last_assistant_message=""),
            ),
        ]

        for expected, raw in cases:
            with self.subTest(expected=expected, raw=raw):
                with self.assertRaises(CodexStopPayloadError) as raised:
                    translate_stop_event(raw)  # type: ignore[arg-type]
                self.assertEqual(raised.exception.code, expected)
                self.assertNotIn(secret, repr(raised.exception))
                self.assertNotIn(secret, str(raised.exception))

        self.assertFalse(self.spool_root.exists())

    def test_transport_failure_raises_safe_error(self) -> None:
        with mock.patch(
            "talktomeclaude.assistant.codex.ReplySpool.enqueue",
            side_effect=OSError("SECRET-ANSWER"),
        ):
            with self.assertRaises(CodexStopTransportError) as raised:
                transport_stop_event(
                    payload(last_assistant_message="SECRET-ANSWER"),
                    spool_root=self.spool_root,
                )

        self.assertEqual(raised.exception.code, CodexStopErrorCode.TRANSPORT_FAILED)
        self.assertNotIn("SECRET-ANSWER", repr(raised.exception))
        self.assertNotIn("SECRET-ANSWER", str(raised.exception))


class CodexCliIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runner = CliRunner()

    def test_assistant_provider_config_cli_round_trips(self) -> None:
        environment = {
            **os.environ,
            "TALKTOMECLAUDE_CONFIG_DIR": str(self.root / "config"),
        }

        initial = self.runner.invoke(
            main, ["config", "get", "assistant-provider"], env=environment
        )
        changed = self.runner.invoke(
            main,
            ["config", "set", "assistant-provider", "codex"],
            env=environment,
        )
        current = self.runner.invoke(
            main, ["config", "get", "assistant-provider"], env=environment
        )

        self.assertEqual(initial.exit_code, 0, initial.output)
        self.assertEqual(initial.output.strip(), "both")
        self.assertEqual(changed.exit_code, 0, changed.output)
        self.assertEqual(current.exit_code, 0, current.output)
        self.assertEqual(current.output.strip(), "codex")

    def test_hook_install_uses_codex_hooks_json_additively(self) -> None:
        hooks = self.root / "hooks.json"
        hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "other"}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        result = self.runner.invoke(
            main,
            ["hook", "install", "--provider", "codex", "--settings", str(hooks)],
            env={
                **os.environ,
                "HOME": str(self.root / "home"),
                "USERPROFILE": str(self.root / "home"),
                "TALKTOMEJOHNNY_HOOK_EXECUTABLE": str(
                    (self.root / "bin" / "talktomejohnny").resolve()
                ),
            },
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("open /hooks in Codex", result.output)
        document = json.loads(hooks.read_text(encoding="utf-8"))
        self.assertEqual(document["hooks"]["Stop"][0]["hooks"][0]["command"], "other")
        command = document["hooks"]["Stop"][1]["hooks"][0]["command"]
        self.assertTrue(command.startswith("powershell.exe -NoProfile"))
        decoded = base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16-le")
        self.assertIn("'hook' 'stop' '--transport' '--provider' 'codex'", decoded)
        self.assertIn(CODEX_OWNED_HOOK_MARKER, decoded)
        self.assertEqual(len(document["hooks"]["UserPromptSubmit"]), 1)
        self.assertEqual(len(document["hooks"]["SessionEnd"]), 1)
        self.assertTrue(
            (self.root / "home" / ".agents" / "skills" / "talktomejohnny" / "SKILL.md").exists()
        )

    def test_transport_accepts_an_exact_attached_codex_session_without_wrapper(
        self,
    ) -> None:
        spool = self.root / "spool"
        attachment = self.root / "attachment.json"
        registry = SessionAttachmentRegistry(attachment)
        lease = registry.open_live_lease("codex", pid=303)
        self.addCleanup(lease.close)
        lease.attach("session-1")
        event = json.dumps(payload(), ensure_ascii=False)
        environment = {
            **os.environ,
            "TALKTOMECLAUDE_REPLY_SPOOL": str(spool),
            "TALKTOMECLAUDE_CONFIG_DIR": str(self.root),
            "TALKTOMEJOHNNY_ATTACHMENT_STATE": str(attachment),
        }

        result = self.runner.invoke(
            main,
            [
                "hook",
                "stop",
                "--transport",
                "--provider",
                "codex",
                "--owner-marker",
                CODEX_OWNED_HOOK_MARKER,
            ],
            input=event,
            env=environment,
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, "")
        self.assertEqual(len(ReplySpool(spool).pending()), 1)

    def test_transport_keeps_wrapper_activation_backward_compatible(self) -> None:
        spool = self.root / "spool"
        event = json.dumps(payload(), ensure_ascii=False)
        environment = {
            **os.environ,
            "TALKTOMECLAUDE_REPLY_SPOOL": str(spool),
            "TALKTOMECLAUDE_CONFIG_DIR": str(self.root),
        }
        command = [
            "hook",
            "stop",
            "--transport",
            "--provider",
            "codex",
            "--owner-marker",
            CODEX_OWNED_HOOK_MARKER,
        ]

        inactive = self.runner.invoke(main, command, input=event, env=environment)
        active = self.runner.invoke(
            main,
            command,
            input=event,
            env={**environment, "TALKTOMECLAUDE_CODEX_ACTIVE": "1"},
        )

        self.assertEqual(inactive.exit_code, 0, inactive.output)
        self.assertEqual(active.exit_code, 0, active.output)
        self.assertEqual(inactive.output + active.output, "")
        self.assertEqual(len(ReplySpool(spool).pending()), 1)

    def test_null_stop_message_is_ignored_without_fault_diagnostics(self) -> None:
        spool = self.root / "spool"
        environment = {
            **os.environ,
            "TALKTOMECLAUDE_CODEX_ACTIVE": "1",
            "TALKTOMECLAUDE_REPLY_SPOOL": str(spool),
            "TALKTOMECLAUDE_CONFIG_DIR": str(self.root),
        }

        result = self.runner.invoke(
            main,
            [
                "hook",
                "stop",
                "--transport",
                "--provider",
                "codex",
                "--owner-marker",
                CODEX_OWNED_HOOK_MARKER,
            ],
            input=json.dumps(payload(last_assistant_message=None)),
            env=environment,
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, "")
        self.assertFalse(spool.exists())
        self.assertFalse((self.root / "hook-transport-status.json").exists())

    def test_codex_launcher_scopes_activation_to_child_environment(self) -> None:
        completed = mock.Mock(returncode=7)
        with mock.patch("shutil.which", return_value="codex-path"), mock.patch(
            "subprocess.run", return_value=completed
        ) as run, mock.patch.dict(
            os.environ,
            {"TALKTOMECLAUDE_CODEX_ACTIVE": "parent-value"},
            clear=False,
        ):
            result = self.runner.invoke(main, ["codex", "--model", "gpt-test"])
            self.assertEqual(
                os.environ["TALKTOMECLAUDE_CODEX_ACTIVE"], "parent-value"
            )

        self.assertEqual(result.exit_code, 7, result.output)
        self.assertEqual(
            run.call_args.args[0], ["codex-path", "--model", "gpt-test"]
        )
        self.assertEqual(
            run.call_args.kwargs["env"]["TALKTOMECLAUDE_CODEX_ACTIVE"], "1"
        )

    def test_codex_launcher_passes_help_through_to_codex(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch("shutil.which", return_value="codex-path"), mock.patch(
            "subprocess.run", return_value=completed
        ) as run:
            result = self.runner.invoke(main, ["codex", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(run.call_args.args[0], ["codex-path", "--help"])

    def test_codex_launcher_reports_missing_cli(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            result = self.runner.invoke(main, ["codex"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Codex CLI was not found on PATH", result.output)


if __name__ == "__main__":
    unittest.main()
