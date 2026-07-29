"""Windows companion production composition root."""

from __future__ import annotations

import subprocess
import shlex
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from talktomeclaude import config
from talktomeclaude.assistant.hooks import (
    ClaudeHookManager,
    CodexHookManager,
    HookSettingsError,
    resolve_hook_executable,
)
from talktomeclaude.assistant.skills import (
    SkillInstallError,
    install_control_skills,
)
from talktomeclaude.assistant import SessionAttachmentRegistry
from talktomeclaude.assistant.session_control import (
    LiveLeaseHeartbeatOwner,
    attachment_state_path,
)
from talktomeclaude.capture import (
    CaptureMode,
    CaptureService,
    SnapshotCallableAdapter,
)
from talktomeclaude.companion.audio import (
    DedicatedAudioInput,
    Float32AudioAssembler,
)
from talktomeclaude.companion.capture_delivery import CaptureDeliveryCoordinator
from talktomeclaude.companion.contracts import (
    CompanionIntent,
    CompanionSnapshot,
    IntentKind,
)
from talktomeclaude.companion.hotkey import ThreadHotkeyListener
from talktomeclaude.companion.inbox import (
    DurableReplyInbox,
    InboxStatus,
    InboxStatusCode,
    SSHTransportOwner,
)
from talktomeclaude.companion.runtime import (
    CompanionController,
    CompanionSurfaces,
)
from talktomeclaude.companion.settings import VoiceSettingsService
from talktomeclaude.companion.speech import CompanionSpeech
from talktomeclaude.companion.tk_settings import TkCompanionSurfaces
from talktomeclaude.companion.tk_shell import TkCompanionShell
from talktomeclaude.diagnostics import DiagnosticStore
from talktomeclaude.listen import UtteranceTranscriber
from talktomeclaude.platform.windows.injector import TextInjector
from talktomeclaude.reply import (
    AckDisposition,
    AckResult,
    DiagnosticCode,
    ReplyAck,
    ReplyReceiver,
    ReplySpool,
)
from talktomeclaude.reply.ssh import (
    PersistentSSHReplyTransport,
    SSHConnectionSpec,
    TransportStatus,
    TransportStatusCode,
)
from talktomeclaude.speech import parse_control_command
from talktomeclaude.speech.voices import default_voice, get_voice, is_available


class CompanionStartupError(RuntimeError):
    """The configured production companion cannot start safely."""


_REMOTE_REPLY_COMMAND = ("talktomejohnny", "hook", "stream")


def _remote_reply_command(provider: str) -> tuple[str, ...]:
    if provider == "claude":
        return _REMOTE_REPLY_COMMAND
    if provider == "codex":
        return (*_REMOTE_REPLY_COMMAND, "--provider", "codex")
    raise ValueError(f"unsupported assistant provider {provider!r}")


def _reply_spool_path(root: Path, provider: str) -> Path:
    if provider == "claude":
        return root / "reply-spool"
    if provider == "codex":
        return root / "reply-spool-codex"
    raise ValueError(f"unsupported assistant provider {provider!r}")


def _reply_inbox_path(root: Path, provider: str) -> Path:
    if provider == "claude":
        return root / "reply-inbox"
    if provider == "codex":
        return root / "reply-inbox-codex"
    raise ValueError(f"unsupported assistant provider {provider!r}")


def _active_providers(provider: str) -> tuple[str, ...]:
    if provider == "both":
        return ("claude", "codex")
    if provider in {"claude", "codex"}:
        return (provider,)
    raise ValueError(f"unsupported assistant provider {provider!r}")


def _build_production_reply_inbox(
    root: Path,
    *,
    remote: str | None,
    provider: str,
) -> "ProductionReplyInbox | CompositeReplyInbox":
    registry = (
        SessionAttachmentRegistry(attachment_state_path()) if remote is None else None
    )
    inboxes = {
        selected: ProductionReplyInbox(
            ReplyReceiver(_reply_inbox_path(root, selected)),
            local_spool=(
                None
                if remote
                else ReplySpool(_reply_spool_path(root, selected))
            ),
            remote=remote,
            remote_command=_remote_reply_command(selected),
            provider=selected,
            attachment_registry=registry,
        )
        for selected in _active_providers(provider)
    }
    if len(inboxes) == 1:
        return next(iter(inboxes.values()))
    return CompositeReplyInbox(inboxes)


def _safe_stt_status(message: str) -> str:
    """Reduce third-party status prose to a content-free capability code."""

    normalized = message.casefold()
    for marker, code in (
        ("cuda", "cuda"),
        ("cpu", "cpu"),
        ("fallback", "fallback"),
        ("load", "loading"),
        ("ready", "ready"),
    ):
        if marker in normalized:
            return code
    return "updated"


def ensure_companion_hook(
    remote: str | None,
    *,
    provider: str = "claude",
    remote_cwd: str | None = None,
    local_settings_path: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Idempotently install the selected provider lifecycle integrations."""

    if provider not in config.ASSISTANT_PROVIDERS:
        raise CompanionStartupError(f"unsupported assistant provider {provider!r}")

    if provider == "both":
        for selected in ("claude", "codex"):
            ensure_companion_hook(
                remote,
                provider=selected,
                remote_cwd=remote_cwd,
                local_settings_path=local_settings_path,
                runner=runner,
            )
        return

    if remote:
        remote_install = ["talktomejohnny", "hook", "install"]
        if provider == "codex":
            remote_install.extend(["--provider", "codex"])
        command = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            remote,
            shlex.join(remote_install),
        ]
        try:
            result = runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CompanionStartupError(
                f"remote {provider.title()} lifecycle hook installation failed"
            ) from exc
        if result.returncode != 0:
            raise CompanionStartupError(
                f"remote {provider.title()} lifecycle hooks are unavailable; "
                "install TalkToMeJohnny on the remote"
            )
        return
    target = (
        Path(local_settings_path)
        if local_settings_path is not None
        else (
            Path.home() / ".claude" / "settings.json"
            if provider == "claude"
            else Path.home() / ".codex" / "hooks.json"
        )
    )
    try:
        executable = resolve_hook_executable()
        manager = (
            ClaudeHookManager(target, executable=executable)
            if provider == "claude"
            else CodexHookManager(target, executable=executable)
        )
        manager.install()
        install_control_skills(provider)
    except (HookSettingsError, SkillInstallError) as exc:
        raise CompanionStartupError(
            f"local {provider.title()} lifecycle hook installation failed"
        ) from exc


class PersistentTranscriberFactory:
    """Lazily warm one Faster Whisper owner and refresh cancellation per turn."""

    def __init__(
        self,
        device: str,
        model: str | None = None,
        *,
        status: Callable[[str], None] | None = None,
        transcriber_type: type[UtteranceTranscriber] = UtteranceTranscriber,
    ) -> None:
        self._device = device
        self._model = model
        self._status = status
        self._type = transcriber_type
        self._lock = threading.Lock()
        self._active_cancelled: Callable[[], bool] = lambda: False
        self._transcriber: UtteranceTranscriber | None = None

    def _cancelled(self) -> bool:
        return self._active_cancelled()

    def __call__(self, cancelled: Callable[[], bool]) -> UtteranceTranscriber:
        with self._lock:
            self._active_cancelled = cancelled
            if self._transcriber is None:
                self._transcriber = self._type(
                    self._device,
                    self._model,
                    on_status=self._status,
                    cancelled=self._cancelled,
                )
            return self._transcriber


class _EmptySpool:
    def pending(self) -> tuple[()]:
        return ()

    def commit_ack(self, ack: ReplyAck) -> AckResult:
        return AckResult(
            AckDisposition.REJECTED,
            DiagnosticCode.ACK_REJECTED,
            ack.event_id,
            ack.digest,
        )


class ProductionReplyInbox:
    """Bind the callback-shaped runtime protocol to durable inbox owners."""

    def __init__(
        self,
        receiver: ReplyReceiver,
        *,
        local_spool: ReplySpool | None = None,
        remote: str | None = None,
        remote_command: tuple[str, ...] = _REMOTE_REPLY_COMMAND,
        provider: str = "claude",
        attachment_registry: SessionAttachmentRegistry | None = None,
        shutdown_deadline_seconds: float = 1.2,
    ) -> None:
        if shutdown_deadline_seconds <= 0:
            raise ValueError("reply inbox shutdown deadline must be positive")
        self._receiver = receiver
        self._spool = local_spool
        self._remote = remote
        self._remote_command = remote_command
        self._provider = provider
        self._attachment_registry = attachment_registry
        self._shutdown_deadline = shutdown_deadline_seconds
        self._inbox: DurableReplyInbox | None = None
        self._transport: SSHTransportOwner | None = None
        self._lease_owner: LiveLeaseHeartbeatOwner | None = None
        self._lock = threading.Lock()

    def start(
        self,
        on_reply: Callable[[Any], bool],
        on_status: Callable[[str], None],
    ) -> None:
        with self._lock:
            if self._inbox is not None:
                return

            def observe(status: InboxStatus) -> None:
                if status.code is InboxStatusCode.TRANSPORT_FAULT:
                    on_status("disconnected")

            inbox = DurableReplyInbox(
                self._spool or _EmptySpool(),
                self._receiver,
                on_reply,
                shutdown_timeout_seconds=min(1.0, self._shutdown_deadline),
                on_status=observe,
            )
            transport_owner = None
            if self._remote:

                def transport_status(status: TransportStatus) -> None:
                    if status.code is TransportStatusCode.CONNECTED:
                        on_status("connected")
                    elif status.code in {
                        TransportStatusCode.DISCONNECTED,
                        TransportStatusCode.IO_ERROR,
                        TransportStatusCode.PROTOCOL_ERROR,
                    }:
                        on_status("disconnected")

                transport = PersistentSSHReplyTransport(
                    SSHConnectionSpec(
                        remote=self._remote,
                        remote_command=self._remote_command,
                    ),
                    self._receiver,
                    status=transport_status,
                )
                transport_owner = SSHTransportOwner(
                    transport,
                    shutdown_timeout_seconds=min(1.0, self._shutdown_deadline),
                    on_status=observe,
                )
            self._inbox = inbox
            self._transport = transport_owner
            lease_owner = None
            if self._remote is None and self._attachment_registry is not None:
                lease_owner = LiveLeaseHeartbeatOwner(
                    self._attachment_registry,
                    self._provider,
                    shutdown_timeout_seconds=min(1.0, self._shutdown_deadline),
                )
                lease_owner.start()
            self._lease_owner = lease_owner
            inbox.start()
            if transport_owner is not None:
                transport_owner.start()

    def stop(self) -> bool:
        with self._lock:
            inbox = self._inbox
            transport = self._transport
            lease_owner = self._lease_owner
            self._inbox = None
            self._transport = None
            self._lease_owner = None
        results: dict[str, bool] = {}
        threads: list[threading.Thread] = []

        def launch(name: str, action: Callable[[], bool]) -> None:
            def run() -> None:
                try:
                    results[name] = action()
                except BaseException:
                    results[name] = False

            thread = threading.Thread(
                target=run,
                name=f"ttc-inbox-stop-{name}",
                daemon=True,
            )
            threads.append(thread)
            thread.start()

        if transport is not None:
            launch("transport", lambda: transport.stop().stopped)
        if inbox is not None:
            launch("inbox", lambda: inbox.stop().stopped)
        if lease_owner is not None:
            launch("lease", lease_owner.stop)
        deadline = time.monotonic() + self._shutdown_deadline
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return bool(
            all(not thread.is_alive() for thread in threads)
            and len(results) == len(threads)
            and all(results.values())
        )


class CompositeReplyInbox:
    """Run isolated provider inboxes while exposing one runtime callback surface."""

    def __init__(
        self,
        inboxes: dict[str, ProductionReplyInbox],
        *,
        shutdown_deadline_seconds: float = 1.5,
    ) -> None:
        if not inboxes:
            raise ValueError("at least one provider inbox is required")
        if shutdown_deadline_seconds <= 0:
            raise ValueError("reply inbox shutdown deadline must be positive")
        self._inboxes = dict(inboxes)
        self._shutdown_deadline = shutdown_deadline_seconds
        self._started = False
        self._lock = threading.Lock()

    def start(
        self,
        on_reply: Callable[[Any], bool],
        on_status: Callable[[str], None],
    ) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        provider_status: dict[str, str] = {}
        status_lock = threading.Lock()
        last_status: list[str | None] = [None]

        def observe(provider: str, status: str) -> None:
            with status_lock:
                provider_status[provider] = status
                aggregate = None
                if "connected" in provider_status.values():
                    aggregate = "connected"
                elif len(provider_status) == len(self._inboxes) and all(
                    value == "disconnected" for value in provider_status.values()
                ):
                    aggregate = "disconnected"
                if aggregate is None or aggregate == last_status[0]:
                    return
                last_status[0] = aggregate
            on_status(aggregate)

        started: list[ProductionReplyInbox] = []
        try:
            for provider, inbox in self._inboxes.items():
                inbox.start(
                    on_reply,
                    lambda status, provider=provider: observe(provider, status),
                )
                started.append(inbox)
        except BaseException:
            for inbox in reversed(started):
                inbox.stop()
            with self._lock:
                self._started = False
            raise

    def stop(self) -> bool:
        with self._lock:
            if not self._started:
                return True
            self._started = False
        results: list[bool] = []
        result_lock = threading.Lock()

        def stop_one(inbox: ProductionReplyInbox) -> None:
            stopped = inbox.stop()
            with result_lock:
                results.append(stopped)

        threads = [
            threading.Thread(
                target=stop_one,
                args=(inbox,),
                name=f"ttj-composite-inbox-stop-{provider}",
                daemon=True,
            )
            for provider, inbox in self._inboxes.items()
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self._shutdown_deadline
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return bool(
            len(results) == len(threads)
            and all(results)
            and all(not thread.is_alive() for thread in threads)
        )


class CompanionShell(Protocol):
    def publish(self, snapshot: CompanionSnapshot) -> None: ...

    def run(self) -> None: ...

    def close(self) -> None: ...


class HotkeyOwner(Protocol):
    def start(self) -> None: ...

    def stop(self) -> bool: ...


class SurfaceOwner(Protocol):
    def stop(self) -> bool: ...


class ControllerOwner(Protocol):
    def subscribe(
        self, listener: Callable[[CompanionSnapshot], None]
    ) -> Callable[[], None]: ...

    def start_background(self) -> None: ...

    def dispatch(self, intent: CompanionIntent) -> CompanionSnapshot: ...


class DesktopCompanionApplication:
    """Own shell, controller subscription, hotkey, and final cleanup order."""

    def __init__(
        self,
        controller: ControllerOwner,
        shell: CompanionShell,
        hotkey: HotkeyOwner,
        *,
        surface_owner: SurfaceOwner | None = None,
        shutdown_deadline_seconds: float = 1.8,
    ) -> None:
        if shutdown_deadline_seconds <= 0:
            raise ValueError("application shutdown deadline must be positive")
        self.controller = controller
        self.shell = shell
        self.hotkey = hotkey
        self._surface_owner = surface_owner
        self._shutdown_deadline = shutdown_deadline_seconds

    def run(self) -> int:
        unsubscribe = self.controller.subscribe(self.shell.publish)
        try:
            self.controller.start_background()
            self.hotkey.start()
            self.shell.run()
            return 0
        finally:
            deadline = time.monotonic() + self._shutdown_deadline
            results: dict[str, bool] = {}

            def stop_hotkey() -> None:
                try:
                    results["hotkey"] = self.hotkey.stop()
                except BaseException:
                    results["hotkey"] = False

            def stop_controller() -> None:
                try:
                    self.controller.dispatch(CompanionIntent(IntentKind.QUIT))
                    results["controller"] = bool(
                        getattr(self.controller, "shutdown_clean", True)
                    )
                except BaseException:
                    results["controller"] = False

            shutdown_threads = [
                threading.Thread(
                    target=stop_hotkey,
                    name="ttc-shutdown-hotkey",
                    daemon=True,
                ),
                threading.Thread(
                    target=stop_controller,
                    name="ttc-shutdown-controller",
                    daemon=True,
                ),
            ]
            surface_owner = self._surface_owner
            if surface_owner is not None:

                def stop_surfaces() -> None:
                    try:
                        results["surfaces"] = surface_owner.stop()
                    except BaseException:
                        results["surfaces"] = False

                shutdown_threads.append(
                    threading.Thread(
                        target=stop_surfaces,
                        name="ttc-shutdown-surfaces",
                        daemon=True,
                    )
                )
            for thread in shutdown_threads:
                thread.start()
            for thread in shutdown_threads:
                thread.join(max(0.0, deadline - time.monotonic()))
            try:
                unsubscribe()
            finally:
                self.shell.close()
            expected_results = {"hotkey": True, "controller": True}
            if surface_owner is not None:
                expected_results["surfaces"] = True
            clean = (
                time.monotonic() <= deadline
                and all(not thread.is_alive() for thread in shutdown_threads)
                and results == expected_results
            )
            if not clean:
                raise RuntimeError("companion shutdown deadline was not met")


def _route_hotkey_press(controller: CompanionController) -> bool:
    """Route one registered key-down without conflating toggle and hold modes."""

    recording = controller.snapshot.runtime.phase.value == "recording"
    if recording and controller.capture_mode is CaptureMode.HOLD_TO_TALK:
        return False
    controller.dispatch(
        CompanionIntent(
            IntentKind.FINISH_RECORDING
            if recording
            else IntentKind.START_RECORDING
        )
    )
    return not recording and controller.capture_mode is CaptureMode.HOLD_TO_TALK


def _route_hotkey_release(controller: CompanionController) -> None:
    """Finish only an active hold-to-talk capture on primary-key release."""

    if (
        controller.capture_mode is CaptureMode.HOLD_TO_TALK
        and controller.snapshot.runtime.phase.value == "recording"
    ):
        controller.dispatch(CompanionIntent(IntentKind.FINISH_RECORDING))


def _selected_voice() -> str:
    configured = config.default_voice_name()
    try:
        voice = get_voice(configured) if configured else default_voice()
    except Exception as exc:
        raise CompanionStartupError(
            f"selected voice {configured!r} cannot be resolved"
        ) from exc
    if not is_available(voice):
        raise CompanionStartupError(
            f"selected voice {voice.name!r} is not available; selection was not changed"
        )
    return voice.name


def build_headless_controller() -> CompanionController:
    """Build the production controller without constructing Tk or hotkeys."""

    root = config.config_dir()
    remote = config.remote()
    provider = config.assistant_provider()
    ensure_companion_hook(
        remote,
        provider=provider,
        remote_cwd=config.remote_cwd(),
    )
    diagnostics = DiagnosticStore(root / "companion-diagnostics.json")
    injector = TextInjector()
    capture_service = CaptureService(
        snapshot_resolver=SnapshotCallableAdapter(injector.snapshot_target),
        audio_assembler=Float32AudioAssembler(),
    )
    capture = CaptureDeliveryCoordinator(
        capture_service,
        injector,
        control_parser=parse_control_command,
    )

    def record_audio_fault(fault: Any) -> None:
        diagnostics.record("audio_fault", error_code=fault.code.value)

    microphone = DedicatedAudioInput(
        capture,
        on_fault=record_audio_fault,
    )

    def record_stt_status(message: str) -> None:
        diagnostics.record("stt_status", status=_safe_stt_status(message))

    transcriber = PersistentTranscriberFactory(
        config.stt_device(),
        status=record_stt_status,
    )
    holder: dict[str, CompanionController] = {}
    speech = CompanionSpeech.create(
        _selected_voice(),
        root / "oral-session.json",
        initially_muted=not config.voice_assist_enabled(),
        on_answer_finished=lambda: holder["controller"].speech_finished(),
    )
    inbox = _build_production_reply_inbox(
        root,
        remote=remote,
        provider=provider,
    )
    mode = (
        CaptureMode.HOLD_TO_TALK
        if config.companion_recording_mode() == "push-to-talk"
        else CaptureMode.PUSH_TOGGLE
    )
    controller = CompanionController(
        capture,
        microphone,
        transcriber,
        speech=speech,
        inbox=inbox,
        diagnostics=diagnostics,
        surfaces=CompanionSurfaces(),
        capture_mode=mode,
        assistant_auto_submit=config.assistant_auto_submit_enabled(),
        output_muted=not config.voice_assist_enabled(),
        persist_output_muted=lambda muted: config.set_voice_assist(not muted),
    )
    holder["controller"] = controller
    return controller


def build_desktop_application() -> DesktopCompanionApplication:
    """Build the Windows-only product graph without opening Notepad or a terminal."""

    root = config.config_dir()
    remote = config.remote()
    provider = config.assistant_provider()
    ensure_companion_hook(
        remote,
        provider=provider,
        remote_cwd=config.remote_cwd(),
    )
    diagnostics = DiagnosticStore(root / "companion-diagnostics.json")
    injector = TextInjector()
    capture_service = CaptureService(
        snapshot_resolver=SnapshotCallableAdapter(injector.snapshot_target),
        audio_assembler=Float32AudioAssembler(),
    )
    capture = CaptureDeliveryCoordinator(
        capture_service,
        injector,
        control_parser=parse_control_command,
    )
    def record_audio_fault(fault: Any) -> None:
        diagnostics.record("audio_fault", error_code=fault.code.value)

    microphone = DedicatedAudioInput(capture, on_fault=record_audio_fault)

    def record_stt_status(message: str) -> None:
        diagnostics.record("stt_status", status=_safe_stt_status(message))

    transcriber = PersistentTranscriberFactory(
        config.stt_device(),
        status=record_stt_status,
    )
    selected_voice = _selected_voice()
    holder: dict[str, Any] = {}
    speech = CompanionSpeech.create(
        selected_voice,
        root / "oral-session.json",
        initially_muted=not config.voice_assist_enabled(),
        on_answer_finished=lambda: holder["controller"].speech_finished(),
    )
    inbox = _build_production_reply_inbox(
        root,
        remote=remote,
        provider=provider,
    )
    surface_holder: dict[str, TkCompanionSurfaces] = {}
    review_holder: dict[str, Callable[[], None]] = {}

    def open_settings() -> None:
        surface_holder["surfaces"].open_settings()

    def open_voice() -> None:
        surface_holder["surfaces"].open_voice()

    def open_pending_review() -> None:
        review_holder["open"]()

    def open_diagnostics() -> None:
        surface_holder["surfaces"].open_diagnostics()

    surfaces = CompanionSurfaces(
        settings=open_settings,
        voice=open_voice,
        review=open_pending_review,
        diagnostics=open_diagnostics,
    )
    mode = (
        CaptureMode.HOLD_TO_TALK
        if config.companion_recording_mode() == "push-to-talk"
        else CaptureMode.PUSH_TOGGLE
    )
    controller = CompanionController(
        capture,
        microphone,
        transcriber,
        speech=speech,
        inbox=inbox,
        diagnostics=diagnostics,
        surfaces=surfaces,
        capture_mode=mode,
        assistant_auto_submit=config.assistant_auto_submit_enabled(),
        output_muted=not config.voice_assist_enabled(),
        persist_output_muted=lambda muted: config.set_voice_assist(not muted),
    )
    holder["controller"] = controller
    shell = TkCompanionShell(controller.dispatch, controller.snapshot)

    def set_auto_submit(enabled: bool) -> None:
        config.set_assistant_auto_submit(enabled)
        controller.set_assistant_auto_submit(enabled)

    def set_recording_mode(value: str) -> None:
        config.set_recording_mode(value)
        controller.set_capture_mode(
            CaptureMode.HOLD_TO_TALK
            if value == "push-to-talk"
            else CaptureMode.PUSH_TOGGLE
        )

    def set_assistant_provider(value: str) -> None:
        config.set_assistant_provider(value)

    surface_holder["surfaces"] = TkCompanionSurfaces(
        shell.root,
        VoiceSettingsService(),
        diagnostics,
        get_auto_submit=config.assistant_auto_submit_enabled,
        set_auto_submit=set_auto_submit,
        get_recording_mode=config.companion_recording_mode,
        set_recording_mode=set_recording_mode,
        get_assistant_provider=config.assistant_provider,
        set_assistant_provider=set_assistant_provider,
    )

    def open_review() -> None:
        review = controller.pending_review
        if review is None:
            raise RuntimeError("there is no transcript awaiting review")
        surface_holder["surfaces"].open_review(
            review,
            on_confirm=controller.confirm_review,
            on_cancel=lambda: controller.dispatch(
                CompanionIntent(IntentKind.CANCEL)
            ),
        )

    review_holder["open"] = open_review

    hotkey = ThreadHotkeyListener(
        lambda: _route_hotkey_press(controller),
        release_callback=lambda: _route_hotkey_release(controller),
    )
    return DesktopCompanionApplication(
        controller,
        shell,
        hotkey,
        surface_owner=surface_holder["surfaces"],
    )


def run_desktop_companion() -> int:
    return build_desktop_application().run()


__all__ = [
    "CompanionStartupError",
    "DesktopCompanionApplication",
    "PersistentTranscriberFactory",
    "ProductionReplyInbox",
    "build_desktop_application",
    "build_headless_controller",
    "ensure_companion_hook",
    "run_desktop_companion",
]
