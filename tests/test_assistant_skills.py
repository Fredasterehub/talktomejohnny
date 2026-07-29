from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from talktomeclaude.assistant.skills import (
    AssistantSkillInstaller,
    SkillInstallError,
    SkillStatus,
    install_control_skills,
)


class AssistantSkillInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.installer = AssistantSkillInstaller(home=self.home)

    def test_installs_primary_skill_in_provider_specific_location(self) -> None:
        claude = self.installer.install("claude")
        codex = self.installer.install("codex")

        self.assertEqual(
            claude,
            self.home / ".claude" / "skills" / "talktomejohnny" / "SKILL.md",
        )
        self.assertEqual(
            codex,
            self.home / ".agents" / "skills" / "talktomejohnny" / "SKILL.md",
        )
        self.assertIn("/talktomejohnny on|off|status", claude.read_text(encoding="utf-8"))
        self.assertIn("$talktomejohnny on|off|status", codex.read_text(encoding="utf-8"))

    def test_install_is_idempotent_and_recognizes_owned_marker(self) -> None:
        path = self.installer.install("claude")
        first = path.read_text(encoding="utf-8")
        path.write_text(first.replace("local control", "local control"), encoding="utf-8")

        second = self.installer.install("claude")

        self.assertEqual(second, path)
        self.assertEqual(
            self.installer.inspect("claude").status,
            SkillStatus.INSTALLED,
        )

    def test_conflicting_user_skill_fails_closed_without_overwrite(self) -> None:
        path = self.home / ".claude" / "skills" / "talktomejohnny" / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# user skill\nDo something else.\n", encoding="utf-8")

        with self.assertRaises(SkillInstallError):
            self.installer.install("claude")

        self.assertEqual(path.read_text(encoding="utf-8"), "# user skill\nDo something else.\n")
        self.assertEqual(self.installer.inspect("claude").status, SkillStatus.CONFLICT)

    def test_uninstall_only_removes_owned_skill(self) -> None:
        self.installer.install("codex")
        self.assertTrue(self.installer.uninstall("codex"))
        self.assertFalse(
            (self.home / ".agents" / "skills" / "talktomejohnny" / "SKILL.md").exists()
        )

        conflict = self.home / ".agents" / "skills" / "talktomejohnny" / "SKILL.md"
        conflict.parent.mkdir(parents=True, exist_ok=True)
        conflict.write_text("# keep me\n", encoding="utf-8")
        with self.assertRaises(SkillInstallError):
            self.installer.uninstall("codex")
        self.assertEqual(conflict.read_text(encoding="utf-8"), "# keep me\n")

    def test_install_control_skills_supports_both(self) -> None:
        written = install_control_skills(
            "both",
            environment={
                "HOME": str(self.home),
                "USERPROFILE": str(self.home),
            },
        )

        self.assertEqual(len(written), 2)
        self.assertTrue(all(path.exists() for path in written))


if __name__ == "__main__":
    unittest.main()
