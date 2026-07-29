#!/usr/bin/env bash
# Legacy-compatible Stop hook — routes the final reply through TalkToMeJohnny.
# Registered async: it must never block Claude Code, so every failure
# (CLI missing, TTS unavailable) exits 0 silently.
ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

for candidate in \
  "$ROOT/.venv/bin/talktomejohnny" \
  "$ROOT/.venv/Scripts/talktomejohnny.exe" \
  "$ROOT/.venv/Scripts/talktomejohnny" \
  "$ROOT/.venv/bin/talktomeclaude" \
  "$ROOT/.venv/Scripts/talktomeclaude.exe" \
  "$ROOT/.venv/Scripts/talktomeclaude" \
  "$(command -v talktomejohnny 2>/dev/null)" \
  "$(command -v talktomeclaude 2>/dev/null)"
do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    exec "$candidate" hook stop "$@"
  fi
done

exit 0
