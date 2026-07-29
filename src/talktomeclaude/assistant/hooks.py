"""Idempotent ownership-scoped assistant Stop-hook JSON merges."""

from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from talktomeclaude.storage import AtomicJsonTransaction, AtomicStorageError

OWNED_HOOK_MARKER = "talktomeclaude.windows-companion.v1"
CLAUDE_STOP_HOOK_COMMAND = (
    f"talktomeclaude hook stop --transport --owner-marker {OWNED_HOOK_MARKER}"
)
CODEX_OWNED_HOOK_MARKER = "talktomeclaude.windows-companion.codex.v1"
CODEX_STOP_HOOK_COMMAND = (
    "talktomeclaude hook stop --transport --provider codex "
    f"--owner-marker {CODEX_OWNED_HOOK_MARKER}"
)
CLAUDE_SESSION_CONTROL_MARKER = "talktomejohnny.session-control.claude.v1"
CODEX_SESSION_CONTROL_MARKER = "talktomejohnny.session-control.codex.v1"
CLAUDE_SESSION_CONTROL_COMMAND = (
    "talktomejohnny hook session --provider claude "
    f"--owner-marker {CLAUDE_SESSION_CONTROL_MARKER}"
)
CODEX_SESSION_CONTROL_COMMAND = (
    "talktomejohnny hook session --provider codex "
    f"--owner-marker {CODEX_SESSION_CONTROL_MARKER}"
)


class HookSettingsError(RuntimeError):
    """Assistant hook settings cannot be safely inspected or changed."""


class _ExternalSettingsConflict(AtomicStorageError):
    """An uncooperative writer changed settings during an optimistic update."""


class HookStatus(StrEnum):
    ABSENT = "absent"
    INSTALLED = "installed"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class HookInspection:
    status: HookStatus
    owned_entries: int


def _quoted_command(executable: str, *arguments: str) -> str:
    if not isinstance(executable, str) or not executable.strip():
        raise ValueError("hook executable must be a non-empty string")
    argv = [executable, *arguments]
    windows_style = (
        ":" in executable[:3]
        or "\\" in executable
        or executable.casefold().endswith(".exe")
    )
    return (
        subprocess.list2cmdline(argv)
        if os.name == "nt" or windows_style
        else shlex.join(argv)
    )


def resolve_hook_executable(environment: dict[str, str] | None = None) -> str:
    """Resolve the trusted CLI path that should be baked into installed hooks."""

    active = os.environ if environment is None else environment
    override = active.get(
        "TALKTOMEJOHNNY_HOOK_EXECUTABLE"
    ) or active.get("TALKTOMECLAUDE_HOOK_EXECUTABLE")
    if isinstance(override, str) and override.strip():
        override_path = Path(override.strip()).expanduser()
        if not override_path.is_absolute():
            raise HookSettingsError(
                "assistant hook executable override must be an absolute path"
            )
        return str(override_path)
    argv0 = Path(sys.argv[0]).expanduser()
    if argv0.is_absolute() and argv0.exists():
        return str(argv0.resolve())
    executable_parent = Path(sys.executable).expanduser().resolve().parent
    for candidate in (
        "talktomejohnny.exe",
        "talktomejohnny",
        "talktomeclaude.exe",
        "talktomeclaude",
    ):
        sibling = executable_parent / candidate
        if sibling.exists():
            return str(sibling)
    for candidate in ("talktomejohnny", "talktomeclaude"):
        resolved = shutil.which(candidate, path=active.get("PATH"))
        if resolved:
            return resolved
    raise HookSettingsError(
        "assistant hook executable could not be resolved safely"
    )


def _stop_hook_entries(provider: str, executable: str) -> tuple[dict[str, str], ...]:
    if provider == "claude":
        return (
            {
                "type": "command",
                "command": _quoted_command(
                    executable,
                    "hook",
                    "stop",
                    "--transport",
                    "--owner-marker",
                    OWNED_HOOK_MARKER,
                ),
            },
        )
    if provider == "codex":
        return (
            {
                "type": "command",
                "command": _quoted_command(
                    executable,
                    "hook",
                    "stop",
                    "--transport",
                    "--provider",
                    "codex",
                    "--owner-marker",
                    CODEX_OWNED_HOOK_MARKER,
                ),
            },
        )
    raise ValueError(f"unsupported assistant provider {provider!r}")


def _session_control_entries(
    provider: str, executable: str
) -> dict[str, tuple[dict[str, str], ...]]:
    if provider == "claude":
        marker = CLAUDE_SESSION_CONTROL_MARKER
        event_name = "UserPromptExpansion"
    elif provider == "codex":
        marker = CODEX_SESSION_CONTROL_MARKER
        event_name = "UserPromptSubmit"
    else:
        raise ValueError(f"unsupported assistant provider {provider!r}")
    entry = {
        "type": "command",
        "command": _quoted_command(
            executable,
            "hook",
            "session",
            "--provider",
            provider,
            "--owner-marker",
            marker,
        ),
    }
    return {
        event_name: (entry,),
        "SessionEnd": (dict(entry),),
    }


def _legacy_stop_entries(provider: str) -> dict[str, tuple[dict[str, str], ...]]:
    if provider == "claude":
        return {
            "Stop": (
                {
                    "type": "command",
                    "command": CLAUDE_STOP_HOOK_COMMAND,
                },
            )
        }
    if provider == "codex":
        return {
            "Stop": (
                {
                    "type": "command",
                    "command": CODEX_STOP_HOOK_COMMAND,
                },
            )
        }
    raise ValueError(f"unsupported assistant provider {provider!r}")


def _legacy_session_control_entries(
    provider: str,
) -> dict[str, tuple[dict[str, str], ...]]:
    marker = (
        CLAUDE_SESSION_CONTROL_MARKER
        if provider == "claude"
        else CODEX_SESSION_CONTROL_MARKER
    )
    event_name = "UserPromptExpansion" if provider == "claude" else "UserPromptSubmit"
    entry = {
        "type": "command",
        "command": (
            f"talktomeclaude hook session --provider {provider} "
            f"--owner-marker {marker}"
        ),
    }
    return {event_name: (entry,), "SessionEnd": (dict(entry),)}


def _owned_entry(entry: dict[str, Any], owned: dict[str, str]) -> bool:
    return entry == owned


def _marker_conflict(
    entry: dict[str, Any], marker: str, owned: dict[str, str]
) -> bool:
    command = entry.get("command")
    return (
        isinstance(command, str)
        and marker in command
        and not _owned_entry(entry, owned)
    )


def _command_entries(
    settings: dict[str, Any],
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    hooks = settings.get("hooks")
    if hooks is None:
        return []
    if not isinstance(hooks, dict):
        raise HookSettingsError("assistant hooks must be an object")
    found: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for event_name, rules in hooks.items():
        if not isinstance(event_name, str):
            raise HookSettingsError("assistant hook event must be a string")
        if not isinstance(rules, list):
            raise HookSettingsError(
                f"assistant {event_name} hooks must be a list"
            )
        for rule in rules:
            if not isinstance(rule, dict):
                raise HookSettingsError(
                    f"assistant {event_name} hook rule must be an object"
                )
            commands = rule.get("hooks")
            if not isinstance(commands, list):
                raise HookSettingsError(
                    f"assistant {event_name} hook commands must be a list"
                )
            for command in commands:
                if not isinstance(command, dict):
                    raise HookSettingsError(
                        f"assistant {event_name} hook command must be an object"
                    )
                found.append((event_name, rule, command))
    return found


def _inspect_settings(
    settings: dict[str, Any],
    *,
    markers: frozenset[str],
    owned_entries: dict[str, tuple[dict[str, str], ...]],
    accepted_entries: dict[str, tuple[dict[str, str], ...]],
) -> HookInspection:
    entries = _command_entries(settings)
    expected_owned = {
        (event_name, json.dumps(entry, sort_keys=True, separators=(",", ":")))
        for event_name, event_entries in owned_entries.items()
        for entry in event_entries
    }
    expected_accepted = {
        (event_name, json.dumps(entry, sort_keys=True, separators=(",", ":")))
        for event_name, event_entries in accepted_entries.items()
        for entry in event_entries
    }
    owned_count = sum(
        (
            event_name,
            json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
        in expected_owned
        for event_name, _rule, item in entries
    )
    conflict = any(
        (
            isinstance(item.get("command"), str)
            and any(marker in item["command"] for marker in markers)
            and (
                event_name,
                json.dumps(item, sort_keys=True, separators=(",", ":")),
            )
            not in expected_accepted
        )
        for event_name, _rule, item in entries
    )
    if conflict or owned_count not in {0, len(expected_owned)}:
        return HookInspection(HookStatus.CONFLICT, owned_count)
    return HookInspection(
        HookStatus.INSTALLED if owned_count else HookStatus.ABSENT,
        owned_count,
    )


class _JsonHookManager:
    """Merge only one exact command carrying one product ownership marker."""

    def __init__(
        self,
        settings_path: str | os.PathLike[str],
        *,
        owned_entries: dict[str, tuple[dict[str, str], ...]],
        legacy_entries: dict[str, tuple[dict[str, str], ...]] | None = None,
        markers: tuple[str, ...],
        purpose: str,
        max_conflict_attempts: int = 8,
        phase_hook: Callable[[str], None] | None = None,
    ) -> None:
        if max_conflict_attempts < 1:
            raise ValueError("max conflict attempts must be positive")
        self.path = Path(settings_path)
        self._markers = frozenset(markers)
        self._owned_entries = {
            event_name: tuple(dict(entry) for entry in entries)
            for event_name, entries in owned_entries.items()
        }
        self._legacy_entries = {
            event_name: tuple(dict(entry) for entry in entries)
            for event_name, entries in (legacy_entries or {}).items()
        }
        self._accepted_entries = {
            event_name: tuple(
                dict(entry)
                for entry in (
                    self._legacy_entries.get(event_name, ())
                    + self._owned_entries.get(event_name, ())
                )
            )
            for event_name in (
                self._legacy_entries.keys() | self._owned_entries.keys()
            )
        }
        self._max_conflict_attempts = max_conflict_attempts
        self._phase_hook = phase_hook
        self._attempt_state = threading.local()
        self._transaction = AtomicJsonTransaction(
            self.path,
            purpose=purpose,
            phase_hook=self._transaction_phase,
        )

    def _snapshot(self) -> tuple[bool, bytes]:
        try:
            return True, self.path.read_bytes()
        except FileNotFoundError:
            return False, b""

    def _capture_snapshot(self, transaction_value: dict[str, Any]) -> None:
        snapshot = self._snapshot()
        if snapshot[0]:
            try:
                observed = json.loads(snapshot[1].decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise _ExternalSettingsConflict(
                    "assistant hook settings changed during update"
                ) from exc
            if not isinstance(observed, dict) or observed != transaction_value:
                raise _ExternalSettingsConflict(
                    "assistant hook settings changed during update"
                )
        elif transaction_value:
            raise _ExternalSettingsConflict(
                "assistant hook settings changed during update"
            )
        self._attempt_state.expected_snapshot = snapshot

    def _transaction_phase(self, phase: str) -> None:
        if phase != "before_replace":
            return
        if self._phase_hook is not None:
            self._phase_hook("before_external_conflict_check")
        expected = getattr(self._attempt_state, "expected_snapshot", None)
        if expected is None or self._snapshot() != expected:
            raise _ExternalSettingsConflict(
                "assistant hook settings changed during update"
            )

    def _matches_current_value(self, expected: dict[str, Any]) -> bool:
        exists, raw = self._snapshot()
        if not exists:
            return not expected
        try:
            observed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return False
        return isinstance(observed, dict) and observed == expected

    def _update_with_retry(
        self,
        operation: Callable[
            [dict[str, Any]], tuple[dict[str, Any], HookInspection]
        ],
        *,
        create_if_missing: bool = True,
    ) -> tuple[dict[str, Any], HookInspection | None]:
        for _attempt in range(self._max_conflict_attempts):
            result: list[HookInspection] = []

            def guarded(settings: dict[str, Any]) -> dict[str, Any]:
                self._capture_snapshot(settings)
                updated, inspection = operation(settings)
                result.append(inspection)
                return updated

            try:
                updated = self._transaction.update(
                    guarded, create_if_missing=create_if_missing
                )
            except _ExternalSettingsConflict:
                continue
            finally:
                if hasattr(self._attempt_state, "expected_snapshot"):
                    del self._attempt_state.expected_snapshot
            # Also catch a complete external replace immediately after ours.
            # This cannot make a portable filesystem CAS, but it narrows the
            # unobservable interval and merges a stable later writer on retry.
            if not self._matches_current_value(updated):
                continue
            return updated, result[0] if result else None
        raise HookSettingsError(
            "assistant hook settings changed too often to update safely"
        )

    def inspect(self) -> HookInspection:
        try:
            return _inspect_settings(
                self._transaction.read(),
                markers=self._markers,
                owned_entries=self._owned_entries,
                accepted_entries=self._accepted_entries,
            )
        except HookSettingsError:
            raise
        except (OSError, AtomicStorageError) as exc:
            raise HookSettingsError("assistant hook settings are unreadable") from exc

    def install(self) -> HookInspection:
        def merge(
            settings: dict[str, Any],
        ) -> tuple[dict[str, Any], HookInspection]:
            settings = copy.deepcopy(settings)
            inspection = _inspect_settings(
                settings,
                markers=self._markers,
                owned_entries=self._owned_entries,
                accepted_entries=self._accepted_entries,
            )
            if inspection.status is HookStatus.CONFLICT:
                raise HookSettingsError(
                    "owned hook marker has a conflicting settings entry"
                )
            if inspection.status is HookStatus.INSTALLED:
                return settings, inspection
            hooks = settings.setdefault("hooks", {})
            if not isinstance(hooks, dict):
                raise HookSettingsError("assistant hooks must be an object")
            for event_name, entries in self._accepted_entries.items():
                rules = hooks.setdefault(event_name, [])
                if not isinstance(rules, list):
                    raise HookSettingsError(
                        f"assistant {event_name} hooks must be a list"
                    )
                retained_rules: list[dict[str, Any]] = []
                for rule in rules:
                    commands = rule["hooks"]
                    retained = [
                        item
                        for item in commands
                        if not any(_owned_entry(item, entry) for entry in entries)
                    ]
                    if retained:
                        replacement = dict(rule)
                        replacement["hooks"] = retained
                        retained_rules.append(replacement)
                hooks[event_name] = retained_rules
                for entry in self._owned_entries.get(event_name, ()):
                    hooks[event_name].append({"hooks": [dict(entry)]})
            installed = HookInspection(
                HookStatus.INSTALLED,
                sum(len(entries) for entries in self._owned_entries.values()),
            )
            return settings, installed

        try:
            _updated, inspection = self._update_with_retry(merge)
        except HookSettingsError:
            raise
        except (OSError, AtomicStorageError) as exc:
            raise HookSettingsError("assistant hook settings update failed") from exc
        if inspection is None:
            raise HookSettingsError(
                "assistant hook settings update produced no result"
            )
        return inspection

    def uninstall(self) -> HookInspection:
        def remove(
            settings: dict[str, Any],
        ) -> tuple[dict[str, Any], HookInspection]:
            settings = copy.deepcopy(settings)
            inspection = _inspect_settings(
                settings,
                markers=self._markers,
                owned_entries=self._owned_entries,
                accepted_entries=self._accepted_entries,
            )
            if inspection.status is HookStatus.CONFLICT:
                raise HookSettingsError(
                    "owned hook marker has a conflicting settings entry"
                )
            if inspection.status is HookStatus.ABSENT:
                return settings, inspection
            hooks = settings["hooks"]
            for event_name, entries in self._owned_entries.items():
                rules = hooks.get(event_name)
                if not isinstance(rules, list):
                    continue
                owned = tuple(entries)
                retained_rules: list[dict[str, Any]] = []
                for rule in rules:
                    commands = rule["hooks"]
                    retained = [
                        item
                        for item in commands
                        if not any(_owned_entry(item, entry) for entry in owned)
                    ]
                    if retained:
                        replacement = dict(rule)
                        replacement["hooks"] = retained
                        retained_rules.append(replacement)
                if retained_rules:
                    hooks[event_name] = retained_rules
                else:
                    hooks.pop(event_name, None)
            if not hooks:
                settings.pop("hooks")
            absent = HookInspection(HookStatus.ABSENT, 0)
            return settings, absent

        try:
            updated, inspection = self._update_with_retry(
                remove, create_if_missing=False
            )
        except HookSettingsError:
            raise
        except (OSError, AtomicStorageError) as exc:
            raise HookSettingsError("assistant hook settings update failed") from exc
        return (
            inspection
            if inspection is not None
            else _inspect_settings(
                updated,
                markers=self._markers,
                owned_entries=self._owned_entries,
                accepted_entries=self._accepted_entries,
            )
        )


class SessionControlHookManager(_JsonHookManager):
    """Manage only the provider's deterministic TalkToMeJohnny session controls."""

    def __init__(
        self,
        settings_path: str | os.PathLike[str],
        *,
        provider: str,
        executable: str = "talktomejohnny",
        max_conflict_attempts: int = 8,
        phase_hook: Callable[[str], None] | None = None,
    ) -> None:
        if provider == "claude":
            marker = CLAUDE_SESSION_CONTROL_MARKER
        elif provider == "codex":
            marker = CODEX_SESSION_CONTROL_MARKER
        else:
            raise ValueError(f"unsupported assistant provider {provider!r}")
        super().__init__(
            settings_path,
            owned_entries=_session_control_entries(provider, executable),
            legacy_entries=_legacy_session_control_entries(provider),
            markers=(marker,),
            purpose=f"{provider}-session-control-hook-settings",
            max_conflict_attempts=max_conflict_attempts,
            phase_hook=phase_hook,
        )


class ClaudeHookManager(_JsonHookManager):
    """Merge the owned Claude Code Stop hook without replacing other settings."""

    def __init__(
        self,
        settings_path: str | os.PathLike[str],
        *,
        executable: str = "talktomeclaude",
        max_conflict_attempts: int = 8,
        phase_hook: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            settings_path,
            owned_entries={
                "Stop": _stop_hook_entries("claude", executable),
                **_session_control_entries("claude", executable),
            },
            legacy_entries={
                **_legacy_stop_entries("claude"),
                **_legacy_session_control_entries("claude"),
            },
            markers=(OWNED_HOOK_MARKER, CLAUDE_SESSION_CONTROL_MARKER),
            purpose="claude-hook-settings",
            max_conflict_attempts=max_conflict_attempts,
            phase_hook=phase_hook,
        )


class CodexHookManager(_JsonHookManager):
    """Merge the owned Codex Stop hook without replacing other hook sources."""

    def __init__(
        self,
        settings_path: str | os.PathLike[str],
        *,
        executable: str = "talktomeclaude",
        max_conflict_attempts: int = 8,
        phase_hook: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            settings_path,
            owned_entries={
                "Stop": _stop_hook_entries("codex", executable),
                **_session_control_entries("codex", executable),
            },
            legacy_entries={
                **_legacy_stop_entries("codex"),
                **_legacy_session_control_entries("codex"),
            },
            markers=(CODEX_OWNED_HOOK_MARKER, CODEX_SESSION_CONTROL_MARKER),
            purpose="codex-hook-settings",
            max_conflict_attempts=max_conflict_attempts,
            phase_hook=phase_hook,
        )
