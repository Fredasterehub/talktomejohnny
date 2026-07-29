"""Atomic session attachment and live companion lease state."""

from __future__ import annotations

import math
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from talktomeclaude.storage import AtomicJsonTransaction, AtomicStorageError

ATTACHMENT_STATE_VERSION = 2
DEFAULT_LIVE_LEASE_TTL_SECONDS = 30.0
_LEGACY_STATE_VERSION = 1
_LEGACY_ROOT_KEYS = frozenset({"version", "attachment", "live_lease"})
_ROOT_KEYS = frozenset({"version", "attachment", "live_leases"})
_LEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PROVIDERS = frozenset({"claude", "codex"})

ProviderName = Literal["claude", "codex"]


class SessionAttachmentError(RuntimeError):
    """Durable session attachment state cannot be safely read or written."""


@dataclass(frozen=True, slots=True)
class SessionAttachment:
    provider: ProviderName
    session_id: str
    lease_id: str


@dataclass(frozen=True, slots=True)
class LiveLeaseState:
    provider: ProviderName
    lease_id: str
    pid: int
    heartbeat_at: float


@dataclass(frozen=True, slots=True)
class _AttachmentState:
    attachment: SessionAttachment | None
    live_leases: dict[ProviderName, LiveLeaseState]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": ATTACHMENT_STATE_VERSION,
            "attachment": (
                None
                if self.attachment is None
                else {
                    "provider": self.attachment.provider,
                    "session_id": self.attachment.session_id,
                    "lease_id": self.attachment.lease_id,
                }
            ),
            "live_leases": {
                provider: {
                    "provider": live_lease.provider,
                    "lease_id": live_lease.lease_id,
                    "pid": live_lease.pid,
                    "heartbeat_at": live_lease.heartbeat_at,
                }
                for provider, live_lease in sorted(self.live_leases.items())
            },
        }


def _invalid(field: str) -> ValueError:
    return ValueError(f"assistant {field} is invalid")


def _state_error(message: str = "assistant attachment state is invalid") -> SessionAttachmentError:
    return SessionAttachmentError(message)


def _validated_provider(value: object) -> ProviderName:
    if value not in _PROVIDERS:
        raise _invalid("provider")
    return value


def _validated_session_id(value: object) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 256):
        raise _invalid("session_id")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise _invalid("session_id")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise _invalid("session_id") from exc
    return value


def _validated_lease_id(value: object) -> str:
    if not isinstance(value, str) or _LEASE_ID.fullmatch(value) is None:
        raise _invalid("lease_id")
    return value


def _validated_pid(value: object) -> int:
    if type(value) is not int or value < 1:
        raise _invalid("pid")
    return value


def _validated_heartbeat(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _state_error("assistant live lease is invalid")
    heartbeat = float(value)
    if not math.isfinite(heartbeat) or heartbeat < 0:
        raise _state_error("assistant live lease is invalid")
    return heartbeat


def _parsed_attachment(value: object) -> SessionAttachment:
    if not isinstance(value, dict):
        raise _state_error()
    try:
        return SessionAttachment(
            provider=_validated_provider(value.get("provider")),
            session_id=_validated_session_id(value.get("session_id")),
            lease_id=_validated_lease_id(value.get("lease_id")),
        )
    except ValueError as exc:
        raise _state_error() from exc


def _parsed_live_lease(value: object) -> LiveLeaseState:
    if not isinstance(value, dict):
        raise _state_error("assistant live lease is invalid")
    try:
        return LiveLeaseState(
            provider=_validated_provider(value.get("provider")),
            lease_id=_validated_lease_id(value.get("lease_id")),
            pid=_validated_pid(value.get("pid")),
            heartbeat_at=_validated_heartbeat(value.get("heartbeat_at")),
        )
    except ValueError as exc:
        raise _state_error("assistant live lease is invalid") from exc


def _empty_state() -> _AttachmentState:
    return _AttachmentState(attachment=None, live_leases={})


def _parsed_live_lease_map(
    value: object, *, tolerate_malformed_live_leases: bool
) -> dict[ProviderName, LiveLeaseState]:
    if not isinstance(value, dict):
        raise _state_error("assistant live lease state is invalid")
    live_leases: dict[ProviderName, LiveLeaseState] = {}
    for key, raw_lease in value.items():
        if key not in _PROVIDERS:
            if tolerate_malformed_live_leases:
                continue
            raise _state_error("assistant live lease state is invalid")
        try:
            parsed = _parsed_live_lease(raw_lease)
        except SessionAttachmentError:
            if tolerate_malformed_live_leases:
                continue
            raise
        if parsed.provider != key:
            if tolerate_malformed_live_leases:
                continue
            raise _state_error("assistant live lease state is invalid")
        live_leases[key] = parsed
    return live_leases


def _parsed_legacy_live_lease_map(
    value: object, *, tolerate_malformed_live_leases: bool
) -> dict[ProviderName, LiveLeaseState]:
    if value is None:
        return {}
    try:
        lease = _parsed_live_lease(value)
    except SessionAttachmentError:
        if tolerate_malformed_live_leases:
            return {}
        raise
    return {lease.provider: lease}


def _parsed_state(
    value: dict[str, Any], *, tolerate_malformed_live_leases: bool
) -> _AttachmentState:
    if not value:
        return _empty_state()
    version = value.get("version")
    if version == _LEGACY_STATE_VERSION:
        if frozenset(value) - _LEGACY_ROOT_KEYS:
            raise _state_error()
        live_leases = _parsed_legacy_live_lease_map(
            value.get("live_lease"),
            tolerate_malformed_live_leases=tolerate_malformed_live_leases,
        )
    elif version == ATTACHMENT_STATE_VERSION:
        if frozenset(value) - _ROOT_KEYS:
            raise _state_error()
        live_leases = _parsed_live_lease_map(
            value.get("live_leases", {}),
            tolerate_malformed_live_leases=tolerate_malformed_live_leases,
        )
    else:
        raise _state_error()
    attachment_raw = value.get("attachment")
    attachment = (
        None if attachment_raw is None else _parsed_attachment(attachment_raw)
    )
    return _AttachmentState(attachment=attachment, live_leases=live_leases)


@dataclass(slots=True)
class LiveCompanionLease:
    """Owner handle for the current live companion heartbeat."""

    registry: "SessionAttachmentRegistry"
    provider: ProviderName
    lease_id: str
    pid: int
    _closed: bool = field(default=False, init=False, repr=False)
    _close_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def attach(self, session_id: str) -> SessionAttachment:
        return self.registry.attach(self.provider, session_id, self.lease_id)

    def active(self, session_id: str) -> bool:
        return self.registry.active(self.provider, session_id, self.lease_id)

    def heartbeat(self) -> LiveLeaseState | None:
        return self.registry.refresh_live_lease(
            self.provider, self.lease_id, pid=self.pid
        )

    def close(self) -> bool:
        with self._close_lock:
            if self._closed:
                return False
            closed = self.registry.close_live_lease(
                self.provider, self.lease_id, pid=self.pid
            )
            self._closed = True
            return closed


class SessionAttachmentRegistry:
    """Atomic attachment state for one active assistant session and live leases."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = time.time,
        live_lease_ttl_seconds: float = DEFAULT_LIVE_LEASE_TTL_SECONDS,
        lock_timeout: float = 5.0,
    ) -> None:
        if live_lease_ttl_seconds < 0:
            raise ValueError("live_lease_ttl_seconds cannot be negative")
        self.path = Path(path)
        self._clock = clock
        self._live_lease_ttl_seconds = live_lease_ttl_seconds
        self._transaction = AtomicJsonTransaction(
            self.path, timeout=lock_timeout, purpose="assistant-attachment"
        )

    def _read_state(
        self, *, tolerate_malformed_live_leases: bool
    ) -> _AttachmentState:
        try:
            return _parsed_state(
                self._transaction.read(),
                tolerate_malformed_live_leases=tolerate_malformed_live_leases,
            )
        except SessionAttachmentError:
            raise
        except (OSError, AtomicStorageError) as exc:
            raise _state_error("assistant attachment state is unreadable") from exc

    def _live_lease_if_current(
        self, state: _AttachmentState, provider: ProviderName
    ) -> LiveLeaseState | None:
        live_lease = state.live_leases.get(provider)
        if live_lease is None:
            return None
        if self._clock() - live_lease.heartbeat_at > self._live_lease_ttl_seconds:
            return None
        return live_lease

    def attachment(self) -> SessionAttachment | None:
        return self._read_state(tolerate_malformed_live_leases=True).attachment

    def live_lease(self, provider: ProviderName) -> LiveLeaseState | None:
        live_provider = _validated_provider(provider)
        state = self._read_state(tolerate_malformed_live_leases=True)
        return self._live_lease_if_current(state, live_provider)

    def live_leases(self) -> dict[ProviderName, LiveLeaseState]:
        state = self._read_state(tolerate_malformed_live_leases=True)
        return {
            provider: live_lease
            for provider, live_lease in state.live_leases.items()
            if self._live_lease_if_current(state, provider) is not None
        }

    def attach(
        self, provider: ProviderName, session_id: str, lease_id: str
    ) -> SessionAttachment:
        attachment = SessionAttachment(
            provider=_validated_provider(provider),
            session_id=_validated_session_id(session_id),
            lease_id=_validated_lease_id(lease_id),
        )

        def update(current: dict[str, Any]) -> dict[str, Any]:
            state = _parsed_state(current, tolerate_malformed_live_leases=True)
            return _AttachmentState(
                attachment=attachment,
                live_leases=state.live_leases,
            ).to_dict()

        try:
            self._transaction.update(update)
        except SessionAttachmentError:
            raise
        except (OSError, AtomicStorageError) as exc:
            raise _state_error("assistant attachment update failed") from exc
        return attachment

    def detach(self, provider: ProviderName, session_id: str) -> bool:
        expected_provider = _validated_provider(provider)
        expected_session = _validated_session_id(session_id)
        detached = False

        def update(current: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal detached
            state = _parsed_state(current, tolerate_malformed_live_leases=True)
            attachment = state.attachment
            if (
                attachment is None
                or attachment.provider != expected_provider
                or attachment.session_id != expected_session
            ):
                return state.to_dict()
            detached = True
            return _AttachmentState(
                attachment=None,
                live_leases=state.live_leases,
            ).to_dict()

        try:
            self._transaction.update(update, create_if_missing=False)
        except SessionAttachmentError:
            raise
        except (OSError, AtomicStorageError) as exc:
            raise _state_error("assistant attachment update failed") from exc
        return detached

    def active(self, provider: ProviderName, session_id: str, lease_id: str) -> bool:
        expected = SessionAttachment(
            provider=_validated_provider(provider),
            session_id=_validated_session_id(session_id),
            lease_id=_validated_lease_id(lease_id),
        )
        state = self._read_state(tolerate_malformed_live_leases=True)
        if state.attachment != expected:
            return False
        live_lease = self._live_lease_if_current(state, expected.provider)
        return (
            live_lease is not None
            and live_lease.lease_id == expected.lease_id
        )

    def open_live_lease(
        self, provider: ProviderName, *, pid: int | None = None
    ) -> LiveCompanionLease:
        live_provider = _validated_provider(provider)
        live_pid = _validated_pid(os.getpid() if pid is None else pid)
        lease_id = f"lease-{secrets.token_hex(16)}"
        now = self._clock()
        replacement = LiveLeaseState(
            provider=live_provider,
            lease_id=lease_id,
            pid=live_pid,
            heartbeat_at=now,
        )

        def update(current: dict[str, Any]) -> dict[str, Any]:
            state = _parsed_state(current, tolerate_malformed_live_leases=True)
            live_leases = dict(state.live_leases)
            live_leases[live_provider] = replacement
            return _AttachmentState(
                attachment=state.attachment,
                live_leases=live_leases,
            ).to_dict()

        try:
            self._transaction.update(update)
        except SessionAttachmentError:
            raise
        except (OSError, AtomicStorageError) as exc:
            raise _state_error("assistant live lease update failed") from exc
        return LiveCompanionLease(self, live_provider, lease_id, live_pid)

    def refresh_live_lease(
        self, provider: ProviderName, lease_id: str, *, pid: int
    ) -> LiveLeaseState | None:
        live_provider = _validated_provider(provider)
        live_lease_id = _validated_lease_id(lease_id)
        live_pid = _validated_pid(pid)
        now = self._clock()
        refreshed: LiveLeaseState | None = None

        def update(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal refreshed
            state = _parsed_state(current, tolerate_malformed_live_leases=True)
            current_lease = state.live_leases.get(live_provider)
            if (
                current_lease is None
                or current_lease.lease_id != live_lease_id
                or current_lease.pid != live_pid
            ):
                return state.to_dict()
            refreshed = LiveLeaseState(
                provider=live_provider,
                lease_id=live_lease_id,
                pid=live_pid,
                heartbeat_at=now,
            )
            live_leases = dict(state.live_leases)
            live_leases[live_provider] = refreshed
            return _AttachmentState(
                attachment=state.attachment,
                live_leases=live_leases,
            ).to_dict()

        try:
            self._transaction.update(update, create_if_missing=False)
        except SessionAttachmentError:
            raise
        except (OSError, AtomicStorageError) as exc:
            raise _state_error("assistant live lease update failed") from exc
        return refreshed

    def close_live_lease(
        self, provider: ProviderName, lease_id: str, *, pid: int
    ) -> bool:
        live_provider = _validated_provider(provider)
        live_lease_id = _validated_lease_id(lease_id)
        live_pid = _validated_pid(pid)
        closed = False

        def update(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal closed
            state = _parsed_state(current, tolerate_malformed_live_leases=True)
            current_lease = state.live_leases.get(live_provider)
            if (
                current_lease is None
                or current_lease.lease_id != live_lease_id
                or current_lease.pid != live_pid
            ):
                return state.to_dict()
            closed = True
            live_leases = dict(state.live_leases)
            live_leases.pop(live_provider, None)
            return _AttachmentState(
                attachment=state.attachment,
                live_leases=live_leases,
            ).to_dict()

        try:
            self._transaction.update(update, create_if_missing=False)
        except SessionAttachmentError:
            raise
        except (OSError, AtomicStorageError) as exc:
            raise _state_error("assistant live lease update failed") from exc
        return closed
