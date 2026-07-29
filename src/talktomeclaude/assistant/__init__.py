"""Assistant integration contracts for Claude Code and Codex CLI."""

from .claude import (
    AssistantAdapter,
    AssistantEventCode,
    AssistantEventResult,
    ClaudeCodeAdapter,
    ValidatedAssistantEvent,
)
from talktomeclaude.reply_protocol import canonical_reply_digest
from .hooks import (
    CLAUDE_STOP_HOOK_COMMAND,
    CODEX_OWNED_HOOK_MARKER,
    CODEX_STOP_HOOK_COMMAND,
    OWNED_HOOK_MARKER,
    ClaudeHookManager,
    CodexHookManager,
    HookInspection,
    HookStatus,
)
from .suppression import (
    DirectorEventGate,
    DirectorLaunchGuard,
    DirectorLease,
    ManagedDirectorProcess,
    SuppressionRegistry,
)

__all__ = [
    "AssistantAdapter",
    "AssistantEventCode",
    "AssistantEventResult",
    "CLAUDE_STOP_HOOK_COMMAND",
    "CODEX_OWNED_HOOK_MARKER",
    "CODEX_STOP_HOOK_COMMAND",
    "ClaudeCodeAdapter",
    "ClaudeHookManager",
    "CodexHookManager",
    "DirectorEventGate",
    "DirectorLaunchGuard",
    "DirectorLease",
    "ManagedDirectorProcess",
    "HookInspection",
    "HookStatus",
    "OWNED_HOOK_MARKER",
    "SuppressionRegistry",
    "ValidatedAssistantEvent",
    "canonical_reply_digest",
]
