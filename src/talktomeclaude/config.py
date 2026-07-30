"""Persistent configuration shared by the CLI and assistant lifecycle hooks.

Settings live in a single JSON file at one stable location — an explicit
``TALKTOMEJOHNNY_CONFIG_DIR`` override (with the legacy alias still accepted),
else the user's XDG config directory
— so a setting written from a normal shell is exactly the state the installed
hook reads. ``CLAUDE_PLUGIN_DATA`` is deliberately ignored: Claude Code hands
it to the hook while the shell CLI never sees it, and honoring it would split
state across two files (an ``assist off`` that never mutes the hook).
"""

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from talktomeclaude.storage import AtomicJsonTransaction, AtomicStorageError, ConfigStore

_CONFIG_FILE = "config.json"
_NATIVE_PATH = type(Path())
_PRODUCT_DIR = "talktomejohnny"
_LEGACY_PRODUCT_DIR = "talktomeclaude"
_VOICE_REGISTRY_FILE = "voices.json"
_VOICE_REFS_DIR = "voice-refs"
_LEGACY_MIGRATION_MARKER = ".legacy-state-migration.json"

RECORDING_MODES = ("always-on", "push-to-talk", "push-toggle")
DEFAULT_RECORDING_MODE = "push-to-talk"
DEFAULT_COMPANION_RECORDING_MODE = "push-toggle"
DEFAULT_WAKE_PHRASE = "hey johnny"
ASSISTANT_PROVIDERS = ("both", "claude", "codex")
DEFAULT_CONTROL_KEYBINDING = "ctrl+alt+space"
DEFAULT_ASSISTANT_PROVIDER = "both"
CLAUDE_PERMISSIONS = ("off", "skip", "acceptEdits", "bypassPermissions")
STT_DEVICES = ("auto", "cuda", "cpu")
COMMAND_NAMESPACE_POLICIES = ("allow-all", "ask-first-use", "allowlist")
DEFAULT_COMMAND_NAMESPACE_POLICY = "ask-first-use"
CLONE_RECIPE_CHOICES = ("shown", "later")


class ConfigLoadError(RuntimeError):
    pass


def _env_override(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _xdg_config_base() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return _NATIVE_PATH(xdg).expanduser() if xdg else _NATIVE_PATH.home() / ".config"


def _xdg_cache_base() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    return _NATIVE_PATH(xdg).expanduser() if xdg else _NATIVE_PATH.home() / ".cache"


def _current_config_dir() -> Path:
    override = _env_override("TALKTOMEJOHNNY_CONFIG_DIR", "TALKTOMECLAUDE_CONFIG_DIR")
    if override:
        return _NATIVE_PATH(override).expanduser()
    return _xdg_config_base() / _PRODUCT_DIR


def legacy_config_dir() -> Path:
    override = os.environ.get("TALKTOMECLAUDE_CONFIG_DIR")
    if override:
        return _NATIVE_PATH(override).expanduser()
    return _xdg_config_base() / _LEGACY_PRODUCT_DIR


def config_dir() -> Path:
    """Return the TalkToMeJohnny state root without implicit filesystem writes."""

    return _current_config_dir()



def preferred_cache_dir(*parts: str) -> Path:
    """Return the current cache path, reusing a populated legacy cache in place."""

    current_path = (_xdg_cache_base() / _PRODUCT_DIR).joinpath(*parts)
    legacy_path = (_xdg_cache_base() / _LEGACY_PRODUCT_DIR).joinpath(*parts)
    if current_path.exists() or not legacy_path.exists():
        return current_path
    return legacy_path


def _copy_missing_file(source: Path, destination: Path) -> bool:
    if not source.is_file() or destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temp_path = Path(temp_name)
    try:
        os.close(descriptor)
        shutil.copyfile(source, temp_path)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return True


def _copy_missing_voice_refs(source_root: Path, destination_root: Path) -> int:
    source_dir = source_root / _VOICE_REFS_DIR
    destination_dir = destination_root / _VOICE_REFS_DIR
    if not source_dir.is_dir():
        return 0
    copied = 0
    for source in sorted(source_dir.iterdir()):
        if _copy_missing_file(source, destination_dir / source.name):
            copied += 1
    return copied


def migrate_legacy_state() -> bool:
    """Copy supported legacy TalkToMeClaude state into the TalkToMeJohnny root."""

    destination_root = config_dir()
    source_root = legacy_config_dir()
    if source_root == destination_root or not source_root.exists():
        return False

    copied_any: list[bool] = []

    def migrate(_state: dict) -> dict:
        copied_config = _copy_missing_file(
            source_root / _CONFIG_FILE, destination_root / _CONFIG_FILE
        )
        copied_registry = _copy_missing_file(
            source_root / _VOICE_REGISTRY_FILE,
            destination_root / _VOICE_REGISTRY_FILE,
        )
        copied_refs = _copy_missing_voice_refs(source_root, destination_root)
        copied_any.append(copied_config or copied_registry or copied_refs > 0)
        return {
            "version": 1,
            "legacy_product": _LEGACY_PRODUCT_DIR,
            "current_product": _PRODUCT_DIR,
            "copied": {
                _CONFIG_FILE: copied_config,
                _VOICE_REGISTRY_FILE: copied_registry,
                _VOICE_REFS_DIR: copied_refs,
            },
            "completed_at": datetime.now(UTC).isoformat(),
        }

    try:
        AtomicJsonTransaction(
            destination_root / _LEGACY_MIGRATION_MARKER,
            purpose="legacy-state-migration",
        ).update(migrate)
    except (OSError, AtomicStorageError) as exc:
        raise ConfigLoadError(f"legacy state migration failed ({exc})") from exc
    return copied_any[0]


def config_path() -> Path:
    return config_dir() / _CONFIG_FILE


def _store() -> ConfigStore:
    return ConfigStore(config_path())


def _load_checked() -> dict:
    try:
        settings = _store().load()
    except (OSError, UnicodeError, AtomicStorageError) as exc:
        raise ConfigLoadError(f"configuration is unreadable ({exc})") from exc
    if not isinstance(settings, dict):
        raise ConfigLoadError("configuration root must be an object")
    return settings


def load() -> dict:
    try:
        return _load_checked()
    except ConfigLoadError:
        return {}


def save(settings: dict) -> None:
    try:
        _store().save(settings)
    except (OSError, AtomicStorageError) as exc:
        raise ConfigLoadError(f"configuration is unwritable ({exc})") from exc


def get_value(key: str, default=None):
    return load().get(key, default)


def set_value(key: str, value) -> None:
    try:
        _store().update(lambda settings: settings.__setitem__(key, value))
    except (OSError, AtomicStorageError) as exc:
        raise ConfigLoadError(f"configuration is unwritable ({exc})") from exc


def _clear_value(key: str) -> None:
    def clear(settings: dict) -> None:
        settings.pop(key, None)

    try:
        _store().update(clear)
    except (OSError, AtomicStorageError) as exc:
        raise ConfigLoadError(f"configuration is unwritable ({exc})") from exc


def recording_mode() -> str:
    """The persisted recording mode; push-to-talk is the reliable default."""
    value = load().get("recording-mode")
    return value if value in RECORDING_MODES else DEFAULT_RECORDING_MODE


def set_recording_mode(mode: str) -> None:
    if mode not in RECORDING_MODES:
        raise ValueError(
            f"unknown recording mode {mode!r}: expected one of {', '.join(RECORDING_MODES)}"
        )
    set_value("recording-mode", mode)


def companion_recording_mode() -> str:
    """Companion capture defaults to toggle without changing legacy listen."""

    value = load().get("recording-mode")
    return value if value in RECORDING_MODES else DEFAULT_COMPANION_RECORDING_MODE


def stt_device() -> str:
    """The persisted speech-to-text device tier; auto-detect is the default."""
    value = load().get("stt-device")
    return value if value in STT_DEVICES else "auto"


def set_stt_device(value: str) -> None:
    if value not in STT_DEVICES:
        raise ValueError(
            f"unknown stt device {value!r}: expected one of {', '.join(STT_DEVICES)}"
        )
    set_value("stt-device", value)


def command_namespace_policy() -> str:
    """The persisted command policy; fresh installs confirm first use."""
    value = load().get("command-namespace-policy")
    return (
        value
        if value in COMMAND_NAMESPACE_POLICIES
        else DEFAULT_COMMAND_NAMESPACE_POLICY
    )


def set_command_namespace_policy(value: str) -> None:
    if value not in COMMAND_NAMESPACE_POLICIES:
        raise ValueError(
            f"unknown command-namespace policy {value!r}: expected one of "
            f"{', '.join(COMMAND_NAMESPACE_POLICIES)}"
        )
    set_value("command-namespace-policy", value)


def command_namespace_allowlist() -> tuple[str, ...]:
    """The allowed command namespaces, parsed from the persisted
    comma-separated string; empty when unset."""
    value = load().get("command-namespace-allowlist")
    if not isinstance(value, str):
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def set_command_namespace_allowlist(value: str | None) -> None:
    """Persist the comma-separated allowlist, or clear it when empty."""
    if value and value.strip():
        set_value("command-namespace-allowlist", value.strip())
    else:
        _clear_value("command-namespace-allowlist")


def clone_recipe_choice() -> str:
    """Whether the operator asked to see the clone install recipe during
    onboarding; later is the default."""
    value = load().get("clone-recipe")
    return value if value in CLONE_RECIPE_CHOICES else "later"


def set_clone_recipe_choice(value: str) -> None:
    if value not in CLONE_RECIPE_CHOICES:
        raise ValueError(
            f"unknown clone-recipe choice {value!r}: expected one of "
            f"{', '.join(CLONE_RECIPE_CHOICES)}"
        )
    set_value("clone-recipe", value)


def onboarding_version() -> int:
    """The persisted onboarding version; zero means onboarding is incomplete."""
    value = get_value("onboarding-version", 0)
    return value if type(value) is int else 0


def set_onboarding_version(version: int) -> None:
    set_value("onboarding-version", version)


def onboarding_needed(current: int) -> bool:
    return onboarding_version() < current


def claude_permissions() -> str:
    """The persisted Claude Code permission posture; off is the safe default."""
    value = load().get("claude-permissions")
    return value if value in CLAUDE_PERMISSIONS else "off"


def set_claude_permissions(value: str) -> None:
    if value not in CLAUDE_PERMISSIONS:
        raise ValueError(
            f"unknown Claude permission posture {value!r}: expected one of "
            f"{', '.join(CLAUDE_PERMISSIONS)}"
        )
    set_value("claude-permissions", value)


def voice_assist_enabled() -> bool:
    """The full-mute switch the Stop hook consults before speaking."""
    return load().get("voice-assist", "on") == "on"


def set_voice_assist(enabled: bool) -> None:
    set_value("voice-assist", "on" if enabled else "off")


def assistant_auto_submit_enabled() -> bool:
    """Whether acceptable assistant dictation sends exactly one Enter."""

    return load().get("assistant-auto-submit", "on") == "on"


def set_assistant_auto_submit(enabled: bool) -> None:
    set_value("assistant-auto-submit", "on" if enabled else "off")


def assistant_provider() -> str:
    """The CLI whose authoritative completion events drive spoken replies."""

    value = load().get("assistant-provider")
    return value if value in ASSISTANT_PROVIDERS else DEFAULT_ASSISTANT_PROVIDER


def set_assistant_provider(value: str) -> None:
    """Persist one supported assistant provider without changing other settings."""

    if value not in ASSISTANT_PROVIDERS:
        raise ValueError(
            f"unknown assistant provider {value!r}: expected one of "
            f"{', '.join(ASSISTANT_PROVIDERS)}"
        )
    set_value("assistant-provider", value)


def control_hotkey() -> str:
    """The persisted global hotkey for push-to-talk and push-toggle."""

    value = load().get("control-keybinding")
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_CONTROL_KEYBINDING
    from talktomeclaude.companion.hotkey import normalize_control_hotkey

    try:
        return normalize_control_hotkey(value)
    except ValueError:
        return DEFAULT_CONTROL_KEYBINDING


def set_control_keybinding(binding: str) -> None:
    """Validate and persist the canonical global companion shortcut."""

    from talktomeclaude.companion.hotkey import normalize_control_hotkey

    set_value("control-keybinding", normalize_control_hotkey(binding))


def remote() -> str | None:
    """The persisted SSH target (``user@host``) the assistant runs on, or None."""
    value = load().get("remote")
    return value if isinstance(value, str) and value.strip() else None


def set_remote(value: str | None) -> None:
    """Persist the SSH target, or clear it (local) when value is empty."""
    if value and value.strip():
        set_value("remote", value.strip())
    else:
        _clear_value("remote")


def remote_cwd() -> str | None:
    """The persisted project directory for remote assistant sessions, or None."""
    value = load().get("remote-cwd")
    return value if isinstance(value, str) and value.strip() else None


def set_remote_cwd(value: str | None) -> None:
    """Persist the remote project directory, or clear it when empty."""
    if value and value.strip():
        set_value("remote-cwd", value)
    else:
        _clear_value("remote-cwd")


def barge_in_enabled() -> bool:
    """Whether the listen loop may be interrupted while the assistant is still
    speaking. Off by default: half-duplex is safe on every machine, and
    full-duplex barge-in is opt-in and gated on capable audio hardware."""
    return load().get("barge-in", "off") == "on"


def set_barge_in(enabled: bool) -> None:
    set_value("barge-in", "on" if enabled else "off")


def wake_word_enabled() -> bool:
    """Whether always-on listening waits for a wake word before recording."""
    return load().get("wake-word", "off") == "on"


def wake_word_state() -> tuple[bool, bool]:
    """Return (enabled, unavailable), failing closed on unreadable state."""
    try:
        settings = _load_checked()
    except ConfigLoadError:
        return True, True
    return settings.get("wake-word", "off") == "on", False


def set_wake_word(enabled: bool) -> None:
    set_value("wake-word", "on" if enabled else "off")


def wake_phrase() -> str:
    """The phrase associated with the user's configured wake-word model."""
    value = load().get("wake-phrase")
    return value if isinstance(value, str) and value.strip() else DEFAULT_WAKE_PHRASE


def set_wake_phrase(value: str) -> None:
    set_value("wake-phrase", value)


def wake_model_path() -> str | None:
    """Path to the trained wake-word model for the configured phrase, or None
    when no detector model has been installed yet."""
    value = load().get("wake-model")
    return value if isinstance(value, str) and value.strip() else None


def set_wake_model_path(value: str | None) -> None:
    """Persist the wake-word model path, or clear it when empty."""
    if value and value.strip():
        set_value("wake-model", value.strip())
    else:
        _clear_value("wake-model")


def onboarding_completed_at() -> str | None:
    """ISO timestamp of the last completed onboarding run, or None."""
    value = load().get("onboarding-completed-at")
    return value if isinstance(value, str) and value.strip() else None


def set_onboarding_completed_at(value: str) -> None:
    set_value("onboarding-completed-at", value)


def default_voice_name() -> str | None:
    """The user's chosen default voice, or None to auto-select the best
    available voice. The name is validated against the registry when it is
    used, not here, so a removed voice degrades gracefully to auto-select."""
    value = load().get("default-voice")
    return value.strip() if isinstance(value, str) and value.strip() else None


def set_default_voice(value: str | None) -> None:
    """Persist the default voice name, or clear it (auto-select) when empty."""
    if value and value.strip():
        set_value("default-voice", value.strip())
    else:
        _clear_value("default-voice")
