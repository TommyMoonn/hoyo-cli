"""Map verified ZZZ notes without inferring amounts or exposing unknown fields."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import genshin

from hoyo_cli.errors import UnsupportedDataError
from hoyo_cli.hoyolab.adapters.notes import (
    boolean,
    integer,
    mapping,
    optional,
    resource,
    seconds,
    state,
)
from hoyo_cli.models.common import CountStatus, TimedActivity, require_aware
from hoyo_cli.models.zzz import TempleStatus, ZZZStatus


def _deadline(value: object, now: datetime) -> datetime | None:
    remaining = seconds(value)
    return now + timedelta(seconds=remaining) if remaining > 0 else None


def _count(value: object, current: str, maximum: str) -> CountStatus:
    data = mapping(value)
    return CountStatus(integer(data[current]), integer(data[maximum]))


@dataclass(frozen=True)
class _Bounties:
    progress: CountStatus | None
    availability: Literal["Available", "Locked"]
    reset_at: datetime | None


def _bounties(value: object, now: datetime) -> _Bounties:
    data = mapping(value)
    if data.get("unlock") is not None and not boolean(data["unlock"]):
        return _Bounties(None, "Locked", None)
    return _Bounties(
        _count(data, "num", "total"), "Available", _deadline(data["refresh_time"], now)
    )


def _member(value: object, now: datetime) -> TimedActivity:
    data = mapping(value)
    label = state(
        data["member_card_state"],
        {"MemberCardStateACK": "Active", "MemberCardStateNo": "Inactive"},
    )
    return TimedActivity(
        "Member Card",
        label,
        _deadline(data["exp_time"], now) if label == "Active" else None,
    )


def _temple(value: object, now: datetime) -> TempleStatus | Literal["Locked"]:
    data = mapping(value)
    level = integer(data["level"])
    # Level zero has no management progress; present this as a locked subsystem.
    if level == 0:
        return "Locked"
    bench = TimedActivity(
        "Temple Bench",
        state(
            data["bench_state"],
            {
                "BenchStateCanProduce": "Can produce",
                "BenchStateProducing": "Producing",
            },
        ),
    )
    shelves = TimedActivity(
        "Temple Shelves",
        state(
            data["shelve_state"],
            {
                "ShelveStateCanSell": "Can stock",
                "ShelveStateSelling": "Selling",
                "ShelveStateSoldOut": "Sold out",
            },
        ),
    )
    expedition = TimedActivity(
        "Temple Expedition",
        state(
            data["expedition_state"],
            {
                "ExpeditionStateInCanSend": "Can dispatch",
                "ExpeditionStateInProgress": "Adventuring",
                "ExpeditionStateEnd": "Expedition complete",
            },
        ),
    )
    auto = optional(data.get("auto_work"), lambda value: _auto_work(value, now))
    return TempleStatus(
        level,
        _count(data, "current_currency", "weekly_currency_max"),
        _deadline(data["currency_next_refresh_ts"], now),
        bench,
        shelves,
        expedition,
        auto,
    )


def _auto_work(value: object, now: datetime) -> TimedActivity:
    data = mapping(value)
    running = boolean(data["is_auto_work_running"])
    ended = boolean(data["auto_work_ended"])
    label = "Finished" if ended else "Running" if running else "Inactive"
    return TimedActivity(
        "Temple Auto-work",
        label,
        _deadline(data["left_ts"], now) if label == "Running" else None,
    )


def normalize_zzz(upstream: object, *, now: datetime | None = None) -> ZZZStatus:
    captured = now if now is not None else datetime.now(UTC)
    require_aware(captured)
    try:
        data = mapping(
            upstream.model_dump(by_alias=True)
            if isinstance(upstream, genshin.models.ZZZNotes)
            else upstream
        )
        energy = mapping(data["energy"])
        battery = resource(
            {**energy, **mapping(energy.get("progress", {}))},
            "current",
            "max",
            "restore",
            captured,
        )
        hollow: Mapping[str, object] = optional(data.get("hollow_zero"), mapping) or {}
        bounty = optional(
            hollow.get("bounty_commission", data.get("bounty_commission")),
            lambda value: _bounties(value, captured),
        )
        points = optional(
            hollow.get("survey_points", data.get("survey_points")),
            lambda value: _count(value, "num", "total"),
        )
        weekly = optional(
            data.get("weekly_task"),
            lambda value: _count(value, "cur_point", "max_point"),
        )
        weekly_reset = optional(
            data.get("weekly_task"),
            lambda value: _deadline(mapping(value)["refresh_time"], captured),
        )
        temple = optional(
            data.get("temple_running"), lambda value: _temple(value, captured)
        )
        video = data.get("video_store_state")
        if video is None:
            video = (optional(data.get("vhs_sale"), mapping) or {}).get("sale_state")
        card = data.get("card_sign")
        # A model dump may carry the scratch-card boolean under this alias.
        scratch = (
            "Done"
            if card is True
            else "Not done"
            if card is False
            else state(
                card,
                {
                    "CardSignDone": "Done",
                    "CardSignNo": "Not done",
                    "CardSignNotDone": "Not done",
                },
            )
        )
        return ZZZStatus(
            battery=battery,
            engagement=_count(data["vitality"], "current", "max"),
            scratch_card=scratch,
            video_store=state(
                video,
                {
                    "SaleStateDone": "Revenue available",
                    "SaleStateNo": "Waiting to open",
                    "SaleStateDoing": "Currently open",
                },
            ),
            hollow_zero_bounties=bounty.progress if bounty else None,
            hollow_zero_points=points,
            weekly_tasks=weekly,
            member_card=optional(
                data.get("member_card"), lambda value: _member(value, captured)
            )
            or TimedActivity("Member Card", "Unavailable"),
            temple=temple
            if isinstance(temple, TempleStatus)
            else "Locked"
            if temple == "Locked"
            else "Unavailable",
            bounties_state=bounty.availability if bounty else "Unavailable",
            bounties_reset_at=bounty.reset_at if bounty else None,
            weekly_reset_at=weekly_reset,
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        raise UnsupportedDataError(
            "HoYoLAB returned unsupported ZZZ real-time notes."
        ) from None
