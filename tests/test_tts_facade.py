"""Compatibility guarantees for the ``talktomeclaude.tts`` facade."""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from talktomeclaude import clone, f5, tts
from talktomeclaude.speech import voices


class TTSFacadeTests(unittest.TestCase):
    def test_extraction_preserves_the_repository_voice_directory(self) -> None:
        expected = Path(tts.__file__).resolve().parents[2] / "voices"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tts.voices_dir(), expected)

    def test_error_identity_is_shared_across_facade_and_engines(self) -> None:
        self.assertIs(tts.TTSError, voices.TTSError)
        self.assertTrue(issubclass(clone.CloneError, tts.TTSError))
        self.assertTrue(issubclass(f5.F5Error, tts.TTSError))

    def test_public_function_signatures_match_the_implementation(self) -> None:
        names = (
            "voices_dir",
            "cache_voices_dir",
            "voice_files",
            "is_available",
            "get_voice",
            "default_voice",
            "synthesize",
            "play_wav",
            "synthesize_and_play",
        )
        for name in names:
            with self.subTest(name=name):
                facade = getattr(tts, name)
                implementation = getattr(voices, name)
                self.assertEqual(inspect.signature(facade), inspect.signature(implementation))

    def test_synthesize_delegates_with_legacy_patch_seams(self) -> None:
        output = Path("result.wav")
        rendered = object()
        with mock.patch.object(
            voices, "_synthesize_with", return_value=rendered
        ) as implementation:
            result = tts.synthesize("hello", output, "rick", mock.sentinel.status)

        self.assertIs(result, rendered)
        implementation.assert_called_once_with(
            "hello",
            output,
            "rick",
            mock.sentinel.status,
            get_voice_fn=tts.get_voice,
            default_voice_fn=tts.default_voice,
            voice_files_fn=tts.voice_files,
            piper_executable_fn=tts._piper_executable,
        )

    def test_combined_helper_delegates_with_legacy_patch_seams(self) -> None:
        rendered = object()
        with mock.patch.object(
            voices, "_synthesize_and_play_with", return_value=rendered
        ) as implementation:
            result = tts.synthesize_and_play(
                "hello", "rick", mock.sentinel.status
            )

        self.assertIs(result, rendered)
        implementation.assert_called_once_with(
            "hello",
            "rick",
            mock.sentinel.status,
            synthesize_fn=tts.synthesize,
            play_wav_fn=tts.play_wav,
        )

    def test_voice_preview_playback_honors_persisted_output_volume(self) -> None:
        import numpy as np

        captured: list[tuple[object, int, bool]] = []
        sounddevice = SimpleNamespace(
            play=lambda samples, *, samplerate, blocking: captured.append(
                (np.array(samples, copy=True), samplerate, blocking)
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.wav"
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16_000)
                handle.writeframes(
                    np.array([20_000, -20_000], dtype=np.int16).tobytes()
                )

            with mock.patch.dict(sys.modules, {"sounddevice": sounddevice}), mock.patch(
                "talktomeclaude.config.output_volume", return_value=25
            ):
                voices.play_wav(path)

        samples, sample_rate, blocking = captured[0]
        self.assertEqual(samples.tolist(), [5_000, -5_000])
        self.assertEqual(sample_rate, 16_000)
        self.assertTrue(blocking)


if __name__ == "__main__":
    unittest.main()
