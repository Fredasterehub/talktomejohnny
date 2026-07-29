"""Guided voice creation.

The core, :func:`create_clone_voice`, is headless-testable: it registers a
cloned voice and — when the optional engine is installed — renders a short test
sample so the user can hear the result immediately. Registration works even
before the engine is installed (register now, install later), so the wizard
never dead-ends. :func:`record_reference` captures a reference clip from the
microphone and is kept separate because it needs audio hardware.
"""

import wave
from pathlib import Path

from talktomeclaude import config
from talktomeclaude import registry
from talktomeclaude.clone import clone_available

DEFAULT_SAMPLE_TEXT = "Hello, this is my new voice, running locally on my own machine."
_REFERENCE_SAMPLE_RATE = 24000


class WizardError(RuntimeError):
    """Raised when guided voice creation cannot proceed."""


def samples_dir() -> Path:
    return config.preferred_cache_dir("samples")


def create_clone_voice(
    name: str,
    reference_path: str | Path,
    *,
    sample_text: str | None = None,
    sample_out: str | Path | None = None,
    exaggeration: float = 0.5,
    cfg_weight: float = 0.5,
    language: str = "en",
    provenance: str = "voice clone (timbre only)",
    on_status=None,
) -> tuple["registry.RegisteredVoice", Path | None]:
    """Register a cloned voice and, if the engine is installed and *sample_text*
    is given, render a test sample. Returns (voice, sample_path_or_None)."""
    status = on_status or (lambda _message: None)
    voice = registry.add_clone(
        name,
        reference_path,
        exaggeration=exaggeration,
        cfg_weight=cfg_weight,
        language=language,
        provenance=provenance,
    )
    status(f"registered cloned voice {name!r}")

    if not sample_text:
        return voice, None
    if not clone_available():
        status(
            "the cloning engine is not installed yet, so no test sample was rendered. "
            "Run `talktomeclaude doctor` for the install recipe, then "
            f'`talktomeclaude speak --voice {name} "..."`.'
        )
        return voice, None

    from talktomeclaude import tts

    sample_path = Path(sample_out) if sample_out else samples_dir() / f"{name}.wav"
    status(f"rendering a test sample to {sample_path} (the first run loads the model)…")
    try:
        tts.synthesize(sample_text, sample_path, voice_name=name, on_status=status)
    except Exception as exc:  # keep the voice registered; surface the render failure
        raise WizardError(f"the voice was registered, but the test sample failed: {exc}") from exc
    return voice, sample_path


def record_reference(
    out_path: str | Path,
    *,
    seconds: float = 15.0,
    sample_rate: int = _REFERENCE_SAMPLE_RATE,
    on_status=None,
) -> Path:
    """Record *seconds* of mono reference audio from the microphone to a WAV.

    Needs audio hardware, so it is isolated from the headless-testable core.
    """
    status = on_status or (lambda _message: None)
    if seconds <= 0:
        raise WizardError("recording length must be positive")
    try:
        import sounddevice
    except Exception as exc:  # missing lib or no audio backend
        raise WizardError(f"microphone capture needs sounddevice: {exc}") from exc
    import numpy as np

    status(f"recording {seconds:.0f}s of reference audio — speak now…")
    try:
        frames = int(seconds * sample_rate)
        audio = sounddevice.rec(frames, samplerate=sample_rate, channels=1, dtype="int16")
        sounddevice.wait()
    except Exception as exc:
        raise WizardError(f"microphone capture failed: {exc}") from exc

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(np.asarray(audio, dtype="<i2").tobytes())
    status(f"saved reference clip to {out} ({seconds:.0f}s)")
    return out
