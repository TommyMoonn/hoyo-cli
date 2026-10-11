"""Click grammar and option resolution for HoYo CLI."""

from collections.abc import Callable
from typing import TypeVar

import click

from hoyo_cli.commands import CommandOptions, check_in, config, init, profile, status
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
                    "Review your profile configuration. For check-in, check today's "
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
            "--profile",
            metavar="<name>",
            help=(
                "Select an exact named profile for one invocation; "
                "keep the saved default."
            ),
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
    profile: str | None,
    no_color: bool,
    detailed: bool = False,
) -> CommandOptions:
    root = ctx.find_root().ensure_object(CommandOptions)
    return CommandOptions(
        profile=profile if profile is not None else root.profile,
        detailed=root.detailed or detailed,
        no_color=root.no_color or no_color,
    )


@click.group(name="hoyo", cls=_ApplicationGroup, invoke_without_command=True)
@click.version_option(package_name="hoyo-cli", prog_name="hoyo")
@_common_options(include_detailed=True)
@click.pass_context
def main(
    ctx: click.Context, profile: str | None, detailed: bool, no_color: bool
) -> None:
    """HoYoLAB status for Genshin Impact, Honkai: Star Rail, and Zenless Zone Zero.

    \b
    Common commands:
      hoyo init          Set up the first profile.
      hoyo profile add [name]  Add a new profile without switching defaults.
      hoyo profile edit <name>   Update one existing profile.
      hoyo               Show all enabled games.
      hoyo genshin       Show one game's status.
      hoyo check-in zzz  Claim daily rewards for one game.
      hoyo config        Inspect selections, including disabled games.
      hoyo config order  Change the global game display order.
      hoyo config editor Choose the global preferred editor.
      hoyo profile       List profiles and mark the saved default.
      hoyo profile use <name>  Change the persistent default profile.
    """
    ctx.obj = CommandOptions(profile=profile, detailed=detailed, no_color=no_color)
    if ctx.invoked_subcommand is None:
        ctx.invoke(
            status_command, game=None, profile=None, detailed=False, no_color=False
        )


@main.group(name="profile", invoke_without_command=True)
@click.pass_context
def profile_command(ctx: click.Context) -> None:
    """List profiles, add/edit a profile, or save the default with use <name>.

    Listing and switching do not access credentials or contact HoYoLAB.
    --profile on status/check-in selects a profile for one invocation only.
    """
    if ctx.invoked_subcommand is None:
        profile.list_profiles()


@profile_command.command(name="use")
@click.argument("name", metavar="<name>")
def profile_use_command(name: str) -> None:
    """Save <name> as the default for future commands. Names must match exactly."""
    profile.use_profile(name)


@profile_command.command(name="add")
@click.argument("name", metavar="[name]", required=False)
@click.option("--no-color", is_flag=True, help="Disable terminal styling.")
@click.pass_context
def profile_add_command(ctx: click.Context, name: str | None, no_color: bool) -> None:
    """Add a new profile; prompt for <name> when omitted. Keep the saved default."""
    init.configure_profile(
        options=_resolve_options(ctx, profile=None, no_color=no_color),
        mode="add",
        name=name,
    )


@profile_command.command(name="edit")
@click.argument("name", metavar="<name>")
@click.option("--no-color", is_flag=True, help="Disable terminal styling.")
@click.pass_context
def profile_edit_command(ctx: click.Context, name: str, no_color: bool) -> None:
    """Edit <name>'s games/characters, HoYoLAB session, or credential storage.

    Names must match exactly. Character changes reuse the saved session.
    Review before Save; Cancel leaves files unchanged. Global preferences
    belong to hoyo config; default switching belongs to hoyo profile use <name>.
    """
    init.configure_profile(
        options=_resolve_options(ctx, profile=None, no_color=no_color),
        mode="edit",
        name=name,
    )


@main.command(name="status")
@click.argument("game", type=click.Choice(GAME_IDS), metavar="[game]", required=False)
@_common_options(include_detailed=True)
@click.pass_context
def status_command(
    ctx: click.Context,
    game: GameId | None,
    profile: str | None,
    detailed: bool,
    no_color: bool,
) -> None:
    """Show all enabled games, or the focused status for [game]."""
    status.run_status(
        options=_resolve_options(
            ctx, profile=profile, detailed=detailed, no_color=no_color
        ),
        game=game,
    )


def _game_alias(game: GameId) -> click.Command:
    @click.command(name=game, help=f"Alias for 'hoyo status {game}'.")
    @_common_options(include_detailed=True)
    @click.pass_context
    def alias(
        ctx: click.Context, profile: str | None, detailed: bool, no_color: bool
    ) -> None:
        ctx.invoke(
            status_command,
            game=game,
            profile=profile,
            detailed=detailed,
            no_color=no_color,
        )

    return alias


for _game in GAME_IDS:
    main.add_command(_game_alias(_game))


@main.command(name="check-in")
@click.argument("game", type=click.Choice(GAME_IDS), metavar="[game]", required=False)
@_common_options()
@click.pass_context
def check_in_command(
    ctx: click.Context, game: GameId | None, profile: str | None, no_color: bool
) -> None:
    """Explicitly claim daily rewards for all enabled games, or [game].

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
        options=_resolve_options(ctx, profile=profile, no_color=no_color), game=game
    )


@main.command(name="init")
@click.option("--no-color", is_flag=True, help="Disable terminal styling.")
@click.pass_context
def init_command(ctx: click.Context, no_color: bool) -> None:
    """Set up the first profile interactively.

    Once initialized, show profile/config command guidance without changes.
    Use hoyo profile add to add a profile, hoyo profile edit <name> to update
    one, and hoyo config order to change global game order.
    Choose an optional Editor preference at first-run review, or later with
    hoyo config editor.
    """
    init.configure_profile(
        options=_resolve_options(ctx, profile=None, no_color=no_color)
    )


@main.group(name="config", invoke_without_command=True)
@click.option("--edit", is_flag=True, help="Open the config in the configured editor.")
@_common_options()
@click.pass_context
def config_command(
    ctx: click.Context, edit: bool, profile: str | None, no_color: bool
) -> None:
    """Inspect non-secret configuration, or open it with --edit.

    Shows enabled, disabled, and unconfigured games without reading secrets.
    Use hoyo config order for global order and hoyo profile edit <name> for
    characters, session, or credential storage.
    Use hoyo config editor to select a saved editor. --edit uses the saved
    preference, then VISUAL, then EDITOR. Wait-capable GUI presets use --wait; custom
    commands may require running hoyo config after saving to validate edits.
    """
    if ctx.invoked_subcommand is not None:
        if edit:
            raise click.UsageError("Use hoyo config --edit without a subcommand.")
        return
    config.run_config(
        options=_resolve_options(ctx, profile=profile, no_color=no_color), edit=edit
    )


@config_command.command(name="order")
def config_order_command() -> None:
    """Change only the global game display order, then review and save."""
    config.configure_order()


@config_command.command(name="editor")
@click.option("--no-color", is_flag=True, help="Disable terminal styling.")
@click.pass_context
def config_editor_command(ctx: click.Context, no_color: bool) -> None:
    """Choose the global editor, review and save, or clear it to use VISUAL/EDITOR.

    Only detected presets are listed. Custom executable paths are entered
    without quotes, and arguments individually. No editor opens during selection.
    """
    config.configure_editor(
        no_color=_resolve_options(ctx, profile=None, no_color=no_color).no_color
    )
