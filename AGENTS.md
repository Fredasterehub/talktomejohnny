# AGENTS.md — TalkToMeJohnny

Stack: Python >=3.11. Env/deps: `uv`, project venv at `.venv`. CLI framework: `click`.
Primary console script: `talktomejohnny`; compatibility console script:
`talktomeclaude`. The implementation intentionally remains under
`src/talktomeclaude/` during the migration window, with a public
`src/talktomejohnny/` facade. Fixtures: `tests/fixtures/`.

Test command: `bash .kiln/law/check.sh` (run from project root) — exit 0 is the bar. Do not
edit anything under `.kiln/` — it is Kiln's own control plane and evidence store, never
project source.

Before changing installation, release, Windows CUDA, or voice-cloning behavior, read
`docs/DEPLOYMENT.md`. If `.kiln/law/check.sh` is not present in the active checkout, run
the documented compile, unit-test, CLI-smoke, and platform-smoke checks directly.
