"""Explicit daily check-in command boundary."""

import asyncio
import sys

import click

from hoyo_cli.auth.credentials import open_store
from hoyo_cli.commands import CommandOptions
from hoyo_cli.config.models import resolve_account, resolve_games
from hoyo_cli.config.paths import config_path, credentials_path
from hoyo_cli.config.repository import load_config
from hoyo_cli.errors import AppError, CredentialError
from hoyo_cli.hoyolab.client import GenshinPyGateway
from hoyo_cli.models.common import GameId
from hoyo_cli.render.check_in import render_check_in
from hoyo_cli.render.console import make_console
from hoyo_cli.services.check_in import check_in_games


def run_check_in(*, options: CommandOptions, game: GameId | None) -> None:
    config = load_config(config_path())
    name, account = resolve_account(config, options.account)
    games = resolve_games(config, account, game=game, account_name=name)
    credentials = open_store(account.credential_backend, credentials_path()).get(name)
    if credentials is None:
        raise CredentialError(f"No credentials for account {name}; run hoyo init.")
    try:
        results = asyncio.run(
            check_in_games(
                GenshinPyGateway(credentials),
                {selected: account.games[selected].uid for selected in games},
            )
        )
    except asyncio.CancelledError:
        raise AppError(
            "Check-in cancelled; completed claims remain claimed. "
            "Check today's state before trying again."
        ) from None
    except AppError:
        raise
    except Exception:
        raise AppError(
            "Unable to complete check-in; check today's state before trying again."
        ) from None
    console = make_console(
        width=None,
        no_color=options.no_color,
        interactive=sys.stdout.isatty(),
        configured_color=config.display_color,
    )
    console.print(render_check_in(results, account=name))
    if not results or any(result.outcome == "failed" for result in results):
        raise click.exceptions.Exit(1)
