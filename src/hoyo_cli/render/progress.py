"""Delayed, transient stderr feedback for observed network work."""

import asyncio
import os
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from types import TracebackType
from typing import Literal

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from hoyo_cli.models.common import GameId
from hoyo_cli.models.progress import ProgressEvent, ProgressStage
from hoyo_cli.render.console import make_console

PRESENTATION_DELAY = 0.2


class OperationProgress:
    """One operation owns its timer and live display; services own all results.

    Use as an async context around service work and pass ``observe`` to the
    service. Wrap any future interactive prompt in ``suspend``.
    """

    def __init__(
        self,
        operation: Literal["status", "check_in", "browser_read", "session_verify"],
        games: Sequence[GameId] = (),
        *,
        no_color: bool = False,
        configured_color: str = "auto",
        console: Console | None = None,
    ) -> None:
        self._operation = operation
        self._games = frozenset(games)
        self._completed: set[GameId] = set()
        self._stages: dict[GameId, ProgressStage] = {}
        self._console = console or make_console(
            width=None,
            no_color=no_color,
            interactive=sys.stderr.isatty(),
            configured_color=configured_color,
            stderr=True,
        )
        self._enabled = (
            (bool(self._games) or operation in ("browser_read", "session_verify"))
            and sys.stdout.isatty()
            and sys.stderr.isatty()
            and self._console.is_terminal
            and not self._console.is_dumb_terminal
            and not no_color
            and configured_color != "never"
            and "NO_COLOR" not in os.environ
            and "CI" not in os.environ
        )
        self._timer: asyncio.TimerHandle | None = None
        self._live: Live | None = None
        self._running = False
        self._suspended = 0

    async def __aenter__(self) -> "OperationProgress":
        self._running = True
        self._schedule()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._running = False
        self._stop()

    def observe(self, event: ProgressEvent) -> None:
        if not self._running or event.game not in self._games:
            return
        if event.stage == "complete":
            self._completed.add(event.game)
            self._stages.pop(event.game, None)
        elif event.game not in self._completed:
            self._stages[event.game] = event.stage
        if self._live is not None:
            try:
                self._live.update(self._renderable(), refresh=True)
            except Exception:
                self._enabled = False
                self._stop()

    @contextmanager
    def suspend(self) -> Generator[None]:
        """Clear and pause progress for a prompt, including nested prompts."""
        self._suspended += 1
        self._stop()
        try:
            yield
        finally:
            self._suspended -= 1
            self._schedule()

    def _schedule(self) -> None:
        if self._enabled and self._running and not self._suspended:
            self._timer = asyncio.get_running_loop().call_later(
                PRESENTATION_DELAY, self._start
            )

    def _start(self) -> None:
        self._timer = None
        if not self._running or self._suspended:
            return
        self._live = Live(
            self._renderable(),
            console=self._console,
            transient=True,
            refresh_per_second=10,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        try:
            self._live.start(refresh=True)
        except Exception:
            self._enabled = False
            self._stop()

    def _stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._live is not None:
            live, self._live = self._live, None
            try:
                live.stop()
            except Exception:
                # A broken terminal must not discard an observed API result.
                self._enabled = False

    def _renderable(self) -> Spinner:
        if self._operation in ("browser_read", "session_verify"):
            label = (
                "Reading browser cookies"
                if self._operation == "browser_read"
                else "Verifying HoYoLAB session"
            )
            return Spinner("dots", text=Text(label))
        label = "Fetching status"
        if self._operation == "check_in":
            active = self._stages.values()
            label = (
                "Verifying"
                if "verifying" in active
                else "Claiming"
                if "claiming" in active
                else "Checking today's rewards"
            )
        count = f"{len(self._completed)}/{len(self._games)}"
        suffix = "complete" if self._console.width < 60 else "games complete"
        return Spinner("dots", text=Text(f"{label} · {count} {suffix}"))
