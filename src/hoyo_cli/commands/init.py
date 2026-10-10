"""Interactive account setup command boundary."""

from hoyo_cli.commands import CommandOptions, NotConfiguredError


def run_init(*, options: CommandOptions) -> None:
    raise NotConfiguredError(
        "Account setup is not configured yet in this implementation phase."
    )
