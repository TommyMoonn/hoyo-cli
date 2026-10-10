"""Star Rail snapshot contract; upstream mapping is Phase 5 work."""

from dataclasses import dataclass, field
from typing import Literal

from hoyo_cli.models.common import CountStatus, ResourceStatus, TimedActivity


@dataclass(frozen=True)
class HSRStatus:
    power: ResourceStatus
    reserved_power: CountStatus
    daily_training: CountStatus
    assignments: tuple[TimedActivity, ...]
    max_assignments: int
    simulated_universe: CountStatus
    divergent_universe: CountStatus | Literal["Unavailable", "Locked"]
    echo_of_war: CountStatus
    game: Literal["hsr"] = field(default="hsr", init=False)
