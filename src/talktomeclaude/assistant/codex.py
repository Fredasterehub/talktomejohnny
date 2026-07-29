"""Codex Stop-hook payload translation into the durable reply spool.

The current Codex hook contract delivers one Stop-event object containing:

- ``hook_event_name`` set to ``"Stop"``
- ``session_id``
- ``turn_id``
- ``last_assistant_message``

This module translates that provider-specific shape into the canonical
``ReplyEvent`` contract and persists it through ``ReplySpool``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from talktomeclaude.reply import (
    ReplyEvent,
    ReplyProtocolError,
    ReplySpool,
    ReplySpoolError,
    SpoolRecord,
)

_EVENT_ID_PREFIX = "codex-stop-"


class CodexStopErrorCode(StrEnum):
    INVALID_ROOT = "invalid_root"
    INVALID_EVENT_NAME = "invalid_event_name"
    INVALID_SESSION = "invalid_session"
    INVALID_TURN_ID = "invalid_turn_id"
    INVALID_ANSWER = "invalid_answer"
    TRANSPORT_FAILED = "transport_failed"


_ERROR_MESSAGES = {
    CodexStopErrorCode.INVALID_ROOT: "Codex Stop payload must be an object",
    CodexStopErrorCode.INVALID_EVENT_NAME: "Codex hook event must be Stop",
    CodexStopErrorCode.INVALID_SESSION: "Codex Stop payload session_id is invalid",
    CodexStopErrorCode.INVALID_TURN_ID: "Codex Stop payload turn_id is invalid",
    CodexStopErrorCode.INVALID_ANSWER: (
        "Codex Stop payload last_assistant_message is invalid"
    ),
    CodexStopErrorCode.TRANSPORT_FAILED: "Codex Stop transport could not persist reply",
}


class CodexStopPayloadError(ValueError):
    """A Codex Stop payload is structurally invalid."""

    def __init__(self, code: CodexStopErrorCode) -> None:
        if code is CodexStopErrorCode.TRANSPORT_FAILED:
            raise ValueError("transport failures must use CodexStopTransportError")
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


class CodexStopTransportError(RuntimeError):
    """Reply-spool persistence failed for an otherwise valid Codex Stop event."""

    def __init__(self) -> None:
        self.code = CodexStopErrorCode.TRANSPORT_FAILED
        super().__init__(_ERROR_MESSAGES[self.code])


def _validated_session_id(value: object) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 256):
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_SESSION)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_SESSION)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_SESSION) from exc
    return value


def _canonical_turn_id(value: object) -> str:
    if isinstance(value, bool):
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_TURN_ID)
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str) or not value:
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_TURN_ID)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_TURN_ID)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_TURN_ID) from exc
    return value


def _validated_answer(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_ANSWER)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_ANSWER) from exc
    return value


def codex_stop_event_id(
    session_id: str, turn_id: str | int, answer: str
) -> str:
    """Derive the durable identifier for one exact Codex Stop emission.

    Codex can emit ``Stop`` more than once for a turn when another additive
    hook requests continuation. Binding the answer keeps exact replays
    idempotent while allowing the later authoritative message to be retained.
    """

    session = _validated_session_id(session_id)
    turn = _canonical_turn_id(turn_id)
    message = _validated_answer(answer)
    digest = hashlib.sha256(
        f"{session}\0{turn}\0{message}".encode("utf-8", errors="strict")
    )
    return f"{_EVENT_ID_PREFIX}{digest.hexdigest()}"


def translate_stop_event(payload: Mapping[str, Any]) -> ReplyEvent | None:
    """Translate one Codex Stop payload into the canonical ``ReplyEvent``."""

    if not isinstance(payload, Mapping):
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_ROOT)
    if payload.get("hook_event_name") != "Stop":
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_EVENT_NAME)

    session_id = _validated_session_id(payload.get("session_id"))
    turn_id = _canonical_turn_id(payload.get("turn_id"))
    if "last_assistant_message" not in payload:
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_ANSWER)
    raw_answer = payload["last_assistant_message"]
    if raw_answer is None:
        return None
    answer = _validated_answer(raw_answer)
    event_id = codex_stop_event_id(session_id, turn_id, answer)
    try:
        # Round-tripping through the canonical wire validator guarantees the
        # derived digest and JSON shape match the durable reply contract.
        return ReplyEvent.from_bytes(
            ReplyEvent.create(
                session=session_id,
                event_id=event_id,
                answer=answer,
            ).to_bytes()
        )
    except ReplyProtocolError as exc:
        raise CodexStopPayloadError(CodexStopErrorCode.INVALID_ANSWER) from exc


def transport_stop_event(
    payload: Mapping[str, Any], *, spool_root: str | Path
) -> SpoolRecord | None:
    """Translate and persist one Codex Stop payload to ``ReplySpool``."""

    event = translate_stop_event(payload)
    if event is None:
        return None
    try:
        return ReplySpool(spool_root).enqueue(event)
    except (OSError, ReplySpoolError) as exc:
        raise CodexStopTransportError() from exc
