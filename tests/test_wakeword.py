"""Tests for the wake-word module: lazy optional-dependency import and the
DEFAULT_WAKE_PHRASE it exposes."""

from __future__ import annotations

import importlib
import sys
import unittest
from unittest import mock


class WakeWordModuleTests(unittest.TestCase):
    def test_default_wake_phrase(self) -> None:
        from talktomeclaude.wakeword import DEFAULT_WAKE_PHRASE

        self.assertEqual(DEFAULT_WAKE_PHRASE, "hey johnny")

    def test_module_imports_without_the_optional_engine_installed(self) -> None:
        # Simulate a machine that never installed the optional wake-word
        # engine: sys.modules[name] = None makes any `import <name>` raise
        # ImportError. Reloading the module under that condition must still
        # succeed, proving the optional import is deferred, never eager.
        sys.modules.pop("talktomeclaude.wakeword", None)
        with mock.patch.dict(sys.modules, {"openwakeword": None}):
            module = importlib.import_module("talktomeclaude.wakeword")
            importlib.reload(module)
            self.assertEqual(module.DEFAULT_WAKE_PHRASE, "hey johnny")

    def test_corrupt_model_is_normalized_to_wakeword_error(self) -> None:
        from talktomeclaude import wakeword

        constructor = mock.Mock(side_effect=RuntimeError("corrupt ONNX"))
        with mock.patch.object(wakeword, "_model_class", return_value=constructor):
            with self.assertRaisesRegex(wakeword.WakeWordError, "corrupt ONNX"):
                wakeword.wait_for_wake_word("/models/corrupt.onnx")


if __name__ == "__main__":
    unittest.main()
