"""Microphone listen loop: hear the operator, transcribe locally, drive Claude Code.

Injection strategy (directive D-3): the primary path drives ``claude -p`` with
``--resume`` so the voice loop owns its own session and every reply comes back
as structured JSON for the dialogue-only filter — no tmux requirement. The
alternate path types the transcript into a live interactive Claude Code TUI
pane via ``tmux send-keys``. One driver per session: a session owned by the
voice loop is never simultaneously driven from a live interactive window.

Remote/SSH (``remote=user@host``): the microphone, transcription and spoken
reply stay on the machine the operator sits at, while Claude Code runs on the
server — either injection path is tunnelled over SSH. Multiplexing is used on
POSIX clients; native Windows uses its in-box OpenSSH without Unix
control-socket options. This is the headless-server pattern (e.g. a laptop
driving Claude Code on a Proxmox box): the server needs no audio hardware, the
client needs no Claude.

Recording modes (locked vocabulary): ``always-on`` segments hands-free at
pauses with VAD-gated transcription; ``push-to-talk`` records while a key is
held (terminal raw-mode reads, no extra dependencies); ``push-toggle`` starts
on one tap and sends on the next. The microphone stream is opened per
utterance and closed before any reply is spoken, so the listener never hears
the TTS voice and re-transcribes it as a prompt.
"""

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from talktomeclaude import command_catalog, config, conveyance, intent
from talktomeclaude.capture.contracts import CaptureCancelled
from talktomeclaude.stt import HOTWORDS, detect_tier, models_dir
from talktomeclaude.transcript import speakable

SAMPLE_RATE = 16000
_BLOCK_SECONDS = 0.05
_PREROLL_SECONDS = 0.3
_CALIBRATION_SECONDS = 0.4
_SILENCE_HANG_SECONDS = 0.9
_KEY_RELEASE_SECONDS = 0.6
_MAX_UTTERANCE_SECONDS = 60.0
_MIN_UTTERANCE_SECONDS = 0.3
_WINDOWS_KEY_POLL_SECONDS = 0.01


class ListenError(RuntimeError):
    """Raised when the listen loop cannot proceed."""


CONVEYANCE_VERBS = (
    "continue",
    "repeat",
    "back",
    "skip",
    "stop",
    "slower",
    "expand",
    "steer",
)


def detect_verb(utterance: str) -> str | None:
    """Return a closed conveyance verb only for an exact spoken match."""
    normalized = utterance.strip().casefold()
    return normalized if normalized in CONVEYANCE_VERBS else None


def _is_windows() -> bool:
    """Return whether native Windows console/SSH behavior is required."""
    return os.name == "nt"


def barge_in_active(config_on: bool, headphones: bool) -> bool:
    """Return whether opted-in barge-in has capable audio hardware."""
    return config_on and headphones


# ── voice-activated commands ─────────────────────────────────────────────────
WAKE_GREETING = "Yeah, I'm listening."
INTENT_MODEL = "haiku"
_INTENT_CONFIDENCE_FLOOR = 0.75
_MAX_CLARIFICATIONS = 2
_COMMAND_TRIGGERS = ("run command ", "fire command ", "command ")
_FIRE_WORDS = frozenset({"go", "yes", "fire", "confirm", "do it"})
_CANCEL_WORDS = frozenset({"cancel", "no", "stop", "never mind", "nevermind"})

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class IntentOutcome:
    """The typed result of classifying one spoken command request."""

    kind: str  # complete | missing_slot | ambiguous | no_match
    record: dict | None = None
    args: str = ""
    missing_slots: tuple = ()
    alternatives: tuple = ()


_NO_MATCH = IntentOutcome("no_match")


def _matching_records(catalog: list[dict], identity) -> list[dict]:
    """Records named by *identity* — an exact qualified identity wins; a bare
    id returns every carrier so a collision can surface as ambiguity."""
    wanted = str(identity).strip().casefold()
    if not wanted:
        return []
    qualified = [
        record
        for record in catalog
        if command_catalog.qualified_id(record).casefold() == wanted
    ]
    if qualified:
        return qualified
    return [record for record in catalog if str(record.get("id", "")).casefold() == wanted]


def _fire_text(record: dict, args: str) -> str:
    return f"/{command_catalog.qualified_id(record)} {args}".strip()


def _allowed_records(catalog: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split the enabled catalog into (resolvable, policy-blocked) records."""
    enabled = [record for record in catalog if record.get("enabled", True)]
    if _setting(config.command_namespace_policy, "allow-all") != "allowlist":
        return enabled, []
    allowlist = _setting(config.command_namespace_allowlist, ())
    allowed = [r for r in enabled if (r.get("namespace") or "") in allowlist]
    blocked = [r for r in enabled if (r.get("namespace") or "") not in allowlist]
    return allowed, blocked


def _is_fireable(record: dict) -> bool:
    """Whether a catalog record may be voice-fired: enabled and permitted by the
    active namespace policy."""
    if not record.get("enabled", True):
        return False
    if _setting(config.command_namespace_policy, "allow-all") == "allowlist":
        allowlist = _setting(config.command_namespace_allowlist, ())
        return (record.get("namespace") or "") in allowlist
    return True


def _command_request(utterance: str) -> str:
    lowered = utterance.strip().casefold()
    for trigger in _COMMAND_TRIGGERS:
        if lowered.startswith(trigger):
            return utterance.strip()[len(trigger):].strip()
    return utterance.strip()


def _match_name(utterance: str, records: list[dict]) -> "IntentOutcome | None":
    """Deterministic name match: the leading word exactly names a command
    (keyword prefilter, no model round-trip) and the remainder rides along as
    candidate args; a bare-name collision surfaces as ambiguity."""
    words = utterance.strip().split()
    if not words or not records:
        return None
    head, remainder = words[0], " ".join(words[1:])
    if intent.keyword_prefilter(head, records) is None:
        return None
    matches = _matching_records(records, head)
    if not matches:
        return None
    if len(matches) == 1:
        record = matches[0]
        if not remainder:
            slots = command_catalog.required_slots(record)
            if slots:
                # Named exactly, but the advertised schema needs arguments the
                # utterance did not supply — clarify instead of firing empty.
                return IntentOutcome(
                    "missing_slot", record=record, missing_slots=tuple(slots)
                )
        return IntentOutcome("complete", record=record, args=remainder)
    choices = tuple(
        command_catalog.qualified_id(match)
        for match in matches[: intent.MAX_SPOKEN_ALTERNATIVES]
    )
    return IntentOutcome("ambiguous", args=remainder, alternatives=choices)


def _pick_alternative(
    utterance: str, alternatives: tuple, catalog: list[dict], args: str
) -> "IntentOutcome | None":
    """Resolve a disambiguation follow-up that exactly names one offered
    alternative, carrying the candidate args unless the follow-up brings its own."""
    candidates = []
    for alternative in alternatives:
        matches = _matching_records(catalog, alternative)
        if len(matches) == 1 and matches[0] not in candidates:
            candidates.append(matches[0])
    hit = _match_name(utterance, candidates)
    if hit is None or hit.kind != "complete":
        return None
    return IntentOutcome("complete", record=hit.record, args=hit.args or args)


def _intent_prompt(request: str, catalog: list[dict], clarifications=()) -> str:
    lines = [
        "Classify this spoken request against the available commands. Answer "
        "with ONLY a JSON object shaped as "
        '{"command_id": string or null, "args": string, "missing_slots": [], '
        '"confidence": number between 0 and 1, "alternatives": []}. '
        "Use the qualified command name exactly as listed for command_id and "
        "alternatives; name genuinely required argument slots in missing_slots.",
        f"Request: {request}",
    ]
    for label, answer in clarifications:
        lines.append(f"Clarification ({label}): {answer}")
    lines.append("Commands:")
    for record in catalog:
        description = record.get("description") or "no description"
        lines.append(f"- {command_catalog.qualified_id(record)}: {description}")
    return "\n".join(lines)


def _classify_intent(
    request: str,
    catalog: list[dict],
    status: Callable[[str], None],
    clarifications=(),
) -> IntentOutcome:
    """Resolve *request* through the isolated intent sub-call — its own
    throwaway ``claude -p`` session, never resumed into the working history."""
    command = intent.intent_subcall_command(
        _intent_prompt(request, catalog, clarifications), INTENT_MODEL
    )
    try:
        result = _run_captured(command)
    except (OSError, subprocess.SubprocessError) as exc:
        status(f"intent classification unavailable ({exc}); treating the request as content")
        return _NO_MATCH
    if result.returncode != 0 or not result.stdout.strip():
        status("intent classification failed; treating the request as content")
        return _NO_MATCH
    try:
        envelope = json.loads(result.stdout)
        raw = envelope.get("result", "") if isinstance(envelope, dict) else ""
        raw = raw.strip() if isinstance(raw, str) else ""
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = intent.parse_intent_response(raw)
    except (ValueError, KeyError, TypeError, IndexError):
        status("intent response unreadable; treating the request as content")
        return _NO_MATCH
    confidence = parsed.confidence if isinstance(parsed.confidence, (int, float)) else 0.0
    args = parsed.args if isinstance(parsed.args, str) else ""
    slots = intent.sanitize_missing_slots(parsed.missing_slots)
    alternatives = intent.sanitize_alternatives(parsed.alternatives, catalog)
    matches = _matching_records(catalog, parsed.command_id) if parsed.command_id else []
    if len(matches) > 1:
        choices = tuple(
            command_catalog.qualified_id(match)
            for match in matches[: intent.MAX_SPOKEN_ALTERNATIVES]
        )
        return IntentOutcome("ambiguous", args=args, alternatives=choices)
    record = matches[0] if matches else None
    if record is not None and confidence >= _INTENT_CONFIDENCE_FLOOR:
        if slots:
            return IntentOutcome(
                "missing_slot", record=record, args=args, missing_slots=tuple(slots)
            )
        identity = command_catalog.qualified_id(record)
        unresolved = [alt for alt in alternatives if alt != identity]
        if unresolved:
            choices = tuple(
                [identity, *unresolved][: intent.MAX_SPOKEN_ALTERNATIVES]
            )
            return IntentOutcome("ambiguous", args=args, alternatives=choices)
        return IntentOutcome("complete", record=record, args=args)
    if alternatives:
        return IntentOutcome("ambiguous", args=args, alternatives=tuple(alternatives))
    status("no confident command match; treating the request as content")
    return _NO_MATCH


def _report_non_fireable(
    matched: IntentOutcome, catalog: list[dict], status: Callable[[str], None]
) -> None:
    """Explain why an exactly-named command is not fireable — disabled, or
    blocked by the namespace allowlist — before routing it to content."""
    records = [matched.record] if matched.record is not None else []
    if not records:
        for alternative in matched.alternatives:
            records.extend(_matching_records(catalog, alternative))
    if records and all(not record.get("enabled", True) for record in records):
        names = ", ".join(command_catalog.qualified_id(record) for record in records)
        status(f"/{names} is disabled; treating the request as content")
        return
    namespace = next(
        (record.get("namespace") for record in records if record.get("namespace")),
        None,
    ) or (matched.alternatives[0].split(":")[0] if matched.alternatives else "top-level")
    status(
        f"the {namespace} namespace is not allowed by the "
        "command-namespace allowlist; treating the request as content"
    )


def _resolve_command(
    utterance: str, catalog: list[dict], status: Callable[[str], None]
) -> IntentOutcome:
    """Resolve a spoken utterance to a typed command outcome.

    Exact command identities resolve against the COMPLETE catalog first. An
    utterance that exactly names a known-but-non-fireable command (disabled or
    policy-blocked) stops as content immediately — it never reaches the
    classifier, so it can never be remapped onto a different enabled mutating
    command. Only a genuinely unmatched explicit command request ("command …",
    "run command …", "fire command …") escalates to the isolated intent
    sub-call, whose result _classify_intent validates back to a fireable record.
    """
    allowed, _blocked = _allowed_records(catalog)
    non_fireable = [record for record in catalog if not _is_fireable(record)]
    if not allowed and not non_fireable:
        return _NO_MATCH
    request = _command_request(utterance)
    triggered = request != utterance.strip()
    # Fast path: an exact deterministic match among the fireable commands.
    outcome = _match_name(utterance, allowed)
    if outcome is None and triggered and request:
        outcome = _match_name(request, allowed)
    if outcome is not None and outcome.kind != "no_match":
        return outcome
    # An exact identity naming a disabled/blocked command is content — resolved
    # here, ahead of any classifier call that could remap it.
    named = _match_name(utterance, non_fireable)
    if named is None and triggered and request:
        named = _match_name(request, non_fireable)
    if named is not None and named.kind != "no_match":
        _report_non_fireable(named, catalog, status)
        return _NO_MATCH
    # Genuinely unmatched: escalate an explicit command request to the classifier.
    if triggered and request and allowed:
        return _classify_intent(request, allowed, status)
    return _NO_MATCH


def _spoken_choices(alternatives) -> str:
    listed = list(alternatives)
    if len(listed) == 1:
        return listed[0]
    return ", ".join(listed[:-1]) + f", or {listed[-1]}"


# ── wake word, barge-in, and gradual conveyance ──────────────────────────────
class WakeDisposition(Enum):
    OFF_UNGATED = "off-ungated"
    WAKE_GRANTED = "wake-granted"
    MANUAL_FALLBACK = "manual-fallback"
    STOP = "stop"


def _setting(reader: Callable[[], object], default):
    """Read a persisted setting, degrading to *default* when the config store
    is unreachable (for example, no resolvable home directory)."""
    try:
        return reader()
    except Exception:
        return default


def _wake_gate(
    speak: Callable[[str], None],
    status: Callable[[str], None],
    stop_event: "threading.Event | None",
    state: dict,
) -> WakeDisposition:
    """Resolve wake gating without ever opening a failed gate."""
    if stop_event is not None and stop_event.is_set():
        return WakeDisposition.STOP
    try:
        enabled, unavailable = config.wake_word_state()
    except Exception:
        enabled, unavailable = True, True
    if unavailable:
        if not state.get("degraded"):
            state["degraded"] = True
            status(
                "wake word unavailable: configuration is unreadable; "
                "manual push-to-talk required"
            )
        return WakeDisposition.MANUAL_FALLBACK
    if not enabled:
        return WakeDisposition.OFF_UNGATED
    if state.get("degraded"):
        return WakeDisposition.MANUAL_FALLBACK
    model_path = _setting(config.wake_model_path, None)
    phrase = _setting(config.wake_phrase, config.DEFAULT_WAKE_PHRASE)
    if not model_path:
        state["degraded"] = True
        status(
            "wake word manual fallback: no detector model is configured "
            "(config set wake-model /path/to/model.onnx); push-to-talk required"
        )
        return WakeDisposition.MANUAL_FALLBACK
    from talktomeclaude import wakeword

    status(f"waiting for the wake phrase {phrase!r}")
    try:
        heard = wakeword.wait_for_wake_word(
            model_path, phrase=phrase, stop_event=stop_event
        )
    except wakeword.WakeWordError as exc:
        state["degraded"] = True
        status(f"wake word unavailable: {exc}; manual push-to-talk required")
        return WakeDisposition.MANUAL_FALLBACK
    if heard is None:
        return WakeDisposition.STOP
    speak(WAKE_GREETING)  # local canned greeting — never a Claude round-trip
    return WakeDisposition.WAKE_GRANTED


def headphones_present() -> bool:
    """Best-effort detection of headphone-class output hardware.

    Any failure reads as absent, auto-degrading barge-in to half-duplex.
    """
    try:
        sounddevice = _sounddevice()
        device = sounddevice.query_devices(kind="output")
        name = str(device["name"]).casefold()
    except Exception:
        return False
    return any(
        marker in name for marker in ("headphone", "headset", "earbud", "airpod")
    )


_BARGE_IN_CALIBRATION_BLOCKS = 6
_BARGE_IN_VOICED_BLOCKS = 3


def _speak_interruptible(
    speak: Callable[[str], None],
    text: str,
    stop_event: "threading.Event | None" = None,
):
    """Play *text* while watching the microphone, halting playback the moment
    the operator speaks. Returns the operator's interrupting utterance as
    captured audio — the barge-in IS the next turn, never a repeat — or None
    when playback finished undisturbed (or the session is stopping).

    Playback runs on a worker thread; detection watches microphone energy
    against a floor calibrated just before playback starts (the headphone
    gate keeps the TTS voice out of the microphone). On detection, playback
    is terminated through ``sounddevice.stop()`` while the input stream stays
    open, capturing the rest of the utterance until a trailing pause. A
    rolling pre-roll keeps the words that triggered detection.
    """
    sounddevice = _sounddevice()
    blocksize = int(_BLOCK_SECONDS * SAMPLE_RATE)
    preroll_blocks = max(1, int(_PREROLL_SECONDS / _BLOCK_SECONDS))
    hang_blocks = max(1, int(_SILENCE_HANG_SECONDS / _BLOCK_SECONDS))
    max_blocks = int(_MAX_UTTERANCE_SECONDS / _BLOCK_SECONDS)
    failure: list[BaseException] = []
    done = threading.Event()

    def playback() -> None:
        try:
            speak(text)
        except BaseException as exc:  # surfaced to the caller after join
            failure.append(exc)
        finally:
            done.set()

    def stopping() -> bool:
        return stop_event is not None and stop_event.is_set()
    chunks: list = []
    with sounddevice.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=blocksize
    ) as stream:
        floors = []
        for _ in range(_BARGE_IN_CALIBRATION_BLOCKS):
            block, _overflowed = stream.read(blocksize)
            floors.append(_rms(block))
        threshold = max(sum(floors) / len(floors) * 4.0, 0.02)
        worker = threading.Thread(target=playback, daemon=True)
        worker.start()
        voiced = 0
        preroll: list = []
        while not done.is_set():
            if stopping():
                break
            block, _overflowed = stream.read(blocksize)
            preroll.append(block.copy())
            if len(preroll) > preroll_blocks:
                preroll.pop(0)
            voiced = voiced + 1 if _rms(block) >= threshold else 0
            if voiced >= _BARGE_IN_VOICED_BLOCKS:
                # Barge-in: halt playback, then keep the already-open stream
                # capturing the interruption until a trailing pause.
                try:
                    sounddevice.stop()
                except Exception:
                    pass
                chunks = list(preroll)
                silent_blocks = 0
                while len(chunks) < max_blocks and not stopping():
                    block, _overflowed = stream.read(blocksize)
                    chunks.append(block.copy())
                    silent_blocks = silent_blocks + 1 if _rms(block) < threshold else 0
                    if silent_blocks >= hang_blocks:
                        break
                break
    if stopping():
        try:
            sounddevice.stop()
        except Exception:
            pass
        chunks = []
    if chunks or stopping():
        # A cloned voice may still be synthesizing when the interruption lands.
        # Drain the worker instead of abandoning it: repeated stop calls catch
        # playback as soon as synthesis hands off to sounddevice, so stale audio
        # cannot leak into the next turn or survive session shutdown.
        while worker.is_alive():
            try:
                sounddevice.stop()
            except Exception:
                pass
            worker.join(0.05)
    else:
        worker.join()
    if failure:
        raise failure[0]
    return _finish(chunks)


def _chunk_heading(text: str) -> str:
    return " ".join(text.split()[:6])


def _write_checkpoint(
    session_id: str | None, cursor: int, heading: str, status_value: str
) -> None:
    if not session_id:
        return
    try:
        conveyance.write_scratchpad(
            session_id, cursor=cursor, heading=heading, status=status_value
        )
    except OSError:
        pass


def _resegment(chunks: list[str]) -> list[str]:
    """Split remaining chunks to sentence granularity for the slower verb."""
    finer: list[str] = []
    for chunk in chunks:
        parts = [part for part in _SENTENCE_SPLIT.split(chunk) if part.strip()]
        finer.extend(parts or [chunk])
    return finer


class UtteranceTranscriber:
    """One loaded Whisper model reused for every utterance of the session.

    Tier selection follows directive D-1 (auto-detected, GPU first) and any
    fallback from the detected tier is reported through *on_status* — never
    a silent quality cut (D-2). Transcription runs with the Silero VAD
    filter so silence never hallucinates phantom phrases.
    """

    def __init__(
        self,
        device: str = "auto",
        model: str | None = None,
        on_status: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        status = on_status or (lambda message: None)
        self._requested_device = device
        self._model_override = model
        self._status = status
        self._cancelled = cancelled or (lambda: False)
        tier = detect_tier(device, model)
        try:
            self._check_cancelled()
            self._whisper = self._load(tier)
            self._check_cancelled()
        except CaptureCancelled:
            raise
        except Exception as exc:
            if tier.device != "cuda" or device == "cuda":
                raise ListenError(
                    f"could not load STT tier ({tier.describe()}): {exc}"
                ) from exc
            fallback = detect_tier("cpu", model)
            status(
                f"stt tier degraded: {tier.describe()} failed ({exc}); "
                f"falling back to {fallback.describe()}"
            )
            try:
                self._check_cancelled()
                self._whisper = self._load(fallback)
                self._check_cancelled()
            except CaptureCancelled:
                raise
            except Exception as fallback_exc:
                raise ListenError(
                    f"could not load STT tier ({fallback.describe()}): {fallback_exc}"
                ) from fallback_exc
            tier = fallback
        self.tier = tier
        status(f"stt tier: {tier.describe()}")

    @staticmethod
    def _load(tier):
        from faster_whisper import WhisperModel

        return WhisperModel(
            tier.model,
            device=tier.device,
            compute_type=tier.compute_type,
            download_root=str(models_dir()),
        )

    def transcribe(self, audio) -> str:
        try:
            self._check_cancelled()
            return self._transcribe_text(audio)
        except CaptureCancelled:
            raise
        except Exception as exc:
            if self.tier.device != "cuda" or self._requested_device == "cuda":
                raise ListenError(
                    f"transcription failed on {self.tier.describe()}: {exc}"
                ) from exc
            fallback = detect_tier("cpu", self._model_override)
            self._status(
                f"stt tier degraded: {self.tier.describe()} failed ({exc}); "
                f"falling back to {fallback.describe()}"
            )
            try:
                self._check_cancelled()
                self._whisper = self._load(fallback)
                self._check_cancelled()
                self.tier = fallback
                return self._transcribe_text(audio)
            except CaptureCancelled:
                raise
            except Exception as fallback_exc:
                raise ListenError(
                    f"transcription failed on {fallback.describe()}: {fallback_exc}"
                ) from fallback_exc

    def _transcribe_text(self, audio) -> str:
        segments = self._decode(audio)
        parts: list[str] = []
        iterator = iter(segments)
        while True:
            # faster-whisper returns a lazy generator.  Check both sides of
            # ``next`` so cancellation requested during segment decoding is
            # honored before any later segment is consumed.
            self._check_cancelled()
            try:
                segment = next(iterator)
            except StopIteration:
                break
            self._check_cancelled()
            part = segment.text.strip()
            if part:
                parts.append(part)
        return " ".join(parts)

    def _decode(self, audio):
        self._check_cancelled()
        segments, _info = self._whisper.transcribe(
            audio,
            beam_size=5,
            hotwords=HOTWORDS,
            vad_filter=True,
        )
        self._check_cancelled()
        return segments

    def _check_cancelled(self) -> None:
        if self._cancelled():
            raise CaptureCancelled("transcription cancelled")


def _sounddevice():
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise ListenError(f"microphone capture unavailable ({exc})") from exc
    return sounddevice


def _numpy():
    import numpy

    return numpy


class _RawKeys:
    """Dependency-free key reads on the listen process's own terminal.

    POSIX terminals use cbreak mode and ``select``. Native Windows consoles use
    ``msvcrt`` and need no terminal-mode changes. Imports stay inside their
    platform branches so importing this module works on either platform.
    """

    def __init__(self) -> None:
        if not sys.stdin.isatty():
            raise ListenError(
                "push-to-talk and push-toggle need an interactive terminal; "
                "use --mode always-on when running without one"
            )
        self._windows = _is_windows()
        self._fd = -1 if self._windows else sys.stdin.fileno()
        self._saved = None

    def __enter__(self) -> "_RawKeys":
        if self._windows:
            return self
        import termios
        import tty

        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._saved is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def read_key(self, timeout: float | None) -> str | None:
        if self._windows:
            return self._read_windows_key(timeout)

        import select

        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return None
        data = os.read(self._fd, 1)
        if data in (b"\x03", b"\x04"):
            raise KeyboardInterrupt
        return data.decode("utf-8", errors="ignore")

    @staticmethod
    def _read_windows_key(timeout: float | None) -> str | None:
        import msvcrt

        if timeout is None:
            key = msvcrt.getwch()
        else:
            deadline = time.monotonic() + max(timeout, 0.0)
            while not msvcrt.kbhit():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                time.sleep(min(_WINDOWS_KEY_POLL_SECONDS, remaining))
            key = msvcrt.getwch()

        if key in ("\x03", "\x04"):
            raise KeyboardInterrupt
        # Function/arrow keys arrive as a prefix plus scan code. Consume both
        # bytes as one event so push-toggle does not immediately stop again.
        if key in ("\x00", "\xe0"):
            key += msvcrt.getwch()
        return key

    def drain(self) -> None:
        while self.read_key(0) is not None:
            pass

    def is_pressed(self, key: str) -> bool | None:
        """Return whether *key* is physically held on Windows.

        Native Windows exposes key-up state through ``GetAsyncKeyState``.  The
        POSIX terminal stream has no key-up events, so callers receive ``None``
        there and retain the existing repeat-gap fallback.
        """
        if not self._windows:
            return None

        import ctypes

        user32 = getattr(ctypes, "windll").user32
        vk_key_scan = user32.VkKeyScanW
        vk_key_scan.argtypes = [ctypes.c_wchar]
        vk_key_scan.restype = ctypes.c_short
        map_virtual_key = user32.MapVirtualKeyW
        map_virtual_key.argtypes = [ctypes.c_uint, ctypes.c_uint]
        map_virtual_key.restype = ctypes.c_uint
        get_async_key_state = user32.GetAsyncKeyState
        get_async_key_state.argtypes = [ctypes.c_int]
        get_async_key_state.restype = ctypes.c_short

        if len(key) == 1:
            mapped = vk_key_scan(key)
            if mapped in (-1, 0xFFFF):
                return None
            virtual_key = mapped & 0xFF
        elif len(key) == 2 and key[0] in ("\x00", "\xe0"):
            # The second character returned by getwch() is the scan code.
            scan_code = ord(key[1])
            if key[0] == "\xe0":
                # MAPVK_VSC_TO_VK_EX expects the extended-key marker in the
                # high byte (for example Up is E0 48, represented as 0xE048).
                scan_code |= 0xE000
            virtual_key = map_virtual_key(scan_code, 3)
            if not virtual_key:
                return None
        else:
            return None
        return bool(get_async_key_state(virtual_key) & 0x8000)


def _rms(block) -> float:
    numpy = _numpy()
    return float(numpy.sqrt(numpy.mean(numpy.square(block.astype(numpy.float64)))))


def _finish(chunks, minimum_seconds: float = _MIN_UTTERANCE_SECONDS):
    numpy = _numpy()
    if not chunks:
        return None
    audio = numpy.concatenate(chunks).reshape(-1)
    if audio.shape[0] < int(minimum_seconds * SAMPLE_RATE):
        return None
    return audio


def _wait_for_trigger(keys: _RawKeys, trigger_key: str | None) -> str | None:
    while True:
        key = keys.read_key(None)
        if key is None or trigger_key is None or key == trigger_key:
            return key


def _report_level(block, on_level: Callable[[float], None] | None) -> None:
    if on_level is not None:
        on_level(_rms(block))


def _record_push_to_talk(
    keys: _RawKeys,
    trigger_key: str | None = None,
    on_level: Callable[[float], None] | None = None,
    on_recording: Callable[[], None] | None = None,
) -> "object | None":
    """Record while a key is held: terminal auto-repeat keeps the take alive,
    and a repeat gap longer than the release window ends it on POSIX. Native
    Windows polls the physical key state so its configurable repeat delay cannot
    truncate the start of an utterance."""
    keys.drain()
    active_key = _wait_for_trigger(keys, trigger_key)
    if active_key is None:
        return None
    physical_state = keys.is_pressed(active_key)
    sounddevice = _sounddevice()
    blocksize = int(_BLOCK_SECONDS * SAMPLE_RATE)
    chunks = []
    last_key = time.monotonic()
    started = last_key
    if on_recording is not None:
        on_recording()
    with sounddevice.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=blocksize
    ) as stream:
        while True:
            block, _overflowed = stream.read(blocksize)
            chunks.append(block.copy())
            _report_level(block, on_level)
            if physical_state is None:
                while keys.read_key(0) is not None:
                    last_key = time.monotonic()
            else:
                physical_state = keys.is_pressed(active_key)
            now = time.monotonic()
            if physical_state is False:
                break
            if physical_state is None and now - last_key > _KEY_RELEASE_SECONDS:
                break
            if now - started > _MAX_UTTERANCE_SECONDS:
                break
    keys.drain()
    return _finish(chunks)


def _record_push_toggle(
    keys: _RawKeys,
    trigger_key: str | None = None,
    on_level: Callable[[float], None] | None = None,
    on_recording: Callable[[], None] | None = None,
    start_immediately: bool = False,
) -> "object | None":
    """Tap to start recording, tap again to send."""
    keys.drain()
    if not start_immediately and _wait_for_trigger(keys, trigger_key) is None:
        return None
    keys.drain()
    sounddevice = _sounddevice()
    blocksize = int(_BLOCK_SECONDS * SAMPLE_RATE)
    chunks = []
    if on_recording is not None:
        on_recording()
    with sounddevice.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=blocksize
    ) as stream:
        while True:
            block, _overflowed = stream.read(blocksize)
            chunks.append(block.copy())
            _report_level(block, on_level)
            key = keys.read_key(0)
            if key is not None and (trigger_key is None or key == trigger_key):
                break
    keys.drain()
    return _finish(chunks)


def _record_always_on(
    on_level: Callable[[float], None] | None = None,
    on_recording: Callable[[], None] | None = None,
    stop_event: "threading.Event | None" = None,
    wake_check: "Callable[[], bool] | None" = None,
) -> "object | None":
    """Hands-free capture: calibrate the noise floor, trigger on speech
    energy, and end the utterance after a trailing pause.

    A set *stop_event* aborts at the next audio block so an idle hands-free
    session — blocked on the microphone rather than on a key source — can still
    unwind promptly when the dashboard asks it to stop.

    *wake_check* re-reads wake enablement roughly once a second during the
    pre-trigger wait (while no speech is captured yet); when it reports wake
    switched on, this ungated pass returns None so the next capture re-gates
    through the wake detector instead of running an unbounded ungated window.
    """
    def stopping() -> bool:
        return stop_event is not None and stop_event.is_set()
    sounddevice = _sounddevice()
    blocksize = int(_BLOCK_SECONDS * SAMPLE_RATE)
    preroll_blocks = max(1, int(_PREROLL_SECONDS / _BLOCK_SECONDS))
    hang_blocks = max(1, int(_SILENCE_HANG_SECONDS / _BLOCK_SECONDS))
    max_blocks = int(_MAX_UTTERANCE_SECONDS / _BLOCK_SECONDS)
    wake_poll_blocks = max(1, int(1.0 / _BLOCK_SECONDS))
    blocks_since_wake_poll = 0
    with sounddevice.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=blocksize
    ) as stream:
        ambient_samples = []
        for _ in range(max(1, int(_CALIBRATION_SECONDS / _BLOCK_SECONDS))):
            if stopping():
                return None
            block, _overflowed = stream.read(blocksize)
            ambient_samples.append(_rms(block))
        noise_floor = sum(ambient_samples) / len(ambient_samples)
        threshold = max(noise_floor * 3.0, 0.01)
        preroll: list = []
        chunks: list = []
        silent_blocks = 0
        while True:
            if stopping():
                return None
            if not chunks and wake_check is not None:
                blocks_since_wake_poll += 1
                if blocks_since_wake_poll >= wake_poll_blocks:
                    blocks_since_wake_poll = 0
                    if wake_check():
                        return None
            block, _overflowed = stream.read(blocksize)
            level = _rms(block)
            if chunks:
                _report_level(block, on_level)
            if not chunks:
                preroll.append(block.copy())
                if len(preroll) > preroll_blocks:
                    preroll.pop(0)
                if level >= threshold:
                    chunks = list(preroll)
                    silent_blocks = 0
                    if on_recording is not None:
                        on_recording()
                    _report_level(block, on_level)
                else:
                    noise_floor = 0.95 * noise_floor + 0.05 * level
                    threshold = max(noise_floor * 3.0, 0.01)
                continue
            chunks.append(block.copy())
            silent_blocks = silent_blocks + 1 if level < threshold else 0
            if silent_blocks >= hang_blocks or len(chunks) >= max_blocks:
                break
    return _finish(chunks)


def _ssh_base(remote: str) -> list[str]:
    """Build a platform-safe SSH invocation.

    POSIX OpenSSH keeps its low-latency control socket. Native Windows omits
    Unix control-socket settings, which are not consistently supported by the
    in-box OpenSSH client.
    """
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if not _is_windows():
        command += [
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=~/.ssh/cm-talktomejohnny-%r@%h:%p",
            "-o", "ControlPersist=600",
        ]
    return command + [remote]


def _remote_shell_command(inner: str, remote_cwd: str | None = None) -> str:
    """Wrap *inner* for a remote login shell, optionally changing directory.

    The target directory and complete shell program are quoted independently:
    this preserves spaces/metacharacters without allowing a configured path to
    become shell syntax.
    """
    if remote_cwd:
        inner = f"cd -- {shlex.quote(remote_cwd)} && {inner}"
    return f"bash -lc {shlex.quote(inner)}"


def build_claude_command(
    text,
    session_id,
    *,
    remote=None,
    remote_cwd=None,
    stream=False,
    permission="off",
) -> list[str]:
    fmt = "stream-json" if stream else "json"
    if remote:
        inner = f"claude -p {shlex.quote(text)} --output-format {fmt}"
        if stream:
            inner += " --verbose"
        if session_id:
            inner += f" --resume {shlex.quote(session_id)}"
        if permission == "skip":
            inner += " --dangerously-skip-permissions"
        elif permission in ("acceptEdits", "bypassPermissions"):
            inner += f" --permission-mode {permission}"
        return _ssh_base(remote) + [_remote_shell_command(inner, remote_cwd)]

    claude = shutil.which("claude")
    if claude is None:
        raise ListenError(
            "the claude CLI is not on PATH; install Claude Code, pass "
            "--remote user@host to run it on a server, or use --tmux-pane"
        )
    command = [claude, "-p", text, "--output-format", fmt]
    if stream:
        command += ["--verbose"]
    if session_id:
        command += ["--resume", session_id]
    if permission == "skip":
        command += ["--dangerously-skip-permissions"]
    elif permission in ("acceptEdits", "bypassPermissions"):
        command += ["--permission-mode", permission]
    return command


def _prompt_claude(
    text: str,
    session_id: str | None,
    remote: str | None = None,
    remote_cwd: str | None = None,
    on_wait: Callable[[], None] | None = None,
    on_event: "Callable[[dict], None] | None" = None,
    stop_event: "threading.Event | None" = None,
    permission: str = "off",
) -> tuple[str, str | None]:
    """Primary injection path (D-3): drive a claude -p session, resuming it
    across turns; the reply arrives as structured JSON.

    With *remote* set (``user@host``) the claude process runs on that host over
    SSH — the microphone, speech-to-text and spoken reply stay local, while
    Claude Code runs on the server. The remote command runs through a login
    shell so the server's normal PATH finds the ``claude`` CLI. When
    *remote_cwd* is set, Claude starts in that safely quoted project directory.

    When *on_event* is given, the session runs with ``--output-format
    stream-json`` and every activity event (tool calls, thinking, results) is
    surfaced live for the dashboard's session mirror; the spoken reply still
    comes only from the authoritative final ``result`` event.
    """
    command = build_claude_command(
        text,
        session_id,
        remote=remote,
        remote_cwd=remote_cwd,
        stream=on_event is not None,
        permission=permission,
    )
    if on_event is not None:
        return _consume_stream(command, on_event, on_wait, remote, stop_event)
    try:
        result = _run_captured(command, on_wait=on_wait)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ListenError(f"could not run claude -p: {exc}") from exc
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    if result.returncode != 0:
        detail = stderr.strip() or f"exit {result.returncode}"
        where = f" on {remote}" if remote else ""
        raise ListenError(
            f"claude -p failed{where}: {detail}"
            + (
                "  (remote needs passwordless SSH keys and the claude CLI installed there)"
                if remote
                else ""
            )
        )
    if not stdout.strip():
        raise ListenError("claude -p returned no JSON output")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ListenError(f"claude -p returned non-JSON output: {exc}") from exc
    if not isinstance(payload, dict):
        raise ListenError("claude -p returned an unexpected JSON shape")
    reply = payload.get("result")
    new_session = payload.get("session_id")
    return (
        reply if isinstance(reply, str) else "",
        new_session if isinstance(new_session, str) else session_id,
    )


def _run_captured(
    command: list[str], on_wait: Callable[[], None] | None = None
) -> subprocess.CompletedProcess:
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if on_wait is None:
        result = subprocess.run(command, **kwargs)
        return subprocess.CompletedProcess(
            command,
            result.returncode,
            result.stdout if isinstance(result.stdout, str) else "",
            result.stderr if isinstance(result.stderr, str) else "",
        )

    process = subprocess.Popen(command, **kwargs)
    captured: list[str | None] = [None, None]
    failure: list[BaseException] = []

    def communicate() -> None:
        try:
            captured[:] = process.communicate()
        except BaseException as exc:
            failure.append(exc)

    worker = threading.Thread(target=communicate, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            on_wait()
            worker.join(0.1)
    except BaseException:
        process.terminate()
        worker.join(2.0)
        if worker.is_alive():
            process.kill()
            worker.join()
        raise
    if failure:
        raise failure[0]
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        captured[0] if isinstance(captured[0], str) else "",
        captured[1] if isinstance(captured[1], str) else "",
    )


def _reap(process: "subprocess.Popen", timeout: float = 2.0) -> None:
    """Terminate a child and guarantee it is reaped: SIGTERM, a bounded wait,
    then SIGKILL so a signal-resistant process can never hang the caller."""
    process.terminate()
    try:
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _consume_stream(
    command: list[str],
    on_event: "Callable[[dict], None]",
    on_wait: Callable[[], None] | None,
    remote: str | None,
    stop_event: "threading.Event | None" = None,
) -> tuple[str, str | None]:
    """Run a ``claude -p --output-format stream-json`` session, surfacing each
    NDJSON event live via *on_event*, and return only the authoritative final
    ``result`` text plus its session id.

    stdout is read on a side thread and fed through a queue so a set
    *stop_event* interrupts a mid-stream turn (the reader would otherwise block
    until Claude finished). stderr is drained on its own thread so a full pipe
    can never deadlock; unknown event types and malformed lines are tolerated;
    the child is always reaped with a bounded wait. No PTY is used, so the
    stream stays clean NDJSON even over SSH.
    """
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ListenError(f"could not run claude -p: {exc}") from exc

    stderr_chunks: list[str] = []

    def drain_stderr() -> None:
        if process.stderr is not None:
            for chunk in process.stderr:
                stderr_chunks.append(chunk)

    sentinel = object()
    stdout_lines: "queue.Queue[object]" = queue.Queue()

    def read_stdout() -> None:
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    stdout_lines.put(line)
        finally:
            stdout_lines.put(sentinel)

    stderr_worker = threading.Thread(target=drain_stderr, daemon=True)
    reader = threading.Thread(target=read_stdout, daemon=True)
    stderr_worker.start()
    reader.start()

    result_text = ""
    session_id: str | None = None
    is_error = False
    outcome = "eof"
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                outcome = "stopped"
                break
            try:
                line = stdout_lines.get(timeout=0.1)
            except queue.Empty:
                if on_wait is not None:
                    on_wait()
                continue
            if line is sentinel:
                outcome = "eof"
                break
            if on_wait is not None:
                on_wait()
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            try:
                on_event(event)
            except Exception:
                pass
            etype = event.get("type")
            new_session = event.get("session_id")
            if isinstance(new_session, str) and new_session:
                session_id = new_session
            if etype == "result":
                is_error = bool(event.get("is_error"))
                reply = event.get("result")
                result_text = reply if isinstance(reply, str) else ""
                outcome = "result"
                break
    except BaseException:
        _reap(process)
        reader.join(1.0)
        stderr_worker.join(1.0)
        raise

    if outcome == "eof":
        process.wait()  # ended on its own; keep the real exit code
    else:
        _reap(process)  # stopped or done early — the child may still be running
    reader.join(1.0)
    stderr_worker.join(1.0)

    if outcome == "stopped":
        return "", session_id
    if outcome == "eof":
        detail = "".join(stderr_chunks).strip() or f"exit {process.returncode}"
        where = f" on {remote}" if remote else ""
        if process.returncode not in (0, None):
            raise ListenError(
                f"claude -p failed{where}: {detail}"
                + (
                    "  (remote needs passwordless SSH keys and the claude CLI installed there)"
                    if remote
                    else ""
                )
            )
        raise ListenError("claude -p stream ended without a result event")
    if is_error:
        raise ListenError(result_text.strip() or "claude reported an error")
    return result_text, session_id


def _send_tmux(pane: str, text: str, remote: str | None = None) -> None:
    """Alternate injection path (D-3): type into a live interactive TUI.

    With *remote* set, the ``tmux send-keys`` runs on that host over SSH, so a
    local voice loop can type into a Claude Code TUI running on the server.
    """
    if remote:
        inner = (
            f"tmux send-keys -t {shlex.quote(pane)} -l {shlex.quote(text)} && "
            f"tmux send-keys -t {shlex.quote(pane)} Enter"
        )
        command = _ssh_base(remote) + [_remote_shell_command(inner)]
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as exc:
            raise ListenError(
                f"remote tmux send-keys to pane {pane!r} on {remote} failed: {exc}"
            ) from exc
        return
    if shutil.which("tmux") is None:
        raise ListenError("tmux is not on PATH; drop --tmux-pane to drive claude -p")
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
    except subprocess.CalledProcessError as exc:
        raise ListenError(f"tmux send-keys to pane {pane!r} failed: {exc}") from exc


def run_listen(
    mode: str,
    session_id: str | None,
    tmux_pane: str | None,
    device: str,
    model: str | None,
    once: bool,
    echo: Callable[[str], None],
    speak: Callable[[str], None],
    status: Callable[[str], None],
    remote: str | None = None,
    remote_cwd: str | None = None,
    on_level: Callable[[float], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    on_progress: Callable[[], None] | None = None,
    trigger_key: str | None = None,
    start_recording: bool = False,
    keys: "_RawKeys | None" = None,
    stop_event: "threading.Event | None" = None,
    on_event: "Callable[[dict], None] | None" = None,
    on_session: "Callable[[str], None] | None" = None,
    permission: str = "off",
) -> None:
    """Drive the capture → transcribe → inject → reply loop until interrupted.

    When *remote* (``user@host``) is set, Claude Code runs on that host over
    SSH while the microphone, transcription and spoken reply stay on this
    machine — the remote/SSH pattern (voice where you sit, compute on the
    server). *remote_cwd* selects the project directory for remote ``claude``
    sessions.

    *keys* lets a caller inject its own key source instead of opening a raw
    terminal reader. The interactive dashboard passes a queue-backed source so
    Textual keeps sole ownership of the TTY while still feeding the trigger key.
    *stop_event*, when set, unwinds the loop cleanly between (and during)
    captures — the graceful path for hands-free ``always-on`` sessions, whose
    microphone read never sees the key source's shutdown sentinel.
    """
    set_phase = on_phase or (lambda _phase: None)
    set_phase("starting")
    if stop_event is not None and stop_event.is_set():
        return
    transcriber = UtteranceTranscriber(device, model, on_status=status)
    if stop_event is not None and stop_event.is_set():
        return
    if remote:
        status(f"remote: Claude Code runs on {remote} over SSH; voice stays local")
        if remote_cwd:
            status(f"remote project directory: {remote_cwd}")
    prompts = {
        "always-on": "listening (hands-free); Ctrl-C to stop",
        "push-to-talk": "hold any key to talk; release to send; Ctrl-C to stop",
        "push-toggle": "tap any key to talk; tap again to send; Ctrl-C to stop",
    }
    status(prompts[mode])
    if keys is None and mode in ("push-to-talk", "push-toggle"):
        keys = _RawKeys()
    first_capture = True
    wake_state: dict = {}

    barge_opted_in = _setting(config.barge_in_enabled, False)
    barge = barge_in_active(barge_opted_in, headphones_present()) if barge_opted_in else False
    if barge:
        status("barge-in armed: speak over a reply to interrupt it (headphones detected)")
    elif barge_opted_in:
        status("barge-in is on but no headphones were detected; staying half-duplex")

    catalog: list[dict] = []
    # Bootstrap a usable roster from the persisted flags so the very first
    # utterance can resolve; the first system/init event refreshes it below.
    try:
        catalog[:] = command_catalog.roster_from_saved()
    except Exception:
        pass
    pending_fire: "tuple[dict, str] | None" = None
    pending_namespace: "tuple[dict, str] | None" = None
    clarify: dict | None = None
    approved_namespaces: set = set()
    pending_text: str | None = None

    def handle_event(event: dict) -> None:
        # The session's system/init event advertises the voice-fireable
        # commands; refresh the catalog while preserving the user-owned flags.
        # Installed unconditionally so a plain CLI session populates the
        # catalog too; an external callback chains behind it.
        if (
            isinstance(event, dict)
            and event.get("type") == "system"
            and event.get("subtype") == "init"
        ):
            try:
                catalog[:] = command_catalog.load_catalog(event)
                command_catalog.save_flags(catalog)
            except Exception:
                pass
        if on_event is not None:
            on_event(event)

    stream_events = handle_event

    def capture():
        nonlocal first_capture, keys
        set_phase("ready")
        def recording() -> None:
            set_phase("recording")
        if mode == "always-on":
            disposition = _wake_gate(speak, status, stop_event, wake_state)
            if disposition is WakeDisposition.STOP:
                return None
            if disposition is WakeDisposition.MANUAL_FALLBACK:
                if keys is None:
                    try:
                        keys = _RawKeys()
                    except ListenError as exc:
                        raise ListenError(
                            "wake-word manual fallback needs an interactive terminal"
                        ) from exc
                with keys:
                    return _record_push_to_talk(
                        keys,
                        trigger_key=trigger_key,
                        on_level=on_level,
                        on_recording=recording,
                    )
            # An ungated pass re-reads wake enablement mid-wait: if the operator
            # switches wake on, abort so the next capture re-gates on the phrase.
            wake_check = (
                (lambda: bool(_setting(config.wake_word_enabled, False)))
                if disposition is WakeDisposition.OFF_UNGATED
                else None
            )
            return _record_always_on(
                on_level=on_level,
                on_recording=recording,
                stop_event=stop_event,
                wake_check=wake_check,
            )
        with keys:
            if mode == "push-to-talk":
                return _record_push_to_talk(
                    keys,
                    trigger_key=trigger_key,
                    on_level=on_level,
                    on_recording=recording,
                )
            record_now = start_recording and first_capture
            first_capture = False
            return _record_push_toggle(
                keys,
                trigger_key=trigger_key,
                on_level=on_level,
                on_recording=recording,
                start_immediately=record_now,
            )

    def deliver(text: str):
        """Speak *text*; returns the operator's interrupting utterance as
        captured audio when barge-in halted the playback, else None."""
        if barge:
            return _speak_interruptible(speak, text, stop_event=stop_event)
        speak(text)
        return None

    def deliver_reply(dialogue: str, active_session: str | None) -> str | None:
        """The voice-conveyance checkpoint loop.

        Long replies are spoken gradually: after each chunk the cursor,
        heading, and status persist to the session scratchpad, the microphone
        reopens, and an exact feedback verb steers delivery. A content-bearing
        utterance — heard at a checkpoint or barged in over the playback — is
        returned so it resumes through the same session as the next ordinary
        turn: the interruption continues the conversation, never repeats it.
        """
        chunks = conveyance.chunk(dialogue)
        if once or len(chunks) <= 1:
            set_phase("speaking")
            interrupt = deliver(dialogue)
            if interrupt is None:
                return None
            set_phase("transcribing")
            heard = transcriber.transcribe(interrupt).strip()
            # A bare verb has nothing left to steer here — playback already
            # stopped; content flows back into the same resumed session.
            if not heard or detect_verb(heard) is not None:
                return None
            return heard
        cursor = 0
        while cursor < len(chunks):
            current = chunks[cursor]
            _write_checkpoint(active_session, cursor, _chunk_heading(current), "delivering")
            set_phase("speaking")
            interrupt = deliver(current)
            if interrupt is None and cursor == len(chunks) - 1:
                break
            if stop_event is not None and stop_event.is_set():
                _write_checkpoint(active_session, cursor, _chunk_heading(current), "stopped")
                return None
            # A barged-in utterance is the checkpoint utterance — same verbs,
            # same content routing — without waiting for the chunk to finish.
            audio = interrupt if interrupt is not None else capture()
            if audio is None:
                if stop_event is not None and stop_event.is_set():
                    _write_checkpoint(
                        active_session, cursor, _chunk_heading(current), "stopped"
                    )
                    return None
                cursor += 1
                continue
            set_phase("transcribing")
            heard = transcriber.transcribe(audio).strip()
            if not heard:
                cursor += 1
                continue
            verb = detect_verb(heard)
            if verb is None:
                _write_checkpoint(active_session, cursor, _chunk_heading(current), "steered")
                return heard
            echo(f"you: {verb}")
            if verb == "continue":
                cursor += 1
            elif verb == "repeat":
                continue
            elif verb == "back":
                cursor = max(0, cursor - 1)
            elif verb == "skip":
                cursor += 2
            elif verb == "stop":
                _write_checkpoint(active_session, cursor, _chunk_heading(current), "stopped")
                return None
            elif verb == "slower":
                chunks[cursor:] = _resegment(chunks[cursor:])
            elif verb == "expand":
                _write_checkpoint(active_session, cursor, _chunk_heading(current), "steered")
                return "Expand on the point you just made, in more detail."
            elif verb == "steer":
                _write_checkpoint(active_session, cursor, _chunk_heading(current), "steered")
                return None
        _write_checkpoint(
            active_session, len(chunks) - 1, _chunk_heading(chunks[-1]), "done"
        )
        return None

    def say(line: str) -> None:
        status(line)
        set_phase("speaking")
        speak(line)
        set_phase("ready")

    def fire(record: dict, args: str) -> str:
        fired = _fire_text(record, args)
        record["fire_count"] = int(record.get("fire_count", 0)) + 1
        try:
            command_catalog.save_flags(catalog)
        except OSError:
            pass
        echo(f"you: {fired}")
        status(f"firing {fired} into the session")
        return fired

    def advance(outcome, original, request, history, rounds):
        """Route a typed outcome: queue the confirmation, ask one bounded
        clarification, or return the text to send as ordinary content."""
        nonlocal pending_fire, clarify
        if outcome.kind == "complete":
            pending_fire = (outcome.record, outcome.args)
            say(f"Firing {_fire_text(outcome.record, outcome.args)}. Say go, or cancel.")
            return None
        if outcome.kind == "missing_slot" and rounds < _MAX_CLARIFICATIONS:
            slot = outcome.missing_slots[0]
            clarify = {
                "original": original,
                "request": request,
                "args": outcome.args,
                "kind": "missing_slot",
                "label": f"asked for {slot}",
                "alternatives": (),
                "history": history,
                "rounds": rounds,
            }
            say(f"I need the {slot} for {_fire_text(outcome.record, '')}. Say it, or cancel.")
            return None
        if outcome.kind == "ambiguous" and rounds < _MAX_CLARIFICATIONS and outcome.alternatives:
            choices = tuple(outcome.alternatives[: intent.MAX_SPOKEN_ALTERNATIVES])
            clarify = {
                "original": original,
                "request": request,
                "args": outcome.args,
                "kind": "ambiguous",
                "label": f"asked to choose between {', '.join(choices)}",
                "alternatives": choices,
                "history": history,
                "rounds": rounds,
            }
            say(f"Which command: {_spoken_choices(choices)}? Say the name, or cancel.")
            return None
        if rounds:
            status("clarification exhausted; sending the original request as content")
        return original

    while True:
        if stop_event is not None and stop_event.is_set():
            return
        if pending_text is not None:
            text = pending_text
            pending_text = None
        else:
            audio = capture()
            if audio is None:
                continue
            set_phase("transcribing")
            text = transcriber.transcribe(audio).strip()
            if not text:
                continue
        echo(f"you: {text}")
        if tmux_pane:
            set_phase("thinking")
            _send_tmux(tmux_pane, text, remote=remote)
            status(f"sent to tmux pane {tmux_pane}; the live TUI owns the reply")
            set_phase("ready")
            if once:
                return
            continue
        # Voice-activated commands: resolve to a typed outcome, clarify within
        # the bounded budget, confirm, then fire the confirmed command through
        # the same resumed session with the operator posture.
        normalized = " ".join(text.casefold().split())
        if pending_namespace is not None:
            record, args = pending_namespace
            pending_namespace = None
            if normalized in _CANCEL_WORDS:
                status(f"cancelled /{command_catalog.qualified_id(record)}")
                set_phase("ready")
                if once:
                    return
                continue
            if normalized in _FIRE_WORDS:
                approved_namespaces.add(record.get("namespace") or "")
                text = fire(record, args)
            # Anything else is ordinary content — fall through unchanged.
        elif pending_fire is not None:
            record, args = pending_fire
            pending_fire = None
            if normalized in _CANCEL_WORDS:
                status(f"cancelled /{command_catalog.qualified_id(record)}")
                set_phase("ready")
                if once:
                    return
                continue
            if normalized in _FIRE_WORDS:
                namespace = record.get("namespace") or ""
                if (
                    _setting(config.command_namespace_policy, "allow-all")
                    == "ask-first-use"
                    and namespace not in approved_namespaces
                ):
                    # The first fire of each namespace this session needs an
                    # explicit extra spoken yes; the grant is session-scoped.
                    pending_namespace = (record, args)
                    say(
                        f"First {namespace or 'top-level'} command this session. "
                        "Say yes to allow."
                    )
                    continue
                text = fire(record, args)
            # Anything else is ordinary content — fall through unchanged.
        elif clarify is not None:
            round_state, clarify = clarify, None
            if normalized in _CANCEL_WORDS:
                status("cancelled the command request")
                set_phase("ready")
                if once:
                    return
                continue
            history = round_state["history"] + [(round_state["label"], text)]
            rounds = round_state["rounds"] + 1
            allowed, _blocked = _allowed_records(catalog)
            outcome = None
            if round_state["kind"] == "ambiguous":
                outcome = _pick_alternative(
                    text, round_state["alternatives"], allowed, round_state["args"]
                )
            if outcome is None:
                outcome = _classify_intent(
                    round_state["request"], allowed, status, history
                )
            handled = advance(
                outcome, round_state["original"], round_state["request"], history, rounds
            )
            if handled is None:
                # Under --once the session stays alive through confirmation,
                # cancellation, and namespace approval; it exits only once the
                # resulting fire or content turn actually completes below.
                continue
            text = handled  # exhausted: the ORIGINAL utterance flows as content
        else:
            outcome = _resolve_command(text, catalog, status)
            if outcome.kind != "no_match":
                handled = advance(outcome, text, _command_request(text), [], 0)
                if handled is None:
                    continue
                text = handled
        set_phase("thinking")
        reply, session_id = _prompt_claude(
            text,
            session_id,
            remote=remote,
            remote_cwd=remote_cwd,
            on_wait=on_progress,
            on_event=stream_events,
            stop_event=stop_event,
            permission=permission,
        )
        if on_session is not None and session_id is not None:
            on_session(session_id)
        dialogue = speakable(reply)
        if dialogue:
            echo(f"claude: {dialogue}")
            pending_text = deliver_reply(dialogue, session_id)
        set_phase("ready")
        if once:
            return
