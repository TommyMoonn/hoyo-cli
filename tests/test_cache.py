"""Versioned normalized snapshots, strict eligibility, and atomic isolation."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from hoyo_cli.cache.models import CacheEntry
from hoyo_cli.cache.repository import read_eligible, write_cache
from hoyo_cli.models.common import GAME_IDS, GameId, GameStatus

NOW = datetime(2026, 10, 10, 11, 31, tzinfo=UTC)


@pytest.fixture
def domain_snapshots() -> dict[GameId, GameStatus]:
    from hoyo_cli.hoyolab.adapters.genshin import normalize_genshin
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


def entry(snapshots: dict[GameId, GameStatus], game: GameId = "genshin") -> CacheEntry:
    return CacheEntry(2, "main", game, NOW, snapshots[game], uid=1)


@pytest.mark.parametrize(
    "age, eligible",
    [
        (timedelta(hours=2, minutes=59), True),
        (timedelta(hours=3), True),
        (timedelta(hours=3, microseconds=1), False),
        (timedelta(microseconds=-1), False),
    ],
)
def test_cache_age_boundary(
    tmp_path: Path,
    domain_snapshots: dict[GameId, GameStatus],
    age: timedelta,
    eligible: bool,
) -> None:
    snapshot = replace(entry(domain_snapshots), fetched_at=NOW - age)
    write_cache(tmp_path, snapshot)
    assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) == (
        snapshot if eligible else None
    )


@pytest.mark.parametrize("game", GAME_IDS)
def test_roundtrip_each_domain_model(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus], game: GameId
) -> None:
    snapshot = entry(domain_snapshots, game)
    write_cache(tmp_path, snapshot)
    assert read_eligible(tmp_path, "main", game, uid=1, now=NOW) == snapshot
    content = next(tmp_path.rglob("*.json")).read_text()
    assert "example.invalid" not in content and "ltoken" not in content


@pytest.mark.parametrize(
    "location, value",
    [
        (("cache_version",), 1),
        (("cache_version",), True),
        (("profile",), "alt"),
        (("game",), "hsr"),
        (("fetched_at",), "2026-10-10T11:31:00"),
        (("fetched_at",), "invalid"),
        (("status", "game"), "hsr"),
        (("status",), {}),
        (("status", "resin", "current"), True),
        (("status", "resin", "current"), -1),
        (("status", "resin", "maximum"), 0),
        (("status", "resin", "full_at"), "2026-10-10T12:00:00"),
        (("status", "commission_reward_claimed"), "false"),
        (("status", "max_expeditions"), -1),
        (("status", "expeditions"), ["not an activity"]),
        (("status", "transformer", "state"), 4),
        (("status", "daily_tasks", "stored_encounter_points"), float("nan")),
        (("status", "archon_quests", "state"), "invented"),
        (("status", "archon_quests", "interchapters_complete"), 1),
        (("status", "ltoken"), "test-secret"),
    ],
)
def test_invalid_envelope_or_nested_payload_is_ignored(
    tmp_path: Path,
    domain_snapshots: dict[GameId, GameStatus],
    location: tuple[str, ...],
    value: object,
) -> None:
    write_cache(tmp_path, entry(domain_snapshots))
    path = next(tmp_path.rglob("*.json"))
    payload = json.loads(path.read_text())
    target = payload
    for key in location[:-1]:
        target = target[key]
    target[location[-1]] = value
    path.write_text(json.dumps(payload))
    assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) is None


@pytest.mark.parametrize("content", ["{broken", "[]", "null", "\xff"])
def test_corrupt_cache_is_ignored(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus], content: str
) -> None:
    write_cache(tmp_path, entry(domain_snapshots))
    next(tmp_path.rglob("*.json")).write_bytes(content.encode("latin-1"))
    assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) is None


def test_missing_and_unreadable_cache_are_ignored(tmp_path: Path) -> None:
    assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) is None
    with patch.object(Path, "read_text", side_effect=PermissionError):
        assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) is None


def test_write_only_updates_target_game(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus]
) -> None:
    for game in GAME_IDS:
        write_cache(tmp_path, entry(domain_snapshots, game))
    write_cache(tmp_path, replace(entry(domain_snapshots), profile="alt"))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    updated = replace(
        entry(domain_snapshots, "hsr"), fetched_at=NOW - timedelta(hours=1)
    )
    write_cache(tmp_path, updated)
    changed = [p.name for p, content in before.items() if p.read_bytes() != content]
    assert changed == ["hsr-1.json"]
    assert read_eligible(tmp_path, "main", "hsr", uid=1, now=NOW) == updated


def test_failed_atomic_replace_preserves_previous_bytes_and_cleans_temp(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus]
) -> None:
    snapshot = entry(domain_snapshots)
    write_cache(tmp_path, snapshot)
    path = next(tmp_path.rglob("*.json"))
    before = path.read_bytes()
    with patch("hoyo_cli.cache.repository.os.replace", side_effect=PermissionError):
        with pytest.raises(OSError):
            write_cache(
                tmp_path, replace(snapshot, fetched_at=NOW - timedelta(hours=1))
            )
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("profile", ["../escape", "MAIN", "main", "CON", "a/b", "a\\b"])
def test_account_paths_are_safe_and_exact(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus], profile: str
) -> None:
    snapshot = replace(entry(domain_snapshots), profile=profile)
    write_cache(tmp_path, snapshot)
    assert read_eligible(tmp_path, profile, "genshin", uid=1, now=NOW) == snapshot
    assert read_eligible(tmp_path, profile + ".", "genshin", uid=1, now=NOW) is None
    assert all(p.is_relative_to(tmp_path) for p in tmp_path.rglob("*.json"))


def test_case_distinct_accounts_do_not_overwrite(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus]
) -> None:
    lower = entry(domain_snapshots)
    upper = replace(lower, profile="MAIN", fetched_at=NOW - timedelta(hours=1))
    write_cache(tmp_path, lower)
    write_cache(tmp_path, upper)
    assert len(list(tmp_path.rglob("*.json"))) == 2
    assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) == lower
    assert read_eligible(tmp_path, "MAIN", "genshin", uid=1, now=NOW) == upper


@pytest.mark.parametrize("game", ["hsr", "zzz"])
@pytest.mark.parametrize("state", ["Locked", "Unavailable"])
def test_optional_system_states_roundtrip(
    tmp_path: Path,
    domain_snapshots: dict[GameId, GameStatus],
    game: GameId,
    state: str,
) -> None:
    write_cache(tmp_path, entry(domain_snapshots, game))
    path = next(tmp_path.rglob("*.json"))
    payload = json.loads(path.read_text())
    payload["status"]["divergent_universe" if game == "hsr" else "temple"] = state
    path.write_text(json.dumps(payload))
    loaded = read_eligible(tmp_path, "main", game, uid=1, now=NOW)
    assert loaded is not None
    write_cache(tmp_path, loaded)
    assert read_eligible(tmp_path, "main", game, uid=1, now=NOW) == loaded


@pytest.mark.parametrize(
    "game, location, value",
    [
        ("hsr", ("reserve_full",), "false"),
        ("hsr", ("accepted_assignments",), -1),
        ("hsr", ("divergent_universe",), "invented"),
        ("zzz", ("bounties_state",), "invented"),
        ("zzz", ("weekly_reset_at",), "2026-10-12T00:00:00"),
        ("zzz", ("temple", "level"), True),
        ("zzz", ("temple", "bench", "completes_at"), "invalid"),
    ],
)
def test_invalid_hsr_and_zzz_payloads_are_ignored(
    tmp_path: Path,
    domain_snapshots: dict[GameId, GameStatus],
    game: GameId,
    location: tuple[str, ...],
    value: object,
) -> None:
    write_cache(tmp_path, entry(domain_snapshots, game))
    path = next(tmp_path.rglob("*.json"))
    payload = json.loads(path.read_text())
    target = payload["status"]
    for key in location[:-1]:
        target = target[key]
    target[location[-1]] = value
    path.write_text(json.dumps(payload))
    assert read_eligible(tmp_path, "main", game, uid=1, now=NOW) is None


def test_duplicate_fields_are_rejected(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus]
) -> None:
    write_cache(tmp_path, entry(domain_snapshots))
    path = next(tmp_path.rglob("*.json"))
    content = path.read_text().replace(
        '"cache_version": 2', '"cache_version": 2, "cache_version": 2'
    )
    path.write_text(content)
    assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) is None


def test_equivalent_timezone_instants_have_same_eligibility(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus]
) -> None:
    from datetime import timezone

    local_zone = timezone(timedelta(hours=7))
    snapshot = replace(
        entry(domain_snapshots),
        fetched_at=(NOW - timedelta(hours=3)).astimezone(local_zone),
    )
    write_cache(tmp_path, snapshot)
    assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) == snapshot


@pytest.mark.parametrize(
    "value", [None, True, False, "1", 1.0, 0, -1, [], {}, float("nan")]
)
def test_cache_with_invalid_uid_is_ineligible_for_any_character(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus], value: object
) -> None:
    write_cache(tmp_path, entry(domain_snapshots))
    path = next(tmp_path.rglob("*.json"))
    payload = json.loads(path.read_text())
    payload["uid"] = value
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    for uid in (1, 2):
        assert read_eligible(tmp_path, "main", "genshin", uid=uid, now=NOW) is None
    assert path.read_bytes() == before


def test_legacy_cache_without_uid_is_not_attributed_to_any_character(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus]
) -> None:
    write_cache(tmp_path, entry(domain_snapshots))
    path = next(tmp_path.rglob("*.json"))
    payload = json.loads(path.read_text())
    del payload["uid"]
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    for uid in (1, 2):
        assert read_eligible(tmp_path, "main", "genshin", uid=uid, now=NOW) is None
    assert path.read_bytes() == before


def test_uid_change_replaces_only_its_account_game_snapshot(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus]
) -> None:
    original = entry(domain_snapshots)
    write_cache(tmp_path, original)
    target = next(tmp_path.rglob("*.json"))
    for game in ("hsr", "zzz"):
        write_cache(tmp_path, entry(domain_snapshots, game))
    write_cache(tmp_path, replace(original, profile="alt"))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    assert read_eligible(tmp_path, "main", "genshin", uid=2, now=NOW) is None
    assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) == original
    assert all(p.read_bytes() == data for p, data in before.items())

    changed = replace(original, uid=2)
    write_cache(tmp_path, changed)
    assert len(set(tmp_path.rglob("*.json")) - set(before)) == 1
    assert all(p.read_bytes() == data for p, data in before.items())
    assert target.read_bytes() == before[target]
    assert read_eligible(tmp_path, "main", "genshin", uid=1, now=NOW) == original
    assert read_eligible(tmp_path, "main", "genshin", uid=2, now=NOW) == changed


@pytest.mark.parametrize("uid", [0, -1, True])
def test_invalid_uid_is_rejected_before_writing(
    tmp_path: Path, domain_snapshots: dict[GameId, GameStatus], uid: int
) -> None:
    with pytest.raises(ValueError, match="positive integer cache UID"):
        write_cache(tmp_path, replace(entry(domain_snapshots), uid=uid))
    assert list(tmp_path.iterdir()) == []
