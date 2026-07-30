"""Local voice resolution, synthesis, and playback implementation.

Piper's lineage is GPL-3.0, so it is never imported as a library: the plugin
stays MIT by driving the ``piper`` executable through a subprocess boundary
only. Bundled voices are from-scratch public-domain Piper trains, fetched on
first use from the Hugging Face Hub and cached locally (a bundled/override
``voices/`` directory is honored first for offline use).

Beyond the three bundled voices, :func:`get_voice` resolves user voices through
the :mod:`registry`: bring-your-own Piper voices synthesize through the same
subprocess path, while cloned voices dispatch to the optional :mod:`clone`
engine. The engine choice rides on each :class:`~catalog.Voice`'s ``engine``
field; the Piper path is unchanged from the sealed build. The legacy
``talktomeclaude.tts`` module re-exports this surface as a compatibility facade.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from talktomeclaude import config
from talktomeclaude import registry
from talktomeclaude.catalog import BUNDLED_VOICES, HF_VOICES_REPO, Voice

_QUALITY_RANK = {"low": 0, "x_low": 0, "medium": 1, "high": 2}


class TTSError(RuntimeError):
    """Raised when speech synthesis cannot proceed."""


def voices_dir() -> Path:
    override = os.environ.get("TALKTOMEJOHNNY_VOICES_DIR") or os.environ.get(
        "TALKTOMECLAUDE_VOICES_DIR"
    )
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "voices"


def cache_voices_dir() -> Path:
    """Local cache where downloaded voices are materialized (flat names)."""
    override = os.environ.get("TALKTOMEJOHNNY_VOICES_CACHE") or os.environ.get(
        "TALKTOMECLAUDE_VOICES_CACHE"
    )
    if override:
        return Path(override)
    return config.preferred_cache_dir("voices")


def _download_voice(voice: Voice, on_status=None) -> tuple[Path, Path]:
    rel = voice.params.get("hf_path")
    if not rel:
        raise TTSError(f"no download source registered for voice {voice.name!r}")
    dest = cache_voices_dir()
    model_dst = voice.model_path(dest)
    config_dst = voice.config_path(dest)
    if model_dst.is_file() and config_dst.is_file():
        return model_dst, config_dst
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise TTSError(
            "huggingface_hub is required to download voices (pip install huggingface_hub), "
            "or place the voice files under TALKTOMECLAUDE_VOICES_DIR"
        ) from exc
    status = on_status or (lambda _message: None)
    status(f"downloading voice {voice.name} from {HF_VOICES_REPO} (once; caching to {dest})")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        model_src = hf_hub_download(repo_id=HF_VOICES_REPO, filename=rel)
        config_src = hf_hub_download(repo_id=HF_VOICES_REPO, filename=rel + ".json")
    except Exception as exc:  # network / hub errors surface as a clean TTS error
        raise TTSError(f"failed to download voice {voice.name!r} from {HF_VOICES_REPO}: {exc}") from exc
    shutil.copyfile(model_src, model_dst)
    shutil.copyfile(config_src, config_dst)
    return model_dst, config_dst


def voice_files(voice: Voice, on_status=None) -> tuple[Path, Path]:
    """Resolve (model, config) paths for a Piper *voice*, fetching on demand.

    A bring-your-own voice carries explicit ``model``/``config`` paths in its
    params and is used in place. A bundled voice resolves in order: the
    bundled/override ``voices/`` directory (offline), the local download cache,
    then a one-time download from the Hub.
    """
    model = voice.params.get("model")
    config = voice.params.get("config")
    if model and config:
        model_path, config_path = Path(model), Path(config)
        if not model_path.is_file() or not config_path.is_file():
            raise TTSError(
                f"registered voice {voice.name!r} points at missing files "
                f"({model_path} / {config_path})"
            )
        return model_path, config_path
    bundled = voices_dir()
    if voice.is_installed(bundled):
        return voice.model_path(bundled), voice.config_path(bundled)
    cache = cache_voices_dir()
    if voice.is_installed(cache):
        return voice.model_path(cache), voice.config_path(cache)
    return _download_voice(voice, on_status)


def is_available(voice: Voice) -> bool:
    """True if the voice is usable now (Piper voices: no download needed; a
    clone: the reference clip is present and the cloning engine is installed —
    model weights may still download on first use)."""
    if voice.engine == "clone":
        reference = voice.params.get("reference")
        if not (reference and Path(reference).is_file()):
            return False
        try:
            from talktomeclaude import clone

            return clone.clone_available()
        except Exception:
            return False
    if voice.engine == "f5":
        reference = voice.params.get("reference")
        ref_text = voice.params.get("ref_text")
        if not (
            reference
            and Path(reference).is_file()
            and isinstance(ref_text, str)
            and ref_text.strip()
        ):
            return False
        try:
            from talktomeclaude import f5

            return f5.f5_available()
        except Exception:
            return False
    model = voice.params.get("model")
    config = voice.params.get("config")
    if model and config:
        return Path(model).is_file() and Path(config).is_file()
    return voice.is_installed(voices_dir()) or voice.is_installed(cache_voices_dir())


def get_voice(name: str) -> Voice:
    """Resolve *name* to a Voice: a bundled voice, else a registered one."""
    for voice in BUNDLED_VOICES:
        if voice.name == name:
            return voice
    try:
        registered = registry.get(name)
    except registry.RegistryError as exc:
        raise TTSError(str(exc)) from exc
    if registered is not None:
        quality = "n/a" if registered.engine in {"clone", "f5"} else "medium"
        return Voice(
            name=registered.name,
            language=registered.language,
            quality=quality,
            license=registered.license,
            provenance=registered.provenance,
            engine=registered.engine,
            params=dict(registered.params),
        )
    bundled = ", ".join(v.name for v in BUNDLED_VOICES)
    registered_names = ", ".join(v.name for v in registry.list_voices()) or "none"
    raise TTSError(
        f"unknown voice {name!r} (bundled: {bundled}; registered: {registered_names})"
    )


def _hardware_allows_high_tier() -> bool:
    """Hardware-tier auto-detection (directive D-1).

    Piper's high-quality profile stays faster than realtime on any
    multi-core CPU, and a visible CUDA GPU always carries it; only very
    small machines drop to the medium profile. One code path either way.
    """
    if shutil.which("nvidia-smi"):
        return True
    return (os.cpu_count() or 1) >= 4


def default_voice(directory: Path | None = None) -> Voice:
    """The best bundled Piper voice for this machine.

    Deliberately bundled-only: this is what the per-turn Stop hook speaks with,
    so it must never be a clone (a fresh subprocess must not load 2 GB and touch
    the GPU every turn). A user's cloned default is used only where it is named
    explicitly (the persistent ``listen`` loop and ``speak --voice``).
    """
    directory = directory or voices_dir()
    cache = cache_voices_dir()
    # Prefer a voice already on disk (bundled or cached) so the default never
    # forces a download when a usable voice is present; otherwise consider all
    # bundled voices and let the best one fetch on demand.
    available = [v for v in BUNDLED_VOICES if v.is_installed(directory) or v.is_installed(cache)]
    candidates = available or list(BUNDLED_VOICES)
    max_rank = 2 if _hardware_allows_high_tier() else 1
    eligible = [v for v in candidates if _QUALITY_RANK.get(v.quality, 1) <= max_rank]
    if not eligible:
        eligible = candidates
    return max(eligible, key=lambda v: _QUALITY_RANK.get(v.quality, 1))


def _piper_executable() -> str:
    local = Path(sys.executable).parent / "piper"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    found = shutil.which("piper")
    if found:
        return found
    raise TTSError(
        "piper executable not found; install it into the project venv "
        "(pip install piper-tts)"
    )


def _synthesize_clone(text: str, out_path: Path, voice: Voice) -> Voice:
    from talktomeclaude import clone  # optional engine, imported lazily

    reference = voice.params.get("reference")
    if not reference:
        raise TTSError(f"cloned voice {voice.name!r} has no reference clip")
    clone.synthesize_clone(
        text,
        Path(out_path),
        reference,
        exaggeration=voice.params.get("exaggeration", 0.5),
        cfg_weight=voice.params.get("cfg_weight", 0.5),
    )
    return voice


def _synthesize_f5(text: str, out_path: Path, voice: Voice) -> Voice:
    from talktomeclaude import f5  # optional engine, imported lazily

    reference = voice.params.get("reference")
    ref_text = voice.params.get("ref_text")
    if not reference:
        raise TTSError(f"F5 voice {voice.name!r} has no reference clip")
    if not isinstance(ref_text, str) or not ref_text.strip():
        raise TTSError(f"F5 voice {voice.name!r} has no reference text")
    f5.synthesize_f5(text, Path(out_path), reference, ref_text)
    return voice


def _synthesize_with(
    text: str,
    out_path: Path,
    voice_name: str | None = None,
    on_status=None,
    *,
    get_voice_fn,
    default_voice_fn,
    voice_files_fn,
    piper_executable_fn,
) -> Voice:
    """Implementation seam used by the legacy facade's patchable symbols."""
    voice = get_voice_fn(voice_name) if voice_name else default_voice_fn()
    if voice.engine == "clone":
        return _synthesize_clone(text, Path(out_path), voice)
    if voice.engine == "f5":
        return _synthesize_f5(text, Path(out_path), voice)
    model_path, config_path = voice_files_fn(voice, on_status)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            piper_executable_fn(),
            "--model", str(model_path),
            "--config", str(config_path),
            "--output-file", str(out_path),
        ],
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise TTSError(f"piper failed (exit {result.returncode}): {detail}")
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise TTSError(f"piper produced no audio at {out_path}")
    return voice


def synthesize(
    text: str,
    out_path: Path,
    voice_name: str | None = None,
    on_status=None,
) -> Voice:
    """Render *text* to a WAV file at *out_path*, fully locally.

    Returns the voice used. Piper voices (bundled or bring-your-own) run through
    the ``piper`` subprocess only (GPL isolation); a cloned voice dispatches to
    the optional clone engine.
    """
    return _synthesize_with(
        text,
        out_path,
        voice_name,
        on_status,
        get_voice_fn=get_voice,
        default_voice_fn=default_voice,
        voice_files_fn=voice_files,
        piper_executable_fn=_piper_executable,
    )


def _scale_to_volume(samples, numpy, volume: int):
    gain = max(0.0, min(1.0, volume / 100))
    if gain == 1.0:
        return samples
    scaled = samples.astype(numpy.float32) * gain
    return numpy.clip(scaled, -32768, 32767).astype(samples.dtype)


def play_wav(path: Path) -> None:
    """Play a WAV file through the default output device, blocking."""
    import wave

    try:
        import numpy
        import sounddevice
    except (ImportError, OSError) as exc:
        raise TTSError(f"audio playback unavailable ({exc})") from exc
    from talktomeclaude import config

    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        samples = numpy.frombuffer(frames, dtype=numpy.int16)
        channels = handle.getnchannels()
        if channels > 1:
            samples = samples.reshape(-1, channels)
        volume = config.output_volume()
        if volume < 100:
            samples = _scale_to_volume(samples, numpy, volume)
        sounddevice.play(samples, samplerate=handle.getframerate(), blocking=True)


def _synthesize_and_play_with(
    text: str,
    voice_name: str | None = None,
    on_status=None,
    *,
    synthesize_fn,
    play_wav_fn,
) -> Voice:
    """Render *text* and play it immediately; the audition path onboarding and
    the dashboard share. Blocking — callers run it off the UI thread."""
    import tempfile

    handle, raw_path = tempfile.mkstemp(prefix="talktomeclaude-audition-", suffix=".wav")
    os.close(handle)
    out_path = Path(raw_path)
    try:
        voice = synthesize_fn(text, out_path, voice_name, on_status)
        play_wav_fn(out_path)
        return voice
    finally:
        out_path.unlink(missing_ok=True)


def synthesize_and_play(
    text: str,
    voice_name: str | None = None,
    on_status=None,
) -> Voice:
    """Render *text* and play it immediately; callers keep it off the UI thread."""
    return _synthesize_and_play_with(
        text,
        voice_name,
        on_status,
        synthesize_fn=synthesize,
        play_wav_fn=play_wav,
    )
