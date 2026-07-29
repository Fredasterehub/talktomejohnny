"""Optional voice-cloning engine (Chatterbox, Resemble AI).

Import-safe without the cloning stack installed: ``torch`` and ``chatterbox``
are imported lazily inside the functions, so ``import clone`` never fails and
the core stays dependency-light and MIT. The engine and its ~2 GB of weights are
an optional, user-installed extra (see :mod:`advisor` for the install recipe),
never a hard dependency and never redistributed.

The model is loaded once and cached for the process: the persistent ``listen``
loop reuses it, while the per-turn Stop hook deliberately stays on Piper (a
fresh subprocess must not load 2 GB and touch the GPU every turn).

Audio is written with the stdlib :mod:`wave` module rather than
``torchaudio.save`` — torchaudio 2.11 routes ``save`` through the optional
``torchcodec`` package, which is not part of the pinned set.
"""

import importlib
import importlib.machinery
import os
import sys
import types
import wave
from pathlib import Path

import numpy as np

from talktomeclaude import config
from talktomeclaude.speech.voices import TTSError

_SAMPLE_RATE_FALLBACK = 24000
_YTDLP_MAX_FILESIZE = "250M"
_model = None  # cached ChatterboxTTS singleton for this process


class CloneError(TTSError):
    """Raised when voice cloning cannot proceed."""


def ytdlp_command(url: str, dest: str) -> list[str]:
    """Build the yt-dlp argv for downloading a YouTube reference audio source."""
    return [
        "yt-dlp",
        "--no-playlist",
        "--format",
        "bestaudio[ext=m4a]/bestaudio",
        "--extractor-args",
        "youtube:player_client=android_vr,web,tv",
        "--max-filesize",
        _YTDLP_MAX_FILESIZE,
        "--output",
        dest,
        "--",
        url,
    ]


def clone_cache_dir() -> Path:
    override = os.environ.get("TALKTOMEJOHNNY_CLONE_CACHE") or os.environ.get(
        "TALKTOMECLAUDE_CLONE_CACHE"
    )
    if override:
        return Path(override)
    return config.preferred_cache_dir("hf")


def _ensure_hf_home() -> None:
    # Redirect the Hugging Face cache under our own cache dir. Set before the
    # first chatterbox/huggingface_hub import so the weights land there.
    os.environ.setdefault("HF_HOME", str(clone_cache_dir()))


def _repair_perth_watermarker(perth_module) -> None:
    """Bridge Perth 1.0.1 after setuptools removed ``pkg_resources``."""

    if callable(getattr(perth_module, "PerthImplicitWatermarker", None)):
        return
    package_name = "perth.perth_net"
    package_root = Path(perth_module.__file__).resolve().parent / "perth_net"
    compat = types.ModuleType(package_name)
    compat.__file__ = str(package_root / "__init__.py")
    compat.__package__ = package_name
    compat.__path__ = [str(package_root)]
    compat.__spec__ = importlib.machinery.ModuleSpec(
        package_name,
        loader=None,
        is_package=True,
    )
    compat.PREPACKAGED_MODELS_DIR = str(package_root / "pretrained")
    previous = sys.modules.get(package_name)
    sys.modules[package_name] = compat
    try:
        implementation = importlib.import_module(
            "perth.perth_net.perth_net_implicit.perth_watermarker"
        )
    except BaseException:
        if previous is None:
            sys.modules.pop(package_name, None)
        else:
            sys.modules[package_name] = previous
        raise
    perth_module.PerthImplicitWatermarker = implementation.PerthImplicitWatermarker


def clone_available() -> bool:
    """True if the optional cloning stack (torch + chatterbox) is importable."""
    _ensure_hf_home()
    try:
        import perth
        import torch  # noqa: F401

        _repair_perth_watermarker(perth)
        from chatterbox.tts import ChatterboxTTS  # noqa: F401
    except Exception:
        return False
    return True


def _select_device() -> str:
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _load_model():
    global _model
    if _model is not None:
        return _model
    _ensure_hf_home()
    if not clone_available():
        raise CloneError(
            "the voice-cloning engine is not installed; run `talktomejohnny doctor` "
            "for the exact install recipe for this machine"
        )
    from chatterbox.tts import ChatterboxTTS

    device = _select_device()
    try:
        _model = ChatterboxTTS.from_pretrained(device=device)
    except Exception as exc:
        raise CloneError(f"failed to load the cloning model on {device}: {exc}") from exc
    return _model


def _to_mono_float(wav) -> np.ndarray:
    try:
        array = wav.detach().cpu().numpy()
    except AttributeError:
        array = np.asarray(wav)
    array = np.asarray(array, dtype=np.float32)
    array = np.squeeze(array)
    if array.ndim > 1:  # (channels, samples) -> first channel
        array = array[0]
    return np.ascontiguousarray(array)


def _write_wav(out_path: Path, wav, sample_rate: int) -> None:
    if sample_rate <= 0:
        raise CloneError(f"the cloning engine reported an invalid sample rate ({sample_rate})")
    array = _to_mono_float(wav)
    if array.size == 0 or not np.all(np.isfinite(array)):
        raise CloneError("the cloning engine produced empty or non-finite audio")
    pcm = (np.clip(array, -1.0, 1.0) * 32767.0).astype("<i2")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(pcm.tobytes())
    except OSError as exc:
        out_path.unlink(missing_ok=True)
        raise CloneError(f"failed to write cloned audio to {out_path}: {exc}") from exc
    if not out_path.is_file() or out_path.stat().st_size <= 44:  # 44 = WAV header, no frames
        raise CloneError(f"cloning produced no audio at {out_path}")


def synthesize_clone(
    text: str,
    out_path: Path,
    reference_path: str | Path,
    *,
    exaggeration: float = 0.5,
    cfg_weight: float = 0.5,
) -> Path:
    """Render *text* in the cloned voice from *reference_path* to a WAV.

    The model is loaded once and cached. *reference_path* is a short, clean,
    single-speaker clip (~10–20 s). Returns *out_path*.
    """
    if not text.strip():
        raise CloneError("nothing to speak")
    reference = Path(reference_path)
    if not reference.is_file():
        raise CloneError(f"reference clip not found: {reference}")
    model = _load_model()
    try:
        wav = model.generate(
            text,
            audio_prompt_path=str(reference),
            exaggeration=float(exaggeration),
            cfg_weight=float(cfg_weight),
        )
    except Exception as exc:
        raise CloneError(f"voice cloning failed: {exc}") from exc
    sample_rate = int(getattr(model, "sr", _SAMPLE_RATE_FALLBACK))
    _write_wav(Path(out_path), wav, sample_rate)
    return Path(out_path)
