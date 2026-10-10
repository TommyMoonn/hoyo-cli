"""Spec acceptance through the CLI, with only external boundaries replaced."""

import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import cast
from unittest.mock import call

import genshin
import pytest
from click.testing import CliRunner
from conftest import TEST_COOKIE, TEST_UIDS, AcceptanceEnvironment
from rich.cells import cell_len

from hoyo_cli.auth.browser_import import BrowserSession
from hoyo_cli.auth.credentials import parse_cookie_header
from hoyo_cli.auth.plaintext_backend import PlaintextStore
from hoyo_cli.cli import main
from hoyo_cli.config.models import AccountConfig
from hoyo_cli.config.repository import load_config, save_account
from hoyo_cli.hoyolab.discovery import CharacterRef
from hoyo_cli.models.common import GAME_IDS, GameId


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize(
    "failure, summary, recovery",
    [
        (
            genshin.errors.InvalidCookies(msg="acceptance-secret-not-real"),
            "rejected",
            "hoyo init",
        ),
        (TimeoutError("acceptance-secret-not-real"), "timed out", "connection"),
        (ValueError("acceptance-secret-not-real"), "unsupported", "hoyo-cli"),
        (RuntimeError("acceptance-secret-not-real"), "failed", "Retry"),
    ],
)
def test_partial_result_preserves_safe_cause_and_recovery(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    width: int,
    failure: Exception,
    summary: str,
    recovery: str,
) -> None:
    acceptance_env.width = width
    acceptance_env.notes("hsr").side_effect = failure
    result = runner.invoke(main, ["--no-color"])
    assert result.exit_code == 0, result.output
    assert "190 / 200" in result.stdout and "89 / 240" in result.stdout
    assert "Error:" in result.stdout and "Try:" in result.stdout
    assert summary in result.stdout and recovery in result.stdout
    assert not result.stderr
    assert all(
        value not in result.output
        for value in ("acceptance-secret-not-real", "Traceback", "RuntimeError", "\x1b")
    )
    assert all(cell_len(line) <= width for line in result.output.splitlines())
    acceptance_env.client.claim_daily_reward.assert_not_called()


def test_check_in_failure_shows_hint_without_erasing_claimed_siblings(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
) -> None:
    async def state(*, game: genshin.Game) -> genshin.models.DailyRewardInfo:
        if game == genshin.Game.STARRAIL:
            raise genshin.errors.InvalidCookies(msg="acceptance-secret-not-real")
        return genshin.models.DailyRewardInfo(False, 0)

    acceptance_env.client.get_reward_info.side_effect = state
    result = runner.invoke(main, ["check-in", "--no-color"])
    assert result.exit_code == 1
    assert result.stdout.count("Claimed") == 2
    assert "Failed" in result.stdout and "Error:" in result.stdout
    assert "Try:" in result.stdout and "hoyo init" in result.stdout
    assert "acceptance-secret-not-real" not in result.output
    assert acceptance_env.client.claim_daily_reward.await_count == 2


RESOURCE: dict[GameId, tuple[str, str]] = {
    "genshin": ("Resin", "190 / 200"),
    "hsr": ("Trailblaze Power", "211 / 300"),
    "zzz": ("Battery Charge", "89 / 240"),
}


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("verification", [True, False, "failed", "delayed"])
@pytest.mark.parametrize("single_game", [True, False])
def test_ambiguous_claim_is_reconciled_without_another_mutation(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    width: int,
    verification: bool | str,
    single_game: bool,
) -> None:
    acceptance_env.width = width
    reads: dict[genshin.Game, int] = {}

    async def state(*, game: genshin.Game) -> genshin.models.DailyRewardInfo:
        reads[game] = reads.get(game, 0) + 1
        if game == genshin.Game.STARRAIL and reads[game] > 1:
            if verification == "failed":
                raise genshin.errors.InvalidCookies(msg="acceptance-secret-not-real")
            if verification == "delayed":
                raise TimeoutError("acceptance-secret-not-real")
            return genshin.models.DailyRewardInfo(verification is True, 1)
        return genshin.models.DailyRewardInfo(game == genshin.Game.ZZZ, 0)

    async def claim(*, game: genshin.Game, reward: bool) -> None:
        if game == genshin.Game.STARRAIL:
            raise TimeoutError("acceptance-secret-not-real")

    acceptance_env.client.get_reward_info.side_effect = state
    acceptance_env.client.claim_daily_reward.side_effect = claim
    arguments = ["check-in", *(["hsr"] if single_game else []), "--no-color"]
    result = runner.invoke(main, arguments)
    assert result.exit_code == (0 if verification is True else 1), result.output
    assert ("Claimed (verified)" if verification is True else "Unconfirmed") in (
        result.stdout
    )
    assert "Failed" not in result.stdout
    if verification is not True:
        assert "Check today's state on HoYoLAB" in " ".join(result.stdout.split())
    if not single_game:
        assert "Genshin Impact" in result.stdout and "Claimed" in result.stdout
        assert "Zenless Zone Zero" in result.stdout
        assert "Already checked in" in result.stdout
    assert not result.stderr
    assert all(cell_len(line) <= width for line in result.stdout.splitlines())
    assert all(
        value not in result.output
        for value in ("acceptance-secret-not-real", "Traceback", "\x1b", "…")
    )
    claims = acceptance_env.client.claim_daily_reward.await_args_list
    assert claims.count(call(game=genshin.Game.STARRAIL, reward=False)) == 1
    assert len(claims) == (1 if single_game else 2)
    # A reconciliation query uses the gateway's existing two-attempt read budget.
    assert reads[genshin.Game.STARRAIL] == (3 if verification == "delayed" else 2)
    assert all(
        entry[0] in ("get_reward_info", "claim_daily_reward")
        for entry in acceptance_env.client.mock_calls
    )
    for game in GAME_IDS:
        acceptance_env.notes(game).assert_not_called()


@pytest.mark.parametrize("arguments", [[], ["status"], ["--account", "alt"]])
def test_all_three_fresh(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment, arguments: list[str]
) -> None:
    result = runner.invoke(main, arguments)
    assert result.exit_code == 0, result.output
    for game in GAME_IDS:
        label, amount = RESOURCE[game]
        assert label in result.output and amount in result.output
        acceptance_env.notes(game).assert_awaited_once_with(
            TEST_UIDS[game], autoauth=False, return_raw_data=True
        )
    assert result.output.count("Updated") == 1 and "STALE" not in result.output
    assert len(list(acceptance_env.cache.rglob("*.json"))) == 3
    acceptance_env.client.get_reward_info.assert_not_called()
    acceptance_env.client.claim_daily_reward.assert_not_called()


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("detailed", [False, True])
@pytest.mark.parametrize("command", [["genshin"], ["status", "genshin"]])
def test_genshin_expedition_times_are_combined_through_cli(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    width: int,
    detailed: bool,
    command: list[str],
) -> None:
    acceptance_env.width = width
    flags = ["--no-color", *(["--detailed"] if detailed else [])]
    result = runner.invoke(main, [*command, *flags])
    assert result.exit_code == 0, result.output
    assert result.stdout.count("Expedition 1") == 1
    assert result.stdout.count("Expedition 2") == 1
    assert "Expedition 1 at" not in result.output
    assert "Expedition 2 at" not in result.output
    assert "2 / 5" in result.stdout and "1 finished" in result.stdout
    local_finish = (acceptance_env.now + timedelta(hours=2, minutes=14)).strftime(
        "%H:%M"
    )
    assert any(
        "Expedition 2" in line and "2h 14m" in line and local_finish in line
        for line in result.stdout.splitlines()
    )
    assert "Finished" in result.stdout
    assert not result.stderr and "\x1b" not in result.output
    assert all(cell_len(line) <= width for line in result.output.splitlines())
    acceptance_env.notes("genshin").assert_awaited_once_with(
        TEST_UIDS["genshin"], autoauth=False, return_raw_data=True
    )
    acceptance_env.notes("hsr").assert_not_called()
    acceptance_env.notes("zzz").assert_not_called()
    acceptance_env.client.get_reward_info.assert_not_called()
    acceptance_env.client.claim_daily_reward.assert_not_called()


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("detailed", [False, True])
def test_daily_state_hierarchy_remains_clear_without_color(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    width: int,
    detailed: bool,
) -> None:
    acceptance_env.width = width
    genshin_payload = cast(
        dict[str, object], acceptance_env.notes("genshin").return_value
    )
    genshin_payload.update(
        current_resin=200,
        resin_recovery_time="0",
        finished_task_num=2,
        is_extra_task_reward_received=False,
    )
    hsr_payload = cast(dict[str, object], acceptance_env.notes("hsr").return_value)
    hsr_payload["expeditions"] = [
        {"name": "Finished assignment", "remaining_time": 0, "status": "Finished"}
    ]
    zzz_payload = cast(dict[str, object], acceptance_env.notes("zzz").return_value)
    zzz_payload["member_card"] = {
        "member_card_state": "MemberCardStateNo",
        "exp_time": "0",
        "is_open": False,
    }
    zzz_payload["bounty_commission"] = {
        "num": 0,
        "total": 8000,
        "refresh_time": 86400,
        "unlock": True,
    }
    zzz_payload.pop("survey_points")
    result = runner.invoke(main, ["--no-color", *(["--detailed"] if detailed else [])])
    assert result.exit_code == 0, result.output
    for text in (
        "200 / 200",
        "Full",
        "2 / 4",
        "Reward unclaimed",
        "finished",
        "400 / 400",
        "Done",
        "Inactive",
        "0 / 8000",
    ):
        assert text in result.output
    hollow = result.output[
        result.output.index("Hollow Zero") : result.output.index("Weekly Tasks")
    ]
    assert hollow.count("0 / 8000") == 1
    assert "HoYo Daily · main" in result.output and "Updated" in result.output
    assert "Investigation Unavailable" not in result.output
    assert "\x1b" not in result.output and "…" not in result.output
    assert all(cell_len(line) <= width for line in result.output.splitlines())
    acceptance_env.client.get_reward_info.assert_not_called()
    acceptance_env.client.claim_daily_reward.assert_not_called()


@pytest.mark.parametrize("game", GAME_IDS)
@pytest.mark.parametrize("age", [timedelta(minutes=34), timedelta(hours=3)])
def test_one_stale_fallback(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    game: GameId,
    age: timedelta,
) -> None:
    assert runner.invoke(main, []).exit_code == 0
    before = {p: p.read_bytes() for p in acceptance_env.cache.rglob("*.json")}
    acceptance_env.now += age
    acceptance_env.notes(game).side_effect = TimeoutError("acceptance-secret-not-real")
    result = runner.invoke(main, [])
    assert result.exit_code == 0, result.output
    assert result.output.count("STALE · fetched") == 1
    assert ("34m ago" if age < timedelta(hours=3) else "3h ago") in result.output
    assert "timed out" in result.output and "Updated" not in result.output
    assert "acceptance-secret-not-real" not in result.output
    for label, amount in RESOURCE.values():
        assert label in result.output and amount in result.output
    cached = next(p for p in before if p.stem == game)
    assert cached.read_bytes() == before[cached]
    assert all(
        p.read_bytes() != content for p, content in before.items() if p != cached
    )
    assert (
        acceptance_env.notes(game).await_count == 3
    )  # initial read plus bounded retry


@pytest.mark.parametrize("game", GAME_IDS)
def test_one_unavailable_with_two_fresh(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment, game: GameId
) -> None:
    acceptance_env.notes(game).side_effect = TimeoutError()
    result = runner.invoke(main, [])
    assert result.exit_code == 0, result.output
    assert result.output.count("No eligible cached status available") == 1
    assert "timed out" in result.output and "STALE" not in result.output
    for selected, (label, amount) in RESOURCE.items():
        assert (label in result.output) == (selected != game)
        assert (amount in result.output) == (selected != game)
    assert {p.stem for p in acceptance_env.cache.rglob("*.json")} == set(GAME_IDS) - {
        game
    }


@pytest.mark.parametrize(
    "failure", [TimeoutError(), RuntimeError("acceptance-secret-not-real")]
)
def test_all_fail_nonzero(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment, failure: Exception
) -> None:
    for game in GAME_IDS:
        acceptance_env.notes(game).side_effect = failure
    result = runner.invoke(main, [])
    assert result.exit_code == 1
    assert result.output.count("Unable to fetch fresh status") == 3
    assert result.output.count("No eligible cached status available") == 3
    assert (
        "Traceback" not in result.output
        and "acceptance-secret-not-real" not in result.output
    )
    assert not list(acceptance_env.cache.rglob("*.json"))


@pytest.mark.parametrize("width", [100, 80, 79, 70, 60, 59, 48])
@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--detailed"],
        ["genshin"],
        ["genshin", "--detailed"],
        ["hsr"],
        ["hsr", "--detailed"],
        ["zzz"],
        ["zzz", "--detailed"],
    ],
)
def test_readable_width_thresholds(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    width: int,
    arguments: list[str],
) -> None:
    acceptance_env.width = width
    result = runner.invoke(main, arguments)
    assert result.exit_code == 0, result.output
    assert all(cell_len(line) <= width for line in result.output.splitlines())
    assert "…" not in result.output and "\r" not in result.output
    for game, (label, amount) in RESOURCE.items():
        if not arguments or arguments[0] == "--detailed" or arguments[0] == game:
            assert label in result.output and amount in result.output
    if not arguments or arguments == ["--detailed"] or arguments == ["genshin"]:
        lines = result.output.splitlines()
        index = next(i for i, line in enumerate(lines) if line.startswith("Resin"))
        if width < 60:
            assert lines[index] == "Resin"
            assert lines[index + 1].strip() == "190 / 200"
            assert lines[index + 2].strip().startswith("Full in")
        elif width < 80:
            assert "190 / 200" in lines[index] and "Full in" not in lines[index]
            assert lines[index + 1].strip().startswith("Full in")
        else:
            assert "190 / 200" in lines[index] and "Full in" in lines[index]


@pytest.mark.parametrize("mode", ["redirected", "no-color", "NO_COLOR"])
def test_unstyled_output(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    text = acceptance_env.config.read_text().replace(
        'color = "auto"', 'color = "always"'
    )
    acceptance_env.config.write_text(text)
    assert load_config(acceptance_env.config).display_color == "always"
    acceptance_env.interactive = mode != "redirected"
    if mode == "NO_COLOR":
        monkeypatch.setenv("NO_COLOR", "1")
    result = runner.invoke(
        main, ["--no-color"] if mode == "no-color" else [], color=True
    )
    assert result.exit_code == 0, result.output
    assert "Resin" in result.output and "Updated" in result.output
    assert "\x1b" not in result.output and "\r" not in result.output


@pytest.mark.parametrize("source", ["manual", "browser"])
def test_commented_config_survives_init(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment, source: str
) -> None:
    original = acceptance_env.config.read_text()
    alt = load_config(acceptance_env.config).accounts["alt"]
    acceptance_env.config.write_text(
        '# Personal settings\ncustom = "retain-me" # unrelated\n' + original
    )
    acceptance_env.discovery.discover.return_value = {
        game: (CharacterRef(game, uid + 1, "test", f"Updated {game}"),)
        for game, uid in TEST_UIDS.items()
    }
    # Replace consent, choose session source, three character/enabled
    # choices, default consent, keep order, backend, optional plaintext consent, save.
    backend_answers = "1\n" if acceptance_env.backend == "keyring" else "2\ny\n"
    session_answers = "n\n" + TEST_COOKIE + "\n"
    if source == "browser":
        acceptance_env.browser.list_sessions.return_value = (
            BrowserSession("edge", "Edge"),
            BrowserSession("chrome", "Chrome"),
        )
        acceptance_env.browser.import_session.return_value = parse_cookie_header(
            TEST_COOKIE
        )
        session_answers = "y\n2\n"
    answers = (
        "y\n" + session_answers + "1\ny\n1\ny\n1\ny\ny\ny\n" + backend_answers + "y\n"
    )
    result = runner.invoke(main, ["init", "--account", "main"], input=answers)
    assert result.exit_code == 0, result.output
    after = acceptance_env.config.read_text()
    assert '# Personal settings\ncustom = "retain-me" # unrelated\n' in after
    assert load_config(acceptance_env.config).accounts["alt"] == alt
    assert (
        load_config(acceptance_env.config).accounts["main"].games["genshin"].uid
        == TEST_UIDS["genshin"] + 1
    )
    assert "acceptance-secret-not-real" not in result.output
    if source == "browser":
        acceptance_env.browser.import_session.assert_called_once_with("chrome")
    else:
        acceptance_env.browser.list_sessions.assert_not_called()


def test_config_never_reads_or_displays_secrets(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment
) -> None:
    acceptance_env.config.write_text(
        'private_note = "acceptance-secret-not-real"\n'
        + acceptance_env.config.read_text()
    )
    acceptance_env.keyring.get_password.side_effect = AssertionError(
        "Inspection must not read secrets"
    )
    acceptance_env.credentials.write_text(
        "Malformed private store: acceptance-secret-not-real"
    )
    result = runner.invoke(main, ["config"])
    assert result.exit_code == 0, result.output
    assert acceptance_env.backend in result.output
    assert (
        "acceptance-secret-not-real" not in result.output
        and "ltoken" not in result.output
    )
    acceptance_env.keyring.get_password.assert_not_called()
    acceptance_env.client.get_genshin_notes.assert_not_called()


@pytest.mark.parametrize("claimed", GAME_IDS)
def test_already_claimed_checkin_skips_mutation(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment, claimed: GameId
) -> None:
    games = {
        "genshin": genshin.Game.GENSHIN,
        "hsr": genshin.Game.STARRAIL,
        "zzz": genshin.Game.ZZZ,
    }

    async def state(*, game: genshin.Game) -> genshin.models.DailyRewardInfo:
        return genshin.models.DailyRewardInfo(
            signed_in=game == games[claimed], claimed_rewards=1
        )

    acceptance_env.client.get_reward_info.side_effect = state
    result = runner.invoke(main, ["check-in"])
    assert result.exit_code == 0, result.output
    assert (
        result.output.count("Already checked in") == 1
        and result.output.count("Claimed") == 2
    )
    assert acceptance_env.client.claim_daily_reward.await_args_list == [
        call(game=games[game], reward=False) for game in GAME_IDS if game != claimed
    ]
    assert "Resin" not in result.output and "Updated" not in result.output
    assert not acceptance_env.cache.exists()


@pytest.mark.parametrize("game", GAME_IDS)
@pytest.mark.parametrize(
    "flags", [[], ["--detailed"], ["--account", "alt", "--no-color"]]
)
def test_alias_equivalence(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    game: GameId,
    flags: list[str],
) -> None:
    alias = runner.invoke(main, [game, *flags])
    explicit = runner.invoke(main, ["status", game, *flags])
    assert alias.exit_code == explicit.exit_code == 0
    assert alias.output == explicit.output
    assert RESOURCE[game][1] in alias.output
    assert acceptance_env.notes(game).await_count == 2


def test_plaintext_fallback_is_separate(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment
) -> None:
    account = load_config(acceptance_env.config).accounts["main"]
    save_account(
        acceptance_env.config,
        "main",
        AccountConfig("plaintext", account.games),
        make_default=True,
        game_order=GAME_IDS,
    )
    PlaintextStore(acceptance_env.credentials).put(
        "main", parse_cookie_header(TEST_COOKIE)
    )
    result = runner.invoke(main, [])
    assert result.exit_code == 0, result.output
    assert "acceptance-secret-not-real" in acceptance_env.credentials.read_text()
    assert "acceptance-secret-not-real" not in acceptance_env.config.read_text()
    assert "ltoken" not in acceptance_env.config.read_text()
    assert "acceptance-secret-not-real" not in result.output
    for path in acceptance_env.cache.rglob("*.json"):
        assert "acceptance-secret-not-real" not in path.read_text()


def test_readme_links_to_user_guides() -> None:
    root = Path(__file__).parents[1]
    documents = (
        "README.md",
        "docs/setup.md",
        "docs/commands.md",
        "docs/configuration.md",
        "docs/development.md",
    )
    readme = (root / "README.md").read_text(encoding="utf-8")
    readme_links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", readme)
    for guide in documents[1:]:
        assert guide in readme_links, f"Missing guide link: {guide}"
    for document in documents:
        path = root / document
        contents = path.read_text(encoding="utf-8")
        for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", contents):
            if "://" in link or link.startswith("#"):
                continue
            target = link.partition("#")[0]
            assert (path.parent / target).is_file(), (
                f"Broken link in {document}: {link}"
            )


@pytest.mark.parametrize(
    ("document", "commands"),
    [
        ("README.md", ("uv tool install .", "hoyo init", "hoyo genshin --detailed")),
        ("docs/setup.md", ("hoyo init", "hoyo --account alt")),
        (
            "docs/commands.md",
            ("hoyo check-in zzz", "hoyo config --edit", "uv tool uninstall hoyo-cli"),
        ),
        ("docs/configuration.md", ("hoyo config", "hoyo config --edit")),
        ("docs/development.md", ("uv sync", "uv run hoyo --help", "--live-read-only")),
    ],
)
def test_user_documentation_covers_commands(
    document: str, commands: tuple[str, ...]
) -> None:
    contents = (Path(__file__).parents[1] / document).read_text(encoding="utf-8")
    for command in commands:
        assert command in contents, (
            f"Missing documented command in {document}: {command}"
        )


def test_live_test_is_opt_in() -> None:
    root = Path(__file__).parents[1]
    assert (root / "tests/live/test_read_only.py").is_file()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/live/" not in result.stdout.replace("\\", "/")
    opted_in = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--live-read-only",
            "-m",
            "live_read_only",
            "tests/live",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert opted_in.returncode == 0, opted_in.stdout + opted_in.stderr
    assert "test_configured_account_status_is_read_only" in opted_in.stdout
    # An explicit file selector must still be harmless without the flag.
    direct = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/live/test_read_only.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert direct.returncode == 0, direct.stdout + direct.stderr
    assert "1 skipped" in direct.stdout
