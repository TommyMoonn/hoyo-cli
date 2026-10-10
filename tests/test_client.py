"""Request attempts, deadlines, cancellation and the read-only gateway boundary."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import genshin
import pytest

from hoyo_cli.auth.credentials import parse_cookie_header
from hoyo_cli.errors import (
    AuthenticationError,
    ConfigError,
    NetworkError,
    UnsupportedDataError,
)
from hoyo_cli.hoyolab.client import GenshinPyGateway, create_client, request_with_retry
from hoyo_cli.models.common import GameId


@pytest.mark.asyncio
async def test_transient_error_gets_one_retry() -> None:
    operation = AsyncMock(
        side_effect=[aiohttp.ClientConnectionError("test-secret-123"), "ok"]
    )
    assert await request_with_retry(operation, timeout_seconds=1) == "ok"
    assert operation.await_count == 2
    operation = AsyncMock(side_effect=TimeoutError("test-secret-123"))
    with pytest.raises(NetworkError, match="timed out") as caught:
        await request_with_retry(operation, timeout_seconds=1)
    assert operation.await_count == 2
    assert "test-secret-123" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        genshin.errors.InvalidCookies(msg="test-secret-123"),
        ValueError("test-secret-123"),
        UnsupportedDataError("unsupported"),
    ],
)
async def test_auth_validation_and_unsupported_errors_are_not_retried(
    error: Exception,
) -> None:
    operation = AsyncMock(side_effect=error)
    with pytest.raises((AuthenticationError, UnsupportedDataError)):
        await request_with_retry(operation, timeout_seconds=1)
    assert operation.await_count == 1


@pytest.mark.asyncio
async def test_timeouts_are_bounded_and_cleanup_runs() -> None:
    closed: list[bool] = []

    async def hanging() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    with pytest.raises(NetworkError, match="timed out"):
        await asyncio.wait_for(
            request_with_retry(hanging, timeout_seconds=0.01), timeout=1
        )
    assert closed == [True, True]


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry() -> None:
    operation = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await request_with_retry(operation, timeout_seconds=1)
    assert operation.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code, transient",
    [(429, True), (503, True), (401, False), (403, False), (400, False)],
)
async def test_http_failures_are_classified_and_redacted(
    code: int, transient: bool
) -> None:
    operation = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            AsyncMock(), (), status=code, message="test-secret-123"
        )
    )
    with pytest.raises(
        (NetworkError, AuthenticationError, UnsupportedDataError)
    ) as caught:
        await request_with_retry(operation, timeout_seconds=1)
    assert operation.await_count == (2 if transient else 1)
    assert "test-secret-123" not in str(caught.value)


@pytest.mark.asyncio
async def test_pinned_transport_has_no_nested_retries_and_closes_sessions() -> None:
    client = create_client(parse_cookie_header("ltuid=123; ltoken=test-secret-123"))
    session = AsyncMock()
    session.__aenter__.return_value = session
    request = AsyncMock()
    request.__aenter__.side_effect = aiohttp.ClientConnectionError("test-secret-123")
    session.request = Mock(return_value=request)
    with patch.object(client.cookie_manager, "create_session", return_value=session):
        with pytest.raises(NetworkError):
            await request_with_retry(
                lambda: client.get_game_accounts(), timeout_seconds=1
            )
    assert request.__aenter__.await_count == 2
    assert session.__aexit__.await_count == 2


@pytest.mark.asyncio
async def test_pinned_session_enforces_http_status_checks() -> None:
    client = create_client(parse_cookie_header("ltuid=123; ltoken=test-secret-123"))
    async with client.cookie_manager.create_session() as session:
        assert session.raise_for_status is True
    assert session.closed


@pytest.mark.asyncio
async def test_gateway_is_read_only_and_normalizes_raw_notes() -> None:
    upstream = AsyncMock()
    upstream.get_genshin_notes.return_value = {"changed": "test-secret-123"}
    gateway = GenshinPyGateway(
        parse_cookie_header("ltuid=123; ltoken=test-secret-123"),
        client=upstream,
        now=lambda: datetime.now(UTC),
    )
    with pytest.raises(UnsupportedDataError):
        await gateway.fetch_status("genshin", 111111111)
    upstream.get_genshin_notes.assert_awaited_once_with(
        111111111, autoauth=False, return_raw_data=True
    )
    upstream.update_settings.assert_not_called()
    upstream.claim_daily_reward.assert_not_called()
    with pytest.raises(ConfigError):
        await gateway.fetch_status("genshin", 0)
    with pytest.raises(UnsupportedDataError):
        await gateway.fetch_status("hsr", 222222222)


@pytest.mark.asyncio
async def test_gateway_success_preserves_uid_and_snapshot_clock() -> None:
    captured = datetime(2026, 10, 10, 11, 31, tzinfo=UTC)
    client = AsyncMock()
    client.get_genshin_notes.return_value = json.loads(
        (Path(__file__).parent / "fixtures/genshin_notes.json").read_text()
    )
    gateway = GenshinPyGateway(
        parse_cookie_header("ltuid=123; ltoken=test-secret-123"),
        client=client,
        now=lambda: captured,
    )
    status = await gateway.fetch_status("genshin", 111111111)
    assert status.game == "genshin"
    from hoyo_cli.models.genshin import GenshinStatus

    assert isinstance(status, GenshinStatus)
    assert status.resin.current == 190
    assert status.resin.full_at is not None
    assert (status.resin.full_at - captured).total_seconds() == 4560


@pytest.mark.asyncio
async def test_real_notes_method_never_enables_settings() -> None:
    client = create_client(parse_cookie_header("ltuid=123; ltoken=test-secret-123"))
    with (
        patch.object(client, "_request_genshin_record", new_callable=AsyncMock) as read,
        patch.object(
            client, "_get_uid", new_callable=AsyncMock, return_value=111111111
        ),
        patch.object(client, "update_settings", new_callable=AsyncMock) as mutate,
    ):
        read.side_effect = genshin.errors.DataNotPublic(msg="test-secret-123")
        gateway = GenshinPyGateway(
            parse_cookie_header("ltuid=123; ltoken=test-secret-123"), client=client
        )
        from hoyo_cli.errors import AppError

        with pytest.raises(AppError):
            await gateway.fetch_status("genshin", 111111111)
    assert read.await_count == 1
    mutate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retcode, expected", [(-110, 2), (10101, 2), (-1, 2), (-100, 1)]
)
async def test_pinned_retcode_policy_has_exact_attempt_counts(
    retcode: int, expected: int
) -> None:
    client = create_client(parse_cookie_header("ltuid=123; ltoken=test-secret-123"))
    session = AsyncMock()
    response = AsyncMock()
    session.__aenter__.return_value = session
    response.__aenter__.return_value = response
    response.content_type = "application/json"
    response.cookies = {}
    response.json.return_value = {
        "retcode": retcode,
        "message": "test-secret-123",
        "data": None,
    }
    session.request = Mock(return_value=response)
    with patch.object(client.cookie_manager, "create_session", return_value=session):
        with pytest.raises((NetworkError, AuthenticationError)):
            await request_with_retry(
                lambda: client.get_game_accounts(), timeout_seconds=1
            )
    assert session.request.call_count == expected
    assert session.__aexit__.await_count == expected
    assert response.__aexit__.await_count == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "game, method", [("hsr", "get_starrail_notes"), ("zzz", "get_zzz_notes")]
)
async def test_phase5_gateway_preserves_selected_uid_and_read_only_options(
    game: GameId, method: str
) -> None:
    captured = datetime(2026, 10, 10, 11, 31, tzinfo=UTC)
    client = AsyncMock()
    fetch = getattr(client, method)
    fetch.return_value = json.loads(
        (Path(__file__).parent / f"fixtures/{game}_notes.json").read_text()
    )
    gateway = GenshinPyGateway(
        parse_cookie_header("ltuid=123; ltoken=test-secret-123"),
        client=client,
        now=lambda: captured,
    )
    status = await gateway.fetch_status(game, 222222222)
    assert status.game == game
    fetch.assert_awaited_once_with(222222222, autoauth=False, return_raw_data=True)
    client.update_settings.assert_not_called()
    client.claim_daily_reward.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "game, method",
    [("hsr", "_request_starrail_record"), ("zzz", "_request_zzz_record")],
)
async def test_phase5_real_notes_methods_never_enable_settings(
    game: GameId, method: str
) -> None:
    credentials = parse_cookie_header("ltuid=123; ltoken=test-secret-123")
    client = create_client(credentials)
    with (
        patch.object(client, method, new_callable=AsyncMock) as read,
        patch.object(
            client, "_get_uid", new_callable=AsyncMock, return_value=222222222
        ),
        patch.object(client, "update_settings", new_callable=AsyncMock) as mutate,
    ):
        read.side_effect = genshin.errors.DataNotPublic(msg="test-secret-123")
        from hoyo_cli.errors import AppError

        with pytest.raises(AppError):
            await GenshinPyGateway(credentials, client=client).fetch_status(
                game, 222222222
            )
    assert read.await_count == 1
    mutate.assert_not_called()
