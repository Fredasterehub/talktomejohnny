from __future__ import annotations

import unittest

import talktomeclaude
import talktomejohnny
from talktomeclaude.cli import main as legacy_main
from talktomejohnny.cli import main as primary_main


class BrandCompatibilityTests(unittest.TestCase):
    def test_primary_namespace_shares_the_legacy_implementation_version(self) -> None:
        self.assertEqual(talktomejohnny.__version__, talktomeclaude.__version__)

    def test_primary_and_legacy_console_surfaces_share_one_click_group(self) -> None:
        self.assertIs(primary_main, legacy_main)


if __name__ == "__main__":
    unittest.main()
