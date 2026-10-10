"""Semantic terminal output and explicit layout/color policies."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import cast

import pytest
from rich.console import Console

from hoyo_cli.errors import NetworkError
from hoyo_cli.hoyolab.adapters.genshin import normalize_genshin
from hoyo_cli.models.common import GameStatus, StatusResult
from hoyo_cli.models.genshin import GenshinStatus
from hoyo_cli.render.console import make_console
from hoyo_cli.render.game import render_game
from hoyo_cli.render.overview import render_overview
from hoyo_cli.render.widths import choose_layout

NOW = datetime(2026, 10, 10, 18, 31, tzinfo=timezone(timedelta(hours=7)))


@pytest.fixture
def status_snapshot() -> GenshinStatus:
    payload = cast(
        dict[str, object],
        json.loads((Path(__file__).parent / "fixtures/genshin_notes.json").read_text()),
    )
    return normalize_genshin(payload, now=NOW)


def render_text(
    status: GameStatus,
    width: int = 100,
    *,
    focused: bool = False,
    detailed: bool = False,
    now: datetime = NOW,
) -> str:
    output = StringIO()
    console = Console(file=output, width=width, color_system=None)
    console.print(render_game(status, detailed=detailed, focused=focused, now=now))
    return output.getvalue()


def test_genshin_compact_rows(status_snapshot: GenshinStatus) -> None:
    output = render_text(status_snapshot)
    for text in (
        "GENSHIN IMPACT",
        "190 / 200",
        "930 / 2400",
        "Reward claimed",
        "1 finished",
        "2d 4h",
        "Weekly Discounts",
    ):
        assert text in output
    assert "Expedition 1" not in output and "Archon" not in output


def test_focused_shows_individual_expeditions(status_snapshot: GenshinStatus) -> None:
    output = render_text(status_snapshot, focused=True)
    assert "Expedition 1" in output and "Finished" in output
    assert "Expedition 2" in output and "2h 14m" in output
    assert "Archon" not in output


def test_detailed_has_only_intentional_fields(status_snapshot: GenshinStatus) -> None:
    output = render_text(status_snapshot, detailed=True)
    assert "Stored Encounter" in output and "12.5" in output
    assert "Task Reward 1" in output and "Claimed" in output
    assert "Archon Quests" in output and "Test chapter" in output
    assert "avatar" not in output and "example.invalid" not in output


@pytest.mark.parametrize("width", [100, 70, 48])
@pytest.mark.parametrize("detailed", [False, True])
def test_completed_states_do_not_fabricate_historical_times(
    width: int, detailed: bool
) -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/genshin_notes.json").read_text()
    )
    payload.update(
        current_resin=200,
        current_home_coin=2400,
        resin_recovery_time="0",
        home_coin_recovery_time="0",
        transformer={
            "obtained": True,
            "recovery_time": {"Day": 0, "Hour": 0, "Minute": 0, "Second": 0},
        },
    )
    output = render_text(
        normalize_genshin(payload, now=NOW), width, focused=True, detailed=detailed
    )
    assert "200 / 200" in output and "2400 / 2400" in output
    assert output.count("Full") == 2
    assert "Finished" in output and "Ready" in output
    for fabricated in (
        "Expedition 1 at",
        "Transformer ready",
        "Resin full at",
        "Realm full at",
    ):
        assert fabricated not in output
    assert all(len(line) <= width for line in output.splitlines())


@pytest.mark.parametrize("width", [100, 70, 48])
def test_active_deadlines_remain_in_detailed_output(
    status_snapshot: GenshinStatus, width: int
) -> None:
    output = render_text(status_snapshot, width, detailed=True)
    assert "Expedition 1 at" not in output
    for label, deadline in (
        ("Expedition 2 at", "20:45"),
        ("Resin full at", "19:47"),
        ("Realm full at", "Mon 02:31"),
        ("Transformer ready", "Mon 22:31"),
    ):
        assert label in output and deadline in output
    for remaining in ("2h 14m", "1h 16m", "1d 8h", "2d 4h"):
        assert remaining in output
    assert "190 / 200" in output and "930 / 2400" in output
    assert all(len(line) <= width for line in output.splitlines())


def test_renderer_preserves_explicit_historical_completion_time(
    status_snapshot: GenshinStatus,
) -> None:
    activity = replace(
        status_snapshot.expeditions[0], completes_at=NOW - timedelta(hours=1)
    )
    snapshot = replace(status_snapshot, expeditions=(activity,))
    output = render_text(snapshot, detailed=True)
    assert "Finished" in output and "Expedition 1 at" in output and "17:31" in output


@pytest.mark.parametrize(
    "width, layout",
    [
        (100, "wide"),
        (80, "wide"),
        (79, "medium"),
        (70, "medium"),
        (60, "medium"),
        (59, "narrow"),
        (48, "narrow"),
    ],
)
def test_width_layouts_are_explicit(
    status_snapshot: GenshinStatus, width: int, layout: str
) -> None:
    assert choose_layout(width) == layout
    output = render_text(status_snapshot, width)
    lines = output.splitlines()
    resin = (
        lines.index("Resin")
        if layout == "narrow"
        else next(index for index, line in enumerate(lines) if line.startswith("Resin"))
    )
    if layout == "narrow":
        assert lines[resin + 1].strip() == "190 / 200"
        assert lines[resin + 2].strip().startswith("Full in")
    elif layout == "medium":
        assert "190 / 200" in lines[resin]
        assert lines[resin + 1].startswith("  Full in")
    else:
        assert "Full in" in lines[resin]
    assert all(len(line) <= width for line in lines)


@pytest.mark.parametrize(
    "interactive, no_color, configured",
    [(False, False, "always"), (True, True, "always"), (True, False, "never")],
)
def test_redirected_and_no_color_outputs_are_unstyled(
    status_snapshot: GenshinStatus,
    interactive: bool,
    no_color: bool,
    configured: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    console = make_console(
        width=100,
        no_color=no_color,
        interactive=interactive,
        configured_color=configured,
    )
    console.print(render_game(status_snapshot, detailed=False, focused=False, now=NOW))
    output = capsys.readouterr().out
    assert "\x1b[" not in output


def test_no_color_environment_overrides_always(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    console = make_console(
        width=100, no_color=False, interactive=True, configured_color="always"
    )
    assert console.color_system is None


def test_game_accents_only_on_headings(status_snapshot: GenshinStatus) -> None:
    console = Console(width=100, force_terminal=True, color_system="truecolor")
    segments = list(
        console.render(
            render_game(status_snapshot, detailed=True, focused=True, now=NOW),
            console.options,
        )
    )
    gold = [
        segment.text
        for segment in segments
        if segment.style is not None
        and segment.style.color is not None
        and segment.style.color.name == "#d3bc8e"
    ]
    assert gold and all("GENSHIN IMPACT" in text for text in gold)


def test_overview_stale_and_partial_failure_do_not_hide_success(
    status_snapshot: GenshinStatus,
) -> None:
    error = NetworkError("HoYoLAB request timed out.")
    results = (
        StatusResult(
            "genshin", status_snapshot, NOW - timedelta(minutes=34), True, error
        ),
        StatusResult("hsr", None, None, error=error),
    )
    console = Console(file=StringIO(), width=70, color_system=None)
    with console.capture() as capture:
        console.print(render_overview(results, account="main", detailed=False, now=NOW))
    output = capture.get()
    assert "STALE · fetched 34m ago" in output
    assert "190 / 200" in output and "HONKAI: STAR RAIL" in output
    assert "No eligible cached status" in output
    assert "Updated" not in output


def test_snapshot_resource_amount_is_never_recomputed(
    status_snapshot: GenshinStatus,
) -> None:
    output = render_text(status_snapshot, now=NOW + timedelta(hours=2))
    assert "190 / 200" in output
    assert "Recovery time elapsed" in output


def test_full_resource_has_full_wording(status_snapshot: GenshinStatus) -> None:
    full = replace(
        status_snapshot, resin=replace(status_snapshot.resin, current=200, full_at=NOW)
    )
    output = render_text(full)
    assert "200 / 200" in output and "Full" in output
    assert "0m" not in output


def test_fresh_overview_footer_and_markup_are_literal(
    status_snapshot: GenshinStatus,
) -> None:
    console = Console(file=StringIO(), width=100, color_system=None)
    with console.capture() as captured:
        console.print(
            render_overview(
                (StatusResult("genshin", status_snapshot, NOW),),
                account="[red]main[/red]",
                detailed=False,
                now=NOW,
            )
        )
    output = captured.get()
    assert "Updated 18:31" in output
    assert "[red]main[/red]" in output
    assert output.count("GENSHIN IMPACT") == 1


@pytest.mark.parametrize("game", ["hsr", "zzz"])
@pytest.mark.parametrize("width", [100, 80, 70, 60, 59, 48])
@pytest.mark.parametrize("mode", ["compact", "focused", "detailed"])
def test_phase5_game_views(game: str, width: int, mode: str) -> None:
    from hoyo_cli.hoyolab.adapters.hsr import normalize_hsr
    from hoyo_cli.hoyolab.adapters.zzz import normalize_zzz

    payload = json.loads(
        (Path(__file__).parent / f"fixtures/{game}_notes.json").read_text()
    )
    status = (normalize_hsr if game == "hsr" else normalize_zzz)(payload, now=NOW)
    output = render_text(
        status, width, focused=mode == "focused", detailed=mode == "detailed"
    )
    if game == "hsr":
        for text in (
            "Trailblaze Power",
            "211 / 300",
            "Reserved Power",
            "842 / 2400",
            "Daily Training",
            "Assignments",
            "Simulated Universe",
            "Divergent Universe",
            "Echo of War",
        ):
            assert text in output
        assert ("Test assignment A" in output) == (mode != "compact")
        if mode == "detailed":
            assert "Power full at" in output and "Sun 03:25" in output
            assert "Reserve full" in output and "Not full" in output
    else:
        for text in (
            "Battery Charge",
            "89 / 240",
            "Engagement",
            "Scratch Card",
            "Video Store",
            "Hollow Zero",
            "Weekly Tasks",
            "Member Card",
            "Temple",
        ):
            assert text in output
        assert ("Temple Bench" in output) == (mode != "compact")
        if mode == "detailed":
            for text in (
                "Battery full at",
                "Temple Level",
                "Temple Currency",
                "Temple Reset",
                "Weekly Reset",
                "Member Card Expiry",
            ):
                assert text in output
        assert (
            "Bench can produce" in output
            if mode == "compact"
            else "Can produce" in output
        )
    assert "example.invalid" not in output and "0m" not in output
    assert all(len(line) <= width for line in output.splitlines())
