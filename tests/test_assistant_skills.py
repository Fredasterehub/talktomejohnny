from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        active_codex = (
            self.home / ".codex" / "skills" / "talktomejohnny" / "SKILL.md"
        )
        self.assertTrue(active_codex.exists())
        claude_content = claude.read_text(encoding="utf-8")
        codex_content = codex.read_text(encoding="utf-8")
        self.assertTrue(claude_content.startswith("---\nname: talktomejohnny\n"))
        self.assertIn("description:", claude_content)
        self.assertIn("/talktomejohnny on", claude_content)
        self.assertIn("/talktomejohnny off", claude_content)
        self.assertIn("/talktomejohnny status", claude_content)
        self.assertIn("$talktomejohnny on", codex_content)
        self.assertIn("$talktomejohnny off", codex_content)
        self.assertIn("$talktomejohnny status", codex_content)
        self.assertEqual(active_codex.read_text(encoding="utf-8"), codex_content)
        self.assertIn(
            "talktomejohnny hook install --provider claude", claude_content
        )
        self.assertIn(
            "talktomejohnny hook install --provider codex", codex_content
        )
        self.assertIn("open `/hooks` in Codex", codex_content)

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

    def test_install_migrates_exact_markerless_generated_skill(self) -> None:
        legacy = (
            "# TalkToMeJohnny local control\n\n"
            "This command is intercepted locally by TalkToMeJohnny before the assistant sees it.\n\n"
            "If you are reading this, the local TalkToMeJohnny session-control hook is missing,\n"
            "untrusted, or offline.\n\n"
            "Do not perform any tool actions.\n"
            "Reply with exactly one short sentence:\n"
            "TalkToMeJohnny local control is unavailable; run `talktomejohnny hook install` and trust the installed hooks.\n"
        )
        paths = (
            self.home / ".agents" / "skills" / "talktomejohnny" / "SKILL.md",
            self.home / ".codex" / "skills" / "talktomejohnny" / "SKILL.md",
        )
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(legacy, encoding="utf-8", newline="\n")

        self.installer.install("codex")

        for path in paths:
            migrated = path.read_text(encoding="utf-8")
            self.assertIn("<!-- talktomejohnny.control-skill.v1 -->", migrated)
            self.assertIn(
                "talktomejohnny hook install --provider codex", migrated
            )

    def test_install_removes_only_owned_legacy_namespace_skill(self) -> None:
        generated = (
            "# TalkToMeJohnny local control\n\n"
            "This command is intercepted locally by TalkToMeJohnny before the assistant sees it.\n\n"
            "If you are reading this, the local TalkToMeJohnny session-control hook is missing,\n"
            "untrusted, or offline.\n\n"
            "Do not perform any tool actions.\n"
            "Reply with exactly one short sentence:\n"
            "TalkToMeJohnny local control is unavailable; run `talktomejohnny hook install` and trust the installed hooks.\n"
        )
        legacy = self.home / ".agents" / "skills" / "talktomeclaude" / "SKILL.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(generated, encoding="utf-8", newline="\n")

        current = self.installer.install("codex")

        self.assertTrue(current.exists())
        self.assertFalse(legacy.exists())

    def test_install_preserves_user_authored_legacy_namespace_skill(self) -> None:
        legacy = self.home / ".claude" / "skills" / "talktomeclaude" / "SKILL.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("# My legacy workflow\n", encoding="utf-8")

        current = self.installer.install("claude")

        self.assertTrue(current.exists())
        self.assertEqual(
            legacy.read_text(encoding="utf-8"),
            "# My legacy workflow\n",
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
        self.assertFalse(
            (self.home / ".codex" / "skills" / "talktomejohnny" / "SKILL.md").exists()
        )

        conflict = self.home / ".agents" / "skills" / "talktomejohnny" / "SKILL.md"
        conflict.parent.mkdir(parents=True, exist_ok=True)
        conflict.write_text("# keep me\n", encoding="utf-8")
        with self.assertRaises(SkillInstallError):
            self.installer.uninstall("codex")
        self.assertEqual(conflict.read_text(encoding="utf-8"), "# keep me\n")

    def test_codex_conflict_in_active_home_prevents_partial_install(self) -> None:
        active = self.home / ".codex" / "skills" / "talktomejohnny" / "SKILL.md"
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text("# user skill\n", encoding="utf-8")

        with self.assertRaises(SkillInstallError):
            self.installer.install("codex")

        portable = self.home / ".agents" / "skills" / "talktomejohnny" / "SKILL.md"
        self.assertFalse(portable.exists())
        self.assertEqual(active.read_text(encoding="utf-8"), "# user skill\n")

    def test_codex_home_override_receives_a_copy(self) -> None:
        codex_home = Path(self.temporary.name) / "custom-codex"
        installer = AssistantSkillInstaller(home=self.home, codex_home=codex_home)

        installer.install("codex")

        self.assertTrue(
            (codex_home / "skills" / "talktomejohnny" / "SKILL.md").exists()
        )

    def test_codex_secondary_write_failure_rolls_back_primary_install(self) -> None:
        codex_home = Path(self.temporary.name) / "custom-codex"
        installer = AssistantSkillInstaller(home=self.home, codex_home=codex_home)
        target = codex_home / "skills" / "talktomejohnny" / "SKILL.md"
        calls: list[Path] = []

        def failing_write(path: Path, content: str) -> None:
            calls.append(path)
            if path == target:
                raise OSError("disk full")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")

        with patch(
            "talktomeclaude.assistant.skills._atomic_write_text",
            side_effect=failing_write,
        ):
            with self.assertRaises(SkillInstallError):
                installer.install("codex")

        self.assertIn(target, calls)
        self.assertFalse(
            (self.home / ".agents" / "skills" / "talktomejohnny" / "SKILL.md").exists()
        )
        self.assertFalse(target.exists())

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
