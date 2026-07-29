"""Deterministic in-session attachment commands for supported assistant CLIs."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from talktomeclaude.assistant.attachment import (
    ProviderName,
    SessionAttachmentError,
    SessionAttachmentRegistry,
)

MAX_SESSION_CONTROL_INPUT_BYTES = 1024 * 1024
_CODEX_COMMAND = re.compile(
    r"^\$(?:talktomejohnny|talktomeclaude)(?:\s+(on|off|status))?\s*$",
    re.IGNORECASE,
)
_CLAUDE_COMMANDS = frozenset({"talktomejohnny", "talktomeclaude"})
_PROVIDER_LABELS = {"claude": "Claude Code", "codex": "Codex CLI"}


class LiveLeaseHeartbeatOwner:
    """Keep one provider lease live for exactly one companion transport owner."""

    def __init__(
        self,
        registry: SessionAttachmentRegistry,
        provider: ProviderName,
        *,
        interval_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 1.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("lease heartbeat interval must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("lease shutdown timeout must be positive")
        self._registry = registry
        self._provider = provider
        self._interval = interval_seconds
        self._shutdown_timeout = shutdown_timeout_seconds
        self._stopping = threading.Event()
        self._lease = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stopping.clear()
            lease = self._registry.open_live_lease(self._provider)
            self._lease = lease
            thread = threading.Thread(
                target=self._run,
                name=f"ttj-{self._provider}-lease-heartbeat",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def _run(self) -> None:
        while not self._stopping.wait(self._interval):
            lease = self._lease
            if lease is None:
                return
            try:
                if lease.heartbeat() is None:
                    return
            except (SessionAttachmentError, OSError, ValueError):
                return

    def stop(self) -> bool:
        with self._lock:
            thread = self._thread
            lease = self._lease
            self._thread = None
            self._lease = None
        if thread is None:
            return True
        self._stopping.set()
        thread.join(self._shutdown_timeout)
        try:
            if lease is not None:
                lease.close()
        except (SessionAttachmentError, OSError, ValueError):
            return False
        return not thread.is_alive()

    def __enter__(self) -> "LiveLeaseHeartbeatOwner":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def attachment_state_path(environment: Mapping[str, str] | None = None) -> Path:
    """Return the shared content-free attachment state path."""

    active_environment = os.environ if environment is None else environment
    override = active_environment.get(
        "TALKTOMEJOHNNY_ATTACHMENT_STATE"
    ) or active_environment.get("TALKTOMECLAUDE_ATTACHMENT_STATE")
    if override:
        return Path(override).expanduser()
    from talktomeclaude.config import config_dir

    return config_dir() / "assistant-attachment.json"


def read_session_control_event(
    stream: TextIO, *, max_bytes: int = MAX_SESSION_CONTROL_INPUT_BYTES
) -> dict[str, Any] | None:
    """Read one bounded hook event without retaining its prompt content."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    try:
        raw = stream.read(max_bytes + 1)
        if len(raw.encode("utf-8", errors="strict")) > max_bytes:
            return None
        value = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _session_id(event: Mapping[str, Any]) -> str | None:
    value = event.get("session_id")
    if not isinstance(value, str) or not value:
        return None
    if len(value) > 256 or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        return None
    return value


def _requested_action(
    event: Mapping[str, Any], provider: ProviderName
) -> str | None:
    event_name = event.get("hook_event_name")
    if provider == "codex":
        if event_name != "UserPromptSubmit":
            return None
        prompt = event.get("prompt")
        if not isinstance(prompt, str):
            return None
        matched = _CODEX_COMMAND.fullmatch(prompt)
        return (matched.group(1) or "status").casefold() if matched else None

    if event_name != "UserPromptExpansion":
        return None
    command_name = event.get("command_name")
    if not isinstance(command_name, str) or command_name.casefold() not in _CLAUDE_COMMANDS:
        return None
    arguments = event.get("command_args", "")
    if not isinstance(arguments, str):
        return "invalid"
    normalized = arguments.strip().casefold()
    return normalized or "status"


def _blocked(message: str) -> dict[str, str]:
    return {"decision": "block", "reason": message}


def handle_session_control_event(
    event: Mapping[str, Any],
    *,
    provider: ProviderName,
    registry: SessionAttachmentRegistry,
) -> dict[str, str] | None:
    """Apply one exact-session attach command or SessionEnd cleanup.

    ``None`` means the hook did not recognize a TalkToMeJohnny control command
    and the assistant CLI should continue normally.
    """

    session_id = _session_id(event)
    if session_id is None:
        return None
    if event.get("hook_event_name") == "SessionEnd":
        try:
            registry.detach(provider, session_id)
        except (SessionAttachmentError, ValueError):
            pass
        return None

    action = _requested_action(event, provider)
    if action is None:
        return None
    label = _PROVIDER_LABELS[provider]
    if action not in {"on", "off", "status"}:
        return _blocked("Use TalkToMeJohnny with: on, off, or status.")

    try:
        if action == "on":
            live_lease = registry.live_lease(provider)
            if live_lease is None:
                return _blocked(
                    "TalkToMeJohnny is offline. Start the companion, then try again."
                )
            registry.attach(provider, session_id, live_lease.lease_id)
            return _blocked(f"TalkToMeJohnny is attached to this {label} session.")

        if action == "off":
            detached = registry.detach(provider, session_id)
            state = "detached" if detached else "not attached"
            return _blocked(f"This {label} session is {state}.")

        attachment = registry.attachment()
        active = bool(
            attachment is not None
            and attachment.provider == provider
            and attachment.session_id == session_id
            and registry.active(provider, session_id, attachment.lease_id)
        )
        state = "attached" if active else "not attached"
        return _blocked(f"This {label} session is {state}.")
    except (SessionAttachmentError, ValueError):
        return _blocked(
            "TalkToMeJohnny could not update its attachment safely; it remains off."
        )


def session_is_attached(
    event: Mapping[str, Any],
    *,
    provider: ProviderName,
    registry: SessionAttachmentRegistry,
) -> bool:
    """Fail closed unless *event* belongs to the exact current live attachment."""

    session_id = _session_id(event)
    if session_id is None:
        return False
    try:
        attachment = registry.attachment()
        return bool(
            attachment is not None
            and attachment.provider == provider
            and attachment.session_id == session_id
            and registry.active(provider, session_id, attachment.lease_id)
        )
    except (SessionAttachmentError, ValueError):
        return False


__all__ = [
    "LiveLeaseHeartbeatOwner",
    "MAX_SESSION_CONTROL_INPUT_BYTES",
    "attachment_state_path",
    "handle_session_control_event",
    "read_session_control_event",
    "session_is_attached",
]
