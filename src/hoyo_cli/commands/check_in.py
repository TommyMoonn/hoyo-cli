"""Explicit daily check-in command boundary."""

from hoyo_cli.commands import CommandOptions
from hoyo_cli.errors import ConfigError
from hoyo_cli.models.common import GameId


def run_check_in(*, options: CommandOptions, game: GameId | None) -> None:
    raise ConfigError(
        "Daily check-in is not configured yet in this implementation phase."
    )
