"""Explicit daily check-in command boundary."""

from hoyo_cli.commands import CommandOptions, NotConfiguredError


def run_check_in(*, options: CommandOptions, game: str | None) -> None:
    raise NotConfiguredError(
        "Daily check-in is not configured yet in this implementation phase."
    )
