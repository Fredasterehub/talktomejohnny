"""Optional, local wake-word detection.

The detection dependencies are deliberately imported only when detection is
started, so configuring TalkToMeJohnny never requires a wake-word engine.
"""

from __future__ import annotations

import time
from pathlib import Path
from threading import Event

from talktomeclaude.config import DEFAULT_WAKE_PHRASE


class WakeWordError(RuntimeError):
    """Base error for wake-word detection failures."""


class WakeWordUnavailable(WakeWordError):
    """Raised when an optional wake-word runtime dependency is unavailable."""


def _model_class():
    try:
        from openwakeword.model import Model
    except (ImportError, OSError) as exc:
        raise WakeWordUnavailable(
            "wake-word detection needs the optional openwakeword package"
        ) from exc
    return Model


def _sounddevice():
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise WakeWordUnavailable(
            "wake-word detection needs sounddevice and a working PortAudio install"
        ) from exc
    return sounddevice


def wait_for_wake_word(
    model_path: str | Path,
    *,
    phrase: str = DEFAULT_WAKE_PHRASE,
    threshold: float = 0.5,
    timeout: float | None = None,
    stop_event: Event | None = None,
) -> str | None:
    """Listen until a custom openWakeWord model detects its phrase.

    ``model_path`` identifies a trained openWakeWord model for ``phrase``.
    The microphone is sampled as 16-bit, 16 kHz mono PCM. The phrase is
    returned on detection; ``None`` is returned if ``timeout`` expires or
    ``stop_event`` is set.
    """
    if not 0.0 < threshold <= 1.0:
        raise ValueError("threshold must be greater than 0 and at most 1")
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative")

    try:
        model = _model_class()(wakeword_models=[str(model_path)])
        sounddevice = _sounddevice()
        try:
            import numpy
        except ImportError as exc:
            raise WakeWordUnavailable("wake-word detection needs numpy") from exc

        block_size = 1280  # 80 ms at the engine's required 16 kHz sample rate.
        deadline = None if timeout is None else time.monotonic() + timeout
        with sounddevice.RawInputStream(
            samplerate=16000,
            blocksize=block_size,
            channels=1,
            dtype="int16",
        ) as stream:
            while stop_event is None or not stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                audio, _overflowed = stream.read(block_size)
                scores = model.predict(numpy.frombuffer(audio, dtype=numpy.int16))
                if any(float(score) >= threshold for score in scores.values()):
                    return phrase
    except WakeWordError:
        raise
    except Exception as exc:
        raise WakeWordError(f"wake-word detection failed ({exc})") from exc
    return None
