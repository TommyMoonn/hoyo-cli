"""Click grammar and option resolution for HoYo CLI."""

from collections.abc import Callable
from typing import TypeVar

import click

from hoyo_cli.commands import CommandOptions, check_in, config, init, status

_Callback = TypeVar("_Callback", bound=Callable[..., object])
_GAME_NAMES = ("genshin", "hsr", "zzz")


def _common_options(
    *, include_detailed: bool = False
) -> Callable[[_Callback], _Callback]:
    def decorate(callback: _Callback) -> _Callback:
        callback = click.option(
            "--account", metavar="NAME", help="Select an exact named account."
        )(callback)
        callback = click.option(
            "--no-color", is_flag=True, help="Disable terminal styling."
        )(callback)
        if include_detailed:
            callback = click.option(
                "--detailed", is_flag=True, help="Show all supported status details."
            )(callback)
        return callback

    return decorate


def _resolve_options(
    ctx: click.Context,
    *,
    account: str | None,
    no_color: bool,
    detailed: bool = False,
) -> CommandOptions:
    root = ctx.find_root().ensure_object(CommandOptions)
    return CommandOptions(
        account=account if account is not None else root.account,
        detailed=root.detailed or detailed,
        no_color=root.no_color or no_color,
    )


@click.group(name="hoyo", invoke_without_command=True)
@click.version_option(package_name="hoyo-cli", prog_name="hoyo")
@_common_options(include_detailed=True)
@click.pass_context
def main(
    ctx: click.Context, account: str | None, detailed: bool, no_color: bool
) -> None:
    """HoYoLAB status for Genshin Impact, Honkai: Star Rail, and Zenless Zone Zero."""
    ctx.obj = CommandOptions(account=account, detailed=detailed, no_color=no_color)
    if ctx.invoked_subcommand is None:
        ctx.invoke(
            status_command, game=None, account=None, detailed=False, no_color=False
        )


@main.command(name="status")
@click.argument("game", type=click.Choice(_GAME_NAMES), required=False)
@_common_options(include_detailed=True)
@click.pass_context
def status_command(
    ctx: click.Context,
    game: str | None,
    account: str | None,
    detailed: bool,
    no_color: bool,
) -> None:
    """Show all enabled games, or the focused status for GAME."""
    status.run_status(
        options=_resolve_options(
            ctx, account=account, detailed=detailed, no_color=no_color
        ),
        game=game,
    )


def _game_alias(game: str) -> click.Command:
    @click.command(name=game, help=f"Alias for 'hoyo status {game}'.")
    @_common_options(include_detailed=True)
    @click.pass_context
    def alias(
        ctx: click.Context, account: str | None, detailed: bool, no_color: bool
    ) -> None:
        ctx.invoke(
            status_command,
            game=game,
            account=account,
            detailed=detailed,
            no_color=no_color,
        )

    return alias


for _game in _GAME_NAMES:
    main.add_command(_game_alias(_game))


@main.command(name="check-in")
@click.argument("game", type=click.Choice(_GAME_NAMES), required=False)
@_common_options()
@click.pass_context
def check_in_command(
    ctx: click.Context, game: str | None, account: str | None, no_color: bool
) -> None:
    """Explicitly claim daily rewards for all enabled games, or GAME."""
    check_in.run_check_in(
        options=_resolve_options(ctx, account=account, no_color=no_color), game=game
    )


@main.command(name="init")
@_common_options()
@click.pass_context
def init_command(ctx: click.Context, account: str | None, no_color: bool) -> None:
    """Set up or reconfigure an account interactively."""
    init.run_init(options=_resolve_options(ctx, account=account, no_color=no_color))


@main.command(name="config")
@click.option("--edit", is_flag=True, help="Open the config in the configured editor.")
@_common_options()
@click.pass_context
def config_command(
    ctx: click.Context, edit: bool, account: str | None, no_color: bool
) -> None:
    """Inspect non-secret configuration, or open it with --edit."""
    config.run_config(
        options=_resolve_options(ctx, account=account, no_color=no_color), edit=edit
    )
