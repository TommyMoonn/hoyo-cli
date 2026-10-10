"""Canonical game identifiers shared by CLI and application models."""

from typing import Literal

GameId = Literal["genshin", "hsr", "zzz"]
GAME_IDS: tuple[GameId, ...] = ("genshin", "hsr", "zzz")
