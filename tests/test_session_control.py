from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from talktomeclaude.assistant import SessionAttachmentRegistry
from talktomeclaude.assistant.session_control import (
    LiveLeaseHeartbeatOwner,
    attachment_state_path,
    handle_session_control_event,
    read_session_control_event,
    session_is_attached,
)


class SessionControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.now = 100.0
        self.registry = SessionAttachmentRegistry(
            Path(self.temporary.name) / "attachment.json",
            clock=lambda: self.now,
            live_lease_ttl_seconds=5.0,
        )

    def codex(self, prompt: str, session: str = "codex-session") -> dict[str, object]:
        return {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "prompt": prompt,
        }

    def claude(self, args: str, session: str = "claude-session") -> dict[str, object]:
        return {
            "hook_event_name": "UserPromptExpansion",
            "session_id": session,
            "command_name": "talktomejohnny",
            "command_args": args,
        }

    def test_codex_exact_command_attaches_statuses_and_detaches(self) -> None:
        lease = self.registry.open_live_lease("codex", pid=101)

        result = handle_session_control_event(
            self.codex("$talktomejohnny on"),
            provider="codex",
            registry=self.registry,
        )

        self.assertEqual(result["decision"], "block")
        self.assertIn("attached", result["reason"])
        self.assertTrue(
            session_is_attached(
                self.codex("ordinary prompt"),
                provider="codex",
                registry=self.registry,
            )
        )
        status = handle_session_control_event(
            self.codex("$talktomejohnny status"),
            provider="codex",
            registry=self.registry,
        )
        self.assertIn("is attached", status["reason"])
        off = handle_session_control_event(
            self.codex("$talktomejohnny off"),
            provider="codex",
            registry=self.registry,
        )
        self.assertIn("detached", off["reason"])
        self.assertFalse(
            self.registry.active("codex", "codex-session", lease.lease_id)
        )

    def test_claude_command_uses_expansion_metadata_not_prompt_text(self) -> None:
        self.registry.open_live_lease("claude", pid=202)
        event = self.claude("on")
        event["prompt"] = "/untrusted-other-command on"

        result = handle_session_control_event(
            event, provider="claude", registry=self.registry
        )

        self.assertIn("attached", result["reason"])
        self.assertTrue(
            session_is_attached(
                {"session_id": "claude-session"},
                provider="claude",
                registry=self.registry,
            )
        )

    def test_unrelated_prompts_and_commands_are_ignored(self) -> None:
        self.registry.open_live_lease("codex", pid=303)
        self.assertIsNone(
            handle_session_control_event(
                self.codex("please run $talktomejohnny on"),
                provider="codex",
                registry=self.registry,
            )
        )
        event = self.claude("on")
        event["command_name"] = "other-skill"
        self.assertIsNone(
            handle_session_control_event(
                event, provider="claude", registry=self.registry
            )
        )
        self.assertIsNone(self.registry.attachment())

    def test_attach_fails_closed_while_companion_is_offline_or_stale(self) -> None:
        offline = handle_session_control_event(
            self.codex("$talktomejohnny on"),
            provider="codex",
            registry=self.registry,
        )
        self.assertIn("offline", offline["reason"])
        self.assertIsNone(self.registry.attachment())

        self.registry.open_live_lease("codex", pid=404)
        self.now += 5.01
        stale = handle_session_control_event(
            self.codex("$talktomejohnny on"),
            provider="codex",
            registry=self.registry,
        )
        self.assertIn("offline", stale["reason"])
        self.assertIsNone(self.registry.attachment())

    def test_second_provider_attachment_replaces_first_without_cross_talk(self) -> None:
        self.registry.open_live_lease("claude", pid=505)
        self.registry.open_live_lease("codex", pid=606)
        handle_session_control_event(
            self.claude("on"), provider="claude", registry=self.registry
        )
        self.assertTrue(
            session_is_attached(
                {"session_id": "claude-session"},
                provider="claude",
                registry=self.registry,
            )
        )

        handle_session_control_event(
            self.codex("$talktomejohnny on"),
            provider="codex",
            registry=self.registry,
        )

        self.assertFalse(
            session_is_attached(
                {"session_id": "claude-session"},
                provider="claude",
                registry=self.registry,
            )
        )
        self.assertTrue(
            session_is_attached(
                {"session_id": "codex-session"},
                provider="codex",
                registry=self.registry,
            )
        )

    def test_session_end_only_detaches_the_exact_session(self) -> None:
        self.registry.open_live_lease("codex", pid=707)
        handle_session_control_event(
            self.codex("$talktomejohnny on"),
            provider="codex",
            registry=self.registry,
        )
        handle_session_control_event(
            {"hook_event_name": "SessionEnd", "session_id": "other"},
            provider="codex",
            registry=self.registry,
        )
        self.assertIsNotNone(self.registry.attachment())

        self.assertIsNone(
            handle_session_control_event(
                {"hook_event_name": "SessionEnd", "session_id": "codex-session"},
                provider="codex",
                registry=self.registry,
            )
        )
        self.assertIsNone(self.registry.attachment())

    def test_invalid_action_is_blocked_with_usage_and_legacy_alias_works(self) -> None:
        self.registry.open_live_lease("claude", pid=808)
        invalid = handle_session_control_event(
            self.claude("toggle"), provider="claude", registry=self.registry
        )
        self.assertIn("on, off, or status", invalid["reason"])
        legacy = self.claude("on")
        legacy["command_name"] = "talktomeclaude"
        self.assertIn(
            "attached",
            handle_session_control_event(
                legacy, provider="claude", registry=self.registry
            )["reason"],
        )

    def test_bounded_reader_and_state_override_are_content_safe(self) -> None:
        wire = json.dumps(self.codex("$talktomejohnny status"))
        self.assertEqual(
            read_session_control_event(io.StringIO(wire)), json.loads(wire)
        )
        self.assertIsNone(
            read_session_control_event(io.StringIO(wire), max_bytes=3)
        )
        override = Path(self.temporary.name) / "custom-state.json"
        self.assertEqual(
            attachment_state_path(
                {"TALKTOMEJOHNNY_ATTACHMENT_STATE": str(override)}
            ),
            override,
        )

    def test_heartbeat_owner_opens_and_owner_safely_closes_provider_lease(self) -> None:
        owner = LiveLeaseHeartbeatOwner(
            self.registry,
            "codex",
            interval_seconds=0.01,
        )

        owner.start()
        first = self.registry.live_lease("codex")
        owner.start()
        second = self.registry.live_lease("codex")

        self.assertIsNotNone(first)
        self.assertEqual(first.lease_id, second.lease_id)
        self.assertTrue(owner.stop())
        self.assertIsNone(self.registry.live_lease("codex"))
        self.assertTrue(owner.stop())


if __name__ == "__main__":
    unittest.main()
