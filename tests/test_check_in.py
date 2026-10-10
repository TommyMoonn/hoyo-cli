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
from hoyo_cli.models.progress import ProgressEvent
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
@pytest.mark.parametrize("verification", [True, False, "failed"])
async def test_ambiguous_claim_checks_state_once_and_reports_uncertainty(
    verification: bool | str,
) -> None:
    from hoyo_cli.errors import AmbiguousClaimError

    gateway = AsyncMock()
    gateway.check_in_state.side_effect = [
        CheckInState(False),
        NetworkError("Read failed.")
        if verification == "failed"
        else CheckInState(verification is True),
    ]
    gateway.claim_check_in.side_effect = AmbiguousClaimError(
        NetworkError("Request timed out.")
    )
    events: list[ProgressEvent] = []
    (result,) = await check_in_games(gateway, {"hsr": 2}, observer=events.append)
    assert result.outcome == (
        "claimed_verified" if verification is True else "unconfirmed"
    )
    assert result.reward is None
    assert (result.error is None) is (verification is True)
    gateway.check_in_state.assert_has_awaits([call("hsr", 2), call("hsr", 2)])
    assert gateway.check_in_state.await_count == 2
    gateway.claim_check_in.assert_awaited_once_with("hsr", 2)
    gateway.fetch_status.assert_not_called()
    assert events == [
        ProgressEvent("hsr", "checking"),
        ProgressEvent("hsr", "claiming"),
        ProgressEvent("hsr", "verifying"),
        ProgressEvent("hsr", "complete", result.outcome),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["checking", "claiming", "verifying"])
async def test_reconciliation_cancellation_propagates_without_completion(
    stage: str,
) -> None:
    from hoyo_cli.errors import AmbiguousClaimError

    gateway = AsyncMock()
    gateway.check_in_state.side_effect = (
        asyncio.CancelledError
        if stage == "checking"
        else [CheckInState(False), asyncio.CancelledError()]
    )
    gateway.claim_check_in.side_effect = (
        asyncio.CancelledError
        if stage == "claiming"
        else AmbiguousClaimError(NetworkError("Request timed out."))
    )
    events: list[ProgressEvent] = []
    with pytest.raises(asyncio.CancelledError):
        await check_in_games(gateway, {"zzz": 3}, observer=events.append)
    assert not any(event.stage == "complete" for event in events)
    assert events[-1].stage == stage
    assert gateway.claim_check_in.await_count == (stage != "checking")
    assert gateway.check_in_state.await_count == (2 if stage == "verifying" else 1)


@pytest.mark.asyncio
async def test_reconciliation_is_concurrent_isolated_and_keeps_result_order() -> None:
    from hoyo_cli.errors import AmbiguousClaimError

    gateway = AsyncMock()
    verifying = asyncio.Event()
    sibling_complete = asyncio.Event()
    reads: list[tuple[GameId, int]] = []
    events: list[ProgressEvent] = []

    async def state(game: GameId, uid: int) -> CheckInState:
        reads.append((game, uid))
        if reads.count((game, uid)) == 2:
            verifying.set()
            await sibling_complete.wait()
            return CheckInState(True)
        return CheckInState(False)

    async def claim(game: GameId, uid: int) -> CheckInReceipt:
        if game == "hsr":
            raise AmbiguousClaimError(NetworkError("Request timed out."))
        await verifying.wait()
        return CheckInReceipt("Safe reward")

    def observe(event: ProgressEvent) -> None:
        events.append(event)
        if event == ProgressEvent("zzz", "complete", "claimed"):
            sibling_complete.set()

    gateway.check_in_state.side_effect = state
    gateway.claim_check_in.side_effect = claim
    results = await asyncio.wait_for(
        check_in_games(gateway, {"hsr": 2, "zzz": 3}, observer=observe), timeout=1
    )
    assert [result.game for result in results] == ["hsr", "zzz"]
    assert [result.outcome for result in results] == ["claimed_verified", "claimed"]
    assert results[0].reward is None and results[1].reward == "Safe reward"
    assert [e.game for e in events if e.stage == "complete"] == ["zzz", "hsr"]
    assert reads.count(("hsr", 2)) == 2
    assert reads.count(("zzz", 3)) == 1
    assert len(reads) == 3
    assert gateway.claim_check_in.await_count == 2


@pytest.mark.asyncio
async def test_read_finishes_before_claim_and_result_order_is_stable() -> None:
    gateway = AsyncMock()
    entered: set[GameId] = set()
    release = asyncio.Event()
    events: list[tuple[str, GameId]] = []
    observed: list[ProgressEvent] = []

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
            gateway,
            {"zzz": 333333333, "hsr": 222222222, "genshin": 111111111},
            observer=observed.append,
        ),
        timeout=1,
    )
    assert [result.game for result in results] == ["zzz", "hsr", "genshin"]
    assert all(result.outcome == "claimed" for result in results)
    for game in ("genshin", "hsr", "zzz"):
        assert [e.stage for e in observed if e.game == game] == [
            "checking",
            "claiming",
            "complete",
        ]


@pytest.mark.asyncio
async def test_failed_preflight_observes_no_claim_stage() -> None:
    gateway = AsyncMock()
    gateway.check_in_state.side_effect = NetworkError("Request timed out.")
    observed: list[ProgressEvent] = []
    (result,) = await check_in_games(gateway, {"zzz": 3}, observer=observed.append)
    assert result.outcome == "failed"
    assert observed == [
        ProgressEvent("zzz", "checking"),
        ProgressEvent("zzz", "complete", "failed"),
    ]
    gateway.claim_check_in.assert_not_called()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["claimed", "already_checked_in", "failed"])
async def test_check_in_progress_tracks_real_stages(outcome: str) -> None:
    gateway = AsyncMock()
    gateway.check_in_state.return_value = CheckInState(outcome == "already_checked_in")
    gateway.claim_check_in.return_value = CheckInReceipt("safe reward")
    if outcome == "failed":
        gateway.claim_check_in.side_effect = RuntimeError("cookie-secret")
    events: list[ProgressEvent] = []
    (result,) = await check_in_games(gateway, {"zzz": 3}, observer=events.append)
    assert result.outcome == outcome
    expected = [ProgressEvent("zzz", "checking")]
    if outcome != "already_checked_in":
        expected.append(ProgressEvent("zzz", "claiming"))
    expected.append(ProgressEvent("zzz", "complete", result.outcome))
    assert events == expected
    assert "cookie-secret" not in repr(events) and "safe reward" not in repr(events)
    assert gateway.claim_check_in.await_count == (outcome != "already_checked_in")


@pytest.mark.asyncio
async def test_check_in_observer_failure_does_not_repeat_claim() -> None:
    gateway = AsyncMock()
    gateway.check_in_state.return_value = CheckInState(False)
    gateway.claim_check_in.return_value = CheckInReceipt()

    def broken(event: ProgressEvent) -> None:
        raise ValueError("observer-secret")

    (result,) = await check_in_games(gateway, {"hsr": 2}, observer=broken)
    assert result.outcome == "claimed"
    gateway.claim_check_in.assert_awaited_once_with("hsr", 2)


@pytest.mark.asyncio
async def test_check_in_progress_order_and_claim_cancellation() -> None:
    gateway = AsyncMock()
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    events: list[ProgressEvent] = []
    gateway.check_in_state.return_value = CheckInState(False)

    async def claim(game: GameId, uid: int) -> CheckInReceipt:
        if game == "zzz":
            await entered.wait()
            return CheckInReceipt()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
        raise AssertionError("unreachable")

    gateway.claim_check_in.side_effect = claim

    def observe(event: ProgressEvent) -> None:
        events.append(event)
        if event == ProgressEvent("zzz", "complete", "claimed"):
            task.cancel()

    task = asyncio.create_task(
        check_in_games(
            gateway,
            {"hsr": 2, "zzz": 3},
            observer=observe,
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert cleaned.is_set()
    assert [e.game for e in events if e.stage == "complete"] == ["zzz"]
    assert gateway.claim_check_in.await_count == 2
