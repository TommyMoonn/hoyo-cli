"""Star Rail real-time notes from genshin 1.7.31, normalized at capture time."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import genshin

from hoyo_cli.errors import UnsupportedDataError
from hoyo_cli.hoyolab.adapters.notes import (
    boolean,
    integer,
    mapping,
    optional,
    resource,
    safe_text,
    seconds,
)
from hoyo_cli.models.common import CountStatus, TimedActivity, require_aware
from hoyo_cli.models.hsr import HSRStatus

# The pinned notes omit a reserve maximum. HoYoWiki documents this game limit:
# https://wiki.hoyolab.com/pc/hsr/entry/179?lang=en-us
RESERVED_POWER_MAXIMUM = 2400


def _divergent(value: object) -> CountStatus | Literal["Locked"]:
    data = mapping(value)
    if not boolean(data["rogue_tourn_weekly_unlocked"]):
        return "Locked"
    return CountStatus(
        integer(data["rogue_tourn_weekly_cur"]), integer(data["rogue_tourn_weekly_max"])
    )


def normalize_hsr(upstream: object, *, now: datetime | None = None) -> HSRStatus:
    captured = now if now is not None else datetime.now(UTC)
    require_aware(captured)
    try:
        data = mapping(
            upstream.model_dump(by_alias=True)
            if isinstance(upstream, genshin.models.StarRailNote)
            else upstream
        )
        raw_assignments = data["expeditions"]
        if not isinstance(raw_assignments, Sequence) or isinstance(
            raw_assignments, (str, bytes)
        ):
            raise ValueError
        assignments: list[TimedActivity] = []
        for index, value in enumerate(cast(Sequence[object], raw_assignments), 1):
            assignment = mapping(value)
            remaining = seconds(assignment["remaining_time"])
            state = assignment["status"]
            if state not in ("Ongoing", "Finished"):
                raise ValueError
            finished = state == "Finished" or remaining == 0
            name = assignment.get("name")
            label = safe_text(name) if isinstance(name, str) else ""
            assignments.append(
                TimedActivity(
                    label or f"Assignment {index}",
                    "Finished" if finished else "Ongoing",
                    None if finished else captured + timedelta(seconds=remaining),
                )
            )
        accepted = integer(data["accepted_epedition_num"])
        maximum = integer(data["total_expedition_num"])
        if accepted > maximum or len(assignments) > accepted:
            raise ValueError
        divergent = optional(data, _divergent)
        return HSRStatus(
            power=resource(
                data, "current_stamina", "max_stamina", "stamina_recover_time", captured
            ),
            reserved_power=CountStatus(
                integer(data["current_reserve_stamina"]), RESERVED_POWER_MAXIMUM
            ),
            daily_training=CountStatus(
                integer(data["current_train_score"]), integer(data["max_train_score"])
            ),
            assignments=tuple(assignments),
            max_assignments=maximum,
            simulated_universe=CountStatus(
                integer(data["current_rogue_score"]), integer(data["max_rogue_score"])
            ),
            divergent_universe=divergent if divergent is not None else "Unavailable",
            echo_of_war=CountStatus(
                integer(data["weekly_cocoon_cnt"]), integer(data["weekly_cocoon_limit"])
            ),
            accepted_assignments=accepted,
            reserve_full=boolean(data["is_reserve_stamina_full"]),
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        raise UnsupportedDataError(
            "HoYoLAB returned unsupported Star Rail real-time notes."
        ) from None
