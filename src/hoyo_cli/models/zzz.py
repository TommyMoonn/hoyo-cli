"""Zenless Zone Zero snapshot contract; upstream mapping is Phase 5 work."""

from dataclasses import dataclass, field
from typing import Literal

from hoyo_cli.models.common import CountStatus, ResourceStatus, TimedActivity


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
    temple: tuple[TimedActivity, ...] | Literal["Unavailable", "Locked"]
    game: Literal["zzz"] = field(default="zzz", init=False)
