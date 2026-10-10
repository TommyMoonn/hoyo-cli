"""Shared command input contract and bootstrap failure classification."""

from dataclasses import dataclass

import click


@dataclass(frozen=True)
class CommandOptions:
    account: str | None = None
    detailed: bool = False
    no_color: bool = False


class NotConfiguredError(click.ClickException):
    """A command is registered but its configuration workflow is not ready."""

    exit_code = 2
