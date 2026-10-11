"""Read-only account listing and explicit persistent default selection."""

import click

from hoyo_cli.config.paths import config_path
from hoyo_cli.config.repository import (
    commit_config,
    load_config,
    prepare_default_account,
)


def list_accounts() -> None:
    config = load_config(config_path())
    click.echo("ACCOUNTS")
    for name in config.accounts:
        marker = "  Default" if name == config.default_account else ""
        click.echo(f"  {name}{marker}")
    click.echo("\nSwitch default: hoyo account use NAME")


def use_account(name: str) -> None:
    prepared = prepare_default_account(config_path(), name)
    if prepared.content.encode("utf-8") == prepared.original:
        click.echo(f"{name} is already the default account.")
        return
    commit_config(prepared)
    click.echo(f"Default account: {name}")
