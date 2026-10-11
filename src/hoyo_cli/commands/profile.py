"""Read-only profile listing and explicit persistent default selection."""

import click

from hoyo_cli.config.paths import config_path
from hoyo_cli.config.repository import (
    commit_config,
    load_config,
    prepare_default_profile,
)
from hoyo_cli.models.common import GAME_IDS


def list_profiles() -> None:
    config = load_config(config_path())
    click.echo("PROFILES")
    for name, profile in config.profiles.items():
        marker = "Default · " if name == config.default_profile else ""
        click.echo(f"  {name} · {marker}Login: {profile.login}")
        for game in GAME_IDS:
            selection = profile.games.get(game)
            if selection is not None:
                state = "Enabled" if selection.enabled else "Disabled"
                click.echo(f"    {game} · {state} · UID {selection.uid}")
    click.echo("\nSwitch default: hoyo profile use <name>")
    click.echo("Add profile:    hoyo profile add [name]")
    click.echo("Edit profile:   hoyo profile edit <name>")


def use_profile(name: str) -> None:
    prepared = prepare_default_profile(config_path(), name)
    if prepared.content.encode("utf-8") == prepared.original:
        click.echo(f"{name} is already the default profile.")
        return
    commit_config(prepared)
    click.echo(f"Default profile: {name}")
