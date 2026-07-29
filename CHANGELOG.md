# Changelog

## 0.1.0 - 2026-07-29

- Rebranded the public package and primary CLI to `TalkToMeJohnny` /
  `talktomejohnny`.
- Kept `talktomeclaude` as a backward-compatible CLI and import alias.
- Added deterministic exact-session activation for normal Claude Code and Codex
  CLI sessions.
- Added provider-local control skills for `/talktomejohnny` and
  `$talktomejohnny`.
- Hardened attachment/lease recovery so stale or offline companions fail closed.
- Added non-destructive migration support for legacy config, voice registry,
  voice references, and cache paths.
- Sanitized public fixtures and examples for provider-neutral publication.
