"""The production voice loop wiring: catalog-driven command firing with typed
intent outcomes, bounded clarification, namespace policy, wake-word gating,
the conveyance checkpoint loop, and barge-in playback selection."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from talktomeclaude import command_catalog, config, listen


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(
            os.environ, {"TALKTOMECLAUDE_CONFIG_DIR": self.tmp.name}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        # Most dispatcher tests isolate command parsing from policy gating.
        # NamespacePolicyTests override this explicitly for the safer default.
        config.set_command_namespace_policy("allow-all")


class VoiceCommandDispatchTests(_Isolated):
    def _run(self, utterances: list[str], prompts: list[str], spoken: list[str]) -> None:
        stop_event = threading.Event()
        takes = iter(utterances)

        def next_take():
            try:
                next_utterance = next(takes)
            except StopIteration:
                stop_event.set()
                return None
            return next_utterance

        captured: list[str | None] = []

        def record(**_kwargs):
            captured.append(next_take())
            return captured[-1]

        def fake_prompt(text, session_id, **kwargs):
            prompts.append(text)
            handler = kwargs.get("on_event")
            if handler is not None:
                handler(
                    {
                        "type": "system",
                        "subtype": "init",
                        "slash_commands": ["kiln-fire", "model", "help"],
                    }
                )
            return ("ok", "sess-1")

        transcriber = mock.Mock()
        transcriber.transcribe.side_effect = lambda audio: audio or ""

        with mock.patch.object(
            listen, "_is_windows", return_value=False
        ), mock.patch.object(
            listen, "_record_always_on", side_effect=lambda **kwargs: next_take()
        ), mock.patch.object(
            listen, "UtteranceTranscriber", return_value=transcriber
        ), mock.patch.object(listen, "_prompt_claude", fake_prompt):
            listen.run_listen(
                mode="always-on",
                session_id=None,
                tmux_pane=None,
                device="cpu",
                model=None,
                once=False,
                echo=lambda _line: None,
                speak=spoken.append,
                status=lambda _line: None,
                stop_event=stop_event,
                on_event=lambda _event: None,
            )

    def test_exact_command_name_confirms_then_fires_into_the_same_session(self) -> None:
        prompts: list[str] = []
        spoken: list[str] = []
        self._run(["hello", "kiln-fire", "go"], prompts, spoken)
        self.assertEqual(prompts, ["hello", "/kiln-fire"])
        self.assertTrue(any("Firing /kiln-fire" in line for line in spoken))
        saved = command_catalog.load_saved_flags()
        self.assertEqual(saved[":kiln-fire"]["fire_count"], 1)

    def test_cancel_drops_the_pending_command(self) -> None:
        # The catalog is discovered from the session's init event, so the
        # first (ordinary) turn seeds it before a command can resolve.
        prompts: list[str] = []
        spoken: list[str] = []
        self._run(["hello", "kiln-fire", "cancel"], prompts, spoken)
        self.assertEqual(prompts, ["hello"])
        saved = command_catalog.load_saved_flags()
        self.assertEqual(saved[":kiln-fire"]["fire_count"], 0)

    def test_ordinary_content_never_resolves_without_a_catalog(self) -> None:
        prompts: list[str] = []
        spoken: list[str] = []
        with mock.patch.object(
            listen, "_classify_intent", side_effect=AssertionError("no sub-call")
        ):
            self._run(["what is the capital of france"], prompts, spoken)
        self.assertEqual(prompts, ["what is the capital of france"])


_INIT_EVENT = {
    "type": "system",
    "subtype": "init",
    "slash_commands": ["kiln-fire", "model", "help"],
    "skills": [
        {"name": "commit", "namespace": "git", "description": "Commit staged work", "read_only": False},
        {"name": "deploy", "namespace": "web", "description": "Ship the web app", "read_only": False},
        {"name": "deploy", "namespace": "api", "description": "Ship the api", "read_only": False},
    ],
}


class _LoopHarness(_Isolated):
    """Drive run_listen offline: scripted utterances, a faked working session
    that seeds the catalog from the init event, and scripted intent sub-call
    payloads consumed through the real classify/sanitize pipeline."""

    def run_loop(self, utterances, intents=(), once=False, external_on_event=None):
        out = types.SimpleNamespace(
            prompts=[], spoken=[], statuses=[], sub_prompts=[], sub_commands=[],
            handlers=[],
        )
        stop_event = threading.Event()
        takes = iter(utterances)

        def next_take():
            try:
                return next(takes)
            except StopIteration:
                stop_event.set()
                return None

        def fake_prompt(text, session_id, **kwargs):
            out.prompts.append(text)
            handler = kwargs.get("on_event")
            out.handlers.append(handler)
            if handler is not None:
                handler(_INIT_EVENT)
            return ("ok", "sess-1")

        responses = iter(intents)

        def fake_captured(command, on_wait=None):
            out.sub_commands.append(command)
            out.sub_prompts.append(command[2])
            payload = next(responses)
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"result": json.dumps(payload)}), ""
            )

        transcriber = mock.Mock()
        transcriber.transcribe.side_effect = lambda audio: audio or ""

        with mock.patch.object(
            listen, "_is_windows", return_value=False
        ), mock.patch.object(
            listen, "_record_always_on", side_effect=lambda **kwargs: next_take()
        ), mock.patch.object(
            listen, "UtteranceTranscriber", return_value=transcriber
        ), mock.patch.object(listen, "_prompt_claude", fake_prompt), mock.patch.object(
            listen, "_run_captured", fake_captured
        ):
            listen.run_listen(
                mode="always-on",
                session_id=None,
                tmux_pane=None,
                device="cpu",
                model=None,
                once=once,
                echo=lambda _line: None,
                speak=out.spoken.append,
                status=out.statuses.append,
                stop_event=stop_event,
                on_event=external_on_event,
            )
        out.leftover = list(takes)
        return out


class TypedOutcomeDispatchTests(_LoopHarness):
    def test_missing_slot_prompts_once_then_completes(self) -> None:
        out = self.run_loop(
            ["hello", "run command save my work to git", "fix the login bug", "go"],
            intents=[
                {"command_id": "git:commit", "args": "", "missing_slots": ["message"],
                 "confidence": 0.9, "alternatives": []},
                {"command_id": "git:commit", "args": '-m "fix the login bug"',
                 "missing_slots": [], "confidence": 0.95, "alternatives": []},
            ],
        )
        self.assertTrue(any("I need the message for /git:commit" in line for line in out.spoken))
        self.assertTrue(any('Firing /git:commit -m "fix the login bug"' in line for line in out.spoken))
        self.assertEqual(out.prompts, ["hello", '/git:commit -m "fix the login bug"'])
        self.assertEqual(len(out.sub_prompts), 2)
        self.assertIn("Request: save my work to git", out.sub_prompts[1])
        self.assertIn("Clarification (asked for message): fix the login bug", out.sub_prompts[1])
        for command in out.sub_commands:
            self.assertNotIn("--resume", command)

    def test_ambiguity_is_spoken_capped_at_three_then_disambiguated(self) -> None:
        out = self.run_loop(
            ["hello", "run command ship it to production", "api:deploy", "go"],
            intents=[
                {"command_id": None, "args": "", "missing_slots": [], "confidence": 0.3,
                 "alternatives": ["web:deploy", "api:deploy", "commit", "kiln-fire"]},
            ],
        )
        choice = next(line for line in out.spoken if line.startswith("Which command"))
        self.assertIn("web:deploy, api:deploy, or git:commit", choice)
        self.assertNotIn("kiln-fire", choice)
        self.assertEqual(out.prompts, ["hello", "/api:deploy"])
        self.assertEqual(len(out.sub_prompts), 1)  # the pick was deterministic

    def test_confident_match_with_unresolved_alternatives_stays_ambiguous(self) -> None:
        out = self.run_loop(
            ["hello", "run command ship the changes", "git:commit", "go"],
            intents=[
                {"command_id": "git:commit", "args": "", "missing_slots": [],
                 "confidence": 0.9, "alternatives": ["web:deploy"]},
            ],
        )
        choice = next(line for line in out.spoken if line.startswith("Which command"))
        self.assertIn("git:commit, or web:deploy", choice)
        self.assertEqual(out.prompts, ["hello", "/git:commit"])

    def test_low_confidence_without_alternatives_falls_back_to_content(self) -> None:
        out = self.run_loop(
            ["hello", "run command make it nice"],
            intents=[
                {"command_id": "git:commit", "args": "", "missing_slots": [],
                 "confidence": 0.2, "alternatives": []},
            ],
        )
        self.assertEqual(out.prompts, ["hello", "run command make it nice"])
        self.assertTrue(any("no confident command match" in line for line in out.statuses))

    def test_cancel_mid_clarification_aborts_cleanly(self) -> None:
        out = self.run_loop(
            ["hello", "run command save my work to git", "never mind"],
            intents=[
                {"command_id": "git:commit", "args": "", "missing_slots": ["message"],
                 "confidence": 0.9, "alternatives": []},
            ],
        )
        self.assertEqual(out.prompts, ["hello"])
        self.assertTrue(any("cancelled the command request" in line for line in out.statuses))
        saved = command_catalog.load_saved_flags()
        self.assertEqual(saved["git:commit"]["fire_count"], 0)

    def test_exhaustion_routes_the_original_utterance_as_content(self) -> None:
        slot = {"command_id": "git:commit", "args": "", "missing_slots": ["message"],
                "confidence": 0.9, "alternatives": []}
        out = self.run_loop(
            ["hello", "run command save my work", "alpha", "beta"],
            intents=[slot, dict(slot, missing_slots=["scope"]), dict(slot, missing_slots=["scope"])],
        )
        self.assertEqual(out.prompts, ["hello", "run command save my work"])
        self.assertNotIn("alpha", out.prompts)
        self.assertNotIn("beta", out.prompts)
        self.assertEqual(len(out.sub_prompts), 3)  # initial + two clarification rounds
        self.assertTrue(any("clarification exhausted" in line for line in out.statuses))

    def test_exact_command_carries_trailing_args_to_the_fire(self) -> None:
        out = self.run_loop(["hello", "kiln-fire --dry-run", "go"])
        self.assertTrue(any("Firing /kiln-fire --dry-run" in line for line in out.spoken))
        self.assertEqual(out.prompts, ["hello", "/kiln-fire --dry-run"])

    def test_exact_command_with_remainder_never_fires_empty_args(self) -> None:
        out = self.run_loop(["hello", "kiln-fire --dry-run", "go"])
        self.assertNotIn("/kiln-fire", out.prompts)
        self.assertFalse(any(line.startswith("Firing /kiln-fire.") for line in out.spoken))

    def test_namespace_collision_resolves_by_qualified_identity(self) -> None:
        out = self.run_loop(["hello", "deploy now", "web:deploy", "go"])
        choice = next(line for line in out.spoken if line.startswith("Which command"))
        self.assertIn("web:deploy, or api:deploy", choice)
        self.assertTrue(any("Firing /web:deploy now" in line for line in out.spoken))
        self.assertEqual(out.prompts, ["hello", "/web:deploy now"])
        self.assertEqual(out.sub_prompts, [])
        saved = command_catalog.load_saved_flags()
        self.assertEqual(saved["web:deploy"]["fire_count"], 1)
        self.assertEqual(saved["api:deploy"]["fire_count"], 0)

    def test_internal_handler_installed_without_external_callback(self) -> None:
        out = self.run_loop(["hello", "kiln-fire", "go"], external_on_event=None)
        self.assertIsNotNone(out.handlers[0])
        self.assertEqual(out.prompts, ["hello", "/kiln-fire"])

    def test_internal_handler_chains_the_external_callback(self) -> None:
        events: list[dict] = []
        out = self.run_loop(["hello"], external_on_event=events.append)
        self.assertEqual(out.prompts, ["hello"])
        self.assertIn(_INIT_EVENT, events)


class NamespacePolicyTests(_LoopHarness):
    def test_allowlist_blocks_other_namespaces_and_says_so(self) -> None:
        config.set_command_namespace_policy("allowlist")
        config.set_command_namespace_allowlist("git")
        out = self.run_loop(["hello", "kiln-fire", "commit -m done", "go"])
        self.assertEqual(out.prompts, ["hello", "kiln-fire", "/git:commit -m done"])
        self.assertTrue(any("is not allowed" in line for line in out.statuses))
        self.assertFalse(any("Firing /kiln-fire" in line for line in out.spoken))

    def test_ask_first_use_gates_each_namespace_once_per_session(self) -> None:
        config.set_command_namespace_policy("ask-first-use")
        out = self.run_loop(["hello", "kiln-fire", "go", "yes", "kiln-fire", "go"])
        asks = [line for line in out.spoken if "Say yes to allow" in line]
        self.assertEqual(len(asks), 1)
        self.assertEqual(out.prompts, ["hello", "/kiln-fire", "/kiln-fire"])
        saved = command_catalog.load_saved_flags()
        self.assertEqual(saved[":kiln-fire"]["fire_count"], 2)

    def test_ask_first_use_cancel_drops_the_fire(self) -> None:
        config.set_command_namespace_policy("ask-first-use")
        out = self.run_loop(["hello", "kiln-fire", "go", "cancel"])
        self.assertEqual(out.prompts, ["hello"])
        saved = command_catalog.load_saved_flags()
        self.assertEqual(saved[":kiln-fire"]["fire_count"], 0)


class OnceClarificationTests(_LoopHarness):
    def test_once_fires_the_command_after_the_spoken_go(self) -> None:
        # --once must keep the session alive through disambiguation AND the
        # confirmation prompt, firing only once the spoken "go" is captured.
        records = command_catalog.parse_init_event(_INIT_EVENT)
        with mock.patch.object(listen, "_allowed_records", return_value=(records, [])):
            out = self.run_loop(["deploy now", "web:deploy", "go"], once=True)
        self.assertTrue(any(line.startswith("Which command") for line in out.spoken))
        self.assertTrue(any("Firing /web:deploy now" in line for line in out.spoken))
        self.assertEqual(out.prompts, ["/web:deploy now"])
        self.assertEqual(out.leftover, [])


def _catalog_record(command_id, namespace, *, enabled=True, mutating=True, arg_schema=None):
    return {
        "id": command_id,
        "namespace": namespace,
        "description": "",
        "mutating": mutating,
        "enabled": enabled,
        "favorite": False,
        "fire_count": 0,
        "arg_schema": arg_schema,
    }


class DispatcherSecurityTests(_Isolated):
    def test_disabled_command_is_content_and_never_reaches_the_classifier(self) -> None:
        # A disabled command's exact name must resolve against the COMPLETE
        # catalog first and stop as content, so it can never be remapped by the
        # classifier onto a different enabled mutating command.
        catalog = [
            _catalog_record("deploy", "", enabled=False),
            _catalog_record("release", "", enabled=True),
        ]
        statuses: list[str] = []
        with mock.patch.object(
            listen,
            "_classify_intent",
            side_effect=AssertionError("must not classify a disabled command"),
        ):
            outcome = listen._resolve_command(
                "command deploy", catalog, statuses.append
            )
        self.assertEqual(outcome.kind, "no_match")
        self.assertTrue(any("disabled" in line for line in statuses))

    def test_required_arg_command_emits_missing_slot_on_empty_remainder(self) -> None:
        records = [
            _catalog_record(
                "commit", "git", arg_schema=[{"name": "message", "required": True}]
            )
        ]
        outcome = listen._match_name("git:commit", records)
        self.assertEqual(outcome.kind, "missing_slot")
        self.assertEqual(outcome.missing_slots, ("message",))

    def test_no_arg_command_still_fires_on_empty_remainder(self) -> None:
        records = [_catalog_record("kiln-fire", "")]
        outcome = listen._match_name("kiln-fire", records)
        self.assertEqual(outcome.kind, "complete")
        self.assertEqual(outcome.args, "")


class CatalogBootstrapTests(_LoopHarness):
    def test_command_resolves_on_the_first_utterance_from_persisted_roster(self) -> None:
        # With catalog metadata persisted before the session, the very first
        # utterance resolves — no ordinary turn is needed to seed the roster.
        command_catalog.save_flags([_catalog_record("kiln-fire", "")])
        out = self.run_loop(["kiln-fire", "go"])
        self.assertEqual(out.prompts, ["/kiln-fire"])
        self.assertTrue(any("Firing /kiln-fire" in line for line in out.spoken))


class MidWaitWakeTests(unittest.TestCase):
    def test_enabling_wake_mid_wait_aborts_the_ungated_capture(self) -> None:
        block = mock.Mock()
        block.copy.return_value = block
        stream = mock.MagicMock()
        stream.__enter__.return_value.read.return_value = (block, False)
        sounddevice = mock.Mock()
        sounddevice.InputStream.return_value = stream
        polled = {"n": 0}

        def wake_check() -> bool:
            polled["n"] += 1
            return True  # the operator switched wake on mid-wait

        with mock.patch.object(listen, "_sounddevice", return_value=sounddevice), \
                mock.patch.object(listen, "_rms", return_value=0.0), \
                mock.patch.object(listen, "_finish", side_effect=lambda c, *a: c or None):
            result = listen._record_always_on(wake_check=wake_check)

        self.assertIsNone(result)  # aborted so the next capture re-gates
        self.assertGreaterEqual(polled["n"], 1)


class WakeGateTests(_Isolated):
    def _run_once(self, spoken: list[str]) -> None:
        transcriber = mock.Mock()
        transcriber.transcribe.return_value = "hello"
        with mock.patch.object(
            listen, "_is_windows", return_value=False
        ), mock.patch.object(
            listen, "_record_always_on", return_value=object()
        ), mock.patch.object(
            listen, "UtteranceTranscriber", return_value=transcriber
        ), mock.patch.object(listen, "_prompt_claude", return_value=("ok", "s1")):
            listen.run_listen(
                mode="always-on",
                session_id=None,
                tmux_pane=None,
                device="cpu",
                model=None,
                once=True,
                echo=lambda _line: None,
                speak=spoken.append,
                status=lambda _line: None,
            )

    def test_enabled_wake_word_gates_capture_and_greets(self) -> None:
        config.set_wake_word(True)
        config.set_wake_model_path("/models/yo-claude.onnx")
        spoken: list[str] = []
        with mock.patch(
            "talktomeclaude.wakeword.wait_for_wake_word", return_value="yo claude"
        ) as detector:
            self._run_once(spoken)
        detector.assert_called_once()
        self.assertEqual(
            detector.call_args.args[0], "/models/yo-claude.onnx"
        )
        self.assertIn(listen.WAKE_GREETING, spoken)

    def test_disabled_wake_word_leaves_capture_ungated(self) -> None:
        config.set_wake_word(False)
        spoken: list[str] = []
        with mock.patch(
            "talktomeclaude.wakeword.wait_for_wake_word",
            side_effect=AssertionError("must not run the detector"),
        ):
            self._run_once(spoken)
        self.assertNotIn(listen.WAKE_GREETING, spoken)

    def test_each_wake_gate_disposition(self) -> None:
        statuses: list[str] = []
        spoken: list[str] = []

        config.set_wake_word(False)
        self.assertIs(
            listen._wake_gate(spoken.append, statuses.append, None, {}),
            listen.WakeDisposition.OFF_UNGATED,
        )

        config.set_wake_word(True)
        config.set_wake_model_path(None)
        self.assertIs(
            listen._wake_gate(spoken.append, statuses.append, None, {}),
            listen.WakeDisposition.MANUAL_FALLBACK,
        )

        config.set_wake_model_path("/models/yo-claude.onnx")
        with mock.patch(
            "talktomeclaude.wakeword.wait_for_wake_word", return_value="yo claude"
        ):
            self.assertIs(
                listen._wake_gate(spoken.append, statuses.append, None, {}),
                listen.WakeDisposition.WAKE_GRANTED,
            )
        self.assertIn(listen.WAKE_GREETING, spoken)

        stop_event = threading.Event()
        stop_event.set()
        self.assertIs(
            listen._wake_gate(spoken.append, statuses.append, stop_event, {}),
            listen.WakeDisposition.STOP,
        )

    def test_missing_model_routes_to_manual_push_to_talk(self) -> None:
        config.set_wake_word(True)
        config.set_wake_model_path(None)
        keys = mock.MagicMock()
        keys.__enter__.return_value = keys
        transcriber = mock.Mock()
        transcriber.transcribe.return_value = "hello"
        with mock.patch.object(
            listen, "_record_always_on", side_effect=AssertionError("must stay gated")
        ), mock.patch.object(
            listen, "_record_push_to_talk", return_value=object()
        ) as manual, mock.patch.object(
            listen, "UtteranceTranscriber", return_value=transcriber
        ), mock.patch.object(
            listen, "_prompt_claude", return_value=("ok", "s1")
        ):
            listen.run_listen(
                mode="always-on",
                session_id=None,
                tmux_pane=None,
                device="cpu",
                model=None,
                once=True,
                echo=lambda _line: None,
                speak=lambda _line: None,
                status=lambda _line: None,
                keys=keys,
            )

        manual.assert_called_once()

    def test_manual_fallback_requires_an_interactive_cli(self) -> None:
        config.set_wake_word(True)
        config.set_wake_model_path(None)
        with mock.patch.object(
            listen.sys.stdin, "isatty", return_value=False
        ), mock.patch.object(
            listen, "UtteranceTranscriber", return_value=mock.Mock()
        ), mock.patch.object(
            listen, "_record_always_on", side_effect=AssertionError("must stay gated")
        ):
            with self.assertRaisesRegex(listen.ListenError, "interactive terminal"):
                listen.run_listen(
                    mode="always-on",
                    session_id=None,
                    tmux_pane=None,
                    device="cpu",
                    model=None,
                    once=True,
                    echo=lambda _line: None,
                    speak=lambda _line: None,
                    status=lambda _line: None,
                )

    def test_detector_degradation_stays_manual_and_notices_once(self) -> None:
        wakeword = importlib.import_module("talktomeclaude.wakeword")

        config.set_wake_word(True)
        config.set_wake_model_path("/models/corrupt.onnx")
        statuses: list[str] = []
        state: dict = {}
        with mock.patch(
            "talktomeclaude.wakeword.wait_for_wake_word",
            side_effect=wakeword.WakeWordError("corrupt model"),
        ) as detector:
            first = listen._wake_gate(lambda _line: None, statuses.append, None, state)
            second = listen._wake_gate(lambda _line: None, statuses.append, None, state)

        self.assertIs(first, listen.WakeDisposition.MANUAL_FALLBACK)
        self.assertIs(second, listen.WakeDisposition.MANUAL_FALLBACK)
        detector.assert_called_once()
        self.assertEqual(len(statuses), 2)  # waiting + one unavailable notice
        self.assertIn("manual push-to-talk", statuses[-1])

    def test_unreadable_config_fails_closed_for_wake_only(self) -> None:
        config.config_path().write_bytes(b"\xff\xfe")
        statuses: list[str] = []

        disposition = listen._wake_gate(
            lambda _line: None, statuses.append, None, {}
        )

        self.assertIs(disposition, listen.WakeDisposition.MANUAL_FALLBACK)
        self.assertIn("unavailable", statuses[-1])
        self.assertEqual(config.recording_mode(), config.DEFAULT_RECORDING_MODE)


class ConveyanceDeliveryTests(_Isolated):
    def _run_delivery(self, checkpoint_words: list[str], reply: str, spoken: list[str]):
        stop_event = threading.Event()
        takes = iter(["ask"] + checkpoint_words)

        def next_take(**_kwargs):
            try:
                return next(takes)
            except StopIteration:
                stop_event.set()
                return None

        transcriber = mock.Mock()
        transcriber.transcribe.side_effect = lambda audio: audio or ""

        cwd = os.getcwd()
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        os.chdir(workdir.name)
        self.addCleanup(os.chdir, cwd)

        with mock.patch.object(
            listen, "_is_windows", return_value=False
        ), mock.patch.object(
            listen, "_record_always_on", side_effect=next_take
        ), mock.patch.object(
            listen, "UtteranceTranscriber", return_value=transcriber
        ), mock.patch.object(listen, "_prompt_claude", return_value=(reply, "sess-7")):
            listen.run_listen(
                mode="always-on",
                session_id=None,
                tmux_pane=None,
                device="cpu",
                model=None,
                once=False,
                echo=lambda _line: None,
                speak=spoken.append,
                status=lambda _line: None,
                stop_event=stop_event,
            )
        return Path(workdir.name)

    def test_long_reply_is_chunked_with_persisted_checkpoints(self) -> None:
        reply = " ".join(f"Sentence number {index} is here." for index in range(30))
        spoken: list[str] = []
        root = self._run_delivery(["continue", "stop"], reply, spoken)
        self.assertGreater(len(spoken), 1)
        self.assertTrue(all(len(chunk.split()) <= 75 for chunk in spoken))
        pad = root / ".omc" / "state" / "sessions" / "sess-7" / "voice-conveyance.json"
        self.assertTrue(pad.is_file())
        import json

        state = json.loads(pad.read_text())
        self.assertEqual(set(state), {"cursor", "heading", "status"})
        self.assertEqual(state["status"], "stopped")

    def test_content_at_a_checkpoint_resumes_through_the_same_session(self) -> None:
        reply = " ".join(f"Sentence number {index} is here." for index in range(30))
        prompts: list[str] = []
        stop_event = threading.Event()
        takes = iter(["ask", "actually tell me about rust"])

        def next_take(**_kwargs):
            try:
                return next(takes)
            except StopIteration:
                stop_event.set()
                return None

        transcriber = mock.Mock()
        transcriber.transcribe.side_effect = lambda audio: audio or ""

        def fake_prompt(text, session_id, **_kwargs):
            prompts.append(text)
            return (reply if len(prompts) == 1 else "Short answer.", "sess-8")

        cwd = os.getcwd()
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        os.chdir(workdir.name)
        self.addCleanup(os.chdir, cwd)

        with mock.patch.object(
            listen, "_is_windows", return_value=False
        ), mock.patch.object(
            listen, "_record_always_on", side_effect=next_take
        ), mock.patch.object(
            listen, "UtteranceTranscriber", return_value=transcriber
        ), mock.patch.object(listen, "_prompt_claude", fake_prompt):
            listen.run_listen(
                mode="always-on",
                session_id=None,
                tmux_pane=None,
                device="cpu",
                model=None,
                once=False,
                echo=lambda _line: None,
                speak=lambda _line: None,
                status=lambda _line: None,
                stop_event=stop_event,
            )
        self.assertEqual(prompts, ["ask", "actually tell me about rust"])


class BargeInWiringTests(_Isolated):
    def test_enabled_gate_routes_speech_through_terminable_playback(self) -> None:
        config.set_barge_in(True)
        transcriber = mock.Mock()
        transcriber.transcribe.return_value = "hello"
        interruptible = mock.Mock(return_value=False)
        with mock.patch.object(
            listen, "_is_windows", return_value=False
        ), mock.patch.object(
            listen, "headphones_present", return_value=True
        ), mock.patch.object(
            listen, "_speak_interruptible", interruptible
        ), mock.patch.object(
            listen, "_record_always_on", return_value=object()
        ), mock.patch.object(
            listen, "UtteranceTranscriber", return_value=transcriber
        ), mock.patch.object(listen, "_prompt_claude", return_value=("Hi.", "s1")):
            listen.run_listen(
                mode="always-on",
                session_id=None,
                tmux_pane=None,
                device="cpu",
                model=None,
                once=True,
                echo=lambda _line: None,
                speak=lambda _line: None,
                status=lambda _line: None,
            )
        interruptible.assert_called_once()

    def test_no_headphones_stays_on_the_sequential_path(self) -> None:
        config.set_barge_in(True)
        transcriber = mock.Mock()
        transcriber.transcribe.return_value = "hello"
        spoken: list[str] = []
        with mock.patch.object(
            listen, "_is_windows", return_value=False
        ), mock.patch.object(
            listen, "headphones_present", return_value=False
        ), mock.patch.object(
            listen,
            "_speak_interruptible",
            side_effect=AssertionError("half-duplex must not monitor the mic"),
        ), mock.patch.object(
            listen, "_record_always_on", return_value=object()
        ), mock.patch.object(
            listen, "UtteranceTranscriber", return_value=transcriber
        ), mock.patch.object(listen, "_prompt_claude", return_value=("Hi.", "s1")):
            listen.run_listen(
                mode="always-on",
                session_id=None,
                tmux_pane=None,
                device="cpu",
                model=None,
                once=True,
                echo=lambda _line: None,
                speak=spoken.append,
                status=lambda _line: None,
            )
        self.assertEqual(spoken, ["Hi."])


if __name__ == "__main__":
    unittest.main()
