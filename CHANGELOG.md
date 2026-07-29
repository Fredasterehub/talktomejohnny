# Changelog

All notable changes to TalkToMeJohnny will be recorded here.

## Unreleased

- Documentation and governance cleanup for the public TalkToMeJohnny rename.
- Provider-neutral activation guidance for normal `claude` and normal `codex` sessions.
- Backward-compatible migration notes for legacy `talktomeclaude` names, caches, settings, and voice assets.

## 0.1.1 - 2026-07-29

- Made installed lifecycle-hook commands fail closed unless the executable can
  be resolved to a trusted absolute path.
- Added safe migration of exact legacy hook entries and valid provider skill
  metadata for normal Claude Code and Codex CLI activation.
- Corrected the public plugin metadata, provider-neutral documentation, and
  default command-namespace posture.
- Reused legacy model caches and copied settings and voice references forward
  without overwriting or deleting existing state.

## 0.1.0

Initial public release of TalkToMeJohnny.

### Highlights

- Public rename to `TalkToMeJohnny` / `talktomejohnny`.
- Exact-session activation model for Claude Code and Codex CLI.
- One global attached session with isolated provider streams.
- Fail-closed recovery when the companion or hook lease is stale or offline.
- Compatibility aliases for legacy CLI, import, config, cache, and voice registry names.
- Non-destructive migration for existing settings, voice references, and caches.
