"""Comment-preserving targeted config edits with validated atomic replacement."""

import os
import stat
import tempfile
from collections.abc import MutableMapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import Item
from tomlkit.toml_document import TOMLDocument

from hoyo_cli.config.models import (
    AppConfig,
    LoginConfig,
    ProfileConfig,
    parse_config,
    resolve_profile,
    validate_editor_command,
)
from hoyo_cli.errors import ConfigError, ErrorCode
from hoyo_cli.models.common import GAME_IDS, GameId


def _read_snapshot(path: Path) -> tuple[TOMLDocument, bytes]:
    try:
        original = path.read_bytes()
        return tomlkit.parse(original.decode("utf-8")), original
    except FileNotFoundError:
        raise ConfigError(
            f"Configuration file not found.\nConfig: {path}",
            code=ErrorCode.CONFIG_MISSING,
            hint="Run: hoyo init",
        ) from None
    except (ParseError, UnicodeError):
        raise ConfigError(f"Invalid TOML configuration.\nConfig: {path}") from None
    except OSError:
        raise ConfigError(
            f"Unable to read configuration.\nConfig: {path}",
            hint="Check access to your configuration file, then retry.",
        ) from None


def _validate(document: TOMLDocument, path: Path) -> AppConfig:
    try:
        return parse_config(document)
    except ConfigError as error:
        raise ConfigError(
            f"{error.message}\nConfig: {path}", code=error.code, hint=error.hint
        ) from None


def load_config(path: Path) -> AppConfig:
    return load_config_snapshot(path)[0]


def load_config_snapshot(path: Path) -> tuple[AppConfig, bytes]:
    """Read validated settings and their exact transaction baseline together."""
    document, original = _read_snapshot(path)
    return _validate(document, path), original


def _ensure_table(
    parent: MutableMapping[str, object], key: str
) -> MutableMapping[str, object]:
    value = parent.get(key)
    if value is None:
        value = tomlkit.table()
        parent[key] = value
    if not isinstance(value, MutableMapping):
        raise ConfigError("Invalid config table structure.")
    # Only validated TOML tables reach this mutation boundary.
    return cast(MutableMapping[str, object], value)


def _set_field(table: MutableMapping[str, object], key: str, value: object) -> None:
    previous = table.get(key)
    if isinstance(previous, Item):
        previous = previous.unwrap()
    if type(previous) is not type(value) or previous != value:
        table[key] = value


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ConfigError(
            f"Refusing to write configuration through a symbolic link. "
            f"Edit the managed target directly.\nConfig: {path}"
        )


def _atomic_write(path: Path, content: str) -> None:
    _reject_symlink(path)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            if path.exists():
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _reject_symlink(path)
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise ConfigError(f"Unable to save configuration.\nConfig: {path}") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                raise ConfigError(
                    f"Unable to remove temporary configuration file.\nConfig: {path}"
                ) from None


@dataclass(frozen=True, repr=False)
class PreparedConfig:
    path: Path
    content: str
    original: bytes | None
    conflict_hint: str = "Retry the profile or config command."


def _upgrade(document: TOMLDocument) -> None:
    """Relocate validated legacy data once, without touching credential stores."""
    if document["version"] != 1:
        return
    document["version"] = 2
    document["default_profile"] = deepcopy(document["default_account"])
    del document["default_account"]
    profiles = deepcopy(document["accounts"])
    document["profiles"] = profiles
    logins = _ensure_table(document, "logins")
    for name, value in _ensure_table(document, "profiles").items():
        profile = cast(MutableMapping[str, object], value)
        login = _ensure_table(logins, name)
        login["credential_backend"] = deepcopy(profile["credential_backend"])
        login["credential_key"] = name
        del profile["credential_backend"]
        profile["login"] = name
    del document["accounts"]


def prepare_default_profile(path: Path, name: str) -> PreparedConfig:
    """Validate an exact existing name and prepare only the default field change."""
    _reject_symlink(path)
    document, original = _read_snapshot(path)
    config = _validate(document, path)
    try:
        resolve_profile(config, name)
    except ConfigError:
        raise ConfigError(
            "Unknown profile name; names must match exactly.",
            hint="Run: hoyo profile",
        ) from None
    # An already-selected default is a read-only no-op even for legacy data.
    if name != config.default_profile:
        _upgrade(document)
        _set_field(document, "default_profile", name)
    content = tomlkit.dumps(document)
    _validate(tomlkit.parse(content), path)
    return PreparedConfig(
        path, content, original, conflict_hint="Retry hoyo profile use <name>."
    )


def prepare_profile(
    path: Path,
    name: str,
    profile: ProfileConfig,
    *,
    make_default: bool,
    login: LoginConfig | None = None,
    game_order: tuple[GameId, ...] | None = None,
    editor_command: tuple[str, ...] | None = None,
) -> PreparedConfig:
    _reject_symlink(path)
    if path.exists():
        document, original = _read_snapshot(path)
        _validate(document, path)
        _upgrade(document)
    else:
        original = None
        document = tomlkit.document()
        document["version"] = 2
        document["default_profile"] = name
        document["game_order"] = list(
            game_order if game_order is not None else GAME_IDS
        )
        document["display"] = tomlkit.table().add("color", "auto")
        _set_editor(document, editor_command)

    if make_default:
        _set_field(document, "default_profile", name)
    if game_order is not None:
        _set_field(document, "game_order", list(game_order))
    if login is not None:
        selected_login = _ensure_table(_ensure_table(document, "logins"), profile.login)
        _set_field(selected_login, "credential_backend", login.credential_backend)
        _set_field(selected_login, "credential_key", login.credential_key)
    selected = _ensure_table(_ensure_table(document, "profiles"), name)
    _set_field(selected, "login", profile.login)
    games = _ensure_table(selected, "games")
    for game in GAME_IDS:
        if game not in profile.games and game in games:
            del games[game]
    for game, selection in profile.games.items():
        fields = _ensure_table(games, game)
        _set_field(fields, "enabled", selection.enabled)
        _set_field(fields, "uid", selection.uid)

    # Reparse the exact serialized candidate before creating or replacing any file.
    content = tomlkit.dumps(document)
    _validate(tomlkit.parse(content), path)
    return PreparedConfig(path, content, original)


def _set_editor(document: TOMLDocument, command: tuple[str, ...] | None) -> None:
    if command is not None:
        _set_field(
            _ensure_table(document, "editor"),
            "command",
            list(validate_editor_command(command)),
        )
    elif "editor" in document:
        editor = _ensure_table(document, "editor")
        if "command" in editor:
            del editor["command"]


def prepare_editor(path: Path, command: tuple[str, ...] | None) -> PreparedConfig:
    """Change only the editor command, retaining unknown editor fields and comments."""
    _reject_symlink(path)
    document, original = _read_snapshot(path)
    _validate(document, path)
    _upgrade(document)
    _set_editor(document, command)
    content = tomlkit.dumps(document)
    _validate(tomlkit.parse(content), path)
    return PreparedConfig(
        path, content, original, conflict_hint="Retry hoyo config editor."
    )


def prepare_game_order(path: Path, order: tuple[GameId, ...]) -> PreparedConfig:
    """Prepare only the global game order, retaining all other TOML fields."""
    _reject_symlink(path)
    document, original = _read_snapshot(path)
    _validate(document, path)
    _upgrade(document)
    _set_field(document, "game_order", list(order))
    content = tomlkit.dumps(document)
    _validate(tomlkit.parse(content), path)
    return PreparedConfig(
        path, content, original, conflict_hint="Retry hoyo config order."
    )


def commit_config(prepared: PreparedConfig) -> None:
    _reject_symlink(prepared.path)
    try:
        current = prepared.path.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError:
        raise ConfigError(
            f"Unable to verify configuration before saving.\nConfig: {prepared.path}"
        ) from None
    if current != prepared.original:
        raise ConfigError(
            f"Configuration changed before saving.\nConfig: {prepared.path}",
            hint=prepared.conflict_hint,
        )
    _atomic_write(prepared.path, prepared.content)


def save_profile(
    path: Path,
    name: str,
    profile: ProfileConfig,
    *,
    make_default: bool,
    game_order: tuple[GameId, ...],
    login: LoginConfig | None = None,
) -> None:
    commit_config(
        prepare_profile(
            path,
            name,
            profile,
            make_default=make_default,
            game_order=game_order,
            login=login,
        )
    )
