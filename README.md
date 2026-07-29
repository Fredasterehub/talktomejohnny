<!--
AI ASSISTANTS: machine-readable install/usage instructions live in llms.txt at the repo root.
URL: https://raw.githubusercontent.com/Fredasterehub/talktomejohnny/main/llms.txt
Agent-facing conventions: llms.txt (install/usage) · AGENTS.md (contributor/agent guidance).
-->

<p align="center">
  <img src="assets/skull-emblem.jpg" width="210" alt="TalkToMeJohnny emblem">
</p>

<h1 align="center">TalkToMeJohnny</h1>

<p align="center"><em>Covey leader to Raven... talk to me, Johnny.</em></p>

<p align="center">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-171310">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-e6b22e?labelColor=171310">
  <img alt="local-first" src="https://img.shields.io/badge/local--first-no%20cloud%20voice-171310">
  <img alt="voices: public domain" src="https://img.shields.io/badge/voices-public%20domain-e6b22e?labelColor=171310">
</p>

<p align="center"><a href="https://fredasterehub.github.io/talktomejohnny/"><strong>Live site</strong></a></p>

TalkToMeJohnny is a local-first voice medium for Claude Code and Codex CLI.
You speak locally, the assistant reply comes back through deterministic hooks,
and the speech layer stays on your machine.

This repository is in transition from the legacy TalkToMeClaude naming. Public
docs use `TalkToMeJohnny` / `talktomejohnny` as the primary names. The older
`talktomeclaude` CLI, Python import, config directory, cache directory, and voice
registry remain supported compatibility aliases during the migration so existing
voices, caches, and settings are not stranded.

## What it does

- Open the dashboard with `talktomejohnny` for live status, recording controls,
  and assistant routing.
- Attach an exact Claude Code session with `/talktomejohnny on|off|status`.
- Attach an exact Codex CLI session with `$talktomejohnny on|off|status`.
- Keep ordinary `claude` and `codex` launches silent until they are explicitly
  attached.
- Keep Claude and Codex reply streams isolated, with one active attached session
  globally.
- Fail closed if the companion is offline or the attachment lease is stale.
- Keep speech-to-text local and keep speech output local.
- Preserve legacy settings, voice references, and caches non-destructively.

## Requirements

- Python 3.11 or newer.
- Claude Code on `PATH` for the Claude voice loop.
- Codex CLI on `PATH` for the Codex voice loop.
- Git for Windows if you want native Windows hook execution through Git Bash for
  the automatic reply-speaking path.
- No PyPI release is described here. Install from a Git checkout or editable
  working tree.

## Install

### Windows

1. Open Windows Terminal and install `uv`:
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
2. Get the code:
   ```powershell
   git clone https://github.com/Fredasterehub/talktomejohnny.git
   cd talktomejohnny
   ```
   If you are still on a pre-rename checkout, `talktomeclaude` remains the legacy
   alias.
3. Create the environment and install:
   ```powershell
   uv venv --python 3.12
   uv pip install -e .
   ```
4. Verify the install:
   ```powershell
   .\.venv\Scripts\talktomejohnny --help
   .\.venv\Scripts\talktomejohnny voices
   ```
5. Open the dashboard:
   ```powershell
   .\.venv\Scripts\talktomejohnny
   ```

### macOS

1. Install `uv`:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Clone and install:
   ```bash
   git clone https://github.com/Fredasterehub/talktomejohnny.git
   cd talktomejohnny
   uv venv --python 3.12
   uv pip install -e .
   ```
3. Verify:
   ```bash
   ./.venv/bin/talktomejohnny --help
   ./.venv/bin/talktomejohnny voices
   ```

### Linux

1. Install system audio support and `git`:
   ```bash
   sudo apt install -y libportaudio2 git
   ```
2. Install `uv`:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
3. Clone and install:
   ```bash
   git clone https://github.com/Fredasterehub/talktomejohnny.git
   cd talktomejohnny
   uv venv --python 3.12
   uv pip install -e .
   ```
4. Optional CUDA speech-to-text tier:
   ```bash
   uv pip install -e ".[cuda]"
   ```

## Assistant activation

TalkToMeJohnny does not replace the assistant CLI. It attaches to the exact
session you choose.

### Claude Code

Start Claude Code normally:

```bash
claude
```

Then attach or detach the current session from inside Claude Code:

```text
/talktomejohnny on
/talktomejohnny off
/talktomejohnny status
```

### Codex CLI

Start Codex normally:

```bash
codex
```

Then attach or detach the current session from inside Codex:

```text
$talktomejohnny on
$talktomejohnny off
$talktomejohnny status
```

Only the exact attached session is eligible for spoken replies. Detaching stops
future completions from being spoken, but an already accepted in-flight reply may
still finish.

## Remote SSH setup

Voice capture stays on the machine you sit at. The assistant can live on a remote
host over SSH.

1. Install the assistant CLI on the remote host and confirm it starts.
2. Set up passwordless SSH from your local machine to the remote host.
3. Configure the remote target and project directory:
   ```bash
   talktomejohnny config set remote you@example.com
   talktomejohnny config set remote-cwd /path/to/project
   ```
4. Start the voice loop:
   ```bash
   talktomejohnny listen
   ```

Use `remote local` and `remote-cwd home` to clear those settings. If a path
contains spaces, quote it as usual.

## Windows companion

The Windows companion is an explicit opt-in desktop path. It is Windows 11 only.
The companion and its recovery paths are documented in
[`docs/WINDOWS_COMPANION.md`](docs/WINDOWS_COMPANION.md).

Recommended entry points:

```powershell
talktomejohnny companion
talktomejohnny companion --headless
talktomejohnny tui
```

## Commands

The primary command name is `talktomejohnny`. The legacy `talktomeclaude` name
remains a compatibility alias during migration.

| Command | What it does |
|---|---|
| `talktomejohnny` | Open the interactive dashboard. |
| `companion [--headless]` | Launch the Windows companion or its headless recovery controller. |
| `tui` | Open the legacy Textual dashboard. |
| `setup [--reset] [--force]` | Re-run first-run setup. |
| `speak "text"` | Synthesize and play a local line. |
| `listen` | Drive a remote or local assistant by voice. |
| `transcribe FILE` | Transcribe an audio file locally. |
| `filter TRANSCRIPT.jsonl` | Print only spoken dialogue. |
| `voices` | List bundled and registered voices. |
| `voice create ...` | Register a voice from a reference file or recording. |
| `config set KEY VALUE` / `config get KEY` | Persist or read settings. |
| `assist on|off|status` | Toggle the speech layer. |

## Migration notes

- Existing `talktomeclaude` command lines remain supported as aliases during the
  migration.
- Existing `import talktomeclaude` remains the compatibility import path while the
  rename lands.
- Existing `~/.config/talktomeclaude/` and `~/.cache/talktomeclaude/` data should
  be copied forward non-destructively, not moved or deleted.
- Existing voice references and model caches stay reusable. Do not strand them
  during rename or upgrade work.

## For AI agents

Machine-readable install and usage instructions live in
[`llms.txt`](llms.txt). Contributor and operator guidance lives in
[`AGENTS.md`](AGENTS.md).

```text
https://raw.githubusercontent.com/Fredasterehub/talktomejohnny/main/llms.txt
```

## License

MIT. See [LICENSE](LICENSE).

