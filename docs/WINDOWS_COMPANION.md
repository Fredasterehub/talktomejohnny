# Windows Companion (Stage A)

The Windows companion is an opt-in desktop path for using local microphone,
speech recognition, and speech output alongside a supported assistant CLI terminal.
It supports Windows 11 only. The terminal remains the visible source of truth; the
companion does not replace, scrape, or infer the state of the assistant CLI.

Stage A deliberately requires an explicit launch. Running `talktomeclaude` with no
subcommand still opens the existing dashboard.

## Launch and exit

From an installed PowerShell environment, run:

```powershell
talktomeclaude companion
```

The selected desktop shell is Tk plus Win32 adapters. It was chosen over the larger
WPF/IPC candidate by the recorded shell capability gate and adds no dependency. The
compact window shows semantic state and non-color cues. Runtime updates do not take
focus; Settings, Voice, Review, and Diagnostics focus only when you open them.

The global recording control is `Ctrl+Alt+Space`:

- In push-toggle mode, press once to start and once to finish.
- In hold-to-talk mode, hold the combination while speaking and release to finish.
- Quit from the companion window to stop workers and unregister the hotkey.

If the desktop shell cannot start, see [Recovery paths](#recovery-paths).

When Claude Code runs over the configured SSH remote, install this same
TalkToMeClaude checkout there as a code-only helper. The companion idempotently
installs its owned Claude Stop hook or its owned Codex Stop hook, then runs the
matching `talktomeclaude hook stream` path over passwordless SSH. The remote needs
no microphone, speaker, Torch, TTS engine, or voice/model cache. The Codex path is
opt-in and only activates when you launch attached sessions through
`talktomeclaude codex`; ordinary `codex` sessions stay out of scope unless you
explicitly wrap them. The remote helper requirement applies to the companion path;
the older `listen` command can still drive a remote that has only Claude Code.

## Assistant provider

The default assistant provider remains Claude. To use the authorized Codex CLI
provider, select Codex and restart the companion on Windows:

```powershell
talktomeclaude config set assistant-provider codex
talktomeclaude companion
```

Then, on the assistant host, change to whichever directory you want Codex to work in
and launch the attached session:

```text
cd /path/to/your/project
talktomeclaude codex
```

The Codex Stop hook is additive in the assistant host's user-level
`~/.codex/hooks.json`. Trust it once from Codex with `/hooks`; that one trust applies
when an attached session starts in another directory. Only `talktomeclaude codex`
sets the child activation marker, so ordinary `codex` sessions remain outside reply
transport even though they can see the user-level hook. Codex also uses separate
remote spool and local inbox roots, so switching providers cannot replay a historical
Claude queue. To roll back, set the provider back to Claude and relaunch the normal
path:

```powershell
talktomeclaude config set assistant-provider claude
talktomeclaude companion
```

## Choose the delivery terminal

You select the target for every turn. Before finishing push-toggle or releasing the
hold control:

1. Bring the intended supported terminal to the foreground.
2. Select the intended tab, pane, and shell yourself.
3. Place the blinking cursor in the exact input field that should receive the text.
4. Finish recording, then keep that terminal foregrounded until delivery completes.

Supported targets are Windows Terminal, a Windows console host, WezTerm, Alacritty,
and mintty when their expected process and window-class evidence match. Notepad,
browsers, editors, and unsupported or unverifiable windows are rejected.

Finishing recording snapshots the currently foreground eligible terminal into one
ephemeral delivery transaction. The companion revalidates that same evidence before
touching the clipboard, before paste, and before optional Enter. It never searches
for another terminal, changes tabs or panes, inspects terminal contents, or guesses
whether a prompt is ready. The snapshot is discarded on success or failure and is
never remembered for the next turn.

If the target changes before paste, the companion stops without sending keys and
restores its clipboard change when safe. If text was pasted but the target changes
before Enter, it reports `pasted_not_submitted`, sends no Enter, and keeps the
transcript recoverable. Retry or transcript confirmation creates a fresh transaction
against the then-current foreground terminal.

## Auto-submit

The Settings window contains the assistant auto-submit switch and this warning:

> Auto-submit sends Enter to the eligible foreground terminal captured at finish-toggle; the operator is responsible for the intended tab, pane, shell, and cursor position.

With assistant auto-submit on, an acceptable transcript is pasted once and Enter is
sent once, provided every revalidation succeeds. With it off, the transcript is
pasted but not submitted. Generic dictation never sends Enter. Empty, low-confidence,
edited, and recovery transcripts remain available for explicit review rather than
being guessed or silently injected.

The same settings can be inspected or changed from the CLI:

```powershell
talktomeclaude config get assistant-auto-submit
talktomeclaude config set assistant-auto-submit off   # or on
talktomeclaude config set recording-mode push-toggle # or push-to-talk
```

## Spoken reply controls

While a reply is speaking, start a new recording to stop and park it immediately.
An accepted exact control such as `pause`, `continue`, `repeat`, `back`, `next`,
`topics`, `summarize`, `where were you`, `go back`, `keep going`, `stop talking`,
`voice off`, `help`, or `jump to <topic>` is handled locally. It is not copied,
pasted, submitted to Claude, or allowed to reuse the finish-time terminal snapshot.
`go back` speaks a short recap before resuming the parked answer.

Only normal-confidence assistant input takes this local path. Generic dictation is
always delivered as dictation, while low-confidence, edited, safety-stop, and recovery
text remains in the explicit review flow and is never silently reinterpreted as a
control.

## Voice settings and import

Open **Voice** to see bundled and registered voices with written
`AVAILABLE`/`UNAVAILABLE`/`FAULT` status. Select persists the voice transactionally;
Preview auditions it immediately. Import guides either:

- a cloned voice from a local reference-audio file; or
- a Piper `.onnx` model with its adjacent `.onnx.json` file, or an explicitly chosen
  config file.

Import validates the name and source, rejects case-insensitive duplicates, previews
when requested, and selects only after the earlier stages succeed. Cancellation or a
failed preview/config write rolls back only the new registration. Existing registered
voices, reference audio, and model caches are not replacement or cleanup targets.
The companion never silently changes to a fallback voice; an unavailable selected
voice produces a visible startup error and leaves the selection unchanged.

## Diagnostics and recovery

Open **Diagnostics** to inspect semantic, content-safe event names and export a JSON
support file. The live store is
`~/.config/talktomeclaude/companion-diagnostics.json`. Diagnostics and exports omit
audio, transcripts, answers, prompts, tokens, full home/reference paths, SSH secrets,
terminal contents, window titles, and persistent target identifiers. Export includes
a manifest of included and omitted categories. A recoverable or partially damaged
store is reported instead of being treated as user content.

Common fail-closed results:

- **Unsupported/no foreground terminal:** refocus a supported terminal, place the
  cursor, and start a new explicit delivery.
- **`pasted_not_submitted`:** inspect the pasted text and submit manually if correct.
- **Selected voice unavailable:** restore the referenced assets or choose an
  available voice; the companion does not substitute one.
- **Hotkey registration failed:** close the process holding `Ctrl+Alt+Space`, or use
  headless recovery.

## Recovery paths

Stage A keeps both established recovery surfaces:

```powershell
talktomeclaude companion --headless
talktomeclaude tui
```

Headless mode runs the production controller without Tk or the global hotkey. Its
commands are `status`, `start`, `finish`, `cancel`, `mute`, and `quit`; direct desktop
Settings, Voice, Review, and Diagnostics windows are intentionally unavailable.
`talktomeclaude tui` opens the legacy Textual dashboard. Running `talktomeclaude`
with no subcommand also remains the legacy/default dashboard during Stage A.

Rollback therefore changes only which explicit entry point you use. It does not
downgrade configuration, delete diagnostics or reply state, clear model caches, or
remove registered voice references. Restart the chosen process after any code update;
running Python processes do not reload edited modules.

## Stage boundary

Do not treat the Windows companion as the no-argument default until the full automated
and physical Windows/RTX/remote-assistant gates are complete. macOS and Linux
companions and remembered terminal targets across turns are not part of this release.
