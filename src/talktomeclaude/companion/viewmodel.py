"""Pure mapping from companion state to semantic presentation text."""

from __future__ import annotations

import math
from dataclasses import dataclass

from talktomeclaude.companion.contracts import CompanionSnapshot
from talktomeclaude.core import EventKind, RuntimePhase, legal_events


@dataclass(frozen=True, slots=True)
class CompanionViewModel:
    """Display data shared by graphical and headless presentations.

    ``cue`` is a textual, non-color state indicator.  Runtime state updates
    never request focus; a future settings window may be opened only from its
    direct user intent.
    """

    phase: RuntimePhase
    cue: str
    status: str
    detail: str
    can_start_recording: bool
    can_finish_recording: bool
    microphone_level: str
    focus_requested: bool = False


_PRESENTATION = {
    RuntimePhase.IDLE: ("IDLE", "Companion ready"),
    RuntimePhase.RECORDING: ("RECORDING", "Recording"),
    RuntimePhase.TRANSCRIBING: ("TRANSCRIBING", "Transcribing speech"),
    RuntimePhase.AWAITING_CONFIRMATION: ("CONFIRM", "Review transcript"),
    RuntimePhase.DELIVERING: ("DELIVERING", "Delivering transcript"),
    RuntimePhase.WAITING_FOR_CLAUDE: ("WAITING", "Waiting for assistant"),
    RuntimePhase.PLANNING: ("PLANNING", "Preparing spoken reply"),
    RuntimePhase.SPEAKING: ("SPEAKING", "Speaking reply"),
    RuntimePhase.PAUSED: ("PAUSED", "Speech paused"),
    RuntimePhase.STOPPING: ("STOPPING", "Stopping companion"),
    RuntimePhase.DISCONNECTED: ("DISCONNECTED", "Assistant connection interrupted"),
    RuntimePhase.RECOVERABLE_ERROR: ("ERROR", "Companion needs attention"),
}


def to_view_model(snapshot: CompanionSnapshot) -> CompanionViewModel:
    """Map a content-safe runtime snapshot to stable display data."""

    runtime = snapshot.runtime
    cue, status = _PRESENTATION[runtime.phase]
    accepted = legal_events(runtime)
    level = snapshot.microphone_level
    normalized = 0.0 if not math.isfinite(level) else max(0.0, min(1.0, float(level)))
    return CompanionViewModel(
        phase=runtime.phase,
        cue=cue,
        status=status,
        detail=snapshot.detail,
        can_start_recording=EventKind.START_RECORDING in accepted,
        can_finish_recording=EventKind.FINISH_RECORDING in accepted,
        microphone_level=f"{int(round(normalized * 100))}%",
    )
