"""Idempotent ownership-scoped assistant Stop-hook JSON merges."""

from __future__ import annotations

import copy
import json
import os
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
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    hooks = settings.get("hooks")
    if hooks is None:
        return []
    if not isinstance(hooks, dict):
        raise HookSettingsError("assistant hooks must be an object")
    stop = hooks.get("Stop")
    if stop is None:
        return []
    if not isinstance(stop, list):
        raise HookSettingsError("assistant Stop hooks must be a list")
    found: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for rule in stop:
        if not isinstance(rule, dict):
            raise HookSettingsError("assistant Stop hook rule must be an object")
        commands = rule.get("hooks")
        if not isinstance(commands, list):
            raise HookSettingsError("assistant Stop hook commands must be a list")
        for command in commands:
            if not isinstance(command, dict):
                raise HookSettingsError(
                    "assistant Stop hook command must be an object"
                )
            found.append((rule, command))
    return found


def _inspect_settings(
    settings: dict[str, Any], *, marker: str, owned_entry: dict[str, str]
) -> HookInspection:
    entries = _command_entries(settings)
    owned_count = sum(
        _owned_entry(item, owned_entry) for _rule, item in entries
    )
    conflict = any(
        _marker_conflict(item, marker, owned_entry) for _rule, item in entries
    )
    if conflict or owned_count > 1:
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
        marker: str,
        command: str,
        purpose: str,
        max_conflict_attempts: int = 8,
        phase_hook: Callable[[str], None] | None = None,
    ) -> None:
        if max_conflict_attempts < 1:
            raise ValueError("max conflict attempts must be positive")
        self.path = Path(settings_path)
        self._marker = marker
        self._owned_entry = {"type": "command", "command": command}
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
                marker=self._marker,
                owned_entry=self._owned_entry,
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
                marker=self._marker,
                owned_entry=self._owned_entry,
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
            stop = hooks.setdefault("Stop", [])
            if not isinstance(stop, list):
                raise HookSettingsError("assistant Stop hooks must be a list")
            stop.append({"hooks": [dict(self._owned_entry)]})
            installed = HookInspection(HookStatus.INSTALLED, 1)
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
                marker=self._marker,
                owned_entry=self._owned_entry,
            )
            if inspection.status is HookStatus.CONFLICT:
                raise HookSettingsError(
                    "owned hook marker has a conflicting settings entry"
                )
            if inspection.status is HookStatus.ABSENT:
                return settings, inspection
            hooks = settings["hooks"]
            stop = hooks["Stop"]
            retained_rules: list[dict[str, Any]] = []
            for rule in stop:
                commands = rule["hooks"]
                retained = [
                    item
                    for item in commands
                    if not _owned_entry(item, self._owned_entry)
                ]
                if retained:
                    replacement = dict(rule)
                    replacement["hooks"] = retained
                    retained_rules.append(replacement)
            if retained_rules:
                hooks["Stop"] = retained_rules
            else:
                hooks.pop("Stop")
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
                marker=self._marker,
                owned_entry=self._owned_entry,
            )
        )


class ClaudeHookManager(_JsonHookManager):
    """Merge the owned Claude Code Stop hook without replacing other settings."""

    def __init__(
        self,
        settings_path: str | os.PathLike[str],
        *,
        max_conflict_attempts: int = 8,
        phase_hook: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            settings_path,
            marker=OWNED_HOOK_MARKER,
            command=CLAUDE_STOP_HOOK_COMMAND,
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
        max_conflict_attempts: int = 8,
        phase_hook: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            settings_path,
            marker=CODEX_OWNED_HOOK_MARKER,
            command=CODEX_STOP_HOOK_COMMAND,
            purpose="codex-hook-settings",
            max_conflict_attempts=max_conflict_attempts,
            phase_hook=phase_hook,
        )
