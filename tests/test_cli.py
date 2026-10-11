"""Behavioral coverage for the installed CLI and command grammar."""

import asyncio
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import pytest
from click.testing import CliRunner

from hoyo_cli.auth.credentials import Credentials, parse_cookie_header
from hoyo_cli.auth.plaintext_backend import PlaintextStore
from hoyo_cli.cli import main
from hoyo_cli.config.models import AccountConfig, GameSelection
from hoyo_cli.config.repository import load_config, save_account
from hoyo_cli.errors import NetworkError
from hoyo_cli.models.common import GameId
from hoyo_cli.models.progress import ProgressEvent


@pytest.mark.parametrize(
    "arguments", [[], ["status"], ["zzz"], ["check-in"], ["config"]]
)
def test_first_run_points_to_setup(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    from hoyo_cli.errors import ConfigError

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path / ".hoyo/config.toml")
    assert caught.value.code == "config_missing"
    assert not caught.value.retryable
    result = runner.invoke(main, arguments)
    assert result.exit_code == 2
    assert "Error:" in result.stderr and "Run: hoyo init" in result.stderr
    assert result.stderr.splitlines()[-1] == "Run: hoyo init"
    assert not result.stdout
    assert "Traceback" not in result.output and "\x1b" not in result.output


def test_missing_credentials_has_specific_category_and_setup_hint(
    runner: CliRunner,
    status_gateway: AsyncMock,
) -> None:
    from hoyo_cli.commands import CommandOptions, status
    from hoyo_cli.errors import CredentialError

    with patch.object(status, "open_store") as store:
        store.return_value.get.return_value = None
        with pytest.raises(CredentialError) as caught:
            status.run_status(options=CommandOptions(), game=None)
        result = runner.invoke(main, [])
    assert caught.value.code == "credentials_missing"
    assert not caught.value.retryable
    assert result.exit_code == 3
    assert result.stderr.splitlines()[-1] == "Run: hoyo init"
    status_gateway.fetch_status.assert_not_called()


def test_malformed_config_has_repair_hint_and_safe_summary(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hoyo_cli.errors import ConfigError

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".hoyo/config.toml"
    path.parent.mkdir()
    path.write_text('ltoken = "test-secret-123"\n[broken')
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert caught.value.code == "config_invalid"
    assert caught.value.hint and "configuration" in caught.value.hint.lower()
    result = runner.invoke(main, [])
    assert result.exit_code == 2
    assert "Try:" in result.stderr
    assert "test-secret-123" not in result.output and "Traceback" not in result.output


def test_unexpected_command_exception_is_sanitized(runner: CliRunner) -> None:
    with patch(
        "hoyo_cli.commands.config.run_config",
        side_effect=RuntimeError("ltoken=test-secret-123"),
    ):
        result = runner.invoke(main, ["config"])
    assert result.exit_code == 1
    assert "Error:" in result.stderr and "Try:" in result.stderr
    assert not result.stdout
    assert all(
        value not in result.output
        for value in ("test-secret-123", "RuntimeError", "Traceback", "\x1b", "locked")
    )


@pytest.mark.parametrize("command", ["status", "check-in"])
def test_known_boundary_error_uses_same_safe_format(
    runner: CliRunner,
    command: str,
) -> None:
    from hoyo_cli.errors import AuthenticationError

    error = AuthenticationError("HoYoLAB rejected the session.")
    handler = "run_status" if command == "status" else "run_check_in"
    module = command.replace("-", "_")
    with patch(f"hoyo_cli.commands.{module}.{handler}", side_effect=error):
        result = runner.invoke(main, [command])
    assert result.exit_code == 3
    assert result.stderr.startswith("Error: HoYoLAB rejected the session.\nTry: ")
    assert "hoyo init" in result.stderr and not result.stdout


def test_help_identifies_hoyo(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Usage: hoyo" in result.output


@pytest.mark.parametrize("command", [[], ["init"], ["config"]])
def test_help_connects_setup_settings_and_normal_commands(
    runner: CliRunner, command: list[str]
) -> None:
    result = runner.invoke(main, [*command, "--help"])
    assert result.exit_code == 0
    if command == ["init"]:
        for action in (
            "Games and characters",
            "Game order",
            "Default account",
            "Update HoYoLAB session",
            "Credential storage",
        ):
            assert action in result.output
    elif command == ["config"]:
        assert "hoyo init" in result.output
    else:
        for example in (
            "hoyo init",
            "hoyo genshin",
            "hoyo check-in zzz",
            "hoyo config",
        ):
            assert example in result.output


def test_version_uses_installed_metadata(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"hoyo, version {version('hoyo-cli')}"


@pytest.mark.parametrize(
    ("arguments", "game"),
    [
        ([], None),
        (["status"], None),
        (["genshin"], "genshin"),
        (["status", "genshin"], "genshin"),
        (["hsr"], "hsr"),
        (["status", "hsr"], "hsr"),
        (["zzz"], "zzz"),
        (["status", "zzz"], "zzz"),
    ],
)
def test_status_forms_share_handler(
    runner: CliRunner, arguments: list[str], game: str | None
) -> None:
    with patch("hoyo_cli.commands.status.run_status") as handler:
        result = runner.invoke(main, arguments)
    assert result.exit_code == 0, result.output
    handler.assert_called_once()
    assert handler.call_args.kwargs["game"] == game
    options = handler.call_args.kwargs["options"]
    assert options.account is None
    assert options.detailed is False
    assert options.no_color is False


@pytest.mark.parametrize("command", [[], ["status"], ["genshin"], ["status", "hsr"]])
@pytest.mark.parametrize("placement", ["root", "local", "both"])
def test_status_options_resolve_consistently(
    runner: CliRunner, command: list[str], placement: str
) -> None:
    flags = ["--account", "Alt.Exact", "--no-color", "--detailed"]
    arguments = flags + command if placement == "root" else command + flags
    if placement == "both":
        arguments = ["--account", "root", "--no-color", "--detailed"] + command
        arguments += ["--account", "Alt.Exact"]
    with patch("hoyo_cli.commands.status.run_status") as handler:
        result = runner.invoke(main, arguments)
    assert result.exit_code == 0, result.output
    handler.assert_called_once()
    options = handler.call_args.kwargs["options"]
    assert options.account == "Alt.Exact"
    assert options.no_color is True
    assert options.detailed is True


@pytest.mark.parametrize("command", ["status", "genshin", "hsr", "zzz"])
def test_root_account_survives_local_boolean_flags(
    runner: CliRunner, command: str
) -> None:
    with patch("hoyo_cli.commands.status.run_status") as handler:
        result = runner.invoke(
            main, ["--account", "root", command, "--detailed", "--no-color"]
        )
    assert result.exit_code == 0, result.output
    options = handler.call_args.kwargs["options"]
    assert options.account == "root"
    assert options.detailed is True
    assert options.no_color is True


@pytest.mark.parametrize(
    ("arguments", "handler_path", "extra"),
    [
        (["check-in"], "hoyo_cli.commands.check_in.run_check_in", {"game": None}),
        (
            ["check-in", "zzz"],
            "hoyo_cli.commands.check_in.run_check_in",
            {"game": "zzz"},
        ),
        (["init"], "hoyo_cli.commands.init.configure_account", {}),
        (["config"], "hoyo_cli.commands.config.run_config", {"edit": False}),
        (["config", "--edit"], "hoyo_cli.commands.config.run_config", {"edit": True}),
    ],
)
@pytest.mark.parametrize("placement", ["root", "local", "both"])
def test_other_commands_dispatch_options(
    runner: CliRunner,
    arguments: list[str],
    handler_path: str,
    extra: dict[str, object],
    placement: str,
) -> None:
    flags = ["--account", "Exact.Account", "--no-color"]
    if placement == "root":
        arguments = flags + arguments
    elif placement == "local":
        arguments = arguments + flags
    else:
        arguments = ["--account", "root", "--no-color"] + arguments
        arguments += ["--account", "Exact.Account"]
    with patch(handler_path) as handler:
        result = runner.invoke(main, arguments)
    assert result.exit_code == 0, result.output
    handler.assert_called_once()
    assert {
        key: value
        for key, value in handler.call_args.kwargs.items()
        if key != "options"
    } == extra
    options = handler.call_args.kwargs["options"]
    assert options.account == "Exact.Account"
    assert options.no_color is True
    assert options.detailed is False


@pytest.mark.parametrize(
    "command", ["", "status", "genshin", "hsr", "zzz", "check-in", "init", "config"]
)
def test_command_help_never_dispatches(runner: CliRunner, command: str) -> None:
    with (
        patch("hoyo_cli.commands.status.run_status") as status_handler,
        patch("hoyo_cli.commands.check_in.run_check_in") as check_in_handler,
        patch("hoyo_cli.commands.init.run_init") as init_handler,
        patch("hoyo_cli.commands.config.run_config") as config_handler,
    ):
        result = runner.invoke(main, ([command] if command else []) + ["--help"])
    assert result.exit_code == 0, result.output
    assert "--account" in result.output
    assert "--no-color" in result.output
    for handler in (status_handler, check_in_handler, init_handler, config_handler):
        handler.assert_not_called()


def test_root_help_lists_command_surface(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    for name in ("status", "genshin", "hsr", "zzz", "check-in", "init", "config"):
        assert name in result.output
    assert "--detailed" in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["check-in"],
        ["check-in", "genshin"],
    ],
)
def test_checkin_without_config_fails_cleanly(
    runner: CliRunner,
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = runner.invoke(main, arguments)
    assert result.exit_code == 2
    assert "configuration file not found" in result.output.lower()
    assert "Traceback" not in result.output
    assert "\x1b[" not in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["status", "unknown"],
        ["status", "Genshin"],
        ["check-in", "unknown"],
        ["unknown"],
        ["gen"],
        ["refresh"],
        ["watch"],
        ["notify"],
        ["--json"],
        ["--account"],
        ["status", "genshin", "hsr"],
        ["genshin", "zzz"],
        ["config", "--detailed"],
        ["check-in", "--detailed"],
    ],
)
def test_invalid_grammar_is_usage_error(
    runner: CliRunner, arguments: list[str]
) -> None:
    result = runner.invoke(main, arguments)
    assert result.exit_code == 2
    assert "Error:" in result.output
    assert "Traceback" not in result.output


@pytest.fixture
def status_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    import json

    from hoyo_cli.commands import status
    from hoyo_cli.hoyolab.adapters.genshin import normalize_genshin
    from hoyo_cli.hoyolab.adapters.hsr import normalize_hsr
    from hoyo_cli.hoyolab.adapters.zzz import normalize_zzz

    now = datetime(2026, 10, 10, 11, 31, tzinfo=UTC)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    account = AccountConfig(
        "plaintext",
        {
            "genshin": GameSelection(True, 111111111),
            "hsr": GameSelection(True, 222222222),
            "zzz": GameSelection(True, 333333333),
        },
    )
    for name in ("main", "alt"):
        save_account(
            tmp_path / ".hoyo/config.toml",
            name,
            account,
            make_default=name == "main",
            game_order=("zzz", "hsr", "genshin"),
        )
        PlaintextStore(tmp_path / ".hoyo/credentials.toml").put(
            name, parse_cookie_header("ltuid=123; ltoken=test-secret-123")
        )
    data = {
        game: normalize(
            json.loads(
                (Path(__file__).parent / f"fixtures/{game}_notes.json").read_text()
            ),
            now=now,
        )
        for game, normalize in (
            ("genshin", normalize_genshin),
            ("hsr", normalize_hsr),
            ("zzz", normalize_zzz),
        )
    }
    gateway = AsyncMock()

    async def fetch(game: GameId, uid: int):
        return data[game]

    gateway.fetch_status.side_effect = fetch

    def create_gateway(credentials: Credentials) -> AsyncMock:
        assert credentials.fields["ltoken"] == "test-secret-123"
        return gateway

    monkeypatch.setattr(status, "GenshinPyGateway", create_gateway)
    monkeypatch.setattr(status, "_now", lambda: now)
    monkeypatch.setattr(status, "cache_root", lambda: tmp_path / "cache")
    return gateway


def test_configured_game_order_is_preserved(
    runner: CliRunner, status_gateway: AsyncMock
) -> None:
    result = runner.invoke(main, [])
    assert result.exit_code == 0, result.output
    assert (
        result.output.index("ZENLESS ZONE ZERO")
        < result.output.index("HONKAI: STAR RAIL")
        < result.output.index("GENSHIN IMPACT")
    )
    assert result.output.count("Updated") == 1
    assert "Test assignment A" not in result.output
    assert "test-secret-123" not in result.output and "\x1b[" not in result.output
    assert [call.args for call in status_gateway.fetch_status.await_args_list] == [
        ("zzz", 333333333),
        ("hsr", 222222222),
        ("genshin", 111111111),
    ]
    status_gateway.check_in_state.assert_not_called()
    status_gateway.claim_check_in.assert_not_called()


@pytest.mark.parametrize("game", ["genshin", "hsr", "zzz"])
@pytest.mark.parametrize(
    "flags", [[], ["--detailed", "--no-color"], ["--account", "alt"]]
)
def test_alias_and_status_game_render_equivalently(
    runner: CliRunner, status_gateway: AsyncMock, game: str, flags: list[str]
) -> None:
    alias = runner.invoke(main, [game, *flags])
    explicit = runner.invoke(main, ["status", game, *flags])
    assert alias.exit_code == explicit.exit_code == 0
    assert alias.output == explicit.output
    if game == "hsr":
        assert "Test assignment A" in alias.output
    if flags == ["--account", "alt"]:
        assert "alt" in alias.output


def test_root_and_explicit_overview_render_equivalently(
    runner: CliRunner, status_gateway: AsyncMock
) -> None:
    assert runner.invoke(main, []).output == runner.invoke(main, ["status"]).output


def test_partial_failure_keeps_successful_output(
    runner: CliRunner, status_gateway: AsyncMock
) -> None:
    original = status_gateway.fetch_status.side_effect

    async def partial(game: GameId, uid: int):
        if game == "hsr":
            raise NetworkError("HoYoLAB request timed out.")
        return await original(game, uid)

    status_gateway.fetch_status.side_effect = partial
    result = runner.invoke(main, ["status"])
    assert (
        result.exit_code == 0
        and "Resin" in result.output
        and "Battery Charge" in result.output
    )
    assert "timed out" in result.output and "Trailblaze Power" not in result.output


def test_all_fail_without_cache_exits_1(
    runner: CliRunner, status_gateway: AsyncMock
) -> None:
    status_gateway.fetch_status.side_effect = RuntimeError("test-secret-123")
    failed = runner.invoke(main, [])
    assert failed.exit_code == 1
    assert failed.output.count("Unable to fetch fresh status") == 3
    assert "Traceback" not in failed.output and "test-secret-123" not in failed.output


@pytest.mark.parametrize("arguments", [[], ["hsr"], ["status", "hsr", "--detailed"]])
def test_eligible_stale_only_exits_0(
    runner: CliRunner, status_gateway: AsyncMock, arguments: list[str]
) -> None:
    assert runner.invoke(main, ["hsr"]).exit_code == 0
    status_gateway.fetch_status.reset_mock()
    status_gateway.fetch_status.side_effect = NetworkError("HoYoLAB request timed out.")
    result = runner.invoke(main, arguments)
    assert result.exit_code == 0, result.output
    assert "STALE · fetched 0m ago" in result.output
    assert "211 / 300" in result.output and "timed out" in result.output
    assert "Updated" not in result.output
    assert "Traceback" not in result.output and "\x1b[" not in result.output
    status_gateway.fetch_status.assert_awaited()
    status_gateway.claim_check_in.assert_not_called()


@pytest.mark.parametrize("case", ["corrupt", "expired", "future"])
def test_unusable_cache_exits_1_without_traceback(
    runner: CliRunner,
    status_gateway: AsyncMock,
    tmp_path: Path,
    case: str,
) -> None:
    import json
    from datetime import timedelta

    assert runner.invoke(main, ["hsr"]).exit_code == 0
    path = next((tmp_path / "cache").rglob("*.json"))
    if case == "corrupt":
        path.write_text("{broken")
    else:
        payload = json.loads(path.read_text())
        now = datetime(2026, 10, 10, 11, 31, tzinfo=UTC)
        offset = (
            timedelta(hours=-3, seconds=-1)
            if case == "expired"
            else timedelta(seconds=1)
        )
        payload["fetched_at"] = (now + offset).isoformat()
        path.write_text(json.dumps(payload))
    status_gateway.fetch_status.side_effect = NetworkError("HoYoLAB request timed out.")
    result = runner.invoke(main, ["hsr"])
    assert result.exit_code == 1
    assert "No eligible cached status available." in result.output
    assert "timed out" in result.output
    assert "Trailblaze Power" not in result.output and "STALE" not in result.output
    assert "Traceback" not in result.output


def test_cache_write_failure_keeps_cli_success(
    runner: CliRunner, status_gateway: AsyncMock
) -> None:
    with patch("hoyo_cli.services.status.write_cache", side_effect=PermissionError):
        result = runner.invoke(main, ["hsr"])
    assert result.exit_code == 0, result.output
    assert "211 / 300" in result.output and "Updated" in result.output
    assert "Unable to save status cache" in result.output
    assert "Unable to fetch" not in result.output and "STALE" not in result.output


@pytest.mark.parametrize("change_uid", [False, True])
def test_cached_status_requires_current_selected_uid(
    runner: CliRunner,
    status_gateway: AsyncMock,
    tmp_path: Path,
    change_uid: bool,
) -> None:
    assert runner.invoke(main, []).exit_code == 0
    assert runner.invoke(main, ["--account", "alt"]).exit_code == 0
    before = {path: path.read_bytes() for path in (tmp_path / "cache").rglob("*.json")}
    config_file = tmp_path / ".hoyo/config.toml"
    config = load_config(config_file)
    account = config.accounts["main"]
    selected_uid = account.games["genshin"].uid + int(change_uid)
    save_account(
        config_file,
        "main",
        AccountConfig(
            account.credential_backend,
            {**account.games, "genshin": GameSelection(True, selected_uid)},
        ),
        make_default=True,
        game_order=config.game_order,
    )
    status_gateway.fetch_status.reset_mock()
    status_gateway.fetch_status.side_effect = NetworkError("HoYoLAB request timed out.")
    result = runner.invoke(main, ["genshin"])
    status_gateway.fetch_status.assert_awaited_once_with("genshin", selected_uid)
    assert result.exit_code == (1 if change_uid else 0), result.output
    assert ("STALE" in result.output) == (not change_uid)
    assert ("190 / 200" in result.output) == (not change_uid)
    assert "timed out" in result.output and "Traceback" not in result.output
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize(
    "initial_age, elapsed, eligible",
    [
        (timedelta(hours=3), timedelta(microseconds=1), False),
        (timedelta(hours=3, seconds=-1), timedelta(seconds=1), True),
        (timedelta(hours=2, minutes=59), timedelta(seconds=30), True),
    ],
)
def test_cli_checks_cache_age_after_awaited_failed_request(
    runner: CliRunner,
    status_gateway: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_age: timedelta,
    elapsed: timedelta,
    eligible: bool,
) -> None:
    import json

    from hoyo_cli.commands import status

    start = datetime(2026, 10, 10, 11, 31, tzinfo=UTC)
    assert runner.invoke(main, ["genshin"]).exit_code == 0
    path = next((tmp_path / "cache").rglob("*.json"))
    payload = json.loads(path.read_text())
    payload["fetched_at"] = (start - initial_age).isoformat()
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    current = start
    monkeypatch.setattr(status, "_now", lambda: current)

    async def delayed_failure(game: GameId, uid: int) -> None:
        nonlocal current
        await asyncio.sleep(0)
        current += elapsed
        raise NetworkError("HoYoLAB request timed out.")

    status_gateway.fetch_status.side_effect = delayed_failure
    result = runner.invoke(main, ["genshin"])
    assert result.exit_code == (0 if eligible else 1), result.output
    assert ("STALE" in result.output) == eligible
    assert ("190 / 200" in result.output) == eligible
    if eligible:
        assert (
            "STALE · fetched 3h ago" in result.output or "2h 59m ago" in result.output
        )
    assert "timed out" in result.output and "Updated" not in result.output
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "case",
    [
        "disabled",
        "unconfigured",
        "no-enabled",
        "account",
        "missing-credentials",
        "malformed-credentials",
        "missing-config",
    ],
)
def test_status_preflight_failures_prevent_requests(
    runner: CliRunner, status_gateway: AsyncMock, tmp_path: Path, case: str
) -> None:
    from hoyo_cli.commands import status
    from hoyo_cli.errors import ConfigError, CredentialError

    arguments = ["hsr"]
    expected = 2
    if case in ("disabled", "unconfigured", "no-enabled"):
        games: dict[GameId, GameSelection] = {
            "genshin": GameSelection(case != "no-enabled", 111111111)
        }
        if case == "disabled":
            games["hsr"] = GameSelection(False, 222222222)
        save_account(
            tmp_path / ".hoyo/config.toml",
            "main",
            AccountConfig("plaintext", games),
            make_default=True,
            game_order=(),
        )
        if case == "no-enabled":
            arguments = []
    elif case == "account":
        arguments = ["--account", "ma"]
    elif case in ("missing-credentials", "malformed-credentials"):
        expected = 3
    with (
        patch.object(status, "open_store") as store,
        patch.object(status, "load_config", wraps=status.load_config) as loader,
    ):
        if case == "missing-credentials":
            store.return_value.get.return_value = None
        elif case == "malformed-credentials":
            store.side_effect = CredentialError("Invalid credential store.")
        elif case == "missing-config":
            loader.side_effect = ConfigError("Config missing; run hoyo init.")
        result = runner.invoke(main, arguments)
    assert result.exit_code == expected, result.output
    assert "Traceback" not in result.output and "test-secret-123" not in result.output
    status_gateway.fetch_status.assert_not_called()


@pytest.fixture
def check_in_gateway(
    status_gateway: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> AsyncMock:
    from hoyo_cli.commands import check_in
    from hoyo_cli.models.common import CheckInReceipt, CheckInState

    def create_gateway(credentials: Credentials) -> AsyncMock:
        return status_gateway

    monkeypatch.setattr(check_in, "GenshinPyGateway", create_gateway)
    status_gateway.check_in_state.return_value = CheckInState(False)
    status_gateway.claim_check_in.return_value = CheckInReceipt()
    return status_gateway


def test_checkin_all_prints_three_outcomes_without_confirmation_or_dashboard(
    runner: CliRunner, check_in_gateway: AsyncMock, tmp_path: Path
) -> None:
    from hoyo_cli.models.common import CheckInReceipt, CheckInState

    async def state(game: GameId, uid: int) -> CheckInState:
        return CheckInState(game == "hsr")

    async def claim(game: GameId, uid: int) -> CheckInReceipt:
        return CheckInReceipt("20 Primogems" if game == "genshin" else None)

    check_in_gateway.check_in_state.side_effect = state
    check_in_gateway.claim_check_in.side_effect = claim
    cache = tmp_path / "cache/untouched.json"
    cache.parent.mkdir()
    cache.write_bytes(b"prior cache bytes")
    with (
        patch("click.confirm", side_effect=AssertionError("No confirmation allowed")),
        patch("click.prompt", side_effect=AssertionError("No input allowed")),
        patch("hoyo_cli.cache.repository.read_eligible") as read,
        patch("hoyo_cli.cache.repository.write_cache") as write,
    ):
        result = runner.invoke(main, ["check-in", "--no-color"])
    assert result.exit_code == 0, result.output
    names = ["Zenless Zone Zero", "Honkai: Star Rail", "Genshin Impact"]
    assert [result.output.index(name) for name in names] == sorted(
        result.output.index(name) for name in names
    )
    assert all(result.output.count(name) == 1 for name in names)
    assert result.output.count("Claimed") == 2
    assert result.output.count("Already checked in") == 1
    assert result.output.count("20 Primogems") == 1
    assert "main" in result.output
    assert all(
        text not in result.output
        for text in (
            "Resin",
            "Trailblaze Power",
            "Battery Charge",
            "Updated",
            "STALE",
            "Traceback",
            "test-secret-123",
            "\x1b[",
        )
    )
    check_in_gateway.fetch_status.assert_not_called()
    assert check_in_gateway.check_in_state.await_args_list == [
        call("zzz", 333333333),
        call("hsr", 222222222),
        call("genshin", 111111111),
    ]
    assert check_in_gateway.claim_check_in.await_count == 2
    read.assert_not_called()
    write.assert_not_called()
    assert cache.read_bytes() == b"prior cache bytes"
    assert list(cache.parent.iterdir()) == [cache]


@pytest.mark.parametrize("placement", ["root", "local", "both"])
def test_checkin_zzz_scope_and_exact_account_override(
    runner: CliRunner, check_in_gateway: AsyncMock, tmp_path: Path, placement: str
) -> None:
    from hoyo_cli.commands import check_in

    save_account(
        tmp_path / ".hoyo/config.toml",
        "alt",
        AccountConfig("plaintext", {"zzz": GameSelection(True, 444444444)}),
        make_default=False,
        game_order=("zzz", "hsr", "genshin"),
    )
    PlaintextStore(tmp_path / ".hoyo/credentials.toml").put(
        "alt", parse_cookie_header("ltuid=456; ltoken=other-test-secret")
    )
    arguments = ["check-in", "zzz"]
    if placement == "root":
        arguments = ["--account", "alt", "--no-color"] + arguments
    elif placement == "local":
        arguments += ["--account", "alt", "--no-color"]
    else:
        arguments = ["--account", "main", "--no-color"] + arguments
        arguments += ["--account", "alt"]
    with patch.object(
        check_in, "GenshinPyGateway", return_value=check_in_gateway
    ) as make:
        result = runner.invoke(main, arguments)
    assert result.exit_code == 0, result.output
    assert make.call_args.args[0].fields["ltoken"] == "other-test-secret"
    check_in_gateway.check_in_state.assert_awaited_once_with("zzz", 444444444)
    check_in_gateway.claim_check_in.assert_awaited_once_with("zzz", 444444444)
    assert "alt" in result.output and "Zenless Zone Zero" in result.output
    assert (
        "Genshin Impact" not in result.output
        and "Honkai: Star Rail" not in result.output
    )
    assert "other-test-secret" not in result.output and "\x1b[" not in result.output


@pytest.mark.parametrize(
    "failure", [NetworkError("Request timed out."), RuntimeError("test-secret-123")]
)
def test_checkin_partial_failure_is_nonzero_but_prints_successes(
    runner: CliRunner, check_in_gateway: AsyncMock, failure: Exception
) -> None:
    from hoyo_cli.models.common import CheckInReceipt

    async def claim(game: GameId, uid: int) -> CheckInReceipt:
        if game == "hsr":
            raise failure
        return CheckInReceipt()

    check_in_gateway.claim_check_in.side_effect = claim
    result = runner.invoke(main, ["check-in"])
    assert result.exit_code == 1, result.output
    assert result.output.count("Claimed") == 2
    assert "Honkai: Star Rail" in result.output and "Failed" in result.output
    assert "Traceback" not in result.output and "test-secret-123" not in result.output
    assert check_in_gateway.claim_check_in.await_count == 3


def test_checkin_already_checked_in_is_success(
    runner: CliRunner, check_in_gateway: AsyncMock
) -> None:
    from hoyo_cli.models.common import CheckInState

    check_in_gateway.check_in_state.return_value = CheckInState(True)
    result = runner.invoke(main, ["check-in"])
    assert result.exit_code == 0, result.output
    assert result.output.count("Already checked in") == 3
    check_in_gateway.claim_check_in.assert_not_called()


def test_check_in_help_explains_targeting_and_state_reads(runner: CliRunner) -> None:
    result = runner.invoke(main, ["check-in", "--help"])
    assert result.exit_code == 0
    for command in (
        "hoyo check-in",
        "hoyo check-in genshin",
        "hoyo check-in hsr",
        "hoyo check-in zzz",
    ):
        assert command in result.output
    assert "checked" in result.output and "skipped" in result.output
    assert "network request" in result.output


def test_checkin_all_action_auth_failures_exit_1(
    runner: CliRunner, check_in_gateway: AsyncMock
) -> None:
    from hoyo_cli.errors import AuthenticationError

    check_in_gateway.check_in_state.side_effect = AuthenticationError(
        "Session expired."
    )
    result = runner.invoke(main, ["check-in"])
    assert result.exit_code == 1, result.output
    assert result.output.count("Failed") == 3
    assert result.output.count("Session expired.") == 3
    check_in_gateway.claim_check_in.assert_not_called()


def test_checkin_known_already_claimed_response_exits_0(
    runner: CliRunner, check_in_gateway: AsyncMock
) -> None:
    from hoyo_cli.errors import AlreadyCheckedInError

    check_in_gateway.claim_check_in.side_effect = AlreadyCheckedInError()
    result = runner.invoke(main, ["check-in", "zzz"])
    assert result.exit_code == 0, result.output
    assert "Already checked in" in result.output
    check_in_gateway.claim_check_in.assert_awaited_once_with("zzz", 333333333)


def test_checkin_cancelled_operation_exits_cleanly(
    runner: CliRunner, check_in_gateway: AsyncMock
) -> None:
    check_in_gateway.check_in_state.side_effect = asyncio.CancelledError
    result = runner.invoke(main, ["check-in"])
    assert result.exit_code == 1, result.output
    assert "cancelled" in result.output
    assert "Traceback" not in result.output
    check_in_gateway.claim_check_in.assert_not_called()


@pytest.mark.parametrize("arguments", [[], ["zzz"], ["check-in"], ["check-in", "hsr"]])
def test_commands_observe_service_progress_before_final_output(
    runner: CliRunner,
    check_in_gateway: AsyncMock,
    arguments: list[str],
) -> None:
    from hoyo_cli.render.progress import OperationProgress

    events: list[ProgressEvent] = []
    with patch.object(OperationProgress, "observe", side_effect=events.append):
        result = runner.invoke(main, arguments)
    assert result.exit_code == 0, result.output
    total = 1 if arguments in (["zzz"], ["check-in", "hsr"]) else 3
    assert len([e for e in events if e.stage == "complete"]) == total
    assert "games complete" not in result.stdout
    assert result.stderr == ""
    assert "\x1b" not in result.output and "\r" not in result.output
    assert "Traceback" not in result.output
    if arguments and arguments[0] == "check-in":
        assert check_in_gateway.claim_check_in.await_count == total
    else:
        check_in_gateway.claim_check_in.assert_not_called()


@pytest.mark.parametrize("arguments", [[], ["zzz"], ["check-in"], ["check-in", "hsr"]])
@pytest.mark.parametrize("failed", [False, True])
def test_slow_commands_show_stderr_progress_then_final_results(
    runner: CliRunner,
    check_in_gateway: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    failed: bool,
) -> None:
    from hoyo_cli.models.common import CheckInState
    from hoyo_cli.render import progress

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(progress, "PRESENTATION_DELAY", 0.01)
    original_fetch = check_in_gateway.fetch_status.side_effect
    checking = bool(arguments and arguments[0] == "check-in")

    async def fetch(game: GameId, uid: int):
        await asyncio.sleep(0.03)
        if failed and game == "zzz":
            raise RuntimeError("cookie-secret")
        return await original_fetch(game, uid)

    async def state(game: GameId, uid: int) -> CheckInState:
        await asyncio.sleep(0.03)
        if failed and game == "hsr":
            raise RuntimeError("cookie-secret")
        return CheckInState(False)

    check_in_gateway.fetch_status.side_effect = fetch
    check_in_gateway.check_in_state.side_effect = state
    with patch("click.testing._NamedTextIOWrapper.isatty", return_value=True):
        result = runner.invoke(main, arguments)
    expected_failure = failed and (checking or arguments == ["zzz"])
    assert result.exit_code == int(expected_failure), result.output
    label = "Checking today's rewards" if checking else "Fetching status"
    assert label in result.stderr and label not in result.stdout
    assert "\x1b[?25h" in result.stderr
    assert result.stderr.endswith("\x1b[2K")
    assert ("Check-in" if checking else "ZENLESS ZONE ZERO") in result.stdout
    assert "cookie-secret" not in result.output and "Traceback" not in result.output
    if checking:
        total = 1 if len(arguments) == 2 else 3
        assert check_in_gateway.claim_check_in.await_count == total - int(failed)
    else:
        check_in_gateway.claim_check_in.assert_not_called()


@pytest.mark.parametrize("arguments", [[], ["check-in"]])
@pytest.mark.parametrize("signal", [asyncio.CancelledError, KeyboardInterrupt])
def test_command_cancellation_stops_live_progress_before_error(
    runner: CliRunner,
    check_in_gateway: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    signal: type[BaseException],
) -> None:
    from hoyo_cli.commands import check_in, status
    from hoyo_cli.render import progress

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(progress, "PRESENTATION_DELAY", 0.01)

    async def abort(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0.03)
        raise signal()

    owner, method = (
        (check_in, "check_in_games") if arguments else (status, "fetch_all_status")
    )
    monkeypatch.setattr(owner, method, abort)
    with patch("click.testing._NamedTextIOWrapper.isatty", return_value=True):
        result = runner.invoke(main, arguments)
    assert result.exit_code == 1
    assert "\x1b[?25h" in result.stderr
    assert result.stderr.rfind("\x1b[2K") < result.stderr.index(
        "cancelled" if signal is asyncio.CancelledError else "Aborted!"
    )
    assert result.stdout == "" and "Traceback" not in result.stderr
    check_in_gateway.claim_check_in.assert_not_called()


@pytest.mark.parametrize(
    "case",
    [
        "disabled",
        "unconfigured",
        "no-enabled",
        "account",
        "missing-credentials",
        "malformed-credentials",
        "missing-config",
    ],
)
def test_checkin_preflight_failures_prevent_requests(
    runner: CliRunner, check_in_gateway: AsyncMock, tmp_path: Path, case: str
) -> None:
    from hoyo_cli.commands import check_in
    from hoyo_cli.errors import ConfigError, CredentialError

    arguments = ["check-in", "hsr"]
    expected = 2
    if case in ("disabled", "unconfigured", "no-enabled"):
        games: dict[GameId, GameSelection] = {
            "genshin": GameSelection(case != "no-enabled", 111111111)
        }
        if case == "disabled":
            games["hsr"] = GameSelection(False, 222222222)
        save_account(
            tmp_path / ".hoyo/config.toml",
            "main",
            AccountConfig("plaintext", games),
            make_default=True,
            game_order=(),
        )
        if case == "no-enabled":
            arguments = ["check-in"]
    elif case == "account":
        arguments += ["--account", "ma"]
    elif case in ("missing-credentials", "malformed-credentials"):
        expected = 3
    with (
        patch.object(check_in, "open_store") as store,
        patch.object(check_in, "load_config", wraps=check_in.load_config) as loader,
    ):
        if case == "missing-credentials":
            store.return_value.get.return_value = None
        elif case == "malformed-credentials":
            store.side_effect = CredentialError("Invalid credential store.")
        elif case == "missing-config":
            loader.side_effect = ConfigError("Config missing; run hoyo init.")
        result = runner.invoke(main, arguments)
    assert result.exit_code == expected, result.output
    assert "Traceback" not in result.output and "test-secret-123" not in result.output
    check_in_gateway.check_in_state.assert_not_called()
    check_in_gateway.claim_check_in.assert_not_called()
