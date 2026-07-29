from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import talktomeclaude.assistant.hooks as hook_module
from talktomeclaude.assistant import (
    AssistantAdapter,
    AssistantEventCode,
    CLAUDE_STOP_HOOK_COMMAND,
    CODEX_OWNED_HOOK_MARKER,
    CODEX_STOP_HOOK_COMMAND,
    ClaudeCodeAdapter,
    ClaudeHookManager,
    CodexHookManager,
    DirectorEventGate,
    DirectorLaunchGuard,
    HookStatus,
    OWNED_HOOK_MARKER,
    SessionControlHookManager,
    SuppressionRegistry,
    canonical_reply_digest,
)
from talktomeclaude.assistant.hooks import HookSettingsError
from talktomeclaude.assistant.suppression import (
    CORRELATION_ENV,
    DIRECTOR_ROLE,
    ROLE_ENV,
    SuppressionError,
)
from talktomeclaude.storage import AtomicJsonTransaction


_WINDOWS_HOOK_PREFIX = (
    "powershell.exe -NoProfile -NonInteractive -EncodedCommand "
)


def _decode_windows_hook(command: str) -> str:
    if not command.startswith(_WINDOWS_HOOK_PREFIX):
        raise AssertionError(f"unexpected Windows hook command: {command!r}")
    encoded = command.removeprefix(_WINDOWS_HOOK_PREFIX)
    return base64.b64decode(encoded).decode("utf-16-le")


def payload(**changes: object) -> str:
    answer = changes.pop(
        "answer", "Exact Unicode: café U0001f642 \u05e9\u05dc\u05d5\u05dd"
    )
    session = str(changes.get("session", "session-1"))
    event_id = str(changes.get("event_id", "event-1"))
    version = changes.get("version", 1)
    value: dict[str, object] = {
        "version": version,
        "session": session,
        "event_id": event_id,
        "answer": answer,
        "digest": canonical_reply_digest(
            version=version, session=session, event_id=event_id, answer=str(answer)
        ),
    }
    value.update(changes)
    return json.dumps(value, ensure_ascii=False)


class ClaudeHookManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "settings.json"
        self.manager = ClaudeHookManager(self.path)

    def test_install_and_uninstall_are_owned_idempotent_and_preserve_unrelated_semantics(
        self,
    ) -> None:
        original_value = {
            "theme": "dark",
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "audit"}],
                    }
                ],
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "other-stop", "timeout": 9}
                        ],
                    }
                ],
            },
            "custom": {"unicode": "雪"},
        }
        original = json.dumps(
            original_value, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self.path.write_bytes(original)

        installed = self.manager.install()
        first_install = self.path.read_bytes()
        self.assertEqual(installed.status, HookStatus.INSTALLED)
        document = json.loads(first_install)
        self.assertEqual(document["theme"], "dark")
        self.assertEqual(document["custom"], {"unicode": "雪"})
        self.assertEqual(
            document["hooks"]["Stop"][0], original_value["hooks"]["Stop"][0]
        )
        owned = document["hooks"]["Stop"][1]["hooks"][0]
        self.assertEqual(
            owned, {"type": "command", "command": CLAUDE_STOP_HOOK_COMMAND}
        )

        self.manager.install()
        self.assertEqual(self.path.read_bytes(), first_install)
        self.assertEqual(self.manager.inspect().owned_entries, 3)

        self.assertEqual(self.manager.uninstall().status, HookStatus.ABSENT)
        after_uninstall = self.path.read_bytes()
        self.assertEqual(json.loads(after_uninstall), original_value)
        self.manager.uninstall()
        self.assertEqual(self.path.read_bytes(), after_uninstall)

    def test_inspect_is_byte_preserving_and_missing_uninstall_does_not_create_file(
        self,
    ) -> None:
        self.assertEqual(self.manager.inspect().status, HookStatus.ABSENT)
        self.assertEqual(self.manager.uninstall().status, HookStatus.ABSENT)
        self.assertFalse(self.path.exists())
        raw = b'{  "hooks": {"Stop": []}, "x": 1 }\r\n'
        self.path.write_bytes(raw)
        self.assertEqual(self.manager.inspect().status, HookStatus.ABSENT)
        self.manager.uninstall()
        self.assertEqual(self.path.read_bytes(), raw)

    def test_conflicting_owned_marker_and_malformed_structure_fail_closed(self) -> None:
        conflict = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"other --owner-marker {OWNED_HOOK_MARKER}",
                            }
                        ]
                    }
                ]
            }
        }
        self.path.write_text(json.dumps(conflict), encoding="utf-8")
        before = self.path.read_bytes()
        self.assertEqual(self.manager.inspect().status, HookStatus.CONFLICT)
        with self.assertRaises(HookSettingsError):
            self.manager.install()
        with self.assertRaises(HookSettingsError):
            self.manager.uninstall()
        self.assertEqual(self.path.read_bytes(), before)

        conflict["hooks"]["Stop"][0]["hooks"][0] = {
            "type": "command",
            "command": CLAUDE_STOP_HOOK_COMMAND,
            "timeout": 10,
        }
        self.path.write_text(json.dumps(conflict), encoding="utf-8")
        self.assertEqual(self.manager.inspect().status, HookStatus.CONFLICT)

        self.path.write_text('{"hooks":{"Stop":{}}}', encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(HookSettingsError):
            self.manager.install()
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_owned_install_and_unrelated_updates_lose_no_keys(self) -> None:
        barrier = threading.Barrier(9)
        failures: list[BaseException] = []

        def install() -> None:
            try:
                barrier.wait()
                ClaudeHookManager(self.path).install()
            except BaseException as exc:
                failures.append(exc)

        def update(index: int) -> None:
            try:
                barrier.wait()
                AtomicJsonTransaction(
                    self.path, purpose="claude-hook-settings"
                ).update(lambda current: {**current, f"unrelated-{index}": index})
            except BaseException as exc:
                failures.append(exc)

        threads = [threading.Thread(target=install)] + [
            threading.Thread(target=update, args=(index,)) for index in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            {document[f"unrelated-{index}"] for index in range(8)}, set(range(8))
        )
        self.assertEqual(ClaudeHookManager(self.path).inspect().owned_entries, 3)

    def test_uncooperative_external_write_between_read_and_replace_is_retried(
        self,
    ) -> None:
        self.path.write_text('{"theme":"dark"}', encoding="utf-8")
        conflict_window = threading.Barrier(2)
        external_complete = threading.Event()
        phase_calls = 0

        def phase(phase_name: str) -> None:
            nonlocal phase_calls
            self.assertEqual(phase_name, "before_external_conflict_check")
            phase_calls += 1
            if phase_calls == 1:
                conflict_window.wait(2)
                self.assertTrue(external_complete.wait(2))

        def external_writer() -> None:
            conflict_window.wait(2)
            # Deliberately bypass AtomicJsonTransaction and its private mutex.
            value = json.loads(self.path.read_text(encoding="utf-8"))
            value["external-unrelated"] = {"preserved": True}
            self.path.write_text(json.dumps(value), encoding="utf-8")
            external_complete.set()

        writer = threading.Thread(target=external_writer)
        writer.start()
        manager = ClaudeHookManager(self.path, phase_hook=phase)

        installed = manager.install()
        writer.join(2)

        self.assertFalse(writer.is_alive())
        self.assertGreaterEqual(phase_calls, 2)
        self.assertEqual(installed.status, HookStatus.INSTALLED)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["theme"], "dark")
        self.assertEqual(document["external-unrelated"], {"preserved": True})
        self.assertEqual(manager.inspect().owned_entries, 3)

    def test_exact_legacy_entries_are_migrated_to_the_resolved_executable(self) -> None:
        legacy = {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": CLAUDE_STOP_HOOK_COMMAND}]}],
                "UserPromptExpansion": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    "talktomeclaude hook session --provider claude "
                                    f"--owner-marker {hook_module.CLAUDE_SESSION_CONTROL_MARKER}"
                                ),
                            }
                        ]
                    }
                ],
                "SessionEnd": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    "talktomeclaude hook session --provider claude "
                                    f"--owner-marker {hook_module.CLAUDE_SESSION_CONTROL_MARKER}"
                                ),
                            }
                        ]
                    }
                ],
            }
        }
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        executable = r"C:\Program Files\Talk To Me\talktomejohnny.exe"

        installed = ClaudeHookManager(self.path, executable=executable).install()

        self.assertEqual(installed.status, HookStatus.INSTALLED)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        command = document["hooks"]["UserPromptExpansion"][0]["hooks"][0][
            "command"
        ]
        self.assertEqual(
            _decode_windows_hook(command),
            "& 'C:\\Program Files\\Talk To Me\\talktomejohnny.exe' 'hook' "
            "'session' '--provider' 'claude' '--owner-marker' "
            f"'{hook_module.CLAUDE_SESSION_CONTROL_MARKER}'",
        )
        self.assertNotIn("talktomeclaude hook session", json.dumps(document))

    def test_resolve_hook_executable_and_session_manager_quote_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            interpreter = Path(temporary) / "python.exe"
            interpreter.write_text("", encoding="utf-8")
            with mock.patch.object(hook_module.sys, "argv", ["talktomejohnny"]):
                with mock.patch.object(
                    hook_module.sys, "executable", str(interpreter)
                ):
                    with self.assertRaisesRegex(
                        HookSettingsError, "could not be resolved safely"
                    ):
                        hook_module.resolve_hook_executable({"PATH": ""})
                    with self.assertRaisesRegex(HookSettingsError, "absolute path"):
                        hook_module.resolve_hook_executable(
                            {
                                "PATH": "",
                                "TALKTOMEJOHNNY_HOOK_EXECUTABLE": "talktomejohnny",
                            }
                        )
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "talktomejohnny.exe"
            executable.write_text("", encoding="utf-8")
            with mock.patch.object(hook_module.sys, "argv", [str(executable)]):
                self.assertEqual(
                    hook_module.resolve_hook_executable({"PATH": ""}),
                    str(executable.resolve()),
                )
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary)
            executable = scripts / "talktomejohnny.exe"
            interpreter = scripts / "python.exe"
            executable.write_text("", encoding="utf-8")
            interpreter.write_text("", encoding="utf-8")
            with mock.patch.object(hook_module.sys, "argv", ["talktomejohnny"]):
                with mock.patch.object(
                    hook_module.sys, "executable", str(interpreter)
                ):
                    self.assertEqual(
                        hook_module.resolve_hook_executable({"PATH": ""}),
                        str(executable),
                    )
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary)
            sibling = scripts / "talktomejohnny.exe"
            interpreter = scripts / "python.exe"
            sibling.write_text("", encoding="utf-8")
            interpreter.write_text("", encoding="utf-8")
            preferred = scripts / "preferred" / "talktomejohnny.exe"
            preferred.parent.mkdir()
            preferred.write_text("", encoding="utf-8")
            with mock.patch.object(hook_module.sys, "argv", ["talktomejohnny"]):
                with mock.patch.object(
                    hook_module.sys, "executable", str(interpreter)
                ):
                    with mock.patch.object(
                        hook_module.shutil, "which", return_value=str(preferred)
                    ):
                        self.assertEqual(
                            hook_module.resolve_hook_executable({"PATH": "shim"}),
                            str(preferred),
                        )

        windows_path = r"C:\Program Files\Talk To Me\talktomejohnny.exe"
        manager = SessionControlHookManager(
            self.path,
            provider="codex",
            executable=windows_path,
        )
        installed = manager.install()
        self.assertEqual(installed.status, HookStatus.INSTALLED)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            _decode_windows_hook(
                document["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            ),
            "& 'C:\\Program Files\\Talk To Me\\talktomejohnny.exe' 'hook' "
            "'session' '--provider' 'codex' '--owner-marker' "
            f"'{hook_module.CODEX_SESSION_CONTROL_MARKER}'",
        )
        with mock.patch.object(hook_module.os, "name", "posix"):
            self.assertEqual(
                hook_module._quoted_command(
                    "/opt/Talk To Me/talktomejohnny",
                    "hook",
                    "session",
                ),
                "'/opt/Talk To Me/talktomejohnny' hook session",
            )

    def test_install_migrates_previous_absolute_windows_command_format(self) -> None:
        executable = r"C:\Program Files\Talk To Me\talktomejohnny.exe"
        session = subprocess.list2cmdline(
            [
                executable,
                "hook",
                "session",
                "--provider",
                "claude",
                "--owner-marker",
                hook_module.CLAUDE_SESSION_CONTROL_MARKER,
            ]
        )
        stop = subprocess.list2cmdline(
            [
                executable,
                "hook",
                "stop",
                "--transport",
                "--owner-marker",
                OWNED_HOOK_MARKER,
            ]
        )
        self.path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptExpansion": [
                            {"hooks": [{"type": "command", "command": session}]}
                        ],
                        "SessionEnd": [
                            {"hooks": [{"type": "command", "command": session}]}
                        ],
                        "Stop": [
                            {"hooks": [{"type": "command", "command": stop}]}
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )

        installed = ClaudeHookManager(self.path, executable=executable).install()

        self.assertEqual(installed.status, HookStatus.INSTALLED)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        for event_name in ("UserPromptExpansion", "SessionEnd", "Stop"):
            command = document["hooks"][event_name][0]["hooks"][0]["command"]
            self.assertTrue(command.startswith(_WINDOWS_HOOK_PREFIX))
            self.assertNotEqual(command, session)
            self.assertNotEqual(command, stop)

    def test_resolve_hook_executable_preserves_stable_absolute_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "versions" / "talktomejohnny"
            target.parent.mkdir()
            target.write_text("", encoding="utf-8")
            launcher = root / "bin" / "talktomejohnny"
            launcher.parent.mkdir()
            try:
                launcher.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            with mock.patch.object(hook_module.sys, "argv", [str(launcher)]):
                self.assertEqual(
                    hook_module.resolve_hook_executable({"PATH": ""}),
                    os.path.abspath(launcher),
                )

    def test_install_migrates_previous_resolved_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "tool" / "talktomejohnny"
            target.parent.mkdir()
            target.write_text("", encoding="utf-8")
            launcher = root / "bin" / "talktomejohnny"
            launcher.parent.mkdir()
            try:
                launcher.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            session = hook_module._previous_quoted_command(
                str(target),
                "hook",
                "session",
                "--provider",
                "claude",
                "--owner-marker",
                hook_module.CLAUDE_SESSION_CONTROL_MARKER,
            )
            stop = hook_module._previous_quoted_command(
                str(target),
                "hook",
                "stop",
                "--transport",
                "--owner-marker",
                OWNED_HOOK_MARKER,
            )
            self.path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptExpansion": [
                                {"hooks": [{"type": "command", "command": session}]}
                            ],
                            "SessionEnd": [
                                {"hooks": [{"type": "command", "command": session}]}
                            ],
                            "Stop": [
                                {"hooks": [{"type": "command", "command": stop}]}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            installed = ClaudeHookManager(
                self.path,
                executable=str(launcher),
            ).install()

            self.assertEqual(installed.status, HookStatus.INSTALLED)
            document = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(
                document["hooks"]["UserPromptExpansion"][0]["hooks"][0][
                    "command"
                ],
                hook_module._quoted_command(
                    str(launcher),
                    "hook",
                    "session",
                    "--provider",
                    "claude",
                    "--owner-marker",
                    hook_module.CLAUDE_SESSION_CONTROL_MARKER,
                ),
            )

    def test_external_conflict_retries_are_bounded_without_losing_latest_file(
        self,
    ) -> None:
        self.path.write_text('{"generation":0}', encoding="utf-8")
        generation = 0

        def always_conflict(_phase_name: str) -> None:
            nonlocal generation
            generation += 1
            self.path.write_text(
                json.dumps({"generation": generation}), encoding="utf-8"
            )

        manager = ClaudeHookManager(
            self.path, max_conflict_attempts=3, phase_hook=always_conflict
        )

        with self.assertRaisesRegex(HookSettingsError, "changed too often"):
            manager.install()

        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"generation": 3})
        self.assertEqual(manager.inspect().status, HookStatus.ABSENT)


class CodexHookManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "hooks.json"
        self.manager = CodexHookManager(self.path)

    def test_install_is_additive_owned_and_idempotent(self) -> None:
        existing = {
            "description": "existing Codex hooks",
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "other-stop",
                                "timeout": 9,
                            }
                        ]
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "audit"}],
                    }
                ],
            },
        }
        self.path.write_text(json.dumps(existing), encoding="utf-8")

        first = self.manager.install()
        installed_wire = self.path.read_bytes()
        second = self.manager.install()
        document = json.loads(installed_wire)

        self.assertEqual(first.status, HookStatus.INSTALLED)
        self.assertEqual(second.status, HookStatus.INSTALLED)
        self.assertEqual(self.path.read_bytes(), installed_wire)
        self.assertEqual(document["description"], existing["description"])
        self.assertEqual(document["hooks"]["Stop"][0], existing["hooks"]["Stop"][0])
        self.assertEqual(
            document["hooks"]["Stop"][1]["hooks"],
            [{"type": "command", "command": CODEX_STOP_HOOK_COMMAND}],
        )
        self.assertEqual(self.manager.inspect().owned_entries, 3)

    def test_uninstall_removes_only_codex_entry(self) -> None:
        self.manager.install()
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["hooks"]["Stop"].insert(
            0,
            {"hooks": [{"type": "command", "command": "other-stop"}]},
        )
        self.path.write_text(json.dumps(document), encoding="utf-8")

        self.assertEqual(self.manager.uninstall().status, HookStatus.ABSENT)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            after,
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-stop"}]}]}},
        )

    def test_marker_conflict_fails_closed(self) -> None:
        conflict = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"other --owner-marker {CODEX_OWNED_HOOK_MARKER}",
                            }
                        ]
                    }
                ]
            }
        }
        self.path.write_text(json.dumps(conflict), encoding="utf-8")
        before = self.path.read_bytes()

        self.assertEqual(self.manager.inspect().status, HookStatus.CONFLICT)
        with self.assertRaises(HookSettingsError):
            self.manager.install()
        self.assertEqual(self.path.read_bytes(), before)


class PayloadValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        registry = SuppressionRegistry(Path(self.temporary.name) / "suppression.json")
        self.adapter = ClaudeCodeAdapter(registry)

    def assert_code(self, expected: AssistantEventCode, raw: bytes | str) -> None:
        self.assertEqual(self.adapter.validate(raw).code, expected)

    def test_valid_exact_unicode_payload_is_repr_private(self) -> None:
        result = self.adapter.validate(payload())
        self.assertTrue(result.accepted)
        assert result.event is not None
        self.assertIn("session-1", repr(result.event))
        self.assertNotIn("Exact Unicode", repr(result.event))
        self.assertNotIn("Exact Unicode", repr(result))

    def test_missing_and_invalid_required_fields(self) -> None:
        base = json.loads(payload())
        for field, expected in (
            ("version", AssistantEventCode.INVALID_VERSION),
            ("session", AssistantEventCode.INVALID_SESSION),
            ("event_id", AssistantEventCode.INVALID_EVENT_ID),
            ("answer", AssistantEventCode.INVALID_ANSWER),
            ("digest", AssistantEventCode.INVALID_DIGEST),
        ):
            missing = dict(base)
            missing.pop(field)
            with self.subTest(missing=field):
                self.assert_code(expected, json.dumps(missing))
        cases = [
            (AssistantEventCode.INVALID_VERSION, payload(version=2)),
            (AssistantEventCode.INVALID_VERSION, payload(version=True)),
            (AssistantEventCode.INVALID_SESSION, payload(session="")),
            (AssistantEventCode.INVALID_SESSION, payload(session="bad\nvalue")),
            (AssistantEventCode.INVALID_EVENT_ID, payload(event_id=None)),
            (AssistantEventCode.INVALID_ANSWER, payload(answer="")),
            (AssistantEventCode.INVALID_DIGEST, payload(digest="0" * 64)),
            (AssistantEventCode.INVALID_DIGEST, payload(digest="A" * 64)),
        ]
        for expected, raw in cases:
            with self.subTest(expected=expected, raw=raw):
                self.assert_code(expected, raw)

    def test_corrupt_duplicate_key_encoding_root_and_size_are_rejected(self) -> None:
        self.assert_code(AssistantEventCode.INVALID_JSON, "{")
        self.assert_code(
            AssistantEventCode.INVALID_JSON,
            '{"version":1,"version":1,"session_id":"s"}',
        )
        self.assert_code(AssistantEventCode.INVALID_ENCODING, b"\xff")
        self.assert_code(AssistantEventCode.INVALID_ROOT, "[]")
        small = ClaudeCodeAdapter(self.adapter._suppression, max_payload_bytes=8)
        self.assertEqual(
            small.validate(payload()).code, AssistantEventCode.PAYLOAD_TOO_LARGE
        )

    def test_digest_binds_session_and_event_identity_on_first_arrival(self) -> None:
        original = json.loads(payload())
        for field, replacement in (
            ("session", "session-other"),
            ("event_id", "event-other"),
        ):
            changed = dict(original)
            changed[field] = replacement
            with self.subTest(field=field):
                self.assert_code(
                    AssistantEventCode.INVALID_DIGEST,
                    json.dumps(changed, ensure_ascii=False),
                )

    def test_throwing_metric_never_changes_delivery_or_validation(self) -> None:
        adapter = ClaudeCodeAdapter(
            self.adapter._suppression,
            metric=lambda _code: (_ for _ in ()).throw(RuntimeError("observer")),
        )
        published: list[object] = []

        accepted = adapter.handle(payload(), published.append)
        invalid = adapter.handle("{", published.append)

        self.assertTrue(accepted.accepted)
        self.assertEqual(len(published), 1)
        self.assertEqual(invalid.code, AssistantEventCode.INVALID_JSON)

    def test_assistant_submit_policy_is_local_to_adapter(self) -> None:
        enabled = ClaudeCodeAdapter(
            self.adapter._suppression, assistant_auto_submit=True
        )
        disabled = ClaudeCodeAdapter(
            self.adapter._suppression, assistant_auto_submit=False
        )
        self.assertTrue(enabled.submit_eligible(transcript_acceptable=True))
        self.assertFalse(enabled.submit_eligible(transcript_acceptable=False))
        self.assertFalse(disabled.submit_eligible(transcript_acceptable=True))
        self.assertIsInstance(enabled, AssistantAdapter)


class DirectorSuppressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.now = [1000.0]
        self.registry = SuppressionRegistry(
            Path(self.temporary.name) / "suppression.json", clock=lambda: self.now[0]
        )

    def test_launch_preregisters_before_spawn_and_sets_role_and_correlation(
        self,
    ) -> None:
        observations: list[tuple[tuple[str, ...], dict[str, str], str | None]] = []

        def spawn(command: tuple[str, ...], environment: dict[str, str]) -> object:
            probe = type(
                "Probe",
                (),
                {"role": None, "session_id": "other", "correlation_id": "corr-1"},
            )()
            observations.append((command, environment, self.registry.reason_for(probe)))
            return object()

        managed = DirectorLaunchGuard(self.registry).launch(
            ["claude", "-p"], "corr-1", spawn, environment={"KEEP": "yes"}
        )
        self.assertIsNotNone(managed.process)
        self.assertEqual(managed.lease.correlation_id, "corr-1")
        command, environment, reason = observations[0]
        self.assertEqual(command, ("claude", "-p"))
        self.assertEqual(environment["KEEP"], "yes")
        self.assertEqual(environment[ROLE_ENV], DIRECTOR_ROLE)
        self.assertEqual(environment[CORRELATION_ENV], "corr-1")
        self.assertEqual(reason, "suppressed_correlation")

    def test_managed_launch_closes_suppression_on_wait_and_context_failures(
        self,
    ) -> None:
        process = mock.Mock()
        process.wait.return_value = 0
        managed = DirectorLaunchGuard(self.registry).launch(
            ["claude"], "corr-wait", lambda _command, _environment: process
        )
        self.assertEqual(managed.wait(), 0)
        self.assertEqual(managed.wait(), 0)
        self.now[0] = 1005.001
        probe = type(
            "Probe", (), {"role": None, "session_id": None, "correlation_id": "corr-wait"}
        )()
        self.assertIsNone(self.registry.reason_for(probe))

        context_process = mock.Mock()
        context_process.poll.return_value = None
        context_process.wait.return_value = 0
        with self.assertRaisesRegex(RuntimeError, "body"):
            with DirectorLaunchGuard(self.registry).launch(
                ["claude"],
                "corr-context",
                lambda _command, _environment: context_process,
                drain_seconds=0.0,
            ):
                raise RuntimeError("body")
        context_process.terminate.assert_called_once_with()
        context_process.wait.assert_called_once_with(timeout=1.0)
        self.now[0] += 0.001
        probe.correlation_id = "corr-context"
        self.assertIsNone(self.registry.reason_for(probe))

    def test_wait_timeout_while_alive_retains_suppression_until_confirmed_exit(
        self,
    ) -> None:
        process = mock.Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("claude", 0.01)
        process.poll.return_value = None
        managed = DirectorLaunchGuard(self.registry).launch(
            ["claude"], "corr-timeout", lambda _command, _environment: process
        )
        probe = type(
            "Probe",
            (),
            {"role": None, "session_id": None, "correlation_id": "corr-timeout"},
        )()

        with self.assertRaises(subprocess.TimeoutExpired):
            managed.wait(timeout=0.01)

        self.assertEqual(self.registry.reason_for(probe), "suppressed_correlation")
        process.poll.return_value = 0
        self.assertEqual(managed.poll(), 0)
        self.now[0] = 1005.001
        self.assertIsNone(self.registry.reason_for(probe))

    def test_context_terminate_timeout_kills_reaps_then_starts_drain(self) -> None:
        calls: list[str] = []

        class Process:
            returncode: int | None = None
            killed = False

            def poll(self) -> int | None:
                calls.append("poll")
                return self.returncode

            def terminate(self) -> None:
                calls.append("terminate")

            def kill(self) -> None:
                calls.append("kill")
                self.killed = True

            def wait(self, *, timeout: float) -> int:
                calls.append(f"wait:{timeout}")
                if not self.killed:
                    raise subprocess.TimeoutExpired("claude", timeout)
                self.returncode = -9
                return self.returncode

        process = Process()
        with DirectorLaunchGuard(self.registry).launch(
            ["claude"],
            "corr-kill",
            lambda _command, _environment: process,
            drain_seconds=5.0,
        ):
            pass

        self.assertEqual(
            calls,
            [
                "poll",
                "terminate",
                "wait:1.0",
                "poll",
                "poll",
                "kill",
                "wait:1.0",
            ],
        )
        probe = type(
            "Probe",
            (),
            {"role": None, "session_id": None, "correlation_id": "corr-kill"},
        )()
        self.now[0] = 1005.0
        self.assertEqual(self.registry.reason_for(probe), "suppressed_correlation")
        self.now[0] = 1005.001
        self.assertIsNone(self.registry.reason_for(probe))

    def test_context_preserves_body_error_and_suppression_when_child_survives_kill(
        self,
    ) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("claude", 0.01)
        managed = DirectorLaunchGuard(self.registry).launch(
            ["claude"], "corr-alive", lambda _command, _environment: process
        )
        probe = type(
            "Probe",
            (),
            {"role": None, "session_id": None, "correlation_id": "corr-alive"},
        )()

        with self.assertRaisesRegex(RuntimeError, "body-original") as raised:
            with managed:
                raise RuntimeError("body-original")

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertTrue(any("TimeoutExpired" in note for note in raised.exception.__notes__))
        self.assertEqual(self.registry.reason_for(probe), "suppressed_correlation")

    def test_spawn_error_is_preserved_when_suppression_cleanup_also_fails(self) -> None:
        registry = mock.Mock(spec=SuppressionRegistry)
        lease = mock.Mock()
        lease.mark_exited.side_effect = SuppressionError("cleanup")
        registry.preregister.return_value = lease

        with self.assertRaisesRegex(RuntimeError, "spawn") as raised:
            DirectorLaunchGuard(registry).launch(
                ["claude"],
                "corr",
                lambda _command, _environment: (_ for _ in ()).throw(
                    RuntimeError("spawn")
                ),
            )

        self.assertTrue(any("cleanup" in note for note in raised.exception.__notes__))

    def test_registration_failure_prevents_spawn(self) -> None:
        registry = mock.Mock(spec=SuppressionRegistry)
        registry.preregister.side_effect = SuppressionError("disk unavailable")
        spawn = mock.Mock()
        with self.assertRaises(SuppressionError):
            DirectorLaunchGuard(registry).launch(["claude"], "corr", spawn)
        spawn.assert_not_called()

    def test_registry_rejects_non_identifier_correlation_and_session_content(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            self.registry.preregister("answer text must not become an identifier")
        lease = self.registry.preregister("corr-safe")
        with self.assertRaises(ValueError):
            lease.register_initialization("bad\nsession")

    def test_initialization_is_registered_before_callback_and_gates_results(
        self,
    ) -> None:
        lease = self.registry.preregister("corr-2")
        gate = DirectorEventGate(lease)
        accepted: list[str] = []
        self.assertFalse(
            gate.result("director-session", lambda: accepted.append("early"))
        )

        def accept_initialization(session_id: str) -> None:
            self.assertTrue(self.registry.session_registered("corr-2", session_id))
            accepted.append("initialized")

        gate.initialization("director-session", accept_initialization)
        self.assertTrue(
            gate.result("director-session", lambda: accepted.append("result"))
        )
        self.assertEqual(accepted, ["initialized", "result"])

    def test_role_session_and_correlation_each_suppress_through_drain_window(
        self,
    ) -> None:
        lease = self.registry.preregister("corr-3")
        lease.register_initialization("director-session")
        adapter = ClaudeCodeAdapter(self.registry)
        event = adapter.validate(payload()).event
        assert event is not None
        self.assertEqual(
            self.registry.reason_for(event, environment={ROLE_ENV: DIRECTOR_ROLE}),
            "suppressed_role",
        )
        role_event = adapter.validate(payload(role=DIRECTOR_ROLE)).event
        assert role_event is not None
        self.assertEqual(self.registry.reason_for(role_event), "suppressed_role")

        session_event = adapter.validate(payload(session="director-session")).event
        assert session_event is not None
        self.assertEqual(self.registry.reason_for(session_event), "suppressed_session")
        correlation_event = adapter.validate(payload(correlation_id="corr-3")).event
        assert correlation_event is not None
        self.assertEqual(
            self.registry.reason_for(correlation_event), "suppressed_correlation"
        )
        self.assertEqual(
            self.registry.reason_for(event, environment={CORRELATION_ENV: "corr-3"}),
            "suppressed_correlation",
        )

        lease.mark_exited(drain_seconds=5.0)
        self.now[0] = 1005.0
        self.assertEqual(self.registry.reason_for(session_event), "suppressed_session")
        self.now[0] = 1005.001
        self.assertIsNone(self.registry.reason_for(session_event))
        self.assertEqual(self.registry.prune_expired(), 1)

    def test_suppressed_event_only_emits_content_free_metric_and_never_publishes(
        self,
    ) -> None:
        metrics: list[str] = []
        published: list[object] = []
        adapter = ClaudeCodeAdapter(self.registry, metric=metrics.append)
        result = adapter.handle(
            payload(), published.append, environment={ROLE_ENV: DIRECTOR_ROLE}
        )
        self.assertEqual(result.code, AssistantEventCode.SUPPRESSED_ROLE)
        self.assertEqual(metrics, ["suppressed_role"])
        self.assertEqual(published, [])
        self.assertNotIn("Exact Unicode", repr(result))


if __name__ == "__main__":
    unittest.main()
