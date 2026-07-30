"""Headless settings and voice-flow services for the Windows companion.

The presentation layer owns prompts and controls; this module owns the small
transaction around discovering, previewing, importing, and selecting a voice.
Every dependency with hardware or filesystem side effects is injectable so the
flow remains deterministic in tests.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol

from talktomeclaude import config, registry, tts, wizard
from talktomeclaude.catalog import Voice
from talktomeclaude.storage import ConfigStore

AUTO_SUBMIT_WARNING = (
    "Auto-submit sends Enter to the eligible foreground terminal captured at "
    "finish-toggle; the operator is responsible for the intended tab, pane, shell, "
    "and cursor position."
)
DEFAULT_PREVIEW_TEXT = "Hello. This is your TalkToMeJohnny voice."


class SettingsError(RuntimeError):
    """Base error for a settings operation that could not be completed."""


class DuplicateVoiceError(SettingsError):
    """Raised before mutation when an imported name is already in use."""


class VoiceFlowCancelled(SettingsError):
    """Raised after safely rolling back a cancelled import flow."""


class VoicePreviewError(SettingsError):
    """Raised when the injected preview worker cannot play a sample."""


class VoiceActivationError(SettingsError):
    """Raised when a persisted selection cannot reach the live speech runtime."""


class VoiceRollbackError(SettingsError):
    """Raised when a failed flow cannot completely remove its new voice."""


class VoiceAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    FAULT = "fault"


class VoiceFaultCode(str, Enum):
    MISSING_ASSET = "missing-asset"
    ENGINE_UNAVAILABLE = "engine-unavailable"
    INVALID_CONFIGURATION = "invalid-configuration"
    RESOLUTION_FAILED = "resolution-failed"


class VoiceSource(str, Enum):
    BUNDLED = "bundled"
    REGISTERED = "registered"
    CLONED = "cloned"


class VoiceImportKind(str, Enum):
    PIPER = "piper"
    CLONE = "clone"


class VoiceFlowStep(str, Enum):
    VALIDATE = "validate"
    REGISTER = "register"
    PREVIEW = "preview"
    SELECT = "select"


@dataclass(frozen=True)
class VoiceFault:
    code: VoiceFaultCode
    message: str


@dataclass(frozen=True)
class VoiceOption:
    name: str
    engine: str
    source: VoiceSource
    selected: bool
    availability: VoiceAvailability
    fault: VoiceFault | None = None


@dataclass(frozen=True)
class VoiceImportRequest:
    kind: VoiceImportKind
    name: str
    source_path: str | Path
    config_path: str | Path | None = None
    language: str = "en"
    preview_text: str = DEFAULT_PREVIEW_TEXT
    preview: bool = True
    select: bool = True


@dataclass(frozen=True)
class VoiceImportResult:
    voice: VoiceOption
    previewed: bool
    selected: bool


class SettingsStore(Protocol):
    def load(self) -> dict[str, Any]: ...

    def update(
        self,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    ) -> dict[str, Any]: ...


PreviewWorker = Callable[[str, str], object]
VoiceActivator = Callable[[str], object]
CancelCheck = Callable[[], bool]
StepCallback = Callable[[VoiceFlowStep], object]


def _default_preview_worker(voice_name: str, text: str) -> object:
    return tts.synthesize_and_play(text, voice_name=voice_name)


class VoiceSettingsService:
    """Voice settings transaction boundary shared by any companion UI."""

    def __init__(
        self,
        *,
        settings_store: SettingsStore | None = None,
        bundled_voices: Sequence[Voice] | None = None,
        list_registered: Callable[[], list[registry.RegisteredVoice]] = registry.list_voices,
        add_piper: Callable[..., registry.RegisteredVoice] = registry.add_piper,
        create_clone: Callable[..., tuple[registry.RegisteredVoice, Path | None]] = (
            wizard.create_clone_voice
        ),
        remove_registered: Callable[[str], object] = registry.remove,
        resolve_voice: Callable[[str], Voice] = tts.get_voice,
        is_available: Callable[[Voice], bool] = tts.is_available,
        preview_worker: PreviewWorker = _default_preview_worker,
        activate_voice: VoiceActivator | None = None,
    ) -> None:
        self._settings = settings_store or ConfigStore(config.config_path())
        self._bundled = tuple(tts.BUNDLED_VOICES if bundled_voices is None else bundled_voices)
        self._list_registered = list_registered
        self._add_piper = add_piper
        self._create_clone = create_clone
        self._remove_registered = remove_registered
        self._resolve_voice = resolve_voice
        self._is_available = is_available
        self._preview_worker = preview_worker
        self._activate_voice = activate_voice

    def selected_voice(self) -> str | None:
        value = self._settings.load().get("default-voice")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def list_voices(self) -> tuple[VoiceOption, ...]:
        """Return every bundled and registered voice with actionable status."""
        selected = self.selected_voice()
        selected_key = selected.casefold() if selected else None
        bundled_names = {voice.name.casefold() for voice in self._bundled}
        options = [
            self._option_for(voice, VoiceSource.BUNDLED, selected_key)
            for voice in self._bundled
        ]
        for registered_voice in self._list_registered():
            if registered_voice.name.casefold() in bundled_names:
                continue
            source = (
                VoiceSource.CLONED
                if registered_voice.engine in {"clone", "f5"}
                else VoiceSource.REGISTERED
            )
            try:
                voice = self._resolve_voice(registered_voice.name)
            except Exception:
                options.append(
                    VoiceOption(
                        name=registered_voice.name,
                        engine=registered_voice.engine,
                        source=source,
                        selected=registered_voice.name.casefold() == selected_key,
                        availability=VoiceAvailability.FAULT,
                        fault=VoiceFault(
                            VoiceFaultCode.RESOLUTION_FAILED,
                            "The voice registration could not be resolved.",
                        ),
                    )
                )
                continue
            options.append(self._option_for(voice, source, selected_key))
        return tuple(options)

    def select_voice(self, name: str) -> VoiceOption:
        """Persist one known voice and apply it to the live runtime when present."""
        canonical = self._canonical_name(name)
        option = self._option_by_name(canonical)
        previous = self._settings.load().get("default-voice")

        def select(settings: dict[str, Any]) -> None:
            settings["default-voice"] = canonical

        self._settings.update(select)
        if self._activate_voice is not None:
            try:
                self._activate_voice(canonical)
            except Exception as exc:
                def restore(settings: dict[str, Any]) -> None:
                    if isinstance(previous, str) and previous.strip():
                        settings["default-voice"] = previous
                    else:
                        settings.pop("default-voice", None)

                try:
                    self._settings.update(restore)
                except Exception as rollback_exc:
                    raise VoiceRollbackError(
                        f"voice {canonical!r} could not be activated or rolled back"
                    ) from rollback_exc
                raise VoiceActivationError(
                    f"voice {canonical!r} could not be activated"
                ) from exc
        return replace(option, selected=True)

    def clear_selection(self) -> None:
        """Return to automatic voice selection transactionally."""

        def clear(settings: dict[str, Any]) -> None:
            settings.pop("default-voice", None)

        self._settings.update(clear)

    def preview_voice(self, name: str, text: str = DEFAULT_PREVIEW_TEXT) -> None:
        """Run an immediate preview without changing persistent selection."""
        canonical = self._canonical_name(name)
        if not isinstance(text, str) or not text.strip():
            raise SettingsError("preview text must not be empty")
        try:
            self._preview_worker(canonical, text.strip())
        except Exception as exc:
            raise VoicePreviewError(f"preview failed for voice {canonical!r}") from exc

    def import_voice(
        self,
        request: VoiceImportRequest,
        *,
        cancelled: CancelCheck | None = None,
        on_step: StepCallback | None = None,
    ) -> VoiceImportResult:
        """Validate, register, optionally preview, then atomically select a voice.

        Selection commits last. If validation, registration, preview, selection,
        or cancellation fails, only the registry entry created by this invocation
        is removed; pre-existing voices and their assets are never rollback targets.
        """
        cancel_check = cancelled or (lambda: False)
        step_callback = on_step or (lambda _step: None)
        before_names = {voice.name.casefold() for voice in self._list_registered()}
        creation_receipt: registry.RegisteredVoice | None = None
        previewed = False

        try:
            self._advance(VoiceFlowStep.VALIDATE, cancel_check, step_callback)
            name = self._validate_request(request, before_names)
            self._advance(VoiceFlowStep.REGISTER, cancel_check, step_callback)
            if request.kind is VoiceImportKind.PIPER:
                creation_receipt = self._add_piper(
                    name,
                    request.source_path,
                    request.config_path,
                    language=request.language,
                )
            else:
                creation_receipt, _sample = self._create_clone(
                    name,
                    request.source_path,
                    sample_text=None,
                    language=request.language,
                )
            created_name = creation_receipt.name
            self._check_cancelled(cancel_check)

            if request.preview:
                self._advance(VoiceFlowStep.PREVIEW, cancel_check, step_callback)
                self.preview_voice(created_name, request.preview_text)
                previewed = True
                self._check_cancelled(cancel_check)

            option = self._option_by_name(created_name)
            if request.select:
                self._advance(VoiceFlowStep.SELECT, cancel_check, step_callback)
                option = self.select_voice(created_name)
            return VoiceImportResult(
                voice=option,
                previewed=previewed,
                selected=request.select,
            )
        except Exception:
            # A successful creator return is the ownership receipt for this flow.
            # If creation raises, its commit state is unknown; discovering a voice
            # by requested name could select a concurrent creator's registration.
            if creation_receipt is not None:
                try:
                    self._remove_registered(creation_receipt.name)
                except Exception as rollback_exc:
                    raise VoiceRollbackError(
                        f"voice flow failed and {creation_receipt.name!r} could not be "
                        "rolled back"
                    ) from rollback_exc
            raise

    def _validate_request(
        self, request: VoiceImportRequest, registered_names: set[str]
    ) -> str:
        if not isinstance(request.kind, VoiceImportKind):
            raise SettingsError("unsupported voice import kind")
        if not isinstance(request.name, str) or not request.name.strip():
            raise SettingsError("voice name must not be empty")
        name = request.name.strip()
        known_names = registered_names | {voice.name.casefold() for voice in self._bundled}
        if name.casefold() in known_names:
            raise DuplicateVoiceError(
                f"a voice named {name!r} already exists (names are case-insensitive)"
            )
        if request.preview and (
            not isinstance(request.preview_text, str) or not request.preview_text.strip()
        ):
            raise SettingsError("preview text must not be empty")
        return name

    def _canonical_name(self, name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise SettingsError("voice name must not be empty")
        wanted = name.strip().casefold()
        for option in self.list_voices():
            if option.name.casefold() == wanted:
                return option.name
        raise SettingsError(f"unknown voice {name!r}")

    def _option_by_name(self, name: str) -> VoiceOption:
        wanted = name.casefold()
        for option in self.list_voices():
            if option.name.casefold() == wanted:
                return option
        raise SettingsError(f"unknown voice {name!r}")

    def _option_for(
        self, voice: Voice, source: VoiceSource, selected_key: str | None
    ) -> VoiceOption:
        try:
            available = self._is_available(voice)
        except Exception:
            return VoiceOption(
                name=voice.name,
                engine=voice.engine,
                source=source,
                selected=voice.name.casefold() == selected_key,
                availability=VoiceAvailability.FAULT,
                fault=VoiceFault(
                    VoiceFaultCode.RESOLUTION_FAILED,
                    "Voice availability could not be checked.",
                ),
            )
        fault = None if available else self._unavailable_fault(voice, source)
        state = (
            VoiceAvailability.AVAILABLE
            if available
            else VoiceAvailability.FAULT
            if fault is not None
            else VoiceAvailability.UNAVAILABLE
        )
        return VoiceOption(
            name=voice.name,
            engine=voice.engine,
            source=source,
            selected=voice.name.casefold() == selected_key,
            availability=state,
            fault=fault,
        )

    @staticmethod
    def _unavailable_fault(voice: Voice, source: VoiceSource) -> VoiceFault | None:
        if source is VoiceSource.BUNDLED:
            return None  # known voice, installable/downloadable on first use
        params = voice.params
        if voice.engine in {"clone", "f5"}:
            reference = params.get("reference")
            if not reference or not Path(str(reference)).is_file():
                return VoiceFault(
                    VoiceFaultCode.MISSING_ASSET,
                    "The cloned voice reference audio is missing.",
                )
            if voice.engine == "f5" and not str(params.get("ref_text", "")).strip():
                return VoiceFault(
                    VoiceFaultCode.INVALID_CONFIGURATION,
                    "The cloned voice reference text is missing.",
                )
            return VoiceFault(
                VoiceFaultCode.ENGINE_UNAVAILABLE,
                "The cloned voice engine is not available.",
            )
        model = params.get("model")
        voice_config = params.get("config")
        if not model or not voice_config:
            return VoiceFault(
                VoiceFaultCode.INVALID_CONFIGURATION,
                "The registered voice configuration is incomplete.",
            )
        return VoiceFault(
            VoiceFaultCode.MISSING_ASSET,
            "The registered voice model or configuration file is missing.",
        )

    @staticmethod
    def _check_cancelled(cancelled: CancelCheck) -> None:
        if cancelled():
            raise VoiceFlowCancelled("voice flow cancelled")

    @classmethod
    def _advance(
        cls,
        step: VoiceFlowStep,
        cancelled: CancelCheck,
        on_step: StepCallback,
    ) -> None:
        cls._check_cancelled(cancelled)
        on_step(step)
        cls._check_cancelled(cancelled)
