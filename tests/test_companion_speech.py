from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from talktomeclaude.companion.speech import CompanionSpeech, _MuteGate
from talktomeclaude.reply import ReplyEvent
from talktomeclaude.speech import (
    CanonicalSpeechController,
    Control,
    ControlCommand,
    OralSessionStore,
    OralStatus,
)


class _Pipeline:
    def __init__(self) -> None:
        self.offers: list[tuple[str, str, str | None]] = []
        self.queued: list[tuple[str, str, str | None]] = []
        self.stops = 0

    def offer(
        self, unit_id: str, text: str, *, effect_id: str | None = None
    ) -> bool:
        offered = (unit_id, text, effect_id)
        self.offers.append(offered)
        self.queued.append(offered)
        return True

    def stop(self) -> object:
        self.stops += 1
        self.queued.clear()
        return SimpleNamespace(silence_confirmed=True)

    def complete(self, speech: CompanionSpeech, unit_id: str) -> bool:
        self.queued[:] = [item for item in self.queued if item[0] != unit_id]
        return speech._unit_completed(unit_id)


class _Runtime:
    def __init__(self) -> None:
        self.shutdowns = 0
        self.selected_voice = "rick"
        self.switched: list[str] = []
        self.raise_on_switch = False

    def switch_voice(self, selected_voice: str, parent_state: object = None) -> object:
        self.switched.append(selected_voice)
        if self.raise_on_switch:
            raise RuntimeError("voice worker failed")
        self.selected_voice = selected_voice
        return SimpleNamespace(selected_voice=selected_voice, parent_state=parent_state)

    def shutdown(self) -> bool:
        self.shutdowns += 1
        return True


class CompanionSpeechTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "oral.json"
        self.pipeline = _Pipeline()
        self.gate = _MuteGate(self.pipeline, muted=False)
        self.session = OralSessionStore(self.path)
        self.controller = CanonicalSpeechController(self.session, self.gate)
        self.runtime = _Runtime()
        self.finished = 0

        def finish() -> None:
            self.finished += 1

        self.speech = CompanionSpeech(
            self.controller,
            self.session,
            self.gate,
            self.runtime,
            on_answer_finished=finish,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def event(identifier: str = "reply-1") -> ReplyEvent:
        return ReplyEvent.create(
            session="session", event_id=identifier, answer="A short complete reply."
        )

    @staticmethod
    def complex_event(identifier: str = "reply-complex") -> ReplyEvent:
        return ReplyEvent.create(
            session="session",
            event_id=identifier,
            answer=(
                "# Outcome\nThe result is ready.\n\n"
                "# Safety\nKeep the recovery copy.\n\n"
                "# Next\nRun the verification command."
            ),
        )

    def test_muted_answer_freezes_without_audio_admission(self) -> None:
        self.speech.set_muted(True)
        event = self.event()
        self.speech.accept(event)

        state = self.session.restore(event.event_id)
        self.assertIsNotNone(state)
        self.assertEqual(self.pipeline.offers, [])
        self.assertTrue(self.speech.muted)

    def test_unmute_does_not_auto_resume_muted_answer(self) -> None:
        self.speech.set_muted(True)
        self.speech.accept(self.event())
        self.speech.set_muted(False)
        self.assertEqual(self.pipeline.offers, [])

    def test_live_voice_switch_stops_current_answer_and_updates_runtime(self) -> None:
        event = self.event()
        self.speech.accept(event)

        self.speech.set_voice("gimli")

        self.assertEqual(self.runtime.switched, ["gimli"])
        self.assertEqual(self.runtime.selected_voice, "gimli")
        self.assertEqual(self.pipeline.queued, [])
        state = self.session.restore(event.event_id)
        assert state is not None
        self.assertEqual(state.status, OralStatus.PARKED)

    def test_selecting_current_live_voice_does_not_interrupt_speech(self) -> None:
        self.speech.accept(self.event())

        self.speech.set_voice("rick")

        self.assertEqual(self.runtime.switched, [])
        self.assertEqual(self.pipeline.stops, 0)

    def test_failed_live_voice_switch_parks_current_answer_and_old_voice_can_resume(
        self,
    ) -> None:
        event = self.event()
        self.speech.accept(event)
        self.runtime.raise_on_switch = True

        with self.assertRaisesRegex(RuntimeError, "voice worker failed"):
            self.speech.set_voice("gimli")

        state = self.session.restore(event.event_id)
        assert state is not None
        self.assertEqual(state.status, OralStatus.PARKED)
        self.assertEqual(self.pipeline.queued, [])
        self.assertEqual(self.runtime.selected_voice, "rick")
        self.assertEqual(self.runtime.switched, ["gimli"])

        outcome = self.speech.handle_control(ControlCommand(Control.GO_BACK))

        self.assertTrue(outcome.applied)
        self.assertTrue(outcome.speaking)
        self.assertEqual(len(self.pipeline.queued), 1)
        resumed = self.session.restore(event.event_id)
        assert resumed is not None
        self.assertEqual(resumed.status, OralStatus.ACTIVE)

    def test_completion_persists_then_notifies_whole_answer(self) -> None:
        event = self.event()
        self.speech.accept(event)
        self.assertEqual(len(self.pipeline.offers), 1)
        unit_id = self.pipeline.offers[0][0]

        self.assertTrue(self.pipeline.complete(self.speech, unit_id))

        state = self.session.restore(event.event_id)
        assert state is not None
        self.assertEqual(state.status, OralStatus.COMPLETE)
        self.assertEqual(self.finished, 1)

    def test_interrupt_parks_before_new_turn_and_shutdown_is_idempotent(self) -> None:
        event = self.event()
        self.speech.accept(event)
        self.speech.interrupt()
        state = self.session.restore(event.event_id)
        assert state is not None
        self.assertEqual(state.status, OralStatus.PARKED)

        self.assertTrue(self.speech.shutdown())
        self.assertTrue(self.speech.shutdown())
        self.assertEqual(self.runtime.shutdowns, 1)

    def test_parked_go_back_recaps_before_resuming_answer(self) -> None:
        event = self.event()
        self.speech.accept(event)
        original_unit = self.pipeline.queued[0][0]
        self.speech.interrupt()

        outcome = self.speech.handle_control(ControlCommand(Control.GO_BACK))

        self.assertTrue(outcome.applied)
        self.assertTrue(outcome.speaking)
        self.assertEqual(len(self.pipeline.queued), 1)
        recap_unit = self.pipeline.queued[0][0]
        self.assertTrue(recap_unit.startswith("control-response-"))
        self.assertNotEqual(recap_unit, original_unit)

        self.assertTrue(self.pipeline.complete(self.speech, recap_unit))
        self.assertEqual([item[0] for item in self.pipeline.queued], [original_unit])
        self.assertEqual(self.finished, 0)

    def test_go_back_skips_the_answer_parked_by_the_control_recording(self) -> None:
        first = self.event("reply-first")
        second = self.event("reply-second")
        self.speech.accept(first)
        self.speech.interrupt()
        self.speech.accept(second)
        self.speech.interrupt()

        outcome = self.speech.handle_control(ControlCommand(Control.GO_BACK))

        self.assertTrue(outcome.applied)
        self.assertTrue(outcome.speaking)
        self.assertEqual(self.session.active_answer_id(), first.event_id)
        first_state = self.session.restore(first.event_id)
        second_state = self.session.restore(second.event_id)
        assert first_state is not None
        assert second_state is not None
        self.assertEqual(first_state.status, OralStatus.ACTIVE)
        self.assertEqual(second_state.status, OralStatus.PARKED)
        self.assertEqual(len(self.pipeline.queued), 1)
        self.assertTrue(self.pipeline.queued[0][0].startswith("control-response-"))

    def test_parked_topics_speaks_response_without_auto_resuming_answer(self) -> None:
        event = self.event()
        self.speech.accept(event)
        original_unit = self.pipeline.queued[0][0]
        self.speech.interrupt()

        outcome = self.speech.handle_control(ControlCommand(Control.TOPICS))

        self.assertTrue(outcome.applied)
        self.assertTrue(outcome.speaking)
        self.assertEqual(len(self.pipeline.queued), 1)
        response_unit = self.pipeline.queued[0][0]
        self.assertTrue(response_unit.startswith("control-response-"))
        self.assertNotEqual(response_unit, original_unit)

        self.assertTrue(self.pipeline.complete(self.speech, response_unit))
        self.assertEqual(self.pipeline.queued, [])
        state = self.session.restore(event.event_id)
        assert state is not None
        self.assertEqual(state.status, OralStatus.PAUSED)
        self.assertEqual(self.finished, 1)

    def test_muted_control_does_not_change_durable_navigation(self) -> None:
        event = self.event()
        self.speech.accept(event)
        self.speech.interrupt()
        self.speech.set_muted(True)
        before = self.path.read_bytes()

        outcome = self.speech.handle_control(ControlCommand(Control.GO_BACK))

        self.assertFalse(outcome.applied)
        self.assertFalse(outcome.speaking)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.pipeline.queued, [])

    def test_parked_jump_preserves_the_parsed_frozen_topic_target(self) -> None:
        event = self.complex_event()
        self.speech.accept(event)
        self.speech.interrupt()

        outcome = self.speech.handle_control(
            ControlCommand(Control.JUMP, "next")
        )

        self.assertTrue(outcome.applied)
        self.assertTrue(outcome.speaking)
        state = self.session.restore(event.event_id)
        assert state is not None
        assert state.current_unit is not None
        assert state.current_unit.topic_id is not None
        self.assertEqual(
            state.roadmap.topic(state.current_unit.topic_id).label.casefold(),
            "next",
        )

    def test_invalid_parked_jump_restores_fail_closed_parked_state(self) -> None:
        event = self.complex_event()
        self.speech.accept(event)
        self.speech.interrupt()

        outcome = self.speech.handle_control(
            ControlCommand(Control.JUMP, "missing topic")
        )

        self.assertFalse(outcome.applied)
        self.assertFalse(outcome.speaking)
        state = self.session.restore(event.event_id)
        assert state is not None
        self.assertEqual(state.status, OralStatus.PARKED)
        self.assertEqual(self.pipeline.queued, [])


if __name__ == "__main__":
    unittest.main()
