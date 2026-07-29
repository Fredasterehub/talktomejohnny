"""Exhaustive tests for the pure companion lifecycle reducer."""

from __future__ import annotations

import unittest

from talktomeclaude.core import (
    EventKind,
    RuntimeEvent,
    RuntimePhase,
    RuntimeState,
    TransitionCode,
    legal_events,
    reduce_state,
)


def _event(kind: EventKind, generation: int = 7) -> RuntimeEvent:
    if kind is EventKind.ERROR_OCCURRED:
        return RuntimeEvent(kind, generation, error_code="synthetic_failure")
    return RuntimeEvent(kind, generation)


def _state(phase: RuntimePhase, generation: int = 7) -> RuntimeState:
    if phase is RuntimePhase.DISCONNECTED:
        return RuntimeState(
            phase,
            generation,
            resume_phase=RuntimePhase.WAITING_FOR_CLAUDE,
        )
    if phase is RuntimePhase.RECOVERABLE_ERROR:
        return RuntimeState(
            phase,
            generation,
            resume_phase=RuntimePhase.IDLE,
            error_code="synthetic_failure",
        )
    return RuntimeState(phase, generation)


class StateReducerTests(unittest.TestCase):
    def test_happy_path_has_only_explicit_legal_transitions(self) -> None:
        state = RuntimeState()
        path = [
            (EventKind.START_RECORDING, RuntimePhase.RECORDING),
            (EventKind.FINISH_RECORDING, RuntimePhase.TRANSCRIBING),
            (EventKind.TRANSCRIPT_ACCEPTED, RuntimePhase.DELIVERING),
            (EventKind.DELIVERY_SUCCEEDED, RuntimePhase.WAITING_FOR_CLAUDE),
            (EventKind.REPLY_RECEIVED, RuntimePhase.PLANNING),
            (EventKind.PLAN_READY, RuntimePhase.SPEAKING),
            (EventKind.PAUSE_SPEECH, RuntimePhase.PAUSED),
            (EventKind.RESUME_SPEECH, RuntimePhase.SPEAKING),
            (EventKind.SPEECH_FINISHED, RuntimePhase.IDLE),
        ]
        for event_kind, expected in path:
            with self.subTest(event=event_kind):
                result = reduce_state(state, RuntimeEvent(event_kind))
                self.assertTrue(result.accepted)
                self.assertEqual(TransitionCode.APPLIED, result.code)
                self.assertEqual(expected, result.current.phase)
                state = result.current

        self.assertEqual(1, state.generation)

    def test_review_path_is_not_inserted_into_acceptable_transcript_path(self) -> None:
        state = RuntimeState(RuntimePhase.TRANSCRIBING, generation=3)
        result = reduce_state(
            state,
            RuntimeEvent(EventKind.TRANSCRIPT_REVIEW_REQUIRED, generation=3),
        )
        self.assertEqual(RuntimePhase.AWAITING_CONFIRMATION, result.current.phase)
        result = reduce_state(
            result.current,
            RuntimeEvent(EventKind.CONFIRM_TRANSCRIPT, generation=3),
        )
        self.assertEqual(RuntimePhase.DELIVERING, result.current.phase)

    def test_generic_dictation_delivery_returns_directly_to_idle(self) -> None:
        state = RuntimeState(RuntimePhase.DELIVERING, generation=3)

        generic = reduce_state(
            state,
            RuntimeEvent(EventKind.DICTATION_DELIVERED, generation=3),
        )
        assistant = reduce_state(
            state,
            RuntimeEvent(EventKind.DELIVERY_SUCCEEDED, generation=3),
        )

        self.assertTrue(generic.accepted)
        self.assertEqual(RuntimePhase.IDLE, generic.current.phase)
        self.assertTrue(assistant.accepted)
        self.assertEqual(RuntimePhase.WAITING_FOR_CLAUDE, assistant.current.phase)

    def test_every_state_event_pair_returns_an_explicit_result(self) -> None:
        for phase in RuntimePhase:
            state = _state(phase)
            expected_legal = legal_events(state)
            for kind in EventKind:
                with self.subTest(phase=phase, event=kind):
                    result = reduce_state(state, _event(kind))
                    self.assertIs(result.previous, state)
                    self.assertIsInstance(result.accepted, bool)
                    self.assertIn(result.code, TransitionCode)
                    self.assertEqual(kind in expected_legal, result.accepted)
                    if not result.accepted:
                        self.assertIs(result.current, state)
                        self.assertIn(
                            result.code,
                            {
                                TransitionCode.ILLEGAL_TRANSITION,
                                TransitionCode.INVALID_EVENT,
                            },
                        )

    def test_error_requires_code_and_retry_uses_explicit_destination(self) -> None:
        state = RuntimeState(RuntimePhase.DELIVERING, generation=4)
        invalid = reduce_state(state, RuntimeEvent(EventKind.ERROR_OCCURRED))
        self.assertFalse(invalid.accepted)
        self.assertEqual(TransitionCode.INVALID_EVENT, invalid.code)

        failed = reduce_state(
            state,
            RuntimeEvent(
                EventKind.ERROR_OCCURRED,
                error_code="delivery_timeout",
                recover_to=RuntimePhase.AWAITING_CONFIRMATION,
            ),
        )
        self.assertEqual(RuntimePhase.RECOVERABLE_ERROR, failed.current.phase)
        self.assertEqual("delivery_timeout", failed.current.error_code)
        self.assertEqual(
            RuntimePhase.AWAITING_CONFIRMATION, failed.current.resume_phase
        )
        retried = reduce_state(failed.current, RuntimeEvent(EventKind.RETRY))
        self.assertEqual(RuntimePhase.AWAITING_CONFIRMATION, retried.current.phase)
        self.assertIsNone(retried.current.error_code)

    def test_disconnect_remembers_only_an_explicit_resume_phase(self) -> None:
        state = RuntimeState(RuntimePhase.WAITING_FOR_CLAUDE, generation=2)
        disconnected = reduce_state(
            state, RuntimeEvent(EventKind.TRANSPORT_DISCONNECTED)
        )
        self.assertEqual(RuntimePhase.DISCONNECTED, disconnected.current.phase)
        self.assertEqual(
            RuntimePhase.WAITING_FOR_CLAUDE, disconnected.current.resume_phase
        )
        reconnected = reduce_state(
            disconnected.current, RuntimeEvent(EventKind.TRANSPORT_RECONNECTED)
        )
        self.assertEqual(RuntimePhase.WAITING_FOR_CLAUDE, reconnected.current.phase)
        self.assertIsNone(reconnected.current.resume_phase)

    def test_stop_is_bounded_state_intent_and_cancel_invalidates_work(self) -> None:
        for phase in RuntimePhase:
            if phase is RuntimePhase.STOPPING:
                continue
            with self.subTest(phase=phase):
                state = _state(phase, generation=11)
                stopped = reduce_state(state, RuntimeEvent(EventKind.STOP_REQUESTED))
                self.assertTrue(stopped.accepted)
                self.assertEqual(RuntimePhase.STOPPING, stopped.current.phase)
                self.assertEqual(12, stopped.current.generation)
                complete = reduce_state(
                    stopped.current, RuntimeEvent(EventKind.STOPPED)
                )
                self.assertEqual(RuntimePhase.IDLE, complete.current.phase)

                cancelled = reduce_state(state, RuntimeEvent(EventKind.CANCEL))
                self.assertTrue(cancelled.accepted)
                self.assertEqual(RuntimePhase.IDLE, cancelled.current.phase)
                self.assertEqual(12, cancelled.current.generation)

    def test_starting_a_new_turn_interrupts_wait_plan_or_speech_and_advances_generation(
        self,
    ) -> None:
        for phase in (
            RuntimePhase.AWAITING_CONFIRMATION,
            RuntimePhase.WAITING_FOR_CLAUDE,
            RuntimePhase.PLANNING,
            RuntimePhase.SPEAKING,
            RuntimePhase.PAUSED,
        ):
            with self.subTest(phase=phase):
                state = _state(phase, generation=8)
                result = reduce_state(
                    state, RuntimeEvent(EventKind.START_RECORDING)
                )
                self.assertTrue(result.accepted)
                self.assertEqual(RuntimePhase.RECORDING, result.current.phase)
                self.assertEqual(9, result.current.generation)


class StateValidationTests(unittest.TestCase):
    def test_negative_generation_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeState(generation=-1)

    def test_recovery_fields_and_phases_are_consistent(self) -> None:
        invalid = [
            dict(phase=RuntimePhase.IDLE, resume_phase=RuntimePhase.RECORDING),
            dict(phase=RuntimePhase.IDLE, error_code="bad"),
            dict(phase=RuntimePhase.DISCONNECTED),
            dict(phase=RuntimePhase.RECOVERABLE_ERROR),
            dict(
                phase=RuntimePhase.RECOVERABLE_ERROR,
                resume_phase=RuntimePhase.IDLE,
            ),
            dict(
                phase=RuntimePhase.RECOVERABLE_ERROR,
                resume_phase=RuntimePhase.STOPPING,
                error_code="bad",
            ),
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RuntimeState(**kwargs)

    def test_mismatched_event_generation_is_stale_not_illegal(self) -> None:
        state = RuntimeState(RuntimePhase.TRANSCRIBING, generation=5)
        result = reduce_state(
            state,
            RuntimeEvent(EventKind.TRANSCRIPT_ACCEPTED, generation=4),
        )
        self.assertFalse(result.accepted)
        self.assertEqual(TransitionCode.STALE_GENERATION, result.code)
        self.assertIs(state, result.current)


if __name__ == "__main__":
    unittest.main()
