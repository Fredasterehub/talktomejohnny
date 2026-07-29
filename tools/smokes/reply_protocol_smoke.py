"""Real Proxmox Unicode/reconnect smoke for the durable reply protocol.

The smoke stages code only under a unique remote /tmp directory.  It never
reads or changes Claude settings, TalkToMeClaude config, voices, or caches.
Its report contains only synthetic identities, hashes, counts, and booleans.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from talktomeclaude.reply import ReplyEvent, ReplyReceiver
from talktomeclaude.reply.ssh import (
    PersistentSSHReplyTransport,
    SSHConnectionSpec,
    TransportStatusCode,
)


EVENT_ID = "g5-unicode-reconnect"
SESSION = "g5-smoke-session"
ANSWER = "Café 世界 👋 e\N{COMBINING ACUTE ACCENT}\r\nمرحبا"
_REMOTE_ROOT = re.compile(r"^/tmp/ttc-g5-smoke-[A-Za-z0-9]+$")


def _ssh(remote: str, command: Sequence[str], *, timeout: float = 30.0) -> bytes:
    result = subprocess.run(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            remote,
            shlex.join(tuple(command)),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=timeout,
    )
    return result.stdout


def _stage(remote: str, repository: Path) -> tuple[str, str, str]:
    remote_root = _ssh(
        remote, ("mktemp", "-d", "/tmp/ttc-g5-smoke-XXXXXXXX")
    ).decode("ascii").strip()
    if _REMOTE_ROOT.fullmatch(remote_root) is None:
        raise RuntimeError("unsafe_remote_root")
    remote_source = f"{remote_root}/src"
    remote_spool = f"{remote_root}/spool"
    try:
        _ssh(remote, ("mkdir", "-p", remote_source))
        subprocess.run(
            [
                "scp",
                "-q",
                "-r",
                "--",
                str(repository / "src" / "talktomeclaude"),
                f"{remote}:{remote_source}/",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )
        encoded_answer = base64.b64encode(ANSWER.encode("utf-8")).decode("ascii")
        seed = (
            "import base64,sys;"
            "from talktomeclaude.reply import ReplyEvent,ReplySpool;"
            "a=base64.b64decode(sys.argv[2]).decode('utf-8');"
            "e=ReplyEvent.create(session=sys.argv[3],event_id=sys.argv[4],answer=a);"
            "ReplySpool(sys.argv[1]).enqueue(e)"
        )
        _ssh(
            remote,
            (
                "env",
                f"PYTHONPATH={remote_source}",
                "python3",
                "-c",
                seed,
                remote_spool,
                encoded_answer,
                SESSION,
                EVENT_ID,
            ),
        )
        return remote_root, remote_source, remote_spool
    except BaseException:
        fallback = (
            "import pathlib,shutil,sys;root=pathlib.Path(sys.argv[1]);"
            "assert root.parent==pathlib.Path('/tmp') and "
            "root.name.startswith('ttc-g5-smoke-');shutil.rmtree(root)"
        )
        try:
            _ssh(remote, ("python3", "-c", fallback, remote_root))
        except (OSError, subprocess.SubprocessError):
            pass
        raise


def _remote_ready(remote: str, remote_spool: str, *, acknowledged: bool) -> bool:
    probe = (
        "import pathlib,sys;"
        "r=pathlib.Path(sys.argv[1]);i=sys.argv[2]+'.json';"
        "ok=((r/'acked'/i).is_file() and not (r/'ready'/i).exists()) "
        "if sys.argv[3]=='acked' else (r/'ready'/i).is_file();"
        "raise SystemExit(0 if ok else 1)"
    )
    try:
        _ssh(
            remote,
            (
                "python3",
                "-c",
                probe,
                remote_spool,
                EVENT_ID,
                "acked" if acknowledged else "ready",
            ),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _cleanup(remote: str, remote_root: str, remote_source: str, remote_spool: str) -> bool:
    if _REMOTE_ROOT.fullmatch(remote_root) is None:
        return False
    cleanup = (
        "import pathlib,shutil,sys;"
        "from talktomeclaude.storage.atomic import "
        "_posix_lock_path,lock_identity_for_path;"
        "root=pathlib.Path(sys.argv[1]);spool=pathlib.Path(sys.argv[2]);"
        "assert root.parent==pathlib.Path('/tmp') and root.name.startswith('ttc-g5-smoke-');"
        "lock=_posix_lock_path(lock_identity_for_path("
        "spool/'.spool-state.json',purpose='reply-spool'));"
        "lock.unlink(missing_ok=True);shutil.rmtree(root)"
    )
    try:
        _ssh(
            remote,
            (
                "env",
                f"PYTHONPATH={remote_source}",
                "python3",
                "-c",
                cleanup,
                remote_root,
                remote_spool,
            ),
        )
        verify = _ssh(
            remote,
            (
                "python3",
                "-c",
                "import pathlib,sys;raise SystemExit(pathlib.Path(sys.argv[1]).exists())",
                remote_root,
            ),
        )
        return verify == b""
    except (OSError, subprocess.SubprocessError):
        fallback = (
            "import pathlib,shutil,sys;root=pathlib.Path(sys.argv[1]);"
            "assert root.parent==pathlib.Path('/tmp') and "
            "root.name.startswith('ttc-g5-smoke-');shutil.rmtree(root)"
        )
        try:
            _ssh(remote, ("python3", "-c", fallback, remote_root))
            return True
        except (OSError, subprocess.SubprocessError):
            return False


def _transport(
    remote: str,
    remote_source: str,
    remote_spool: str,
    receiver: object,
    stop: threading.Event,
    status: Any,
) -> PersistentSSHReplyTransport:
    return PersistentSSHReplyTransport(
        SSHConnectionSpec(
            remote=remote,
            remote_command=(
                "env",
                f"PYTHONPATH={remote_source}",
                "python3",
                "-u",
                "-m",
                "talktomeclaude.reply.remote",
                "stream",
                "--spool-root",
                remote_spool,
                "--poll-interval",
                "0.02",
            ),
            connect_timeout_seconds=10,
        ),
        receiver,  # type: ignore[arg-type]
        shutdown_deadline_seconds=2,
        status=status,
    )


def _run(args: argparse.Namespace) -> dict[str, object]:
    repository = Path(args.repository).resolve()
    remote_root = ""
    remote_source = ""
    remote_spool = ""
    cleanup_ok = False
    try:
        remote_root, remote_source, remote_spool = _stage(args.remote, repository)
        with tempfile.TemporaryDirectory(prefix="ttc-g5-local-") as directory:
            receiver = ReplyReceiver(Path(directory) / "receiver")
            first_results: list[object] = []
            first_stop = threading.Event()

            class WithholdAck:
                def receive(self, wire: bytes) -> object:
                    result = receiver.receive(wire)
                    first_results.append(result)
                    return SimpleNamespace(ack=None)

            def first_status(item: object) -> None:
                if getattr(item, "code", None) is TransportStatusCode.COMMIT_REJECTED:
                    first_stop.set()

            first = _transport(
                args.remote,
                remote_source,
                remote_spool,
                WithholdAck(),
                first_stop,
                first_status,
            ).run(first_stop)
            if (
                len(first_results) != 1
                or not getattr(first_results[0], "apply", False)
                or first.acknowledgements_sent != 0
                or not _remote_ready(args.remote, remote_spool, acknowledged=False)
            ):
                raise RuntimeError("disconnect_phase_failed")

            second_results: list[object] = []
            second_stop = threading.Event()
            second_seen = threading.Event()

            class ReplayReceiver:
                def receive(self, wire: bytes) -> object:
                    result = receiver.receive(wire)
                    second_results.append(result)
                    second_seen.set()
                    return result

            holder: list[object] = []
            thread = threading.Thread(
                target=lambda: holder.append(
                    _transport(
                        args.remote,
                        remote_source,
                        remote_spool,
                        ReplayReceiver(),
                        second_stop,
                        lambda _item: None,
                    ).run(second_stop)
                ),
                daemon=True,
            )
            thread.start()
            if not second_seen.wait(15):
                raise RuntimeError("replay_timeout")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not _remote_ready(
                args.remote, remote_spool, acknowledged=True
            ):
                time.sleep(0.05)
            remote_acked = _remote_ready(
                args.remote, remote_spool, acknowledged=True
            )
            second_stop.set()
            thread.join(8)
            if thread.is_alive() or not holder:
                raise RuntimeError("transport_shutdown_failed")
            second = holder[0]
            committed = receiver.read_committed(EVENT_ID)
            if (
                len(second_results) != 1
                or getattr(second_results[0], "apply", True)
                or getattr(second, "acknowledgements_sent", 0) != 1
                or not remote_acked
                or committed is None
                or committed.answer != ANSWER
            ):
                raise RuntimeError("replay_phase_failed")

            return {
                "result_code": "passed",
                "event_id": EVENT_ID,
                "digest": ReplyEvent.create(
                    session=SESSION, event_id=EVENT_ID, answer=ANSWER
                ).digest,
                "answer_utf8_bytes": len(ANSWER.encode("utf-8")),
                "first_local_apply": True,
                "first_ack_withheld": True,
                "remote_ready_after_disconnect": True,
                "replay_local_apply": False,
                "replay_ack_sent": True,
                "remote_ack_committed": remote_acked,
                "local_unicode_exact": True,
                "first_ssh_reaped": getattr(first, "reaped_cleanly", False),
                "second_ssh_reaped": getattr(second, "reaped_cleanly", False),
            }
    finally:
        if remote_root:
            cleanup_ok = _cleanup(
                args.remote, remote_root, remote_source, remote_spool
            )
        args.cleanup_result.append(cleanup_ok)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote", default="user@example-host")
    parser.add_argument("--repository", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args()
    if (
        not args.remote
        or args.remote.startswith("-")
        or re.fullmatch(r"[A-Za-z0-9_.@:-]+", args.remote) is None
    ):
        parser.error("remote must be a safe SSH target")
    args.cleanup_result = []
    try:
        report = _run(args)
    except Exception as exc:
        report = {"result_code": "failed", "failure_code": type(exc).__name__}
    cleanup_ok = bool(args.cleanup_result and args.cleanup_result[-1])
    report["remote_cleanup"] = cleanup_ok
    if not cleanup_ok:
        report["result_code"] = "failed_cleanup"
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["result_code"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
