"""Seed a complete login/profile pair for offline command fixtures."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hoyo_cli.config.models import (
    CredentialBackend,
    GameSelection,
    LoginConfig,
    ProfileConfig,
)
from hoyo_cli.config.repository import save_profile as persist_profile
from hoyo_cli.models.common import GameId


@dataclass(frozen=True)
class ProfileSeed:
    credential_backend: CredentialBackend
    games: Mapping[GameId, GameSelection]


def save_profile(
    path: Path,
    name: str,
    seed: ProfileSeed | ProfileConfig,
    *,
    make_default: bool,
    game_order: tuple[GameId, ...],
) -> None:
    if isinstance(seed, ProfileSeed):
        profile = ProfileConfig(name, seed.games)
        login = LoginConfig(seed.credential_backend, name)
    else:
        profile, login = seed, None
    persist_profile(
        path,
        name,
        profile,
        make_default=make_default,
        game_order=game_order,
        login=login,
    )
