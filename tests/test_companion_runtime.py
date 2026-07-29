from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from talktomeclaude.capture import (
    CaptureMode,
    CaptureService,
    SnapshotCallableAdapter,
)
from talktomeclaude.companion.capture_delivery import CaptureDeliveryCoordinator
from talktomeclaude.companion.contracts import (
    CompanionIntent,
    CompanionSnapshot,
    IntentKind,
)
from talktomeclaude.companion.runtime import (
    CompanionController,
    CompanionSurfaces,
)
from talktomeclaude.companion.speech import SpeechControlOutcome
from talktomeclaude.core import RuntimePhase
from talktomeclaude.diagnostics import DiagnosticStore
from talktomeclaude.platform.contracts import DeliveryCode, DeliveryResult
from talktomeclaude.reply import ReplyEvent
from talktomeclaude.speech import Control, ControlCommand, parse_control_command


class _Resolution:
    code = "valid"
    evidence = object()


class _Injector:
    def __init__(self) -> None:
        self.deliveries: list[tuple[str, object, object, bool]] = []
        self.snapshot_calls = 0

    def snapshot_target(self) -> _Resolution:
        self.snapshot_calls += 1
        return _Resolution()

    def deliver(
        self,
        text: str,
        evidence: object,
        *,
        mode: object,
        auto_submit: bool,
        cancelled: object = None,
    ) -> DeliveryResult:
        del cancelled
        self.deliveries.append((text, evidence, mode, auto_submit))
        return DeliveryResult(DeliveryCode.DELIVERED, pasted=True, submitted=True)


class _Microphone:
    def __init__(self, callback: Any) -> None:
        self.callback = callback
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.stop_result: object | None = None
        self.stop_entered = threading.Event()
        self.stop_gate: threading.Event | None = None
        self.seals = 0

    def start(self) -> None:
        self.started += 1

    def emit(self, chunk: bytes) -> None:
        assert self.callback is not None
        self.callback(chunk)

    def stop(self) -> object | None:
        self.stopped += 1
        self.stop_entered.set()
        if self.stop_gate is not None:
            self.stop_gate.wait(1)
        return self.stop_result

    def seal(self) -> None:
        self.seals += 1

    def close(self) -> None:
        self.closed += 1


class _Transcriber:
    def __init__(self, text: str, confidence: float | None = None) -> None:
        self.text = text
        self.confidence = confidence

    def transcribe(self, _audio: object) -> tuple[str, float | None]:
        return self.text, self.confidence


class _Speech:
    def __init__(self) -> None:
        self.events: list[ReplyEvent] = []
        self.interrupts = 0
        self.stops = 0
        self.shutdowns = 0
        self.muted = False
        self.controls: list[ControlCommand] = []
        self.control_outcome = SpeechControlOutcome(False, False)
        self.control_entered: threading.Event | None = None
        self.control_gate: threading.Event | None = None

    def accept(self, event: ReplyEvent) -> None:
        self.events.append(event)

    def interrupt(self) -> None:
        self.interrupts += 1

    def handle_control(self, command: ControlCommand) -> SpeechControlOutcome:
        self.controls.append(command)
        if self.control_entered is not None:
            self.control_entered.set()
        if self.control_gate is not None:
            self.control_gate.wait(1)
        return self.control_outcome

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    def stop(self) -> None:
        self.stops += 1

    def shutdown(self) -> bool:
        self.shutdowns += 1
        return True


class _Inbox:
    def __init__(self) -> None:
        self.reply = None
        self.status = None
        self.stops = 0
        self.stop_gate: threading.Event | None = None

    def start(self, reply: Any, status: Any) -> None:
        self.reply = reply
        self.status = status

    def stop(self) -> bool:
        self.stops += 1
        if self.stop_gate is not None:
            self.stop_gate.wait(1)
        return True


class _QueuedWorkers:
    def __init__(self) -> None:
        self.targets: list[Any] = []

    def __call__(self, _name: str, target: Any) -> object:
        self.targets.append(target)
        return target

    def run_next(self) -> None:
        self.targets.pop(0)()


class CompanionControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.injector = _Injector()
        self.capture_service = CaptureService(
            snapshot_resolver=SnapshotCallableAdapter(self.injector.snapshot_target)
        )
        self.capture = CaptureDeliveryCoordinator(
            self.capture_service,
            self.injector,
            control_parser=parse_control_command,
        )
        self.microphone = _Microphone(self.capture.add_audio)
        self.speech = _Speech()
        self.inbox = _Inbox()
        self.workers = _QueuedWorkers()
        self.transcription: list[Any] = ["hello terminal", None]
        self.surface_calls: list[str] = []
        self.persisted_mute: list[bool] = []
        self.controller = CompanionController(
            self.capture,
            self.microphone,
            lambda _cancelled: _Transcriber(*self.transcription),
            speech=self.speech,
            inbox=self.inbox,
            diagnostics=DiagnosticStore(self.root / "diagnostics.json"),
            surfaces=CompanionSurfaces(
                settings=lambda: self.surface_calls.append("settings"),
                voice=lambda: self.surface_calls.append("voice"),
                review=lambda: self.surface_calls.append("review"),
                diagnostics=lambda: self.surface_calls.append("diagnostics"),
            ),
            worker_starter=self.workers,
            shutdown_deadline_seconds=0.2,
            persist_output_muted=self.persisted_mute.append,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_capture_updates_transcribing_before_worker_and_delivers_once(self) -> None:
        observed: list[CompanionSnapshot] = []
        self.controller.subscribe(observed.append)

        recording = self.controller.dispatch(
            CompanionIntent(IntentKind.START_RECORDING)
        )
        self.microphone.emit(b"audio")
        transcribing = self.controller.dispatch(
            CompanionIntent(IntentKind.FINISH_RECORDING)
        )

        self.assertEqual(recording.runtime.phase, RuntimePhase.RECORDING)
        self.assertEqual(transcribing.runtime.phase, RuntimePhase.TRANSCRIBING)
        self.assertEqual(self.injector.deliveries, [])
        self.workers.run_next()
        self.assertEqual(len(self.injector.deliveries), 1)
        self.assertEqual(
            self.controller.snapshot.runtime.phase, RuntimePhase.WAITING_FOR_CLAUDE
        )
        self.assertEqual(
            observed[-1].detail,
            "Transcript delivered; waiting for assistant",
        )

    def test_hold_to_talk_finishes_only_through_release_boundary(self) -> None:
        self.controller.set_capture_mode(CaptureMode.HOLD_TO_TALK)
        self.assertEqual(self.controller.capture_mode, CaptureMode.HOLD_TO_TALK)
        self.controller.dispatch(CompanionIntent(IntentKind.START_RECORDING))
        self.microphone.emit(b"audio")

        transcribing = self.controller.dispatch(
            CompanionIntent(IntentKind.FINISH_RECORDING)
        )

        self.assertEqual(transcribing.runtime.phase, RuntimePhase.TRANSCRIBING)
        self.workers.run_next()
        self.assertEqual(len(self.injector.deliveries), 1)

    def test_finish_while_idle_does_not_touch_microphone_or_start_capture(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no active recording"):
            self.controller.dispatch(CompanionIntent(IntentKind.FINISH_RECORDING))

        self.assertEqual(self.microphone.stopped, 0)
        self.assertEqual(self.controller.snapshot.runtime.phase, RuntimePhase.IDLE)

    def test_unconfirmed_microphone_stop_discards_take_before_transcription(self) -> None:
        class UnsafeStop:
            silence_confirmed = False
            boundary_replacement_required = True

        self.microphone.stop_result = UnsafeStop()
        self.controller.dispatch(CompanionIntent(IntentKind.START_RECORDING))
        self.microphone.emit(b"audio")

        stopped = self.controller.dispatch(
            CompanionIntent(IntentKind.FINISH_RECORDING)
        )

        self.assertEqual(stopped.runtime.phase, RuntimePhase.RECOVERABLE_ERROR)
        self.assertEqual(self.workers.targets, [])
        self.assertEqual(self.injector.deliveries, [])
        self.assertNotIn("audio", stopped.detail.casefold())

    def test_finish_snapshots_foreground_before_bounded_device_teardown(self) -> None:
        self.microphone.stop_gate = threading.Event()
        self.controller.dispatch(CompanionIntent(IntentKind.START_RECORDING))
        self.microphone.emit(b"audio")
        errors: list[BaseException] = []

        def finish() -> None:
            try:
                self.controller.dispatch(
                    CompanionIntent(IntentKind.FINISH_RECORDING)
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=finish)
        worker.start()
        self.assertTrue(self.microphone.stop_entered.wait(1))

        self.assertEqual(self.microphone.seals, 1)
        self.assertEqual(self.injector.snapshot_calls, 1)
        self.assertEqual(
            self.controller.snapshot.runtime.phase,
            RuntimePhase.TRANSCRIBING,
        )
        self.microphone.stop_gate.set()
        worker.join(1)
        self.assertEqual(errors, [])

    def test_uncooperative_owner_is_not_reported_as_clean_shutdown(self) -> None:
        self.inbox.stop_gate = threading.Event()
        self.controller.start_background()
        started = time.monotonic()

        snapshot = self.controller.dispatch(CompanionIntent(IntentKind.QUIT))

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.controller.shutdown_clean)
        self.assertEqual(snapshot.runtime.phase, RuntimePhase.RECOVERABLE_ERROR)
        self.assertIn("incomplete", snapshot.detail.casefold())
        self.inbox.stop_gate.set()

    def test_quit_cancels_inflight_turn_before_any_late_delivery(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingTranscriber:
            def transcribe(self, _audio: object) -> tuple[str, float]:
                entered.set()
                release.wait(1)
                return "must never be delivered", 0.99

        injector = _Injector()
        capture = CaptureDeliveryCoordinator(
            CaptureService(
                snapshot_resolver=SnapshotCallableAdapter(
                    injector.snapshot_target
                )
            ),
            injector,
        )
        microphone = _Microphone(capture.add_audio)
        controller = CompanionController(
            capture,
            microphone,
            lambda _cancelled: BlockingTranscriber(),
            shutdown_deadline_seconds=0.5,
        )
        controller.dispatch(CompanionIntent(IntentKind.START_RECORDING))
        microphone.emit(b"audio")
        controller.dispatch(CompanionIntent(IntentKind.FINISH_RECORDING))
        self.assertTrue(entered.wait(1))

        stopped = controller.dispatch(CompanionIntent(IntentKind.QUIT))
        release.set()
        time.sleep(0.05)

        self.assertTrue(controller.shutdown_clean)
        self.assertEqual(stopped.runtime.phase, RuntimePhase.IDLE)
        self.assertEqual(injector.deliveries, [])

    def test_capture_mode_change_cannot_race_recording_start(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        original_start = self.capture.start

        def blocked_start(mode: CaptureMode | None = None) -> int:
            entered.set()
            self.assertTrue(release.wait(1))
            return original_start(mode)

        self.capture.start = blocked_start  # type: ignore[method-assign]
        start_errors: list[BaseException] = []
        mode_errors: list[BaseException] = []

        def start() -> None:
            try:
                self.controller.dispatch(CompanionIntent(IntentKind.START_RECORDING))
            except BaseException as exc:
                start_errors.append(exc)

        def change_mode() -> None:
            try:
                self.controller.set_capture_mode(CaptureMode.HOLD_TO_TALK)
            except BaseException as exc:
                mode_errors.append(exc)

        starter = threading.Thread(target=start)
        changer = threading.Thread(target=change_mode)
        starter.start()
        self.assertTrue(entered.wait(1))
        changer.start()
        changer.join(0.05)
        self.assertTrue(changer.is_alive())
        release.set()
        starter.join(1)
        changer.join(1)

        self.assertEqual(start_errors, [])
        self.assertEqual(len(mode_errors), 1)
        self.assertIsInstance(mode_errors[0], RuntimeError)
        self.assertEqual(self.controller.capture_mode, CaptureMode.PUSH_TOGGLE)
        self.controller.dispatch(CompanionIntent(IntentKind.CANCEL))

    def test_low_confidence_text_is_retained_only_in_review_boundary(self) -> None:
        self.transcription[:] = ["uncertain private words", 0.1]
        self.controller.dispatch(CompanionIntent(IntentKind.START_RECORDING))
        self.microphone.emit(b"audio")
        self.controller.dispatch(CompanionIntent(IntentKind.FINISH_RECORDING))
        self.workers.run_next()

        snapshot = self.controller.snapshot
        review = self.controller.pending_review
        self.assertEqual(snapshot.runtime.phase, RuntimePhase.AWAITING_CONFIRMATION)
        self.assertNotIn("private", snapshot.detail)
        assert review is not None
        self.assertEqual(review.text, "uncertain private words")

        self.controller.confirm_review("edited private words")
        self.workers.run_next()
        self.assertEqual(self.injector.deliveries[-1][0], "edited private words")
        self.assertEqual(
            self.controller.snapshot.runtime.phase, RuntimePhase.WAITING_FOR_CLAUDE
        )

    def test_direct_surfaces_require_explicit_focus_authority(self) -> None:
        with self.assertRaises(ValueError):
            self.controller.dispatch(CompanionIntent(IntentKind.OPEN_SETTINGS))
        for kind in (
            IntentKind.OPEN_SETTINGS,
            IntentKind.OPEN_VOICE,
            IntentKind.OPEN_DIAGNOSTICS,
        ):
            self.controller.dispatch(CompanionIntent(kind, allow_focus=True))
        self.assertEqual(self.surface_calls, ["settings", "voice", "diagnostics"])

    def test_durable_reply_can_arrive_after_restart_idle_and_muting_skips_audio(self) -> None:
        event = ReplyEvent.create(
            session="session-1", event_id="reply-1", answer="Unicode reply ✅"
        )
        self.assertTrue(self.controller.receive_reply(event))
        self.assertEqual(self.speech.events, [event])
        self.assertEqual(self.controller.snapshot.runtime.phase, RuntimePhase.SPEAKING)
        self.controller.speech_finished()
        self.assertEqual(self.controller.snapshot.runtime.phase, RuntimePhase.IDLE)

        self.controller.dispatch(CompanionIntent(IntentKind.TOGGLE_OUTPUT_MUTE))
        muted = ReplyEvent.create(
            session="session-2", event_id="reply-2", answer="still visible"
        )
        self.assertTrue(self.controller.receive_reply(muted))
        self.assertEqual(self.speech.events, [event, muted])
        self.assertTrue(self.speech.muted)
        self.assertEqual(self.controller.snapshot.runtime.phase, RuntimePhase.IDLE)

    def test_muting_active_reply_finishes_semantic_speech_phase(self) -> None:
        event = ReplyEvent.create(
            session="session-mute",
            event_id="reply-mute",
            answer="long reply that is currently speaking",
        )
        self.assertTrue(self.controller.receive_reply(event))
        self.assertEqual(
            self.controller.snapshot.runtime.phase,
            RuntimePhase.SPEAKING,
        )

        muted = self.controller.dispatch(
            CompanionIntent(IntentKind.TOGGLE_OUTPUT_MUTE)
        )

        self.assertEqual(muted.runtime.phase, RuntimePhase.IDLE)
        self.assertTrue(muted.output_muted)
        self.assertEqual(muted.detail, "Spoken output muted")
        self.assertEqual(self.speech.stops, 1)
        self.assertEqual(self.persisted_mute, [True])

    def test_new_recording_interrupts_active_speech_before_microphone_start(self) -> None:
        event = ReplyEvent.create(
            session="session-1", event_id="reply-1", answer="long answer"
        )
        self.assertTrue(self.controller.receive_reply(event))
        self.controller.dispatch(CompanionIntent(IntentKind.START_RECORDING))
        self.assertEqual(self.speech.interrupts, 1)
        self.assertEqual(self.microphone.started, 1)
        self.assertEqual(self.controller.snapshot.runtime.phase, RuntimePhase.RECORDING)

    def test_local_control_routes_to_speech_without_terminal_effect(self) -> None:
        event = ReplyEvent.create(
            session="session-1",
            event_id="reply-control",
            answer="A reply with private topic wording.",
        )
        self.assertTrue(self.controller.receive_reply(event))
        self.speech.control_outcome = SpeechControlOutcome(True, True)
        self.transcription[:] = ["where were you", 0.99]

        self.controller.dispatch(CompanionIntent(IntentKind.START_RECORDING))
        self.microphone.emit(b"audio")
        self.controller.dispatch(CompanionIntent(IntentKind.FINISH_RECORDING))
        self.workers.run_next()

        self.assertEqual(self.speech.interrupts, 1)
        self.assertEqual(len(self.speech.controls), 1)
        self.assertIs(self.speech.controls[0].control, Control.WHERE)
        self.assertEqual(self.injector.deliveries, [])
        self.assertIsNone(self.controller.pending_review)
        snapshot = self.controller.snapshot
        self.assertEqual(snapshot.runtime.phase, RuntimePhase.SPEAKING)
        self.assertNotIn("where", snapshot.detail.casefold())
        diagnostics = (self.root / "diagnostics.json").read_text(encoding="utf-8")
        self.assertNotIn("where were you", diagnostics.casefold())
        self.assertIn('"control_code": "where"', diagnostics)

    def test_inflight_control_exclusively_owns_local_runtime_effects(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.speech.control_entered = entered
        self.speech.control_gate = release
        self.speech.control_outcome = SpeechControlOutcome(True, True)
        self.transcription[:] = ["where were you", 0.99]
        self.controller.dispatch(CompanionIntent(IntentKind.START_RECORDING))
        self.microphone.emit(b"audio")
        self.controller.dispatch(CompanionIntent(IntentKind.FINISH_RECORDING))

        worker = threading.Thread(target=self.workers.run_next)
        worker.start()
        self.assertTrue(entered.wait(1))

        incoming = ReplyEvent.create(
            session="session-race",
            event_id="reply-race",
            answer="durable reply retries after the local control",
        )
        self.assertFalse(self.controller.receive_reply(incoming))
        with self.assertRaisesRegex(RuntimeError, "finish is already in progress"):
            self.controller.dispatch(CompanionIntent(IntentKind.START_RECORDING))
        self.assertEqual(self.controller.snapshot.runtime.phase, RuntimePhase.IDLE)
        self.assertEqual(self.speech.events, [])

        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.controller.snapshot.runtime.phase, RuntimePhase.SPEAKING)
        self.assertEqual(self.microphone.started, 1)

    def test_voice_off_control_mutes_and_persists_without_navigation(self) -> None:
        self.transcription[:] = ["voice off", 0.99]
        self.controller.dispatch(CompanionIntent(IntentKind.START_RECORDING))
        self.microphone.emit(b"audio")
        self.controller.dispatch(CompanionIntent(IntentKind.FINISH_RECORDING))
        self.workers.run_next()

        self.assertTrue(self.controller.snapshot.output_muted)
        self.assertEqual(self.speech.controls, [])
        self.assertEqual(self.speech.stops, 1)
        self.assertEqual(self.persisted_mute, [True])
        self.assertEqual(self.injector.deliveries, [])

    def test_start_background_and_quit_are_idempotent_and_bounded_at_seams(self) -> None:
        self.controller.start_background()
        self.controller.start_background()
        self.assertIsNotNone(self.inbox.reply)
        first = self.controller.dispatch(CompanionIntent(IntentKind.QUIT))
        second = self.controller.dispatch(CompanionIntent(IntentKind.QUIT))

        self.assertEqual(first.runtime.phase, RuntimePhase.IDLE)
        self.assertEqual(second.runtime.phase, RuntimePhase.IDLE)
        self.assertEqual(self.microphone.closed, 1)
        self.assertEqual(self.inbox.stops, 1)
        self.assertEqual(self.speech.shutdowns, 1)


if __name__ == "__main__":
    unittest.main()
