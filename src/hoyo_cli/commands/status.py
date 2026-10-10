"""Status command boundary, shared by overview and game aliases."""

import asyncio
import sys
from datetime import datetime

import click

from hoyo_cli.auth.credentials import open_store
from hoyo_cli.commands import CommandOptions
from hoyo_cli.config.models import resolve_account, resolve_games
from hoyo_cli.config.paths import cache_root, config_path, credentials_path
from hoyo_cli.config.repository import load_config
from hoyo_cli.errors import AppError, CredentialError
from hoyo_cli.hoyolab.client import GenshinPyGateway
from hoyo_cli.models.common import GameId
from hoyo_cli.render.console import make_console
from hoyo_cli.render.overview import render_overview
from hoyo_cli.services.status import fetch_all_status


def _now() -> datetime:
    return datetime.now().astimezone()


def run_status(*, options: CommandOptions, game: GameId | None) -> None:
    config = load_config(config_path())
    name, account = resolve_account(config, options.account)
    games = resolve_games(config, account, game=game, account_name=name)
    credentials = open_store(account.credential_backend, credentials_path()).get(name)
    if credentials is None:
        raise CredentialError(f"No credentials for account {name}; run hoyo init.")
    try:
        gateway = GenshinPyGateway(credentials)
        results = asyncio.run(
            fetch_all_status(
                gateway,
                {selected: account.games[selected].uid for selected in games},
                account=name,
                cache_root=cache_root(),
                clock=_now,
            )
        )
    except asyncio.CancelledError:
        raise AppError("Status request cancelled.") from None
    except AppError:
        raise
    except Exception:
        raise AppError(
            "Unable to fetch status; retry or check account configuration."
        ) from None
    console = make_console(
        width=None,
        no_color=options.no_color,
        interactive=sys.stdout.isatty(),
        configured_color=config.display_color,
    )
    console.print(
        render_overview(
            results,
            account=name,
            detailed=options.detailed,
            now=_now(),
            focused=game is not None,
        )
    )
    if not any(result.status is not None for result in results):
        # Per-game errors were already printed alongside their sections.
        raise click.exceptions.Exit(1)
