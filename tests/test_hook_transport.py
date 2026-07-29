"""Composition tests for authoritative Stop events entering the durable spool."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from talktomeclaude.assistant import (
    OWNED_HOOK_MARKER,
    AssistantEventCode,
    SessionAttachmentRegistry,
)
from talktomeclaude.assistant.suppression import (
    CORRELATION_ENV,
    DIRECTOR_ROLE,
    ROLE_ENV,
)
from talktomeclaude.cli import main
from talktomeclaude.hook import transport_fault_status, transport_stop_event
from talktomeclaude.reply import ReplySpool


class HookTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "spool"
        self.environment = {
            "TALKTOMECLAUDE_REPLY_SPOOL": str(self.root),
            "TALKTOMECLAUDE_CONFIG_DIR": self.temp.name,
            "TALKTOMEJOHNNY_ATTACHMENT_STATE": str(
                Path(self.temp.name) / "attachment.json"
            ),
        }
        registry = SessionAttachmentRegistry(
            self.environment["TALKTOMEJOHNNY_ATTACHMENT_STATE"]
        )
        self.lease = registry.open_live_lease("claude", pid=101)
        self.lease.attach("session-1")
        self.addCleanup(self.lease.close)

    @staticmethod
    def event(answer: str = "Café 世界 👋\nsecond line") -> dict[str, object]:
        return {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "last_assistant_message": answer,
            "transcript_path": "must-not-be-read.jsonl",
        }

    def test_exact_unicode_answer_is_spooled_once_without_speakable_filtering(self) -> None:
        result = transport_stop_event(
            self.event(),
            environment=self.environment,
            event_id_factory=lambda: "event-1",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.code, AssistantEventCode.ACCEPTED)
        pending = ReplySpool(self.root).pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].event.answer, "Café 世界 👋\nsecond line")
        self.assertNotIn("transcript_path", pending[0].wire_bytes.decode("utf-8"))

    def test_non_stop_or_missing_authoritative_fields_never_create_spool(self) -> None:
        cases = [
            {},
            {**self.event(), "hook_event_name": "SubagentStop"},
            {**self.event(), "session_id": None},
            {**self.event(), "last_assistant_message": ""},
        ]
        for index, event in enumerate(cases):
            with self.subTest(index=index):
                self.assertIsNone(
                    transport_stop_event(event, environment=self.environment)
                )
        self.assertFalse(self.root.exists())

    def test_director_role_is_suppressed_before_spool_publication(self) -> None:
        environment = {
            **self.environment,
            ROLE_ENV: DIRECTOR_ROLE,
            CORRELATION_ENV: "director-1",
        }

        result = transport_stop_event(
            self.event(),
            environment=environment,
            event_id_factory=lambda: "event-director",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.code, AssistantEventCode.SUPPRESSED_ROLE)
        self.assertFalse(self.root.exists())

    def test_hidden_cli_transport_requires_exact_owner_and_never_speaks(self) -> None:
        payload = json.dumps(self.event(), ensure_ascii=False)
        runner = CliRunner()
        environment = {**os.environ, **self.environment}
        with mock.patch("talktomeclaude.cli.synthesize") as synth:
            rejected = runner.invoke(
                main,
                ["hook", "stop", "--transport", "--owner-marker", "other"],
                input=payload,
                env=environment,
            )
            accepted = runner.invoke(
                main,
                [
                    "hook",
                    "stop",
                    "--transport",
                    "--owner-marker",
                    OWNED_HOOK_MARKER,
                ],
                input=payload,
                env=environment,
            )

        self.assertEqual(rejected.exit_code, 0, rejected.output)
        self.assertEqual(accepted.exit_code, 0, accepted.output)
        self.assertEqual(rejected.output + accepted.output, "")
        synth.assert_not_called()
        self.assertEqual(len(ReplySpool(self.root).pending()), 1)

    def test_hidden_cli_transport_ignores_an_unattached_claude_session(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "hook",
                "stop",
                "--transport",
                "--owner-marker",
                OWNED_HOOK_MARKER,
            ],
            input=json.dumps(self.event() | {"session_id": "other-session"}),
            env={**os.environ, **self.environment},
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(ReplySpool(self.root).pending(), ())

    def test_transport_storage_failure_still_exits_zero_without_content(self) -> None:
        runner = CliRunner()
        environment = {**os.environ, **self.environment}
        with mock.patch(
            "talktomeclaude.hook.transport_stop_event",
            side_effect=OSError("synthetic storage failure"),
        ):
            result = runner.invoke(
                main,
                [
                    "hook",
                    "stop",
                    "--transport",
                    "--owner-marker",
                    OWNED_HOOK_MARKER,
                ],
                input=json.dumps(self.event()),
                env=environment,
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, "")
        status = transport_fault_status(environment=environment)
        self.assertEqual(status["version"], 1)
        self.assertEqual(status["failure_count"], 1)
        self.assertEqual(status["last_code"], "exception_OSError")
        self.assertNotIn("synthetic storage failure", repr(status))

    def test_publish_failure_is_persisted_without_answer_content(self) -> None:
        runner = CliRunner()
        environment = {**os.environ, **self.environment}
        with mock.patch(
            "talktomeclaude.reply.ReplySpool.enqueue",
            side_effect=OSError("SECRET-ANSWER"),
        ):
            result = runner.invoke(
                main,
                [
                    "hook",
                    "stop",
                    "--transport",
                    "--owner-marker",
                    OWNED_HOOK_MARKER,
                ],
                input=json.dumps(self.event("SECRET-ANSWER")),
                env=environment,
            )

        self.assertEqual(result.exit_code, 0, result.output)
        status = transport_fault_status(environment=environment)
        self.assertEqual(status["last_code"], "publish_failed")
        self.assertNotIn("SECRET-ANSWER", repr(status))


if __name__ == "__main__":
    unittest.main()
