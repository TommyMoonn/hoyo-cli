"""Intentionally supported Genshin notes, captured without upstream objects."""

from dataclasses import dataclass, field
from typing import Literal

from hoyo_cli.models.common import CountStatus, ResourceStatus, TimedActivity


@dataclass(frozen=True)
class EncounterPointStatus:
    progress: int
    state: str


@dataclass(frozen=True)
class DailyTaskStatus:
    tasks: CountStatus
    reward_states: tuple[str, ...]
    encounter_points: tuple[EncounterPointStatus, ...]
    stored_encounter_points: float | None


@dataclass(frozen=True)
class QuestStatus:
    state: Literal["Complete", "In progress", "Locked"]
    chapters: tuple[str, ...]
    interchapters_complete: bool


@dataclass(frozen=True)
class GenshinStatus:
    resin: ResourceStatus
    realm_currency: ResourceStatus | None
    commissions: CountStatus
    commission_reward_claimed: bool
    expeditions: tuple[TimedActivity, ...]
    max_expeditions: int
    transformer: TimedActivity
    weekly_discounts: CountStatus
    daily_tasks: DailyTaskStatus | None = None
    archon_quests: QuestStatus | None = None
    game: Literal["genshin"] = field(default="genshin", init=False)
