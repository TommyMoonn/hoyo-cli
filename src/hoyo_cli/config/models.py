"""Immutable profiles and logins, with a bounded v1 legacy-data reader."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast
from unicodedata import category

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
class LoginConfig:
    credential_backend: CredentialBackend
    credential_key: str


@dataclass(frozen=True)
class ProfileConfig:
    login: str
    games: Mapping[GameId, GameSelection]

    def __post_init__(self) -> None:
        object.__setattr__(self, "games", MappingProxyType(dict(self.games)))


@dataclass(frozen=True)
class AppConfig:
    version: int
    default_profile: str
    game_order: tuple[GameId, ...]
    display_color: DisplayColor
    logins: Mapping[str, LoginConfig]
    profiles: Mapping[str, ProfileConfig]
    editor_command: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "logins", MappingProxyType(dict(self.logins)))
        object.__setattr__(self, "profiles", MappingProxyType(dict(self.profiles)))


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


def validate_name(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(path, "must be a non-empty name")
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


def validate_editor_command(value: object) -> tuple[str, ...]:
    """Validate argv without including user-supplied values in diagnostics."""
    if not isinstance(value, (list, tuple)) or not value:
        raise _invalid("editor.command", "must be a nonempty argument array")
    entries = cast(list[object] | tuple[object, ...], value)
    if any(
        not isinstance(entry, str)
        or not entry
        or any(category(character) == "Cc" for character in entry)
        for entry in entries
    ):
        raise _invalid(
            "editor.command", "must contain nonempty strings without control characters"
        )
    return tuple(cast(str, entry) for entry in entries)


def _parse_login(value: object, path: str) -> LoginConfig:
    table = _table(value, path)
    backend = table.get("credential_backend")
    if backend == "keyring":
        credential_backend: CredentialBackend = "keyring"
    elif backend == "plaintext":
        credential_backend = "plaintext"
    else:
        raise _invalid(f"{path}.credential_backend", "must be keyring or plaintext")
    return LoginConfig(
        credential_backend,
        validate_name(table.get("credential_key"), f"{path}.credential_key"),
    )


def _parse_profile(value: object, path: str) -> ProfileConfig:
    table = _table(value, path)
    login = validate_name(table.get("login"), f"{path}.login")
    games: dict[GameId, GameSelection] = {}
    for name, value in _table(table.get("games", {}), f"{path}.games").items():
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
    return ProfileConfig(login=login, games=games)


def parse_config(document: TOMLDocument) -> AppConfig:
    fields: Mapping[str, object] = document.unwrap()
    version = fields.get("version")
    if type(version) is not int or version not in (1, 2):
        raise _invalid("version", "must be integer 1 or 2")
    # Legacy data becomes the same active model. Reading never mutates TOML or secrets.
    if version == 1:
        if any(key in fields for key in ("logins", "profiles", "default_profile")):
            raise _invalid("version", "must not mix legacy and v2 fields")
        legacy = _table(fields.get("accounts"), "accounts")
        logins: dict[str, LoginConfig] = {}
        profiles: dict[str, ProfileConfig] = {}
        for name, value in legacy.items():
            validate_name(name, "accounts")
            table = _table(value, f"accounts.{name}")
            logins[name] = _parse_login(
                {**table, "credential_key": name}, f"accounts.{name}"
            )
            profiles[name] = _parse_profile(
                {**table, "login": name}, f"accounts.{name}"
            )
        default = validate_name(fields.get("default_account"), "default_account")
    else:
        if any(key in fields for key in ("accounts", "default_account")):
            raise _invalid("version", "must not mix legacy and v2 fields")
        default = validate_name(fields.get("default_profile"), "default_profile")
        logins = {
            validate_name(name, "logins"): _parse_login(value, f"logins.{name}")
            for name, value in _table(fields.get("logins"), "logins").items()
        }
        profiles = {
            validate_name(name, "profiles"): _parse_profile(value, f"profiles.{name}")
            for name, value in _table(fields.get("profiles"), "profiles").items()
        }
    keys = [
        (login.credential_backend, login.credential_key) for login in logins.values()
    ]
    if len(keys) != len(set(keys)):
        raise _invalid("logins", "must have distinct credential-store identities")
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
    editor = _table(fields["editor"], "editor") if "editor" in fields else {}
    config = AppConfig(
        version=2,
        default_profile=default,
        game_order=order,
        display_color=display_color,
        logins=logins,
        profiles=profiles,
        editor_command=(
            validate_editor_command(editor["command"]) if "command" in editor else None
        ),
    )
    resolve_profile(config, None)
    for profile in profiles.values():
        resolve_login(config, profile)
    return config


def resolve_profile(
    config: AppConfig, requested: str | None
) -> tuple[str, ProfileConfig]:
    name = config.default_profile if requested is None else requested
    profile = config.profiles.get(name)
    if profile is None:
        field = "default_profile" if requested is None else "--profile"
        raise _invalid(field, "must identify an existing profile exactly")
    return name, profile


def resolve_login(config: AppConfig, profile: ProfileConfig) -> LoginConfig:
    login = config.logins.get(profile.login)
    if login is None:
        raise _invalid("profile.login", "must identify an existing login exactly")
    return login


def ordered_games(config: AppConfig) -> tuple[GameId, ...]:
    fallback: tuple[GameId, ...] = tuple(
        game for game in GAME_IDS if game not in config.game_order
    )
    return (*config.game_order, *fallback)


def enabled_games(config: AppConfig, profile: ProfileConfig) -> tuple[GameId, ...]:
    games: tuple[GameId, ...] = tuple(
        game
        for game in ordered_games(config)
        if game in profile.games and profile.games[game].enabled
    )
    if not games:
        raise ConfigError("The selected profile has no enabled games.")
    return games


def resolve_games(
    config: AppConfig, profile: ProfileConfig, *, game: GameId | None, profile_name: str
) -> tuple[GameId, ...]:
    """One enabled-game selection policy for status and explicit check-in."""
    if game is None:
        return enabled_games(config, profile)
    selection = profile.games.get(game)
    if selection is None or not selection.enabled:
        raise ConfigError(
            f"Game {game} is not enabled and configured for profile {profile_name}."
        )
    return (game,)
