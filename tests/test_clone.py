"""Tests for the optional clone engine's model-free paths.

CI has no GPU/cloning stack, so these cover the guards, the stdlib-wave writer
and the dispatch — the actual generate() runs behind a mocked model.
"""

from __future__ import annotations

import tempfile
import sys
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from talktomeclaude import clone
from talktomeclaude.tts import TTSError


class CloneModelFreeTests(unittest.TestCase):
    def tearDown(self) -> None:
        clone._model = None  # never leak a cached model between tests

    def test_clone_error_is_a_tts_error(self) -> None:
        self.assertTrue(issubclass(clone.CloneError, TTSError))

    def test_ytdlp_command_is_a_pure_argv_builder_with_required_clients(self) -> None:
        command = clone.ytdlp_command("https://youtu.be/example", "agent-cut.m4a")
        self.assertEqual(command[0], "yt-dlp")
        self.assertIn("--extractor-args", command)
        self.assertIn("youtube:player_client=android_vr,web,tv", command)
        self.assertIn("--max-filesize", command)
        self.assertEqual(
            command[command.index("--max-filesize") + 1], clone._YTDLP_MAX_FILESIZE
        )
        self.assertEqual(command[-2:], ["--", "https://youtu.be/example"])

    def test_empty_text_raises_before_loading_the_model(self) -> None:
        with mock.patch.object(clone, "_load_model") as load:
            with self.assertRaises(clone.CloneError):
                clone.synthesize_clone("   ", Path("/tmp/out.wav"), "ref.wav")
        load.assert_not_called()

    def test_missing_reference_raises_before_loading_the_model(self) -> None:
        with mock.patch.object(clone, "_load_model") as load:
            with self.assertRaises(clone.CloneError):
                clone.synthesize_clone("hi", Path("/tmp/out.wav"), "/no/such/ref.wav")
        load.assert_not_called()

    def test_load_model_without_stack_points_at_doctor(self) -> None:
        clone._model = None
        with mock.patch.object(clone, "clone_available", return_value=False):
            with self.assertRaises(clone.CloneError) as ctx:
                clone._load_model()
        self.assertIn("doctor", str(ctx.exception))

    def test_repairs_perth_watermarker_without_legacy_pkg_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            perth = types.SimpleNamespace(
                __file__=str(Path(directory) / "perth" / "__init__.py"),
                PerthImplicitWatermarker=None,
            )
            implementation = types.SimpleNamespace(
                PerthImplicitWatermarker=mock.sentinel.watermarker
            )
            with mock.patch.object(
                clone.importlib, "import_module", return_value=implementation
            ):
                clone._repair_perth_watermarker(perth)

            self.addCleanup(sys.modules.pop, "perth.perth_net", None)
            compat = sys.modules["perth.perth_net"]
            self.assertEqual(
                compat.PREPACKAGED_MODELS_DIR,
                str(
                    (
                        Path(directory) / "perth" / "perth_net" / "pretrained"
                    ).resolve()
                ),
            )
            self.assertIs(
                perth.PerthImplicitWatermarker,
                mock.sentinel.watermarker,
            )

    def test_to_mono_float_normalizes_shapes(self) -> None:
        self.assertEqual(clone._to_mono_float(np.zeros((1, 10), "float32")).shape, (10,))
        self.assertEqual(clone._to_mono_float(np.zeros((2, 10), "float32")).shape, (10,))
        self.assertEqual(clone._to_mono_float(np.zeros(10, "float32")).shape, (10,))

    def test_write_wav_roundtrips_to_24k_mono_pcm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "o.wav"
            tone = np.sin(np.linspace(0, 6.28, 24000)).astype("float32")
            clone._write_wav(out, tone, 24000)
            with wave.open(str(out)) as reader:
                self.assertEqual(reader.getframerate(), 24000)
                self.assertEqual(reader.getnchannels(), 1)
                self.assertEqual(reader.getsampwidth(), 2)
                self.assertEqual(reader.getnframes(), 24000)
            self.assertEqual(out.read_bytes()[:4], b"RIFF")

    def test_write_wav_rejects_empty_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(clone.CloneError):
                clone._write_wav(Path(directory) / "o.wav", np.zeros((1, 0), "float32"), 24000)

    def test_write_wav_rejects_bad_sample_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(clone.CloneError):
                clone._write_wav(Path(directory) / "o.wav", np.zeros((1, 100), "float32"), 0)

    def test_write_wav_rejects_non_finite_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            nan = np.full((100,), np.nan, dtype="float32")
            with self.assertRaises(clone.CloneError):
                clone._write_wav(Path(directory) / "o.wav", nan, 24000)

    def test_synthesize_clone_dispatches_params_and_writes(self) -> None:
        fake = mock.Mock()
        fake.sr = 24000
        fake.generate.return_value = np.zeros((1, 12000), dtype="float32")
        with tempfile.TemporaryDirectory() as directory:
            ref = Path(directory) / "ref.wav"
            ref.write_bytes(b"RIFF0000WAVE")
            out = Path(directory) / "o.wav"
            with mock.patch.object(clone, "_load_model", return_value=fake):
                result = clone.synthesize_clone(
                    "hello", out, ref, exaggeration=0.6, cfg_weight=0.4
                )
            self.assertEqual(result, out)
            self.assertTrue(out.is_file())
            _, kwargs = fake.generate.call_args
            self.assertEqual(kwargs["exaggeration"], 0.6)
            self.assertEqual(kwargs["cfg_weight"], 0.4)
            self.assertEqual(kwargs["audio_prompt_path"], str(ref))


if __name__ == "__main__":
    unittest.main()
