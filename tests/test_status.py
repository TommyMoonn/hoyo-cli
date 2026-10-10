"""Concurrent read orchestration and independent typed results."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hoyo_cli.cache.models import CacheEntry
from hoyo_cli.cache.repository import read_eligible, write_cache
from hoyo_cli.errors import NetworkError
from hoyo_cli.hoyolab.adapters.genshin import normalize_genshin
from hoyo_cli.models.common import GameId, GameStatus
from hoyo_cli.services.status import fetch_all_status

NOW = datetime(2026, 10, 10, 11, 31, tzinfo=UTC)
UIDS: dict[GameId, int] = {"genshin": 1, "hsr": 2, "zzz": 3}


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
            clock=lambda: NOW,
        ),
        timeout=1,
    )
    assert [r.game for r in results] == ["zzz", "genshin", "hsr"]
    assert completed == ["hsr", "genshin", "zzz"]
    assert all(
        r.status is not None and r.fetched_at == NOW and not r.stale for r in results
    )
    for game in data:
        cached = read_eligible(tmp_path, "main", game, uid=UIDS[game], now=NOW)
        assert cached is not None and cached.status == data[game]
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
        clock=lambda: NOW,
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
            clock=lambda: NOW,
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
        gateway,
        {"genshin": 1, "hsr": 2},
        account="main",
        cache_root=tmp_path,
        clock=lambda: NOW,
    )
    assert results[0].status is None and results[0].error is not None
    assert results[1].status is data["hsr"]


@pytest.mark.asyncio
async def test_fresh_request_even_when_cache_exists(tmp_path: Path) -> None:
    data = snapshots()
    old = CacheEntry(
        1, "main", "genshin", NOW - timedelta(hours=1), data["genshin"], uid=1
    )
    write_cache(tmp_path, old)
    gateway = AsyncMock()
    gateway.fetch_status.return_value = data["genshin"]
    results = await fetch_all_status(
        gateway, {"genshin": 1}, account="main", cache_root=tmp_path, clock=lambda: NOW
    )
    gateway.fetch_status.assert_awaited_once_with("genshin", 1)
    assert not results[0].stale and results[0].fetched_at == NOW
    cached = read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW)
    assert cached is not None and cached.fetched_at == NOW


@pytest.mark.asyncio
async def test_partial_stale_fallback_per_game(tmp_path: Path) -> None:
    data = snapshots()
    old = CacheEntry(1, "main", "hsr", NOW - timedelta(minutes=34), data["hsr"], uid=2)
    write_cache(tmp_path, old)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    error = NetworkError("HoYoLAB request timed out.")
    gateway = AsyncMock()

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
        clock=lambda: NOW,
    )
    assert [r.stale for r in results] == [False, True, False]
    assert results[1].status == old.status and results[1].fetched_at == old.fetched_at
    assert results[1].error is error
    assert all(p.read_bytes() == content for p, content in before.items())
    for game in ("genshin", "zzz"):
        cached = read_eligible(tmp_path, "main", game, uid=UIDS[game], now=NOW)
        assert cached is not None and cached.fetched_at == NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["corrupt", "expired", "future", "other-account"])
async def test_ineligible_cache_does_not_mask_error(tmp_path: Path, case: str) -> None:
    old = CacheEntry(1, "main", "hsr", NOW, snapshots()["hsr"], uid=2)
    if case == "expired":
        old = replace(old, fetched_at=NOW - timedelta(hours=3, seconds=1))
    elif case == "future":
        old = replace(old, fetched_at=NOW + timedelta(seconds=1))
    elif case == "other-account":
        old = replace(old, account="alt")
    write_cache(tmp_path, old)
    if case == "corrupt":
        next(tmp_path.rglob("*.json")).write_text("{broken")
    error = NetworkError("HoYoLAB request timed out.")
    gateway = AsyncMock()
    gateway.fetch_status.side_effect = error
    results = await fetch_all_status(
        gateway, {"hsr": 2}, account="main", cache_root=tmp_path, clock=lambda: NOW
    )
    assert results[0].status is None and results[0].error is error


@pytest.mark.asyncio
async def test_cache_write_failure_preserves_fresh_and_other_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hoyo_cli.services import status

    data = snapshots()
    gateway = AsyncMock()
    gateway.fetch_status.side_effect = [data["genshin"], data["hsr"]]

    def write(root: Path, entry: CacheEntry) -> None:
        if entry.game == "genshin":
            raise PermissionError("test-secret-123")
        write_cache(root, entry)

    monkeypatch.setattr(status, "write_cache", write)
    results = await fetch_all_status(
        gateway,
        {"genshin": 1, "hsr": 2},
        account="main",
        cache_root=tmp_path,
        clock=lambda: NOW,
    )
    assert all(r.status is not None and not r.stale for r in results)
    assert results[0].error is None and results[0].cache_error is not None
    assert "test-secret-123" not in results[0].cache_error.message
    assert read_eligible(tmp_path, "main", "hsr", uid=2, now=NOW) is not None


@pytest.mark.asyncio
async def test_success_writes_before_slow_game_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hoyo_cli.services import status

    data = snapshots()
    gateway = AsyncMock()
    persisted = asyncio.Event()

    def write(root: Path, entry: CacheEntry) -> None:
        write_cache(root, entry)
        if entry.game == "genshin":
            persisted.set()

    monkeypatch.setattr(status, "write_cache", write)

    async def fetch(game: GameId, uid: int) -> GameStatus:
        if game == "hsr":
            await persisted.wait()
            assert (
                read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) is not None
            )
        return data[game]

    gateway.fetch_status.side_effect = fetch
    await asyncio.wait_for(
        fetch_all_status(
            gateway,
            {"genshin": 1, "hsr": 2},
            account="main",
            cache_root=tmp_path,
            clock=lambda: NOW,
        ),
        timeout=1,
    )


@pytest.mark.asyncio
async def test_cancelled_request_does_not_use_or_replace_existing_cache(
    tmp_path: Path,
) -> None:
    old = CacheEntry(1, "main", "genshin", NOW, snapshots()["genshin"], uid=1)
    write_cache(tmp_path, old)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    gateway = AsyncMock()
    gateway.fetch_status.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await fetch_all_status(
            gateway,
            {"genshin": 1},
            account="main",
            cache_root=tmp_path,
            clock=lambda: NOW,
        )
    assert all(p.read_bytes() == content for p, content in before.items())


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True])
async def test_changed_uid_never_uses_another_character_snapshot(
    tmp_path: Path, fresh: bool
) -> None:
    from hoyo_cli.models.genshin import GenshinStatus

    data = snapshots()
    original_status = data["genshin"]
    assert isinstance(original_status, GenshinStatus)
    previous_status = replace(
        original_status, resin=replace(original_status.resin, current=12)
    )
    previous = CacheEntry(1, "main", "genshin", NOW, previous_status, uid=1)
    write_cache(tmp_path, previous)
    write_cache(tmp_path, replace(previous, account="alt", uid=2))
    write_cache(tmp_path, CacheEntry(1, "main", "hsr", NOW, data["hsr"], uid=2))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    error = NetworkError("HoYoLAB request timed out.")
    gateway = AsyncMock()
    if fresh:
        gateway.fetch_status.return_value = original_status
    else:
        gateway.fetch_status.side_effect = error
    results = await fetch_all_status(
        gateway, {"genshin": 2}, account="main", cache_root=tmp_path, clock=lambda: NOW
    )
    gateway.fetch_status.assert_awaited_once_with("genshin", 2)
    assert not results[0].stale
    assert set(tmp_path.rglob("*.json")) == set(before)
    if fresh:
        assert results[0].status == original_status and results[0].error is None
        cached = read_eligible(tmp_path, "main", "genshin", uid=2, now=NOW)
        assert cached is not None and cached.status == original_status
        assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) is None
        assert len([p for p, data in before.items() if p.read_bytes() != data]) == 1
    else:
        assert results[0].status is None and results[0].error is error
        assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) == previous
        assert all(p.read_bytes() == data for p, data in before.items())
    assert read_eligible(tmp_path, "alt", "genshin", uid=2, now=NOW) == replace(
        previous, account="alt", uid=2
    )
    assert read_eligible(tmp_path, "main", "hsr", uid=2, now=NOW) == CacheEntry(
        1, "main", "hsr", NOW, data["hsr"], uid=2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_age, elapsed, eligible",
    [
        (timedelta(hours=3), timedelta(microseconds=1), False),
        (timedelta(hours=3, seconds=-1), timedelta(seconds=1), True),
        (timedelta(hours=2, minutes=59), timedelta(seconds=30), True),
    ],
)
async def test_mixed_results_evaluate_age_when_failure_finishes(
    tmp_path: Path, initial_age: timedelta, elapsed: timedelta, eligible: bool
) -> None:
    data = snapshots()
    old = CacheEntry(1, "main", "hsr", NOW - initial_age, data["hsr"], uid=2)
    write_cache(tmp_path, old)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    current = NOW
    error = NetworkError("HoYoLAB request timed out.")
    gateway = AsyncMock()

    async def fetch(game: GameId, uid: int) -> GameStatus:
        nonlocal current
        if game == "hsr":
            await asyncio.sleep(0)
            current += elapsed
            raise error
        return data[game]

    gateway.fetch_status.side_effect = fetch
    results = await fetch_all_status(
        gateway, UIDS, account="main", cache_root=tmp_path, clock=lambda: current
    )
    assert [r.game for r in results] == ["genshin", "hsr", "zzz"]
    assert results[1].stale == eligible and results[1].error is error
    assert results[1].status == (old.status if eligible else None)
    assert results[1].fetched_at == (old.fetched_at if eligible else None)
    assert all(p.read_bytes() == content for p, content in before.items())
    for result in (results[0], results[2]):
        assert not result.stale and result.status == data[result.game]
        cached = read_eligible(
            tmp_path, "main", result.game, uid=UIDS[result.game], now=current
        )
        assert cached is not None and cached.status == result.status
    gateway.claim_check_in.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_timestamp_is_captured_after_awaited_completion(
    tmp_path: Path,
) -> None:
    current = NOW
    snapshot = snapshots()["genshin"]
    gateway = AsyncMock()

    async def fetch(game: GameId, uid: int) -> GameStatus:
        nonlocal current
        await asyncio.sleep(0)
        current += timedelta(seconds=20)
        return snapshot

    gateway.fetch_status.side_effect = fetch
    (result,) = await fetch_all_status(
        gateway,
        {"genshin": 2},
        account="main",
        cache_root=tmp_path,
        clock=lambda: current,
    )
    assert result.status == snapshot and not result.stale
    assert result.fetched_at == NOW + timedelta(seconds=20)
    cached = read_eligible(tmp_path, "main", "genshin", uid=2, now=current)
    assert cached is not None and cached.fetched_at == result.fetched_at
