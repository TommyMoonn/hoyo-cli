"""Version 2 profile cache codec with explicit types at the JSON boundary."""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from math import isfinite
from typing import Literal, TypeVar, cast

from hoyo_cli.models.common import (
    GAME_IDS,
    CountStatus,
    GameId,
    GameStatus,
    ResourceStatus,
    TimedActivity,
    require_aware,
)
from hoyo_cli.models.genshin import (
    DailyTaskStatus,
    EncounterPointStatus,
    GenshinStatus,
    QuestStatus,
)
from hoyo_cli.models.hsr import HSRStatus
from hoyo_cli.models.zzz import TempleStatus, ZZZStatus

CACHE_VERSION = 2
T = TypeVar("T")


def require_uid(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("A positive integer cache UID is required.")
    return value


@dataclass(frozen=True)
class CacheEntry:
    cache_version: int
    profile: str
    game: GameId
    fetched_at: datetime
    status: GameStatus
    uid: int

    def __post_init__(self) -> None:
        if type(self.cache_version) is not int or self.cache_version != CACHE_VERSION:
            raise ValueError("Unsupported cache version.")
        if not self.profile:
            raise ValueError("A cache profile is required.")
        if self.game not in GAME_IDS or self.status.game != self.game:
            raise ValueError("Cache game does not match its snapshot.")
        require_uid(self.uid)
        require_aware(self.fetched_at)


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("A cache object is required.")
    data = cast(dict[object, object], value)
    if set(data) != keys:
        raise ValueError("Invalid cache object fields.")
    # Exact string field names have been checked before indexing this JSON object.
    return cast(dict[str, object], value)


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("A non-negative cache integer is required.")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("A cache boolean is required.")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Cache text is required.")
    return value


def _timestamp(value: object) -> datetime:
    timestamp = datetime.fromisoformat(_text(value))
    require_aware(timestamp)
    return timestamp


def _optional(value: object, parse: Callable[[object], T]) -> T | None:
    return None if value is None else parse(value)


def _sequence(value: object, parse: Callable[[object], T]) -> tuple[T, ...]:
    if not isinstance(value, list):
        raise ValueError("A cache array is required.")
    return tuple(parse(item) for item in cast(list[object], value))


def _count(value: object) -> CountStatus:
    data = _object(value, {field.name for field in fields(CountStatus)})
    return CountStatus(_integer(data["current"]), _integer(data["maximum"]))


def _resource(value: object) -> ResourceStatus:
    data = _object(value, {field.name for field in fields(ResourceStatus)})
    return ResourceStatus(
        _integer(data["current"]),
        _integer(data["maximum"]),
        _optional(data["full_at"], _timestamp),
    )


def _activity(value: object) -> TimedActivity:
    data = _object(value, {field.name for field in fields(TimedActivity)})
    return TimedActivity(
        _text(data["label"]),
        _text(data["state"]),
        _optional(data["completes_at"], _timestamp),
    )


def _points(value: object) -> float:
    if type(value) not in (float, int):
        raise ValueError("Encounter points must be numeric.")
    number = float(cast(float | int, value))
    if not isfinite(number) or number < 0:
        raise ValueError("Encounter points must be finite and non-negative.")
    return number


def _encounter(value: object) -> EncounterPointStatus:
    data = _object(value, {field.name for field in fields(EncounterPointStatus)})
    return EncounterPointStatus(_integer(data["progress"]), _text(data["state"]))


def _daily_tasks(value: object) -> DailyTaskStatus:
    data = _object(value, {field.name for field in fields(DailyTaskStatus)})
    return DailyTaskStatus(
        _count(data["tasks"]),
        _sequence(data["reward_states"], _text),
        _sequence(data["encounter_points"], _encounter),
        _optional(data["stored_encounter_points"], _points),
    )


def _quests(value: object) -> QuestStatus:
    data = _object(value, {field.name for field in fields(QuestStatus)})
    state = data["state"]
    if state not in ("Complete", "In progress", "Locked"):
        raise ValueError("Invalid quest state.")
    return QuestStatus(
        state,
        _sequence(data["chapters"], _text),
        _boolean(data["interchapters_complete"]),
    )


def _genshin(data: dict[str, object]) -> GenshinStatus:
    return GenshinStatus(
        resin=_resource(data["resin"]),
        realm_currency=_optional(data["realm_currency"], _resource),
        commissions=_count(data["commissions"]),
        commission_reward_claimed=_boolean(data["commission_reward_claimed"]),
        expeditions=_sequence(data["expeditions"], _activity),
        max_expeditions=_integer(data["max_expeditions"]),
        transformer=_activity(data["transformer"]),
        weekly_discounts=_count(data["weekly_discounts"]),
        daily_tasks=_optional(data["daily_tasks"], _daily_tasks),
        archon_quests=_optional(data["archon_quests"], _quests),
    )


def _unavailable(value: object) -> Literal["Unavailable", "Locked"]:
    if value not in ("Unavailable", "Locked"):
        raise ValueError("Invalid unavailable system state.")
    return value


def _hsr(data: dict[str, object]) -> HSRStatus:
    divergent = data["divergent_universe"]
    return HSRStatus(
        power=_resource(data["power"]),
        reserved_power=_count(data["reserved_power"]),
        daily_training=_count(data["daily_training"]),
        assignments=_sequence(data["assignments"], _activity),
        max_assignments=_integer(data["max_assignments"]),
        simulated_universe=_count(data["simulated_universe"]),
        divergent_universe=(
            _unavailable(divergent) if isinstance(divergent, str) else _count(divergent)
        ),
        echo_of_war=_count(data["echo_of_war"]),
        accepted_assignments=_integer(data["accepted_assignments"]),
        reserve_full=_boolean(data["reserve_full"]),
    )


def _temple(value: object) -> TempleStatus:
    data = _object(value, {field.name for field in fields(TempleStatus)})
    return TempleStatus(
        level=_integer(data["level"]),
        currency=_count(data["currency"]),
        reset_at=_optional(data["reset_at"], _timestamp),
        bench=_activity(data["bench"]),
        shelves=_activity(data["shelves"]),
        expedition=_activity(data["expedition"]),
        auto_work=_optional(data["auto_work"], _activity),
    )


def _zzz(data: dict[str, object]) -> ZZZStatus:
    temple = data["temple"]
    state = data["bounties_state"]
    if state not in ("Available", "Unavailable", "Locked"):
        raise ValueError("Invalid bounties state.")
    return ZZZStatus(
        battery=_resource(data["battery"]),
        engagement=_count(data["engagement"]),
        scratch_card=_text(data["scratch_card"]),
        video_store=_text(data["video_store"]),
        hollow_zero_bounties=_optional(data["hollow_zero_bounties"], _count),
        hollow_zero_points=_optional(data["hollow_zero_points"], _count),
        weekly_tasks=_optional(data["weekly_tasks"], _count),
        member_card=_activity(data["member_card"]),
        temple=_unavailable(temple) if isinstance(temple, str) else _temple(temple),
        bounties_state=state,
        bounties_reset_at=_optional(data["bounties_reset_at"], _timestamp),
        weekly_reset_at=_optional(data["weekly_reset_at"], _timestamp),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate cache field.")
        result[key] = value
    return result


def decode_entry(content: str) -> CacheEntry:
    raw: object = json.loads(content, object_pairs_hook=_unique_object)
    data = _object(raw, {field.name for field in fields(CacheEntry)})
    game = data["game"]
    if game not in GAME_IDS:
        raise ValueError("Unknown cache game.")
    model = {"genshin": GenshinStatus, "hsr": HSRStatus, "zzz": ZZZStatus}[game]
    payload = _object(data["status"], {field.name for field in fields(model)})
    if payload["game"] != game:
        raise ValueError("Cache payload game mismatch.")
    parse = {"genshin": _genshin, "hsr": _hsr, "zzz": _zzz}[game]
    return CacheEntry(
        _integer(data["cache_version"]),
        _text(data["profile"]),
        game,
        _timestamp(data["fetched_at"]),
        parse(payload),
        uid=require_uid(data["uid"]),
    )


def _encode_timestamp(value: object) -> str:
    if not isinstance(value, datetime):
        raise TypeError("Unsupported cache value.")
    require_aware(value)
    return value.isoformat()


def encode_entry(entry: CacheEntry) -> str:
    content = json.dumps(
        asdict(entry), default=_encode_timestamp, allow_nan=False, ensure_ascii=True
    )
    # Dataclasses alone do not enforce every annotated field at runtime. Validate
    # the serialized candidate through the same codec before any file is written.
    decode_entry(content)
    return content + "\n"
