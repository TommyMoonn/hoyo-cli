"""Concurrent status reads with ordered, independent per-game outcomes."""

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from hoyo_cli.cache.models import CACHE_VERSION, CacheEntry
from hoyo_cli.cache.repository import read_eligible, write_cache
from hoyo_cli.errors import AppError, ErrorCode, UnsupportedDataError
from hoyo_cli.hoyolab.client import HoyolabGateway
from hoyo_cli.models.common import GameId, StatusResult, require_aware
from hoyo_cli.models.progress import ProgressEvent, ProgressObserver, notify_progress


async def fetch_all_status(
    gateway: HoyolabGateway,
    requested: Mapping[GameId, int],
    *,
    account: str,
    cache_root: Path,
    clock: Callable[[], datetime],
    observer: ProgressObserver | None = None,
) -> tuple[StatusResult, ...]:
    async def fetch_one(game: GameId, uid: int) -> StatusResult:
        notify_progress(observer, ProgressEvent(game, "fetching"))
        result = await fetch_result(game, uid)
        outcome = (
            "stale"
            if result.stale
            else "fresh"
            if result.status is not None
            else "failed"
        )
        notify_progress(observer, ProgressEvent(game, "complete", outcome))
        return result

    async def fetch_result(game: GameId, uid: int) -> StatusResult:
        try:
            snapshot = await gateway.fetch_status(game, uid)
            if snapshot.game != game:
                raise UnsupportedDataError(
                    "HoYoLAB returned status for an unexpected game."
                )
        except Exception as outcome:
            error = (
                outcome
                if isinstance(outcome, AppError)
                else AppError("HoYoLAB status request failed.")
            )
            cached = read_eligible(cache_root, account, game, uid=uid, now=clock())
            if cached is not None:
                return StatusResult(
                    game, cached.status, cached.fetched_at, stale=True, error=error
                )
            return StatusResult(game, None, None, error=error)

        fetched_at = clock()
        require_aware(fetched_at)
        cache_error: AppError | None = None
        try:
            write_cache(
                cache_root,
                CacheEntry(CACHE_VERSION, account, game, fetched_at, snapshot, uid=uid),
            )
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            cache_error = AppError(
                "Unable to save status cache; fresh status is still available.",
                code=ErrorCode.CACHE_WRITE,
                hint="Check access to the status cache directory, then retry.",
            )
        return StatusResult(game, snapshot, fetched_at, cache_error=cache_error)

    outcomes = await asyncio.gather(
        *(fetch_one(game, uid) for game, uid in requested.items()),
        return_exceptions=True,
    )
    results: list[StatusResult] = []
    for outcome in outcomes:
        # Cancellation is a control signal, never a failed request eligible for
        # fallback. gather also keeps external cancellation cleanup centralized.
        if isinstance(outcome, BaseException):
            raise outcome
        results.append(outcome)
    return tuple(results)
