"""Normalize the pinned client's raw notes; optional details remain optional."""

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TypeVar, cast

import genshin

from hoyo_cli.errors import UnsupportedDataError
from hoyo_cli.models.common import (
    CountStatus,
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

_Detail = TypeVar("_Detail", DailyTaskStatus, QuestStatus)


def _optional_detail(
    value: object, normalize: Callable[[object], _Detail | None]
) -> _Detail | None:
    try:
        return normalize(value)
    except (ValueError, TypeError, KeyError, OverflowError):
        # Optional upstream expansions must not hide usable recurring notes.
        return None


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError
    entries = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in entries):
        raise ValueError
    return cast(Mapping[str, object], entries)


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError
    return value


def _seconds(value: object) -> int:
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        seconds = int(value)
    else:
        seconds = _integer(value)
    if seconds < 0:
        raise ValueError
    return seconds


def _resource(
    data: Mapping[str, object], current: str, maximum: str, recovery: str, now: datetime
) -> ResourceStatus:
    amount, limit = _integer(data[current]), _integer(data[maximum])
    remaining = _seconds(data[recovery])
    # A countdown predicts a future deadline, not when an already-full resource filled.
    full_at = (
        now + timedelta(seconds=remaining) if amount < limit and remaining > 0 else None
    )
    return ResourceStatus(amount, limit, full_at)


def _transformer(data: Mapping[str, object], now: datetime) -> TimedActivity:
    if "remaining_transformer_recovery_time" in data:
        value = data["remaining_transformer_recovery_time"]
        if value is None:
            return TimedActivity("Transformer", "Locked")
        seconds = _seconds(value)
    elif data.get("transformer") is None:
        return TimedActivity("Transformer", "Unavailable")
    else:
        transformer = _mapping(data["transformer"])
        if not _boolean(transformer["obtained"]):
            return TimedActivity("Transformer", "Locked")
        recovery = _mapping(transformer["recovery_time"])
        days, hours, minutes, seconds = (
            _integer(recovery[name]) for name in ("Day", "Hour", "Minute", "Second")
        )
        if hours >= 24 or minutes >= 60 or seconds >= 60:
            raise ValueError
        seconds += days * 86400 + hours * 3600 + minutes * 60
    return TimedActivity(
        "Transformer",
        "Ready" if seconds == 0 else "Cooldown",
        now + timedelta(seconds=seconds) if seconds > 0 else None,
    )


def _daily_tasks(value: object) -> DailyTaskStatus | None:
    if value is None:
        return None
    tasks = genshin.models.DailyTasks.model_validate(value)
    rewards: dict[str, str] = {
        genshin.models.TaskRewardStatus.UNFINISHED: "Incomplete",
        genshin.models.TaskRewardStatus.FINISHED: "Ready",
        genshin.models.TaskRewardStatus.COLLECTED: "Claimed",
    }
    encounters: dict[str, str] = {
        genshin.models.AttendanceRewardStatus.AVAILABLE: "Ready",
        genshin.models.AttendanceRewardStatus.COLLECTED: "Claimed",
        genshin.models.AttendanceRewardStatus.FORBIDDEN: "Locked",
        genshin.models.AttendanceRewardStatus.UNAVAILABLE: "Incomplete",
    }
    if not math.isfinite(tasks.stored_attendance) or tasks.stored_attendance < 0:
        raise ValueError
    return DailyTaskStatus(
        CountStatus(tasks.completed_tasks, tasks.max_tasks),
        tuple(
            rewards.get(reward.status, "Unavailable") for reward in tasks.task_rewards
        ),
        tuple(
            EncounterPointStatus(
                _integer(reward.progress), encounters.get(reward.status, "Unavailable")
            )
            for reward in tasks.attendance_rewards
        )
        if tasks.attendance_visible
        else (),
        tasks.stored_attendance if tasks.attendance_visible else None,
    )


def _safe_text(value: str) -> str:
    return "".join(char for char in value if char.isprintable())


def _quests(value: object) -> QuestStatus | None:
    if value is None:
        return None
    quests = genshin.models.ArchonQuestProgress.model_validate(value)
    state = (
        "Locked"
        if not quests.archon_quest_unlocked
        else "Complete"
        if quests.mainlines_finished
        else "In progress"
    )
    return QuestStatus(
        state,
        tuple(
            _safe_text(f"{quest.chapter_num}: {quest.chapter_title}")
            for quest in quests.list
        ),
        quests.interchapters_finished,
    )


def normalize_genshin(
    upstream: object, *, now: datetime | None = None
) -> GenshinStatus:
    captured = now if now is not None else datetime.now(UTC)
    require_aware(captured)
    try:
        data = _mapping(
            upstream.model_dump(by_alias=True)
            if isinstance(upstream, genshin.models.Notes)
            else upstream
        )
        resin = _resource(
            data, "current_resin", "max_resin", "resin_recovery_time", captured
        )
        realm = (
            _resource(
                data,
                "current_home_coin",
                "max_home_coin",
                "home_coin_recovery_time",
                captured,
            )
            if _integer(data["max_home_coin"])
            else None
        )
        raw_expeditions = data["expeditions"]
        if not isinstance(raw_expeditions, Sequence) or isinstance(
            raw_expeditions, (str, bytes)
        ):
            raise ValueError
        expeditions: list[TimedActivity] = []
        for index, value in enumerate(cast(Sequence[object], raw_expeditions), 1):
            expedition = _mapping(value)
            seconds = _seconds(expedition["remained_time"])
            state = expedition["status"]
            if state not in ("Ongoing", "Finished"):
                raise ValueError
            finished = state == "Finished" or seconds == 0
            # Notes expose only avatar URLs, not reliable character names.
            # Finished notes establish a state, not a historical completion time.
            expeditions.append(
                TimedActivity(
                    f"Expedition {index}",
                    "Finished" if finished else "Ongoing",
                    None if finished else captured + timedelta(seconds=seconds),
                )
            )
        maximum = _integer(data["max_expedition_num"])
        if len(expeditions) > maximum:
            raise ValueError
        return GenshinStatus(
            resin=resin,
            realm_currency=realm,
            commissions=CountStatus(
                _integer(data["finished_task_num"]), _integer(data["total_task_num"])
            ),
            commission_reward_claimed=_boolean(data["is_extra_task_reward_received"]),
            expeditions=tuple(expeditions),
            max_expeditions=maximum,
            transformer=_transformer(data, captured),
            weekly_discounts=CountStatus(
                _integer(data["remain_resin_discount_num"]),
                _integer(data["resin_discount_num_limit"]),
            ),
            daily_tasks=_optional_detail(data.get("daily_task"), _daily_tasks),
            archon_quests=_optional_detail(data.get("archon_quest_progress"), _quests),
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        raise UnsupportedDataError(
            "HoYoLAB returned unsupported Genshin real-time notes."
        ) from None
