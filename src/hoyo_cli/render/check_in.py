"""Concise daily-action output from application-owned results."""

from collections.abc import Sequence

from rich.console import Group
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from hoyo_cli.models.common import CheckInResult
from hoyo_cli.render.errors import format_error
from hoyo_cli.render.styles import GAME_HEADINGS


def render_check_in(results: Sequence[CheckInResult], *, account: str) -> Group:
    rows: list[Table | Padding] = []
    name_width = max((len(GAME_HEADINGS[result.game]) for result in results), default=0)
    for result in results:
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True, width=name_width)
        table.add_column()
        name = Text(GAME_HEADINGS[result.game].title(), style=f"game.{result.game}")
        if result.outcome == "claimed":
            outcome = Text("Claimed", style="success")
        elif result.outcome == "already_checked_in":
            outcome = Text("Already checked in", style="success")
        elif result.outcome == "claimed_verified":
            outcome = Text("Claimed (verified)", style="success")
        elif result.outcome == "unconfirmed":
            outcome = Text("Unconfirmed", style="warning")
        else:
            outcome = Text("Failed", style="error")
        table.add_row(name, outcome)
        rows.append(table)
        if result.outcome == "claimed" and result.reward is not None:
            rows.append(Padding(Text(result.reward, style="text"), (0, 0, 0, 2)))
        if result.outcome in ("failed", "unconfirmed"):
            detail = Text(
                format_error(result.error)
                if result.error is not None
                else "Error: Check-in failed.",
                style="warning" if result.outcome == "unconfirmed" else "error",
            )
            rows.append(Padding(detail, (0, 0, 0, 2)))
    return Group(Text(f"Check-in · {account}", style="heading"), *rows)
