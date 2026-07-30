"""Cross-process and crash-safety tests for transactional configuration."""

from __future__ import annotations

import hashlib
import inspect
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from talktomeclaude import config
from talktomeclaude.storage import (
    AtomicJsonTransaction,
    ConfigMigrationError,
    ConfigStore,
    InvalidJsonError,
    LockTimeoutError,
    lock_identity_for_path,
)
from talktomeclaude.storage.config_store import SCHEMA_KEY
from talktomeclaude.storage.atomic import _posix_lock_path


def _increment_worker(path: str, worker: int, iterations: int) -> None:
    store = ConfigStore(path, timeout=30.0)
    own_key = f"worker-{worker}"
    for _ in range(iterations):
        def increment(settings: dict) -> None:
            settings[own_key] = settings.get(own_key, 0) + 1
            settings["total"] = settings.get("total", 0) + 1

        store.update(increment)


def _crash_worker(path: str, phase: str, marker: str | None = None) -> None:
    def crash(current_phase: str) -> None:
        if current_phase != phase:
            return
        if marker is not None:
            Path(marker).write_text("ready", encoding="ascii")
            time.sleep(0.4)
        os._exit(73)

    ConfigStore(path, timeout=10.0, phase_hook=crash).update(
        lambda settings: settings.__setitem__("value", "new")
    )


def _holding_worker(path: str, marker: str) -> None:
    def hold(phase: str) -> None:
        if phase == "before_mutex_release":
            Path(marker).write_text("ready", encoding="ascii")
            time.sleep(0.8)

    ConfigStore(path, timeout=10.0, phase_hook=hold).update(
        lambda settings: settings.__setitem__("holder", True)
    )


class AtomicStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.json"

    def test_canonical_equivalents_share_mutex_but_purposes_do_not(self) -> None:
        equivalent = self.root / "folder" / ".." / "config.json"
        self.assertEqual(
            lock_identity_for_path(self.path, purpose="config"),
            lock_identity_for_path(equivalent, purpose="config"),
        )
        identities = {
            lock_identity_for_path(self.path, purpose=purpose)
            for purpose in ("config", "registry", "cursor")
        }
        self.assertEqual(len(identities), 3)

    def test_posix_lock_path_is_stable_and_outside_config_directory(self) -> None:
        identity = lock_identity_for_path(self.path, purpose="config")
        first = _posix_lock_path(identity)
        second = _posix_lock_path(identity)
        self.assertEqual(first, second)
        self.assertNotEqual(first.parent, self.path.parent)
        self.assertNotIn(self.path.parent, first.parents)

    def test_eight_processes_complete_250_transactions_without_lost_updates(self) -> None:
        workers = 8
        iterations = 250
        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(
                target=_increment_worker,
                args=(str(self.path), worker, iterations),
            )
            for worker in range(workers)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(90)
            self.assertEqual(process.exitcode, 0)

        raw = self.path.read_bytes()
        saved = json.loads(raw.decode("utf-8"))
        self.assertEqual(saved["total"], workers * iterations)
        for worker in range(workers):
            self.assertEqual(saved[f"worker-{worker}"], iterations)

    def test_lock_timeout_leaves_observed_bytes_unchanged(self) -> None:
        ConfigStore(self.path).save({"seed": True})
        marker = self.root / "holding"
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_holding_worker, args=(str(self.path), str(marker))
        )
        process.start()
        self._wait_for(marker)
        observed = self.path.read_bytes()

        with self.assertRaises(LockTimeoutError):
            ConfigStore(self.path, timeout=0.03).update(
                lambda settings: settings.__setitem__("timed-out", True)
            )
        self.assertEqual(self.path.read_bytes(), observed)
        process.join(10)
        self.assertEqual(process.exitcode, 0)

    def test_crash_windows_publish_only_complete_old_or_new_json(self) -> None:
        context = multiprocessing.get_context("spawn")
        phases = (
            "before_temp_flush",
            "after_temp_flush",
            "before_replace",
            "after_replace",
            "before_mutex_release",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                ConfigStore(self.path).save({"value": "old"})
                process = context.Process(
                    target=_crash_worker, args=(str(self.path), phase)
                )
                process.start()
                process.join(10)
                self.assertEqual(process.exitcode, 73)
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                expected = "new" if phase in {"after_replace", "before_mutex_release"} else "old"
                self.assertEqual(saved["value"], expected)

    @unittest.skipUnless(os.name == "nt", "WAIT_ABANDONED is a Windows mutex result")
    def test_abandoned_mutex_is_recovered_and_emits_structured_code(self) -> None:
        ConfigStore(self.path).save({"value": "old"})
        marker = self.root / "crashing"
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_crash_worker,
            args=(str(self.path), "before_mutex_release", str(marker)),
        )
        process.start()
        self._wait_for(marker)
        codes: list[str] = []
        saved = ConfigStore(self.path, timeout=5.0, on_event=codes.append).load()
        process.join(10)

        self.assertEqual(process.exitcode, 73)
        self.assertEqual(saved["value"], "new")
        self.assertIn("storage_lock_abandoned_recovered", codes)

    def test_invalid_json_raises_without_replacing_bytes(self) -> None:
        original = b'{"broken":'
        self.path.write_bytes(original)
        store = ConfigStore(self.path)
        with self.assertRaises(InvalidJsonError):
            store.update(lambda settings: settings.__setitem__("x", 1))
        self.assertEqual(self.path.read_bytes(), original)

    def test_closed_temporary_handle_allows_atomic_replace(self) -> None:
        transaction = AtomicJsonTransaction(self.path)
        transaction.write({"first": 1})
        transaction.write({"second": 2})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"second": 2})

    def test_transient_windows_replace_denial_is_retried_without_losing_data(self) -> None:
        real_replace = os.replace
        attempts = 0

        def transient_replace(source: Path, destination: Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError(13, "transient scanner lock")
            real_replace(source, destination)

        with (
            mock.patch("talktomeclaude.storage.atomic._IS_WINDOWS", True),
            mock.patch(
                "talktomeclaude.storage.atomic.os.replace",
                side_effect=transient_replace,
            ),
        ):
            AtomicJsonTransaction(self.path).write({"saved": True})

        self.assertEqual(attempts, 3)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"saved": True})

    def test_cleanup_denial_does_not_mask_exhausted_replace_failure(self) -> None:
        replace_denied = PermissionError(13, "replace denied")
        cleanup_denied = PermissionError(13, "temp cleanup denied")

        with (
            mock.patch("talktomeclaude.storage.atomic._IS_WINDOWS", True),
            mock.patch(
                "talktomeclaude.storage.atomic.os.replace",
                side_effect=replace_denied,
            ),
            mock.patch(
                "talktomeclaude.storage.atomic.time.monotonic",
                side_effect=(0.0, 1.0),
            ),
            mock.patch("talktomeclaude.storage.atomic.time.sleep"),
            mock.patch.object(
                type(self.path),
                "unlink",
                side_effect=cleanup_denied,
            ),
        ):
            with self.assertRaisesRegex(PermissionError, "replace denied"):
                AtomicJsonTransaction(self.path).write({"saved": True})

    def test_missing_config_read_does_not_create_or_version_a_file(self) -> None:
        self.assertEqual(ConfigStore(self.path).load(), {})
        self.assertFalse(self.path.exists())

    @staticmethod
    def _wait_for(path: Path) -> None:
        deadline = time.monotonic() + 10
        while not path.exists():
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out waiting for {path.name}")
            time.sleep(0.01)


class ConfigMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "state" / "config.json"
        self.path.parent.mkdir()
        self.recovery = self.root / "recovery-outside-state"

    def test_additive_migration_preserves_every_legacy_and_unknown_key(self) -> None:
        original = {
            "future-setting": {"nested": [1, 2, 3]},
            "remote": "dev@example",
            "remote-cwd": "/srv/project",
            "recording-mode": "push-toggle",
            "claude-permissions": "acceptEdits",
            "default-voice": "rick",
            "onboarding-version": 7,
            "onboarding-completed-at": "2026-07-23T00:00:00Z",
            "barge-in": "on",
        }
        original_bytes = json.dumps(original, separators=(",", ":")).encode("utf-8")
        self.path.write_bytes(original_bytes)

        store = ConfigStore(self.path, recovery_dir=self.recovery)
        migrated = store.update(lambda _settings: None)

        self.assertEqual(migrated[SCHEMA_KEY], 1)
        for key, value in original.items():
            self.assertEqual(migrated[key], value)
        digest = hashlib.sha256(original_bytes).hexdigest()
        copy = self.recovery / f"config-{digest}.json"
        self.assertEqual(copy.read_bytes(), original_bytes)

    def test_invalid_migration_keeps_original_and_content_addressed_copy(self) -> None:
        original = b'{"unknown":{"keep":true}}'
        self.path.write_bytes(original)

        def invalid(settings: dict) -> dict:
            return dict(settings)  # does not advance _schema-version

        store = ConfigStore(
            self.path,
            recovery_dir=self.recovery,
            current_schema=1,
            migrations={0: invalid},
        )
        with self.assertRaises(ConfigMigrationError):
            store.update(lambda _settings: None)

        self.assertEqual(self.path.read_bytes(), original)
        digest = hashlib.sha256(original).hexdigest()
        self.assertEqual(
            (self.recovery / f"config-{digest}.json").read_bytes(), original
        )

    def test_invalid_stored_schema_keeps_original_and_recovery_copy(self) -> None:
        original = b'{"_schema-version":"broken","unknown":true}'
        self.path.write_bytes(original)
        store = ConfigStore(self.path, recovery_dir=self.recovery)

        with self.assertRaises(ConfigMigrationError):
            store.update(lambda settings: settings.__setitem__("x", 1))

        self.assertEqual(self.path.read_bytes(), original)
        digest = hashlib.sha256(original).hexdigest()
        self.assertEqual(
            (self.recovery / f"config-{digest}.json").read_bytes(), original
        )

    def test_future_schema_is_preserved_without_downgrade(self) -> None:
        original = {SCHEMA_KEY: 99, "future": True}
        self.path.write_text(json.dumps(original), encoding="utf-8")
        ConfigStore(self.path).update(lambda settings: settings.__setitem__("legacy", "ok"))
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved, {SCHEMA_KEY: 99, "future": True, "legacy": "ok"})

    def test_mutator_cannot_remove_or_downgrade_owned_schema(self) -> None:
        for version in (1, 99):
            with self.subTest(version=version):
                original = json.dumps({SCHEMA_KEY: version, "keep": True}).encode()
                self.path.write_bytes(original)
                store = ConfigStore(self.path)

                def remove_schema(settings: dict) -> None:
                    settings.pop(SCHEMA_KEY)

                with self.assertRaises(ConfigMigrationError):
                    store.update(remove_schema)
                self.assertEqual(self.path.read_bytes(), original)

                def downgrade(settings: dict) -> None:
                    settings[SCHEMA_KEY] = max(0, version - 1)

                with self.assertRaises(ConfigMigrationError):
                    store.update(downgrade)
                self.assertEqual(self.path.read_bytes(), original)

    def test_save_cannot_downgrade_a_future_schema(self) -> None:
        original = json.dumps({SCHEMA_KEY: 99, "future": True}).encode()
        self.path.write_bytes(original)
        with self.assertRaises(ConfigMigrationError):
            ConfigStore(self.path).save({"old-client": True})
        self.assertEqual(self.path.read_bytes(), original)

    def test_current_schema_read_does_not_normalize_existing_bytes(self) -> None:
        original = b'{"_schema-version":1, "spacing": "belongs to the user"}\r\n'
        self.path.write_bytes(original)
        self.assertEqual(ConfigStore(self.path).load()["spacing"], "belongs to the user")
        self.assertEqual(self.path.read_bytes(), original)


class ConfigFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ, {"TALKTOMECLAUDE_CONFIG_DIR": self.tmp.name}, clear=False
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = Path(self.tmp.name) / "config.json"

    def test_public_signatures_remain_compatible(self) -> None:
        self.assertEqual(str(inspect.signature(config.load)), "() -> dict")
        self.assertEqual(str(inspect.signature(config.save)), "(settings: dict) -> None")
        self.assertEqual(
            str(inspect.signature(config.get_value)), "(key: str, default=None)"
        )
        self.assertEqual(
            str(inspect.signature(config.set_value)), "(key: str, value) -> None"
        )

    def test_facade_update_preserves_unknown_keys_through_store(self) -> None:
        self.path.write_text(
            json.dumps({"unknown": {"keep": True}, "default-voice": "rick"}),
            encoding="utf-8",
        )
        config.set_recording_mode("push-toggle")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["unknown"], {"keep": True})
        self.assertEqual(saved["default-voice"], "rick")
        self.assertEqual(saved["recording-mode"], "push-toggle")

    def test_schema_less_reads_are_byte_and_directory_side_effect_free(self) -> None:
        original = b'{"future":{"keep":true},"default-voice":"rick"}\r\n'
        self.path.write_bytes(original)
        entries_before = sorted(path.name for path in self.path.parent.iterdir())

        self.assertEqual(config.load()["future"], {"keep": True})
        self.assertEqual(config.get_value("default-voice"), "rick")
        self.assertEqual(config.default_voice_name(), "rick")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(
            sorted(path.name for path in self.path.parent.iterdir()), entries_before
        )

        config.set_value("recording-mode", "push-toggle")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved[SCHEMA_KEY], 1)
        self.assertEqual(saved["future"], {"keep": True})
        self.assertEqual(saved["default-voice"], "rick")
        self.assertEqual(saved["recording-mode"], "push-toggle")
        digest = hashlib.sha256(original).hexdigest()
        self.assertEqual(
            (self.path.parent / "recovery" / f"config-{digest}.json").read_bytes(),
            original,
        )

    def test_facade_write_to_malformed_file_preserves_original_bytes(self) -> None:
        original = b'{"broken":'
        self.path.write_bytes(original)
        with self.assertRaises(config.ConfigLoadError):
            config.set_voice_assist(False)
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
