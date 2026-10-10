"""Typed immutable configuration and deterministic account/game selection."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

from tomlkit.toml_document import TOMLDocument

from hoyo_cli.errors import ConfigError
from hoyo_cli.models.common import GAME_IDS, GameId

CredentialBackend = Literal["keyring", "plaintext"]
DisplayColor = Literal["auto", "always", "never"]


@dataclass(frozen=True)
class GameSelection:
    enabled: bool
    uid: int


@dataclass(frozen=True)
class AccountConfig:
    credential_backend: CredentialBackend
    games: Mapping[GameId, GameSelection]

    def __post_init__(self) -> None:
        object.__setattr__(self, "games", MappingProxyType(dict(self.games)))


@dataclass(frozen=True)
class AppConfig:
    version: int
    default_account: str
    game_order: tuple[GameId, ...]
    display_color: DisplayColor
    accounts: Mapping[str, AccountConfig]

    def __post_init__(self) -> None:
        object.__setattr__(self, "accounts", MappingProxyType(dict(self.accounts)))


def _invalid(path: str, requirement: str) -> ConfigError:
    return ConfigError(f"Invalid config: {path} {requirement}.")


def _table(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid(path, "must be a table")
    # TOML unwrap produces plain dicts; validate keys before narrowing the boundary.
    entries = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in entries):
        raise _invalid(path, "must have string keys")
    return {key: entry for key, entry in entries.items() if isinstance(key, str)}


def _account_name(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(path, "must be a non-empty account name")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _invalid(path, "must not contain control characters")
    return value


def _game_id(value: object, path: str) -> GameId:
    for game in GAME_IDS:
        if value == game:
            return game
    raise _invalid(path, "must name genshin, hsr, or zzz")


def _game_order(value: object) -> tuple[GameId, ...]:
    if not isinstance(value, list):
        raise _invalid("game_order", "must be an array")
    games: tuple[GameId, ...] = tuple(
        _game_id(entry, f"game_order[{index}]")
        for index, entry in enumerate(cast(list[object], value))
    )
    if len(set(games)) != len(games):
        raise _invalid("game_order", "must not contain duplicate games")
    return games


def _parse_account(value: object, path: str) -> AccountConfig:
    table = _table(value, path)
    backend = table.get("credential_backend")
    if backend == "keyring":
        credential_backend: CredentialBackend = "keyring"
    elif backend == "plaintext":
        credential_backend = "plaintext"
    else:
        raise _invalid(f"{path}.credential_backend", "must be keyring or plaintext")
    games: dict[GameId, GameSelection] = {}
    for name, value in _table(table.get("games"), f"{path}.games").items():
        game = _game_id(name, f"{path}.games")
        game_path = f"{path}.games.{game}"
        fields = _table(value, game_path)
        enabled = fields.get("enabled")
        uid = fields.get("uid")
        if not isinstance(enabled, bool):
            raise _invalid(f"{game_path}.enabled", "must be a boolean")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise _invalid(f"{game_path}.uid", "must be a positive integer")
        games[game] = GameSelection(enabled=enabled, uid=uid)
    return AccountConfig(credential_backend=credential_backend, games=games)


def parse_config(document: TOMLDocument) -> AppConfig:
    fields: Mapping[str, object] = document.unwrap()
    version = fields.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise _invalid("version", "must be integer 1")
    default = _account_name(fields.get("default_account"), "default_account")
    order = _game_order(fields.get("game_order"))
    display = _table(fields.get("display"), "display")
    color = display.get("color")
    if color == "auto":
        display_color: DisplayColor = "auto"
    elif color == "always":
        display_color = "always"
    elif color == "never":
        display_color = "never"
    else:
        raise _invalid("display.color", "must be auto, always, or never")
    accounts = {
        _account_name(name, "accounts"): _parse_account(value, f"accounts.{name}")
        for name, value in _table(fields.get("accounts"), "accounts").items()
    }
    config = AppConfig(
        version=version,
        default_account=default,
        game_order=order,
        display_color=display_color,
        accounts=accounts,
    )
    resolve_account(config, None)
    return config


def resolve_account(
    config: AppConfig, requested: str | None
) -> tuple[str, AccountConfig]:
    name = config.default_account if requested is None else requested
    account = config.accounts.get(name)
    if account is None:
        field = "default_account" if requested is None else "--account"
        raise _invalid(field, "must identify an existing account exactly")
    return name, account


def ordered_games(config: AppConfig) -> tuple[GameId, ...]:
    fallback: tuple[GameId, ...] = tuple(
        game for game in GAME_IDS if game not in config.game_order
    )
    return (*config.game_order, *fallback)


def enabled_games(config: AppConfig, account: AccountConfig) -> tuple[GameId, ...]:
    games: tuple[GameId, ...] = tuple(
        game
        for game in ordered_games(config)
        if game in account.games and account.games[game].enabled
    )
    if not games:
        raise ConfigError("The selected account has no enabled games.")
    return games


def resolve_games(
    config: AppConfig, account: AccountConfig, *, game: GameId | None, account_name: str
) -> tuple[GameId, ...]:
    """One enabled-game selection policy for status and explicit check-in."""
    if game is None:
        return enabled_games(config, account)
    selection = account.games.get(game)
    if selection is None or not selection.enabled:
        raise ConfigError(
            f"Game {game} is not enabled and configured for account {account_name}."
        )
    return (game,)
