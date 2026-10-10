"""Read-only result presentation, preserving game identity and freshness."""

from collections.abc import Sequence
from datetime import datetime

from rich.console import Group, RenderableType
from rich.text import Text

from hoyo_cli.models.common import StatusResult, require_aware
from hoyo_cli.render.game import render_game
from hoyo_cli.render.styles import GAME_HEADINGS, STYLES
from hoyo_cli.timefmt import format_duration, format_local_timestamp


def render_overview(
    results: Sequence[StatusResult],
    *,
    account: str,
    detailed: bool,
    now: datetime,
    focused: bool = False,
) -> RenderableType:
    require_aware(now)
    parts: list[RenderableType] = [
        Text(f"HoYo Daily · {account}", style=STYLES["heading"]),
        Text(""),
    ]
    for result in results:
        parts.append(
            Text(GAME_HEADINGS[result.game], style=STYLES[f"game.{result.game}"])
        )
        if result.error is not None:
            parts.append(
                Text(
                    f"Unable to fetch fresh status: {result.error.message}",
                    style=STYLES["error"],
                )
            )
        if result.stale and result.fetched_at is not None:
            age = max(0, int((now - result.fetched_at).total_seconds()))
            parts.append(
                Text(
                    f"STALE · fetched {format_duration(age)} ago", style=STYLES["stale"]
                )
            )
        if result.status is not None:
            parts.append(
                render_game(
                    result.status,
                    detailed=detailed,
                    focused=focused,
                    now=now,
                    include_heading=False,
                )
            )
        else:
            parts.append(
                Text("No eligible cached status available.", style=STYLES["muted"])
            )
        parts.append(Text(""))
    # A mixed/stale overview must not carry a global fresh-data claim.
    if results and all(
        result.status is not None and not result.stale for result in results
    ):
        times = [
            result.fetched_at for result in results if result.fetched_at is not None
        ]
        if times:
            parts.append(
                Text(
                    f"Updated {format_local_timestamp(min(times), now)}",
                    style=STYLES["muted"],
                )
            )
    return Group(*parts)
