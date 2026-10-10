"""Concise daily-action output from application-owned results."""

from collections.abc import Sequence

from rich.console import Group
from rich.table import Table
from rich.text import Text

from hoyo_cli.models.common import CheckInResult
from hoyo_cli.render.errors import format_error
from hoyo_cli.render.styles import GAME_HEADINGS


def render_check_in(results: Sequence[CheckInResult], *, account: str) -> Group:
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column()
    for result in results:
        name = Text(GAME_HEADINGS[result.game].title(), style=f"game.{result.game}")
        if result.outcome == "claimed":
            outcome = Text("Claimed", style="success")
            if result.reward is not None:
                outcome.append(f" · {result.reward}", style="text")
        elif result.outcome == "already_checked_in":
            outcome = Text("Already checked in", style="success")
        else:
            outcome = Text("Failed\n", style="error")
            outcome.append(
                format_error(result.error)
                if result.error is not None
                else "Error: Check-in failed.",
                style="error",
            )
        table.add_row(name, outcome)
    return Group(Text(f"Check-in · {account}", style="heading"), table)
