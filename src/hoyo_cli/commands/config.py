"""Non-secret config inspection and explicitly selected editor launching."""

import os
import subprocess
from pathlib import Path

import click

from hoyo_cli.commands import CommandOptions
from hoyo_cli.commands.init import (
    ClickSetupIO,
    SetupIO,
    choose_setup_option,
    select_editor,
    select_game_order,
)
from hoyo_cli.config.editor import (
    EDITOR_HINT,
    ResolvedEditor,
    describe_editor,
    editor_available,
    resolve_editor,
)
from hoyo_cli.config.models import AppConfig, ordered_games, resolve_account
from hoyo_cli.config.paths import config_path
from hoyo_cli.config.repository import (
    commit_config,
    load_config,
    load_config_snapshot,
    prepare_editor,
    prepare_game_order,
)
from hoyo_cli.errors import AppError, ConfigError
from hoyo_cli.render.setup import SetupPresenter


def show_config(config: AppConfig, path: Path) -> str:
    lines = [
        f"Config: {path}",
        f"Default account: {config.default_account}",
        f"Game order: {', '.join(config.game_order) or '(canonical fallback)'}",
        f"Display color: {config.display_color}",
        "Editor: " + describe_editor(config.editor_command),
        "Editor executable: "
        + ("available" if editor_available(config.editor_command) else "unavailable"),
    ]
    for name, account in config.accounts.items():
        lines.extend(
            [
                "",
                f"Account: {name}",
                f"  Credential backend: {account.credential_backend}",
            ]
        )
        any_enabled = False
        for game in ordered_games(config):
            selection = account.games.get(game)
            if selection is not None and selection.enabled:
                lines.append(f"  {game}: enabled, UID {selection.uid}")
                any_enabled = True
            elif selection is not None:
                lines.append(f"  {game}: disabled, UID {selection.uid}")
            else:
                lines.append(f"  {game}: not configured")
        if not any_enabled:
            lines.append("  No enabled games")
    lines.extend(
        [
            "",
            "Add account: hoyo account add",
            "Update account or session: hoyo account edit NAME",
            "Switch default: hoyo account use NAME",
            "Change game order: hoyo config order",
            "Change editor: hoyo config editor",
        ]
    )
    return "\n".join(lines)


def run_order(path: Path, io: SetupIO) -> None:
    config, original = load_config_snapshot(path)
    order = select_game_order(io, config)
    prepared = prepare_game_order(path, order)
    if prepared.original != original:
        raise ConfigError(
            "Configuration changed during game order selection.",
            hint="Retry hoyo config order.",
        )
    click.echo("Review game order: " + ", ".join(order), color=False)
    if (
        choose_setup_option(io, "Save game order?", ("Save", "Cancel (no changes)"))
        != "Save"
    ):
        raise click.Abort()
    if prepared.content.encode("utf-8") != original:
        commit_config(prepared)
    click.echo("Game order: " + ", ".join(order), color=False)


def configure_order() -> None:
    try:
        run_order(config_path(), ClickSetupIO())
    except (click.Abort, KeyboardInterrupt):
        click.echo("Game order cancelled; no changes saved.", color=False)
        raise


def run_editor(path: Path, io: SetupIO, *, no_color: bool = False) -> None:
    config, original = load_config_snapshot(path)
    presenter = SetupPresenter(no_color=no_color, configured_color=config.display_color)
    command = select_editor(io, presenter, config.editor_command)
    prepared = prepare_editor(path, command)
    if prepared.original != original:
        raise ConfigError(
            "Configuration changed during editor selection.",
            hint="Retry hoyo config editor.",
        )
    presenter.message("Review editor: " + describe_editor(command))
    if (
        choose_setup_option(
            io, "Save editor preference?", ("Save", "Cancel (no changes)")
        )
        != "Save"
    ):
        raise click.Abort()
    if prepared.content.encode("utf-8") != original:
        commit_config(prepared)
    presenter.message("Editor: " + describe_editor(command))


def configure_editor(*, no_color: bool = False) -> None:
    try:
        run_editor(config_path(), ClickSetupIO(), no_color=no_color)
    except (click.Abort, KeyboardInterrupt):
        click.echo("Editor cancelled; no changes saved.", color=False)
        raise


def edit_config(path: Path, editor: ResolvedEditor) -> int:
    try:
        arguments = [*editor.arguments, str(path)]
        if editor.node_cli:
            environment = dict(os.environ)
            environment["ELECTRON_RUN_AS_NODE"] = "1"
            environment.pop("VSCODE_DEV", None)
            result = subprocess.run(
                arguments, check=False, shell=False, env=environment
            )
        else:
            result = subprocess.run(arguments, check=False, shell=False)
    except (OSError, ValueError):
        raise ConfigError(
            f"Unable to launch configured editor.\nConfig: {path}", hint=EDITOR_HINT
        ) from None
    return result.returncode


def run_config(*, options: CommandOptions, edit: bool) -> None:
    path = config_path()
    config = load_config(path)
    resolve_account(config, options.account)
    if edit:
        try:
            editor = resolve_editor(config.editor_command)
        except (ConfigError, OSError) as error:
            message = (
                error.message
                if isinstance(error, ConfigError)
                else "Unable to resolve editor."
            )
            raise ConfigError(f"{message}\nConfig: {path}", hint=EDITOR_HINT) from None
        exit_code = edit_config(path, editor)
        if exit_code != 0:
            raise AppError(
                f"Editor exited with status {exit_code}.\nConfig: {path}",
                hint=(
                    "Edits were preserved. Review the config or choose "
                    "hoyo config editor."
                ),
            )
        if editor.waits:
            try:
                load_config(path)
            except ConfigError as error:
                raise ConfigError(
                    error.message,
                    hint=(
                        "Your edits were preserved. Correct the TOML/schema "
                        "in this file, then run hoyo config."
                    ),
                ) from None
            click.echo("Configuration validated after editor closed.", color=False)
        else:
            click.echo(
                "Editor launched; file-close completion is unconfirmed. "
                "After saving, run hoyo config to validate your edits.",
                color=False,
            )
    else:
        click.echo(show_config(config, path), color=False)
