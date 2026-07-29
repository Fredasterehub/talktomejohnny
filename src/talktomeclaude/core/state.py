"""Pure companion lifecycle reducer."""

from __future__ import annotations

from dataclasses import replace

from .contracts import (
    EventKind,
    RuntimeEvent,
    RuntimePhase,
    RuntimeState,
    TransitionCode,
    TransitionResult,
)


_TRANSITIONS: dict[tuple[RuntimePhase, EventKind], RuntimePhase] = {
    (RuntimePhase.IDLE, EventKind.START_RECORDING): RuntimePhase.RECORDING,
    # A durable Stop-hook reply can arrive for a turn typed directly in the
    # operator's terminal, or be recovered from the inbox after restart.
    (RuntimePhase.IDLE, EventKind.REPLY_RECEIVED): RuntimePhase.PLANNING,
    (RuntimePhase.RECORDING, EventKind.FINISH_RECORDING): RuntimePhase.TRANSCRIBING,
    (
        RuntimePhase.TRANSCRIBING,
        EventKind.TRANSCRIPT_ACCEPTED,
    ): RuntimePhase.DELIVERING,
    (
        RuntimePhase.TRANSCRIBING,
        EventKind.TRANSCRIPT_REVIEW_REQUIRED,
    ): RuntimePhase.AWAITING_CONFIRMATION,
    (
        RuntimePhase.AWAITING_CONFIRMATION,
        EventKind.CONFIRM_TRANSCRIPT,
    ): RuntimePhase.DELIVERING,
    (
        RuntimePhase.DELIVERING,
        EventKind.DELIVERY_SUCCEEDED,
    ): RuntimePhase.WAITING_FOR_CLAUDE,
    (
        RuntimePhase.DELIVERING,
        EventKind.DICTATION_DELIVERED,
    ): RuntimePhase.IDLE,
    (
        RuntimePhase.WAITING_FOR_CLAUDE,
        EventKind.REPLY_RECEIVED,
    ): RuntimePhase.PLANNING,
    (RuntimePhase.PLANNING, EventKind.PLAN_READY): RuntimePhase.SPEAKING,
    (RuntimePhase.SPEAKING, EventKind.PAUSE_SPEECH): RuntimePhase.PAUSED,
    (RuntimePhase.PAUSED, EventKind.RESUME_SPEECH): RuntimePhase.SPEAKING,
    (RuntimePhase.SPEAKING, EventKind.SPEECH_FINISHED): RuntimePhase.IDLE,
    (RuntimePhase.PAUSED, EventKind.SPEECH_FINISHED): RuntimePhase.IDLE,
    (
        RuntimePhase.WAITING_FOR_CLAUDE,
        EventKind.TRANSPORT_DISCONNECTED,
    ): RuntimePhase.DISCONNECTED,
    (
        RuntimePhase.DISCONNECTED,
        EventKind.TRANSPORT_RECONNECTED,
    ): RuntimePhase.WAITING_FOR_CLAUDE,
    (RuntimePhase.RECOVERABLE_ERROR, EventKind.RETRY): RuntimePhase.IDLE,
}

_INTERRUPTIBLE_FOR_RECORDING = {
    RuntimePhase.IDLE,
    RuntimePhase.AWAITING_CONFIRMATION,
    RuntimePhase.WAITING_FOR_CLAUDE,
    RuntimePhase.PLANNING,
    RuntimePhase.SPEAKING,
    RuntimePhase.PAUSED,
    RuntimePhase.RECOVERABLE_ERROR,
}


def _result(
    state: RuntimeState,
    event: RuntimeEvent,
    *,
    accepted: bool,
    code: TransitionCode,
    current: RuntimeState | None = None,
) -> TransitionResult:
    return TransitionResult(
        accepted=accepted,
        code=code,
        previous=state,
        current=current or state,
        event=event,
    )


def reduce_state(state: RuntimeState, event: RuntimeEvent) -> TransitionResult:
    """Apply an event without side effects and always return an explicit result."""

    if event.generation is not None and event.generation != state.generation:
        return _result(
            state,
            event,
            accepted=False,
            code=TransitionCode.STALE_GENERATION,
        )

    if event.kind is EventKind.STOP_REQUESTED:
        if state.phase is RuntimePhase.STOPPING:
            return _result(
                state,
                event,
                accepted=True,
                code=TransitionCode.APPLIED,
            )
        current = RuntimeState(
            phase=RuntimePhase.STOPPING,
            generation=state.generation + 1,
        )
        return _result(
            state,
            event,
            accepted=True,
            code=TransitionCode.APPLIED,
            current=current,
        )

    if event.kind is EventKind.STOPPED:
        if state.phase is not RuntimePhase.STOPPING:
            return _result(
                state,
                event,
                accepted=False,
                code=TransitionCode.ILLEGAL_TRANSITION,
            )
        return _result(
            state,
            event,
            accepted=True,
            code=TransitionCode.APPLIED,
            current=RuntimeState(
                phase=RuntimePhase.IDLE,
                generation=state.generation,
            ),
        )

    if event.kind is EventKind.CANCEL:
        if state.phase is RuntimePhase.STOPPING:
            return _result(
                state,
                event,
                accepted=False,
                code=TransitionCode.ILLEGAL_TRANSITION,
            )
        return _result(
            state,
            event,
            accepted=True,
            code=TransitionCode.APPLIED,
            current=RuntimeState(
                phase=RuntimePhase.IDLE,
                generation=state.generation + 1,
            ),
        )

    if event.kind is EventKind.ERROR_OCCURRED:
        if not event.error_code:
            return _result(
                state,
                event,
                accepted=False,
                code=TransitionCode.INVALID_EVENT,
            )
        recover_to = event.recover_to
        if recover_to is None:
            if state.phase is RuntimePhase.RECOVERABLE_ERROR:
                recover_to = state.resume_phase or RuntimePhase.IDLE
            elif state.phase is RuntimePhase.DISCONNECTED:
                recover_to = RuntimePhase.DISCONNECTED
            elif state.phase is RuntimePhase.STOPPING:
                recover_to = RuntimePhase.IDLE
            else:
                recover_to = state.phase
        if recover_to in {
            RuntimePhase.RECOVERABLE_ERROR,
            RuntimePhase.STOPPING,
        }:
            return _result(
                state,
                event,
                accepted=False,
                code=TransitionCode.INVALID_EVENT,
            )
        current = RuntimeState(
            phase=RuntimePhase.RECOVERABLE_ERROR,
            generation=state.generation,
            resume_phase=recover_to,
            error_code=event.error_code,
        )
        return _result(
            state,
            event,
            accepted=True,
            code=TransitionCode.APPLIED,
            current=current,
        )

    if event.kind is EventKind.RETRY:
        if state.phase is not RuntimePhase.RECOVERABLE_ERROR:
            return _result(
                state,
                event,
                accepted=False,
                code=TransitionCode.ILLEGAL_TRANSITION,
            )
        retry_phase = state.resume_phase or RuntimePhase.IDLE
        current = RuntimeState(
            phase=retry_phase,
            generation=state.generation,
            resume_phase=(
                RuntimePhase.WAITING_FOR_CLAUDE
                if retry_phase is RuntimePhase.DISCONNECTED
                else None
            ),
        )
        return _result(
            state,
            event,
            accepted=True,
            code=TransitionCode.APPLIED,
            current=current,
        )

    if (
        event.kind is EventKind.START_RECORDING
        and state.phase in _INTERRUPTIBLE_FOR_RECORDING
    ):
        current = RuntimeState(
            phase=RuntimePhase.RECORDING,
            generation=state.generation + 1,
        )
        return _result(
            state,
            event,
            accepted=True,
            code=TransitionCode.APPLIED,
            current=current,
        )

    target = _TRANSITIONS.get((state.phase, event.kind))
    if target is None:
        return _result(
            state,
            event,
            accepted=False,
            code=TransitionCode.ILLEGAL_TRANSITION,
        )

    resume_phase: RuntimePhase | None = None
    if target is RuntimePhase.DISCONNECTED:
        resume_phase = state.phase
    elif state.phase is RuntimePhase.DISCONNECTED:
        target = state.resume_phase or target

    current = replace(
        state,
        phase=target,
        resume_phase=resume_phase,
        error_code=None,
    )
    return _result(
        state,
        event,
        accepted=True,
        code=TransitionCode.APPLIED,
        current=current,
    )


def legal_events(state: RuntimeState) -> frozenset[EventKind]:
    """Return accepted event kinds for diagnostics and controller affordances."""

    candidates = {
        kind
        for phase, kind in _TRANSITIONS
        if phase is state.phase
    }
    candidates.update({EventKind.STOP_REQUESTED})
    candidates.add(EventKind.ERROR_OCCURRED)
    if state.phase is not RuntimePhase.STOPPING:
        candidates.add(EventKind.CANCEL)
    if state.phase in _INTERRUPTIBLE_FOR_RECORDING:
        candidates.add(EventKind.START_RECORDING)
    if state.phase is RuntimePhase.RECOVERABLE_ERROR:
        candidates.add(EventKind.RETRY)
    if state.phase is RuntimePhase.STOPPING:
        candidates.add(EventKind.STOPPED)
    return frozenset(candidates)
