"""Semantic terminal output and explicit layout/color policies."""

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import cast

import pytest
from rich.console import Console

from hoyo_cli.errors import AppError, NetworkError
from hoyo_cli.hoyolab.adapters.genshin import normalize_genshin
from hoyo_cli.hoyolab.adapters.zzz import normalize_zzz
from hoyo_cli.models.common import (
    CountStatus,
    GameId,
    GameStatus,
    ResourceStatus,
    StatusResult,
)
from hoyo_cli.models.genshin import GenshinStatus
from hoyo_cli.models.hsr import HSRStatus
from hoyo_cli.models.zzz import ZZZStatus
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


@pytest.fixture
def zzz_snapshot() -> ZZZStatus:
    payload: object = json.loads(
        (Path(__file__).parent / "fixtures/zzz_notes.json").read_text()
    )
    return normalize_zzz(payload, now=NOW)


@pytest.fixture
def color_console(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], Console]:
    # A styled terminal simulation must not inherit the test host's dumb TERM.
    monkeypatch.setenv("TERM", "xterm-256color")

    def make(width: int) -> Console:
        return Console(
            file=StringIO(),
            width=width,
            force_terminal=True,
            color_system="truecolor",
            no_color=False,
            legacy_windows=False,
        )

    return make


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("mode", ["compact", "focused", "detailed"])
@pytest.mark.parametrize(
    "availability", ["bounty-only", "investigation-only", "both", "absent", "locked"]
)
def test_hollow_zero_metrics_are_unambiguous(
    zzz_snapshot: ZZZStatus, width: int, mode: str, availability: str
) -> None:
    snapshot = replace(
        zzz_snapshot,
        hollow_zero_bounties=(
            CountStatus(0, 8000) if availability in ("bounty-only", "both") else None
        ),
        hollow_zero_points=(
            CountStatus(600, 800)
            if availability in ("investigation-only", "both")
            else None
        ),
        bounties_state=(
            "Locked"
            if availability == "locked"
            else "Available"
            if availability in ("bounty-only", "both")
            else "Unavailable"
        ),
    )
    output = render_text(
        snapshot, width, focused=mode == "focused", detailed=mode == "detailed"
    )
    section = output[output.index("Hollow Zero") : output.index("Weekly Tasks")]
    if availability in ("bounty-only", "both"):
        assert section.count("0 / 8000") == 1
    if availability in ("investigation-only", "both"):
        assert section.count("600 / 800") == 1
        assert "Investigation" in section
    if mode == "compact":
        assert section.count("Hollow Zero") == 1
        if availability == "bounty-only":
            assert "Bounties" not in section and "Investigation" not in section
        if availability == "investigation-only":
            assert "Bounties" not in section
        if availability == "both":
            assert "Bounties" in section
    else:
        assert "Hollow Zero Bounties" in section
        assert "Hollow Zero Investigation" in section
        assert not any(line.strip() == "Hollow Zero" for line in section.splitlines())
    if availability == "absent":
        assert "Data unavailable" in section and "Locked" not in section
    if availability == "locked":
        assert "Locked" in section
    assert "…" not in output
    assert all(len(line) <= width for line in output.splitlines())


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


@pytest.mark.parametrize("width", [100, 70, 48])
def test_stale_age_label_belongs_to_each_game_section(
    status_snapshot: GenshinStatus, width: int
) -> None:
    from hoyo_cli.hoyolab.adapters.hsr import normalize_hsr
    from hoyo_cli.hoyolab.adapters.zzz import normalize_zzz

    def read(game: str) -> object:
        return json.loads(
            (Path(__file__).parent / f"fixtures/{game}_notes.json").read_text()
        )

    error = NetworkError("Request timed out.")
    results = (
        StatusResult("genshin", status_snapshot, NOW),
        StatusResult(
            "hsr",
            normalize_hsr(read("hsr"), now=NOW),
            NOW - timedelta(minutes=34),
            stale=True,
            error=error,
        ),
        StatusResult(
            "zzz",
            normalize_zzz(read("zzz"), now=NOW),
            NOW - timedelta(hours=2, minutes=59),
            stale=True,
            error=error,
        ),
    )
    output = StringIO()
    Console(file=output, width=width, color_system=None).print(
        render_overview(results, account="main", detailed=False, now=NOW)
    )
    text = output.getvalue()
    genshin, other = text.split("HONKAI: STAR RAIL")
    hsr, zzz = other.split("ZENLESS ZONE ZERO")
    assert "STALE" not in genshin
    assert "STALE · fetched 34m ago" in hsr and "211 / 300" in hsr
    assert "STALE · fetched 2h 59m ago" in zzz and "89 / 240" in zzz
    assert "Updated" not in text
    assert all(len(line) <= width for line in text.splitlines())


@pytest.mark.parametrize("game", ["genshin", "hsr", "zzz"])
def test_cached_values_never_increase_over_time(game: GameId) -> None:
    from hoyo_cli.hoyolab.adapters.genshin import normalize_genshin
    from hoyo_cli.hoyolab.adapters.hsr import normalize_hsr
    from hoyo_cli.hoyolab.adapters.zzz import normalize_zzz

    payload: object = json.loads(
        (Path(__file__).parent / f"fixtures/{game}_notes.json").read_text()
    )
    normalize = {
        "genshin": normalize_genshin,
        "hsr": normalize_hsr,
        "zzz": normalize_zzz,
    }[game]
    snapshot = normalize(payload, now=NOW)
    error = NetworkError("Request timed out.")
    for hours in (1, 2, 3):
        output = StringIO()
        Console(file=output, width=100, color_system=None).print(
            render_overview(
                (StatusResult(game, snapshot, NOW, stale=True, error=error),),
                account="main",
                detailed=True,
                now=NOW + timedelta(hours=hours),
            )
        )
        text = output.getvalue()
        expected = {"genshin": "190 / 200", "hsr": "211 / 300", "zzz": "89 / 240"}
        assert expected[game] in text
        if game == "genshin":
            assert "930 / 2400" in text
        assert f"STALE · fetched {hours}h ago" in text
        assert "Updated" not in text


def test_cache_write_warning_is_distinct_from_fetch_error(
    status_snapshot: GenshinStatus,
) -> None:
    output = StringIO()
    Console(file=output, width=100, color_system=None).print(
        render_overview(
            (
                StatusResult(
                    "genshin",
                    status_snapshot,
                    NOW,
                    cache_error=AppError(
                        "Unable to save status cache; fresh status is still available."
                    ),
                ),
            ),
            account="main",
            detailed=False,
            now=NOW,
        )
    )
    text = output.getvalue()
    assert "Unable to save status cache" in text
    assert "Updated 18:31" in text and "190 / 200" in text
    assert "Unable to fetch" not in text and "STALE" not in text


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("mode", ["compact", "focused", "detailed"])
def test_missing_zzz_fields_display_unknown_not_gameplay_states(
    width: int, mode: str
) -> None:
    payload: object = {
        "energy": {"progress": {"current": 89, "max": 240}, "restore": 55260},
        "vitality": {"current": 400, "max": 400},
    }
    output = render_text(
        normalize_zzz(payload, now=NOW),
        width,
        focused=mode == "focused",
        detailed=mode == "detailed",
    )
    assert "Data unavailable" in output
    for inferred in ("Locked", "Done", "Inactive", "Unavailable"):
        assert inferred not in output
    assert all(len(line) <= width for line in output.splitlines())


@pytest.mark.parametrize("width", [48, 70, 100])
def test_actionable_states_have_semantic_emphasis(
    status_snapshot: GenshinStatus,
    width: int,
    color_console: Callable[[int], Console],
) -> None:
    from rich.text import Text

    status = replace(
        status_snapshot,
        resin=replace(status_snapshot.resin, current=200, full_at=None),
        commissions=CountStatus(2, 4),
        commission_reward_claimed=False,
    )
    console = color_console(width)
    assert console.width == width
    with console.capture() as captured:
        console.print(render_game(status, detailed=False, focused=True, now=NOW))
    text = Text.from_ansi(captured.get())
    for value in ("200 / 200", "2 / 4", "Reward unclaimed", "1 finished", "Finished"):
        style = text.get_style_at_offset(console, text.plain.index(value))
        assert style.bold is True, value
        assert style.color is not None and style.color.number == 3, value


@pytest.mark.parametrize("width", [48, 70, 100])
def test_explicit_completion_is_quiet_without_claim_inference(
    zzz_snapshot: ZZZStatus,
    width: int,
    color_console: Callable[[int], Console],
) -> None:
    from rich.text import Text

    status = replace(
        zzz_snapshot, member_card=replace(zzz_snapshot.member_card, state="Inactive")
    )
    console = color_console(width)
    assert console.width == width
    with console.capture() as captured:
        console.print(render_game(status, detailed=False, focused=False, now=NOW))
    text = Text.from_ansi(captured.get())
    for value in ("400 / 400", "Done", "8000 / 8000", "Inactive"):
        style = text.get_style_at_offset(console, text.plain.index(value))
        assert style == console.get_style("dim"), value
    assert "Claimed" not in text.plain and "Reward claimed" not in text.plain


def test_complete_commissions_still_show_unclaimed_reward(
    status_snapshot: GenshinStatus,
    color_console: Callable[[int], Console],
) -> None:
    from rich.text import Text

    status = replace(status_snapshot, commission_reward_claimed=False)
    console = color_console(100)
    with console.capture() as captured:
        console.print(render_game(status, detailed=False, focused=False, now=NOW))
    text = Text.from_ansi(captured.get())
    assert "4 / 4" in text.plain and "Reward unclaimed" in text.plain
    style = text.get_style_at_offset(console, text.plain.index("Reward unclaimed"))
    assert style.bold is True
    assert style.color is not None and style.color.number == 3


@pytest.mark.parametrize("width", [48, 70, 100])
def test_mixed_freshness_emphasizes_only_current_actions(
    status_snapshot: GenshinStatus,
    zzz_snapshot: ZZZStatus,
    width: int,
    color_console: Callable[[int], Console],
) -> None:
    from rich.text import Text

    fresh = replace(
        status_snapshot,
        resin=replace(status_snapshot.resin, current=200, full_at=None),
    )
    console = color_console(width)
    with console.capture() as captured:
        console.print(
            render_overview(
                (
                    StatusResult("genshin", fresh, NOW),
                    StatusResult(
                        "zzz",
                        zzz_snapshot,
                        NOW - timedelta(minutes=34),
                        stale=True,
                        error=NetworkError("Timeout."),
                    ),
                ),
                account="main",
                detailed=False,
                now=NOW,
            )
        )
    text = Text.from_ansi(captured.get())
    style = text.get_style_at_offset(console, text.plain.index("200 / 200"))
    assert style.bold is True and style.color is not None and style.color.number == 3
    stale_action = text.get_style_at_offset(
        console, text.plain.index("Revenue available")
    )
    assert stale_action.dim is True and stale_action.color is None
    assert "Snapshot estimate" in text.plain and "STALE · fetched 34m ago" in text.plain
    assert "Updated" not in text.plain


@pytest.mark.parametrize("game", ["genshin", "hsr", "zzz"])
@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("elapsed", [False, True])
@pytest.mark.parametrize("detailed", [False, True])
def test_stale_resource_times_are_snapshot_estimates(
    game: GameId, width: int, elapsed: bool, detailed: bool
) -> None:
    from hoyo_cli.hoyolab.adapters.hsr import normalize_hsr

    payload: object = json.loads(
        (Path(__file__).parent / f"fixtures/{game}_notes.json").read_text()
    )
    snapshot = {
        "genshin": normalize_genshin,
        "hsr": normalize_hsr,
        "zzz": normalize_zzz,
    }[game](payload, now=NOW)
    # Fix one deadline to exercise both sides of render time without changing counts.
    resource = {
        "genshin": "resin",
        "hsr": "power",
        "zzz": "battery",
    }[game]
    original = getattr(snapshot, resource)
    snapshot = replace(
        snapshot, **{resource: replace(original, full_at=NOW + timedelta(hours=1))}
    )
    render_time = NOW + timedelta(hours=2 if elapsed else 0, minutes=34)
    output = StringIO()
    Console(file=output, width=width, color_system=None).print(
        render_overview(
            (
                StatusResult(
                    game, snapshot, NOW, stale=True, error=NetworkError("Timeout.")
                ),
            ),
            account="main",
            detailed=detailed,
            now=render_time,
        )
    )
    text = output.getvalue()
    assert "Snapshot estimate" in text and "19:31" in text
    assert "Full in" not in text and "Recovery time elapsed" not in text
    assert "STALE · fetched" in text and "Updated" not in text
    expected = {"genshin": "190 / 200", "hsr": "211 / 300", "zzz": "89 / 240"}
    assert expected[game] in text
    assert getattr(snapshot, resource).full_at == NOW + timedelta(hours=1)
    assert "…" not in text
    assert all(len(line) <= width for line in text.splitlines())


@pytest.fixture(params=["genshin", "hsr", "zzz"])
def resource_deadline_snapshot(
    request: pytest.FixtureRequest,
) -> tuple[GameStatus, tuple[tuple[str, ResourceStatus], ...]]:
    from hoyo_cli.hoyolab.adapters.hsr import normalize_hsr

    game = cast(GameId, request.param)
    payload: object = json.loads(
        (Path(__file__).parent / f"fixtures/{game}_notes.json").read_text()
    )
    snapshot = {
        "genshin": normalize_genshin,
        "hsr": normalize_hsr,
        "zzz": normalize_zzz,
    }[game](payload, now=NOW)
    deadline = NOW + timedelta(hours=1)
    if isinstance(snapshot, GenshinStatus):
        assert snapshot.realm_currency is not None
        snapshot = replace(
            snapshot,
            resin=replace(snapshot.resin, full_at=deadline),
            realm_currency=replace(
                snapshot.realm_currency, full_at=deadline + timedelta(hours=1)
            ),
        )
        assert snapshot.realm_currency is not None
        resources = (
            ("Resin full at", snapshot.resin),
            ("Realm full at", snapshot.realm_currency),
        )
    elif isinstance(snapshot, HSRStatus):
        snapshot = replace(snapshot, power=replace(snapshot.power, full_at=deadline))
        resources = (("Power full at", snapshot.power),)
    else:
        snapshot = replace(
            snapshot, battery=replace(snapshot.battery, full_at=deadline)
        )
        resources = (("Battery full at", snapshot.battery),)
    return snapshot, resources


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("elapsed", [False, True])
@pytest.mark.parametrize("stale", [False, True])
def test_detailed_resource_deadlines_respect_freshness(
    resource_deadline_snapshot: tuple[
        GameStatus, tuple[tuple[str, ResourceStatus], ...]
    ],
    width: int,
    elapsed: bool,
    stale: bool,
) -> None:
    snapshot, resources = resource_deadline_snapshot
    render_time = NOW + timedelta(hours=2 if elapsed else 0, minutes=34)
    output = StringIO()
    Console(file=output, width=width, color_system=None).print(
        render_overview(
            (
                StatusResult(
                    snapshot.game,
                    snapshot,
                    NOW,
                    stale=stale,
                    error=NetworkError("Timeout.") if stale else None,
                ),
            ),
            account="main",
            detailed=True,
            focused=True,
            now=render_time,
        )
    )
    text = output.getvalue()
    for label, resource in resources:
        assert resource.full_at is not None
        assert (resource.full_at < render_time) == elapsed
        assert f"{resource.current} / {resource.maximum}" in text
        assert resource.full_at.strftime("%H:%M") in text
        if stale:
            assert label not in text
        else:
            assert label in text
    if stale:
        age = "2h 34m" if elapsed else "34m"
        assert f"STALE · fetched {age} ago" in text
        assert text.count("Snapshot estimate") == len(resources)
        assert "Full in" not in text and "Updated" not in text
    else:
        assert "Snapshot estimate" not in text and "STALE" not in text
        assert "Updated" in text
        assert ("Full in" in text) == (not elapsed)
    assert "\x1b" not in text and "…" not in text
    assert all(len(line) <= width for line in text.splitlines())
