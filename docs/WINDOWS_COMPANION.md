# Windows Companion

TalkToMeJohnny's Windows companion is an opt-in desktop path for local
microphone capture, speech recognition, and speech output. It is Windows 11
only. The companion does not replace the assistant CLI; it attaches to the
exact session you choose.

The assistant host can be any machine that can run the relevant CLI and hook
definition. The Windows companion only owns the local microphone, keyboard,
display, and audio path.

## Launch and exit

From an installed PowerShell environment, run:

```powershell
talktomejohnny companion
```

The selected desktop shell is Tk plus Win32 adapters. It shows live state,
current provider, and recovery paths without taking ownership of the assistant
terminal.

Quit the companion window to stop workers and release the global hotkey.

## Provider-neutral activation

Normal `claude` and `codex` sessions stay silent until they are explicitly
attached.

Use the exact session commands inside the assistant CLI you are already
running:

```text
/talktomejohnny on
/talktomejohnny off
/talktomejohnny status
```

for Claude Code, or:

```text
$talktomejohnny on
$talktomejohnny off
$talktomejohnny status
```

for Codex CLI.

The policy is simple:

- one active attachment globally;
- Claude and Codex reply streams stay isolated;
- detaching stops future completions from being spoken;
- an already accepted in-flight reply may still finish;
- if the companion or host lease is offline or stale, fail closed instead of
  replaying backlog.

## Choose the delivery terminal

You select the target for every turn. Before finishing push-toggle or releasing
the hold control:

1. Bring the intended supported terminal to the foreground.
2. Select the intended tab, pane, and shell yourself.
3. Place the blinking cursor in the exact input field that should receive the
   text.
4. Finish recording, then keep that terminal foregrounded until delivery
   completes.

Supported targets are Windows Terminal, a Windows console host, WezTerm,
Alacritty, and mintty when their expected process and window-class evidence
match. Notepad, browsers, editors, and unsupported or unverifiable windows are
rejected.

Finishing recording snapshots the currently foreground eligible terminal into
one ephemeral delivery transaction. The companion revalidates that same
evidence before touching the clipboard, before paste, and before optional Enter.
It never searches for another terminal, changes tabs or panes, inspects terminal
contents, or guesses whether a prompt is ready. The snapshot is discarded on
success or failure and is never remembered for the next turn.

If the target changes before paste, the companion stops without sending keys and
restores its clipboard change when safe. If text was pasted but the target
changes before Enter, it reports `pasted_not_submitted`, sends no Enter, and
keeps the transcript recoverable.

## Auto-submit

The Settings window contains the assistant auto-submit switch and this warning:

> Auto-submit sends Enter to the eligible foreground terminal captured at finish-toggle; the operator is responsible for the intended tab, pane, shell, and cursor position.

With assistant auto-submit on, an acceptable transcript is pasted once and
Enter is sent once, provided every revalidation succeeds. With it off, the
transcript is pasted but not submitted. Generic dictation never sends Enter.
Empty, low-confidence, edited, and recovery transcripts remain available for
explicit review rather than being guessed or silently injected.

The same settings can be inspected or changed from the CLI:

```powershell
talktomejohnny config get assistant-auto-submit
talktomejohnny config set assistant-auto-submit off
talktomejohnny config set assistant-auto-submit on
talktomejohnny config set recording-mode push-toggle
talktomejohnny config set recording-mode push-to-talk
```

## Spoken reply controls

While a reply is speaking, start a new recording to stop and park it
immediately. A recognized control such as `pause`, `continue`, `repeat`, `back`,
`next`, `topics`, `summarize`, `where were you`, `go back`, `keep going`,
`stop talking`, `voice off`, `help`, or `jump to <topic>` is handled locally.
It is not copied, pasted, submitted to the assistant, or allowed to reuse the
finish-time terminal snapshot. `go back` speaks a short recap before resuming
the parked answer.

Only normal-confidence assistant input takes this local path. Generic dictation
is always delivered as dictation, while low-confidence, edited, safety-stop, and
recovery text remains in the explicit review flow and is never silently
reinterpreted as a control.

## Voice settings and import

Open Voice to see bundled and registered voices with written
`AVAILABLE`/`UNAVAILABLE`/`FAULT` status. Select persists the voice
transactionally; Preview auditions it immediately. Import guides either:

- a cloned voice from a local reference-audio file; or
- a Piper `.onnx` model with its adjacent `.onnx.json` file, or an explicitly
  chosen config file.

Import validates the name and source, rejects case-insensitive duplicates,
previews when requested, and selects only after the earlier stages succeed.
Cancellation or a failed preview/config write rolls back only the new
registration. Existing registered voices, reference audio, and model caches are
not replacement or cleanup targets. The companion never silently changes to a
fallback voice; an unavailable selected voice produces a visible startup error
and leaves the selection unchanged.

## Diagnostics and recovery

Open Diagnostics to inspect semantic, content-safe event names and export a JSON
support file. The live store is `~/.config/talktomejohnny/companion-diagnostics.json`.
Diagnostics and exports omit audio, transcripts, answers, prompts, tokens, full
home/reference paths, SSH secrets, terminal contents, window titles, and
persistent target identifiers.

Common fail-closed results:

- Unsupported or no foreground terminal: refocus a supported terminal, place
  the cursor, and start a new explicit delivery.
- `pasted_not_submitted`: inspect the pasted text and submit manually if
  correct.
- Selected voice unavailable: restore the referenced assets or choose an
  available voice; the companion does not substitute one.
- Hotkey registration failed: close the process holding `Ctrl+Alt+Space`, or
  use headless recovery.

## Recovery paths

TalkToMeJohnny keeps both established recovery surfaces:

```powershell
talktomejohnny companion --headless
talktomejohnny tui
```

Headless mode runs the production controller without Tk or the global hotkey.
Its commands are `status`, `start`, `finish`, `cancel`, `mute`, and `quit`.
`talktomejohnny tui` opens the legacy Textual dashboard. Running
`talktomejohnny` with no subcommand still opens the default dashboard while the
rename migration is in progress.

Rollback changes only which explicit entry point you use. It does not downgrade
configuration, delete diagnostics or reply state, clear model caches, or remove
registered voice references. Restart the chosen process after any code update;
running Python processes do not reload edited modules.

## Stage boundary

Do not treat the Windows companion as the no-argument default until the full
automated and physical Windows gates are complete. macOS and Linux can host the
assistant CLI and hooks, but the desktop companion itself is Windows 11 only.

