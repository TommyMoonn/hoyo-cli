"""Terminal capability, real progress counts, and transient lifecycle coverage."""

import asyncio
import io
import sys
from collections.abc import Iterator
from typing import Literal
from unittest.mock import patch

import click
import pytest
from rich.console import Console

from hoyo_cli.models.progress import ProgressEvent
from hoyo_cli.render import progress


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(progress, "PRESENTATION_DELAY", 0.01)
    stream = io.StringIO()
    yield stream


def presenter(stream: io.StringIO, *, width: int = 100) -> progress.OperationProgress:
    return progress.OperationProgress(
        "status",
        ("genshin", "hsr", "zzz"),
        console=Console(file=stream, force_terminal=True, width=width),
    )


async def show() -> None:
    await asyncio.sleep(0.03)


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [48, 70, 100])
async def test_tty_counts_resolved_games_and_clears_display(
    terminal: io.StringIO,
    width: int,
) -> None:
    async with presenter(terminal, width=width) as display:
        display.observe(ProgressEvent("genshin", "fetching"))
        display.observe(ProgressEvent("hsr", "complete", "stale"))
        display.observe(ProgressEvent("zzz", "complete", "failed"))
        # Duplicate and foreign events cannot invent additional completed games.
        display.observe(ProgressEvent("zzz", "complete", "failed"))
        await show()
        assert "Fetching status" in terminal.getvalue()
        assert "2/3" in terminal.getvalue()
        assert "3/3" not in terminal.getvalue() and "%" not in terminal.getvalue()
    assert "\x1b[?25h" in terminal.getvalue()
    assert terminal.getvalue().endswith("\x1b[2K")
    before = terminal.getvalue()
    display.observe(ProgressEvent("genshin", "complete", "fresh"))
    await show()
    assert terminal.getvalue() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["status", "browser_read", "session_verify"])
@pytest.mark.parametrize(
    "mode", ["stdout", "stderr", "no-color", "env", "config", "ci"]
)
async def test_noninteractive_modes_are_silent(
    terminal: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    operation: Literal["status", "browser_read", "session_verify"],
) -> None:
    if mode in ("stdout", "stderr"):
        monkeypatch.setattr(getattr(sys, mode), "isatty", lambda: False)
    if mode == "env":
        monkeypatch.setenv("NO_COLOR", "")
    if mode == "ci":
        monkeypatch.setenv("CI", "true")
    async with progress.OperationProgress(
        operation,
        ("genshin",) if operation == "status" else (),
        no_color=mode == "no-color",
        configured_color="never" if mode == "config" else "always",
        console=Console(file=terminal, force_terminal=True),
    ) as display:
        display.observe(ProgressEvent("genshin", "fetching"))
        await show()
        display.observe(ProgressEvent("genshin", "complete", "fresh"))
    assert terminal.getvalue() == ""


@pytest.mark.asyncio
async def test_fast_operation_has_no_flicker_or_late_timer(
    terminal: io.StringIO,
) -> None:
    async with presenter(terminal) as display:
        display.observe(ProgressEvent("hsr", "complete", "fresh"))
    await show()
    assert terminal.getvalue() == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError, KeyboardInterrupt, asyncio.CancelledError, click.Abort]
)
async def test_every_exit_restores_cursor_and_removes_progress(
    terminal: io.StringIO,
    error: type[BaseException],
) -> None:
    with pytest.raises(error):
        async with presenter(terminal):
            await show()
            raise error()
    assert "\x1b[?25h" in terminal.getvalue()
    assert terminal.getvalue().endswith("\x1b[2K")
    before = terminal.getvalue()
    await show()
    assert terminal.getvalue() == before


@pytest.mark.asyncio
async def test_check_in_stages_follow_active_work(terminal: io.StringIO) -> None:
    async with progress.OperationProgress(
        "check_in",
        ("hsr",),
        console=Console(file=terminal, force_terminal=True),
    ) as display:
        display.observe(ProgressEvent("hsr", "checking"))
        await show()
        assert "Checking today's rewards" in terminal.getvalue()
        terminal.truncate(0)
        terminal.seek(0)
        display.observe(ProgressEvent("hsr", "claiming"))
        assert "Claiming" in terminal.getvalue() and "0/1" in terminal.getvalue()
        display.observe(ProgressEvent("hsr", "verifying"))
        assert "Verifying" in terminal.getvalue()
        display.observe(ProgressEvent("hsr", "complete", "claimed"))
        assert "1/1" in terminal.getvalue()


@pytest.mark.asyncio
async def test_prompt_suspends_pending_and_running_display(
    terminal: io.StringIO,
) -> None:
    async with presenter(terminal) as display:
        with display.suspend():
            await show()
            assert terminal.getvalue() == ""
        await show()
        with display.suspend():
            assert terminal.getvalue().endswith("\x1b[2K")
            before = terminal.getvalue()
            display.observe(ProgressEvent("hsr", "complete", "fresh"))
            await show()
            assert terminal.getvalue() == before
        await show()
        assert "1/3" in terminal.getvalue()


@pytest.mark.asyncio
async def test_presentation_failure_does_not_fail_operation(
    terminal: io.StringIO,
) -> None:
    with patch.object(progress.Live, "start", side_effect=OSError("secret")):
        async with presenter(terminal):
            await show()
    assert "secret" not in terminal.getvalue()
