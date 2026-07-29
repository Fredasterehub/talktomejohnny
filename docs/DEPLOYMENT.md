# Deployment and Upgrade Runbook

This document captures the operational knowledge needed to update and validate
TalkToMeClaude, especially on native Windows with remote Claude Code, CUDA speech
recognition, and Chatterbox cloned voices.

## Deployment Model

- The microphone, speech recognition, TTS, and terminal UI run on the user's local
  machine.
- On Windows, the Stage A companion is an explicit opt-in. The no-argument dashboard
  remains the default until the physical release gate approves a later flip.
- The default assistant provider remains Claude. Codex is an explicit opt-in provider
  selected with `talktomeclaude config set assistant-provider codex` and launched
  through `talktomeclaude codex`.
- Claude Code may run locally or over passwordless SSH. A remote project directory is
  passed to `claude -p` through a safely quoted login shell.
- Remote companion mode requires this same TalkToMeClaude checkout on the Claude
  host for its owned Stop hook and `talktomeclaude hook stream` helper. The Codex
  provider uses its own additive Stop hook at `remote-cwd/.codex/hooks.json`, must be
  trusted once with `/hooks` in Codex, and activates only when launched through the
  `talktomeclaude codex` wrapper. Neither provider installs or runs the local
  audio/Torch/TTS stack there. Legacy `listen` remote mode does not require this
  helper.
- Source installs are editable. A code-only update does not require rebuilding the
  virtual environment, but the running `talktomeclaude` process must be restarted.
- Reinstall dependencies only when `pyproject.toml`, an optional engine, or a pinned
  runtime changes.

Confirm which checkout an installed command imports before debugging the wrong tree:

```powershell
python -c "import talktomeclaude; print(talktomeclaude.__file__)"
```

## Upgrade Procedure

1. Start from a clean feature worktree. Do not overwrite unrelated local changes.
2. Fetch the target branch and merge it into the feature worktree before resolving
   overlapping CLI or configuration changes.
3. Keep the no-argument dashboard, CLI subcommands, and persisted configuration
   backward compatible.
4. Refresh the editable install only if its source target changed:

   ```powershell
   uv pip install --python .venv\Scripts\python.exe -e .
   ```

5. If dependency declarations changed, install them into the existing environment;
   do not recreate a working venv without a concrete reason.
6. Run the verification sequence below on Windows and Linux.
7. Push the feature branch only after both worktrees are clean and green.

## Assistant provider switching

Keep the provider default on Claude unless you are explicitly validating the Codex
path. Roll back by setting Claude again and restarting the companion:

```powershell
talktomeclaude config set assistant-provider claude
```

Use the Codex wrapper only for attached sessions that should stream through the
project-scoped Codex hook:

```powershell
talktomeclaude config set assistant-provider codex
talktomeclaude companion
```

Then, from the configured project on the assistant host, launch the attached CLI:

```text
talktomeclaude codex
```

If the remote Codex hook has not been trusted yet, open Codex and accept the project
hook via `/hooks` before relying on streamed replies.

## Windows Companion Stage A

The selected production shell is the one-process Tk + Win32 adapter. Launch it only
through the explicit Stage A entry point:

```powershell
talktomeclaude companion
```

Do not change the no-argument default during a routine deployment. Verify all three
entry points before shipping:

```powershell
talktomeclaude companion
talktomeclaude companion --headless
talktomeclaude tui
```

For a delivery smoke, focus the intended eligible terminal, select its tab/pane/shell,
place its blinking cursor, and finish push-toggle. That foreground terminal is held as
ephemeral evidence only for the active transaction, revalidated before clipboard,
paste, and optional Enter, then discarded. It is never persisted or reused. Keep the
same terminal foregrounded until the result is visible.

The operator-facing warning must remain exact across settings/onboarding surfaces:

> Auto-submit sends Enter to the eligible foreground terminal captured at finish-toggle; the operator is responsible for the intended tab, pane, shell, and cursor position.

Verify assistant auto-submit both off (one paste, no Enter) and on (one paste, one
Enter), plus a changed-target case that sends no later keys. Verify voice listing,
preview, import cancellation/rollback, and selection without changing existing voice
references or caches. The complete operator contract is in
[`WINDOWS_COMPANION.md`](WINDOWS_COMPANION.md).

Rollback uses `talktomeclaude tui` for the established dashboard or
`talktomeclaude companion --headless` for the production controller without Tk/hotkey.
Do not downgrade config or remove the companion diagnostics/reply state, registry,
voice references, or model caches. A default-launch rollback, after a future Stage B
flip, changes only the entry path.

## Required Verification

Run from the repository root with the environment's Python:

```powershell
python -m unittest discover -s tests -q
python -m compileall -q src tests
git diff --check
talktomeclaude --help
talktomeclaude voices
talktomeclaude doctor
talktomeclaude config get default-voice
talktomeclaude config get assistant-auto-submit
```

When `.kiln/law/check.sh` is available, run it as the canonical project check as well.
CI must retain native Windows and Linux Python 3.12 coverage.

For CUDA changes, unit tests are insufficient. In a fresh Windows process, verify:

1. `cuda_available()` returns true.
2. A real cached audio fixture transcribes with `large-v3`, `device=cuda`, and
   `compute_type=float16`.
3. In that same process, a cloned voice renders a non-empty WAV. This catches CUDA DLL
   combinations that import successfully but fail during a Torch convolution.

For remote changes, call the real `_prompt_claude` path with `on_wait` enabled and a
Unicode response. Confirm valid JSON, a non-empty reply, and a session ID.

## Windows Runtime Invariants

These are regression boundaries, not implementation suggestions:

- Explicitly use UTF-8 for subprocess text input and output. Native Windows otherwise
  uses a legacy code page such as `cp1252`, which fails on emoji and punctuation.
- Open SSH/Claude subprocesses with explicit stdin, stdout, and stderr pipes. Normalize
  missing captured values before JSON parsing.
- Close file descriptors returned by `tempfile.mkstemp` before another Windows process
  writes, reads, plays, or deletes that path.
- A CUDA-enabled Torch build must load its bundled CUDA/cuDNN libraries before
  CTranslate2. Do not also preload the standalone NVIDIA cuDNN wheel in that process.
  CTranslate2 can reuse Torch's loaded CUDA runtime. On systems without CUDA Torch, the
  NVIDIA wheel discovery path remains necessary for Faster Whisper.
- Test actual inference, not only `import torch`, `import ctranslate2`, or device counts.
  Conflicting cuDNN builds can import successfully and fail only during computation.
- Preserve CPU fallback for automatic STT selection. Explicit CUDA failures should be
  reported cleanly rather than producing an unhandled traceback.

## CUDA and Voice Cloning

Use `talktomeclaude doctor` as the canonical install recipe. The Windows RTX deployment
validated during development used:

- Python 3.12
- Torch and torchaudio 2.11.0 with CUDA 12.8
- Chatterbox TTS 0.1.7
- The companion versions printed by `talktomeclaude doctor`

Install into the existing environment with `uv pip install --python <python> ...`.
The environment may intentionally have no `pip` module.

Chatterbox weights are cached under `~/.cache/talktomeclaude/hf`. The first cloned
reply in each new application process loads cached weights into GPU memory. Later
replies reuse the module-level model. The `Sampling` progress shown for each sentence
is inference, not a download. A valid cache can be proved by rendering twice with
`HF_HUB_OFFLINE=1`; the model object should be reused and the second render should be
substantially faster.

Do not delete model caches during ordinary upgrades. A Hugging Face token belongs in
`HF_TOKEN` or the Hugging Face credential store, never in the repository or this file.

The dashboard's optional YouTube reference workflow also requires `yt-dlp`, `ffmpeg`,
and `ffprobe` on `PATH`. These are external media tools, not Python package
dependencies. File and microphone references do not require `yt-dlp`; media formats
that need conversion still require FFmpeg.

## Voice Registry

Persistent settings and voice references live outside the checkout:

- Configuration: `~/.config/talktomeclaude/config.json`
- Registry: `~/.config/talktomeclaude/voices.json`
- Clone references: `~/.config/talktomeclaude/voice-refs/`

Register a reference without generating a sample:

```powershell
talktomeclaude voice create NAME --reference PATH --no-sample
```

Select the active voice:

```powershell
talktomeclaude config set default-voice NAME
```

The legacy dashboard's `V` key still toggles spoken replies; it is not a voice picker.
The explicit Windows companion has a separate Voice window for availability, preview,
selection, and guided clone/Piper import. Preserve the legacy key behavior unless its
label, interaction, tests, and documentation change together.

## Expected Warnings and Performance

- Diffusers, Torch attention, and Chatterbox may print upstream deprecation warnings.
  Treat them separately from nonzero exits, missing WAV output, or CUDA exceptions.
- A cloned voice can take roughly 10-20 seconds on first use in a new process while
  cached weights load. Subsequent short replies should be much faster.
- Do not claim repeated downloads based on `Sampling` progress. Verify with offline
  mode, cache file changes, or network observation.

## Release Evidence

Record these facts in the PR or commit message for changes touching deployment:

- exact test counts and platforms
- Python, Torch, CUDA, and GPU versions for hardware checks
- STT tier actually used
- cloned voice actually rendered and WAV size was nonzero
- remote Claude JSON/session round trip result
- known optional engines or physical microphone paths not tested
- explicit companion, headless, and legacy TUI launch/clean-exit results
- foreground-target/auto-submit matrix and whether any `pasted_not_submitted` recovery
  was exercised
