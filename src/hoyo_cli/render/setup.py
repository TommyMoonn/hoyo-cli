"""Width-aware, non-secret setup presentation, independent of prompts and writes."""

import sys
from collections.abc import Mapping

from rich.text import Text

from hoyo_cli.config.models import CredentialBackend, GameSelection
from hoyo_cli.hoyolab.discovery import CharacterRef
from hoyo_cli.models.common import GAME_IDS, GameId
from hoyo_cli.render.console import make_console

GAME_NAMES: Mapping[GameId, str] = {
    "genshin": "GENSHIN IMPACT",
    "hsr": "HONKAI: STAR RAIL",
    "zzz": "ZENLESS ZONE ZERO",
}


def character_choice(character: CharacterRef) -> str:
    # Discovery already supplies nickname and the upstream human-readable server
    # name in label. The internal region code is not a second display label.
    return f"{character.label} | UID {character.uid}"


class SetupPresenter:
    def __init__(
        self, *, no_color: bool = False, configured_color: str = "auto"
    ) -> None:
        self.console = make_console(
            width=None,
            no_color=no_color,
            interactive=sys.stdout.isatty(),
            configured_color=configured_color,
        )

    def message(self, message: str) -> None:
        self.console.print(Text(message))

    def step(self, number: int, label: str, profile: str | None = None) -> None:
        self.console.print()
        self.message(f"Step {number}/4 · {label}")
        if profile is not None:
            self.message(f"Profile: {profile}")

    def game(self, game: GameId) -> None:
        self.console.print()
        self.message(GAME_NAMES[game])

    def review(
        self,
        *,
        name: str,
        games: Mapping[GameId, GameSelection],
        characters: Mapping[GameId, CharacterRef],
        backend: CredentialBackend,
        login: str,
        default_profile: str,
        order: tuple[GameId, ...],
        staged: bool = True,
        editor: str | None = None,
    ) -> None:
        if staged:
            self.step(4, "Review", name)
        else:
            self.console.print()
            self.message(f"Review changes · Profile: {name}")
        for game in GAME_IDS:
            selection = games.get(game)
            if selection is None:
                self.message(f"{GAME_NAMES[game]}: Skipped (not configured)")
                continue
            character = characters.get(game)
            identity = (
                character_choice(character)
                if character is not None
                else f"UID {selection.uid} (retained selection, unverified)"
            )
            state = "Enabled" if selection.enabled else "Disabled"
            self.message(f"{GAME_NAMES[game]}: {state}")
            self.message(f"  {identity}")
        self.message(f"Login: {login}")
        self.message(f"Credential storage: {backend}")
        self.message(f"Default profile: {default_profile}")
        self.message("Game order: " + ", ".join(order))
        if editor is not None:
            self.message("Editor: " + editor)
