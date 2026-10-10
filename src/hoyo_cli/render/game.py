"""Width-aware Genshin views consume only normalized snapshots."""

from dataclasses import dataclass
from datetime import datetime

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.table import Table
from rich.text import Text

from hoyo_cli.errors import UnsupportedDataError
from hoyo_cli.models.common import (
    GameStatus,
    ResourceStatus,
    TimedActivity,
    require_aware,
)
from hoyo_cli.models.genshin import GenshinStatus
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


def _rows(
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


@dataclass(frozen=True)
class _GameView:
    status: GenshinStatus
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
                    yield Text(f"{row.label:<21}{row.primary}")
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
    if not isinstance(status, GenshinStatus):
        raise UnsupportedDataError("Rendering for this game is not implemented yet.")
    return _GameView(status, detailed, focused, now, include_heading)
