from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from talktomeclaude.assistant import (
    SessionAttachment,
    SessionAttachmentRegistry,
)


class SessionAttachmentRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "attachment.json"
        self.now = 100.0
        self.registry = SessionAttachmentRegistry(
            self.path,
            clock=lambda: self.now,
            live_lease_ttl_seconds=5.0,
        )

    def test_attach_replace_and_provider_scoped_detach(self) -> None:
        first = self.registry.open_live_lease("claude", pid=101)
        first_attachment = first.attach("session-1")

        self.assertEqual(
            first_attachment,
            SessionAttachment("claude", "session-1", first.lease_id),
        )
        self.assertEqual(self.registry.attachment(), first_attachment)
        self.assertTrue(self.registry.active("claude", "session-1", first.lease_id))

        second = self.registry.open_live_lease("codex", pid=202)
        second_attachment = second.attach("session-2")

        self.assertEqual(self.registry.attachment(), second_attachment)
        self.assertFalse(self.registry.active("claude", "session-1", first.lease_id))
        self.assertTrue(self.registry.active("codex", "session-2", second.lease_id))
        self.assertEqual(
            self.registry.live_leases(),
            {
                "claude": self.registry.live_lease("claude"),
                "codex": self.registry.live_lease("codex"),
            },
        )
        self.assertFalse(self.registry.detach("claude", "session-1"))
        self.assertTrue(self.registry.detach("codex", "session-2"))
        self.assertIsNone(self.registry.attachment())

    def test_active_requires_exact_provider_session_and_current_live_lease(self) -> None:
        lease = self.registry.open_live_lease("claude", pid=303)
        lease.attach("session-1")

        self.assertTrue(self.registry.active("claude", "session-1", lease.lease_id))
        self.assertFalse(self.registry.active("codex", "session-1", lease.lease_id))
        self.assertFalse(self.registry.active("claude", "session-2", lease.lease_id))
        self.assertFalse(self.registry.active("claude", "session-1", "lease-other"))
        self.assertFalse(self.registry.detach("codex", "session-1"))

    def test_independent_provider_leases_survive_open_refresh_and_close(self) -> None:
        claude = self.registry.open_live_lease("claude", pid=404)
        codex = self.registry.open_live_lease("codex", pid=505)
        codex.attach("session-2")

        before = self.registry.live_lease("claude")
        assert before is not None
        self.now += 1.0
        refreshed = claude.heartbeat()
        assert refreshed is not None

        self.assertGreater(refreshed.heartbeat_at, before.heartbeat_at)
        self.assertIsNotNone(self.registry.live_lease("codex"))
        self.assertTrue(claude.close())
        self.assertIsNone(self.registry.live_lease("claude"))
        self.assertIsNotNone(self.registry.live_lease("codex"))
        self.assertTrue(self.registry.active("codex", "session-2", codex.lease_id))

    def test_stale_and_malformed_live_lease_isolated_per_provider(self) -> None:
        claude = self.registry.open_live_lease("claude", pid=404)
        claude.attach("session-1")
        codex = self.registry.open_live_lease("codex", pid=505)

        self.now += 5.0
        self.assertIsNotNone(self.registry.live_lease("claude"))
        self.assertIsNotNone(self.registry.live_lease("codex"))
        self.now += 0.01
        self.assertIsNone(self.registry.live_lease("claude"))
        self.assertIsNone(self.registry.live_lease("codex"))
        self.assertFalse(self.registry.active("claude", "session-1", claude.lease_id))

        self.path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "attachment": {
                        "provider": "claude",
                        "session_id": "session-1",
                        "lease_id": claude.lease_id,
                    },
                    "live_leases": {
                        "claude": {
                            "provider": "claude",
                            "lease_id": claude.lease_id,
                            "pid": 404,
                            "heartbeat_at": self.now,
                        },
                        "codex": {
                            "provider": "codex",
                            "lease_id": "bad\nlease",
                            "pid": 505,
                            "heartbeat_at": self.now,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        self.assertIsNotNone(self.registry.live_lease("claude"))
        self.assertIsNone(self.registry.live_lease("codex"))
        self.assertTrue(self.registry.active("claude", "session-1", claude.lease_id))
        self.assertFalse(self.registry.active("codex", "session-1", codex.lease_id))

    def test_close_is_owner_safe_and_idempotent(self) -> None:
        first = self.registry.open_live_lease("claude", pid=505)
        first.attach("session-1")
        other_provider = self.registry.open_live_lease("codex", pid=515)
        second = self.registry.open_live_lease("claude", pid=606)
        second.attach("session-2")

        self.assertFalse(first.close())
        live = self.registry.live_lease("claude")
        assert live is not None
        self.assertEqual(live.lease_id, second.lease_id)
        self.assertEqual(self.registry.live_lease("codex"), self.registry.live_leases()["codex"])

        self.assertTrue(second.close())
        self.assertFalse(second.close())
        self.assertIsNone(self.registry.live_lease("claude"))
        self.assertIsNotNone(self.registry.live_lease("codex"))
        self.assertFalse(self.registry.active("claude", "session-2", second.lease_id))
        self.assertFalse(self.registry.active("codex", "session-2", other_provider.lease_id))

    def test_invalid_inputs_and_state_fail_closed_without_content_leakage(self) -> None:
        secret = "SECRET-\N{WAVING HAND SIGN}"
        lease = self.registry.open_live_lease("claude", pid=707)

        with self.assertRaisesRegex(ValueError, "session_id is invalid") as raised:
            lease.attach(f"bad\n{secret}")
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, repr(raised.exception))

        self.path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "attachment": {
                        "provider": "claude",
                        "session_id": "session-1",
                        "lease_id": "lease-one",
                    },
                    "live_leases": {
                        "claude": {
                            "provider": "claude",
                            "lease_id": f"bad\n{secret}",
                            "pid": 707,
                            "heartbeat_at": self.now,
                        },
                        "codex": {
                            "provider": "codex",
                            "lease_id": "lease-safe",
                            "pid": 808,
                            "heartbeat_at": self.now,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        self.assertIsNone(self.registry.live_lease("claude"))
        self.assertIsNotNone(self.registry.live_lease("codex"))
        self.assertFalse(self.registry.active("claude", "session-1", "lease-one"))

    def test_legacy_v1_single_lease_is_migrated_on_write(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "attachment": {
                        "provider": "claude",
                        "session_id": "session-1",
                        "lease_id": "lease-old",
                    },
                    "live_lease": {
                        "provider": "claude",
                        "lease_id": "lease-old",
                        "pid": 111,
                        "heartbeat_at": self.now,
                    },
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            self.registry.live_lease("claude"),
            self.registry.live_leases()["claude"],
        )
        self.assertTrue(self.registry.active("claude", "session-1", "lease-old"))

        codex = self.registry.open_live_lease("codex", pid=222)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["version"], 2)
        self.assertIn("live_leases", document)
        self.assertEqual(
            set(document["live_leases"]),
            {"claude", "codex"},
        )
        self.assertEqual(document["live_leases"]["codex"]["lease_id"], codex.lease_id)

    def test_concurrent_identical_attach_and_heartbeat_are_idempotent(self) -> None:
        lease = self.registry.open_live_lease("codex", pid=808)
        self.registry.open_live_lease("claude", pid=909)
        barrier = threading.Barrier(9)
        failures: list[BaseException] = []

        def attach_worker() -> None:
            try:
                barrier.wait()
                self.registry.attach("codex", "session-9", lease.lease_id)
            except BaseException as exc:
                failures.append(exc)

        def heartbeat_worker() -> None:
            try:
                barrier.wait()
                lease.heartbeat()
            except BaseException as exc:
                failures.append(exc)

        threads = [threading.Thread(target=attach_worker) for _ in range(4)] + [
            threading.Thread(target=heartbeat_worker) for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)

        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(
            self.registry.attachment(),
            SessionAttachment("codex", "session-9", lease.lease_id),
        )
        live = self.registry.live_lease("codex")
        assert live is not None
        self.assertEqual(live.provider, "codex")
        self.assertEqual(live.lease_id, lease.lease_id)
        self.assertIsNotNone(self.registry.live_lease("claude"))


if __name__ == "__main__":
    unittest.main()
