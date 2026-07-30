"""Production-facing companion controller with injectable side-effect seams."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from talktomeclaude.capture import CaptureMode
from talktomeclaude.capture.contracts import TranscriberFactory
from talktomeclaude.companion.capture_delivery import (
    CaptureDeliveryCode,
    CaptureDeliveryCoordinator,
    CaptureDeliveryResult,
    TranscriptReview,
)
from talktomeclaude.companion.contracts import (
    CompanionIntent,
    CompanionSnapshot,
    IntentKind,
)
from talktomeclaude.companion.speech import SpeechControlOutcome
from talktomeclaude.core import EventKind, RuntimeEvent, RuntimePhase
from talktomeclaude.diagnostics import DiagnosticStore, opaque_identity
from talktomeclaude.platform.contracts import DeliveryMode
from talktomeclaude.reply import ReplyEvent
from talktomeclaude.speech import Control, ControlCommand


class MicrophoneBoundary(Protocol):
    """One dedicated input stream controlled by companion intents."""

    def start(self) -> object: ...

    def seal(self) -> None: ...

    def stop(self) -> object: ...

    def close(self) -> object: ...


class SpeechPresentation(Protocol):
    """Canonical reply presentation owned outside the UI thread."""

    def accept(self, event: ReplyEvent) -> None: ...

    def set_muted(self, muted: bool) -> None: ...

    def interrupt(self) -> None: ...

    def handle_control(self, command: ControlCommand) -> SpeechControlOutcome: ...

    def stop(self) -> None: ...

    def set_volume(self, volume: int) -> None: ...

    def shutdown(self) -> bool: ...


class ReplyInbox(Protocol):
    """Durable local/SSH inbox whose callback commits one local effect."""

    def start(
        self,
        on_reply: Callable[[ReplyEvent], bool],
        on_status: Callable[[str], None],
    ) -> None: ...

    def stop(self) -> bool: ...


SurfaceAction = Callable[[], None]
SnapshotListener = Callable[[CompanionSnapshot], None]
WorkerStarter = Callable[[str, Callable[[], None]], object]


def _normalize_microphone_level(rms: float) -> float:
    """Map float32 input RMS to a useful 0-1 meter using -60 dBFS as silence."""

    if not math.isfinite(rms) or rms <= 0.001:
        return 0.0
    dbfs = 20.0 * math.log10(min(1.0, rms))
    return min(1.0, max(0.0, (dbfs + 60.0) / 60.0))


def _start_daemon(name: str, target: Callable[[], None]) -> threading.Thread:
    worker = threading.Thread(target=target, name=name, daemon=True)
    worker.start()
    return worker


@dataclass(slots=True)
class CompanionSurfaces:
    """Direct-user surfaces that alone may ask Windows for focus."""

    settings: SurfaceAction = field(default=lambda: None)
    voice: SurfaceAction = field(default=lambda: None)
    review: SurfaceAction = field(default=lambda: None)
    diagnostics: SurfaceAction = field(default=lambda: None)


class CompanionController:
    """Join shell intents to capture, delivery, reply, speech, and shutdown.

    The controller publishes only content-safe snapshots.  A rejected or
    low-confidence transcript remains available through :attr:`pending_review`
    for a direct review surface, but never enters diagnostics or snapshot text.
    """

    _INTERRUPTIBLE_SPEECH = frozenset(
        {RuntimePhase.PLANNING, RuntimePhase.SPEAKING, RuntimePhase.PAUSED}
    )

    def __init__(
        self,
        capture: CaptureDeliveryCoordinator,
        microphone: MicrophoneBoundary,
        transcriber_factory: TranscriberFactory,
        *,
        speech: SpeechPresentation | None = None,
        inbox: ReplyInbox | None = None,
        diagnostics: DiagnosticStore | None = None,
        surfaces: CompanionSurfaces | None = None,
        capture_mode: CaptureMode = CaptureMode.PUSH_TOGGLE,
        delivery_mode: DeliveryMode = DeliveryMode.ASSISTANT,
        assistant_auto_submit: bool = True,
        output_muted: bool = False,
        output_volume: int = 100,
        persist_output_muted: Callable[[bool], None] | None = None,
        persist_output_volume: Callable[[int], None] | None = None,
        worker_starter: WorkerStarter = _start_daemon,
        input_level_clock: Callable[[], float] = time.monotonic,
        shutdown_deadline_seconds: float = 1.5,
    ) -> None:
        if shutdown_deadline_seconds <= 0:
            raise ValueError("shutdown deadline must be positive")
        if type(output_volume) is not int or not 0 <= output_volume <= 100:
            raise ValueError("output volume must be an integer between 0 and 100")
        self._capture = capture
        self._microphone = microphone
        self._transcriber_factory = transcriber_factory
        self._speech = speech
        self._inbox = inbox
        self._diagnostics = diagnostics
        self._surfaces = surfaces or CompanionSurfaces()
        self._capture_mode = capture_mode
        self._delivery_mode = delivery_mode
        self._auto_submit = assistant_auto_submit
        self._output_muted = output_muted
        self._output_volume = output_volume
        self._persist_output_muted = persist_output_muted
        self._persist_output_volume = persist_output_volume
        self._start_worker = worker_starter
        self._input_level_clock = input_level_clock
        self._shutdown_deadline = shutdown_deadline_seconds
        self._listeners: list[SnapshotListener] = []
        self._lock = threading.RLock()
        self._capture_control = threading.RLock()
        self._worker_condition = threading.Condition(self._lock)
        self._detail = ""
        self._pending_review: TranscriptReview | None = None
        self._closing = False
        self._started = False
        self._finish_inflight = False
        self._cancel_event = threading.Event()
        self._worker_count = 0
        self._shutdown_clean = True
        self._microphone_level = 0.0
        self._microphone_level_last_publish: float | None = None
        self._microphone_level_publish_interval_seconds = 0.1

    @property
    def snapshot(self) -> CompanionSnapshot:
        with self._lock:
            return CompanionSnapshot(
                runtime=self._capture.runtime.state,
                detail=self._detail,
                output_muted=self._output_muted,
                output_volume=self._output_volume,
                microphone_level=self._microphone_level,
            )

    @property
    def pending_review(self) -> TranscriptReview | None:
        with self._lock:
            return self._pending_review

    @property
    def capture_mode(self) -> CaptureMode:
        """Return the configured control mode for hotkey press/release routing."""

        with self._lock:
            return self._capture_mode

    @property
    def shutdown_clean(self) -> bool:
        with self._lock:
            return self._shutdown_clean

    def subscribe(self, listener: SnapshotListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return unsubscribe

    def start_background(self) -> None:
        with self._lock:
            if self._started or self._closing:
                return
            self._started = True
        if self._inbox is not None:
            self._inbox.start(self.receive_reply, self.transport_status)
        self._publish()

    def set_assistant_auto_submit(self, enabled: bool) -> None:
        with self._lock:
            self._auto_submit = bool(enabled)
        self._set_detail(
            "Assistant auto-submit enabled" if enabled else "Assistant auto-submit disabled"
        )
        self._publish()

    def set_capture_mode(self, mode: CaptureMode) -> None:
        if mode not in {CaptureMode.PUSH_TOGGLE, CaptureMode.HOLD_TO_TALK}:
            raise ValueError("unsupported companion capture mode")
        with self._capture_control:
            with self._lock:
                if self._capture.runtime.state.phase is RuntimePhase.RECORDING:
                    raise RuntimeError("recording mode cannot change during capture")
                self._capture_mode = mode
        self._set_detail(f"Recording control: {mode.value}")
        self._publish()

    def set_output_volume(self, volume: int) -> CompanionSnapshot:
        self._set_output_volume(volume)
        self._set_detail(f"Output volume set to {volume}")
        return self._publish()

    def dispatch(self, intent: CompanionIntent) -> CompanionSnapshot:
        if intent.kind is IntentKind.STATUS:
            return self.snapshot
        if intent.kind is IntentKind.START_RECORDING:
            return self._start_recording()
        if intent.kind is IntentKind.FINISH_RECORDING:
            return self._finish_recording()
        if intent.kind is IntentKind.CANCEL:
            return self._cancel()
        if intent.kind is IntentKind.TOGGLE_OUTPUT_MUTE:
            return self._toggle_output_mute()
        if intent.kind is IntentKind.OPEN_SETTINGS:
            self._require_direct_focus(intent)
            self._surfaces.settings()
            return self.snapshot
        if intent.kind is IntentKind.OPEN_VOICE:
            self._require_direct_focus(intent)
            self._surfaces.voice()
            return self.snapshot
        if intent.kind is IntentKind.OPEN_REVIEW:
            self._require_direct_focus(intent)
            if self.pending_review is None:
                raise RuntimeError("there is no transcript awaiting review")
            self._surfaces.review()
            return self.snapshot
        if intent.kind is IntentKind.OPEN_DIAGNOSTICS:
            self._require_direct_focus(intent)
            self._surfaces.diagnostics()
            return self.snapshot
        if intent.kind is IntentKind.QUIT:
            return self._quit()
        raise ValueError(f"unsupported companion intent {intent.kind.value}")

    @staticmethod
    def _require_direct_focus(intent: CompanionIntent) -> None:
        if not intent.allow_focus:
            raise ValueError("direct companion surface requires explicit focus intent")

    def _start_recording(self) -> CompanionSnapshot:
        with self._capture_control:
            with self._lock:
                if self._closing:
                    raise RuntimeError("companion is stopping")
                if self._finish_inflight:
                    raise RuntimeError("recording finish is already in progress")
                phase = self._capture.runtime.state.phase
                self._pending_review = None
                capture_mode = self._capture_mode
                self._cancel_event.set()
                self._cancel_event = threading.Event()
            if phase in self._INTERRUPTIBLE_SPEECH and self._speech is not None:
                self._speech.interrupt()
            self._capture.start(capture_mode)
            with self._lock:
                self._microphone_level = 0.0
                self._microphone_level_last_publish = None
            try:
                self._microphone.start()
            except BaseException:
                self._capture.cancel()
                raise
        self._set_detail("Microphone active")
        self._record_transition("start_recording")
        return self._publish()

    def _finish_recording(self) -> CompanionSnapshot:
        with self._capture_control:
            with self._lock:
                if self._finish_inflight:
                    raise RuntimeError("recording finish is already in progress")
                if self._capture.runtime.state.phase is not RuntimePhase.RECORDING:
                    raise RuntimeError("there is no active recording to finish")
                self._finish_inflight = True
                capture_mode = self._capture_mode
                cancelled = self._cancel_event.is_set
            try:
                seal = getattr(self._microphone, "seal", None)
                if callable(seal):
                    seal()
                completion = (
                    self._capture.begin_release_hold()
                    if capture_mode is CaptureMode.HOLD_TO_TALK
                    else self._capture.begin_finish_toggle()
                )
                stop_result = self._microphone.stop()
                if getattr(stop_result, "silence_confirmed", True) is not True:
                    self._cancel_event.set()
                    self._capture.runtime.dispatch(
                        RuntimeEvent(
                            EventKind.ERROR_OCCURRED,
                            error_code="audio_input_stop_failed",
                            recover_to=RuntimePhase.IDLE,
                        )
                    )
                    self._set_detail(
                        "Microphone could not stop safely; restart the companion"
                    )
                    self._record_error("audio_input_stop_failed")
                    return self._publish()
            except BaseException:
                with self._lock:
                    self._finish_inflight = False
                raise
            finally:
                if self._capture.runtime.state.phase is RuntimePhase.RECOVERABLE_ERROR:
                    with self._lock:
                        self._finish_inflight = False
        self._set_detail("Transcribing locally")
        self._set_microphone_level(0.0)
        current = self._publish()

        def process() -> None:
            try:
                result = self._capture.process_completion(
                    completion,
                    self._transcriber_factory,
                    mode=self._delivery_mode,
                    auto_submit=self._auto_submit,
                    cancelled=cancelled,
                    runtime_transitioned=True,
                )
                self._capture_finished(result)
            except Exception as exc:
                if self._cancel_event.is_set():
                    return
                self._capture.runtime.dispatch(
                    RuntimeEvent(
                        EventKind.ERROR_OCCURRED,
                        error_code=type(exc).__name__,
                        recover_to=RuntimePhase.AWAITING_CONFIRMATION,
                    )
                )
                self._set_detail("Capture needs attention")
                self._record_error(type(exc).__name__)
                self._publish()
            finally:
                with self._lock:
                    self._finish_inflight = False

        self._start_owned_worker("ttc-companion-capture-turn", process)
        return current

    def _capture_finished(self, result: CaptureDeliveryResult) -> None:
        with self._lock:
            if self._closing:
                return
            self._pending_review = result.transcript
        if result.code is CaptureDeliveryCode.CONTROL:
            self._capture_control_finished(result)
            return
        details = {
            CaptureDeliveryCode.DELIVERED: "Transcript delivered; waiting for assistant",
            CaptureDeliveryCode.REVIEW_REQUIRED: "Review the transcript before delivery",
            CaptureDeliveryCode.CANCELLED: "Capture cancelled",
            CaptureDeliveryCode.STALE: "Stale capture discarded",
            CaptureDeliveryCode.TRANSCRIPTION_FAILED: "Transcription needs attention",
            CaptureDeliveryCode.DELIVERY_FAILED: "Delivery needs attention",
        }
        self._set_detail(details[result.code])
        if self._diagnostics is not None:
            self._diagnostics.record("capture_result", **result.diagnostics)
        self._publish()

    def _capture_control_finished(self, result: CaptureDeliveryResult) -> None:
        raw_control = result.control
        command = (
            raw_control
            if isinstance(raw_control, ControlCommand)
            else ControlCommand(raw_control)
            if isinstance(raw_control, Control)
            else None
        )
        if self._cancel_event.is_set():
            detail = "Capture cancelled"
        elif command is None:
            detail = "Voice control unavailable"
        elif command.control is Control.VOICE_OFF:
            self._set_output_muted(True)
            detail = "Spoken output muted"
        elif self._speech is None:
            detail = "No spoken answer to control"
        else:
            outcome = self._speech.handle_control(command)
            if outcome.speaking:
                received = self._capture.runtime.dispatch(
                    RuntimeEvent(EventKind.REPLY_RECEIVED)
                )
                planned = self._capture.runtime.dispatch(
                    RuntimeEvent(EventKind.PLAN_READY)
                )
                if not received.accepted or not planned.accepted:
                    raise RuntimeError("runtime rejected resumed speech control")
                detail = "Speaking reply"
            elif outcome.applied:
                detail = "Voice control applied"
            else:
                detail = "No spoken answer to control"
        self._set_detail(detail)
        if self._diagnostics is not None:
            self._diagnostics.record("capture_result", **result.diagnostics)
        self._publish()

    def confirm_review(self, text: str) -> CompanionSnapshot:
        """Deliver direct-user edited text against a fresh target snapshot."""

        with self._lock:
            review = self._pending_review
            if review is None:
                raise RuntimeError("there is no transcript awaiting review")
            updated = TranscriptReview(
                review.disposition,
                text,
                review.confidence,
                review.reason,
            )

        def confirm() -> None:
            result = self._capture.confirm_or_recover(
                updated,
                mode=self._delivery_mode,
                auto_submit=self._auto_submit,
                cancelled=self._cancel_event.is_set,
            )
            self._capture_finished(result)

        self._start_owned_worker("ttc-companion-review-delivery", confirm)
        return self.snapshot

    def _cancel(self) -> CompanionSnapshot:
        with self._capture_control:
            self._cancel_event.set()
            try:
                self._microphone.stop()
            except Exception:
                pass
            if self._speech is not None:
                self._speech.stop()
            self._capture.cancel()
        with self._lock:
            self._pending_review = None
        self._set_microphone_level(0.0)
        self._set_detail("Cancelled")
        return self._publish()

    def _toggle_output_mute(self) -> CompanionSnapshot:
        with self._lock:
            muted = not self._output_muted
        self._set_output_muted(muted)
        self._set_detail("Spoken output muted" if muted else "Spoken output enabled")
        return self._publish()

    def _set_output_muted(self, muted: bool) -> None:
        with self._lock:
            changed = self._output_muted != muted
            self._output_muted = muted
            phase = self._capture.runtime.state.phase
        if muted and self._speech is not None:
            self._speech.stop()
        if muted and phase in {RuntimePhase.SPEAKING, RuntimePhase.PAUSED}:
            finished = self._capture.runtime.dispatch(
                RuntimeEvent(EventKind.SPEECH_FINISHED)
            )
            if not finished.accepted:
                raise RuntimeError("runtime rejected muted speech completion")
        if self._speech is not None:
            self._speech.set_muted(muted)
        if changed and self._persist_output_muted is not None:
            self._persist_output_muted(muted)

    def _set_output_volume(self, volume: int) -> None:
        if type(volume) is not int or not 0 <= volume <= 100:
            raise ValueError("output volume must be an integer between 0 and 100")
        with self._lock:
            changed = self._output_volume != volume
            if not changed:
                return
            previous = self._output_volume
            if self._persist_output_volume is not None:
                self._persist_output_volume(volume)
            try:
                if self._speech is not None:
                    self._speech.set_volume(volume)
            except Exception:
                if self._persist_output_volume is not None:
                    self._persist_output_volume(previous)
                raise
            self._output_volume = volume

    def _set_microphone_level(self, level: float) -> None:
        normalized = _normalize_microphone_level(float(level))
        with self._lock:
            self._microphone_level = normalized

    def receive_reply(self, event: ReplyEvent) -> bool:
        """Admit one durable reply and report whether its local effect committed."""

        with self._lock:
            if self._finish_inflight:
                return False
        phase = self._capture.runtime.state.phase
        if phase not in {RuntimePhase.IDLE, RuntimePhase.WAITING_FOR_CLAUDE}:
            return False
        received = self._capture.runtime.dispatch(RuntimeEvent(EventKind.REPLY_RECEIVED))
        if not received.accepted:
            return False
        self._set_detail("Preparing spoken reply")
        self._publish()
        try:
            if self._speech is not None:
                self._speech.accept(event)
            planned = self._capture.runtime.dispatch(RuntimeEvent(EventKind.PLAN_READY))
            if not planned.accepted:
                return False
            if self._output_muted or self._speech is None:
                self._capture.runtime.dispatch(RuntimeEvent(EventKind.SPEECH_FINISHED))
                self._set_detail("Reply available in the terminal")
            else:
                self._set_detail("Speaking reply")
            if self._diagnostics is not None:
                self._diagnostics.record(
                    "reply_effect",
                    event_hash=opaque_identity(event.event_id),
                    digest_hash=opaque_identity(event.digest),
                    output_muted=self._output_muted,
                )
            self._publish()
            return True
        except Exception as exc:
            self._capture.runtime.dispatch(
                RuntimeEvent(
                    EventKind.ERROR_OCCURRED,
                    error_code=type(exc).__name__,
                    recover_to=RuntimePhase.IDLE,
                )
            )
            self._set_detail("Spoken reply failed; complete text remains in the terminal")
            self._record_error(type(exc).__name__)
            self._publish()
            return False

    def speech_finished(self) -> None:
        transition = self._capture.runtime.dispatch(
            RuntimeEvent(EventKind.SPEECH_FINISHED)
        )
        if transition.accepted:
            self._set_detail("Reply complete")
            self._publish()

    def transport_status(self, status: str) -> None:
        normalized = status.casefold()
        state = self._capture.runtime.state
        if normalized == "disconnected" and state.phase is RuntimePhase.WAITING_FOR_CLAUDE:
            self._capture.runtime.dispatch(RuntimeEvent(EventKind.TRANSPORT_DISCONNECTED))
            self._set_detail("Assistant connection interrupted; retrying")
        elif normalized == "connected" and state.phase is RuntimePhase.DISCONNECTED:
            self._capture.runtime.dispatch(RuntimeEvent(EventKind.TRANSPORT_RECONNECTED))
            self._set_detail("Assistant connection restored")
        elif normalized:
            self._set_detail(f"Reply transport: {normalized}")
        if self._diagnostics is not None:
            self._diagnostics.record("transport_status", status=normalized)
        self._publish()

    def _quit(self) -> CompanionSnapshot:
        with self._lock:
            if self._closing:
                return self.snapshot
            self._closing = True
            self._cancel_event.set()
            self._shutdown_clean = False
        with self._capture_control:
            self._capture.cancel()
        self._capture.runtime.dispatch(RuntimeEvent(EventKind.STOP_REQUESTED))
        self._set_detail("Stopping companion")
        self._publish()
        clean = self._shutdown_owners()
        with self._lock:
            self._shutdown_clean = clean
        if clean:
            self._capture.runtime.dispatch(RuntimeEvent(EventKind.STOPPED))
            self._set_detail("Companion stopped")
            self._record_transition("stopped")
        else:
            self._capture.runtime.dispatch(
                RuntimeEvent(
                    EventKind.ERROR_OCCURRED,
                    error_code="shutdown_incomplete",
                    recover_to=RuntimePhase.IDLE,
                )
            )
            self._set_detail("Shutdown incomplete; an owned boundary is still live")
            self._record_error("shutdown_incomplete")
        return self._publish()

    def set_input_level(self, level: float) -> None:
        """Publish live microphone level while recording, without storing audio."""

        now = self._input_level_clock()
        publish = False
        with self._lock:
            phase = self._capture.runtime.state.phase
            if phase is not RuntimePhase.RECORDING:
                return
            self._microphone_level = _normalize_microphone_level(float(level))
            last_publish = self._microphone_level_last_publish
            interval = self._microphone_level_publish_interval_seconds
            if last_publish is None or now - last_publish >= interval:
                self._microphone_level_last_publish = now
                publish = True
        if publish:
            self._publish()

    def _start_owned_worker(self, name: str, target: Callable[[], None]) -> object:
        with self._worker_condition:
            if self._closing:
                raise RuntimeError("companion is stopping")
            self._worker_count += 1

        def owned() -> None:
            try:
                target()
            finally:
                with self._worker_condition:
                    self._worker_count -= 1
                    self._worker_condition.notify_all()

        try:
            return self._start_worker(name, owned)
        except BaseException:
            with self._worker_condition:
                self._worker_count -= 1
                self._worker_condition.notify_all()
            raise

    def _shutdown_owners(self) -> bool:
        deadline = time.monotonic() + self._shutdown_deadline
        results: dict[str, bool] = {}
        threads: list[threading.Thread] = []

        def launch(name: str, action: Callable[[], object]) -> None:
            def run() -> None:
                try:
                    result = action()
                    results[name] = _shutdown_result_succeeded(result)
                except BaseException:
                    results[name] = False

            thread = threading.Thread(
                target=run,
                name=f"ttc-shutdown-{name}",
                daemon=True,
            )
            threads.append(thread)
            thread.start()

        launch("microphone", self._microphone.close)
        if self._inbox is not None:
            launch("inbox", self._inbox.stop)
        speech = self._speech
        if speech is not None:

            def stop_speech() -> object:
                speech.stop()
                return speech.shutdown()

            launch("speech", stop_speech)

        with self._worker_condition:
            while self._worker_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._worker_condition.wait(remaining)
            workers_stopped = self._worker_count == 0

        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        owners_stopped = all(not thread.is_alive() for thread in threads)
        return (
            workers_stopped
            and owners_stopped
            and len(results) == len(threads)
            and all(results.values())
        )

    def _set_detail(self, value: str) -> None:
        with self._lock:
            self._detail = value

    def _publish(self) -> CompanionSnapshot:
        snapshot = self.snapshot
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:
                pass
        return snapshot

    def _record_transition(self, event: str) -> None:
        if self._diagnostics is not None:
            self._diagnostics.record(
                "state_transition",
                event=event,
                current=self._capture.runtime.state.phase.value,
                generation=self._capture.runtime.state.generation,
            )

    def _record_error(self, error_code: str) -> None:
        if self._diagnostics is not None:
            self._diagnostics.record("recoverable_error", error_code=error_code)


def _shutdown_result_succeeded(result: object) -> bool:
    if result is False:
        return False
    if getattr(result, "boundary_replacement_required", False):
        return False
    if getattr(result, "silence_confirmed", True) is not True:
        return False
    return True


__all__ = [
    "CompanionController",
    "CompanionSurfaces",
    "MicrophoneBoundary",
    "ReplyInbox",
    "SpeechPresentation",
]
