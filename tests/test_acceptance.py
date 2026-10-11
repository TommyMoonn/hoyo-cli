"""Spec acceptance through the CLI, with only external boundaries replaced."""

import asyncio
import io
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import cast
from unittest.mock import call, patch

import genshin
import pytest
from click.testing import CliRunner
from conftest import TEST_COOKIE, TEST_UIDS, AcceptanceEnvironment
from rich.cells import cell_len

from hoyo_cli.auth.browser_import import BrowserProfile, BrowserSession, browser_error
from hoyo_cli.auth.credentials import parse_cookie_header
from hoyo_cli.auth.plaintext_backend import PlaintextStore
from hoyo_cli.cli import main
from hoyo_cli.config.models import AccountConfig, GameSelection
from hoyo_cli.config.repository import load_config, save_account
from hoyo_cli.errors import ErrorCode
from hoyo_cli.hoyolab.discovery import CharacterRef
from hoyo_cli.models.common import GAME_IDS, GameId
from hoyo_cli.render import progress


@pytest.mark.parametrize("command", [[], ["status"], ["genshin"], ["check-in"]])
def test_persisted_default_and_invocation_override_remain_account_isolated(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    command: list[str],
) -> None:
    env = acceptance_env
    alternate_uid = 999999999
    save_account(
        env.config,
        "alt",
        AccountConfig(env.backend, {"genshin": GameSelection(True, alternate_uid)}),
        make_default=False,
        game_order=GAME_IDS,
    )
    original = env.config.read_bytes()
    secret_bytes = env.credentials.read_bytes() if env.credentials.exists() else None
    with patch.object(PlaintextStore, "get") as plaintext_get:
        listing = runner.invoke(main, ["account"])
        switched = runner.invoke(main, ["account", "use", "alt"])
    assert listing.exit_code == switched.exit_code == 0, switched.output
    plaintext_get.assert_not_called()
    env.keyring.get_password.assert_not_called()
    env.keyring.set_password.assert_not_called()
    env.keyring.delete_password.assert_not_called()
    env.client.get_reward_info.assert_not_called()
    env.client.claim_daily_reward.assert_not_called()
    for game in GAME_IDS:
        env.notes(game).assert_not_called()
    assert env.config.read_bytes() == original.replace(
        b'default_account = "main"', b'default_account = "alt"', 1
    )
    persisted = env.config.read_bytes()
    for requested, expected_uid in (
        (None, alternate_uid),
        ("main", TEST_UIDS["genshin"]),
        (None, alternate_uid),
    ):
        env.client.reset_mock()
        arguments = [*(["--account", requested] if requested else []), *command]
        result = runner.invoke(main, arguments)
        assert result.exit_code == 0, result.output
        if command == ["check-in"]:
            assert env.client.uids[genshin.Game.GENSHIN] == expected_uid
            env.client.claim_daily_reward.assert_any_await(
                game=genshin.Game.GENSHIN, reward=False
            )
            expected_claims = 3 if requested else 1
            assert env.client.claim_daily_reward.await_count == expected_claims
        else:
            env.notes("genshin").assert_awaited_once_with(
                expected_uid, autoauth=False, return_raw_data=True
            )
            env.client.claim_daily_reward.assert_not_called()
        assert env.config.read_bytes() == persisted
        assert load_config(env.config).default_account == "alt"
    assert (
        env.credentials.read_bytes() if env.credentials.exists() else None
    ) == secret_bytes
    env.browser.list_sessions.assert_not_called()
    env.browser.import_session.assert_not_called()
    env.discovery.discover.assert_not_called()


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


def assert_safe_output(output: str, *, width: int | None = None) -> None:
    assert all(
        text not in output
        for text in ("acceptance-secret-not-real", "Traceback", "\x1b", "\r")
    )
    if width is not None:
        assert all(cell_len(line) <= width for line in output.splitlines())


@pytest.mark.parametrize("width", [48, 70, 100])
def test_mixed_fresh_stale_and_unavailable_journey(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment, width: int
) -> None:
    env = acceptance_env
    env.width = width
    assert runner.invoke(main, ["hsr"]).exit_code == 0
    cached = next(env.cache.rglob("hsr.json"))
    before = cached.read_bytes()
    env.now += timedelta(hours=3)
    env.notes("hsr").side_effect = TimeoutError("acceptance-secret-not-real")
    env.notes("zzz").side_effect = RuntimeError("acceptance-secret-not-real")
    result = runner.invoke(main, ["--no-color"])
    assert result.exit_code == 0, result.output
    assert "190 / 200" in result.stdout and "211 / 300" in result.stdout
    assert "89 / 240" not in result.stdout
    assert result.stdout.count("STALE · fetched 3h ago") == 1
    assert result.stdout.count("No eligible cached status available") == 1
    hsr_section = result.stdout.split("HONKAI: STAR RAIL")[1].split("ZENLESS")[0]
    assert "Snapshot estimate" in hsr_section
    assert "Full in" not in hsr_section
    assert "Updated" not in result.stdout
    assert not result.stderr
    assert_safe_output(result.output, width=width)
    assert cached.read_bytes() == before
    assert {path.stem for path in env.cache.rglob("*.json")} == {"genshin", "hsr"}
    env.client.get_reward_info.assert_not_called()
    env.client.claim_daily_reward.assert_not_called()


@pytest.mark.parametrize("isolation", ["account", "uid"])
def test_cached_identity_cannot_cross_command_selection(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment, isolation: str
) -> None:
    env = acceptance_env
    assert runner.invoke(main, ["genshin"]).exit_code == 0
    before = {path: path.read_bytes() for path in env.cache.rglob("*.json")}
    arguments = ["genshin"]
    if isolation == "account":
        arguments += ["--account", "alt"]
    else:
        config = load_config(env.config)
        games = dict(config.accounts["main"].games)
        games["genshin"] = GameSelection(True, TEST_UIDS["genshin"] + 1)
        save_account(
            env.config,
            "main",
            AccountConfig(env.backend, games),
            make_default=True,
            game_order=GAME_IDS,
        )
    env.notes("genshin").side_effect = TimeoutError("acceptance-secret-not-real")
    result = runner.invoke(main, arguments)
    assert result.exit_code == 1, result.output
    assert "No eligible cached status available" in result.stdout
    assert "190 / 200" not in result.stdout and "STALE" not in result.stdout
    assert {path: path.read_bytes() for path in env.cache.rglob("*.json")} == before
    env.client.claim_daily_reward.assert_not_called()
    assert_safe_output(result.output)


def test_unconfigured_first_run_never_accesses_secrets_or_network(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment
) -> None:
    env = acceptance_env
    env.config.unlink()
    result = runner.invoke(main, [])
    assert result.exit_code == 2, result.output
    assert "Run: hoyo init" in result.stderr
    assert result.stdout == ""
    env.keyring.get_password.assert_not_called()
    assert env.client.mock_calls == []
    assert not env.cache.exists()
    assert_safe_output(result.output)


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("save", [True, False])
def test_new_account_selection_review_and_cancel_journey(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment, width: int, save: bool
) -> None:
    env = acceptance_env
    env.width = width
    original = env.config.read_bytes()
    secrets = env.credentials.read_bytes() if env.credentials.exists() else None
    env.discovery.discover.return_value = {
        game: (
            CharacterRef(game, uid, "os_asia", f"First {game} / Asia"),
            CharacterRef(game, uid + 1, "os_usa", f"Second {game} / America"),
        )
        for game, uid in TEST_UIDS.items()
    }
    backend = "1\n" if env.backend == "keyring" else "2\ny\n"
    # Create account, manual session, second Genshin/first HSR, skip ZZZ,
    # retain default account/order, storage consent, final Save/Cancel.
    answers = (
        "3\njourney\nn\n"
        + TEST_COOKIE
        + "\n2\n1\n3\nn\ny\n"
        + backend
        + ("1\n" if save else "2\n")
    )
    result = runner.invoke(main, ["init", "--no-color"], input=answers)
    assert result.exit_code == (0 if save else 1), result.output
    assert "Step 4/4" in result.stdout and "Review" in result.stdout
    assert "Second genshin / America" in result.stdout
    assert "First hsr / Asia" in result.stdout
    assert "Skip this game" in result.stdout
    assert "Enable game?" not in result.stdout
    env.discovery.discover.assert_called_once_with(parse_cookie_header(TEST_COOKIE))
    env.browser.list_sessions.assert_not_called()
    env.browser.import_session.assert_not_called()
    assert_safe_output(result.output)
    if save:
        config = load_config(env.config)
        assert config.default_account == "main"
        assert config.accounts["journey"].games == {
            "genshin": GameSelection(True, TEST_UIDS["genshin"] + 1),
            "hsr": GameSelection(True, TEST_UIDS["hsr"]),
        }
        assert "Configured account journey" in result.stdout
    else:
        assert env.config.read_bytes() == original
        assert (
            env.credentials.read_bytes() if env.credentials.exists() else None
        ) == secrets
        env.keyring.set_password.assert_not_called()
        env.keyring.delete_password.assert_not_called()
        assert "Setup cancelled; no changes saved" in result.stdout
        assert "Configured account" not in result.stdout
    assert env.client.mock_calls == []


@pytest.mark.parametrize("save", [True, False])
def test_settings_only_journey_with_unavailable_secrets(
    runner: CliRunner, acceptance_env: AcceptanceEnvironment, save: bool
) -> None:
    env = acceptance_env
    before = env.config.read_bytes()
    env.keyring.get_password.side_effect = AssertionError("Settings read keyring")
    env.credentials.write_text("Invalid store: acceptance-secret-not-real")
    stored = env.credentials.read_bytes()
    result = runner.invoke(
        main,
        ["init", "--account", "main", "--no-color"],
        input="2\nn\n3\n2\n1\n" + ("1\n" if save else "2\n"),
    )
    assert result.exit_code == (0 if save else 1), result.output
    assert "Review changes" in result.stdout
    if save:
        assert load_config(env.config).game_order == ("zzz", "hsr", "genshin")
    else:
        assert env.config.read_bytes() == before
    assert env.credentials.read_bytes() == stored
    env.keyring.get_password.assert_not_called()
    env.keyring.set_password.assert_not_called()
    env.keyring.delete_password.assert_not_called()
    assert env.browser.mock_calls == [] and env.discovery.mock_calls == []
    assert env.client.mock_calls == []
    assert_safe_output(result.output)


@pytest.mark.parametrize(
    "code, message",
    [
        (ErrorCode.BROWSER_LOCKED, "locked"),
        (ErrorCode.BROWSER_LOGIN_MISSING, "No complete HoYoLAB login"),
    ],
)
@pytest.mark.parametrize("save", [True, False])
def test_browser_recovery_manual_fallback_through_setup(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    code: ErrorCode,
    message: str,
    save: bool,
) -> None:
    env = acceptance_env
    before = env.config.read_bytes()
    secrets = env.credentials.read_bytes() if env.credentials.exists() else None
    env.browser.list_sessions.return_value = (BrowserSession("brave", "Brave", True),)
    env.browser.list_profiles.return_value = (
        BrowserProfile("brave:0", "brave", "Default"),
        BrowserProfile("brave:1", "brave", "Profile 2"),
    )
    env.browser.import_session.side_effect = browser_error(code)
    # Explicit session replacement, Brave/Profile 2, recover with manual input.
    result = runner.invoke(
        main,
        ["init", "--account", "main", "--no-color"],
        input="4\ny\ny\n1\n2\n3\n"
        + TEST_COOKIE
        + "\n1\n1\n1\n"
        + ("1\n" if save else "2\n"),
    )
    assert result.exit_code == (0 if save else 1), result.output
    assert message in result.stderr
    assert (
        "Browser recovery" in result.stdout and "Paste Cookie header" in result.stdout
    )
    assert "Session verified" in result.stdout
    env.browser.import_session.assert_called_once_with("brave:1")
    env.discovery.discover.assert_called_once_with(parse_cookie_header(TEST_COOKIE))
    if not save:
        assert env.config.read_bytes() == before
        assert (
            env.credentials.read_bytes() if env.credentials.exists() else None
        ) == secrets
        env.keyring.set_password.assert_not_called()
        env.keyring.delete_password.assert_not_called()
    assert env.client.mock_calls == []
    assert_safe_output(result.output)


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize(
    "mode", ["tty", "stdout-pipe", "stderr-pipe", "NO_COLOR", "no-color", "CI", "dumb"]
)
def test_slow_three_game_command_terminal_matrix(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    mode: str,
) -> None:
    env = acceptance_env
    env.width = width
    env.interactive = mode != "stdout-pipe"
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "dumb" if mode == "dumb" else "xterm-256color")
    if mode in ("NO_COLOR", "CI"):
        monkeypatch.setenv(mode, "")
    monkeypatch.setattr(progress, "PRESENTATION_DELAY", 0.005)
    started: set[GameId] = set()

    def slow_read(game: GameId, delay: float):
        payload = env.notes(game).return_value

        async def read(uid: int, *, autoauth: bool, return_raw_data: bool) -> object:
            started.add(game)
            await asyncio.sleep(delay)
            assert started == set(GAME_IDS), "Status requests must run concurrently"
            if game == "zzz":
                raise RuntimeError("acceptance-secret-not-real")
            return payload

        return read

    for game, delay in zip(GAME_IDS, (0.025, 0.05, 0.075), strict=True):
        env.notes(game).side_effect = slow_read(game, delay)

    def isatty(stream: io.TextIOWrapper) -> bool:
        return stream.name != (
            "<stdout>"
            if mode == "stdout-pipe"
            else "<stderr>"
            if mode == "stderr-pipe"
            else ""
        )

    with patch("click.testing._NamedTextIOWrapper.isatty", isatty):
        result = runner.invoke(main, ["--no-color"] if mode == "no-color" else [])
    assert result.exit_code == 0, result.output
    assert "190 / 200" in result.stdout and "211 / 300" in result.stdout
    assert "No eligible cached status available" in result.stdout
    assert "Fetching status" not in result.stdout
    assert "acceptance-secret-not-real" not in result.output
    plain_stdout = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", result.stdout)
    assert_safe_output(plain_stdout, width=width)
    if mode == "tty":
        for count in ("0/3", "1/3", "2/3", "3/3"):
            assert count in result.stderr
        assert "Fetching status" in result.stderr and "%" not in result.stderr
        assert result.stderr.endswith("\x1b[2K")
        assert "\x1b[?25h" in result.stderr
    else:
        assert result.stderr == ""
    if mode in ("stdout-pipe", "NO_COLOR", "no-color", "dumb"):
        assert_safe_output(result.stdout, width=width)
    env.client.claim_daily_reward.assert_not_called()


@pytest.mark.parametrize("command", [[], ["check-in"]])
@pytest.mark.parametrize("signal", [KeyboardInterrupt, asyncio.CancelledError])
def test_abrupt_gateway_cancel_cleans_terminal_and_preserves_stores(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    signal: type[BaseException],
) -> None:
    env = acceptance_env
    before = env.config.read_bytes()
    secrets = env.credentials.read_bytes() if env.credentials.exists() else None
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(progress, "PRESENTATION_DELAY", 0.005)

    async def cancel(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0.025)
        raise signal()

    env.client.get_reward_info.side_effect = cancel
    for game in GAME_IDS:
        env.notes(game).side_effect = cancel
    with patch("click.testing._NamedTextIOWrapper.isatty", return_value=True):
        result = runner.invoke(main, command)
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "\x1b[?25h" in result.stderr
    assert result.stderr.rfind("\x1b[2K") < result.stderr.index(
        "cancelled" if signal is asyncio.CancelledError else "Aborted!"
    )
    assert env.config.read_bytes() == before
    assert (
        env.credentials.read_bytes() if env.credentials.exists() else None
    ) == secrets
    assert not list(env.cache.rglob("*.json"))
    env.keyring.set_password.assert_not_called()
    env.keyring.delete_password.assert_not_called()
    env.client.claim_daily_reward.assert_not_called()


@pytest.mark.parametrize("target", [None, *GAME_IDS])
@pytest.mark.parametrize("already_claimed", [True, False])
def test_explicit_check_in_scope_and_all_claimed_skip(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    target: GameId | None,
    already_claimed: bool,
) -> None:
    env = acceptance_env
    env.client.get_reward_info.return_value = genshin.models.DailyRewardInfo(
        signed_in=already_claimed, claimed_rewards=1 if already_claimed else 0
    )
    games = {
        "genshin": genshin.Game.GENSHIN,
        "hsr": genshin.Game.STARRAIL,
        "zzz": genshin.Game.ZZZ,
    }
    requested = (target,) if target else GAME_IDS
    result = runner.invoke(main, ["check-in", *([target] if target else [])])
    assert result.exit_code == 0, result.output
    assert result.stdout.count(
        "Already checked in" if already_claimed else "Claimed"
    ) == len(requested)
    assert env.client.get_reward_info.await_args_list == [
        call(game=games[game]) for game in requested
    ]
    assert env.client.claim_daily_reward.await_args_list == (
        []
        if already_claimed
        else [call(game=games[game], reward=False) for game in requested]
    )
    for game in GAME_IDS:
        env.notes(game).assert_not_called()
    assert not env.cache.exists()
    assert not result.stderr
    assert_safe_output(result.output)


@pytest.mark.parametrize("verified", [True, False])
def test_slow_ambiguous_check_in_shows_read_only_verification_stage(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    verified: bool,
) -> None:
    env = acceptance_env
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(progress, "PRESENTATION_DELAY", 0.005)
    reads = 0

    async def state(*, game: genshin.Game) -> genshin.models.DailyRewardInfo:
        nonlocal reads
        reads += 1
        await asyncio.sleep(0.025)
        return genshin.models.DailyRewardInfo(
            signed_in=verified and reads == 2, claimed_rewards=0
        )

    async def claim(*, game: genshin.Game, reward: bool) -> None:
        await asyncio.sleep(0.025)
        raise TimeoutError("acceptance-secret-not-real")

    env.client.get_reward_info.side_effect = state
    env.client.claim_daily_reward.side_effect = claim
    with patch("click.testing._NamedTextIOWrapper.isatty", return_value=True):
        result = runner.invoke(main, ["check-in", "hsr"])
    assert result.exit_code == (0 if verified else 1), result.output
    for label in ("Checking today's rewards", "Claiming", "Verifying"):
        assert label in result.stderr and label not in result.stdout
    assert "0/1" in result.stderr and "1/1" in result.stderr
    assert result.stderr.endswith("\x1b[2K") and "\x1b[?25h" in result.stderr
    assert ("Claimed (verified)" if verified else "Unconfirmed") in result.stdout
    assert_safe_output(result.stdout)
    assert reads == 2
    env.client.claim_daily_reward.assert_awaited_once_with(
        game=genshin.Game.STARRAIL, reward=False
    )


def test_mock_account_end_user_command_transcript(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
) -> None:
    env = acceptance_env
    env.client.get_reward_info.return_value = genshin.models.DailyRewardInfo(True, 1)
    commands: list[tuple[list[str], str | None]] = [
        (["--help"], None),
        ([], None),
        (["zzz"], None),
        (["check-in", "--help"], None),
        (["check-in", "hsr"], None),
        (["init"], "1\n2\ny\n1\n"),
        (["config"], None),
    ]
    for arguments, answers in commands:
        result = runner.invoke(main, arguments, input=answers)
        assert result.exit_code == 0, result.output
        assert_safe_output(result.output)
        print("$ hoyo " + " ".join(arguments))
        print(result.output.replace(str(env.home), "<test-home>"))
    env.client.claim_daily_reward.assert_not_called()
    env.browser.list_sessions.assert_not_called()
    env.browser.import_session.assert_not_called()
    env.discovery.discover.assert_not_called()


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
    # Session-update action, explicit replacement consent, source selection,
    # three characters, and Save. Backend/default/order are retained.
    session_answers = "n\n" + TEST_COOKIE + "\n"
    if source == "browser":
        acceptance_env.browser.list_sessions.return_value = (
            BrowserSession("edge", "Edge"),
            BrowserSession("chrome", "Chrome"),
        )
        acceptance_env.browser.import_session.return_value = parse_cookie_header(
            TEST_COOKIE
        )
        acceptance_env.browser.list_profiles.return_value = (
            BrowserProfile("chrome", "chrome", "Library-selected profile"),
        )
        session_answers = "y\n2\n"
    answers = "4\ny\n" + session_answers + "1\n1\n1\n1\n"
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
        acceptance_env.browser.list_profiles.assert_called_once_with("chrome")
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
