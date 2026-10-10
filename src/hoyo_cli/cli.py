"""Click grammar and option resolution for HoYo CLI."""

from collections.abc import Callable
from typing import TypeVar

import click

from hoyo_cli.commands import CommandOptions, check_in, config, init, status
from hoyo_cli.errors import AppError
from hoyo_cli.models.common import GAME_IDS, GameId
from hoyo_cli.render.errors import format_error

_Callback = TypeVar("_Callback", bound=Callable[..., object])


class _ApplicationGroup(click.Group):
    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        except AppError as error:
            click.echo(format_error(error), err=True, color=False)
            ctx.exit(error.exit_code)
        except (click.ClickException, click.Abort, click.exceptions.Exit):
            raise
        except Exception:
            error = AppError(
                "Command failed.",
                hint=(
                    "Review your account configuration. For check-in, check today's "
                    "state on HoYoLAB before trying again."
                ),
            )
            click.echo(format_error(error), err=True, color=False)
            ctx.exit(error.exit_code)


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


@click.group(name="hoyo", cls=_ApplicationGroup, invoke_without_command=True)
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
@click.argument("game", type=click.Choice(GAME_IDS), required=False)
@_common_options(include_detailed=True)
@click.pass_context
def status_command(
    ctx: click.Context,
    game: GameId | None,
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


def _game_alias(game: GameId) -> click.Command:
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


for _game in GAME_IDS:
    main.add_command(_game_alias(_game))


@main.command(name="check-in")
@click.argument("game", type=click.Choice(GAME_IDS), required=False)
@_common_options()
@click.pass_context
def check_in_command(
    ctx: click.Context, game: GameId | None, account: str | None, no_color: bool
) -> None:
    """Explicitly claim daily rewards for all enabled games, or GAME.

    Today's state is checked with a network request for each game. Already
    checked-in games are then skipped without a claim. An uncertain claim gets
    one read-only state check; claims are never automatically retried.

    \b
    Examples:
      hoyo check-in
      hoyo check-in genshin
      hoyo check-in hsr
      hoyo check-in zzz
    """
    check_in.run_check_in(
        options=_resolve_options(ctx, account=account, no_color=no_color), game=game
    )


@main.command(name="init")
@_common_options()
@click.pass_context
def init_command(ctx: click.Context, account: str | None, no_color: bool) -> None:
    """Set up or reconfigure an account interactively."""
    init.configure_account(
        options=_resolve_options(ctx, account=account, no_color=no_color)
    )


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
