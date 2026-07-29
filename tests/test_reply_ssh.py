from __future__ import annotations

import io
import hashlib
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

from talktomeclaude.core.backoff import BackoffPolicy
from talktomeclaude.reply import ReceiveResult, ReplyEvent, ReplyReceiver
from talktomeclaude.reply.ssh import (
    DurableReceiver,
    IncomingFrame,
    PersistentSSHReplyTransport,
    ProtocolErrorCode,
    SSHConnectionSpec,
    TransportStatus,
    TransportStatusCode,
    canonical_json_bytes,
)


class _Ack:
    def __init__(self, event_id: str, digest: str) -> None:
        self.event_id = event_id
        self.digest = digest


class _ReceiveResult:
    def __init__(self, ack: _Ack | None) -> None:
        self.ack = ack


class _CaptureWriter(io.BytesIO):
    def close(self) -> None:
        # Preserve bytes for assertions after the transport reaps the process.
        pass


class _BlockingWriter:
    def write(self, data: bytes) -> int:
        del data
        threading.Event().wait(5)
        return 0

    def flush(self) -> None:
        threading.Event().wait(5)


class _FakeProcess:
    def __init__(
        self,
        stdout: bytes,
        *,
        wait_times_out_once: bool = False,
        wait_always_times_out: bool = False,
    ) -> None:
        self.stdin = _CaptureWriter()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(b"secret remote stderr must not surface")
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._wait_times_out_once = wait_times_out_once
        self._wait_always_times_out = wait_always_times_out

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_always_times_out:
            raise subprocess.TimeoutExpired("ssh", timeout or 0.0)
        if self._wait_times_out_once:
            self._wait_times_out_once = False
            raise subprocess.TimeoutExpired("ssh", timeout or 0.0)
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


class _Receiver:
    def __init__(
        self,
        callback: Callable[[dict[str, object], int], _ReceiveResult],
    ) -> None:
        self.frames: list[dict[str, object]] = []
        self._callback = callback

    def receive(self, wire_bytes: bytes) -> _ReceiveResult:
        import json

        frame = json.loads(wire_bytes)
        self.frames.append(frame)
        return self._callback(frame, len(self.frames))


def _event(*, version: int = 1, digest: str | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "answer": "Unicode: café 🙂 \u05e9\u05dc\u05d5\u05dd",
        "event_id": "event-001",
        "session": "session-001",
        "version": version,
    }
    return {
        **body,
        "digest": digest
        or hashlib.sha256(str(body["answer"]).encode("utf-8")).hexdigest(),
    }


def _line(document: dict[str, object]) -> bytes:
    return canonical_json_bytes(document) + b"\n"


class SSHConnectionSpecTests(unittest.TestCase):
    def test_builds_batch_mode_persistent_helper_command(self) -> None:
        spec = SSHConnectionSpec(
            remote="user@example-host",
            remote_command=("python3", "-m", "talktomeclaude.reply.remote", "stream"),
            connect_timeout_seconds=1.2,
        )

        argv = spec.argv()

        self.assertEqual(argv[0:3], ["ssh", "-T", "-o"])
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=2", argv)
        self.assertEqual(argv[-2], "user@example-host")
        self.assertEqual(
            argv[-1], "python3 -m talktomeclaude.reply.remote stream"
        )

    def test_rejects_option_or_whitespace_remote_injection(self) -> None:
        for remote in ("-oProxyCommand=bad", "dev@host command", "dev@host\nnext"):
            with self.subTest(remote=remote), self.assertRaises(ValueError):
                SSHConnectionSpec(remote=remote, remote_command=("python3",))


class PersistentSSHReplyTransportTests(unittest.TestCase):
    def _transport(
        self,
        processes: list[_FakeProcess],
        receiver: DurableReceiver,
        stop: threading.Event,
        *,
        statuses: list[TransportStatus] | None = None,
        status_callback: Callable[[TransportStatus], None] | None = None,
        waits: list[float] | None = None,
    ) -> PersistentSSHReplyTransport:
        def popen(_argv, **_kwargs):
            return processes.pop(0)

        def wait_for_stop(event: threading.Event, delay: float) -> bool:
            if waits is not None:
                waits.append(delay)
            if not processes:
                event.set()
            return event.is_set()

        return PersistentSSHReplyTransport(
            SSHConnectionSpec("dev@host", ("python3", "helper.py")),
            receiver,
            backoff_policy=BackoffPolicy(
                floor=0.01,
                ceiling=0.04,
                multiplier=2,
                jitter_ratio=0,
            ),
            popen_factory=popen,
            status=(
                status_callback
                if status_callback is not None
                else statuses.append if statuses is not None else None
            ),
            wait_for_stop=wait_for_stop,
            shutdown_deadline_seconds=0.01,
            write_deadline_seconds=0.01,
        )

    def test_unicode_event_is_committed_before_exact_matching_ack(self) -> None:
        stop = threading.Event()
        process = _FakeProcess(_line(_event()))

        def commit(frame: dict[str, object], _count: int) -> _ReceiveResult:
            stop.set()
            return _ReceiveResult(_Ack(str(frame["event_id"]), str(frame["digest"])))

        receiver = _Receiver(commit)
        result = self._transport([process], receiver, stop).run(stop)

        self.assertEqual(receiver.frames[0]["answer"], _event()["answer"])
        self.assertEqual(
            process.stdin.getvalue(),
            _line(
                {
                    "digest": _event()["digest"],
                    "event_id": "event-001",
                    "version": 1,
                }
            ),
        )
        self.assertEqual(result.events_seen, 1)
        self.assertEqual(result.acknowledgements_sent, 1)
        self.assertTrue(result.reaped_cleanly)

    def test_throwing_status_observer_never_changes_commit_or_ack(self) -> None:
        stop = threading.Event()
        process = _FakeProcess(_line(_event()))

        def commit(frame: dict[str, object], _count: int) -> _ReceiveResult:
            stop.set()
            return _ReceiveResult(_Ack(str(frame["event_id"]), str(frame["digest"])))

        result = self._transport(
            [process],
            _Receiver(commit),
            stop,
            status_callback=lambda _status: (_ for _ in ()).throw(
                RuntimeError("observer")
            ),
        ).run(stop)

        self.assertEqual(1, result.events_seen)
        self.assertEqual(1, result.acknowledgements_sent)
        self.assertIn(b'"event_id":"event-001"', process.stdin.getvalue())

    def test_real_receiver_durable_commit_precedes_upstream_ack(self) -> None:
        stop = threading.Event()
        event = ReplyEvent.create(
            session="session-real",
            event_id="event-real",
            answer="Durable Unicode 🙂 \u0645\u0631\u062d\u0628\u0627",
        )
        process = _FakeProcess(event.to_bytes() + b"\n")
        with tempfile.TemporaryDirectory() as directory:
            durable = ReplyReceiver(Path(directory))

            class _StopAfterReceive:
                def receive(self, wire_bytes: bytes) -> ReceiveResult:
                    result = durable.receive(wire_bytes)
                    stop.set()
                    return result

            result = self._transport([process], _StopAfterReceive(), stop).run(stop)

            self.assertEqual(
                (Path(directory) / "canonical" / "event-real.json").read_bytes(),
                event.to_bytes(),
            )
            self.assertEqual(
                process.stdin.getvalue(),
                canonical_json_bytes(
                    {
                        "digest": event.digest,
                        "event_id": event.event_id,
                        "version": event.version,
                    }
                )
                + b"\n",
            )
            self.assertEqual(result.acknowledgements_sent, 1)

    def test_nondurable_commit_disconnects_without_ack_then_replays(self) -> None:
        stop = threading.Event()
        first = _FakeProcess(_line(_event()))
        second = _FakeProcess(_line(_event()))

        def commit(frame: dict[str, object], count: int) -> _ReceiveResult:
            if count == 2:
                stop.set()
            ack = _Ack(str(frame["event_id"]), str(frame["digest"])) if count == 2 else None
            return _ReceiveResult(ack)

        receiver = _Receiver(commit)
        result = self._transport([first, second], receiver, stop).run(stop)

        self.assertEqual(first.stdin.getvalue(), b"")
        self.assertIn(b'"event_id":"event-001"', second.stdin.getvalue())
        self.assertEqual(len(receiver.frames), 2)
        self.assertEqual(result.events_seen, 2)
        self.assertEqual(result.acknowledgements_sent, 1)
        self.assertEqual(result.reconnects, 1)

    def test_disconnect_after_ack_write_allows_duplicate_replay_and_ack(self) -> None:
        stop = threading.Event()
        first = _FakeProcess(_line(_event()))
        second = _FakeProcess(_line(_event()))

        def commit(frame: dict[str, object], count: int) -> _ReceiveResult:
            if count == 2:
                stop.set()
            return _ReceiveResult(_Ack(str(frame["event_id"]), str(frame["digest"])))

        receiver = _Receiver(commit)
        result = self._transport([first, second], receiver, stop).run(stop)

        self.assertIn(b'"event_id":"event-001"', first.stdin.getvalue())
        self.assertEqual(second.stdin.getvalue(), first.stdin.getvalue())
        self.assertEqual(result.events_seen, 2)
        self.assertEqual(result.acknowledgements_sent, 2)
        self.assertEqual(result.reconnects, 1)

    def test_ack_identity_mismatch_is_rejected_without_writing(self) -> None:
        stop = threading.Event()
        statuses: list[TransportStatus] = []
        process = _FakeProcess(_line(_event()))
        receiver = _Receiver(
            lambda frame, _count: _ReceiveResult(_Ack("different", str(frame["digest"])))
        )

        self._transport([process], receiver, stop, statuses=statuses).run(stop)

        self.assertEqual(process.stdin.getvalue(), b"")
        self.assertTrue(
            any(status.protocol_error is ProtocolErrorCode.ACK_MISMATCH for status in statuses)
        )

    def test_permanently_blocking_ack_pipe_forces_bounded_reconnect(self) -> None:
        stop = threading.Event()
        statuses: list[TransportStatus] = []
        process = _FakeProcess(_line(_event()))
        process.stdin = cast(_CaptureWriter, _BlockingWriter())
        receiver = _Receiver(
            lambda frame, _count: _ReceiveResult(
                _Ack(str(frame["event_id"]), str(frame["digest"]))
            )
        )

        started = time.monotonic()
        result = self._transport(
            [process], receiver, stop, statuses=statuses
        ).run(stop)

        self.assertLess(time.monotonic() - started, 0.25)
        self.assertEqual(1, result.events_seen)
        self.assertEqual(0, result.acknowledgements_sent)
        self.assertEqual(1, result.reconnects)
        self.assertTrue(result.reaped_cleanly)
        self.assertTrue(process.terminated)
        self.assertTrue(
            any(status.code is TransportStatusCode.IO_ERROR for status in statuses)
        )

    def test_partial_utf8_is_rejected_and_never_committed(self) -> None:
        stop = threading.Event()
        statuses: list[TransportStatus] = []
        process = _FakeProcess(b'{"answer":"\xf0\x9f')
        receiver = _Receiver(
            lambda frame, _count: _ReceiveResult(
                _Ack(str(frame["event_id"]), str(frame["digest"]))
            )
        )

        self._transport([process], receiver, stop, statuses=statuses).run(stop)

        self.assertEqual(receiver.frames, [])
        self.assertTrue(
            any(status.protocol_error is ProtocolErrorCode.PARTIAL_UTF8 for status in statuses)
        )

    def test_noncanonical_and_incompatible_frames_are_structured_errors(self) -> None:
        cases = (
            (b'{"version": 1}\n', ProtocolErrorCode.NON_CANONICAL_JSON),
            (_line(_event(version=2)), ProtocolErrorCode.VERSION_MISMATCH),
        )
        for payload, expected in cases:
            with self.subTest(expected=expected):
                stop = threading.Event()
                statuses: list[TransportStatus] = []
                receiver = _Receiver(
                    lambda frame, _count: _ReceiveResult(
                        _Ack(str(frame["event_id"]), str(frame["digest"]))
                    )
                )
                self._transport(
                    [_FakeProcess(payload)], receiver, stop, statuses=statuses
                ).run(stop)
                self.assertEqual(receiver.frames, [])
                self.assertTrue(
                    any(status.protocol_error is expected for status in statuses)
                )

    def test_repeated_connect_failures_use_bounded_exponential_waits(self) -> None:
        stop = threading.Event()
        waits: list[float] = []
        calls = 0

        def popen(_argv, **_kwargs):
            nonlocal calls
            calls += 1
            raise OSError("sensitive path must not surface")

        def wait_for_stop(event: threading.Event, delay: float) -> bool:
            waits.append(delay)
            if len(waits) == 5:
                event.set()
            return event.is_set()

        receiver = _Receiver(
            lambda frame, _count: _ReceiveResult(
                _Ack(str(frame["event_id"]), str(frame["digest"]))
            )
        )
        transport = PersistentSSHReplyTransport(
            SSHConnectionSpec("dev@host", ("python3", "helper.py")),
            receiver,
            backoff_policy=BackoffPolicy(0.01, 0.04, 2, 0),
            popen_factory=popen,
            wait_for_stop=wait_for_stop,
        )

        result = transport.run(stop)

        self.assertEqual(calls, 5)
        self.assertEqual(waits, [0.01, 0.02, 0.04, 0.04, 0.04])
        self.assertEqual(result.reconnects, 5)

    def test_shutdown_escalates_to_kill_and_reaps(self) -> None:
        stop = threading.Event()
        process = _FakeProcess(_line(_event()), wait_times_out_once=True)

        def commit(frame: dict[str, object], _count: int) -> _ReceiveResult:
            stop.set()
            return _ReceiveResult(_Ack(str(frame["event_id"]), str(frame["digest"])))

        receiver = _Receiver(commit)

        result = self._transport([process], receiver, stop).run(stop)

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertTrue(result.reaped_cleanly)
        self.assertFalse(result.boundary_replacement_required)

    def test_unreaped_process_taints_boundary_without_reconnect(self) -> None:
        stop = threading.Event()
        statuses: list[TransportStatus] = []
        process = _FakeProcess(_line(_event()), wait_always_times_out=True)
        receiver = _Receiver(
            lambda frame, _count: _ReceiveResult(
                _Ack(str(frame["event_id"]), str(frame["digest"]))
            )
        )

        result = self._transport(
            [process], receiver, stop, statuses=statuses
        ).run(stop)

        self.assertFalse(result.reaped_cleanly)
        self.assertTrue(result.boundary_replacement_required)
        self.assertEqual(result.reconnects, 0)
        self.assertTrue(process.killed)
        self.assertTrue(
            any(status.code is TransportStatusCode.REAP_FAILED for status in statuses)
        )

    def test_status_repr_never_contains_event_or_remote_content(self) -> None:
        status = TransportStatus(
            TransportStatusCode.PROTOCOL_ERROR,
            protocol_error=ProtocolErrorCode.INVALID_JSON,
        )
        frame = IncomingFrame("event-id", "a" * 64, _event())

        self.assertNotIn("answer", repr(status))
        self.assertNotIn("Unicode", repr(frame))


if __name__ == "__main__":
    unittest.main()
