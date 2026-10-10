"""Concurrent read orchestration and independent typed results."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hoyo_cli.errors import NetworkError
from hoyo_cli.hoyolab.adapters.genshin import normalize_genshin
from hoyo_cli.models.common import GameId, GameStatus
from hoyo_cli.services.status import fetch_all_status

NOW = datetime(2026, 10, 10, 11, 31, tzinfo=UTC)


def snapshots() -> dict[GameId, GameStatus]:
    from hoyo_cli.hoyolab.adapters.hsr import normalize_hsr
    from hoyo_cli.hoyolab.adapters.zzz import normalize_zzz

    def read(game: str) -> object:
        return json.loads(
            (Path(__file__).parent / f"fixtures/{game}_notes.json").read_text()
        )

    return {
        "genshin": normalize_genshin(read("genshin"), now=NOW),
        "hsr": normalize_hsr(read("hsr"), now=NOW),
        "zzz": normalize_zzz(read("zzz"), now=NOW),
    }


@pytest.mark.asyncio
async def test_all_three_fetch_concurrently(tmp_path: Path) -> None:
    gateway = AsyncMock()
    started: list[GameId] = []
    completed: list[GameId] = []
    release = asyncio.Event()
    hsr_done = asyncio.Event()
    genshin_done = asyncio.Event()
    data = snapshots()

    async def fetch(game: GameId, uid: int) -> GameStatus:
        started.append(game)
        if len(started) == 3:
            release.set()
        await release.wait()
        if game == "genshin":
            await hsr_done.wait()
            genshin_done.set()
        elif game == "zzz":
            await genshin_done.wait()
        else:
            hsr_done.set()
        completed.append(game)
        return data[game]

    gateway.fetch_status.side_effect = fetch
    results = await asyncio.wait_for(
        fetch_all_status(
            gateway,
            {"zzz": 3, "genshin": 1, "hsr": 2},
            account="main",
            cache_root=tmp_path,
            now=NOW,
        ),
        timeout=1,
    )
    assert [r.game for r in results] == ["zzz", "genshin", "hsr"]
    assert completed == ["hsr", "genshin", "zzz"]
    assert all(
        r.status is not None and r.fetched_at == NOW and not r.stale for r in results
    )
    assert list(tmp_path.iterdir()) == []
    gateway.check_in_state.assert_not_called()
    gateway.claim_check_in.assert_not_called()


@pytest.mark.asyncio
async def test_one_failure_keeps_other_two_sections(tmp_path: Path) -> None:
    data = snapshots()
    gateway = AsyncMock()
    error = NetworkError("HoYoLAB request timed out.")

    async def fetch(game: GameId, uid: int) -> GameStatus:
        if game == "hsr":
            raise error
        return data[game]

    gateway.fetch_status.side_effect = fetch
    results = await fetch_all_status(
        gateway,
        {"genshin": 1, "hsr": 2, "zzz": 3},
        account="main",
        cache_root=tmp_path,
        now=NOW,
    )
    assert results[0].status is data["genshin"] and results[2].status is data["zzz"]
    assert results[1].status is None and results[1].error is error


@pytest.mark.asyncio
async def test_cancellation_propagates_and_cleans_children(tmp_path: Path) -> None:
    gateway = AsyncMock()
    started = asyncio.Event()
    closed: list[bool] = []

    async def fetch(game: GameId, uid: int) -> GameStatus:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)
        raise AssertionError("unreachable")

    gateway.fetch_status.side_effect = fetch
    task = asyncio.create_task(
        fetch_all_status(
            gateway,
            {"genshin": 1, "hsr": 2},
            account="main",
            cache_root=tmp_path,
            now=NOW,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True, True]


@pytest.mark.asyncio
async def test_wrong_game_does_not_suppress_independent_results(tmp_path: Path) -> None:
    gateway = AsyncMock()
    data = snapshots()
    gateway.fetch_status.side_effect = [data["zzz"], data["hsr"]]
    results = await fetch_all_status(
        gateway, {"genshin": 1, "hsr": 2}, account="main", cache_root=tmp_path, now=NOW
    )
    assert results[0].status is None and results[0].error is not None
    assert results[1].status is data["hsr"]
