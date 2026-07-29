"""Local Whisper-class speech-to-text built on faster-whisper.

Hardware-tier auto-detection (directive D-1): a visible CUDA GPU carries the
largest fluid Whisper model in float16; CPU-only machines fall back to the
most accurate model that stays conversationally fluid, quantized to int8.
One code path, switched only through ``device``/``compute_type``.

Fidelity (directive D-2): accuracy beats speed inside each tier — full beam
search, hotword biasing so assistant and product names survive, and
never a silent quality cut: any fallback from the detected tier is reported
through the status callback so the operator can see the active tier.
"""

import ctypes
import glob
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from talktomeclaude import config


@dataclass(frozen=True)
class STTTier:
    model: str
    device: str
    compute_type: str

    def describe(self) -> str:
        return f"model={self.model} device={self.device} compute_type={self.compute_type}"


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


GPU_TIER = STTTier(model="large-v3", device="cuda", compute_type="float16")
CPU_TIER = STTTier(model="small.en", device="cpu", compute_type="int8")

HOTWORDS = "Johnny, TalkToMeJohnny, Claude Code, Codex"

_CUDA_DLL_DIRECTORY_HANDLES: list[object] = []
_CUDA_DLL_DIRECTORIES: set[str] = set()
_CUDA_DLL_HANDLES: list[object] = []
_CUDA_DLL_PATHS: set[str] = set()


class STTError(RuntimeError):
    """Raised when transcription cannot proceed."""


def models_dir() -> Path:
    override = os.environ.get("TALKTOMEJOHNNY_STT_MODELS_DIR") or os.environ.get(
        "TALKTOMECLAUDE_STT_MODELS_DIR"
    )
    if override:
        return Path(override)
    return config.preferred_cache_dir("stt-models")


def _preload_cuda_libraries() -> None:
    """Make the pip-shipped CUDA runtime visible to CTranslate2.

    The nvidia-cublas/nvidia-cudnn wheels install libraries outside the
    loader's default search path. On Windows, a CUDA-enabled Torch install
    already loads a complete compatible CUDA/cuDNN set; use it for both Torch
    and CTranslate2 instead of loading a second, potentially incompatible
    cuDNN from the NVIDIA wheels. Without CUDA Torch, register the NVIDIA bin
    directories. POSIX loads the wheel-provided shared objects RTLD_GLOBAL.
    """
    if os.name == "nt":
        try:
            import torch
        except (ImportError, OSError):
            pass
        else:
            if getattr(getattr(torch, "version", None), "cuda", None):
                return

    try:
        import nvidia
    except ImportError:
        return

    if os.name == "nt":
        registered: list[str] = []
        for package_path in nvidia.__path__:
            for bin_dir in sorted(glob.glob(os.path.join(package_path, "*", "bin"))):
                directory = os.path.abspath(bin_dir)
                if directory in _CUDA_DLL_DIRECTORIES:
                    continue
                try:
                    handle = os.add_dll_directory(directory)
                except OSError:
                    continue
                _CUDA_DLL_DIRECTORIES.add(directory)
                _CUDA_DLL_DIRECTORY_HANDLES.append(handle)
                registered.append(directory)
        if registered:
            current_path = os.environ.get("PATH", "")
            os.environ["PATH"] = os.pathsep.join([*registered, current_path])
            pending = [
                path
                for directory in registered
                for path in glob.glob(os.path.join(directory, "*.dll"))
                if path not in _CUDA_DLL_PATHS
            ]
            priorities = ("cublaslt", "cublas64", "cudnn64")
            pending.sort(
                key=lambda path: next(
                    (
                        index
                        for index, prefix in enumerate(priorities)
                        if os.path.basename(path).lower().startswith(prefix)
                    ),
                    len(priorities),
                )
            )
            while pending:
                remaining: list[str] = []
                loaded = False
                for dll_path in pending:
                    try:
                        handle = ctypes.WinDLL(dll_path)
                    except OSError:
                        remaining.append(dll_path)
                        continue
                    _CUDA_DLL_HANDLES.append(handle)
                    _CUDA_DLL_PATHS.add(dll_path)
                    loaded = True
                if not loaded:
                    break
                pending = remaining
        return

    for package_path in nvidia.__path__:
        for lib_dir in sorted(glob.glob(os.path.join(package_path, "*", "lib"))):
            for shared_object in sorted(glob.glob(os.path.join(lib_dir, "*.so*"))):
                try:
                    ctypes.CDLL(shared_object, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    continue


def cuda_available() -> bool:
    try:
        _preload_cuda_libraries()
        import ctranslate2
    except (ImportError, OSError):
        return False
    try:
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def detect_tier(device: str = "auto", model: str | None = None) -> STTTier:
    """Resolve the active tier (D-1): auto-detected, with a manual override."""
    if device == "cuda":
        _preload_cuda_libraries()
    if device == "auto":
        tier = GPU_TIER if cuda_available() else CPU_TIER
    elif device == "cuda":
        tier = GPU_TIER
    elif device == "cpu":
        tier = CPU_TIER
    else:
        raise STTError(f"unknown device {device!r} (expected auto, cuda, or cpu)")
    if model:
        tier = STTTier(model=model, device=tier.device, compute_type=tier.compute_type)
    return tier


def _tier_segments(tier: STTTier, audio_path: Path):
    from faster_whisper import WhisperModel

    whisper = WhisperModel(
        tier.model,
        device=tier.device,
        compute_type=tier.compute_type,
        download_root=str(models_dir()),
    )
    segments, _info = whisper.transcribe(
        str(audio_path),
        beam_size=5,
        hotwords=HOTWORDS,
    )
    return segments


def _run_tier(tier: STTTier, audio_path: Path) -> str:
    segments = _tier_segments(tier, audio_path)
    return " ".join(part for part in (segment.text.strip() for segment in segments) if part)


def _run_tier_timestamped(tier: STTTier, audio_path: Path) -> list[TranscriptSegment]:
    segments = _tier_segments(tier, audio_path)
    return [
        TranscriptSegment(float(segment.start), float(segment.end), text)
        for segment in segments
        if (text := segment.text.strip())
    ]


_Result = TypeVar("_Result")


def _transcribe_with_fallback(
    audio_path: Path,
    device: str,
    model: str | None,
    on_status: Callable[[str], None] | None,
    runner: Callable[[STTTier, Path], _Result],
) -> tuple[_Result, STTTier]:
    if not audio_path.is_file():
        raise STTError(f"audio file not found: {audio_path}")
    status = on_status or (lambda message: None)
    tier = detect_tier(device, model)
    status(f"stt tier: {tier.describe()}")
    try:
        return runner(tier, audio_path), tier
    except Exception as exc:
        if tier.device != "cuda" or device == "cuda":
            raise STTError(f"transcription failed on {tier.describe()}: {exc}") from exc
        fallback = CPU_TIER if model is None else STTTier(
            model=model, device=CPU_TIER.device, compute_type=CPU_TIER.compute_type
        )
        status(
            f"stt tier degraded: {tier.describe()} failed ({exc}); "
            f"falling back to {fallback.describe()}"
        )
        try:
            return runner(fallback, audio_path), fallback
        except Exception as fallback_exc:
            raise STTError(
                f"transcription failed on {fallback.describe()}: {fallback_exc}"
            ) from fallback_exc


def transcribe_file(
    audio_path: Path,
    device: str = "auto",
    model: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> tuple[str, STTTier]:
    """Transcribe *audio_path* locally; returns (transcript, tier actually used).

    If the GPU tier fails to initialize or decode, falls back to the CPU tier
    and reports it through *on_status* — degradation is never silent (D-2).
    """
    return _transcribe_with_fallback(
        audio_path, device, model, on_status, _run_tier
    )


def transcribe_file_with_timestamps(
    audio_path: Path,
    device: str = "auto",
    model: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> tuple[list[TranscriptSegment], STTTier]:
    """Transcribe locally without flattening faster-whisper segment bounds."""
    return _transcribe_with_fallback(
        audio_path, device, model, on_status, _run_tier_timestamped
    )
