# Deployment and Upgrade Runbook

This runbook covers TalkToMeJohnny deployment, upgrade, and migration. It is the
operational source of truth for the public rename from TalkToMeClaude.

## Deployment model

- The microphone, speech recognition, TTS, and UI run on the local machine.
- The assistant CLI can run locally or over SSH on another host.
- The primary public name is `TalkToMeJohnny` / `talktomejohnny`.
- `talktomeclaude`, `import talktomeclaude`, `~/.config/talktomeclaude/`, and
  `~/.cache/talktomeclaude/` remain compatibility aliases while the migration is
  in flight.
- Existing settings, voice references, and caches must be copied forward
  non-destructively. Never strand or delete the legacy data on first run.
- Keep provider activation neutral: normal `claude` and `codex` launches are
  silent until the exact session is attached with the TalkToMeJohnny command.

## Upgrade procedure

1. Start from a clean feature worktree.
2. Merge or rebase the target branch into the worktree before resolving any
   overlapping docs or config changes.
3. Keep the dashboard, CLI subcommands, and persisted configuration backward
   compatible.
4. Refresh the editable install only if the source target changed:

   ```powershell
   uv pip install --python .venv\Scripts\python.exe -e .
   ```

5. If dependency declarations changed, update the existing environment rather
   than recreating it.
6. Run verification on the local machine and on the assistant host if the
   change touches hooks, remote transport, or assistant activation.
7. Publish only after both worktrees are clean and the verification steps pass.

## Assistant activation

### Claude Code

Start Claude Code normally:

```bash
claude
```

Attach, detach, or query the exact session from inside Claude Code:

```text
/talktomejohnny on
/talktomejohnny off
/talktomejohnny status
```

### Codex CLI

After `talktomejohnny hook install --provider codex`, open `/hooks` inside Codex,
review the TalkToMeJohnny hook entries, and trust their current definitions.
Codex deliberately keeps installation and trust as separate security steps.

Start Codex normally:

```bash
codex
```

Attach, detach, or query the exact session from inside Codex:

```text
$talktomejohnny on
$talktomejohnny off
$talktomejohnny status
```

Only the attached session is eligible for spoken replies. Detached sessions do
not replay old queue contents. If the companion is offline or its live lease is
stale, the system fails closed.

On Windows, generated hook JSON uses `powershell.exe -NoProfile -NonInteractive
-EncodedCommand` around a fixed invocation of the resolved TalkToMeJohnny
executable. Claude Code may execute native Windows hooks through Bash; this form
preserves backslashes plus stdin/stdout JSON across both shells. The encoded
payload contains only the absolute executable path and fixed hook arguments.

## Windows companion

Launch the explicit Windows companion entry point only when you want the
desktop path:

```powershell
talktomejohnny companion
```

Use the headless controller or legacy dashboard for rollback and recovery:

```powershell
talktomejohnny companion --headless
talktomejohnny tui
```

Verify the operator-facing terminal contract on every delivery smoke:

1. Bring the intended terminal to the foreground.
2. Select the intended tab, pane, and shell.
3. Place the blinking cursor in the exact input field.
4. Finish recording and keep that terminal foregrounded until delivery
   completes.

The companion revalidates the target before clipboard, before paste, and before
optional Enter. It never switches to another terminal or infers readiness from
screen contents.

## Verification

Run these commands from the repository root:

```powershell
python -m unittest discover -s tests -q
python -m compileall -q src tests
git diff --check
talktomejohnny --help
talktomejohnny voices
talktomejohnny doctor
talktomejohnny config get default-voice
talktomejohnny config get assistant-auto-submit
```

For Windows speech and voice changes, verify:

1. The local speech stack still starts.
2. The configured voice renders a non-empty WAV.
3. The companion can switch to a different voice while already running, without
   restarting.
4. Spoken output volume changes affect both live replies and previews.
5. The assistant reply path still round-trips through the exact attached
   session.

For SSH changes, verify:

1. Passwordless SSH works from the local machine.
2. The remote assistant host can reach the configured project.
3. The exact-session attach command works from the assistant CLI.

## Runtime invariants

- Use UTF-8 for subprocess text input and output.
- Open SSH and assistant subprocesses with explicit pipes.
- Close `mkstemp` file descriptors before a second process reuses them.
- Preserve CPU fallback for speech recognition.
- Preserve the configured voice registry and caches across upgrades.
- Do not rewrite or delete the legacy alias directories during the migration.

## Voice and cache migration

- Primary voice and cache roots should be treated as the new `talktomejohnny`
  namespace.
- Legacy `talktomeclaude` roots should be copied forward, not moved in place.
- Existing voice references remain valid and reusable.
- Chatterbox and Whisper caches should be reused when already present.
- Do not require users to reclone or rebuild voices just to complete the rename.

## Release evidence

Record these facts in the PR, release notes, or commit message for changes that
touch deployment:

- exact test counts and platforms
- Python, Torch, CUDA, and GPU versions for hardware checks
- STT tier actually used
- cloned voice actually rendered and WAV size was non-zero
- live voice-switch and output-volume checks completed successfully
- remote assistant round-trip result
- known optional engines or physical microphone paths not tested
- explicit companion, headless, and legacy dashboard launch and clean-exit
  results
- foreground-target and auto-submit matrix
