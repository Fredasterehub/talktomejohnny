# Changelog

All notable changes to TalkToMeJohnny will be recorded here.

## Unreleased

- Documentation and governance cleanup for the public TalkToMeJohnny rename.
- Provider-neutral activation guidance for normal `claude` and normal `codex` sessions.
- Backward-compatible migration notes for legacy `talktomeclaude` names, caches, settings, and voice assets.

## 0.1.0

Initial public release of TalkToMeJohnny.

### Highlights

- Public rename to `TalkToMeJohnny` / `talktomejohnny`.
- Exact-session activation model for Claude Code and Codex CLI.
- One global attached session with isolated provider streams.
- Fail-closed recovery when the companion or hook lease is stale or offline.
- Compatibility aliases for legacy CLI, import, config, cache, and voice registry names.
