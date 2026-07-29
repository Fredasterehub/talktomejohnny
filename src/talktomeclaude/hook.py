"""Claude Code Stop-hook event handling.

A Stop event arrives as one JSON object on stdin. Per the platform contract,
the text of Claude's final reply is taken from ``last_assistant_message`` —
never recovered from ``transcript_path``, which is written asynchronously
and may not yet contain the final message when the hook fires.
"""

import json
import os
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from talktomeclaude.storage import AtomicJsonTransaction
from talktomeclaude.transcript import speakable

if TYPE_CHECKING:
    from talktomeclaude.assistant import AssistantEventResult

MAX_STOP_INPUT_BYTES = 8 * 1024 * 1024


def _transport_root(environment: Mapping[str, str]) -> Path:
    override = environment.get(
        "TALKTOMEJOHNNY_REPLY_SPOOL"
    ) or environment.get("TALKTOMECLAUDE_REPLY_SPOOL")
    if override:
        return Path(override).expanduser()
    from talktomeclaude.config import config_dir

    return config_dir() / "reply-spool"


def _fault_path(environment: Mapping[str, str]) -> Path:
    override = environment.get(
        "TALKTOMEJOHNNY_CONFIG_DIR"
    ) or environment.get("TALKTOMECLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser() / "hook-transport-status.json"
    from talktomeclaude.config import config_dir

    return config_dir() / "hook-transport-status.json"


def record_transport_fault(
    code: str, *, environment: Mapping[str, str] | None = None
) -> None:
    """Persist a content-free hook fault counter for later diagnostics."""

    active_environment = os.environ if environment is None else environment
    transaction = AtomicJsonTransaction(
        _fault_path(active_environment), purpose="hook-transport-status"
    )

    def increment(current: dict[str, object]) -> dict[str, object]:
        count = current.get("failure_count", 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            count = 0
        return {
            "version": 1,
            "failure_count": count + 1,
            "last_code": code,
        }

    transaction.update(increment)


def transport_fault_status(
    *, environment: Mapping[str, str] | None = None
) -> dict[str, object]:
    """Read the content-free durable hook fault status."""

    active_environment = os.environ if environment is None else environment
    return AtomicJsonTransaction(
        _fault_path(active_environment), purpose="hook-transport-status"
    ).read()


def read_stop_event(
    stream: TextIO, *, max_bytes: int = MAX_STOP_INPUT_BYTES
) -> dict | None:
    """Parse the hook event JSON from *stream*; None if it is unusable."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    try:
        raw = stream.read(max_bytes + 1)
        encoded = raw.encode("utf-8", errors="strict")
        if len(encoded) > max_bytes:
            return None
        event = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        return None
    return event if isinstance(event, dict) else None


def stop_dialogue(event: dict) -> str:
    """Speakable dialogue of the turn's final assistant message.

    Empty when the event carries no ``last_assistant_message`` (older
    Claude Code versions, or turns that ended without a reply) or when
    nothing speakable remains after filtering.
    """
    message = event.get("last_assistant_message")
    if not isinstance(message, str):
        return ""
    return speakable(message)


def transport_stop_event(
    event: dict,
    *,
    environment: Mapping[str, str] | None = None,
    event_id_factory: Callable[[], str] | None = None,
) -> "AssistantEventResult | None":
    """Validate and durably spool one authoritative Claude Stop event.

    This is the composition boundary between Claude-specific semantics and the
    neutral reply spool.  It deliberately retains the exact assistant message;
    speakable filtering belongs to the later speech presentation.
    """

    if event.get("hook_event_name") != "Stop":
        return None
    session = event.get("session_id")
    answer = event.get("last_assistant_message")
    if not isinstance(session, str) or not isinstance(answer, str) or not answer:
        return None

    from talktomeclaude.assistant import (
        ClaudeCodeAdapter,
        SuppressionRegistry,
        canonical_reply_digest,
    )
    from talktomeclaude.reply import ReplyEvent, ReplySpool

    active_environment = os.environ if environment is None else environment
    root = _transport_root(active_environment)
    identifier = (event_id_factory or (lambda: uuid.uuid4().hex))()
    wire = json.dumps(
        {
            "answer": answer,
            "digest": canonical_reply_digest(
                version=1,
                session=session,
                event_id=identifier,
                answer=answer,
            ),
            "event_id": identifier,
            "session": session,
            "version": 1,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    suppression = SuppressionRegistry(root.parent / "director-suppression.json")
    adapter = ClaudeCodeAdapter(suppression)

    def publish(validated: object) -> None:
        reply = ReplyEvent(
            version=getattr(validated, "version"),
            session=getattr(validated, "session"),
            event_id=getattr(validated, "event_id"),
            answer=getattr(validated, "answer"),
            digest=getattr(validated, "digest"),
        )
        ReplySpool(root).enqueue(reply)

    return adapter.handle(wire, publish, environment=active_environment)
