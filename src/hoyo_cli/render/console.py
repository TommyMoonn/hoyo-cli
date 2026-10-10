"""One color policy for interactive and redirected output."""

import os

from rich.console import Console

from hoyo_cli.render.styles import THEME


def make_console(
    *,
    width: int | None,
    no_color: bool,
    interactive: bool,
    configured_color: str,
    stderr: bool = False,
) -> Console:
    if configured_color not in ("auto", "always", "never"):
        raise ValueError("Invalid configured color policy.")
    disabled = (
        no_color
        or not interactive
        or configured_color == "never"
        or "NO_COLOR" in os.environ
    )
    return Console(
        stderr=stderr,
        width=width,
        theme=THEME,
        color_system=None if disabled else "auto",
        force_terminal=False if disabled else interactive,
        no_color=disabled,
        highlight=False,
        markup=False,
    )
