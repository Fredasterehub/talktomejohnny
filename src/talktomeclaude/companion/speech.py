"""Mute-aware production composition for canonical companion speech."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from talktomeclaude.reply import ReplyEvent
from talktomeclaude.speech import (
    CanonicalSpeechController,
    Control,
    ControlCommand,
    NavigationResult,
    OralSessionStore,
    OralSessionError,
    OralStatus,
    PersistentSpeechRuntime,
    SoundDevicePlayback,
    SpeechPipeline,
    SynthesisWorker,
    production_synthesis_worker,
)


@dataclass(frozen=True, slots=True)
class SpeechControlOutcome:
    """Content-free result for companion lifecycle routing."""

    applied: bool
    speaking: bool


class _OfferQueue(Protocol):
    def offer(
        self,
        unit_id: str,
        text: str,
        *,
        effect_id: str | None = None,
    ) -> bool: ...

    def stop(self) -> object: ...


class _RuntimeOwner(Protocol):
    @property
    def selected_voice(self) -> str: ...

    def switch_voice(self, selected_voice: str, parent_state: object) -> object: ...

    def shutdown(self) -> bool: ...


class _VolumeControl(Protocol):
    def set_volume(self, volume: int) -> None: ...


class _MuteGate:
    """Freeze muted answers while refusing every playback admission."""

    def __init__(self, pipeline: _OfferQueue, *, muted: bool) -> None:
        self._pipeline = pipeline
        self._muted = muted
        self._lock = threading.Lock()

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            self._muted = muted

    def offer(
        self,
        unit_id: str,
        text: str,
        *,
        effect_id: str | None = None,
    ) -> bool:
        with self._lock:
            if self._muted:
                return False
        return self._pipeline.offer(unit_id, text, effect_id=effect_id)

    def stop(self) -> object:
        return self._pipeline.stop()


class CompanionSpeech:
    """Own one selected voice and bridge whole-answer completion to runtime."""

    _INFORMATIONAL_CONTROLS = frozenset(
        {
            Control.TOPICS,
            Control.SUMMARIZE,
            Control.DEEPER,
            Control.WHERE,
            Control.HELP,
        }
    )

    def __init__(
        self,
        controller: CanonicalSpeechController,
        session: OralSessionStore,
        gate: _MuteGate,
        runtime: _RuntimeOwner,
        *,
        on_answer_finished: Callable[[], None],
        playback: _VolumeControl | None = None,
    ) -> None:
        self._controller = controller
        self._session = session
        self._gate = gate
        self._playback = playback
        self._runtime = runtime
        self._on_answer_finished = on_answer_finished
        self._active_answer_id: str | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._control_sequence = 0
        self._control_unit_ids: set[str] = set()
        self._control_resume_ids: set[str] = set()
        self._last_interrupted_answer_id: str | None = None

    @classmethod
    def create(
        cls,
        selected_voice: str,
        session_path: str | Path,
        *,
        on_answer_finished: Callable[[], None],
        initially_muted: bool = False,
        worker_factory: Callable[[str], SynthesisWorker] = production_synthesis_worker,
        playback: SoundDevicePlayback | None = None,
        output_volume: int = 100,
    ) -> "CompanionSpeech":
        """Build the callback cycle without exposing partially initialized owners."""

        runtime = PersistentSpeechRuntime(selected_voice, worker_factory)
        session = OralSessionStore(session_path)
        holder: dict[str, CompanionSpeech] = {}
        playback = playback or SoundDevicePlayback(volume=output_volume)
        pipeline = SpeechPipeline(
            runtime,
            playback,
            on_unit_completed=lambda unit_id: holder["owner"]._unit_completed(unit_id),
        )
        gate = _MuteGate(pipeline, muted=initially_muted)
        controller = CanonicalSpeechController(session, gate)
        owner = cls(
            controller,
            session,
            gate,
            runtime,
            on_answer_finished=on_answer_finished,
            playback=playback,
        )
        holder["owner"] = owner
        return owner

    @property
    def muted(self) -> bool:
        return self._gate.muted

    def accept(self, event: ReplyEvent) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("speech presentation is closed")
            self._discard_control_responses_locked()
            self._controller.accept(event)
            self._active_answer_id = event.event_id
            self._last_interrupted_answer_id = None

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            if self._closed:
                return
            if muted:
                self._gate.stop()
                self._discard_control_responses_locked()
                self._last_interrupted_answer_id = None
            self._gate.set_muted(muted)

    def continue_explicitly(self) -> None:
        """Resume only after a direct user action; unmute never auto-resumes."""

        with self._lock:
            if self._closed or self._gate.muted:
                return
            self._controller.continue_explicitly()

    def interrupt(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                interrupted = self._controller.interrupt()
                self._last_interrupted_answer_id = interrupted.answer_id
            except RuntimeError:
                self._gate.stop()
                self._last_interrupted_answer_id = None
            self._discard_control_responses_locked()
            self._active_answer_id = None

    def handle_control(self, command: ControlCommand) -> SpeechControlOutcome:
        """Apply one accepted local control without exposing answer wording."""

        with self._lock:
            control = command.control
            if self._closed or self._gate.muted or control is Control.VOICE_OFF:
                return SpeechControlOutcome(False, False)

            result: NavigationResult
            resume_after_response = False
            active_answer_id = self._session.active_answer_id()
            try:
                if active_answer_id is None:
                    if control in {Control.CONTINUE, Control.KEEP_GOING}:
                        result = self._controller.go_back()
                    else:
                        result = self._controller.go_back(
                            schedule=False,
                            skip_answer_id=(
                                self._last_interrupted_answer_id
                                if control is Control.GO_BACK
                                else None
                            ),
                        )
                    if control in self._INFORMATIONAL_CONTROLS:
                        self._controller.pause()
                        result = self._controller.navigate(
                            control,
                            target=command.target,
                        )
                    elif control not in {
                        Control.GO_BACK,
                        Control.CONTINUE,
                        Control.KEEP_GOING,
                    }:
                        result = self._controller.navigate(
                            control,
                            target=command.target,
                        )
                else:
                    if control is Control.GO_BACK:
                        result = self._controller.go_back(schedule=False)
                    elif control in self._INFORMATIONAL_CONTROLS:
                        self._controller.pause()
                        result = self._controller.navigate(
                            control,
                            target=command.target,
                        )
                    else:
                        result = self._controller.navigate(
                            control,
                            target=command.target,
                        )
            except (OralSessionError, RuntimeError):
                if (
                    active_answer_id is None
                    and self._session.active_answer_id() is not None
                ):
                    try:
                        self._controller.interrupt()
                    except (OralSessionError, RuntimeError):
                        self._gate.stop()
                    self._active_answer_id = None
                return SpeechControlOutcome(False, False)

            state = result.state
            if state is not None and state.status in {
                OralStatus.ACTIVE,
                OralStatus.PAUSED,
            }:
                self._active_answer_id = state.roadmap.answer_id
            else:
                self._active_answer_id = None

            response_offered = False
            if result.response and (
                control is Control.GO_BACK
                or control in self._INFORMATIONAL_CONTROLS
            ):
                self._control_sequence += 1
                unit_id = f"control-response-{self._control_sequence}"
                response_offered = self._gate.offer(unit_id, result.response)
                if response_offered:
                    self._control_unit_ids.add(unit_id)
                    resume_after_response = control is Control.GO_BACK
                    if resume_after_response:
                        self._control_resume_ids.add(unit_id)
            if control is Control.GO_BACK and not response_offered:
                self._controller.schedule_active()
            self._last_interrupted_answer_id = None
            return SpeechControlOutcome(
                True,
                response_offered
                or (state is not None and state.status is OralStatus.ACTIVE),
            )

    def set_volume(self, volume: int) -> None:
        with self._lock:
            if self._closed or self._playback is None:
                return
            self._playback.set_volume(volume)

    def set_voice(self, selected_voice: str) -> None:
        """Stop the current utterance and bind future speech to one exact voice."""

        if not isinstance(selected_voice, str) or not selected_voice.strip():
            raise ValueError("selected voice must not be empty")
        with self._lock:
            if self._closed:
                raise RuntimeError("speech presentation is closed")
            if self._runtime.selected_voice == selected_voice:
                return
            self.interrupt()
            self._runtime.switch_voice(selected_voice, None)

    def stop(self) -> None:
        with self._lock:
            if not self._closed:
                self._gate.stop()
                self._discard_control_responses_locked()
                self._last_interrupted_answer_id = None

    def _unit_completed(self, unit_id: str) -> bool:
        with self._lock:
            if self._closed:
                return False
            if unit_id in self._control_unit_ids:
                self._control_unit_ids.remove(unit_id)
                resume = unit_id in self._control_resume_ids
                self._control_resume_ids.discard(unit_id)
                if resume:
                    self._controller.schedule_active()
                    return True
                control_finished = True
            else:
                control_finished = False
            if control_finished:
                finished = True
            else:
                answer_id = self._active_answer_id
                if answer_id is None or not self._controller.unit_completed(unit_id):
                    return False
                state = self._session.restore(answer_id)
                finished = state is not None and state.status is OralStatus.COMPLETE
                if finished:
                    self._active_answer_id = None
        if finished:
            self._on_answer_finished()
        return True

    def _discard_control_responses_locked(self) -> None:
        self._control_unit_ids.clear()
        self._control_resume_ids.clear()

    def shutdown(self) -> bool:
        with self._lock:
            if self._closed:
                return True
            self._closed = True
            self._gate.stop()
            self._discard_control_responses_locked()
            self._last_interrupted_answer_id = None
        return self._runtime.shutdown()


__all__ = ["CompanionSpeech", "SpeechControlOutcome"]
