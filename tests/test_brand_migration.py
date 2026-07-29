"""Regression tests for the TalkToMeJohnny state and cache migration layer."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from talktomeclaude import clone, config, stt, wizard
from talktomeclaude.speech import voices


class BrandMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_home = self.root / "config-home"
        self.cache_home = self.root / "cache-home"
        self.env = mock.patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_CACHE_HOME": str(self.cache_home),
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.legacy_root = self.config_home / "talktomeclaude"
        self.current_root = self.config_home / "talktomejohnny"

    def test_config_dir_defaults_to_talktomejohnny_and_new_override_wins(self) -> None:
        self.assertEqual(config.config_dir(), self.current_root)

        with mock.patch.dict(
            os.environ,
            {
                "TALKTOMECLAUDE_CONFIG_DIR": str(self.root / "legacy-override"),
                "TALKTOMEJOHNNY_CONFIG_DIR": str(self.root / "current-override"),
            },
            clear=False,
        ):
            self.assertEqual(config.config_dir(), self.root / "current-override")

        with mock.patch.dict(
            os.environ,
            {"TALKTOMECLAUDE_CONFIG_DIR": str(self.root / "legacy-only")},
            clear=False,
        ):
            self.assertEqual(config.config_dir(), self.root / "legacy-only")

    def test_migrate_legacy_state_copies_supported_files_without_ephemera(self) -> None:
        (self.legacy_root / "voice-refs").mkdir(parents=True)
        (self.legacy_root / "reply-spool-codex").mkdir()
        (self.legacy_root / "transcripts").mkdir()
        legacy_config = self.legacy_root / "config.json"
        legacy_registry = self.legacy_root / "voices.json"
        legacy_ref = self.legacy_root / "voice-refs" / "rick.wav"
        legacy_spool = self.legacy_root / "reply-spool-codex" / "event.json"
        legacy_transcript = self.legacy_root / "transcripts" / "turn.txt"
        legacy_config.write_text('{"default-voice":"rick"}', encoding="utf-8")
        legacy_registry.write_text('{"voices":{"rick":{"engine":"clone"}}}', encoding="utf-8")
        legacy_ref.write_bytes(b"RIFFlegacy")
        legacy_spool.write_text("queued", encoding="utf-8")
        legacy_transcript.write_text("hello", encoding="utf-8")

        self.assertTrue(config.migrate_legacy_state())
        self.assertEqual((self.current_root / "config.json").read_text(encoding="utf-8"), legacy_config.read_text(encoding="utf-8"))
        self.assertEqual((self.current_root / "voices.json").read_text(encoding="utf-8"), legacy_registry.read_text(encoding="utf-8"))
        self.assertEqual((self.current_root / "voice-refs" / "rick.wav").read_bytes(), b"RIFFlegacy")
        self.assertFalse((self.current_root / "reply-spool-codex").exists())
        self.assertFalse((self.current_root / "transcripts").exists())
        self.assertTrue(legacy_spool.exists())
        self.assertTrue(legacy_transcript.exists())

        marker = json.loads((self.current_root / ".legacy-state-migration.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["version"], 1)
        self.assertEqual(marker["legacy_product"], "talktomeclaude")
        self.assertEqual(marker["current_product"], "talktomejohnny")
        self.assertEqual(marker["copied"]["voice-refs"], 1)
        self.assertFalse(config.migrate_legacy_state())

    def test_migrate_legacy_state_preserves_existing_destination_files(self) -> None:
        (self.legacy_root / "voice-refs").mkdir(parents=True)
        self.current_root.mkdir(parents=True)
        (self.current_root / "voice-refs").mkdir()
        (self.legacy_root / "config.json").write_text('{"default-voice":"legacy"}', encoding="utf-8")
        (self.legacy_root / "voices.json").write_text('{"voices":{"rick":{}}}', encoding="utf-8")
        (self.legacy_root / "voice-refs" / "rick.wav").write_bytes(b"legacy")
        (self.current_root / "config.json").write_text('{"default-voice":"current"}', encoding="utf-8")
        (self.current_root / "voice-refs" / "rick.wav").write_bytes(b"current")

        self.assertTrue(config.migrate_legacy_state())
        self.assertEqual(
            (self.current_root / "config.json").read_text(encoding="utf-8"),
            '{"default-voice":"current"}',
        )
        self.assertEqual((self.current_root / "voice-refs" / "rick.wav").read_bytes(), b"current")
        self.assertEqual(
            (self.current_root / "voices.json").read_text(encoding="utf-8"),
            '{"voices":{"rick":{}}}',
        )

    def test_cache_dirs_prefer_new_paths_for_fresh_installs(self) -> None:
        self.assertEqual(clone.clone_cache_dir(), self.cache_home / "talktomejohnny" / "hf")
        self.assertEqual(stt.models_dir(), self.cache_home / "talktomejohnny" / "stt-models")
        self.assertEqual(voices.cache_voices_dir(), self.cache_home / "talktomejohnny" / "voices")
        self.assertEqual(wizard.samples_dir(), self.cache_home / "talktomejohnny" / "samples")

    def test_cache_dirs_reuse_legacy_paths_until_new_cache_exists(self) -> None:
        legacy_cache = self.cache_home / "talktomeclaude"
        (legacy_cache / "hf").mkdir(parents=True)
        (legacy_cache / "stt-models").mkdir()
        (legacy_cache / "voices").mkdir()
        (legacy_cache / "samples").mkdir()

        self.assertEqual(clone.clone_cache_dir(), legacy_cache / "hf")
        self.assertEqual(stt.models_dir(), legacy_cache / "stt-models")
        self.assertEqual(voices.cache_voices_dir(), legacy_cache / "voices")
        self.assertEqual(wizard.samples_dir(), legacy_cache / "samples")

        current_cache = self.cache_home / "talktomejohnny"
        (current_cache / "hf").mkdir(parents=True)
        (current_cache / "stt-models").mkdir()
        (current_cache / "voices").mkdir()
        (current_cache / "samples").mkdir()

        self.assertEqual(clone.clone_cache_dir(), current_cache / "hf")
        self.assertEqual(stt.models_dir(), current_cache / "stt-models")
        self.assertEqual(voices.cache_voices_dir(), current_cache / "voices")
        self.assertEqual(wizard.samples_dir(), current_cache / "samples")

    def test_new_env_overrides_win_for_cache_and_voice_paths(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "TALKTOMECLAUDE_CLONE_CACHE": str(self.root / "legacy-hf"),
                "TALKTOMEJOHNNY_CLONE_CACHE": str(self.root / "current-hf"),
                "TALKTOMECLAUDE_STT_MODELS_DIR": str(self.root / "legacy-stt"),
                "TALKTOMEJOHNNY_STT_MODELS_DIR": str(self.root / "current-stt"),
                "TALKTOMECLAUDE_VOICES_CACHE": str(self.root / "legacy-voices-cache"),
                "TALKTOMEJOHNNY_VOICES_CACHE": str(self.root / "current-voices-cache"),
                "TALKTOMECLAUDE_VOICES_DIR": str(self.root / "legacy-voices-dir"),
                "TALKTOMEJOHNNY_VOICES_DIR": str(self.root / "current-voices-dir"),
            },
            clear=False,
        ):
            self.assertEqual(clone.clone_cache_dir(), self.root / "current-hf")
            self.assertEqual(stt.models_dir(), self.root / "current-stt")
            self.assertEqual(voices.cache_voices_dir(), self.root / "current-voices-cache")
            self.assertEqual(voices.voices_dir(), self.root / "current-voices-dir")


if __name__ == "__main__":
    unittest.main()
