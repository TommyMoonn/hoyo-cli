"""Width-aware, non-secret setup presentation, independent of prompts and writes."""

import os
import shutil
import sys
from collections.abc import Mapping, Sequence

from rich.padding import Padding
from rich.text import Text

from hoyo_cli.config.models import CredentialBackend, GameSelection
from hoyo_cli.errors import AppError
from hoyo_cli.hoyolab.discovery import CharacterRef
from hoyo_cli.models.common import GAME_IDS, GameId
from hoyo_cli.render.console import make_console
from hoyo_cli.render.errors import format_error

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
        no_color = no_color or bool(os.environ.get("CI"))
        self.console = make_console(
            width=None,
            no_color=no_color,
            interactive=sys.stdout.isatty(),
            configured_color=configured_color,
        )
        self.error_console = make_console(
            width=self.console.width,
            no_color=no_color,
            interactive=sys.stdout.isatty() and sys.stderr.isatty(),
            configured_color=configured_color,
            stderr=True,
        )
        # Rich otherwise fixes dumb terminals at 80 columns, even with COLUMNS.
        # Keep guided transcripts within the terminal's advertised dimensions.
        for console in (self.console, self.error_console):
            if console.is_dumb_terminal:
                console.size = shutil.get_terminal_size()

    def message(self, message: str) -> None:
        self.console.print(Text(message))

    def heading(self, label: str) -> None:
        self.console.print(Text(label, style="heading"))

    def current(self, message: str) -> None:
        self.console.print(Text(message, style="heading"))

    def warning(self, message: str, *, stderr: bool = False) -> None:
        console = self.error_console if stderr else self.console
        console.print(Text(message, style="warning"))

    def success(self, message: str) -> None:
        self.console.print(Text(message, style="success"))

    def detail(self, label: str, value: str, *, indent: int = 0) -> None:
        row = Text()
        row.append(f"{label}: ", style="heading")
        row.append(value, style="text")
        self.console.print(Padding(row, (0, 0, 0, indent), expand=False))

    def error(self, error: AppError) -> None:
        # Keep the shared formatter authoritative, including multiline summaries.
        summary, _, recovery = format_error(error).partition("\n")
        self.error_console.print(Text(summary, style="error"))
        if recovery:
            self.error_console.print(Text(recovery, style="muted"))

    def choices(self, choices: Sequence[str], *, default: int = 1) -> None:
        for index, choice in enumerate(choices, 1):
            row = Text()
            row.append(f"{index}. ", style="heading")
            row.append(choice, style="text")
            if index == default:
                row.append(" (Default)", style="heading")
            # Keep every number visible and indent wrapped labels, including
            # paths and long server names. Never truncate a selectable action.
            self.console.print(Padding(row, (0, 0, 0, 2), expand=False))

    def prompt_label(self, label: str) -> str:
        # Click still owns the prompt, suffix, validation, defaults and getpass.
        # Capture only the literal label, reserving room for Click's [Y/n]/[1].
        with self.console.capture() as captured:
            self.console.print(
                Text(label, style="heading"),
                end="",
                width=max(1, self.console.width - 7),
            )
        return captured.get()

    def step(self, number: int, label: str, profile: str | None = None) -> None:
        self.console.print()
        self.heading(f"Step {number}/4 · {label}")
        if profile is not None:
            self.message(f"Profile: {profile}")

    def game(self, game: GameId) -> None:
        self.console.print()
        self.console.print(Text(GAME_NAMES[game], style=f"game.{game}"))

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
            self.heading(f"Review changes · Profile: {name}")
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
            row = Text()
            row.append(f"{GAME_NAMES[game]}: ", style="heading")
            row.append(state, style="text")
            self.console.print(row)
            self.console.print(Padding(Text(identity), (0, 0, 0, 2), expand=False))
        self.message(f"Login: {login}")
        self.message(f"Credential storage: {backend}")
        self.current(f"Default profile: {default_profile}")
        self.message("Game order: " + ", ".join(order))
        if editor is not None:
            self.message("Editor: " + editor)
