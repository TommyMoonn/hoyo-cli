"""Explicit daily check-in command boundary."""

import asyncio
import sys

import click

from hoyo_cli.auth.credentials import open_store
from hoyo_cli.commands import CommandOptions
from hoyo_cli.config.models import resolve_games, resolve_login, resolve_profile
from hoyo_cli.config.paths import config_path, credentials_path
from hoyo_cli.config.repository import load_config
from hoyo_cli.errors import AppError, CredentialError, ErrorCode
from hoyo_cli.hoyolab.client import GenshinPyGateway
from hoyo_cli.models.common import CheckInResult, GameId
from hoyo_cli.render.check_in import render_check_in
from hoyo_cli.render.console import make_console
from hoyo_cli.render.progress import OperationProgress
from hoyo_cli.services.check_in import check_in_games


def run_check_in(*, options: CommandOptions, game: GameId | None) -> None:
    config = load_config(config_path())
    name, profile = resolve_profile(config, options.profile)
    games = resolve_games(config, profile, game=game, profile_name=name)
    login = resolve_login(config, profile)
    credentials = open_store(login.credential_backend, credentials_path()).get(
        login.credential_key
    )
    if credentials is None:
        raise CredentialError(
            f"No credentials for profile {name}.",
            code=ErrorCode.CREDENTIALS_MISSING,
            hint="Run: hoyo profile edit <name>",
        )
    try:
        gateway = GenshinPyGateway(credentials)

        async def claim() -> tuple[CheckInResult, ...]:
            async with OperationProgress(
                "check_in",
                games,
                no_color=options.no_color,
                configured_color=config.display_color,
            ) as progress:
                return await check_in_games(
                    gateway,
                    {selected: profile.games[selected].uid for selected in games},
                    observer=progress.observe,
                )

        results = asyncio.run(claim())
    except asyncio.CancelledError:
        raise AppError(
            "Check-in cancelled; completed claims remain claimed.",
            code=ErrorCode.CANCELLED,
            hint="Check today's state on HoYoLAB before trying check-in again.",
        ) from None
    except AppError:
        raise
    except Exception:
        raise AppError(
            "Unable to complete check-in.",
            hint="Check today's state on HoYoLAB before trying check-in again.",
        ) from None
    console = make_console(
        width=None,
        no_color=options.no_color,
        interactive=sys.stdout.isatty(),
        configured_color=config.display_color,
    )
    console.print(render_check_in(results, profile=name))
    if not results or any(
        result.outcome in ("failed", "unconfirmed") for result in results
    ):
        raise click.exceptions.Exit(1)
