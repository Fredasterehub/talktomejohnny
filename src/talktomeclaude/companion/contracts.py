"""Small, UI-independent contracts shared by companion presentations.

The desktop shell and headless recovery path consume these values.  They do
not own capture, delivery, or speech behavior; those services will eventually
publish snapshots and accept intents behind this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from talktomeclaude.core import RuntimeState


class IntentKind(str, Enum):
    """User actions accepted by companion controllers."""

    STATUS = "status"
    START_RECORDING = "start-recording"
    FINISH_RECORDING = "finish-recording"
    CANCEL = "cancel"
    TOGGLE_OUTPUT_MUTE = "toggle-output-mute"
    OPEN_SETTINGS = "open-settings"
    OPEN_VOICE = "open-voice"
    OPEN_REVIEW = "open-review"
    OPEN_DIAGNOSTICS = "open-diagnostics"
    QUIT = "quit"


@dataclass(frozen=True, slots=True)
class CompanionIntent:
    """A presentation-neutral request from a user-facing controller."""

    kind: IntentKind
    allow_focus: bool = False

    def __post_init__(self) -> None:
        focusable = {
            IntentKind.OPEN_SETTINGS,
            IntentKind.OPEN_VOICE,
            IntentKind.OPEN_REVIEW,
            IntentKind.OPEN_DIAGNOSTICS,
        }
        if self.allow_focus and self.kind not in focusable:
            raise ValueError("workflow intents may not request focus")


@dataclass(frozen=True, slots=True)
class CompanionSnapshot:
    """Content-safe state published to a companion presentation."""

    runtime: RuntimeState
    detail: str = ""
    output_muted: bool = False
    output_volume: int = 100
    microphone_level: float = 0.0
