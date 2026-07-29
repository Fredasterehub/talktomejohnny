# Contributing

## Development Setup

1. Install Python 3.11 or newer.
2. Install `uv`.
3. Create a virtual environment:
   ```bash
   uv venv --python 3.12
   ```
4. Install the project:
   ```bash
   uv pip install -e .
   ```

## Before Opening A Pull Request

- Run the test suite:
  ```bash
  pytest -q
  ```
- Run Ruff:
  ```bash
  ruff check src tests
  ```
- Build the package:
  ```bash
  python -m build
  ```

## Compatibility Rules

- `talktomejohnny` is the primary public name.
- `talktomeclaude` remains a compatibility alias for the CLI, Python package,
  config directory, cache directory, and migrated voice assets.
- Migration changes must be backward-compatible and non-destructive.

## Pull Request Notes

- Keep diffs small and reversible.
- Add or update tests for behavior changes.
- Prefer deterministic hooks, exact-session ownership, and fail-closed
  behavior over convenience shortcuts.
- Use commit messages that explain why the change exists, not only what changed.
