"""Tests for the dialogue-only filter (LAW: filter-dialogue-only).

The milestone contract is dialogue, never code: tool calls, tool results,
fenced blocks, and inline code spans must all stay out of the spoken text.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from talktomeclaude.transcript import iter_dialogue, speakable

FIXTURE = Path(__file__).parent / "fixtures" / "transcript.jsonl"


class InlineCodeTests(unittest.TestCase):
    def test_inline_code_content_is_never_spoken(self) -> None:
        spoken = speakable(
            "Run `python -m pytest -q tests/synthetic`, then audit `forbidden_function`."
        )
        self.assertNotIn("rm -rf", spoken)
        self.assertNotIn("forbidden_function", spoken)
        self.assertIn("Run", spoken)

    def test_prose_around_an_inline_span_survives_cleanly(self) -> None:
        self.assertEqual(speakable("Call `foo()` now."), "Call now.")

    def test_fenced_code_stays_dropped(self) -> None:
        spoken = speakable("Before.\n```python\nforbidden_function()\n```\nAfter.")
        self.assertNotIn("forbidden_function", spoken)
        self.assertIn("Before.", spoken)
        self.assertIn("After.", spoken)


class FixtureDialogueTests(unittest.TestCase):
    def test_fixture_emits_dialogue_and_no_tool_or_code_content(self) -> None:
        with FIXTURE.open(encoding="utf-8") as handle:
            spoken = "\n".join(iter_dialogue(handle))
        self.assertIn("All twelve tests pass", spoken)
        self.assertNotIn("rm -rf", spoken)
        self.assertNotIn("forbidden_function", spoken)
        self.assertNotIn("sidechain", spoken.casefold())


if __name__ == "__main__":
    unittest.main()
