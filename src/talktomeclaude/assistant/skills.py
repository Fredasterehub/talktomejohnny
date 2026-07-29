"""Install owned TalkToMeJohnny control skills without overwriting user content."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from talktomeclaude.assistant.attachment import ProviderName

_SKILL_MARKER = "<!-- talktomejohnny.control-skill.v1 -->"
_LEGACY_GENERATED_SKILL = (
    "# TalkToMeJohnny local control\n\n"
    "This command is intercepted locally by TalkToMeJohnny before the assistant sees it.\n\n"
    "If you are reading this, the local TalkToMeJohnny session-control hook is missing,\n"
    "untrusted, or offline.\n\n"
    "Do not perform any tool actions.\n"
    "Reply with exactly one short sentence:\n"
    "TalkToMeJohnny local control is unavailable; run `talktomejohnny hook install` and trust the installed hooks.\n"
)
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


@dataclass(frozen=True, slots=True)
class _ManagedSkillPath:
    path: Path
    existing: str | None


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


def _skill_paths(
    provider: ProviderName,
    *,
    environment: dict[str, str] | None = None,
) -> tuple[Path, ...]:
    """Return every supported skill location, primary first."""

    primary = _skill_path(provider, environment=environment)
    if provider == "claude":
        return (primary,)
    if provider != "codex":
        raise ValueError(f"unsupported assistant provider {provider!r}")

    active = os.environ if environment is None else environment
    configured_home = active.get("CODEX_HOME")
    codex_home = (
        Path(configured_home).expanduser()
        if configured_home
        else _home(environment) / ".codex"
    )
    active_path = codex_home / "skills" / "talktomejohnny" / "SKILL.md"
    return tuple(dict.fromkeys((primary, active_path)))


def _skill_content(provider: ProviderName) -> str:
    prefix = _COMMAND_PREFIX[provider]
    repair_command = f"talktomejohnny hook install --provider {provider}"
    return (
        "---\n"
        "name: talktomejohnny\n"
        "description: Attach, detach, or check this assistant session for local TalkToMeJohnny voice replies.\n"
        "---\n\n"
        "# TalkToMeJohnny control\n\n"
        f"{_SKILL_MARKER}\n\n"
        "This command is intercepted locally by TalkToMeJohnny before the assistant sees it.\n\n"
        "Use one exact command to control this session:\n\n"
        f"- `{prefix}talktomejohnny on`\n"
        f"- `{prefix}talktomejohnny off`\n"
        f"- `{prefix}talktomejohnny status`\n\n"
        "If you are reading this text, the local TalkToMeJohnny lifecycle hook is missing,\n"
        "untrusted, offline, or not installed for this CLI.\n\n"
        "Do not run tools.\n"
        "Do not modify files.\n"
        "Reply with exactly one short sentence:\n"
        "TalkToMeJohnny local control is unavailable; run "
        f"`{repair_command}` and trust the installed hooks.\n"
    )


def _is_owned(existing: str, expected: str) -> bool:
    return (
        existing == expected
        or existing == _LEGACY_GENERATED_SKILL
        or _SKILL_MARKER in existing
    )


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

    def __init__(
        self,
        home: str | os.PathLike[str] | None = None,
        *,
        codex_home: str | os.PathLike[str] | None = None,
    ) -> None:
        self._home_override = None if home is None else Path(home).expanduser()
        self._codex_home_override = (
            None if codex_home is None else Path(codex_home).expanduser()
        )

    def _environment(self) -> dict[str, str] | None:
        if self._home_override is None and self._codex_home_override is None:
            return None
        home = self._home_override or Path.home()
        environment = {"HOME": str(home), "USERPROFILE": str(home)}
        if self._codex_home_override is not None:
            environment["CODEX_HOME"] = str(self._codex_home_override)
        return environment

    def _inspect_path(self, provider: ProviderName, path: Path) -> SkillInspection:
        if not path.exists():
            return SkillInspection(provider, path, SkillStatus.ABSENT)
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillInstallError("assistant skill is unreadable") from exc
        expected = _skill_content(provider)
        status = (
            SkillStatus.INSTALLED
            if _is_owned(existing, expected)
            else SkillStatus.CONFLICT
        )
        return SkillInspection(provider, path, status)

    def _inspections(self, provider: ProviderName) -> tuple[SkillInspection, ...]:
        return tuple(
            self._inspect_path(provider, path)
            for path in _skill_paths(provider, environment=self._environment())
        )

    def _managed_paths(self, provider: ProviderName) -> tuple[_ManagedSkillPath, ...]:
        managed: list[_ManagedSkillPath] = []
        for path in _skill_paths(provider, environment=self._environment()):
            if not path.exists():
                managed.append(_ManagedSkillPath(path, None))
                continue
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise SkillInstallError("assistant skill is unreadable") from exc
            managed.append(_ManagedSkillPath(path, existing))
        return tuple(managed)

    def inspect(self, provider: ProviderName) -> SkillInspection:
        inspections = self._inspections(provider)
        if any(item.status is SkillStatus.CONFLICT for item in inspections):
            status = SkillStatus.CONFLICT
        elif all(item.status is SkillStatus.INSTALLED for item in inspections):
            status = SkillStatus.INSTALLED
        else:
            status = SkillStatus.ABSENT
        return SkillInspection(provider, inspections[0].path, status)

    def install(self, provider: ProviderName) -> Path:
        expected = _skill_content(provider)
        managed = self._managed_paths(provider)
        if any(
            item.existing is not None and not _is_owned(item.existing, expected)
            for item in managed
        ):
            raise SkillInstallError("assistant skill path contains user-authored content")

        def rollback_writes() -> None:
            for rollback in reversed(written):
                if rollback.existing is None:
                    rollback.path.unlink(missing_ok=True)
                else:
                    _atomic_write_text(rollback.path, rollback.existing)

        written: list[_ManagedSkillPath] = []
        try:
            for item in managed:
                if item.existing == expected:
                    continue
                _atomic_write_text(item.path, expected)
                written.append(item)
        except OSError as exc:
            rollback_writes()
            raise SkillInstallError("assistant skill update failed") from exc
        except BaseException:
            rollback_writes()
            raise
        return managed[0].path

    def uninstall(self, provider: ProviderName) -> bool:
        inspections = self._inspections(provider)
        if all(item.status is SkillStatus.ABSENT for item in inspections):
            return False
        if any(item.status is SkillStatus.CONFLICT for item in inspections):
            raise SkillInstallError("assistant skill path contains user-authored content")
        for inspection in inspections:
            if inspection.status is SkillStatus.ABSENT:
                continue
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

    active = os.environ if environment is None else environment
    installer = AssistantSkillInstaller(
        home=_home(environment),
        codex_home=active.get("CODEX_HOME"),
    )
    return tuple(installer.install(selected) for selected in _provider_sequence(provider))


__all__ = [
    "AssistantSkillInstaller",
    "SkillInspection",
    "SkillInstallError",
    "SkillStatus",
    "install_control_skills",
]
