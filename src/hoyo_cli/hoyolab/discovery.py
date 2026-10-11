"""Linked upstream roles become immutable application-owned character choices."""

from dataclasses import dataclass
from typing import Protocol

import genshin

from hoyo_cli.auth.credentials import Credentials
from hoyo_cli.errors import AppError
from hoyo_cli.hoyolab.client import fetch_linked_accounts
from hoyo_cli.models.common import GameId

_GAMES: dict[genshin.Game, GameId] = {
    genshin.Game.GENSHIN: "genshin",
    genshin.Game.STARRAIL: "hsr",
    genshin.Game.ZZZ: "zzz",
}


@dataclass(frozen=True)
class CharacterRef:
    game: GameId
    uid: int
    region: str
    label: str


class CharacterDiscovery(Protocol):
    def discover(
        self, credentials: Credentials
    ) -> dict[GameId, tuple[CharacterRef, ...]]: ...


def _safe_label(value: str) -> str:
    return "".join(char for char in value if char.isprintable())


def discover_characters(
    credentials: Credentials,
) -> dict[GameId, tuple[CharacterRef, ...]]:
    groups: dict[GameId, list[CharacterRef]] = {}
    for account in fetch_linked_accounts(credentials):
        game = _GAMES.get(account.game)
        if game is None:
            continue
        if account.uid <= 0 or not account.server:
            raise AppError("HoYoLAB returned an invalid linked character.")
        server_name = _safe_label(account.server_name).strip()
        region_label = server_name or "Region unavailable"
        character = CharacterRef(
            game,
            account.uid,
            account.server,
            _safe_label(f"{account.nickname} ({region_label})"),
        )
        choices = groups.setdefault(game, [])
        if character not in choices:
            choices.append(character)
    return {game: tuple(choices) for game, choices in groups.items()}


class HoYoLABDiscovery:
    def discover(
        self, credentials: Credentials
    ) -> dict[GameId, tuple[CharacterRef, ...]]:
        return discover_characters(credentials)
