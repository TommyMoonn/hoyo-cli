"""Width-aware game views consume only normalized snapshots."""

from dataclasses import dataclass
from datetime import datetime

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.table import Table
from rich.text import Text

from hoyo_cli.models.common import (
    CountStatus,
    GameStatus,
    ResourceStatus,
    TimedActivity,
    require_aware,
)
from hoyo_cli.models.genshin import GenshinStatus
from hoyo_cli.models.hsr import HSRStatus
from hoyo_cli.models.zzz import TempleStatus, ZZZStatus
from hoyo_cli.render.styles import GAME_HEADINGS, STYLES
from hoyo_cli.render.widths import choose_layout
from hoyo_cli.timefmt import format_duration, format_full_at, format_local_timestamp


@dataclass(frozen=True)
class _Row:
    label: str
    primary: str
    secondary: str = ""


def _recovery(resource: ResourceStatus, now: datetime) -> str:
    if resource.current >= resource.maximum:
        return "Full"
    if resource.full_at is not None and resource.full_at <= now:
        return "Recovery time elapsed"
    return format_full_at(resource.full_at, now)


def _activity(activity: TimedActivity, now: datetime) -> str:
    if activity.state in ("Ready", "Finished", "Locked", "Unavailable"):
        return activity.state
    if activity.completes_at is None:
        return activity.state
    seconds = int((activity.completes_at - now).total_seconds())
    return format_duration(seconds) if seconds > 0 else "Completion time elapsed"


def _genshin_rows(
    status: GenshinStatus, detailed: bool, focused: bool, now: datetime
) -> list[_Row]:
    resin = status.resin
    rows = [_Row("Resin", f"{resin.current} / {resin.maximum}", _recovery(resin, now))]
    realm = status.realm_currency
    finished = sum(activity.state == "Finished" for activity in status.expeditions)
    rows.append(
        _Row("Realm Currency", "Unavailable")
        if realm is None
        else _Row(
            "Realm Currency",
            f"{realm.current} / {realm.maximum}",
            _recovery(realm, now),
        )
    )
    rows.extend(
        [
            _Row(
                "Commissions",
                f"{status.commissions.current} / {status.commissions.maximum}",
                "Reward claimed"
                if status.commission_reward_claimed
                else "Reward unclaimed",
            ),
            _Row(
                "Expeditions",
                f"{len(status.expeditions)} / {status.max_expeditions}",
                f"{finished} finished",
            ),
            _Row("Transformer", _activity(status.transformer, now)),
            _Row(
                "Weekly Discounts",
                f"{status.weekly_discounts.current} / "
                f"{status.weekly_discounts.maximum}",
                "remaining",
            ),
        ]
    )
    if focused or detailed:
        for activity in status.expeditions:
            rows.append(_Row(activity.label, _activity(activity, now)))
            if detailed and activity.completes_at is not None:
                rows.append(
                    _Row(
                        f"{activity.label} at",
                        format_local_timestamp(activity.completes_at, now),
                    )
                )
    if detailed:
        if resin.full_at is not None:
            rows.append(
                _Row("Resin full at", format_local_timestamp(resin.full_at, now))
            )
        if realm is not None and realm.full_at is not None:
            rows.append(
                _Row("Realm full at", format_local_timestamp(realm.full_at, now))
            )
        if status.transformer.completes_at is not None:
            rows.append(
                _Row(
                    "Transformer ready",
                    format_local_timestamp(status.transformer.completes_at, now),
                )
            )
        tasks = status.daily_tasks
        if tasks is not None:
            rows.append(
                _Row("Daily Tasks", f"{tasks.tasks.current} / {tasks.tasks.maximum}")
            )
            rows.extend(
                _Row(f"Task Reward {index}", state)
                for index, state in enumerate(tasks.reward_states, 1)
            )
            rows.extend(
                _Row(
                    f"Encounter Point {index}",
                    point.state,
                    f"Progress {point.progress}",
                )
                for index, point in enumerate(tasks.encounter_points, 1)
            )
            if tasks.stored_encounter_points is not None:
                rows.append(
                    _Row(
                        "Stored Encounter", f"{tasks.stored_encounter_points:g} points"
                    )
                )
        quests = status.archon_quests
        if quests is not None:
            rows.append(_Row("Archon Quests", quests.state))
            rows.extend(_Row("Quest", chapter) for chapter in quests.chapters)
            rows.append(
                _Row(
                    "Interchapter Quests",
                    "Complete" if quests.interchapters_complete else "Incomplete",
                )
            )
    return rows


def _count(value: CountStatus | None) -> str:
    return f"{value.current} / {value.maximum}" if value is not None else "Unavailable"


def _resource_row(label: str, value: ResourceStatus, now: datetime) -> _Row:
    return _Row(label, _count(value), _recovery(value, now))


def _activity_rows(
    activities: tuple[TimedActivity, ...], detailed: bool, now: datetime
) -> list[_Row]:
    rows: list[_Row] = []
    for activity in activities:
        # Preserve management state as well as its countdown.
        if activity.state == "Running" and activity.completes_at is not None:
            rows.append(_Row(activity.label, activity.state, _activity(activity, now)))
        else:
            rows.append(_Row(activity.label, _activity(activity, now)))
        if detailed and activity.completes_at is not None:
            rows.append(
                _Row(
                    f"{activity.label} at",
                    format_local_timestamp(activity.completes_at, now),
                )
            )
    return rows


def _hsr_rows(
    status: HSRStatus, detailed: bool, focused: bool, now: datetime
) -> list[_Row]:
    du = status.divergent_universe
    rows = [
        _resource_row("Trailblaze Power", status.power, now),
        _Row("Reserved Power", _count(status.reserved_power)),
        _Row("Daily Training", _count(status.daily_training)),
        _Row(
            "Assignments",
            f"{status.accepted_assignments} / {status.max_assignments}",
            f"{sum(a.state == 'Finished' for a in status.assignments)} finished",
        ),
        _Row("Simulated Universe", _count(status.simulated_universe)),
        _Row("Divergent Universe", _count(du) if isinstance(du, CountStatus) else du),
        _Row("Echo of War", _count(status.echo_of_war), "remaining"),
    ]
    if focused or detailed:
        rows.extend(_activity_rows(status.assignments, detailed, now))
    if detailed:
        if status.power.full_at is not None:
            rows.append(
                _Row("Power full at", format_local_timestamp(status.power.full_at, now))
            )
        rows.append(_Row("Reserve full", "Full" if status.reserve_full else "Not full"))
    return rows


def _zzz_rows(
    status: ZZZStatus, detailed: bool, focused: bool, now: datetime
) -> list[_Row]:
    bounty = (
        _count(status.hollow_zero_bounties)
        if status.bounties_state == "Available"
        else status.bounties_state
    )
    points = _count(status.hollow_zero_points)
    temple = status.temple
    if isinstance(temple, TempleStatus):
        summary = temple.expedition.state
        secondary = f"Bench {temple.bench.state.lower()}"
    else:
        summary, secondary = temple, ""
    rows = [
        _resource_row("Battery Charge", status.battery, now),
        _Row("Engagement", _count(status.engagement)),
        _Row("Scratch Card", status.scratch_card),
        _Row("Video Store", status.video_store),
        _Row("Hollow Zero", f"Bounties {bounty}", f"Investigation {points}"),
        _Row("Weekly Tasks", _count(status.weekly_tasks)),
        _Row("Member Card", status.member_card.state),
        _Row("Temple", summary, secondary),
    ]
    if focused or detailed:
        rows.extend(
            [_Row("Hollow Zero Bounties", bounty), _Row("Hollow Zero Points", points)]
        )
        if isinstance(temple, TempleStatus):
            activities = (temple.bench, temple.shelves, temple.expedition)
            if temple.auto_work is not None:
                activities += (temple.auto_work,)
            rows.extend(_activity_rows(activities, detailed, now))
    if detailed:
        for label, timestamp in (
            ("Battery full at", status.battery.full_at),
            ("Hollow Zero Reset", status.bounties_reset_at),
            ("Weekly Reset", status.weekly_reset_at),
            ("Member Card Expiry", status.member_card.completes_at),
            (
                "Temple Reset",
                temple.reset_at if isinstance(temple, TempleStatus) else None,
            ),
        ):
            if timestamp is not None:
                rows.append(_Row(label, format_local_timestamp(timestamp, now)))
        if isinstance(temple, TempleStatus):
            rows.append(_Row("Temple Level", str(temple.level)))
            rows.append(_Row("Temple Currency", _count(temple.currency)))
    return rows


def _rows(
    status: GameStatus, detailed: bool, focused: bool, now: datetime
) -> list[_Row]:
    if isinstance(status, GenshinStatus):
        return _genshin_rows(status, detailed, focused, now)
    if isinstance(status, HSRStatus):
        return _hsr_rows(status, detailed, focused, now)
    return _zzz_rows(status, detailed, focused, now)


@dataclass(frozen=True)
class _GameView:
    status: GameStatus
    detailed: bool
    focused: bool
    now: datetime
    include_heading: bool

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        if self.include_heading:
            yield Text(
                GAME_HEADINGS[self.status.game],
                style=STYLES[f"game.{self.status.game}"],
            )
        rows = _rows(self.status, self.detailed, self.focused, self.now)
        layout = choose_layout(options.max_width)
        if layout == "wide":
            table = Table.grid(padding=(0, 2))
            table.add_column(min_width=19)
            table.add_column(min_width=14)
            table.add_column()
            for row in rows:
                table.add_row(
                    Text(row.label),
                    Text(row.primary),
                    Text(row.secondary, style=STYLES["muted"]),
                )
            yield table
        else:
            for row in rows:
                if layout == "medium":
                    yield Text(f"{row.label:<21}  {row.primary}")
                else:
                    yield Text(row.label)
                    yield Text(f"  {row.primary}")
                if row.secondary:
                    yield Text(f"  {row.secondary}", style=STYLES["muted"])


def render_game(
    status: GameStatus,
    *,
    detailed: bool,
    focused: bool,
    now: datetime,
    include_heading: bool = True,
) -> RenderableType:
    require_aware(now)
    return _GameView(status, detailed, focused, now, include_heading)
