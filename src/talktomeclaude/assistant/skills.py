"""Install owned TalkToMeJohnny control skills without overwriting user content."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from talktomeclaude.assistant.attachment import ProviderName

_SKILL_MARKER = "<!-- talktomejohnny.control-skill.v1 -->"
_SUPPORTED_PROVIDERS = frozenset({"claude", "codex", "both"})
_COMMAND_PREFIX = {"claude": "/", "codex": "$"}


class SkillInstallError(RuntimeError):
    """The owned TalkToMeJohnny skill could not be safely updated."""


class SkillStatus(StrEnum):
    ABSENT = "absent"
    INSTALLED = "installed"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SkillInspection:
    provider: ProviderName
    path: Path
    status: SkillStatus


def _home(environment: dict[str, str] | None = None) -> Path:
    active = os.environ if environment is None else environment
    for key in ("TALKTOMEJOHNNY_HOME", "TALKTOMECLAUDE_HOME", "HOME", "USERPROFILE"):
        value = active.get(key)
        if value:
            return Path(value).expanduser()
    return Path.home()


def _skill_root(
    provider: ProviderName,
    *,
    environment: dict[str, str] | None = None,
) -> Path:
    home = _home(environment)
    if provider == "claude":
        return home / ".claude" / "skills"
    if provider == "codex":
        return home / ".agents" / "skills"
    raise ValueError(f"unsupported assistant provider {provider!r}")


def _skill_path(
    provider: ProviderName,
    *,
    environment: dict[str, str] | None = None,
) -> Path:
    return _skill_root(provider, environment=environment) / "talktomejohnny" / "SKILL.md"


def _skill_content(provider: ProviderName) -> str:
    command = f"{_COMMAND_PREFIX[provider]}talktomejohnny on|off|status"
    return (
        "# TalkToMeJohnny control\n\n"
        f"{_SKILL_MARKER}\n\n"
        "This command is intercepted locally by TalkToMeJohnny before the assistant sees it.\n\n"
        f"Use `{command}` to attach, detach, or inspect this session.\n\n"
        "If you are reading this text, the local TalkToMeJohnny lifecycle hook is missing,\n"
        "untrusted, offline, or not installed for this CLI.\n\n"
        "Do not run tools.\n"
        "Do not modify files.\n"
        "Reply with exactly one short sentence:\n"
        "TalkToMeJohnny local control is unavailable; run `talktomejohnny hook install` and trust the installed hooks.\n"
    )


def _is_owned(existing: str, expected: str) -> bool:
    return existing == expected or _SKILL_MARKER in existing


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _provider_sequence(provider: str) -> tuple[ProviderName, ...]:
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported assistant provider {provider!r}")
    return ("claude", "codex") if provider == "both" else (provider,)


class AssistantSkillInstaller:
    """Install or remove the owned TalkToMeJohnny control skill safely."""

    def __init__(self, home: str | os.PathLike[str] | None = None) -> None:
        self._home_override = None if home is None else Path(home).expanduser()

    def _environment(self) -> dict[str, str] | None:
        if self._home_override is None:
            return None
        return {"HOME": str(self._home_override), "USERPROFILE": str(self._home_override)}

    def inspect(self, provider: ProviderName) -> SkillInspection:
        selected = provider
        path = _skill_path(selected, environment=self._environment())
        if not path.exists():
            return SkillInspection(selected, path, SkillStatus.ABSENT)
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillInstallError("assistant skill is unreadable") from exc
        expected = _skill_content(selected)
        status = (
            SkillStatus.INSTALLED
            if _is_owned(existing, expected)
            else SkillStatus.CONFLICT
        )
        return SkillInspection(selected, path, status)

    def install(self, provider: ProviderName) -> Path:
        inspection = self.inspect(provider)
        expected = _skill_content(provider)
        if inspection.status is SkillStatus.CONFLICT:
            raise SkillInstallError("assistant skill path contains user-authored content")
        if inspection.status is SkillStatus.ABSENT or (
            inspection.path.read_text(encoding="utf-8") != expected
        ):
            try:
                _atomic_write_text(inspection.path, expected)
            except OSError as exc:
                raise SkillInstallError("assistant skill update failed") from exc
        return inspection.path

    def uninstall(self, provider: ProviderName) -> bool:
        inspection = self.inspect(provider)
        if inspection.status is SkillStatus.ABSENT:
            return False
        if inspection.status is SkillStatus.CONFLICT:
            raise SkillInstallError("assistant skill path contains user-authored content")
        try:
            inspection.path.unlink(missing_ok=True)
        except OSError as exc:
            raise SkillInstallError("assistant skill removal failed") from exc
        directory = inspection.path.parent
        try:
            directory.rmdir()
        except OSError:
            pass
        return True


def install_control_skills(
    provider: str,
    *,
    environment: dict[str, str] | None = None,
) -> tuple[Path, ...]:
    """Install the owned TalkToMeJohnny control skill for one or both CLIs."""

    installer = AssistantSkillInstaller(home=_home(environment))
    return tuple(installer.install(selected) for selected in _provider_sequence(provider))


__all__ = [
    "AssistantSkillInstaller",
    "SkillInspection",
    "SkillInstallError",
    "SkillStatus",
    "install_control_skills",
]
