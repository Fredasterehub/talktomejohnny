"""Bounded, generation-safe synthesis and playback pipeline."""

from __future__ import annotations

import threading
import importlib
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from talktomeclaude.core.deadlines import DEFAULT_DEADLINES, DeadlineName

from .runtime import (
    SpeechArtifact,
    SpeechDiagnostic,
    SpeechDiagnosticCode,
    SpeechFaultCode,
    SynthesisRequest,
    SynthesisResult,
)


class PlaybackOutcome(str, Enum):
    COMPLETED = "completed"
    DEVICE_LOST = "device_lost"
    FAILED = "failed"


class Cancellable(Protocol):
    def cancel(self) -> None: ...


class PlaybackDevice(Protocol):
    """Non-blocking device start plus immediate best-effort abort."""

    def start(
        self,
        artifact: SpeechArtifact,
        complete: Callable[[PlaybackOutcome], None],
    ) -> None: ...

    def abort(self) -> bool: ...


class SynthesisRuntime(Protocol):
    @property
    def selected_voice(self) -> str: ...

    def submit(
        self,
        request: SynthesisRequest,
        callback: Callable[[SynthesisResult], None],
    ) -> None: ...

    def reset_synthesis_boundary(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class SpeechRetry:
    generation: int
    unit_id: str
    text: str = field(repr=False)
    selected_voice: str = field(repr=False)
    fault: SpeechFaultCode


@dataclass(frozen=True, slots=True)
class PipelineSnapshot:
    generation: int
    cursor: int
    queue_depth: int
    playing: bool
    synthesis_in_flight: bool
    ready: bool
    pending: bool
    retry_count: int
    fault: SpeechFaultCode | None


@dataclass(frozen=True, slots=True)
class StopResult:
    generation: int
    drained: tuple["StoppedUnit", ...] = field(repr=False)
    device_abort_requested: bool
    device_silence_confirmed: bool
    fault: SpeechFaultCode | None = None
    queue_depth: int = 0


@dataclass(frozen=True, slots=True)
class StoppedUnit:
    generation: int
    unit_id: str
    text: str = field(repr=False)


@dataclass(slots=True)
class _Inflight:
    request: SynthesisRequest
    token: object
    timer: Cancellable | None = None


@dataclass(frozen=True, slots=True)
class _Ready:
    request: SynthesisRequest
    artifact: SpeechArtifact = field(repr=False)


@dataclass(slots=True)
class _Playing:
    request: SynthesisRequest
    artifact: SpeechArtifact = field(repr=False)
    token: object = field(repr=False)
    start_timer: Cancellable | None = field(default=None, repr=False)
    invalidated: bool = field(default=False, repr=False)
    completed: bool = field(default=False, repr=False)


def _schedule_timer(
    seconds: float, callback: Callable[[], None]
) -> Cancellable:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.start()
    return timer


def _bounded_abort(playback: PlaybackDevice, deadline_seconds: float) -> bool:
    finished = threading.Event()
    confirmed = [False]

    def abort() -> None:
        try:
            confirmed[0] = playback.abort() is True
        except Exception:
            confirmed[0] = False
        finally:
            finished.set()

    threading.Thread(
        target=abort,
        name="ttc-speech-playback-abort",
        daemon=True,
    ).start()
    return finished.wait(deadline_seconds) and confirmed[0]


def _bounded_action_callback(
    callback: Callable[[], bool | None], deadline_seconds: float
) -> bool:
    finished = threading.Event()
    succeeded = [False]

    def run() -> None:
        try:
            accepted = callback()
        except Exception:
            pass
        else:
            succeeded[0] = accepted is not False
        finally:
            finished.set()

    threading.Thread(
        target=run,
        name="ttc-speech-unit-completed",
        daemon=True,
    ).start()
    return finished.wait(deadline_seconds) and succeeded[0]


class SoundDevicePlayback:
    """Dedicated OutputStream playback that never stops global audio state."""

    def __init__(
        self,
        *,
        sounddevice_module: Any | None = None,
        numpy_module: Any | None = None,
        abort_deadline_seconds: float | None = None,
        volume: int = 100,
    ) -> None:
        deadline = (
            DEFAULT_DEADLINES[DeadlineName.PLAYBACK_STOP].seconds
            if abort_deadline_seconds is None
            else abort_deadline_seconds
        )
        if deadline <= 0:
            raise ValueError("playback abort deadline must be positive")
        if type(volume) is not int or not 0 <= volume <= 100:
            raise ValueError("output volume must be an integer between 0 and 100")
        self._sounddevice = sounddevice_module
        self._numpy = numpy_module
        self._abort_deadline = deadline
        self._lock = threading.Lock()
        self._next_token = 0
        self._active_token: int | None = None
        self._stream: Any | None = None
        self._volume = volume / 100
        self._silence = threading.Event()
        self._silence.set()

    @property
    def silence_confirmed(self) -> bool:
        return self._silence.is_set()

    def start(
        self,
        artifact: SpeechArtifact,
        complete: Callable[[PlaybackOutcome], None],
    ) -> None:
        if not isinstance(artifact.payload, Path):
            raise ValueError("playback artifact is not a local path")
        with self._lock:
            if self._active_token is not None or not self._silence.is_set():
                raise RuntimeError("playback device is already active")
            self._next_token += 1
            token = self._next_token
            self._active_token = token
            self._silence.clear()
        threading.Thread(
            target=self._play,
            args=(token, artifact.payload, complete),
            name="ttc-dedicated-audio-output",
            daemon=True,
        ).start()

    def _play(
        self,
        token: int,
        path: Path,
        complete: Callable[[PlaybackOutcome], None],
    ) -> None:
        stream: Any | None = None
        outcome = PlaybackOutcome.COMPLETED
        notify = True
        try:
            with wave.open(str(path), "rb") as handle:
                frames = handle.readframes(handle.getnframes())
                channels = handle.getnchannels()
                sample_rate = handle.getframerate()
            numpy = self._numpy
            sounddevice = self._sounddevice
            if numpy is None:
                import numpy as numpy_module

                numpy = numpy_module
            if sounddevice is None:
                sounddevice = importlib.import_module("sounddevice")
            self._numpy = numpy
            samples = numpy.frombuffer(frames, dtype=numpy.int16)
            if channels > 1:
                samples = samples.reshape(-1, channels)
            with self._lock:
                if self._active_token != token:
                    notify = False
                    return
            callback_mode = hasattr(sounddevice, "CallbackStop")
            finished = threading.Event()
            if callback_mode:
                position = [0]

                def render(
                    outdata: Any,
                    frame_count: int,
                    _time_info: object,
                    _status: object,
                ) -> None:
                    start = position[0]
                    end = min(start + frame_count, len(samples))
                    count = end - start
                    outdata.fill(0)
                    chunk = samples[start:end]
                    if chunk.size:
                        chunk = self._scaled(chunk, numpy)
                    if channels == 1:
                        outdata[:count, 0] = chunk
                    else:
                        outdata[:count, :] = chunk
                    position[0] = end
                    if end >= len(samples):
                        raise sounddevice.CallbackStop

                stream = sounddevice.OutputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype="int16",
                    callback=render,
                    finished_callback=finished.set,
                )
            else:
                stream = sounddevice.OutputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype="int16",
                )
            with self._lock:
                if self._active_token != token:
                    notify = False
                    return
                self._stream = stream
            stream.start()
            with self._lock:
                if self._active_token != token:
                    notify = False
                    try:
                        stream.abort()
                    except Exception:
                        pass
                    return
            if callback_mode:
                while not finished.wait(0.02):
                    with self._lock:
                        if self._active_token != token:
                            notify = False
                            break
                if notify:
                    stream.stop()
            else:
                stream.write(self._scaled(samples, numpy))
                stream.stop()
        except Exception:
            outcome = PlaybackOutcome.DEVICE_LOST
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    outcome = PlaybackOutcome.DEVICE_LOST
            with self._lock:
                if self._stream is stream:
                    self._stream = None
                if self._active_token == token:
                    self._active_token = None
                else:
                    notify = False
                self._silence.set()
            if notify:
                try:
                    complete(outcome)
                except Exception:
                    pass

    def set_volume(self, volume: int) -> None:
        """Update playback gain for future and in-flight output."""

        if type(volume) is not int or not 0 <= volume <= 100:
            raise ValueError("output volume must be an integer between 0 and 100")
        with self._lock:
            self._volume = volume / 100

    def _scaled(self, samples, numpy):
        with self._lock:
            volume = self._volume
        if volume == 1.0:
            return samples
        scaled = samples.astype(numpy.float32) * volume
        return numpy.clip(scaled, -32768, 32767).astype(samples.dtype)

    def abort(self) -> bool:
        started = time.monotonic()
        with self._lock:
            self._active_token = None
            stream = self._stream
        if stream is not None:
            completed = threading.Event()

            def abort_stream() -> None:
                try:
                    stream.abort()
                except Exception:
                    pass
                finally:
                    completed.set()

            threading.Thread(
                target=abort_stream,
                name="ttc-dedicated-audio-abort",
                daemon=True,
            ).start()
            remaining = max(0.0, self._abort_deadline - (time.monotonic() - started))
            if completed.wait(remaining):
                # PortAudio's dedicated stream abort returning is the device
                # silence boundary.  The writer thread may unwind later on
                # Windows, but it no longer owns an active output stream.
                self._silence.set()
        remaining = max(0.0, self._abort_deadline - (time.monotonic() - started))
        return self._silence.wait(remaining)


class SpeechPipeline:
    """Keep at most one playing, one synthesizing, and one queued unit."""

    MAX_QUEUE_DEPTH = 3

    def __init__(
        self,
        runtime: SynthesisRuntime,
        playback: PlaybackDevice,
        *,
        synthesis_deadline_seconds: float | None = None,
        schedule: Callable[[float, Callable[[], None]], Cancellable] = _schedule_timer,
        on_diagnostic: Callable[[SpeechDiagnostic], None] | None = None,
        on_unit_completed: Callable[[str], bool | None] | None = None,
        playback_stop_deadline_seconds: float | None = None,
        max_retries: int = 32,
        max_effect_ids: int = 1024,
    ) -> None:
        deadline = (
            DEFAULT_DEADLINES[DeadlineName.SYNTHESIS].seconds
            if synthesis_deadline_seconds is None
            else synthesis_deadline_seconds
        )
        if deadline <= 0:
            raise ValueError("synthesis deadline must be positive")
        playback_deadline = (
            DEFAULT_DEADLINES[DeadlineName.PLAYBACK_STOP].seconds
            if playback_stop_deadline_seconds is None
            else playback_stop_deadline_seconds
        )
        if playback_deadline <= 0:
            raise ValueError("playback stop deadline must be positive")
        if max_retries < 1:
            raise ValueError("retry capacity must be positive")
        if max_effect_ids < 1:
            raise ValueError("effect identity capacity must be positive")
        self._runtime = runtime
        self._playback = playback
        self._deadline = deadline
        self._schedule = schedule
        self._on_diagnostic = on_diagnostic
        self._on_unit_completed = on_unit_completed
        self._playback_stop_deadline = playback_deadline
        self._max_retries = max_retries
        self._max_effect_ids = max_effect_ids
        self._lock = threading.RLock()
        self._completion_gate = threading.Lock()
        self._generation = 0
        self._cursor = 0
        self._inflight: _Inflight | None = None
        self._ready: _Ready | None = None
        self._playing: _Playing | None = None
        self._pending: SynthesisRequest | None = None
        self._retries: list[SpeechRetry] = []
        self._stopping = False
        self._fault: SpeechFaultCode | None = None
        self._admitted_effect_ids: dict[str, None] = {}

    @property
    def selected_voice(self) -> str:
        return self._runtime.selected_voice

    @property
    def retries(self) -> tuple[SpeechRetry, ...]:
        with self._lock:
            return tuple(self._retries)

    @property
    def snapshot(self) -> PipelineSnapshot:
        with self._lock:
            return PipelineSnapshot(
                generation=self._generation,
                cursor=self._cursor,
                queue_depth=self._depth_locked(),
                playing=self._playing is not None,
                synthesis_in_flight=self._inflight is not None,
                ready=self._ready is not None,
                pending=self._pending is not None,
                retry_count=len(self._retries),
                fault=self._fault,
            )

    def _depth_locked(self) -> int:
        return sum(
            item is not None
            for item in (
                self._playing,
                self._inflight,
                self._ready,
                self._pending,
            )
        )

    def _emit_locked(
        self,
        code: SpeechDiagnosticCode,
        fault: SpeechFaultCode | None = None,
    ) -> None:
        if self._on_diagnostic is None:
            return
        diagnostic = SpeechDiagnostic(
            code,
            generation=self._generation,
            queue_depth=self._depth_locked(),
            fault=fault,
        )
        try:
            self._on_diagnostic(diagnostic)
        except Exception:
            pass

    def offer(
        self,
        unit_id: str,
        text: str,
        *,
        effect_id: str | None = None,
    ) -> bool:
        """Offer one unit without allowing an unbounded hidden text queue."""

        with self._lock:
            if effect_id is not None and effect_id in self._admitted_effect_ids:
                return True
            if (
                self._stopping
                or self._depth_locked() >= self.MAX_QUEUE_DEPTH
                or self._pending is not None
            ):
                return False
            request = SynthesisRequest(self._generation, unit_id, text)
            self._pending = request
            if effect_id is not None:
                self._admitted_effect_ids[effect_id] = None
                while len(self._admitted_effect_ids) > self._max_effect_ids:
                    oldest = next(iter(self._admitted_effect_ids))
                    self._admitted_effect_ids.pop(oldest)
            self._pump_locked()
            return True

    def retry(self, retry: SpeechRetry) -> bool:
        """Retry with the same selected voice, rebased to the current generation."""

        with self._lock:
            if self._stopping:
                return False
            if retry.selected_voice != self.selected_voice:
                return False
            if retry not in self._retries:
                return False
            if self._inflight is not None or self._ready is not None or self._playing is not None:
                return False
            self._retries.remove(retry)
            self._fault = None
            request = SynthesisRequest(
                self._generation, retry.unit_id, retry.text
            )
            self._start_synthesis_locked(request)
            return True

    def recover(self) -> bool:
        with self._lock:
            if self._stopping or self._fault is None:
                return False
            self._fault = None
            self._pump_locked()
            return True

    def _pump_locked(self) -> None:
        if self._fault is not None or self._stopping:
            return
        if self._playing is None and self._ready is not None:
            ready = self._ready
            self._ready = None
            self._start_playback_locked(ready)
        # Never start synthesis while a completed artifact occupies the sole
        # ready slot.  This keeps callback arrival unable to grow the queue.
        if (
            self._inflight is None
            and self._pending is not None
            and self._ready is None
        ):
            request = self._pending
            self._pending = None
            self._start_synthesis_locked(request)

    def _start_synthesis_locked(self, request: SynthesisRequest) -> None:
        token = object()
        slot = _Inflight(request, token)
        self._inflight = slot
        try:
            slot.timer = self._schedule(
                self._deadline,
                lambda: self._synthesis_timed_out(token),
            )
        except Exception:
            self._inflight = None
            self._fail_locked(request, SpeechFaultCode.SYNTHESIS_FAILED)
            self._pump_locked()
            return
        if self._inflight is not slot:
            # A deliberately synchronous scheduler may expire during
            # registration.  Do not start already-detached synthesis.
            slot.timer.cancel()
            return
        self._emit_locked(SpeechDiagnosticCode.SYNTHESIS_STARTED)
        try:
            self._runtime.submit(
                request,
                lambda result: self._synthesis_finished(token, result),
            )
        except Exception:
            if self._inflight is slot:
                if slot.timer is not None:
                    slot.timer.cancel()
                self._inflight = None
                self._fail_locked(request, SpeechFaultCode.SYNTHESIS_FAILED)
                self._pump_locked()

    def _synthesis_timed_out(self, token: object) -> None:
        with self._lock:
            slot = self._inflight
            if slot is None or slot.token is not token:
                return
            self._inflight = None
            self._fail_locked(slot.request, SpeechFaultCode.SYNTHESIS_TIMEOUT)
            self._reset_boundary_detached()

    def _synthesis_finished(self, token: object, result: SynthesisResult) -> None:
        with self._lock:
            slot = self._inflight
            if slot is None or slot.token is not token:
                if result.artifact is not None:
                    result.artifact.discard()
                self._emit_locked(
                    SpeechDiagnosticCode.SYNTHESIS_REJECTED,
                    SpeechFaultCode.STALE_ARTIFACT,
                )
                return
            if slot.timer is not None:
                slot.timer.cancel()
            self._inflight = None
            request = slot.request
            if result.request != request or request.generation != self._generation:
                if result.artifact is not None:
                    result.artifact.discard()
                self._fail_locked(request, SpeechFaultCode.STALE_ARTIFACT)
                self._pump_locked()
                return
            if result.fault is not None:
                self._fail_locked(request, result.fault)
                self._pump_locked()
                return
            artifact = result.artifact
            assert artifact is not None
            if (
                artifact.generation != self._generation
                or artifact.unit_id != request.unit_id
                or artifact.discarded
            ):
                artifact.discard()
                self._fail_locked(request, SpeechFaultCode.STALE_ARTIFACT)
                self._pump_locked()
                return
            self._emit_locked(SpeechDiagnosticCode.SYNTHESIS_READY)
            ready = _Ready(request, artifact)
            if self._fault is not None:
                if self._ready is None:
                    self._ready = ready
                else:
                    artifact.discard()
                    self._fail_locked(request, SpeechFaultCode.STALE_ARTIFACT)
            elif self._playing is None:
                self._start_playback_locked(ready)
            elif self._ready is None:
                self._ready = ready
            else:
                # Defensive fail-closed branch; normal scheduling cannot reach
                # it because synthesis is not started with a ready artifact.
                artifact.discard()
                self._fail_locked(request, SpeechFaultCode.STALE_ARTIFACT)
            self._pump_locked()

    def _start_playback_locked(self, ready: _Ready) -> None:
        request = ready.request
        artifact = ready.artifact
        # The second generation check sits immediately beside device start.
        if (
            request.generation != self._generation
            or artifact.generation != self._generation
            or artifact.unit_id != request.unit_id
            or artifact.discarded
        ):
            artifact.discard()
            self._fail_locked(request, SpeechFaultCode.STALE_ARTIFACT)
            return
        token = object()
        playing = _Playing(request, artifact, token)
        self._playing = playing
        try:
            playing.start_timer = self._schedule(
                self._playback_stop_deadline,
                lambda: self._playback_start_timed_out(playing),
            )
        except Exception:
            self._playing = None
            artifact.discard()
            self._fail_locked(request, SpeechFaultCode.PLAYBACK_FAILED)
            return
        threading.Thread(
            target=self._start_playback_external,
            args=(playing,),
            name="ttc-speech-playback-start",
            daemon=True,
        ).start()

    def _start_playback_external(self, playing: _Playing) -> None:
        failed = False
        try:
            self._playback.start(
                playing.artifact,
                lambda outcome: self._playback_finished(playing.token, outcome),
            )
        except Exception:
            failed = True
        must_abort = False
        with self._lock:
            if playing.start_timer is not None:
                playing.start_timer.cancel()
            if self._playing is playing and not playing.invalidated:
                if failed:
                    self._playing = None
                    playing.artifact.discard()
                    self._fail_locked(
                        playing.request, SpeechFaultCode.PLAYBACK_FAILED
                    )
                else:
                    self._emit_locked(SpeechDiagnosticCode.PLAYBACK_STARTED)
            elif playing.invalidated and not playing.completed:
                must_abort = True
        if must_abort:
            _bounded_abort(self._playback, self._playback_stop_deadline)

    def _playback_start_timed_out(self, playing: _Playing) -> None:
        with self._lock:
            if self._playing is not playing:
                return
            playing.invalidated = True
            self._playing = None
            playing.artifact.discard()
            self._fail_locked(
                playing.request, SpeechFaultCode.PLAYBACK_STOP_TIMEOUT
            )
        threading.Thread(
            target=_bounded_abort,
            args=(self._playback, self._playback_stop_deadline),
            name="ttc-speech-late-start-abort",
            daemon=True,
        ).start()

    def _playback_finished(
        self, token: object, outcome: PlaybackOutcome
    ) -> None:
        with self._completion_gate:
            with self._lock:
                playing = self._playing
                if playing is None or playing.token is not token:
                    return
                playing.completed = True
                if playing.start_timer is not None:
                    playing.start_timer.cancel()
                self._playing = None
                playing.artifact.discard()
                if (
                    playing.request.generation != self._generation
                    or outcome is not PlaybackOutcome.COMPLETED
                ):
                    fault = (
                        SpeechFaultCode.DEVICE_LOST
                        if outcome is PlaybackOutcome.DEVICE_LOST
                        else SpeechFaultCode.PLAYBACK_FAILED
                    )
                    self._fail_locked(playing.request, fault)
                    return
                self._cursor += 1
                generation = self._generation
                callback = self._on_unit_completed
                unit_id = playing.request.unit_id
            callback_failed = False
            if callback is not None:
                callback_failed = not _bounded_action_callback(
                    lambda: callback(unit_id), self._playback_stop_deadline
                )
            with self._lock:
                if generation != self._generation:
                    return
                if callback_failed:
                    self._fail_locked(
                        playing.request, SpeechFaultCode.PLAYBACK_FAILED
                    )
                    return
                self._emit_locked(SpeechDiagnosticCode.PLAYBACK_FINISHED)
                self._pump_locked()

    def _fail_locked(
        self, request: SynthesisRequest, fault: SpeechFaultCode
    ) -> SpeechRetry:
        retry = SpeechRetry(
            generation=request.generation,
            unit_id=request.unit_id,
            text=request.text,
            selected_voice=self.selected_voice,
            fault=fault,
        )
        self._fault = fault
        self._retries.append(retry)
        if len(self._retries) > self._max_retries:
            del self._retries[: len(self._retries) - self._max_retries]
        self._emit_locked(SpeechDiagnosticCode.RETRY_AVAILABLE, fault)
        return retry

    def _reset_boundary_detached(self) -> None:
        def reset() -> None:
            try:
                self._runtime.reset_synthesis_boundary()
            except Exception:
                pass

        threading.Thread(
            target=reset,
            name="ttc-speech-boundary-reset",
            daemon=True,
        ).start()

    def stop(self) -> StopResult:
        """Invalidate callbacks, abort playback, and detach synthesis immediately."""

        completion_owned = self._completion_gate.acquire(
            timeout=self._playback_stop_deadline
        )
        with self._lock:
            self._stopping = True
            self._generation += 1
            self._fault = None
            self._retries.clear()
            requests: list[SynthesisRequest] = []
            artifacts: list[SpeechArtifact] = []
            if self._playing is not None:
                self._playing.invalidated = True
                if self._playing.start_timer is not None:
                    self._playing.start_timer.cancel()
                requests.append(self._playing.request)
                artifacts.append(self._playing.artifact)
            if self._ready is not None:
                requests.append(self._ready.request)
                artifacts.append(self._ready.artifact)
            if self._inflight is not None:
                requests.append(self._inflight.request)
                if self._inflight.timer is not None:
                    self._inflight.timer.cancel()
            had_active_synthesis = self._inflight is not None
            if self._pending is not None:
                requests.append(self._pending)
            self._playing = None
            self._ready = None
            self._inflight = None
            self._pending = None
            for artifact in artifacts:
                artifact.discard()
            drained = tuple(
                StoppedUnit(request.generation, request.unit_id, request.text)
                for request in requests
            )
            stopped_generation = self._generation
        if completion_owned:
            self._completion_gate.release()
        # abort() may synchronously issue a late completion callback.  All
        # authority was already detached, so it cannot mutate state.  Do not
        # retain the pipeline lock while waiting for device confirmation.
        confirmed = _bounded_abort(
            self._playback, self._playback_stop_deadline
        )
        if had_active_synthesis:
            self._reset_boundary_detached()
        with self._lock:
            self._stopping = False
            fault = None if confirmed else SpeechFaultCode.PLAYBACK_STOP_TIMEOUT
            self._emit_locked(SpeechDiagnosticCode.PLAYBACK_ABORTED, fault)
            self._emit_locked(SpeechDiagnosticCode.GENERATION_STOPPED)
            return StopResult(
                generation=stopped_generation,
                drained=drained,
                device_abort_requested=True,
                device_silence_confirmed=confirmed,
                fault=fault,
            )
