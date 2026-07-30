from __future__ import annotations

import os
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from talktomeclaude.speech.runtime import (
    PersistentSpeechRuntime,
    SpawnSynthesisWorker,
    SubprocessSynthesisWorker,
    SpeechArtifact,
    SpeechFaultCode,
    SpeechRuntimeError,
    SynthesisRequest,
    SynthesisResult,
)


def _fake_spawn_synthesize(text: str, path: Path, selected_voice: str) -> None:
    path.write_bytes(f"{selected_voice}|{text}".encode("utf-8"))


def _fake_spawn_failure(_text: str, path: Path, _selected_voice: str) -> None:
    path.write_bytes(b"partial")
    raise RuntimeError("model failure")


class _Worker:
    def __init__(
        self,
        *,
        wait_forever: bool = False,
        lifecycle_forever: bool = False,
        submit_forever: bool = False,
    ) -> None:
        self.wait_forever = wait_forever
        self.lifecycle_forever = lifecycle_forever
        self.submit_forever = submit_forever
        self.requests: list[tuple[SynthesisRequest, object]] = []
        self.terminated = 0
        self.killed = 0

    def submit(self, request: SynthesisRequest, callback: object) -> None:
        self.requests.append((request, callback))
        if self.submit_forever:
            threading.Event().wait(5)

    def terminate(self) -> None:
        self.terminated += 1
        if self.lifecycle_forever:
            threading.Event().wait(5)

    def kill(self) -> None:
        self.killed += 1
        if self.lifecycle_forever:
            threading.Event().wait(5)

    def wait(self, timeout: float) -> int:
        if self.wait_forever:
            threading.Event().wait(5)
        return 0


class _KillRequiredWorker(_Worker):
    def __init__(self) -> None:
        super().__init__()
        self._killed = threading.Event()

    def kill(self) -> None:
        super().kill()
        self._killed.set()

    def wait(self, timeout: float) -> int:
        if not self._killed.wait(timeout):
            raise TimeoutError
        return 0


class _BlockingWaitWorker(_Worker):
    def __init__(self) -> None:
        super().__init__()
        self.wait_entered = threading.Event()
        self.wait_release = threading.Event()

    def wait(self, timeout: float) -> int:
        self.wait_entered.set()
        if not self.wait_release.wait(timeout):
            raise TimeoutError
        return 0


class PersistentSpeechRuntimeTests(unittest.TestCase):
    def test_persistent_boundary_submits_without_exposing_text_or_switching_voice(
        self,
    ) -> None:
        voices: list[str] = []
        workers: list[_Worker] = []

        def factory(voice: str) -> _Worker:
            voices.append(voice)
            worker = _Worker()
            workers.append(worker)
            return worker

        runtime = PersistentSpeechRuntime("rick", factory)
        request = SynthesisRequest(7, "unit-1", "SECRET full canonical text")
        accepted: list[SynthesisResult] = []
        runtime.submit(request, accepted.append)

        self.assertEqual(voices, ["rick"])
        self.assertEqual(workers[0].requests[0][0], request)
        self.assertNotIn("SECRET", repr(request))
        self.assertEqual(runtime.selected_voice, "rick")
        self.assertEqual(accepted, [])

    def test_graceful_restart_preserves_exact_voice_and_parent_state_identity(self) -> None:
        workers: list[_Worker] = []
        voices: list[str] = []

        def factory(voice: str) -> _Worker:
            voices.append(voice)
            worker = _Worker()
            workers.append(worker)
            return worker

        runtime = PersistentSpeechRuntime(
            "gimli", factory, shutdown_deadline_seconds=0.05
        )
        parent_state = {"cursor": 12, "canonical": object()}

        result = runtime.restart(parent_state)

        self.assertIs(result.parent_state, parent_state)
        self.assertEqual(result.selected_voice, "gimli")
        self.assertEqual(voices, ["gimli", "gimli"])
        self.assertTrue(result.old_worker_reaped)
        self.assertTrue(result.terminate_sent)
        self.assertFalse(result.kill_sent)
        self.assertFalse(result.boundary_replacement_required)
        self.assertEqual(workers[0].terminated, 1)
        self.assertEqual(workers[0].killed, 0)

    def test_voice_switch_stages_exact_replacement_and_reaps_old_worker(self) -> None:
        workers: list[_Worker] = []
        voices: list[str] = []

        def factory(voice: str) -> _Worker:
            voices.append(voice)
            worker = _Worker()
            workers.append(worker)
            return worker

        runtime = PersistentSpeechRuntime("rick", factory)

        result = runtime.switch_voice("gimli", {"cursor": 4})
        runtime.submit(
            SynthesisRequest(1, "unit", "content stays outside diagnostics"),
            lambda _result: None,
        )

        self.assertEqual(voices, ["rick", "gimli"])
        self.assertEqual(runtime.selected_voice, "gimli")
        self.assertEqual(result.selected_voice, "gimli")
        self.assertEqual(result.parent_state, {"cursor": 4})
        self.assertEqual(workers[0].terminated, 1)
        self.assertEqual(len(workers[1].requests), 1)

    def test_voice_switch_creation_failure_keeps_old_worker_and_voice_available(self) -> None:
        workers: list[_Worker] = []
        voices: list[str] = []

        def factory(voice: str) -> _Worker:
            voices.append(voice)
            if voice == "broken":
                raise RuntimeError("private model path")
            worker = _Worker()
            workers.append(worker)
            return worker

        runtime = PersistentSpeechRuntime("rick", factory)

        with self.assertRaisesRegex(
            SpeechRuntimeError, "selected voice worker initialization failed"
        ):
            runtime.switch_voice("broken", None)
        runtime.submit(
            SynthesisRequest(1, "unit", "canonical reply"),
            lambda _result: None,
        )

        self.assertEqual(voices, ["rick", "broken"])
        self.assertEqual(runtime.selected_voice, "rick")
        self.assertEqual(workers[0].terminated, 0)
        self.assertEqual(len(workers[0].requests), 1)

    def test_voice_switch_reap_failure_keeps_old_worker_and_voice_available(self) -> None:
        workers: list[_Worker] = []

        def factory(_voice: str) -> _Worker:
            worker = _Worker(wait_forever=not workers)
            workers.append(worker)
            return worker

        runtime = PersistentSpeechRuntime(
            "rick",
            factory,
            shutdown_deadline_seconds=0.01,
        )

        with self.assertRaisesRegex(
            SpeechRuntimeError, "speech worker could not be reaped"
        ):
            runtime.switch_voice("gimli", None)
        runtime.submit(
            SynthesisRequest(1, "unit", "canonical reply"),
            lambda _result: None,
        )

        self.assertEqual(runtime.selected_voice, "rick")
        self.assertEqual(len(workers[0].requests), 1)
        self.assertGreaterEqual(workers[1].terminated, 1)

    def test_voice_switch_to_current_voice_is_a_noop(self) -> None:
        worker = _Worker()
        voices: list[str] = []

        def factory(voice: str) -> _Worker:
            voices.append(voice)
            return worker

        runtime = PersistentSpeechRuntime("rick", factory)

        result = runtime.switch_voice("rick", object())

        self.assertEqual(voices, ["rick"])
        self.assertEqual(worker.terminated, 0)
        self.assertEqual(result.selected_voice, "rick")

    def test_restart_reserves_time_to_kill_reap_and_replace_worker(self) -> None:
        workers: list[_Worker] = []
        voices: list[str] = []

        def factory(voice: str) -> _Worker:
            voices.append(voice)
            worker: _Worker = _KillRequiredWorker() if not workers else _Worker()
            workers.append(worker)
            return worker

        runtime = PersistentSpeechRuntime(
            "rick", factory, shutdown_deadline_seconds=0.2
        )
        result = runtime.restart(object())

        self.assertTrue(result.old_worker_reaped)
        self.assertTrue(result.kill_sent)
        self.assertEqual(workers[0].killed, 1)
        self.assertEqual(voices, ["rick", "rick"])

    def test_shutdown_reserves_time_for_forced_kill_and_final_reap(self) -> None:
        worker = _KillRequiredWorker()
        runtime = PersistentSpeechRuntime(
            "rick", lambda _voice: worker, shutdown_deadline_seconds=0.1
        )
        started = time.monotonic()

        self.assertTrue(runtime.shutdown())

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(worker.killed, 1)

    def test_shutdown_prevents_late_boundary_reset_from_creating_worker(self) -> None:
        workers: list[_Worker] = []
        worker = _BlockingWaitWorker()

        def factory(_voice: str) -> _Worker:
            created = worker if not workers else _Worker()
            workers.append(created)
            return created

        runtime = PersistentSpeechRuntime(
            "rick", factory, shutdown_deadline_seconds=0.2
        )
        shutdown_result: list[bool] = []
        shutdown = threading.Thread(
            target=lambda: shutdown_result.append(runtime.shutdown())
        )
        shutdown.start()
        self.assertTrue(worker.wait_entered.wait(1))

        reset_result: list[bool] = []
        reset = threading.Thread(
            target=lambda: reset_result.append(runtime.reset_synthesis_boundary())
        )
        reset.start()
        worker.wait_release.set()
        shutdown.join(1)
        reset.join(1)

        self.assertEqual(shutdown_result, [True])
        self.assertEqual(reset_result, [False])
        self.assertEqual(workers, [worker])

    def test_noncooperative_worker_is_bounded_unavailable_and_never_replaced(self) -> None:
        workers: list[_Worker] = []
        voices: list[str] = []

        def factory(voice: str) -> _Worker:
            voices.append(voice)
            worker = _Worker(
                wait_forever=not workers,
                lifecycle_forever=not workers,
            )
            workers.append(worker)
            return worker

        runtime = PersistentSpeechRuntime(
            "rick", factory, shutdown_deadline_seconds=0.01
        )
        parent_state = object()
        started = time.monotonic()

        with self.assertRaises(SpeechRuntimeError):
            runtime.restart(parent_state)

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(voices, ["rick"])
        self.assertEqual(workers[0].terminated, 1)
        with self.assertRaises(SpeechRuntimeError):
            runtime.submit(
                SynthesisRequest(0, "unit", "parent remains external"),
                lambda _result: None,
            )
        self.assertIsNotNone(parent_state)

    def test_restart_failure_is_content_free_and_never_tries_fallback_voice(self) -> None:
        voices: list[str] = []
        calls = 0

        def factory(voice: str) -> _Worker:
            nonlocal calls
            calls += 1
            voices.append(voice)
            if calls == 2:
                raise RuntimeError("SECRET voice model path")
            return _Worker()

        runtime = PersistentSpeechRuntime("rick", factory)

        with self.assertRaises(SpeechRuntimeError) as raised:
            runtime.restart({"text": "SECRET answer"})

        self.assertEqual(runtime.selected_voice, "rick")
        self.assertEqual(voices, ["rick", "rick"])
        self.assertNotIn("SECRET", str(raised.exception))

    def test_noncooperative_restart_factory_is_bounded_and_old_worker_not_reused(
        self,
    ) -> None:
        voices: list[str] = []
        release = threading.Event()

        def factory(voice: str) -> _Worker:
            voices.append(voice)
            if len(voices) == 2:
                release.wait(2)
            return _Worker()

        runtime = PersistentSpeechRuntime(
            "rick", factory, shutdown_deadline_seconds=0.01
        )
        started = time.monotonic()

        with self.assertRaises(SpeechRuntimeError):
            runtime.restart(object())

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(runtime.selected_voice, "rick")
        with self.assertRaises(SpeechRuntimeError):
            runtime.submit(SynthesisRequest(0, "unit", "text"), lambda _result: None)
        self.assertEqual(voices, ["rick", "rick"])
        release.set()

    def test_artifact_discard_is_idempotent_and_cleanup_failure_is_contained(self) -> None:
        calls: list[object] = []

        def discard(payload: object) -> None:
            calls.append(payload)
            raise RuntimeError("cleanup")

        payload = object()
        artifact = SpeechArtifact(
            generation=3, unit_id="unit", payload=payload, discard=discard
        )

        self.assertTrue(artifact.discard())
        self.assertFalse(artifact.discard())
        self.assertEqual(calls, [payload])
        self.assertNotIn(str(payload), repr(artifact))

    def test_initial_factory_and_submission_each_fail_closed_within_budget(self) -> None:
        release = threading.Event()

        def stuck_factory(_voice: str) -> _Worker:
            release.wait(2)
            return _Worker()

        started = time.monotonic()
        with self.assertRaises(SpeechRuntimeError):
            PersistentSpeechRuntime(
                "rick", stuck_factory, shutdown_deadline_seconds=0.01
            )
        self.assertLess(time.monotonic() - started, 0.2)
        release.set()

        worker = _Worker(submit_forever=True)
        runtime = PersistentSpeechRuntime(
            "rick", lambda _voice: worker, shutdown_deadline_seconds=0.01
        )
        started = time.monotonic()
        with self.assertRaises(SpeechRuntimeError):
            runtime.submit(
                SynthesisRequest(0, "unit", "full text"), lambda _result: None
            )
        self.assertLess(time.monotonic() - started, 0.2)
        with self.assertRaises(SpeechRuntimeError):
            runtime.submit(
                SynthesisRequest(0, "later", "later"), lambda _result: None
            )

    def test_spawn_worker_synthesizes_with_fixed_voice_and_cleans_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = SpawnSynthesisWorker(
                "rick",
                artifact_root=directory,
                synthesize_fn=_fake_spawn_synthesize,
            )
            request = SynthesisRequest(4, "spawn-unit", "model-free Unicode café")
            results: list[SynthesisResult] = []
            ready = threading.Event()

            def accept(result: SynthesisResult) -> None:
                results.append(result)
                ready.set()

            worker.submit(request, accept)
            self.assertTrue(ready.wait(10))
            self.assertEqual(len(results), 1)
            artifact = results[0].artifact
            assert artifact is not None
            path = artifact.payload
            self.assertIsInstance(path, Path)
            assert isinstance(path, Path)
            self.assertEqual(path.read_bytes(), "rick|model-free Unicode café".encode())
            self.assertFalse(tuple(Path(directory).glob("*.tmp.wav")))
            artifact.discard()
            self.assertFalse(path.exists())
            worker.terminate()
            worker.wait(5)

    def test_spawn_worker_rejects_non_positive_startup_deadline(self) -> None:
        with self.assertRaisesRegex(ValueError, "startup deadline"):
            SpawnSynthesisWorker("rick", startup_deadline_seconds=0)

    def test_spawn_startup_timeout_reaps_process_and_removes_owned_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owned_root = Path(directory) / "owned-speech-root"
            with mock.patch(
                "talktomeclaude.speech.runtime.tempfile.mkdtemp",
                return_value=str(owned_root),
            ):
                with self.assertRaisesRegex(SpeechRuntimeError, "did not become ready"):
                    SpawnSynthesisWorker(
                        "rick",
                        synthesize_fn=_fake_spawn_synthesize,
                        startup_deadline_seconds=0.000001,
                    )
            self.assertFalse(owned_root.exists())

    def test_spawn_worker_failure_removes_partial_temp_and_returns_content_free_fault(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = SpawnSynthesisWorker(
                "gimli",
                artifact_root=directory,
                synthesize_fn=_fake_spawn_failure,
            )
            request = SynthesisRequest(1, "failed-unit", "SECRET answer")
            results: list[SynthesisResult] = []
            ready = threading.Event()

            def accept(result: SynthesisResult) -> None:
                results.append(result)
                ready.set()

            worker.submit(request, accept)
            self.assertTrue(ready.wait(10))
            self.assertEqual(results[0].fault, SpeechFaultCode.SYNTHESIS_FAILED)
            self.assertNotIn("SECRET", repr(results[0]))
            self.assertEqual(tuple(Path(directory).iterdir()), ())
            worker.terminate()
            worker.wait(5)

    @unittest.skipUnless(os.name == "nt", "Windows named-pipe worker")
    def test_subprocess_worker_synthesizes_and_cleans_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = SubprocessSynthesisWorker(
                "rick",
                artifact_root=directory,
                worker_module="tests.synthesis_subprocess_fixture",
            )
            outside = Path(directory).parent / "outside.wav"
            self.assertIsNone(worker._owned_reply_path("job", str(outside)))
            self.assertEqual(
                worker._owned_reply_path("job", str(Path(directory) / "job.wav")),
                (Path(directory) / "job.wav").resolve(),
            )
            request = SynthesisRequest(7, "pipe-unit", "Unicode café — 漢字 🚀")
            results: list[SynthesisResult] = []
            ready = threading.Event()
            worker.submit(request, lambda result: (results.append(result), ready.set()))

            self.assertTrue(ready.wait(10))
            artifact = results[0].artifact
            assert artifact is not None
            path = artifact.payload
            self.assertIsInstance(path, Path)
            assert isinstance(path, Path)
            self.assertEqual(
                path.read_bytes(), "rick|Unicode café — 漢字 🚀".encode()
            )
            artifact.discard()
            worker.terminate()
            self.assertEqual(worker.wait(5), 0)
            self.assertEqual(tuple(Path(directory).iterdir()), ())

    @unittest.skipUnless(os.name == "nt", "Windows named-pipe worker")
    def test_subprocess_death_fails_pending_callback_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = SubprocessSynthesisWorker(
                "rick",
                artifact_root=directory,
                worker_module="tests.synthesis_subprocess_fixture",
            )
            request = SynthesisRequest(8, "blocked-unit", "BLOCK SECRET answer")
            results: list[SynthesisResult] = []
            ready = threading.Event()
            worker.submit(request, lambda result: (results.append(result), ready.set()))
            time.sleep(0.1)
            worker.kill()

            self.assertTrue(ready.wait(5))
            self.assertEqual(results[0].fault, SpeechFaultCode.SYNTHESIS_FAILED)
            self.assertNotIn("SECRET", repr(results[0]))
            worker.wait(5)

    @unittest.skipUnless(os.name == "nt", "Windows named-pipe worker")
    def test_subprocess_immediate_wait_does_not_drop_pending_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = SubprocessSynthesisWorker(
                "rick",
                artifact_root=directory,
                worker_module="tests.synthesis_subprocess_fixture",
            )
            request = SynthesisRequest(9, "immediate-wait", "BLOCK SECRET answer")
            results: list[SynthesisResult] = []
            worker.submit(request, results.append)

            worker.kill()
            worker.wait(5)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].fault, SpeechFaultCode.SYNTHESIS_FAILED)
            self.assertNotIn("SECRET", repr(results[0]))


if __name__ == "__main__":
    unittest.main()
