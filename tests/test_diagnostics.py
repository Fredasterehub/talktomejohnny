from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from talktomeclaude.diagnostics import (
    REDACTED,
    DiagnosticStore,
    opaque_identity,
    redact,
)


class DiagnosticStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_records_only_content_safe_state_and_hashes_identity(self) -> None:
        store = DiagnosticStore(
            self.root / "metrics.json", monotonic=lambda: 12.5
        )
        self.assertTrue(
            store.record(
                "state_transition",
                previous="idle",
                current="recording",
                event_hash=opaque_identity("event-one"),
                queue_depth=2,
            )
        )

        state, recovered = store.snapshot()
        self.assertFalse(recovered)
        self.assertEqual(state["events"][0]["monotonic_seconds"], 12.5)
        self.assertEqual(state["events"][0]["fields"]["current"], "recording")
        self.assertEqual(len(state["events"][0]["fields"]["event_hash"]), 64)

    def test_defensive_redactor_catches_content_token_paths_and_ssh_options(self) -> None:
        synthetic = {
            "transcript": "private words",
            "answer": "private answer",
            "nested": {
                "note": "Bearer secret-value",
                "path": r"C:\Users\example\project",
                "forward_path": "C:/Users/example/project",
                "ssh": "-o IdentityFile=C:/secret/key",
                "voice": "reference_path=C:/voices/rick.wav",
            },
        }
        safe = redact(synthetic)
        self.assertEqual(safe["transcript"], REDACTED)
        self.assertEqual(safe["answer"], REDACTED)
        for value in safe["nested"].values():
            self.assertTrue(str(value).startswith(REDACTED))

    def test_event_kind_is_schema_restricted_and_cannot_carry_content(self) -> None:
        store = DiagnosticStore(self.root / "metrics.json")
        for unsafe in (
            "transcript private words",
            "C:/Users/example/private",
            "state-transition",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                store.record(unsafe, current="idle")

        self.assertTrue(store.record("state_transition", current="idle"))

    def test_export_manifest_names_included_and_omitted_fields(self) -> None:
        store = DiagnosticStore(self.root / "metrics.json")
        store.record("capability", capability="hotkey", available=True)
        output = store.export(self.root / "support.json")
        document = json.loads(output.read_text(encoding="utf-8"))
        manifest = document["manifest"]
        self.assertIn("semantic state transitions", manifest["included"])
        self.assertIn("audio and transcripts", manifest["omitted"])
        self.assertFalse(manifest["partial_store_recovered"])

    def test_export_survives_partial_metric_store_corruption(self) -> None:
        path = self.root / "metrics.json"
        path.write_text('{"version":1,"events":', encoding="utf-8")
        store = DiagnosticStore(path)
        output = store.export(self.root / "support.json")
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["diagnostics"], [])
        self.assertTrue(document["manifest"]["partial_store_recovered"])

    def test_store_is_bounded(self) -> None:
        store = DiagnosticStore(self.root / "metrics.json", maximum_events=2)
        for index in range(3):
            self.assertTrue(store.record("retry", retry_count=index))
        state, _recovered = store.snapshot()
        self.assertEqual([event["sequence"] for event in state["events"]], [2, 3])


if __name__ == "__main__":
    unittest.main()
