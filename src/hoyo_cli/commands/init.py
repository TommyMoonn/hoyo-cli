"""Interactive account setup command boundary."""

from hoyo_cli.commands import CommandOptions
from hoyo_cli.errors import ConfigError


def run_init(*, options: CommandOptions) -> None:
    raise ConfigError(
        "Account setup is not configured yet in this implementation phase."
    )
