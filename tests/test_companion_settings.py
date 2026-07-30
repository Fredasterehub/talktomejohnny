from __future__ import annotations

import contextlib
import re
import tempfile
import threading
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from talktomeclaude import registry
from talktomeclaude.catalog import Voice
from talktomeclaude.companion.settings import (
    AUTO_SUBMIT_WARNING,
    DuplicateVoiceError,
    SettingsStore,
    VoiceAvailability,
    VoiceActivationError,
    VoiceFaultCode,
    VoiceFlowCancelled,
    VoiceFlowStep,
    VoiceImportKind,
    VoiceImportRequest,
    VoicePreviewError,
    VoiceSettingsService,
    VoiceSource,
)
from talktomeclaude.storage import ConfigStore


@contextlib.contextmanager
def raises(expected: type[BaseException], *, match: str | None = None):
    try:
        yield
    except expected as exc:
        if match is not None and re.search(match, str(exc)) is None:
            raise AssertionError(f"{exc!r} does not match {match!r}") from exc
    else:
        raise AssertionError(f"{expected.__name__} was not raised")


def _registered(
    name: str, engine: str = "piper", params: dict | None = None
) -> registry.RegisteredVoice:
    return registry.RegisteredVoice(
        name=name,
        engine=engine,
        params=params or {},
        language="en",
        license="test",
        provenance="test",
    )


def _voice(name: str, engine: str = "piper", params: dict | None = None) -> Voice:
    return Voice(
        name=name,
        language="en",
        quality="test",
        license="test",
        provenance="test",
        engine=engine,
        params=params or {},
    )


class FakeRegistry:
    def __init__(self, voices: list[registry.RegisteredVoice] | None = None) -> None:
        self.voices = {voice.name: voice for voice in voices or []}
        self.removed: list[str] = []
        self.assets = {voice.name for voice in voices or []}

    def list(self) -> list[registry.RegisteredVoice]:
        return list(self.voices.values())

    def resolve(self, name: str) -> Voice:
        voice = self.voices[name]
        return _voice(voice.name, voice.engine, dict(voice.params))

    def add_piper(
        self,
        name: str,
        _model: str | Path,
        _config: str | Path | None,
        *,
        language: str,
    ) -> registry.RegisteredVoice:
        voice = _registered(name, params={"model": "model", "config": "config"})
        self.voices[name] = voice
        self.assets.add(name)
        return voice

    def create_clone(
        self,
        name: str,
        _reference: str | Path,
        *,
        sample_text: str | None,
        language: str,
    ) -> tuple[registry.RegisteredVoice, None]:
        assert sample_text is None
        voice = _registered(name, "clone", {"reference": "reference.wav"})
        self.voices[name] = voice
        self.assets.add(name)
        return voice, None

    def remove(self, name: str) -> None:
        self.removed.append(name)
        del self.voices[name]
        self.assets.remove(name)


def _service(
    store: SettingsStore,
    fake_registry: FakeRegistry,
    *,
    bundled: tuple[Voice, ...] = (),
    preview=lambda _name, _text: None,
    available=lambda _voice: True,
    activate=lambda _name: None,
) -> VoiceSettingsService:
    def resolve(name: str) -> Voice:
        for voice in bundled:
            if voice.name == name:
                return voice
        return fake_registry.resolve(name)

    return VoiceSettingsService(
        settings_store=store,
        bundled_voices=bundled,
        list_registered=fake_registry.list,
        add_piper=fake_registry.add_piper,
        create_clone=fake_registry.create_clone,
        remove_registered=fake_registry.remove,
        resolve_voice=resolve,
        is_available=available,
        preview_worker=preview,
        activate_voice=activate,
    )


def test_auto_submit_warning_states_the_entire_operator_boundary() -> None:
    assert AUTO_SUBMIT_WARNING == (
        "Auto-submit sends Enter to the eligible foreground terminal captured at "
        "finish-toggle; the operator is responsible for the intended tab, pane, shell, "
        "and cursor position."
    )


def test_lists_bundled_registered_and_cloned_voices_with_status(tmp_path: Path) -> None:
    missing_reference = tmp_path / "missing.wav"
    bundled = (_voice("bundled"),)
    fake = FakeRegistry(
        [
            _registered("piper", params={"model": "m", "config": "c"}),
            _registered("rick", "clone", {"reference": str(missing_reference)}),
        ]
    )
    store = ConfigStore(tmp_path / "config.json")
    store.save({"default-voice": "rick"})

    service = _service(
        store,
        fake,
        bundled=bundled,
        available=lambda voice: voice.name == "piper",
    )

    options = {option.name: option for option in service.list_voices()}
    assert options["bundled"].availability is VoiceAvailability.UNAVAILABLE
    assert options["bundled"].fault is None
    assert options["piper"].availability is VoiceAvailability.AVAILABLE
    assert options["piper"].source is VoiceSource.REGISTERED
    assert options["rick"].selected is True
    assert options["rick"].source is VoiceSource.CLONED
    assert options["rick"].fault is not None
    assert options["rick"].fault.code is VoiceFaultCode.MISSING_ASSET


def test_select_voice_is_atomic_and_preserves_unrelated_settings(tmp_path: Path) -> None:
    fake = FakeRegistry([_registered("rick")])
    store = ConfigStore(tmp_path / "config.json")
    store.save({"recording-mode": "push-toggle", "future-setting": {"kept": True}})
    service = _service(store, fake)

    selected = service.select_voice("RICK")

    assert selected.name == "rick"
    assert selected.selected is True
    assert store.load() == {
        "_schema-version": 1,
        "recording-mode": "push-toggle",
        "future-setting": {"kept": True},
        "default-voice": "rick",
    }


def test_select_voice_applies_to_live_runtime_after_persisting(tmp_path: Path) -> None:
    fake = FakeRegistry([_registered("rick"), _registered("gimli")])
    store = ConfigStore(tmp_path / "config.json")
    store.save({"default-voice": "rick"})
    activations: list[tuple[str, str]] = []

    def activate(name: str) -> None:
        activations.append((name, store.load()["default-voice"]))

    service = _service(store, fake, activate=activate)

    service.select_voice("GIMLI")

    assert activations == [("gimli", "gimli")]
    assert store.load()["default-voice"] == "gimli"


def test_live_activation_failure_restores_previous_persisted_voice(tmp_path: Path) -> None:
    fake = FakeRegistry([_registered("rick"), _registered("gimli")])
    store = ConfigStore(tmp_path / "config.json")
    store.save({"default-voice": "rick", "future-setting": 7})

    def fail(_name: str) -> None:
        raise RuntimeError("private worker failure")

    service = _service(store, fake, activate=fail)

    with raises(VoiceActivationError, match="gimli"):
        service.select_voice("gimli")

    assert store.load()["default-voice"] == "rick"
    assert store.load()["future-setting"] == 7


def test_preview_is_immediate_and_does_not_persist_selection(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    fake = FakeRegistry([_registered("rick")])
    store = ConfigStore(tmp_path / "config.json")
    store.save({"default-voice": "gimli"})
    service = _service(store, fake, preview=lambda name, text: calls.append((name, text)))

    service.preview_voice("rick", "  Ready.  ")

    assert calls == [("rick", "Ready.")]
    assert store.load()["default-voice"] == "gimli"


def test_preview_failure_is_reported_without_changing_config(tmp_path: Path) -> None:
    fake = FakeRegistry([_registered("rick")])
    store = ConfigStore(tmp_path / "config.json")
    store.save({"default-voice": "rick"})

    def fail(_name: str, _text: str) -> None:
        raise RuntimeError("speaker unavailable")

    service = _service(store, fake, preview=fail)
    with raises(VoicePreviewError):
        service.preview_voice("rick")
    assert store.load()["default-voice"] == "rick"


def test_guided_clone_flow_registers_previews_then_selects(tmp_path: Path) -> None:
    fake = FakeRegistry([_registered("rick"), _registered("gimli")])
    store = ConfigStore(tmp_path / "config.json")
    store.save({"default-voice": "rick", "recording-mode": "push-toggle"})
    previews: list[tuple[str, str]] = []
    steps: list[VoiceFlowStep] = []
    service = _service(store, fake, preview=lambda name, text: previews.append((name, text)))

    result = service.import_voice(
        VoiceImportRequest(
            kind=VoiceImportKind.CLONE,
            name="narrator",
            source_path=tmp_path / "source.wav",
            preview_text="Testing narrator.",
        ),
        on_step=steps.append,
    )

    assert steps == [
        VoiceFlowStep.VALIDATE,
        VoiceFlowStep.REGISTER,
        VoiceFlowStep.PREVIEW,
        VoiceFlowStep.SELECT,
    ]
    assert previews == [("narrator", "Testing narrator.")]
    assert result.previewed is True
    assert result.selected is True
    assert store.load() == {
        "_schema-version": 1,
        "default-voice": "narrator",
        "recording-mode": "push-toggle",
    }
    assert fake.assets == {"rick", "gimli", "narrator"}


def test_duplicate_is_rejected_before_mutation_case_insensitively(tmp_path: Path) -> None:
    fake = FakeRegistry([_registered("Rick"), _registered("gimli")])
    store = ConfigStore(tmp_path / "config.json")
    store.save({"default-voice": "gimli"})
    service = _service(store, fake)

    with raises(DuplicateVoiceError):
        service.import_voice(
            VoiceImportRequest(
                kind=VoiceImportKind.PIPER,
                name="rICK",
                source_path=tmp_path / "voice.onnx",
            )
        )

    assert fake.removed == []
    assert fake.assets == {"Rick", "gimli"}
    assert store.load()["default-voice"] == "gimli"


def test_cancellation_after_registration_rolls_back_only_new_voice(tmp_path: Path) -> None:
    fake = FakeRegistry([_registered("rick"), _registered("gimli")])
    store = ConfigStore(tmp_path / "config.json")
    store.save({"default-voice": "rick"})
    service = _service(store, fake)
    cancel = False

    def on_step(step: VoiceFlowStep) -> None:
        nonlocal cancel
        if step is VoiceFlowStep.PREVIEW:
            cancel = True

    with raises(VoiceFlowCancelled):
        service.import_voice(
            VoiceImportRequest(
                kind=VoiceImportKind.CLONE,
                name="temporary",
                source_path=tmp_path / "source.wav",
            ),
            cancelled=lambda: cancel,
            on_step=on_step,
        )

    assert fake.removed == ["temporary"]
    assert set(fake.voices) == {"rick", "gimli"}
    assert fake.assets == {"rick", "gimli"}
    assert store.load()["default-voice"] == "rick"


def test_preview_failure_rolls_back_new_registration_and_preserves_assets(
    tmp_path: Path,
) -> None:
    fake = FakeRegistry([_registered("rick"), _registered("gimli")])
    store = ConfigStore(tmp_path / "config.json")
    store.save({"default-voice": "gimli"})

    def fail(_name: str, _text: str) -> None:
        raise RuntimeError("preview failed")

    service = _service(store, fake, preview=fail)
    with raises(VoicePreviewError):
        service.import_voice(
            VoiceImportRequest(
                kind=VoiceImportKind.PIPER,
                name="temporary",
                source_path=tmp_path / "voice.onnx",
            )
        )

    assert fake.removed == ["temporary"]
    assert fake.assets == {"rick", "gimli"}
    assert store.load()["default-voice"] == "gimli"


def test_selection_write_failure_rolls_back_new_registration(tmp_path: Path) -> None:
    class RejectingStore:
        def load(self) -> dict[str, Any]:
            return {"default-voice": "rick", "recording-mode": "push-toggle"}

        def update(
            self,
            _mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
        ) -> dict[str, Any]:
            raise OSError("config is read-only")

    fake = FakeRegistry([_registered("rick"), _registered("gimli")])
    service = _service(RejectingStore(), fake)

    with raises(OSError, match="read-only"):
        service.import_voice(
            VoiceImportRequest(
                kind=VoiceImportKind.PIPER,
                name="temporary",
                source_path=tmp_path / "voice.onnx",
                preview=False,
            )
        )

    assert fake.removed == ["temporary"]
    assert fake.assets == {"rick", "gimli"}


def test_failed_registration_does_not_rollback_concurrent_winner(tmp_path: Path) -> None:
    fake = FakeRegistry()
    store = ConfigStore(tmp_path / "config.json")
    winner = _registered(
        "shared",
        "clone",
        {"reference": "winner.wav"},
    )
    barrier = threading.Barrier(2)
    winner_errors: list[BaseException] = []

    def losing_add(
        _name: str,
        _model: str | Path,
        _config: str | Path | None,
        *,
        language: str,
    ) -> registry.RegisteredVoice:
        assert language == "en"
        barrier.wait(timeout=2)
        barrier.wait(timeout=2)
        raise registry.RegistryError("concurrent registration won")

    def register_winner() -> None:
        try:
            barrier.wait(timeout=2)
            fake.voices[winner.name] = winner
            fake.assets.add(winner.name)
            barrier.wait(timeout=2)
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            winner_errors.append(exc)

    service = VoiceSettingsService(
        settings_store=store,
        bundled_voices=(),
        list_registered=fake.list,
        add_piper=losing_add,
        create_clone=fake.create_clone,
        remove_registered=fake.remove,
        resolve_voice=fake.resolve,
        is_available=lambda _voice: True,
    )
    winner_thread = threading.Thread(target=register_winner)
    winner_thread.start()
    try:
        with raises(registry.RegistryError, match="concurrent registration won"):
            service.import_voice(
                VoiceImportRequest(
                    kind=VoiceImportKind.PIPER,
                    name="shared",
                    source_path=tmp_path / "voice.onnx",
                    preview=False,
                    select=False,
                )
            )
    finally:
        winner_thread.join(timeout=2)

    assert winner_thread.is_alive() is False
    assert winner_errors == []
    assert fake.removed == []
    assert fake.voices == {"shared": winner}
    assert fake.assets == {"shared"}


def load_tests(
    _loader: unittest.TestLoader,
    _tests: unittest.TestSuite,
    _pattern: str | None,
) -> unittest.TestSuite:
    """Expose the compact function-style corpus to stdlib unittest discovery."""

    suite = unittest.TestSuite()
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue

        def run(test=function) -> None:
            if test.__code__.co_argcount == 0:
                test()
                return
            with tempfile.TemporaryDirectory() as temporary:
                test(Path(temporary))

        suite.addTest(unittest.FunctionTestCase(run, description=name))
    return suite
