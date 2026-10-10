"""Independent, ordered check-in actions with no status or cache side effects."""

import asyncio
from collections.abc import Mapping

from hoyo_cli.errors import AlreadyCheckedInError, AppError
from hoyo_cli.hoyolab.client import HoyolabGateway
from hoyo_cli.models.common import CheckInResult, GameId
from hoyo_cli.models.progress import ProgressEvent, ProgressObserver, notify_progress


async def check_in_games(
    gateway: HoyolabGateway,
    requested: Mapping[GameId, int],
    *,
    observer: ProgressObserver | None = None,
) -> tuple[CheckInResult, ...]:
    async def check_one(game: GameId, uid: int) -> CheckInResult:
        notify_progress(observer, ProgressEvent(game, "checking"))
        result = await check_result(game, uid)
        notify_progress(observer, ProgressEvent(game, "complete", result.outcome))
        return result

    async def check_result(game: GameId, uid: int) -> CheckInResult:
        try:
            state = await gateway.check_in_state(game, uid)
            if state.already_checked_in:
                return CheckInResult(game, "already_checked_in")
            notify_progress(observer, ProgressEvent(game, "claiming"))
            receipt = await gateway.claim_check_in(game, uid)
            return CheckInResult(game, "claimed", reward=receipt.reward)
        except AlreadyCheckedInError:
            # Another caller may have claimed between our state read and action.
            return CheckInResult(game, "already_checked_in")
        except Exception as outcome:
            error = (
                outcome
                if isinstance(outcome, AppError)
                else AppError(
                    "HoYoLAB check-in failed.",
                    hint="Check today's state on HoYoLAB before trying check-in again.",
                )
            )
            return CheckInResult(game, "failed", error=error)

    outcomes = await asyncio.gather(
        *(check_one(game, uid) for game, uid in requested.items()),
        return_exceptions=True,
    )
    results: list[CheckInResult] = []
    for outcome in outcomes:
        # Cancellation must propagate, rather than appear to be a completed action.
        if isinstance(outcome, BaseException):
            raise outcome
        results.append(outcome)
    return tuple(results)
