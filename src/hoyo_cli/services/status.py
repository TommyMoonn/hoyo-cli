"""Concurrent status reads with ordered, independent per-game outcomes."""

import asyncio
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from hoyo_cli.errors import AppError
from hoyo_cli.hoyolab.client import HoyolabGateway
from hoyo_cli.models.common import GameId, StatusResult, require_aware


async def fetch_all_status(
    gateway: HoyolabGateway,
    requested: Mapping[GameId, int],
    *,
    account: str,
    cache_root: Path,
    now: datetime,
) -> tuple[StatusResult, ...]:
    require_aware(now)
    # Account and cache_root are reserved for Phase 6's per-game fallback.
    requests = tuple(requested.items())
    outcomes = await asyncio.gather(
        *(gateway.fetch_status(game, uid) for game, uid in requests),
        return_exceptions=True,
    )
    results: list[StatusResult] = []
    for (game, _), outcome in zip(requests, outcomes, strict=True):
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome
            error = (
                outcome
                if isinstance(outcome, AppError)
                else AppError(
                    "HoYoLAB status request failed; "
                    "retry or check account configuration."
                )
            )
            results.append(StatusResult(game, None, None, error=error))
        elif outcome.game != game:
            results.append(
                StatusResult(
                    game,
                    None,
                    None,
                    error=AppError("HoYoLAB returned status for an unexpected game."),
                )
            )
        else:
            results.append(StatusResult(game, outcome, now))
    return tuple(results)
