"""Presentation contracts for the TalkToMeJohnny companion."""

from talktomeclaude.companion.contracts import (
    CompanionIntent,
    CompanionSnapshot,
    IntentKind,
)
from talktomeclaude.companion.viewmodel import CompanionViewModel, to_view_model
from talktomeclaude.companion.tk_shell import (
    TkCompanionShell,
    WindowsNonActivatingPolicy,
)

__all__ = [
    "CompanionIntent",
    "CompanionSnapshot",
    "CompanionViewModel",
    "IntentKind",
    "TkCompanionShell",
    "WindowsNonActivatingPolicy",
    "to_view_model",
]
