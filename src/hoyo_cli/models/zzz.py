"""Immutable Zenless Zone Zero recurring notes and optional management details."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from hoyo_cli.models.common import (
    CountStatus,
    ResourceStatus,
    TimedActivity,
    require_aware,
)


@dataclass(frozen=True)
class TempleStatus:
    level: int
    currency: CountStatus
    reset_at: datetime | None
    bench: TimedActivity
    shelves: TimedActivity
    expedition: TimedActivity
    auto_work: TimedActivity | None = None

    def __post_init__(self) -> None:
        require_aware(self.reset_at)


@dataclass(frozen=True)
class ZZZStatus:
    battery: ResourceStatus
    engagement: CountStatus
    scratch_card: str
    video_store: str
    hollow_zero_bounties: CountStatus | None
    hollow_zero_points: CountStatus | None
    weekly_tasks: CountStatus | None
    member_card: TimedActivity
    temple: TempleStatus | Literal["Unavailable", "Locked"]
    bounties_state: Literal["Available", "Unavailable", "Locked"] = "Unavailable"
    bounties_reset_at: datetime | None = None
    weekly_reset_at: datetime | None = None
    game: Literal["zzz"] = field(default="zzz", init=False)

    def __post_init__(self) -> None:
        for timestamp in (
            self.bounties_reset_at,
            self.weekly_reset_at,
        ):
            require_aware(timestamp)
