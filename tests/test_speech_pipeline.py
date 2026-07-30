from __future__ import annotations

import unittest
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

from talktomeclaude.speech.pipeline import (
    PlaybackOutcome,
    SoundDevicePlayback,
    SpeechPipeline,
)
from talktomeclaude.speech.runtime import (
    SpeechArtifact,
    SpeechDiagnostic,
    SpeechFaultCode,
    SpeechRuntimeError,
    SynthesisRequest,
    SynthesisResult,
)


class _Timer:
    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


class _Scheduler:
    def __init__(self) -> None:
        self.timers: list[_Timer] = []

    def __call__(self, _seconds: float, callback: Callable[[], None]) -> _Timer:
        timer = _Timer(callback)
        self.timers.append(timer)
        return timer


class _Runtime:
    def __init__(self, selected_voice: str = "rick") -> None:
        self.selected_voice = selected_voice
        self.submissions: list[tuple[SynthesisRequest, Callable[[SynthesisResult], None]]] = []
        self.submit_error = False
        self.reset_calls = 0

    def submit(
        self,
        request: SynthesisRequest,
        callback: Callable[[SynthesisResult], None],
    ) -> None:
        if self.submit_error:
            self.submit_error = False
            raise SpeechRuntimeError("content-free submission failure")
        self.submissions.append((request, callback))

    def reset_synthesis_boundary(self) -> bool:
        self.reset_calls += 1
        return True

    def complete(
        self,
        index: int,
        *,
        artifact: SpeechArtifact | None = None,
        fault: SpeechFaultCode | None = None,
    ) -> SpeechArtifact | None:
        request, callback = self.submissions[index]
        if fault is not None:
            callback(SynthesisResult.failed(request, fault))
            return None
        ready = artifact or SpeechArtifact(
            generation=request.generation,
            unit_id=request.unit_id,
            payload=object(),
        )
        callback(SynthesisResult.ready(request, ready))
        return ready


class _Playback:
    def __init__(self) -> None:
        self.starts: list[
            tuple[SpeechArtifact, Callable[[PlaybackOutcome], None]]
        ] = []
        self.abort_calls = 0
        self.start_error = False
        self.callback_on_abort = False

    def start(
        self,
        artifact: SpeechArtifact,
        complete: Callable[[PlaybackOutcome], None],
    ) -> None:
        if self.start_error:
            self.start_error = False
            raise OSError("SECRET device detail")
        self.starts.append((artifact, complete))

    def abort(self) -> bool:
        self.abort_calls += 1
        if self.callback_on_abort and self.starts:
            self.starts[-1][1](PlaybackOutcome.COMPLETED)
        return True


def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.002)
    if not predicate():
        raise AssertionError("condition was not reached")


class SpeechPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _Runtime()
        self.playback = _Playback()
        self.scheduler = _Scheduler()
        self.diagnostics: list[SpeechDiagnostic] = []
        self.pipeline = SpeechPipeline(
            self.runtime,  # type: ignore[arg-type]
            self.playback,
            synthesis_deadline_seconds=1.0,
            schedule=self.scheduler,
            on_diagnostic=self.diagnostics.append,
            playback_stop_deadline_seconds=0.05,
        )

    def test_n_plus_one_overlap_and_depth_never_exceed_three(self) -> None:
        self.assertTrue(self.pipeline.offer("unit-1", "first"))
        self.assertTrue(self.pipeline.offer("unit-2", "second"))
        self.assertFalse(self.pipeline.offer("unit-overflow", "not admitted"))
        self.runtime.complete(0)
        _wait_until(lambda: len(self.playback.starts) == 1)
        self.assertEqual(len(self.playback.starts), 1)
        self.assertEqual(len(self.runtime.submissions), 2)
        self.assertTrue(self.pipeline.offer("unit-3", "third"))
        self.assertEqual(self.pipeline.snapshot.queue_depth, 3)
        self.assertFalse(self.pipeline.offer("unit-4", "fourth"))

        self.runtime.complete(1)
        snapshot = self.pipeline.snapshot
        self.assertTrue(snapshot.playing)
        self.assertTrue(snapshot.ready)
        self.assertTrue(snapshot.pending)
        self.assertEqual(snapshot.queue_depth, 3)
        self.assertEqual(len(self.runtime.submissions), 2)

        self.playback.starts[0][1](PlaybackOutcome.COMPLETED)
        _wait_until(lambda: len(self.playback.starts) == 2)
        snapshot = self.pipeline.snapshot
        self.assertEqual(snapshot.cursor, 1)
        self.assertTrue(snapshot.playing)
        self.assertTrue(snapshot.synthesis_in_flight)
        self.assertEqual(snapshot.queue_depth, 2)
        self.assertEqual(len(self.runtime.submissions), 3)

    def test_effect_identity_makes_preview_admission_idempotent(self) -> None:
        self.assertTrue(
            self.pipeline.offer("preview", "preview text", effect_id="effect-1")
        )
        self.assertTrue(
            self.pipeline.offer("preview", "preview text", effect_id="effect-1")
        )
        self.assertEqual(len(self.runtime.submissions), 1)

    def test_stop_detaches_without_join_and_late_callbacks_are_permanently_stale(
        self,
    ) -> None:
        discarded: list[object] = []
        self.assertTrue(self.pipeline.offer("unit-1", "full text one"))
        first = self.runtime.complete(0)
        assert first is not None
        _wait_until(lambda: len(self.playback.starts) == 1)
        self.assertTrue(self.pipeline.offer("unit-2", "full text two"))
        self.assertTrue(self.pipeline.offer("unit-3", "full text three"))
        late_request, late_callback = self.runtime.submissions[1]
        old_playback_callback = self.playback.starts[0][1]
        self.playback.callback_on_abort = True

        stopped = self.pipeline.stop()

        self.assertEqual(stopped.generation, 1)
        self.assertEqual(stopped.queue_depth, 0)
        self.assertTrue(stopped.device_silence_confirmed)
        self.assertGreaterEqual(self.playback.abort_calls, 1)
        self.assertTrue(first.discarded)
        self.assertEqual(
            {unit.text for unit in stopped.drained},
            {"full text one", "full text two", "full text three"},
        )
        self.assertEqual(self.pipeline.retries, ())

        payload = object()
        stale = SpeechArtifact(
            generation=late_request.generation,
            unit_id=late_request.unit_id,
            payload=payload,
            discard=discarded.append,
        )
        result = SynthesisResult.ready(late_request, stale)
        late_callback(result)
        late_callback(result)
        old_playback_callback(PlaybackOutcome.COMPLETED)
        old_playback_callback(PlaybackOutcome.COMPLETED)

        self.assertTrue(stale.discarded)
        self.assertEqual(discarded, [payload])
        self.assertEqual(len(self.playback.starts), 1)
        self.assertEqual(self.pipeline.snapshot.cursor, 0)
        self.assertEqual(self.pipeline.snapshot.queue_depth, 0)
        _wait_until(lambda: self.runtime.reset_calls >= 1)

    def test_wrong_generation_artifact_is_rejected_before_device_start(self) -> None:
        text = "complete visible answer remains"
        self.pipeline.offer("unit", text)
        request, callback = self.runtime.submissions[0]
        stale = SpeechArtifact(
            generation=request.generation + 1,
            unit_id=request.unit_id,
            payload=object(),
        )

        callback(SynthesisResult.ready(request, stale))

        self.assertTrue(stale.discarded)
        self.assertEqual(self.playback.starts, [])
        self.assertEqual(self.pipeline.retries[-1].text, text)
        self.assertEqual(
            self.pipeline.retries[-1].fault, SpeechFaultCode.STALE_ARTIFACT
        )

    def test_synthesis_timeout_detaches_noncooperative_result_and_keeps_retry(self) -> None:
        text = "timeout must preserve all of this"
        self.pipeline.offer("unit", text)
        request, callback = self.runtime.submissions[0]

        self.scheduler.timers[0].fire()

        self.assertEqual(self.pipeline.retries[-1].text, text)
        self.assertEqual(
            self.pipeline.retries[-1].fault, SpeechFaultCode.SYNTHESIS_TIMEOUT
        )
        stale = SpeechArtifact(
            generation=request.generation,
            unit_id=request.unit_id,
            payload=object(),
        )
        callback(SynthesisResult.ready(request, stale))
        self.assertTrue(stale.discarded)
        self.assertEqual(self.playback.starts, [])

    def test_fault_matrix_preserves_full_text_retry_and_never_fallback_voice(self) -> None:
        cases = (
            "submit",
            "synthesis",
            "playback-start",
            "device-loss",
        )
        for case in cases:
            with self.subTest(case=case):
                runtime = _Runtime("rick")
                playback = _Playback()
                scheduler = _Scheduler()
                pipeline = SpeechPipeline(
                    runtime,  # type: ignore[arg-type]
                    playback,
                    synthesis_deadline_seconds=1.0,
                    schedule=scheduler,
                    playback_stop_deadline_seconds=0.05,
                )
                text = f"FULL TEXT {case} must remain visible"
                if case == "submit":
                    runtime.submit_error = True
                if case == "playback-start":
                    playback.start_error = True
                self.assertTrue(pipeline.offer("unit", text))
                if case == "synthesis":
                    runtime.complete(0, fault=SpeechFaultCode.SYNTHESIS_FAILED)
                elif case in ("playback-start", "device-loss"):
                    runtime.complete(0)
                    if case == "device-loss":
                        _wait_until(lambda: bool(playback.starts))
                        playback.starts[0][1](PlaybackOutcome.DEVICE_LOST)
                    else:
                        _wait_until(lambda: bool(pipeline.retries))

                retry = pipeline.retries[-1]
                self.assertEqual(retry.text, text)
                self.assertEqual(retry.selected_voice, "rick")
                self.assertEqual(pipeline.selected_voice, "rick")
                self.assertNotIn(text, repr(retry))

    def test_retry_reuses_selected_voice_but_stop_permanently_invalidates_it(self) -> None:
        self.pipeline.offer("unit", "retry exact text")
        self.runtime.complete(0, fault=SpeechFaultCode.SYNTHESIS_FAILED)
        retry = self.pipeline.retries[0]

        self.assertTrue(self.pipeline.retry(retry))

        request = self.runtime.submissions[-1][0]
        self.assertEqual(request.generation, 0)
        self.assertEqual(request.text, "retry exact text")
        self.assertEqual(self.pipeline.selected_voice, "rick")
        stopped_retry = self.pipeline.retries[0] if self.pipeline.retries else None
        self.pipeline.stop()
        if stopped_retry is not None:
            self.assertFalse(self.pipeline.retry(stopped_retry))

    def test_diagnostics_and_snapshots_are_content_free(self) -> None:
        secret_text = "SECRET ANSWER BODY"
        self.pipeline.offer("opaque-unit", secret_text)
        self.runtime.complete(0, fault=SpeechFaultCode.SYNTHESIS_FAILED)

        rendered = repr((self.diagnostics, self.pipeline.snapshot))
        self.assertNotIn(secret_text, rendered)
        self.assertNotIn("rick", rendered)
        self.assertNotIn("opaque-unit", rendered)
        self.assertTrue(self.diagnostics)

    def test_stuck_abort_is_watchdog_bounded_and_reports_unconfirmed_silence(self) -> None:
        class StuckPlayback(_Playback):
            def abort(self) -> bool:
                self.abort_calls += 1
                threading.Event().wait(5)
                return False

        playback = StuckPlayback()
        pipeline = SpeechPipeline(
            self.runtime,
            playback,
            synthesis_deadline_seconds=1,
            schedule=self.scheduler,
            playback_stop_deadline_seconds=0.01,
        )
        started = time.monotonic()

        result = pipeline.stop()

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(result.device_silence_confirmed)
        self.assertEqual(result.fault, SpeechFaultCode.PLAYBACK_STOP_TIMEOUT)

    def test_completion_callback_is_exact_outside_lock_and_failure_pauses(self) -> None:
        completed: list[str] = []
        pipeline = SpeechPipeline(
            self.runtime,
            self.playback,
            synthesis_deadline_seconds=1,
            schedule=self.scheduler,
            on_unit_completed=completed.append,
            playback_stop_deadline_seconds=0.05,
        )
        pipeline.offer("unit-good", "spoken")
        self.runtime.complete(0)
        _wait_until(lambda: bool(self.playback.starts))
        self.playback.starts[0][1](PlaybackOutcome.COMPLETED)
        self.assertEqual(completed, ["unit-good"])

        def failing(_unit_id: str) -> None:
            raise OSError("storage")

        runtime = _Runtime()
        playback = _Playback()
        failing_pipeline = SpeechPipeline(
            runtime,
            playback,
            synthesis_deadline_seconds=1,
            schedule=_Scheduler(),
            on_unit_completed=failing,
            playback_stop_deadline_seconds=0.05,
        )
        failing_pipeline.offer("unit-fail", "full text")
        runtime.complete(0)
        _wait_until(lambda: bool(playback.starts))
        playback.starts[0][1](PlaybackOutcome.COMPLETED)
        self.assertEqual(failing_pipeline.snapshot.cursor, 1)
        self.assertEqual(
            failing_pipeline.snapshot.fault, SpeechFaultCode.PLAYBACK_FAILED
        )
        self.assertEqual(failing_pipeline.retries[-1].text, "full text")

        false_runtime = _Runtime()
        false_playback = _Playback()
        false_pipeline = SpeechPipeline(
            false_runtime,
            false_playback,
            synthesis_deadline_seconds=1,
            schedule=_Scheduler(),
            on_unit_completed=lambda _unit_id: False,
            playback_stop_deadline_seconds=0.05,
        )
        false_pipeline.offer("unit-false", "full false text")
        false_runtime.complete(0)
        _wait_until(lambda: bool(false_playback.starts))
        false_playback.starts[0][1](PlaybackOutcome.COMPLETED)
        self.assertEqual(
            false_pipeline.snapshot.fault, SpeechFaultCode.PLAYBACK_FAILED
        )
        self.assertEqual(false_pipeline.retries[-1].text, "full false text")

    def test_timeout_and_stop_reset_synthesis_boundary_and_stopped_is_not_retryable(self) -> None:
        self.pipeline.offer("timeout", "timeout text")
        self.scheduler.timers[0].fire()
        _wait_until(lambda: self.runtime.reset_calls == 1)
        retry = self.pipeline.retries[-1]
        stopped = self.pipeline.stop()
        self.assertEqual(self.pipeline.retries, ())
        self.assertFalse(self.pipeline.retry(retry))
        self.assertTrue(all(isinstance(unit.text, str) for unit in stopped.drained))

    def test_device_fault_pauses_ready_and_later_units_until_explicit_recovery(self) -> None:
        self.pipeline.offer("unit-1", "one")
        self.runtime.complete(0)
        _wait_until(lambda: len(self.playback.starts) == 1)
        self.pipeline.offer("unit-2", "two")
        self.pipeline.offer("unit-3", "three")
        self.playback.starts[0][1](PlaybackOutcome.DEVICE_LOST)
        self.runtime.complete(1)

        self.assertEqual(self.pipeline.snapshot.fault, SpeechFaultCode.DEVICE_LOST)
        self.assertTrue(self.pipeline.snapshot.ready)
        self.assertEqual(len(self.playback.starts), 1)
        self.assertEqual(len(self.runtime.submissions), 2)

        self.assertTrue(self.pipeline.recover())
        _wait_until(lambda: len(self.playback.starts) == 2)
        self.assertEqual(self.playback.starts[1][0].unit_id, "unit-2")
        self.assertEqual(self.runtime.submissions[-1][0].unit_id, "unit-3")

    def test_completion_callback_never_fires_for_device_fault_or_stop(self) -> None:
        completed: list[str] = []
        pipeline = SpeechPipeline(
            self.runtime,
            self.playback,
            synthesis_deadline_seconds=1,
            schedule=self.scheduler,
            on_unit_completed=completed.append,
            playback_stop_deadline_seconds=0.05,
        )
        pipeline.offer("fault", "fault text")
        self.runtime.complete(0)
        _wait_until(lambda: bool(self.playback.starts))
        self.playback.starts[0][1](PlaybackOutcome.DEVICE_LOST)
        self.assertEqual(completed, [])

        runtime = _Runtime()
        playback = _Playback()
        stopped = SpeechPipeline(
            runtime,
            playback,
            synthesis_deadline_seconds=1,
            schedule=_Scheduler(),
            on_unit_completed=completed.append,
            playback_stop_deadline_seconds=0.05,
        )
        stopped.offer("stopped", "stop text")
        runtime.complete(0)
        _wait_until(lambda: bool(playback.starts))
        callback = playback.starts[0][1]
        stopped.stop()
        callback(PlaybackOutcome.COMPLETED)
        self.assertEqual(completed, [])

    def test_retry_storage_is_bounded(self) -> None:
        runtime = _Runtime()
        pipeline = SpeechPipeline(
            runtime,
            _Playback(),
            synthesis_deadline_seconds=1,
            schedule=_Scheduler(),
            max_retries=2,
        )
        for index in range(3):
            pipeline.offer(f"unit-{index}", f"text-{index}")
            runtime.complete(index, fault=SpeechFaultCode.SYNTHESIS_FAILED)
            pipeline.recover()

        self.assertEqual(len(pipeline.retries), 2)
        self.assertEqual(
            [retry.text for retry in pipeline.retries], ["text-1", "text-2"]
        )


class SoundDevicePlaybackTests(unittest.TestCase):
    def test_dedicated_stream_aborts_without_global_sounddevice_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x00\x00" * 16)
            write_started = threading.Event()
            release = threading.Event()

            class Samples:
                def reshape(self, *_args: object) -> "Samples":
                    return self

            class Numpy:
                int16 = "int16"

                @staticmethod
                def frombuffer(_frames: bytes, dtype: object) -> Samples:
                    self.assertEqual(dtype, "int16")
                    return Samples()

            class Stream:
                def start(self) -> None:
                    pass

                def write(self, _samples: object) -> None:
                    write_started.set()
                    release.wait(2)

                def stop(self) -> None:
                    pass

                def abort(self) -> None:
                    release.set()

                def close(self) -> None:
                    pass

            class SoundDevice:
                stop = staticmethod(lambda: (_ for _ in ()).throw(AssertionError("global stop")))
                OutputStream = staticmethod(lambda **_kwargs: Stream())

            playback = SoundDevicePlayback(
                sounddevice_module=SoundDevice(),
                numpy_module=Numpy(),
                abort_deadline_seconds=0.1,
            )
            artifact = SpeechArtifact(
                generation=0, unit_id="unit", payload=path
            )
            callbacks: list[PlaybackOutcome] = []
            playback.start(artifact, callbacks.append)
            self.assertTrue(write_started.wait(1))

            self.assertTrue(playback.abort())

            self.assertTrue(playback.silence_confirmed)
            self.assertEqual(callbacks, [])

    def test_abort_confirmation_does_not_wait_for_writer_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x00\x00" * 16)
            write_started = threading.Event()
            release_writer = threading.Event()

            class Numpy:
                int16 = "int16"

                @staticmethod
                def frombuffer(_frames: bytes, dtype: object) -> object:
                    return object()

            class Stream:
                def start(self) -> None:
                    pass

                def write(self, _samples: object) -> None:
                    write_started.set()
                    release_writer.wait(2)

                def stop(self) -> None:
                    pass

                def abort(self) -> None:
                    pass

                def close(self) -> None:
                    release_writer.set()

            class SoundDevice:
                OutputStream = staticmethod(lambda **_kwargs: Stream())

            playback = SoundDevicePlayback(
                sounddevice_module=SoundDevice(),
                numpy_module=Numpy(),
                abort_deadline_seconds=0.1,
            )
            playback.start(
                SpeechArtifact(generation=0, unit_id="unit", payload=path),
                lambda _outcome: None,
            )
            self.assertTrue(write_started.wait(1))
            started = time.monotonic()

            self.assertTrue(playback.abort())

            self.assertLess(time.monotonic() - started, 0.1)
            self.assertTrue(playback.silence_confirmed)
            release_writer.set()

    def test_volume_scales_playback_samples_without_touching_device_state(self) -> None:
        import numpy as np

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(np.array([20000, -20000], dtype=np.int16).tobytes())

            written = threading.Event()
            captured: list[np.ndarray] = []

            class Stream:
                def start(self) -> None:
                    pass

                def write(self, samples: object) -> None:
                    captured.append(np.array(samples, copy=True))
                    written.set()

                def stop(self) -> None:
                    pass

                def abort(self) -> None:
                    pass

                def close(self) -> None:
                    pass

            class SoundDevice:
                OutputStream = staticmethod(lambda **_kwargs: Stream())

            playback = SoundDevicePlayback(
                sounddevice_module=SoundDevice(),
                numpy_module=np,
                volume=50,
            )
            playback.start(
                SpeechArtifact(generation=0, unit_id="unit", payload=path),
                lambda _outcome: None,
            )

            self.assertTrue(written.wait(1))
            self.assertEqual(captured[0].tolist(), [10000, -10000])
            self.assertTrue(playback.silence_confirmed)

    def test_callback_playback_uses_thread_safe_live_volume_updates(self) -> None:
        import numpy as np

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(
                    np.array([20000, -20000, 16000, -16000], dtype=np.int16).tobytes()
                )

            first_rendered = threading.Event()
            continue_rendering = threading.Event()
            finished = threading.Event()
            captured: list[np.ndarray] = []

            class CallbackStop(Exception):
                pass

            class Stream:
                def __init__(self, *, callback, finished_callback, **_kwargs) -> None:
                    self.callback = callback
                    self.finished_callback = finished_callback

                def start(self) -> None:
                    first = np.zeros((2, 1), dtype=np.int16)
                    self.callback(first, 2, None, None)
                    captured.append(first.copy())
                    first_rendered.set()
                    continue_rendering.wait(1)
                    second = np.zeros((2, 1), dtype=np.int16)
                    try:
                        self.callback(second, 2, None, None)
                    except CallbackStop:
                        pass
                    captured.append(second.copy())
                    self.finished_callback()

                def stop(self) -> None:
                    pass

                def abort(self) -> None:
                    pass

                def close(self) -> None:
                    pass

            class SoundDevice:
                OutputStream = Stream

            SoundDevice.CallbackStop = CallbackStop
            playback = SoundDevicePlayback(
                sounddevice_module=SoundDevice(),
                numpy_module=np,
                volume=100,
            )
            playback.start(
                SpeechArtifact(generation=0, unit_id="unit", payload=path),
                lambda _outcome: finished.set(),
            )
            self.assertTrue(first_rendered.wait(1))

            playback.set_volume(50)
            continue_rendering.set()

            self.assertTrue(finished.wait(1))
            self.assertEqual(captured[0].reshape(-1).tolist(), [20000, -20000])
            self.assertEqual(captured[1].reshape(-1).tolist(), [8000, -8000])

    def test_output_volume_validation_is_strict(self) -> None:
        for invalid in (-1, 101, 2.5, True, "50"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                SoundDevicePlayback(volume=invalid)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
