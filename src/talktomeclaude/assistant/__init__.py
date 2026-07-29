"""Assistant integration contracts for Claude Code and Codex CLI."""

from .attachment import (
    DEFAULT_LIVE_LEASE_TTL_SECONDS,
    ATTACHMENT_STATE_VERSION,
    LiveCompanionLease,
    LiveLeaseState,
    SessionAttachment,
    SessionAttachmentError,
    SessionAttachmentRegistry,
)
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
    SessionControlHookManager,
    resolve_hook_executable,
)
from .suppression import (
    DirectorEventGate,
    DirectorLaunchGuard,
    DirectorLease,
    ManagedDirectorProcess,
    SuppressionRegistry,
)

__all__ = [
    "ATTACHMENT_STATE_VERSION",
    "AssistantAdapter",
    "AssistantEventCode",
    "AssistantEventResult",
    "CLAUDE_STOP_HOOK_COMMAND",
    "CODEX_OWNED_HOOK_MARKER",
    "CODEX_STOP_HOOK_COMMAND",
    "ClaudeCodeAdapter",
    "ClaudeHookManager",
    "DEFAULT_LIVE_LEASE_TTL_SECONDS",
    "CodexHookManager",
    "DirectorEventGate",
    "DirectorLaunchGuard",
    "DirectorLease",
    "ManagedDirectorProcess",
    "LiveCompanionLease",
    "LiveLeaseState",
    "HookInspection",
    "HookStatus",
    "OWNED_HOOK_MARKER",
    "resolve_hook_executable",
    "SessionControlHookManager",
    "SessionAttachment",
    "SessionAttachmentError",
    "SessionAttachmentRegistry",
    "SuppressionRegistry",
    "ValidatedAssistantEvent",
    "canonical_reply_digest",
]
