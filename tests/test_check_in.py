"""Explicit actions preserve order, skip completed games, and isolate failures."""

import asyncio
from unittest.mock import AsyncMock, call

import pytest

from hoyo_cli.errors import (
    AlreadyCheckedInError,
    AppError,
    AuthenticationError,
    NetworkError,
)
from hoyo_cli.models.common import CheckInReceipt, CheckInState, GameId
from hoyo_cli.services.check_in import check_in_games


@pytest.mark.asyncio
async def test_already_checked_in_never_claims() -> None:
    gateway = AsyncMock()
    gateway.check_in_state.return_value = CheckInState(True)
    results = await check_in_games(gateway, {"genshin": 111111111})
    assert results[0].outcome == "already_checked_in"
    assert results[0].reward is None and results[0].error is None
    gateway.claim_check_in.assert_not_called()
    gateway.fetch_status.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("reward", [None, "20 Primogems"])
async def test_unclaimed_game_claims_once_with_optional_reward(
    reward: str | None,
) -> None:
    gateway = AsyncMock()
    gateway.check_in_state.return_value = CheckInState(False)
    gateway.claim_check_in.return_value = CheckInReceipt(reward)
    results = await check_in_games(gateway, {"zzz": 333333333})
    assert results[0].outcome == "claimed"
    assert results[0].reward == reward and results[0].error is None
    gateway.check_in_state.assert_awaited_once_with("zzz", 333333333)
    gateway.claim_check_in.assert_awaited_once_with("zzz", 333333333)
    gateway.fetch_status.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["read", "claim"])
@pytest.mark.parametrize(
    "error",
    [
        NetworkError("HoYoLAB request timed out."),
        AuthenticationError("Session expired."),
    ],
)
async def test_one_failure_does_not_cancel_others(stage: str, error: AppError) -> None:
    gateway = AsyncMock()

    async def state(game: GameId, uid: int) -> CheckInState:
        if game == "hsr" and stage == "read":
            raise error
        return CheckInState(game == "genshin")

    async def claim(game: GameId, uid: int) -> CheckInReceipt:
        if game == "hsr":
            raise error
        return CheckInReceipt()

    gateway.check_in_state.side_effect = state
    gateway.claim_check_in.side_effect = claim
    results = await check_in_games(
        gateway, {"hsr": 222222222, "zzz": 333333333, "genshin": 111111111}
    )
    assert [result.game for result in results] == ["hsr", "zzz", "genshin"]
    assert [result.outcome for result in results] == [
        "failed",
        "claimed",
        "already_checked_in",
    ]
    assert results[0].error is error
    assert gateway.claim_check_in.await_args_list == (
        [call("hsr", 222222222), call("zzz", 333333333)]
        if stage == "claim"
        else [call("zzz", 333333333)]
    )


@pytest.mark.asyncio
async def test_unexpected_errors_are_redacted_and_not_retried() -> None:
    gateway = AsyncMock()
    gateway.check_in_state.return_value = CheckInState(False)
    gateway.claim_check_in.side_effect = RuntimeError("test-secret-123")
    results = await check_in_games(gateway, {"hsr": 222222222})
    assert results[0].outcome == "failed"
    assert results[0].error is not None
    assert "test-secret-123" not in results[0].error.message
    gateway.claim_check_in.assert_awaited_once_with("hsr", 222222222)


@pytest.mark.asyncio
async def test_known_already_claimed_race_is_success_without_second_claim() -> None:
    gateway = AsyncMock()
    gateway.check_in_state.return_value = CheckInState(False)
    gateway.claim_check_in.side_effect = AlreadyCheckedInError()
    results = await check_in_games(gateway, {"hsr": 222222222})
    assert results[0].outcome == "already_checked_in"
    assert results[0].error is None and results[0].reward is None
    gateway.claim_check_in.assert_awaited_once_with("hsr", 222222222)


@pytest.mark.asyncio
async def test_read_finishes_before_claim_and_result_order_is_stable() -> None:
    gateway = AsyncMock()
    entered: set[GameId] = set()
    release = asyncio.Event()
    events: list[tuple[str, GameId]] = []

    async def state(game: GameId, uid: int) -> CheckInState:
        entered.add(game)
        if len(entered) == 3:
            release.set()
        await release.wait()
        events.append(("read", game))
        return CheckInState(False)

    async def claim(game: GameId, uid: int) -> CheckInReceipt:
        assert ("read", game) in events
        events.append(("claim", game))
        return CheckInReceipt()

    gateway.check_in_state.side_effect = state
    gateway.claim_check_in.side_effect = claim
    results = await asyncio.wait_for(
        check_in_games(
            gateway, {"zzz": 333333333, "hsr": 222222222, "genshin": 111111111}
        ),
        timeout=1,
    )
    assert [result.game for result in results] == ["zzz", "hsr", "genshin"]
    assert all(result.outcome == "claimed" for result in results)


@pytest.mark.asyncio
async def test_cancellation_cleans_up_children_without_claim_or_retry() -> None:
    gateway = AsyncMock()
    entered = asyncio.Event()
    count = 0
    cleaned: list[GameId] = []

    async def state(game: GameId, uid: int) -> CheckInState:
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(game)
        return CheckInState(False)

    gateway.check_in_state.side_effect = state
    task = asyncio.create_task(check_in_games(gateway, {"hsr": 2, "zzz": 3}))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(cleaned) == {"hsr", "zzz"}
    assert gateway.check_in_state.await_count == 2
    gateway.claim_check_in.assert_not_called()
