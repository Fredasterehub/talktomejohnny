# Contributing to TalkToMeJohnny

TalkToMeJohnny is the public name for this repository. Use `talktomeclaude`
only as a compatibility alias during migration work.

## Before You Start

1. Work in a clean branch or worktree.
2. Keep secrets out of commits, logs, docs, and screenshots.
3. Preserve existing voice, cache, and config data when touching migration code.
4. Do not overwrite unrelated edits that are already present in the worktree.

## Local Setup

```powershell
uv venv --python 3.12
uv pip install -e .
```

## Validation

Run the checks that match the area you changed:

```powershell
python -m unittest discover -s tests -q
python -m compileall -q src tests
git diff --check
ruff check src tests
python -m build
```

If you touched public docs, also run a hygiene search for local-only paths,
hostnames, and destructive examples:

```powershell
rg -n "talktomeclaude|C:\\Users\\<name>|user@host|/path/to/project|rm -rf|git reset --hard|git checkout --" README.md llms.txt docs CONTRIBUTING.md CODE_OF_CONDUCT.md SECURITY.md CHANGELOG.md -S
```

## Compatibility Rules

- `talktomejohnny` is the primary public name.
- `talktomeclaude` remains a compatibility alias for the CLI, Python package,
  config directory, cache directory, and migrated voice assets.
- Migration changes must be backward-compatible and non-destructive.
- Normal `claude` and normal `codex` sessions must stay inert until the exact
  attach command enables them.

## Pull Request Notes

- Keep diffs small and reversible.
- Add or update tests for behavior changes.
- Prefer deterministic hooks, exact-session ownership, and fail-closed behavior
  over convenience shortcuts.
- Use commit messages that explain why the change exists, not only what changed.
