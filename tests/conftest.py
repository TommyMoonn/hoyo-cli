"""Shared offline CLI test fixtures."""

import json
import socket
import sys
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, cast
from unittest.mock import AsyncMock, Mock

import genshin
import keyring
import pytest
from click.testing import CliRunner
from rich.console import Console

from hoyo_cli.auth.credentials import Credentials, parse_cookie_header
from hoyo_cli.auth.keyring_backend import KeyringStore
from hoyo_cli.auth.plaintext_backend import PlaintextStore
from hoyo_cli.commands import check_in, init, status
from hoyo_cli.config.models import AccountConfig, CredentialBackend, GameSelection
from hoyo_cli.config.repository import save_account
from hoyo_cli.hoyolab.client import GatewayClient, GenshinPyGateway
from hoyo_cli.hoyolab.discovery import CharacterRef
from hoyo_cli.models.common import GAME_IDS, GameId
from hoyo_cli.render import progress, setup
from hoyo_cli.render.console import make_console


@dataclass
class AcceptanceEnvironment:
    home: Path
    cache: Path
    now: datetime
    client: AsyncMock
    browser: Mock
    keyring: Mock
    discovery: Mock
    backend: CredentialBackend
    width: int = 100
    interactive: bool = False

    @property
    def config(self) -> Path:
        return self.home / ".hoyo/config.toml"

    @property
    def credentials(self) -> Path:
        return self.home / ".hoyo/credentials.toml"

    def notes(self, game: GameId) -> AsyncMock:
        return cast(AsyncMock, getattr(self.client, NOTES_METHODS[game]))


NOTES_METHODS: dict[GameId, str] = {
    "genshin": "get_genshin_notes",
    "hsr": "get_starrail_notes",
    "zzz": "get_zzz_notes",
}
TEST_COOKIE = "ltuid=123; ltoken=acceptance-secret-not-real"
TEST_UIDS: dict[GameId, int] = {
    "genshin": 111111111,
    "hsr": 222222222,
    "zzz": 333333333,
}


@pytest.fixture(params=["keyring", "plaintext"])
def acceptance_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AcceptanceEnvironment:
    backend = cast(CredentialBackend, request.param)
    client = AsyncMock(spec=GatewayClient)
    client.uids = {}
    client.get_reward_info.return_value = genshin.models.DailyRewardInfo(
        signed_in=False, claimed_rewards=0
    )
    env = AcceptanceEnvironment(
        tmp_path / "home",
        tmp_path / "cache",
        datetime(2026, 10, 10, 11, 31, tzinfo=UTC),
        client,
        Mock(),
        Mock(),
        Mock(),
        backend,
    )
    monkeypatch.setattr(Path, "home", lambda: env.home)

    def cache_path(*args: object, **kwargs: object) -> Path:
        return env.cache

    monkeypatch.setattr("platformdirs.user_cache_path", cache_path)
    # Use the normal keyring owner, replacing only OS access.
    values: dict[tuple[str, str], str] = {}

    def get_password(service: str, name: str) -> str | None:
        return values.get((service, name))

    def set_password(service: str, name: str, value: str) -> None:
        values[service, name] = value

    def delete_password(service: str, name: str) -> None:
        del values[service, name]

    env.keyring.get_password.side_effect = get_password
    env.keyring.set_password.side_effect = set_password
    env.keyring.delete_password.side_effect = delete_password
    for method in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, method, getattr(env.keyring, method))
    account = AccountConfig(
        backend, {game: GameSelection(True, uid) for game, uid in TEST_UIDS.items()}
    )
    for name in ("main", "alt"):
        save_account(
            env.config, name, account, make_default=name == "main", game_order=GAME_IDS
        )
        store = (
            KeyringStore() if backend == "keyring" else PlaintextStore(env.credentials)
        )
        store.put(name, parse_cookie_header(TEST_COOKIE))
    for game in GAME_IDS:
        env.notes(game).return_value = json.loads(
            (Path(__file__).parent / f"fixtures/{game}_notes.json").read_text()
        )

    def gateway(credentials: Credentials) -> GenshinPyGateway:
        return GenshinPyGateway(credentials, client=client, now=lambda: env.now)

    def console(
        *,
        width: int | None,
        no_color: bool,
        interactive: bool,
        configured_color: str,
        stderr: bool = False,
    ) -> Console:
        result = make_console(
            width=env.width,
            no_color=no_color,
            interactive=sys.stderr.isatty() if stderr else env.interactive,
            configured_color=configured_color,
            stderr=stderr,
        )
        # Model the specified dimensions of a modern terminal. Rich otherwise
        # forces dumb terminals to 80x25 or reserves a legacy Windows column.
        result.legacy_windows = False
        result.height = 25
        return result

    monkeypatch.setattr(status, "GenshinPyGateway", gateway)
    monkeypatch.setattr(check_in, "GenshinPyGateway", gateway)
    monkeypatch.setattr(status, "_now", lambda: env.now)
    monkeypatch.setattr(status, "make_console", console)
    monkeypatch.setattr(check_in, "make_console", console)
    monkeypatch.setattr(progress, "make_console", console)
    monkeypatch.setattr(setup, "make_console", console)
    env.browser.list_sessions.return_value = ()
    monkeypatch.setattr(init, "SystemBrowserImporter", lambda: env.browser)
    env.discovery.discover.return_value = {
        game: (CharacterRef(game, uid, "test", f"Test {game}"),)
        for game, uid in TEST_UIDS.items()
    }
    monkeypatch.setattr(init, "HoYoLABDiscovery", lambda: env.discovery)
    env.keyring.reset_mock()
    return env


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-read-only",
        action="store_true",
        default=False,
        help="Opt into configured-account read-only HoYoLAB status tests.",
    )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    live = Path(__file__).parent / "live"
    if collection_path.is_relative_to(live) and not config.getoption(
        "--live-read-only"
    ):
        return True
    return None


@pytest.fixture(autouse=True)
def prevent_network(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    if (
        request.config.getoption("--live-read-only")
        and "live_read_only" in request.keywords
    ):
        return
    constructing_pair: ContextVar[bool] = ContextVar("constructing_pair", default=False)
    original_connect = socket.socket.connect
    original_pair = socket.socketpair

    def reject_network(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("The default test suite must remain offline.")

    def guarded_connect(sock: socket.socket, address: object) -> None:
        if constructing_pair.get():
            cast(Callable[[socket.socket, object], None], original_connect)(
                sock, address
            )
        else:
            reject_network()

    def local_socketpair(
        *args: object, **kwargs: object
    ) -> tuple[socket.socket, socket.socket]:
        token = constructing_pair.set(True)
        try:
            return cast(
                Callable[..., tuple[socket.socket, socket.socket]], original_pair
            )(*args, **kwargs)
        finally:
            constructing_pair.reset(token)

    # Windows builds socketpair through a temporary loopback connection. This
    # allowance ends before any test coroutine or application request can run.
    monkeypatch.setattr(socket, "socketpair", local_socketpair)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
