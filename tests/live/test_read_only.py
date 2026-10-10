"""Manual opt-in: real status reads, no reward claims or notes authorization."""

import os

import pytest

from hoyo_cli.auth.credentials import open_store
from hoyo_cli.config.models import resolve_account, resolve_games
from hoyo_cli.config.paths import config_path, credentials_path
from hoyo_cli.config.repository import load_config
from hoyo_cli.errors import AppError
from hoyo_cli.hoyolab.client import GenshinPyGateway


@pytest.mark.live_read_only
@pytest.mark.asyncio
async def test_configured_account_status_is_read_only(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Also guard direct selection of this file, which can bypass directory ignores.
    if not request.config.getoption("--live-read-only"):
        pytest.skip("Select --live-read-only explicitly to access HoYoLAB.")
    if not config_path().is_file():
        pytest.skip("Live status requires configuration from hoyo init.")
    try:
        config = load_config(config_path())
        name, account = resolve_account(config, os.environ.get("HOYO_LIVE_ACCOUNT"))
        games = resolve_games(config, account, game=None, account_name=name)
        credentials = open_store(account.credential_backend, credentials_path()).get(
            name
        )
    except AppError:
        pytest.skip(
            "Live status requires valid config and an available credential backend."
        )
    if credentials is None:
        pytest.skip("Live status requires stored credentials from hoyo init.")

    async def reject_claim(*args: object, **kwargs: object) -> None:
        pytest.fail("Live verification must never claim daily rewards.", pytrace=False)

    monkeypatch.setattr(GenshinPyGateway, "claim_check_in", reject_claim)
    gateway = GenshinPyGateway(credentials)
    for game in games:
        try:
            result = await gateway.fetch_status(game, account.games[game].uid)
        except AppError as error:
            pytest.fail(f"{game}: {error.message}", pytrace=False)
        assert result.game == game
