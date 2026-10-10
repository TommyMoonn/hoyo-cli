"""Status command boundary, shared by overview and game aliases."""

from hoyo_cli.commands import CommandOptions
from hoyo_cli.errors import ConfigError
from hoyo_cli.models.common import GameId


def run_status(*, options: CommandOptions, game: GameId | None) -> None:
    raise ConfigError("Status is not configured yet in this implementation phase.")
