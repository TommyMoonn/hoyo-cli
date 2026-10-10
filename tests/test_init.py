"""Offline linked-role discovery and failure-safe interactive setup."""

from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import patch

import click
import genshin
import pytest
from click.testing import CliRunner

from hoyo_cli.auth.browser_import import BrowserSession
from hoyo_cli.auth.credentials import Credentials, parse_cookie_header
from hoyo_cli.auth.keyring_backend import KeyringStore
from hoyo_cli.auth.plaintext_backend import PlaintextStore
from hoyo_cli.cli import main
from hoyo_cli.commands.init import run_init
from hoyo_cli.config.models import GameSelection, enabled_games
from hoyo_cli.config.repository import load_config
from hoyo_cli.errors import (
    AppError,
    AuthenticationError,
    BackendUnavailable,
    BrowserImportError,
    CredentialError,
)
from hoyo_cli.hoyolab.client import fetch_linked_accounts
from hoyo_cli.hoyolab.discovery import CharacterRef, discover_characters
from hoyo_cli.models.common import GAME_IDS, GameId


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get(username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[username] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[username]


@pytest.mark.parametrize("existing", [False, True])
def test_keyring_setup_and_rollback_after_config_failure(
    tmp_path: Path, existing: bool
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    backend = MemoryKeyring()
    store = KeyringStore(backend)
    if existing:
        store.put("main", parse_cookie_header("ltuid=123; ltoken=test-original"))
    original = dict(backend.values)
    with patch("hoyo_cli.commands.init.open_store", return_value=store):

        def fail_commit(*args: object) -> None:
            assert store.get("main") == parse_cookie_header(
                "ltuid=123; ltoken=test-secret-123"
            )
            raise AppError("Simulated config failure")

        with patch("hoyo_cli.commands.init.commit_config", side_effect=fail_commit):
            with pytest.raises(AppError):
                run_init(
                    config,
                    secrets,
                    FakeIO(backend="keyring"),
                    FakeBrowser(),
                    FakeDiscovery(),
                )
    assert backend.values == original
    assert not config.exists() and not secrets.exists()
    with patch("hoyo_cli.commands.init.open_store", return_value=store):
        run_init(
            config, secrets, FakeIO(backend="keyring"), FakeBrowser(), FakeDiscovery()
        )
    assert load_config(config).accounts["main"].credential_backend == "keyring"
    assert store.get("main") is not None
    assert not secrets.exists()


@pytest.mark.parametrize("accept", [False, True])
def test_unavailable_keyring_needs_explicit_plaintext_consent(
    tmp_path: Path, accept: bool
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    backend = MemoryKeyring()
    store = KeyringStore(backend)

    class FallbackIO(FakeIO):
        def confirm(self, prompt: str) -> bool:
            if prompt.startswith("OS keyring"):
                return accept
            return super().confirm(prompt)

    with patch.object(
        backend, "get_password", side_effect=RuntimeError("test-secret-123")
    ):
        with patch(
            "hoyo_cli.commands.init.open_store",
            side_effect=[store, PlaintextStore(secrets)],
        ):
            if accept:
                run_init(
                    config,
                    secrets,
                    FallbackIO(backend="keyring"),
                    FakeBrowser(),
                    FakeDiscovery(),
                )
                assert (
                    load_config(config).accounts["main"].credential_backend
                    == "plaintext"
                )
            else:
                with pytest.raises(BackendUnavailable):
                    run_init(
                        config,
                        secrets,
                        FallbackIO(backend="keyring"),
                        FakeBrowser(),
                        FakeDiscovery(),
                    )
                assert not config.exists() and not secrets.exists()


def test_cli_init_hidden_cookie_and_config_inspection(
    tmp_path: Path, runner: CliRunner
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    # Account; decline browser; hidden cookie; select/enable three games;
    # order; plaintext; accept plaintext; save.
    answers = (
        "main\nn\nltuid=123; ltoken=test-secret-123\n"
        "1\ny\n1\ny\n1\ny\n1\n1\n1\n2\ny\ny\n"
    )
    with (
        patch("hoyo_cli.commands.init.config_path", return_value=config),
        patch("hoyo_cli.commands.init.credentials_path", return_value=secrets),
        patch("hoyo_cli.commands.init.HoYoLABDiscovery", return_value=FakeDiscovery()),
        patch(
            "hoyo_cli.commands.init.SystemBrowserImporter", return_value=FakeBrowser()
        ),
    ):
        result = runner.invoke(main, ["init", "--no-color"], input=answers)
    assert result.exit_code == 0, result.output
    assert "Run hoyo" in result.output
    assert "test-secret-123" not in result.output
    assert "\x1b[" not in result.output
    with patch("hoyo_cli.commands.config.config_path", return_value=config):
        result = runner.invoke(main, ["config"])
    assert result.exit_code == 0, result.output
    assert "test-secret-123" not in result.output


def test_cli_invalid_cookie_has_exit_three_and_no_echo(
    tmp_path: Path, runner: CliRunner
) -> None:
    with patch(
        "hoyo_cli.commands.init.config_path", return_value=tmp_path / "config.toml"
    ):
        result = runner.invoke(
            main, ["init"], input="main\nn\nltoken=test-secret-123\n"
        )
    assert result.exit_code == 3
    assert "test-secret-123" not in result.output
    assert "Traceback" not in result.output


def test_cli_cancelled_browser_menu_does_not_import(
    tmp_path: Path, runner: CliRunner
) -> None:
    browser = FakeBrowser()
    with (
        patch(
            "hoyo_cli.commands.init.config_path", return_value=tmp_path / "config.toml"
        ),
        patch("hoyo_cli.commands.init.SystemBrowserImporter", return_value=browser),
    ):
        result = runner.invoke(main, ["init"], input="main\ny\n4\n")
    assert result.exit_code == 1
    assert browser.imported == []
    assert not (tmp_path / "config.toml").exists()


def test_browser_read_failure_offers_manual_fallback(tmp_path: Path) -> None:
    browser = FakeBrowser()
    with patch.object(
        browser,
        "import_session",
        side_effect=BrowserImportError("denied"),
    ):
        run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            FakeIO(browser=True),
            browser,
            FakeDiscovery(),
        )
    assert load_config(tmp_path / "config.toml").default_account == "main"


@pytest.mark.parametrize("stage", ["discover", "no-enabled", "invalid-name"])
def test_invalid_setup_never_persists(tmp_path: Path, stage: str) -> None:
    class InvalidDiscovery(FakeDiscovery):
        def discover(
            self, credentials: Credentials
        ) -> dict[GameId, tuple[CharacterRef, ...]]:
            if stage == "discover":
                raise AuthenticationError("Expired")
            return super().discover(credentials)

    class InvalidIO(FakeIO):
        def confirm(self, prompt: str) -> bool:
            if stage == "no-enabled" and prompt.startswith("Enable"):
                return False
            return super().confirm(prompt)

    io = InvalidIO(name="\n" if stage == "invalid-name" else "main")
    with pytest.raises(AppError):
        run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            io,
            FakeBrowser(),
            InvalidDiscovery(),
        )
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "credentials.toml").exists()


@pytest.mark.parametrize("backend", ["keyring", "plaintext"])
@pytest.mark.parametrize("replacement_uid", [111111111, 999999999])
def test_reconfiguration_disables_undiscovered_games_and_retains_uids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    backend: str,
    replacement_uid: int,
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    store = (
        KeyringStore(MemoryKeyring())
        if backend == "keyring"
        else PlaintextStore(secrets)
    )

    class ReplacementIO(FakeIO):
        def secret(self, prompt: str) -> str:
            if replacement_uid == 999999999:
                return "ltuid=456; ltoken=test-replacement-secret"
            return super().secret(prompt)

    discovery = FakeDiscovery()
    only_genshin: dict[GameId, tuple[CharacterRef, ...]] = {
        "genshin": (CharacterRef("genshin", replacement_uid, "test", "Traveler"),)
    }
    io = ReplacementIO(backend=backend)
    with patch("hoyo_cli.commands.init.open_store", return_value=store):
        run_init(config, secrets, FakeIO(backend=backend), FakeBrowser(), discovery)
        capsys.readouterr()
        with patch.object(discovery, "discover", return_value=only_genshin):
            run_init(config, secrets, io, FakeBrowser(), discovery)
    parsed = load_config(config)
    account = parsed.accounts["main"]
    assert account.games["genshin"] == GameSelection(True, replacement_uid)
    assert account.games["hsr"] == GameSelection(False, 222222222)
    assert account.games["zzz"] == GameSelection(False, 333333333)
    assert enabled_games(parsed, account) == ("genshin",)
    assert store.get("main") == parse_cookie_header(io.secret("test"))
    output = capsys.readouterr().out
    assert "hsr:" in output and "zzz:" in output
    assert "will be disabled" in output
    assert "test-secret-123" not in output
    assert "test-replacement-secret" not in output


@pytest.mark.parametrize("game", GAME_IDS)
def test_first_setup_with_only_one_linked_game_does_not_fabricate_others(
    tmp_path: Path, game: GameId
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    discovery = FakeDiscovery()
    credentials = parse_cookie_header("ltuid=123; ltoken=test-secret-123")
    only_game = {game: discovery.discover(credentials)[game]}
    with patch.object(discovery, "discover", return_value=only_game):
        run_init(config, secrets, FakeIO(), FakeBrowser(), discovery)
    parsed = load_config(config)
    assert tuple(parsed.accounts["main"].games) == (game,)
    assert enabled_games(parsed, parsed.accounts["main"]) == (game,)


def test_unconfirmed_games_cannot_satisfy_the_enabled_game_requirement(
    tmp_path: Path,
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original = config.read_bytes(), secrets.read_bytes()

    class DisableLinkedIO(FakeIO):
        def confirm(self, prompt: str) -> bool:
            return False if prompt.startswith("Enable") else super().confirm(prompt)

    discovery = FakeDiscovery()
    only_genshin: dict[GameId, tuple[CharacterRef, ...]] = {
        "genshin": (CharacterRef("genshin", 999999999, "test", "New Traveler"),)
    }
    with patch.object(discovery, "discover", return_value=only_genshin):
        with pytest.raises(AppError, match="at least one enabled game"):
            run_init(config, secrets, DisableLinkedIO(), FakeBrowser(), discovery)
    assert (config.read_bytes(), secrets.read_bytes()) == original


def test_reconfiguration_preserves_unrelated_accounts_and_game_metadata(
    tmp_path: Path,
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    run_init(config, secrets, FakeIO(name="alt"), FakeBrowser(), FakeDiscovery())
    content = config.read_text(encoding="utf-8")
    content = "# Keep root comment\n" + content.replace(
        "version = 1", 'version = 1\nunknown_root = "keep"', 1
    )
    content = content.replace(
        "[accounts.main]", '[accounts.main]\nunknown_account = "keep"', 1
    ).replace(
        "[accounts.main.games.hsr]",
        '# Keep game comment\n[accounts.main.games.hsr]\nunknown_game = "keep"',
        1,
    )
    config.write_text(content, encoding="utf-8")
    original = load_config(config)
    original_alt_secrets = PlaintextStore(secrets).get("alt")

    class KeepDefaultIO(FakeIO):
        def confirm(self, prompt: str) -> bool:
            if prompt == "Use main as the default account?":
                return False
            return super().confirm(prompt)

    discovery = FakeDiscovery()
    only_genshin: dict[GameId, tuple[CharacterRef, ...]] = {
        "genshin": (CharacterRef("genshin", 999999999, "test", "New Traveler"),)
    }
    with patch.object(discovery, "discover", return_value=only_genshin):
        run_init(config, secrets, KeepDefaultIO(), FakeBrowser(), discovery)
    parsed = load_config(config)
    assert parsed.default_account == original.default_account == "alt"
    assert parsed.game_order == original.game_order
    assert parsed.accounts["alt"] == original.accounts["alt"]
    assert PlaintextStore(secrets).get("alt") == original_alt_secrets
    assert parsed.accounts["main"].games["hsr"] == GameSelection(False, 222222222)
    updated = config.read_text(encoding="utf-8")
    for preserved in (
        "# Keep root comment",
        "# Keep game comment",
        'unknown_root = "keep"',
        'unknown_account = "keep"',
        'unknown_game = "keep"',
    ):
        assert preserved in updated


def test_undiscovered_disabled_games_stay_disabled_without_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class OnlyEnableGenshinIO(FakeIO):
        def confirm(self, prompt: str) -> bool:
            if prompt in ("Enable hsr?", "Enable zzz?"):
                return False
            return super().confirm(prompt)

    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    discovery = FakeDiscovery()
    run_init(config, secrets, OnlyEnableGenshinIO(), FakeBrowser(), discovery)
    previous = load_config(config)
    only_genshin = {
        "genshin": discovery.discover(
            parse_cookie_header("ltuid=123; ltoken=test-secret-123")
        )["genshin"]
    }
    capsys.readouterr()
    with patch.object(discovery, "discover", return_value=only_genshin):
        run_init(config, secrets, FakeIO(), FakeBrowser(), discovery)
    assert load_config(config) == previous
    assert "will be disabled" not in capsys.readouterr().out


@pytest.mark.parametrize("outcome", ["cancel", "write-failure"])
def test_partial_rediscovery_failure_preserves_original_enabled_games_and_secrets(
    tmp_path: Path, outcome: str
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    discovery = FakeDiscovery()
    run_init(config, secrets, FakeIO(), FakeBrowser(), discovery)
    original = config.read_bytes(), secrets.read_bytes()

    class ReplacementIO(FakeIO):
        def secret(self, prompt: str) -> str:
            return "ltuid=456; ltoken=test-replacement-secret"

    only_genshin: dict[GameId, tuple[CharacterRef, ...]] = {
        "genshin": (CharacterRef("genshin", 999999999, "test", "New Traveler"),)
    }
    with patch.object(discovery, "discover", return_value=only_genshin):
        if outcome == "cancel":
            with pytest.raises(click.Abort):
                run_init(
                    config,
                    secrets,
                    ReplacementIO(cancel="Save"),
                    FakeBrowser(),
                    discovery,
                )
        else:

            def fail_commit(*args: object) -> None:
                assert PlaintextStore(secrets).get("main") == parse_cookie_header(
                    "ltuid=456; ltoken=test-replacement-secret"
                )
                raise AppError("Simulated config failure")

            with patch("hoyo_cli.commands.init.commit_config", side_effect=fail_commit):
                with pytest.raises(AppError, match="Simulated config failure"):
                    run_init(config, secrets, ReplacementIO(), FakeBrowser(), discovery)
    assert (config.read_bytes(), secrets.read_bytes()) == original


def test_config_change_at_confirmation_is_not_overwritten(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original_secrets = secrets.read_bytes()

    class EditingIO(FakeIO):
        def confirm(self, prompt: str) -> bool:
            if prompt.startswith("Save"):
                with config.open("a") as stream:
                    stream.write("\n# concurrent user edit\n")
            return super().confirm(prompt)

    with pytest.raises(AppError, match="changed"):
        run_init(config, secrets, EditingIO(), FakeBrowser(), FakeDiscovery())
    assert "# concurrent user edit" in config.read_text()
    assert secrets.read_bytes() == original_secrets


class FakeIO:
    def __init__(
        self,
        *,
        name: str = "main",
        backend: str = "plaintext",
        browser: bool = False,
        replace: bool = True,
        cancel: str = "",
    ) -> None:
        self.name, self.backend, self.browser = name, backend, browser
        self.replace, self.cancel = replace, cancel
        self.prompts: list[str] = []

    def choose(self, prompt: str, choices: Sequence[str]) -> str:
        self.prompts.append(prompt)
        if self.cancel and self.cancel in prompt:
            raise click.Abort()
        if prompt == "Account name":
            return self.name
        if prompt == "Account":
            return self.name if self.name in choices else "Create new account"
        if prompt == "Credential backend":
            return self.backend
        if prompt == "HoYoLAB session":
            return choices[1] if self.browser else "Paste Cookie header"
        return choices[0]

    def confirm(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        if self.cancel and self.cancel in prompt:
            raise click.Abort()
        if prompt.startswith("Import"):
            return self.browser
        if prompt.startswith("Replace"):
            return self.replace
        return True

    def secret(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "ltuid=123; ltoken=test-secret-123"


class FakeBrowser:
    def __init__(self) -> None:
        self.imported: list[str] = []
        self.listed = 0

    def list_sessions(self) -> tuple[BrowserSession, ...]:
        self.listed += 1
        return (BrowserSession("chrome", "Chrome"), BrowserSession("edge", "Edge"))

    def import_session(self, source_id: str) -> Credentials:
        self.imported.append(source_id)
        return parse_cookie_header("ltuid=123; ltoken=test-secret-123")


class FakeDiscovery:
    def discover(
        self, credentials: Credentials
    ) -> dict[GameId, tuple[CharacterRef, ...]]:
        return {
            "genshin": (CharacterRef("genshin", 111111111, "test", "Traveler"),),
            "hsr": (CharacterRef("hsr", 222222222, "test", "Trailblazer"),),
            "zzz": (CharacterRef("zzz", 333333333, "test", "Proxy"),),
        }


def test_initial_setup_one_uid_per_game(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    io = FakeIO()
    run_init(config, secrets, io, FakeBrowser(), FakeDiscovery())
    parsed = load_config(config)
    assert parsed.default_account == "main"
    assert list(parsed.accounts["main"].games) == ["genshin", "hsr", "zzz"]
    assert PlaintextStore(secrets).get("main") is not None
    assert "test-secret-123" not in config.read_text()
    output = capsys.readouterr().out
    assert "run hoyo" in output.lower()
    assert output.count("Warning:") == 1
    assert "test-secret-123" not in output


def test_manual_cookie_fallback_and_named_account(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    browser = FakeBrowser()
    run_init(config, secrets, FakeIO(name="Exact.Account"), browser, FakeDiscovery())
    assert load_config(config).default_account == "Exact.Account"
    assert browser.listed == 0
    assert browser.imported == []


def test_multiple_browser_choice_is_explicit(tmp_path: Path) -> None:
    browser = FakeBrowser()
    run_init(
        tmp_path / "config.toml",
        tmp_path / "credentials.toml",
        FakeIO(browser=True),
        browser,
        FakeDiscovery(),
    )
    assert browser.imported == ["edge"]


def test_existing_config_preserves_other_accounts_and_comments(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(name="alt"), FakeBrowser(), FakeDiscovery())
    original = load_config(config).accounts["alt"]
    with config.open("a") as stream:
        stream.write('\n# user comment\n[custom]\nmarker="keep"\n')
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    assert load_config(config).accounts["alt"] == original
    assert "# user comment" in config.read_text()
    assert 'marker="keep"' in config.read_text()


def test_existing_credentials_not_silently_replaced(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original = config.read_bytes(), secrets.read_bytes()
    with pytest.raises(click.Abort):
        run_init(config, secrets, FakeIO(replace=False), FakeBrowser(), FakeDiscovery())
    assert (config.read_bytes(), secrets.read_bytes()) == original


@pytest.mark.parametrize("cancel", ["Account", "HoYoLAB session", "Save"])
def test_cancel_leaves_no_files(tmp_path: Path, cancel: str) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    browser = FakeBrowser()
    with pytest.raises(click.Abort):
        run_init(
            config,
            secrets,
            FakeIO(browser=True, cancel=cancel),
            browser,
            FakeDiscovery(),
        )
    assert not config.exists() and not secrets.exists()
    if cancel == "HoYoLAB session":
        assert not browser.imported


def test_invalid_config_leaves_original_bytes_unchanged(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    config.write_bytes(b"invalid [")
    secrets.write_bytes(b"private-marker")
    browser = FakeBrowser()
    with pytest.raises(AppError):
        run_init(config, secrets, FakeIO(), browser, FakeDiscovery())
    assert config.read_bytes() == b"invalid ["
    assert secrets.read_bytes() == b"private-marker"
    assert browser.listed == 0


@pytest.mark.parametrize("existing", [False, True])
def test_config_write_failure_restores_exact_secrets(
    tmp_path: Path, existing: bool
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    if existing:
        run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original_config = config.read_bytes() if existing else None
    original_secrets = secrets.read_bytes() if existing else None
    with patch(
        "hoyo_cli.config.repository._atomic_write", side_effect=AppError("fail")
    ):
        with pytest.raises(AppError):
            run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    assert (config.read_bytes() if config.exists() else None) == original_config
    assert (secrets.read_bytes() if secrets.exists() else None) == original_secrets


def role(game_biz: str, uid: int = 111111111) -> genshin.models.GenshinAccount:
    return genshin.models.GenshinAccount.model_validate(
        {
            "game_biz": game_biz,
            "game_uid": uid,
            "level": 10,
            "nickname": "Test character",
            "region": "test-region",
            "region_name": "Test region",
        }
    )


def test_network_guard_allows_only_socketpair() -> None:
    import socket

    left, right = socket.socketpair()
    left.close()
    right.close()
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="offline"):
            sock.connect(("127.0.0.1", 1))


class FakeClient:
    def __init__(self, roles: Sequence[genshin.models.GenshinAccount]) -> None:
        self.roles = roles
        self.calls = 0

    async def get_game_accounts(self) -> Sequence[genshin.models.GenshinAccount]:
        self.calls += 1
        return self.roles


def test_discovery_groups_three_games_and_keeps_multiple_characters() -> None:
    client = FakeClient(
        [
            role("hk4e_global"),
            role("hk4e_global", 111111112),
            role("hkrpg_global", 222222222),
            role("nap_global", 333333333),
            role("bh3_global", 444444444),
        ]
    )
    credentials = parse_cookie_header("ltuid=123; ltoken=test-secret-123")
    with patch("hoyo_cli.hoyolab.client.create_client", return_value=client) as factory:
        discovered = discover_characters(credentials)
    assert list(discovered) == ["genshin", "hsr", "zzz"]
    assert [c.uid for c in discovered["genshin"]] == [111111111, 111111112]
    assert discovered["zzz"][0].region == "test-region"
    assert client.calls == 1
    factory.assert_called_once_with(credentials)


def test_missing_game_is_not_fabricated() -> None:
    with patch("hoyo_cli.hoyolab.client.create_client", return_value=FakeClient([])):
        assert (
            discover_characters(
                parse_cookie_header("ltuid=123; ltoken=test-secret-123")
            )
            == {}
        )


@pytest.mark.parametrize(
    "error",
    [
        genshin.errors.InvalidCookies(msg="test-secret-123"),
        TimeoutError("test-secret-123"),
    ],
)
def test_expired_session_is_auth_error_and_network_error_is_distinct(
    error: Exception,
) -> None:
    class FailingClient:
        async def get_game_accounts(self) -> Sequence[genshin.models.GenshinAccount]:
            raise error

    with patch("hoyo_cli.hoyolab.client.create_client", return_value=FailingClient()):
        with pytest.raises(AppError) as caught:
            fetch_linked_accounts(
                parse_cookie_header("ltuid=123; ltoken=test-secret-123")
            )
    assert isinstance(caught.value, AuthenticationError) == isinstance(
        error, genshin.errors.InvalidCookies
    )
    assert "test-secret-123" not in str(caught.value)


def test_cli_unexpected_discovery_failure_is_redacted(
    tmp_path: Path, runner: CliRunner
) -> None:
    discovery = FakeDiscovery()
    with (
        patch(
            "hoyo_cli.commands.init.config_path", return_value=tmp_path / "config.toml"
        ),
        patch("hoyo_cli.commands.init.HoYoLABDiscovery", return_value=discovery),
        patch.object(
            discovery, "discover", side_effect=RuntimeError("test-secret-123")
        ),
    ):
        result = runner.invoke(
            main, ["init"], input="main\nn\nltuid=123; ltoken=test-secret-123\n"
        )
    assert result.exit_code == 1
    assert "setup failed" in result.output
    assert "test-secret-123" not in result.output
    assert "Traceback" not in result.output


def test_uid_enabled_flags_and_custom_order_are_selected(tmp_path: Path) -> None:
    class SelectionIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Character for genshin":
                return choices[1]
            if prompt == "Next game in display order":
                return choices[-1]
            return super().choose(prompt, choices)

        def confirm(self, prompt: str) -> bool:
            return False if prompt == "Enable hsr?" else super().confirm(prompt)

    class MultipleDiscovery(FakeDiscovery):
        def discover(
            self, credentials: Credentials
        ) -> dict[GameId, tuple[CharacterRef, ...]]:
            discovered = super().discover(credentials)
            discovered["genshin"] += (
                CharacterRef("genshin", 111111112, "alt", "Other"),
            )
            return discovered

    config = tmp_path / "config.toml"
    run_init(
        config,
        tmp_path / "credentials.toml",
        SelectionIO(),
        FakeBrowser(),
        MultipleDiscovery(),
    )
    parsed = load_config(config)
    assert parsed.game_order == ("zzz", "hsr", "genshin")
    assert parsed.accounts["main"].games["genshin"].uid == 111111112
    assert not parsed.accounts["main"].games["hsr"].enabled


def test_backend_change_keeps_old_secrets_without_migration(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original = secrets.read_bytes()
    store = KeyringStore(MemoryKeyring())
    with patch("hoyo_cli.commands.init.open_store", return_value=store):
        run_init(
            config, secrets, FakeIO(backend="keyring"), FakeBrowser(), FakeDiscovery()
        )
    assert load_config(config).accounts["main"].credential_backend == "keyring"
    assert secrets.read_bytes() == original
    assert store.get("main") is not None


def test_rollback_failure_is_explicit(tmp_path: Path) -> None:
    class BrokenStore:
        def get(self, account: str) -> Credentials | None:
            return None

        def put(self, account: str, credentials: Credentials) -> None:
            raise CredentialError("Write failed")

        def checkpoint(self, account: str) -> Callable[[], None]:
            def restore() -> None:
                raise CredentialError("test-secret-123")

            return restore

    with patch("hoyo_cli.commands.init.open_store", return_value=BrokenStore()):
        with pytest.raises(CredentialError, match="rollback failed") as caught:
            run_init(
                tmp_path / "config.toml",
                tmp_path / "credentials.toml",
                FakeIO(backend="keyring"),
                FakeBrowser(),
                FakeDiscovery(),
            )
    assert "test-secret-123" not in str(caught.value)
    assert not (tmp_path / "config.toml").exists()


def test_keyboard_interrupt_during_persistence_restores_secrets(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original = config.read_bytes(), secrets.read_bytes()
    with patch("hoyo_cli.commands.init.commit_config", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    assert (config.read_bytes(), secrets.read_bytes()) == original


def test_declining_save_preserves_existing_files(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original = config.read_bytes(), secrets.read_bytes()

    class DeclineSaveIO(FakeIO):
        def confirm(self, prompt: str) -> bool:
            return False if prompt.startswith("Save") else super().confirm(prompt)

    with pytest.raises(click.Abort):
        run_init(config, secrets, DeclineSaveIO(), FakeBrowser(), FakeDiscovery())
    assert (config.read_bytes(), secrets.read_bytes()) == original
