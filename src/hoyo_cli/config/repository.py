"""Comment-preserving targeted config edits with validated atomic replacement."""

import os
import stat
import tempfile
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import Item
from tomlkit.toml_document import TOMLDocument

from hoyo_cli.config.models import AccountConfig, AppConfig, parse_config
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


def _read_document(path: Path) -> TOMLDocument:
    return _read_snapshot(path)[0]


def _validate(document: TOMLDocument, path: Path) -> AppConfig:
    try:
        return parse_config(document)
    except ConfigError as error:
        raise ConfigError(
            f"{error.message}\nConfig: {path}", code=error.code, hint=error.hint
        ) from None


def load_config(path: Path) -> AppConfig:
    return _validate(_read_document(path), path)


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


def prepare_account(
    path: Path,
    name: str,
    account: AccountConfig,
    *,
    make_default: bool,
    game_order: tuple[GameId, ...],
) -> PreparedConfig:
    _reject_symlink(path)
    if path.exists():
        document, original = _read_snapshot(path)
        _validate(document, path)
    else:
        original = None
        document = tomlkit.document()
        document["version"] = 1
        document["default_account"] = name
        document["game_order"] = list(game_order)
        document["display"] = tomlkit.table().add("color", "auto")

    if make_default:
        _set_field(document, "default_account", name)
    _set_field(document, "game_order", list(game_order))
    accounts = _ensure_table(document, "accounts")
    selected = _ensure_table(accounts, name)
    _set_field(selected, "credential_backend", account.credential_backend)
    games = _ensure_table(selected, "games")
    for game in GAME_IDS:
        if game not in account.games and game in games:
            del games[game]
    for game, selection in account.games.items():
        fields = _ensure_table(games, game)
        _set_field(fields, "enabled", selection.enabled)
        _set_field(fields, "uid", selection.uid)

    # Reparse the exact serialized candidate before creating or replacing any file.
    content = tomlkit.dumps(document)
    _validate(tomlkit.parse(content), path)
    return PreparedConfig(path, content, original)


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
            "Configuration changed during setup; rerun hoyo init.\n"
            f"Config: {prepared.path}"
        )
    _atomic_write(prepared.path, prepared.content)


def save_account(
    path: Path,
    name: str,
    account: AccountConfig,
    *,
    make_default: bool,
    game_order: tuple[GameId, ...],
) -> None:
    commit_config(
        prepare_account(
            path,
            name,
            account,
            make_default=make_default,
            game_order=game_order,
        )
    )
