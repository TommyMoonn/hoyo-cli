"""Non-secret config inspection and explicitly selected editor launching."""

import os
import subprocess
from pathlib import Path

import click

from hoyo_cli.commands import CommandOptions
from hoyo_cli.commands.init import (
    SetupIO,
    choose_setup_option,
    select_editor,
    select_game_order,
    setup_io,
)
from hoyo_cli.config.editor import (
    EDITOR_HINT,
    ResolvedEditor,
    describe_editor,
    resolve_editor,
)
from hoyo_cli.config.models import resolve_profile
from hoyo_cli.config.paths import config_path
from hoyo_cli.config.repository import (
    commit_config,
    load_config,
    load_config_snapshot,
    prepare_editor,
    prepare_game_order,
)
from hoyo_cli.errors import AppError, ConfigError
from hoyo_cli.render.management import ManagementPresenter
from hoyo_cli.render.setup import SetupPresenter


def run_order(path: Path, io: SetupIO | None = None, *, no_color: bool = False) -> None:
    config, original = load_config_snapshot(path)
    presenter = SetupPresenter(no_color=no_color, configured_color=config.display_color)
    io = setup_io(io, presenter)
    presenter.heading("Game display order")
    presenter.current("Current game order: " + ", ".join(config.game_order))
    try:
        order = select_game_order(io, config)
        prepared = prepare_game_order(path, order)
        if prepared.original != original:
            raise ConfigError(
                "Configuration changed during game order selection.",
                hint="Retry hoyo config order.",
            )
        presenter.heading("Review game order: " + ", ".join(order))
        if (
            choose_setup_option(io, "Save game order?", ("Save", "Cancel (no changes)"))
            != "Save"
        ):
            raise click.Abort()
        if prepared.content.encode("utf-8") != original:
            commit_config(prepared)
            presenter.success("Game order: " + ", ".join(order))
        else:
            presenter.message("Game order: " + ", ".join(order))
    except (click.Abort, KeyboardInterrupt):
        presenter.message("Game order cancelled; no changes saved.")
        raise


def configure_order(*, no_color: bool = False) -> None:
    run_order(config_path(), no_color=no_color)


def run_editor(
    path: Path, io: SetupIO | None = None, *, no_color: bool = False
) -> None:
    config, original = load_config_snapshot(path)
    presenter = SetupPresenter(no_color=no_color, configured_color=config.display_color)
    io = setup_io(io, presenter)
    presenter.heading("Editor preference")
    try:
        command = select_editor(io, presenter, config.editor_command)
        prepared = prepare_editor(path, command)
        if prepared.original != original:
            raise ConfigError(
                "Configuration changed during editor selection.",
                hint="Retry hoyo config editor.",
            )
        presenter.heading("Review editor: " + describe_editor(command))
        if (
            choose_setup_option(
                io, "Save editor preference?", ("Save", "Cancel (no changes)")
            )
            != "Save"
        ):
            raise click.Abort()
        if prepared.content.encode("utf-8") != original:
            commit_config(prepared)
            presenter.success("Editor: " + describe_editor(command))
        else:
            presenter.message("Editor: " + describe_editor(command))
    except (click.Abort, KeyboardInterrupt):
        presenter.message("Editor cancelled; no changes saved.")
        raise


def configure_editor(*, no_color: bool = False) -> None:
    run_editor(config_path(), no_color=no_color)


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
    presenter = ManagementPresenter(
        no_color=options.no_color, configured_color=config.display_color
    )
    resolve_profile(config, options.profile)
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
            presenter.success("Configuration validated after editor closed.")
        else:
            presenter.message(
                "Editor launched; file-close completion is unconfirmed. "
                "After saving, run hoyo config to validate your edits.",
            )
    else:
        presenter.config(config, path)
