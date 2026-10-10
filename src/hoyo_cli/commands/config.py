"""Config inspection and editor command boundary."""

from hoyo_cli.commands import CommandOptions, NotConfiguredError


def run_config(*, options: CommandOptions, edit: bool) -> None:
    raise NotConfiguredError(
        "Config inspection and editing are not configured yet "
        "in this implementation phase."
    )
