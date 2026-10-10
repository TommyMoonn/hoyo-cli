"""Status command boundary, shared by overview and game aliases."""

from hoyo_cli.commands import CommandOptions, NotConfiguredError


def run_status(*, options: CommandOptions, game: str | None) -> None:
    raise NotConfiguredError(
        "Status is not configured yet in this implementation phase."
    )
