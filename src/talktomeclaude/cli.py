"""Compatibility implementation behind the TalkToMeJohnny CLI."""

from pathlib import Path

import click

from talktomeclaude.transcript import iter_dialogue
from talktomeclaude.tts import (
    BUNDLED_VOICES,
    TTSError,
    default_voice,
    synthesize,
    voices_dir,
)


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Use voice as a medium for Claude Code or Codex CLI."""
    from talktomeclaude import config

    try:
        config.migrate_legacy_state()
    except config.ConfigLoadError as exc:
        # Transport hooks must fail closed without disrupting an assistant
        # turn. Interactive commands surface migration failures clearly.
        if ctx.invoked_subcommand != "hook":
            raise click.ClickException(str(exc)) from exc
    if ctx.invoked_subcommand is None:
        from talktomeclaude import onboarding

        if config.onboarding_needed(onboarding.CURRENT_ONBOARDING_VERSION):
            onboarding.run_onboarding()
        _launch_dashboard()


@main.command()
@click.option(
    "--reset",
    is_flag=True,
    help="Reset the stored onboarding version and run setup again.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Force the onboarding wizard to run even when setup is current.",
)
def setup(reset: bool, force: bool) -> None:
    """Run setup, with reset and force options for onboarding re-entry."""
    from talktomeclaude import config, onboarding

    if reset:
        config.set_onboarding_version(0)
    onboarding.run_onboarding()


@main.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "help_option_names": [],
    }
)
@click.argument("codex_args", nargs=-1, type=click.UNPROCESSED)
def codex(codex_args: tuple[str, ...]) -> None:
    """Launch the legacy wrapper for an explicitly voice-attached Codex session.

    All arguments after ``codex`` are passed through unchanged. The activation
    marker is inherited only by this Codex process and its lifecycle hooks, so
    unrelated Codex sessions cannot enter the companion reply spool. Normal
    provider-neutral activation uses ``$talktomejohnny on`` inside plain Codex.
    """

    import os
    import shutil
    import subprocess

    environment = os.environ.copy()
    environment["TALKTOMEJOHNNY_CODEX_ACTIVE"] = "1"
    environment["TALKTOMECLAUDE_CODEX_ACTIVE"] = "1"
    executable = shutil.which("codex")
    if executable is None:
        raise click.ClickException("Codex CLI was not found on PATH")
    try:
        completed = subprocess.run([executable, *codex_args], env=environment)
    except OSError as exc:
        raise click.ClickException("Codex CLI could not be launched") from exc
    raise SystemExit(completed.returncode)


@main.command()
@click.argument("text")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write the synthesized audio to this WAV file instead of playing it.",
)
@click.option(
    "--voice",
    "voice_name",
    default=None,
    help="Voice to use — any bundled, bring-your-own Piper, or cloned voice (see `voices`); "
    "defaults to the best bundled tier the hardware carries.",
)
def speak(text: str, out_path: Path | None, voice_name: str | None) -> None:
    """Synthesize TEXT to speech, fully locally.

    Piper voices run through the piper subprocess; a cloned voice (see
    `voice create`) renders through the optional cloning engine.
    """
    playback = out_path is None
    target_path = out_path or _temporary_wav_path("talktomejohnny-")
    voice_name = _resolve_default_voice(voice_name)
    try:
        voice = synthesize(
            text,
            target_path,
            voice_name,
            on_status=lambda message: click.echo(message, err=True),
        )
    except TTSError as exc:
        raise click.ClickException(str(exc)) from exc
    if not playback:
        click.echo(f"wrote {target_path} (voice: {voice.name})")
        return
    try:
        _play_wav(target_path)
    finally:
        target_path.unlink(missing_ok=True)


def _play_wav(path: Path) -> None:
    from talktomeclaude.tts import play_wav

    try:
        play_wav(path)
    except TTSError as exc:
        raise click.ClickException(f"{exc}; use --out to write a WAV file") from exc


def _temporary_wav_path(prefix: str) -> Path:
    import os
    import tempfile

    handle, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".wav")
    os.close(handle)
    return Path(raw_path)


def _resolve_default_voice(explicit: str | None) -> str | None:
    """Which voice speak/listen should use: an explicit --voice wins, else the
    persisted default-voice (falling back to the auto default, with a warning,
    if it no longer resolves). The Stop hook deliberately does NOT call this — it
    stays on the bundled Piper default so it never loads a clone every turn.
    """
    if explicit:
        return explicit
    from talktomeclaude import config as settings

    name = settings.default_voice_name()
    if not name:
        return None
    from talktomeclaude.tts import TTSError, get_voice

    try:
        get_voice(name)
    except TTSError:
        click.echo(f"default voice {name!r} is unavailable; using the auto default", err=True)
        return None
    return name


def _speak_reply(text: str) -> None:
    from talktomeclaude import config

    if not config.voice_assist_enabled():
        return
    wav_path = _temporary_wav_path("talktomejohnny-listen-")
    try:
        synthesize(text, wav_path, _resolve_default_voice(None))
        _play_wav(wav_path)
    except (TTSError, click.ClickException):
        pass
    finally:
        wav_path.unlink(missing_ok=True)


def _launch_dashboard() -> None:
    from talktomeclaude.tui import TUIError, run_dashboard

    try:
        run_dashboard(_speak_reply)
    except TUIError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
def ui() -> None:
    """Open the interactive voice dashboard."""
    _launch_dashboard()


@main.command()
def tui() -> None:
    """Open the legacy interactive voice dashboard."""
    _launch_dashboard()


@main.command()
@click.option(
    "--headless",
    is_flag=True,
    help="Start the non-graphical companion recovery path.",
)
def companion(headless: bool) -> None:
    """Start the staged Windows companion."""
    if headless:
        from talktomeclaude.companion.headless import run_headless

        try:
            run_headless(click.echo)
        except (OSError, RuntimeError) as exc:
            raise click.ClickException(str(exc)) from exc
        return

    from talktomeclaude.companion.app import (
        CompanionStartupError,
        run_desktop_companion,
    )

    try:
        run_desktop_companion()
    except (CompanionStartupError, OSError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@main.group(invoke_without_command=True)
@click.option(
    "--download",
    is_flag=True,
    help="Fetch all bundled voices into the local cache now, instead of on first use.",
)
@click.pass_context
def voices(ctx: click.Context, download: bool) -> None:
    """List available voices, or manage your own.

    Bundled voices are fetched from the Hugging Face Hub on first use and cached
    locally; run `voices --download` to pre-fetch them. Bring your own Piper
    voice with `voices add`, remove one with `voices remove`, or create a cloned
    voice with `voice create`.
    """
    if ctx.invoked_subcommand is not None:
        return

    from talktomeclaude import registry
    from talktomeclaude.tts import cache_voices_dir, voice_files

    directory = voices_dir()
    cache = cache_voices_dir()

    if download:
        for voice in BUNDLED_VOICES:
            try:
                model_path, _config = voice_files(
                    voice, on_status=lambda message: click.echo(message, err=True)
                )
            except TTSError as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo(f"{voice.name}: ready  ({model_path})")
        return

    default = default_voice(directory)
    for voice in BUNDLED_VOICES:
        if voice.is_installed(directory):
            state = "bundled"
        elif voice.is_installed(cache):
            state = "cached"
        else:
            state = "on-demand"
        marker = "  (default)" if voice.name == default.name else ""
        click.echo(
            f"{voice.name}  [{voice.language}, {voice.quality}, {state}]  "
            f"license: {voice.license}  ({voice.provenance}){marker}"
        )
    for voice in registry.list_voices():
        click.echo(
            f"{voice.name}  [{voice.language or '—'}, {voice.engine}, registered]  "
            f"license: {voice.license}  ({voice.provenance})"
        )


@voices.command("add")
@click.argument("name")
@click.argument("model", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to the voice's .onnx.json config (defaults to alongside the .onnx).",
)
@click.option("--language", default="", help="Language tag for the voice (informational).")
def voices_add(name: str, model: Path, config_path: Path | None, language: str) -> None:
    """Register your own Piper voice NAME from a MODEL .onnx file."""
    from talktomeclaude import registry

    try:
        voice = registry.add_piper(name, model, config_path, language=language)
    except registry.RegistryError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f'registered Piper voice {voice.name!r}  (use: speak --voice {voice.name} "...")')


@voices.command("remove")
@click.argument("name")
def voices_remove(name: str) -> None:
    """Remove a registered voice NAME (bundled voices cannot be removed)."""
    from talktomeclaude import registry

    try:
        registry.remove(name)
    except registry.RegistryError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"removed voice {name!r}")


@main.command()
def doctor() -> None:
    """Inspect this machine and recommend STT/TTS tiers and cloning setup."""
    from talktomeclaude.advisor import format_report

    click.echo(format_report())


@main.group()
def voice() -> None:
    """Create your own voices (cloning)."""


@voice.command("create")
@click.argument("name")
@click.option(
    "--reference",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Existing reference clip to clone from (~10-20s clean, single-speaker).",
)
@click.option(
    "--record",
    "record_seconds",
    type=float,
    default=None,
    help="Capture a reference clip from the microphone for this many seconds instead.",
)
@click.option(
    "--sample/--no-sample",
    "want_sample",
    default=True,
    help="Render a short test sample after registering (needs the cloning engine).",
)
@click.option("--sample-text", default=None, help="Text for the test sample.")
@click.option("--play", is_flag=True, help="Play the test sample after rendering.")
@click.option("--set-default", is_flag=True, help="Make this the default voice for speak/listen.")
def voice_create(
    name: str,
    reference: Path | None,
    record_seconds: float | None,
    want_sample: bool,
    sample_text: str | None,
    play: bool,
    set_default: bool,
) -> None:
    """Create a cloned voice NAME from a reference clip.

    Provide --reference PATH, or --record SECONDS to capture from the mic. A
    short test sample is rendered when the cloning engine is installed (run
    `doctor` for the install recipe); the voice registers either way.
    """
    from talktomeclaude import config as settings
    from talktomeclaude import registry, wizard

    if (reference is None) == (record_seconds is None):
        raise click.ClickException("provide exactly one of --reference PATH or --record SECONDS")

    def status(message: str) -> None:
        click.echo(message, err=True)
    capture: Path | None = None
    if record_seconds is not None:
        capture = _temporary_wav_path(f"ttmc-ref-{name}-")
        try:
            reference = wizard.record_reference(capture, seconds=record_seconds, on_status=status)
        except wizard.WizardError as exc:
            capture.unlink(missing_ok=True)
            raise click.ClickException(str(exc)) from exc

    text = (sample_text or wizard.DEFAULT_SAMPLE_TEXT) if want_sample else None
    try:
        created, sample = wizard.create_clone_voice(
            name, reference, sample_text=text, on_status=status
        )
    except (registry.RegistryError, wizard.WizardError, TTSError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        if capture is not None:
            capture.unlink(missing_ok=True)

    click.echo(f"created cloned voice {created.name!r}")
    if sample is not None:
        click.echo(f"test sample: {sample}")
        if play:
            _play_wav(sample)
    if set_default:
        settings.set_default_voice(name)
        click.echo(f"default voice set to {name!r}")


@main.command()
@click.argument(
    "audio",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--device",
    type=click.Choice(["auto", "cuda", "cpu"]),
    default=None,
    help="Hardware tier for this run; defaults to the persisted stt-device setting "
    "(auto-detect picks the GPU tier when CUDA is present).",
)
@click.option(
    "--model",
    "model_name",
    default=None,
    help="Override the Whisper model for the active tier (e.g. large-v3, small.en).",
)
@click.option(
    "--show-tier",
    is_flag=True,
    help="Print the active hardware tier without transcribing, then exit.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Report the active tier and any degradation on stderr.",
)
def transcribe(audio: Path, device: str | None, model_name: str | None, show_tier: bool, verbose: bool) -> None:
    """Transcribe an AUDIO file locally with Whisper-class speech-to-text.

    Auto-detects the hardware tier: a CUDA GPU carries the largest fluid
    model; CPU-only machines use the most accurate model that stays fluid.
    Tier fallbacks are always reported on stderr — never silent.
    """
    from talktomeclaude import config as settings
    from talktomeclaude.stt import STTError, detect_tier, transcribe_file

    device = device or settings.stt_device()

    if show_tier:
        try:
            click.echo(detect_tier(device, model_name).describe())
        except STTError as exc:
            raise click.ClickException(str(exc)) from exc
        return

    def report(message: str) -> None:
        if verbose or "degraded" in message:
            click.echo(message, err=True)

    try:
        text, _tier = transcribe_file(audio, device, model_name, on_status=report)
    except STTError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(text)


@main.command(name="filter")
@click.argument("transcript", type=click.File("r", encoding="utf-8"))
def filter_command(transcript) -> None:
    """Extract spoken dialogue from a transcript.

    Reads a Claude Code JSONL transcript (or - for stdin) and prints only
    Claude's assistant prose — never tool calls, tool results, code blocks,
    or thinking.
    """
    for dialogue in iter_dialogue(transcript):
        click.echo(dialogue)


@main.command()
@click.option(
    "--mode",
    type=click.Choice(["always-on", "push-to-talk", "push-toggle"]),
    default=None,
    help="Recording mode for this run; defaults to the persisted recording-mode setting.",
)
@click.option(
    "--session",
    "session_id",
    default=None,
    help="Claude Code session id to resume (claude -p --resume); omit to start a fresh session.",
)
@click.option(
    "--tmux-pane",
    default=None,
    help="Type transcripts into this live tmux pane via `tmux send-keys` instead of driving claude -p.",
)
@click.option(
    "--remote",
    "remote",
    default=None,
    help="Run Claude Code on a remote host over SSH (user@host): the mic, transcription and "
    "spoken reply stay local; Claude runs on the server. Persist it with "
    "`config set remote user@host`. Needs passwordless SSH keys and the claude CLI on the remote.",
)
@click.option(
    "--remote-cwd",
    default=None,
    help="Start remote Claude Code in this project directory for this run; "
    "defaults to the persisted remote-cwd setting.",
)
@click.option(
    "--device",
    type=click.Choice(["auto", "cuda", "cpu"]),
    default=None,
    help="Hardware tier for this run; defaults to the persisted stt-device setting.",
)
@click.option(
    "--model",
    "model_name",
    default=None,
    help="Override the Whisper model for the active tier (e.g. large-v3, small.en).",
)
@click.option(
    "--permission",
    type=click.Choice(["off", "skip", "acceptEdits", "bypassPermissions"]),
    default=None,
    help="Claude Code permission posture for this run; persist it with "
    "`config set claude-permissions`.",
)
@click.option("--once", is_flag=True, help="Handle a single utterance, then exit.")
def listen(
    mode: str | None,
    session_id: str | None,
    tmux_pane: str | None,
    remote: str | None,
    remote_cwd: str | None,
    device: str | None,
    model_name: str | None,
    permission: str | None,
    once: bool,
) -> None:
    """Listen to the microphone and drive Claude Code by voice.

    Captures speech in the selected recording mode, transcribes it locally,
    and injects the text into Claude Code. The primary path drives the
    loop's own `claude -p --resume` session and speaks each reply; with
    --tmux-pane the transcript is typed into a live interactive TUI via
    `tmux send-keys` instead. One driver per session: never point the voice
    loop and a live interactive window at the same session.

    Runs anywhere: on the machine you sit at (mic, Claude, speakers all local —
    the default), or split with --remote user@host so the voice stays local
    while Claude Code runs on a server (the remote/SSH pattern).

    \b
    Recording modes (persist a default with `config set recording-mode`):
      always-on     hands-free capture, VAD-gated; an utterance ends at a pause
      push-to-talk  hold any key to record; releasing it sends the utterance
      push-toggle   tap a key to start recording, tap again to send
    """
    from talktomeclaude import config
    from talktomeclaude.listen import ListenError, run_listen

    active_mode = mode or config.recording_mode()
    active_device = device or config.stt_device()
    active_permission = permission or config.claude_permissions()
    active_remote = remote or config.remote()
    if remote_cwd is not None and not active_remote:
        raise click.ClickException("--remote-cwd requires --remote or a persisted remote")
    active_remote_cwd = (
        remote_cwd if remote_cwd is not None else config.remote_cwd()
    ) if active_remote else None

    try:
        run_listen(
            mode=active_mode,
            session_id=session_id,
            tmux_pane=tmux_pane,
            device=active_device,
            model=model_name,
            once=once,
            echo=click.echo,
            speak=_speak_reply,
            status=lambda message: click.echo(message, err=True),
            remote=active_remote,
            remote_cwd=active_remote_cwd,
            permission=active_permission,
        )
    except ListenError as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        click.echo("listen stopped", err=True)


@main.group()
def config() -> None:
    """Read and write settings persisted across invocations."""


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Persist KEY = VALUE.

    Known keys: recording-mode (always-on, push-to-talk, push-toggle),
    voice-assist (on, off), assistant-auto-submit (on, off),
    assistant-provider (both, claude, codex),
    remote (user@host, or "local"/"none" to clear),
    remote-cwd (remote project path, or "home"/"none" to clear),
    barge-in (on, off), claude-permissions (off, skip, acceptEdits,
    bypassPermissions), wake-word (on, off), wake-phrase (the spoken phrase),
    wake-model (path to a trained wake-word model, or "none" to clear),
    default-voice (a voice name, or "auto"/"none" to clear),
    stt-device (auto, cuda, cpu), command-namespace-policy (allow-all,
    ask-first-use, allowlist), and command-namespace-allowlist
    (comma-separated namespaces, or "none" to clear).
    """
    from talktomeclaude import config as settings

    if key == "recording-mode":
        try:
            settings.set_recording_mode(value)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    elif key == "voice-assist":
        if value not in ("on", "off"):
            raise click.ClickException(
                f"invalid voice-assist value {value!r}: expected on or off"
            )
        settings.set_voice_assist(value == "on")
    elif key == "assistant-auto-submit":
        if value not in ("on", "off"):
            raise click.ClickException(
                f"invalid assistant-auto-submit value {value!r}: expected on or off"
            )
        settings.set_assistant_auto_submit(value == "on")
    elif key == "assistant-provider":
        try:
            settings.set_assistant_provider(value)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    elif key == "remote":
        settings.set_remote(None if value.lower() in ("", "local", "none", "off") else value)
    elif key == "remote-cwd":
        settings.set_remote_cwd(
            None if value.lower() in ("", "home", "none", "off") else value
        )
    elif key == "barge-in":
        if value not in ("on", "off"):
            raise click.ClickException(
                f"invalid barge-in value {value!r}: expected on or off"
            )
        settings.set_barge_in(value == "on")
    elif key == "claude-permissions":
        try:
            settings.set_claude_permissions(value)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    elif key == "wake-word":
        if value not in ("on", "off"):
            raise click.ClickException(
                f"invalid wake-word value {value!r}: expected on or off"
            )
        settings.set_wake_word(value == "on")
    elif key == "wake-phrase":
        settings.set_wake_phrase(value)
    elif key == "wake-model":
        settings.set_wake_model_path(
            None if value.lower() in ("", "none", "off") else value
        )
    elif key == "default-voice":
        settings.set_default_voice(
            None if value.lower() in ("", "auto", "none", "default") else value
        )
    elif key == "stt-device":
        try:
            settings.set_stt_device(value)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    elif key == "command-namespace-policy":
        try:
            settings.set_command_namespace_policy(value)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    elif key == "command-namespace-allowlist":
        settings.set_command_namespace_allowlist(
            None if value.lower() in ("", "none", "off") else value
        )
    else:
        raise click.ClickException(
            f"unknown setting {key!r}: expected recording-mode, voice-assist, "
            "assistant-auto-submit, assistant-provider, remote, "
            "remote-cwd, barge-in, claude-permissions, wake-word, wake-phrase, "
            "wake-model, default-voice, stt-device, command-namespace-policy, "
            "or command-namespace-allowlist"
        )
    click.echo(f"{key} = {value}")


@config.command("get")
@click.argument("key")
def config_get(key: str) -> None:
    """Print the persisted value of KEY (or its default)."""
    from talktomeclaude import config as settings

    if key == "recording-mode":
        click.echo(settings.recording_mode())
    elif key == "voice-assist":
        click.echo("on" if settings.voice_assist_enabled() else "off")
    elif key == "assistant-auto-submit":
        click.echo("on" if settings.assistant_auto_submit_enabled() else "off")
    elif key == "assistant-provider":
        click.echo(settings.assistant_provider())
    elif key == "remote":
        click.echo(settings.remote() or "local")
    elif key == "remote-cwd":
        click.echo(settings.remote_cwd() or "home")
    elif key == "barge-in":
        click.echo("on" if settings.barge_in_enabled() else "off")
    elif key == "claude-permissions":
        click.echo(settings.claude_permissions())
    elif key == "wake-word":
        click.echo("on" if settings.wake_word_enabled() else "off")
    elif key == "wake-phrase":
        click.echo(settings.wake_phrase())
    elif key == "wake-model":
        click.echo(settings.wake_model_path() or "none")
    elif key == "default-voice":
        click.echo(settings.default_voice_name() or "auto")
    elif key == "stt-device":
        click.echo(settings.stt_device())
    elif key == "command-namespace-policy":
        click.echo(settings.command_namespace_policy())
    elif key == "command-namespace-allowlist":
        click.echo(", ".join(settings.command_namespace_allowlist()) or "none")
    else:
        raise click.ClickException(
            f"unknown setting {key!r}: expected recording-mode, voice-assist, "
            "assistant-auto-submit, assistant-provider, remote, "
            "remote-cwd, barge-in, claude-permissions, wake-word, wake-phrase, "
            "wake-model, default-voice, stt-device, command-namespace-policy, "
            "or command-namespace-allowlist"
        )


@main.command()
@click.argument("state", type=click.Choice(["on", "off", "status"]))
def assist(state: str) -> None:
    """Switch voice-assist (spoken replies) on or off, or query its state.

    With voice-assist off the Stop hook stays silent; this is the full-mute
    switch, persisted across invocations.
    """
    from talktomeclaude import config

    if state == "status":
        click.echo("on" if config.voice_assist_enabled() else "off")
        return
    config.set_voice_assist(state == "on")
    click.echo(f"voice-assist {state}")


@main.group()
def hook() -> None:
    """Entry points invoked by supported assistant lifecycle hooks."""


@hook.command("install")
@click.option(
    "--settings",
    "settings_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Hook JSON path (defaults to the selected provider's user hook file).",
)
@click.option(
    "--provider",
    type=click.Choice(["claude", "codex"]),
    default="claude",
    show_default=True,
)
def hook_install(settings_path: Path | None, provider: str) -> None:
    """Install the owned lifecycle hooks and visible control command."""

    from talktomeclaude.assistant.hooks import (
        ClaudeHookManager,
        CodexHookManager,
        HookSettingsError,
        resolve_hook_executable,
    )
    from talktomeclaude.assistant.skills import (
        SkillInstallError,
        install_control_skills,
    )
    from talktomeclaude import config

    try:
        config.migrate_legacy_state()
    except config.ConfigLoadError as exc:
        raise click.ClickException(str(exc)) from exc

    executable = resolve_hook_executable()
    target = settings_path or (
        Path.home() / ".claude" / "settings.json"
        if provider == "claude"
        else Path.home() / ".codex" / "hooks.json"
    )
    manager = (
        ClaudeHookManager(target, executable=executable)
        if provider == "claude"
        else CodexHookManager(target, executable=executable)
    )
    try:
        inspection = manager.install()
        install_control_skills(provider)
    except (HookSettingsError, SkillInstallError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"companion lifecycle hooks {inspection.status.value}")


@hook.command("status")
@click.option(
    "--settings",
    "settings_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
)
@click.option(
    "--provider",
    type=click.Choice(["claude", "codex"]),
    default="claude",
    show_default=True,
)
def hook_status(settings_path: Path | None, provider: str) -> None:
    """Report only this companion's owned lifecycle-hook status."""

    from talktomeclaude.assistant.hooks import (
        ClaudeHookManager,
        CodexHookManager,
        HookSettingsError,
        resolve_hook_executable,
    )

    target = settings_path or (
        Path.home() / ".claude" / "settings.json"
        if provider == "claude"
        else Path.home() / ".codex" / "hooks.json"
    )
    executable = resolve_hook_executable()
    manager = (
        ClaudeHookManager(target, executable=executable)
        if provider == "claude"
        else CodexHookManager(target, executable=executable)
    )
    try:
        inspection = manager.inspect()
    except HookSettingsError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(inspection.status.value)


@hook.command("stream", hidden=True)
@click.option(
    "--provider",
    type=click.Choice(["claude", "codex"]),
    default="claude",
    hidden=True,
)
def hook_stream(provider: str) -> None:
    """Run the durable reply stream with this console script's Python."""

    from talktomeclaude.assistant import SessionAttachmentRegistry
    from talktomeclaude.assistant.session_control import (
        LiveLeaseHeartbeatOwner,
        attachment_state_path,
    )
    from talktomeclaude.reply.remote import main as run_remote_stream

    from talktomeclaude import config as settings

    arguments = ["stream"]
    if provider == "codex":
        arguments.extend(
            [
                "--spool-root",
                str(settings.config_dir() / "reply-spool-codex"),
            ]
        )
    registry = SessionAttachmentRegistry(attachment_state_path())
    with LiveLeaseHeartbeatOwner(registry, provider):
        raise SystemExit(run_remote_stream(arguments))


@hook.command("session", hidden=True)
@click.option(
    "--provider",
    type=click.Choice(["claude", "codex"]),
    required=True,
    hidden=True,
)
@click.option("--owner-marker", required=True, hidden=True)
def hook_session(provider: str, owner_marker: str) -> None:
    """Apply an exact-session TalkToMeJohnny attach/detach command."""

    import json
    import os
    import sys

    from talktomeclaude.assistant import SessionAttachmentRegistry
    from talktomeclaude.assistant.hooks import (
        CLAUDE_SESSION_CONTROL_MARKER,
        CODEX_SESSION_CONTROL_MARKER,
    )
    from talktomeclaude.assistant.session_control import (
        attachment_state_path,
        handle_session_control_event,
        read_session_control_event,
    )

    expected_marker = (
        CLAUDE_SESSION_CONTROL_MARKER
        if provider == "claude"
        else CODEX_SESSION_CONTROL_MARKER
    )
    if owner_marker != expected_marker:
        return
    event = read_session_control_event(sys.stdin)
    if event is None:
        return
    try:
        result = handle_session_control_event(
            event,
            provider=provider,
            registry=SessionAttachmentRegistry(attachment_state_path(os.environ)),
        )
    except Exception:
        # Lifecycle hooks must never prevent the assistant from closing or a
        # normal user prompt from reaching the model.
        return
    if result is not None:
        click.echo(
            json.dumps(
                result,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )


@hook.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be spoken as a SPEAK: line instead of playing audio.",
)
@click.option("--transport", is_flag=True, hidden=True)
@click.option("--owner-marker", hidden=True)
@click.option(
    "--provider",
    type=click.Choice(["claude", "codex"]),
    default="claude",
    hidden=True,
)
def stop(
    dry_run: bool,
    transport: bool,
    owner_marker: str | None,
    provider: str,
) -> None:
    """Handle a Stop event from a supported assistant CLI.

    Reads the hook event JSON from stdin and speaks the dialogue of
    last_assistant_message through the local TTS voice. Honors the
    voice-assist mute switch and never blocks the assistant CLI — every
    outcome exits 0.
    """
    import os
    import sys

    from talktomeclaude import config
    from talktomeclaude.hook import (
        read_stop_event,
        record_transport_fault,
        stop_dialogue,
        transport_stop_event,
    )

    event = read_stop_event(sys.stdin)
    if transport:
        from talktomeclaude.assistant import (
            CODEX_OWNED_HOOK_MARKER,
            OWNED_HOOK_MARKER,
        )

        expected_marker = (
            OWNED_HOOK_MARKER
            if provider == "claude"
            else CODEX_OWNED_HOOK_MARKER
        )
        active = False
        if event is not None:
            from talktomeclaude.assistant import SessionAttachmentRegistry
            from talktomeclaude.assistant.session_control import (
                attachment_state_path,
                session_is_attached,
            )

            active = session_is_attached(
                event,
                provider=provider,
                registry=SessionAttachmentRegistry(
                    attachment_state_path(os.environ)
                ),
            )
        if provider == "codex" and (
            os.environ.get("TALKTOMEJOHNNY_CODEX_ACTIVE") == "1"
            or os.environ.get("TALKTOMECLAUDE_CODEX_ACTIVE") == "1"
        ):
            active = True
        if event is not None and owner_marker == expected_marker and active:
            try:
                if provider == "claude":
                    transport_result = transport_stop_event(
                        event, environment=os.environ
                    )
                    if transport_result is None:
                        record_transport_fault(
                            "invalid_stop_event", environment=os.environ
                        )
                    elif transport_result.code.value not in {
                        "accepted",
                        "suppressed_role",
                        "suppressed_session",
                        "suppressed_correlation",
                    }:
                        record_transport_fault(
                            transport_result.code.value, environment=os.environ
                        )
                else:
                    from talktomeclaude.assistant.codex import (
                        CodexStopPayloadError,
                        CodexStopTransportError,
                        transport_stop_event as transport_codex_stop_event,
                    )

                    try:
                        transport_codex_stop_event(
                            event,
                            spool_root=(
                                Path(
                                    os.environ.get("TALKTOMEJOHNNY_REPLY_SPOOL")
                                    or os.environ["TALKTOMECLAUDE_REPLY_SPOOL"]
                                )
                                if (
                                    os.environ.get("TALKTOMEJOHNNY_REPLY_SPOOL")
                                    or os.environ.get("TALKTOMECLAUDE_REPLY_SPOOL")
                                )
                                else config.config_dir() / "reply-spool-codex"
                            ),
                        )
                    except CodexStopPayloadError as exc:
                        record_transport_fault(exc.code.value, environment=os.environ)
                    except CodexStopTransportError as exc:
                        record_transport_fault(exc.code.value, environment=os.environ)
            except Exception as exc:
                # A hook must never block the assistant CLI. Durable failures remain
                # visible through spool/transport diagnostics and replay state.
                try:
                    record_transport_fault(
                        f"exception_{type(exc).__name__}", environment=os.environ
                    )
                except Exception:
                    pass
        return
    if event is None or not config.voice_assist_enabled():
        return
    dialogue = stop_dialogue(event)
    if not dialogue:
        return
    if dry_run:
        click.echo("SPEAK: " + " ".join(dialogue.split()))
        return
    wav_path = _temporary_wav_path("talktomejohnny-hook-")
    try:
        synthesize(dialogue, wav_path, None)
        _play_wav(wav_path)
    except (TTSError, click.ClickException):
        pass
    finally:
        wav_path.unlink(missing_ok=True)
