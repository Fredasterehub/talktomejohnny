"""Hardware-aware advisor.

Inspects the machine (OS, CPU, RAM, GPU) and recommends the speech-to-text
tier and whether local voice cloning is feasible — and if so, prints the exact,
validated install recipe. The recipe is the single source of truth shared by
the ``doctor`` and ``clone-install`` CLI commands.

Stdlib only: it must run before any of the optional CUDA / cloning stacks are
installed (that is the whole point of an advisor), so it shells out to
``nvidia-smi`` rather than importing torch.
"""

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as package_version

from talktomeclaude import stt

# Chatterbox loads ~2 GB of weights plus inference activations; below this the
# GPU clone will thrash or OOM, so it is flagged as tight rather than feasible.
_CLONE_MIN_VRAM_MB = 6000
# torch 2.6 (Chatterbox's own pin) has no Blackwell (sm_120) kernels and would
# fall back to CPU silently; the cu128 wheels cover sm_70…sm_120, so the
# validated recipe below installs those and defeats the pin with --no-deps.
_CLONE_MIN_COMPUTE = (7, 0)
_CU128_INDEX = "https://download.pytorch.org/whl/cu128"
_CLONE_SECURITY_FLOORS = {
    "diffusers": (0, 38, 0),
    "setuptools": (83, 0, 0),
    "transformers": (5, 5, 0),
}


def _installer_prefix() -> str:
    """The pip invocation for this environment. uv-managed venvs have no
    standalone ``pip`` module, so prefer uv's shim when uv is on PATH."""
    if shutil.which("uv"):
        return "uv pip install"
    return "python -m pip install"


def clone_install_recipe() -> tuple[str, ...]:
    """The exact commands proven to run ChatterboxTTS.from_pretrained()+
    generate() on CUDA 12.8 GPUs, into the active environment.

    Ordered: CUDA torch first, the engine ``--no-deps`` to skip its
    ``torch==2.6.0`` / ``torchaudio==2.6.0`` pins (no Blackwell kernels), then
    the engine's real runtime deps. The Transformers and Diffusers pins override
    Chatterbox 0.1.7's vulnerable upstream pins with the minimum fixed releases.
    numpy resolves transitively to a working 2.x — cloning is validated on it
    here despite the engine's advisory <2 pin.
    """
    pip = _installer_prefix()
    return (
        f"{pip} --index-url {_CU128_INDEX} torch==2.11.0 torchaudio==2.11.0",
        f"{pip} --no-deps chatterbox-tts==0.1.7",
        (
            f"{pip} "
            '"setuptools>=83" "librosa==0.11.0" s3tokenizer '
            '"transformers==5.5.0" "diffusers==0.38.0" '
            '"resemble-perth>=1.0.0" "conformer==0.3.2" "safetensors==0.5.3" '
            'spacy-pkuseg "pykakasi==2.3.0" pyloudnorm omegaconf'
        ),
    )


def clone_security_warnings() -> tuple[str, ...]:
    """Report installed optional cloning packages below fixed releases."""

    warnings: list[str] = []
    for package, floor in _CLONE_SECURITY_FLOORS.items():
        try:
            installed = package_version(package)
        except PackageNotFoundError:
            continue
        matched = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\.post\d+)?", installed)
        parsed = tuple(int(part) for part in matched.groups()) if matched else None
        if parsed is not None and parsed >= floor:
            continue
        minimum = ".".join(str(part) for part in floor)
        warnings.append(
            f"{package} {installed} is below the safe cloning floor {minimum}; "
            "rerun the current cloning recipe before loading model content"
        )
    return tuple(warnings)


@dataclass(frozen=True)
class GPU:
    name: str
    vram_mb: int
    compute_cap: str  # e.g. "12.0"; "" when nvidia-smi cannot report it


@dataclass(frozen=True)
class Machine:
    os: str
    arch: str
    python: str
    cpu_count: int
    ram_gb: float | None
    gpus: tuple[GPU, ...]

    @property
    def primary_gpu(self) -> GPU | None:
        return self.gpus[0] if self.gpus else None


@dataclass(frozen=True)
class Recommendation:
    stt_tier: str
    clone_feasible: bool
    clone_reason: str
    clone_recipe: tuple[str, ...]
    notes: tuple[str, ...]


def compute_cap_tuple(cap: str) -> tuple[int, int] | None:
    """Parse an ``nvidia-smi`` compute-capability string like ``"12.0"``."""
    try:
        major, minor = cap.strip().split(".")
        return int(major), int(minor)
    except (ValueError, AttributeError):
        return None


def _run_smi(executable: str, query: str) -> list[list[str]] | None:
    try:
        result = subprocess.run(
            [executable, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [
        [field.strip() for field in line.split(",")]
        for line in result.stdout.strip().splitlines()
        if line.strip()
    ]


def _nvidia_smi_gpus() -> tuple[GPU, ...]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return ()
    # Older drivers reject the compute_cap field and fail the whole query, which
    # would lose all GPU detection — fall back to name + memory in that case.
    rows = _run_smi(executable, "name,memory.total,compute_cap")
    has_cap = rows is not None
    if rows is None:
        rows = _run_smi(executable, "name,memory.total")
    if rows is None:
        return ()
    gpus = []
    for fields in rows:
        if not fields or not fields[0]:
            continue
        vram_mb = 0
        if len(fields) > 1:
            try:
                vram_mb = int(float(fields[1]))
            except ValueError:
                vram_mb = 0
        compute_cap = fields[2] if has_cap and len(fields) > 2 else ""
        gpus.append(GPU(name=fields[0], vram_mb=vram_mb, compute_cap=compute_cap))
    return tuple(gpus)


def _ram_gb() -> float | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * page_size / 1024**3, 1)
    except (ValueError, OSError, AttributeError):
        return None


def detect_machine() -> Machine:
    return Machine(
        os=f"{platform.system()} {platform.release()}".strip(),
        arch=platform.machine(),
        python=platform.python_version(),
        cpu_count=os.cpu_count() or 1,
        ram_gb=_ram_gb(),
        gpus=_nvidia_smi_gpus(),
    )


def _best_gpu(gpus: tuple[GPU, ...]) -> GPU | None:
    """The most capable GPU: highest compute capability, then most VRAM."""
    if not gpus:
        return None
    return max(gpus, key=lambda gpu: (compute_cap_tuple(gpu.compute_cap) or (0, 0), gpu.vram_mb))


def recommend(machine: Machine | None = None) -> Recommendation:
    machine = machine or detect_machine()
    gpu = _best_gpu(machine.gpus)
    notes: list[str] = []

    if gpu is not None:
        stt_tier = f"GPU — {stt.GPU_TIER.describe()} (install the [cuda] extra)"
    else:
        stt_tier = f"CPU — {stt.CPU_TIER.describe()}"

    if gpu is None:
        return Recommendation(
            stt_tier=stt_tier,
            clone_feasible=False,
            clone_reason="no NVIDIA GPU detected; voice cloning needs one",
            clone_recipe=(),
            notes=(
                "You can still register and use your own Piper voices "
                "(`voices add`) — cloning is the only GPU-only feature.",
            ),
        )

    cap = compute_cap_tuple(gpu.compute_cap)
    if cap is None:
        clone_reason = (
            f"{gpu.name}: could not read compute capability; cloning support unknown"
        )
        return Recommendation(stt_tier, False, clone_reason, (), tuple(notes))
    if cap < _CLONE_MIN_COMPUTE:
        clone_reason = (
            f"{gpu.name} (sm_{cap[0]}{cap[1]}) is older than the cloning stack "
            f"supports (needs sm_{_CLONE_MIN_COMPUTE[0]}{_CLONE_MIN_COMPUTE[1]}+)"
        )
        return Recommendation(stt_tier, False, clone_reason, (), tuple(notes))

    if gpu.vram_mb == 0:
        notes.append(
            "VRAM could not be read; cloning needs roughly "
            f"{_CLONE_MIN_VRAM_MB // 1000} GB free, so confirm before long runs."
        )
    elif gpu.vram_mb < _CLONE_MIN_VRAM_MB:
        notes.append(
            f"{gpu.vram_mb} MB VRAM is below the ~{_CLONE_MIN_VRAM_MB} MB comfort "
            "level; cloning may be slow or run out of memory on long inputs."
        )
    if cap >= (12, 0):
        notes.append(
            f"{gpu.name} is Blackwell (sm_{cap[0]}{cap[1]}); the recipe uses the "
            "cu128 wheels because the engine's default torch 2.6 has no kernels for it."
        )
    vram_label = f"{gpu.vram_mb} MB" if gpu.vram_mb else "VRAM unknown"
    clone_reason = f"{gpu.name} (sm_{cap[0]}{cap[1]}, {vram_label}) can run local cloning"
    return Recommendation(
        stt_tier=stt_tier,
        clone_feasible=True,
        clone_reason=clone_reason,
        clone_recipe=clone_install_recipe(),
        notes=tuple(notes),
    )


def format_report(machine: Machine | None = None) -> str:
    """Human-readable advisor report for the ``doctor`` command."""
    machine = machine or detect_machine()
    rec = recommend(machine)
    lines = ["Hardware", "--------"]
    lines.append(f"  os          {machine.os} ({machine.arch})")
    lines.append(f"  python      {machine.python}")
    ram = f"{machine.ram_gb} GB" if machine.ram_gb is not None else "unknown"
    lines.append(f"  cpu / ram   {machine.cpu_count} cores / {ram}")
    if machine.gpus:
        for index, gpu in enumerate(machine.gpus):
            cap = gpu.compute_cap or "?"
            lines.append(
                f"  gpu[{index}]      {gpu.name} — {gpu.vram_mb} MB, compute {cap}"
            )
    else:
        lines.append("  gpu         none detected")
    lines += ["", "Recommendation", "--------------"]
    lines.append(f"  speech-to-text   {rec.stt_tier}")
    verdict = "yes" if rec.clone_feasible else "no"
    lines.append(f"  voice cloning    {verdict} — {rec.clone_reason}")
    for note in rec.notes:
        lines.append(f"    note: {note}")
    security_warnings = clone_security_warnings()
    if security_warnings:
        lines += ["", "Optional cloning dependency security:"]
        lines.extend(f"  ! {warning}" for warning in security_warnings)
    if rec.clone_recipe:
        lines += ["", "Install the cloning engine (into this environment):"]
        for command in rec.clone_recipe:
            lines.append(f"  $ {command}")
    return "\n".join(lines)
