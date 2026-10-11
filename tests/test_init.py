"""Offline linked-role discovery and failure-safe interactive setup."""

import asyncio
import io as stream_io
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import patch

import click
import genshin
import pytest
from click.testing import CliRunner
from profile_fixtures import ProfileSeed, save_profile
from rich.console import Console

from hoyo_cli.auth.browser_import import BrowserProfile, BrowserSession
from hoyo_cli.auth.credentials import Credentials, parse_cookie_header
from hoyo_cli.auth.keyring_backend import KeyringStore
from hoyo_cli.auth.plaintext_backend import PlaintextStore
from hoyo_cli.cli import main
from hoyo_cli.commands.init import run_add_profile, run_edit_profile, run_init
from hoyo_cli.config.models import GameSelection, enabled_games
from hoyo_cli.config.repository import (
    commit_config,
    load_config,
    prepare_default_profile,
)
from hoyo_cli.errors import (
    AppError,
    AuthenticationError,
    BackendUnavailable,
    BrowserImportError,
    CredentialError,
    ErrorCode,
    NetworkError,
)
from hoyo_cli.hoyolab.client import fetch_linked_accounts
from hoyo_cli.hoyolab.discovery import CharacterRef, discover_characters
from hoyo_cli.models.common import GAME_IDS, GameId
from hoyo_cli.render import progress


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
    assert load_config(config).logins["main"].credential_backend == "keyring"
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
                    load_config(config).logins["main"].credential_backend == "plaintext"
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
    # Account; decline browser; hidden cookie; select three games;
    # accept default order; plaintext; explicit consent; save.
    answers = "main\nn\nltuid=123; ltoken=test-secret-123\n1\n1\n1\ny\n2\ny\n1\n"
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


def test_cli_invalid_cookie_offers_recovery_and_eof_cancels_without_echo(
    tmp_path: Path, runner: CliRunner
) -> None:
    with patch(
        "hoyo_cli.commands.init.config_path", return_value=tmp_path / "config.toml"
    ):
        result = runner.invoke(
            main, ["init"], input="main\nn\nltoken=test-secret-123\n"
        )
    assert result.exit_code == 1
    assert "Session recovery" in result.output
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
    assert load_config(tmp_path / "config.toml").default_profile == "main"


@pytest.mark.parametrize("recovery", ["retry", "manual", "cancel"])
def test_cli_browser_lock_recovery_is_actionable_and_secret_safe(
    tmp_path: Path, runner: CliRunner, recovery: str
) -> None:
    from hoyo_cli.auth.browser_import import browser_error

    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    browser = FakeBrowser()
    first_failure = browser_error(ErrorCode.BROWSER_LOCKED)
    recovered = parse_cookie_header("ltuid=123; ltoken=test-secret-123")
    recovery_answer = {"retry": "1", "manual": "3", "cancel": "4"}[recovery]
    answers = "main\ny\n2\n" + recovery_answer + "\n"
    if recovery == "manual":
        answers += "ltuid=123; ltoken=test-secret-123\n"
    if recovery != "cancel":
        answers += "1\n1\n1\ny\n2\ny\n1\n"
    with (
        patch("hoyo_cli.commands.init.config_path", return_value=config),
        patch("hoyo_cli.commands.init.credentials_path", return_value=secrets),
        patch("hoyo_cli.commands.init.HoYoLABDiscovery", return_value=FakeDiscovery()),
        patch("hoyo_cli.commands.init.SystemBrowserImporter", return_value=browser),
        patch.object(
            browser, "import_session", side_effect=[first_failure, recovered]
        ) as read,
    ):
        result = runner.invoke(main, ["init", "--no-color"], input=answers)
    assert result.exit_code == (1 if recovery == "cancel" else 0), result.output
    assert "database is locked" in result.stderr
    assert "Close the selected browser fully" in result.stderr
    assert read.call_count == (2 if recovery == "retry" else 1)
    assert "test-secret-123" not in result.output
    assert "Traceback" not in result.output and "\x1b[" not in result.output
    if recovery == "cancel":
        assert "Session verified" not in result.output
        assert not config.exists() and not secrets.exists()
    else:
        assert "Session verified" in result.output
        assert config.exists() and secrets.exists()


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
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if stage == "no-enabled" and prompt.startswith("Character"):
                return "Skip this game"
            if prompt in ("Session recovery", "Profile recovery"):
                return "Cancel (no changes)"
            return super().choose(prompt, choices)

    io = InvalidIO(name="\n" if stage == "invalid-name" else "main")
    with pytest.raises(AppError if stage == "no-enabled" else click.Abort):
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
        old_credentials = store.get("main")
        assert old_credentials is not None
        before = config.read_bytes(), old_credentials
        if replacement_uid == 999999999:
            with patch.object(discovery, "discover", return_value=only_genshin):
                with pytest.raises(CredentialError, match="different HoYoLAB identity"):
                    run_edit_profile(
                        config,
                        secrets,
                        io,
                        FakeBrowser(),
                        discovery,
                        requested_profile="main",
                    )
            assert config.read_bytes() == before[0]
            assert store.get("main") == before[1]
            return
        with patch.object(discovery, "discover", return_value=only_genshin):
            run_edit_profile(
                config, secrets, io, FakeBrowser(), discovery, requested_profile="main"
            )
    parsed = load_config(config)
    account = parsed.profiles["main"]
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
    assert tuple(parsed.profiles["main"].games) == (game,)
    assert enabled_games(parsed, parsed.profiles["main"]) == (game,)


def test_unconfirmed_games_cannot_satisfy_the_enabled_game_requirement(
    tmp_path: Path,
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original = config.read_bytes(), secrets.read_bytes()

    class DisableLinkedIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt.startswith("Character"):
                return "Skip this game"
            return super().choose(prompt, choices)

    discovery = FakeDiscovery()
    only_genshin: dict[GameId, tuple[CharacterRef, ...]] = {
        "genshin": (CharacterRef("genshin", 999999999, "test", "New Traveler"),)
    }
    with patch.object(discovery, "discover", return_value=only_genshin):
        with pytest.raises(AppError, match="at least one enabled game"):
            run_edit_profile(
                config,
                secrets,
                DisableLinkedIO(),
                FakeBrowser(),
                discovery,
                requested_profile="main",
            )
    assert (config.read_bytes(), secrets.read_bytes()) == original


def test_reconfiguration_preserves_unrelated_accounts_and_game_metadata(
    tmp_path: Path,
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    run_add_profile(config, secrets, FakeIO(name="alt"), FakeBrowser(), FakeDiscovery())
    commit_config(prepare_default_profile(config, "alt"))
    content = config.read_text(encoding="utf-8")
    content = "# Keep root comment\n" + content.replace(
        "version = 2", 'version = 2\nunknown_root = "keep"', 1
    )
    content = content.replace(
        "[profiles.main]", '[profiles.main]\nunknown_account = "keep"', 1
    ).replace(
        "[profiles.main.games.hsr]",
        '# Keep game comment\n[profiles.main.games.hsr]\nunknown_game = "keep"',
        1,
    )
    config.write_text(content, encoding="utf-8")
    original = load_config(config)
    original_alt_secrets = PlaintextStore(secrets).get("alt")

    discovery = FakeDiscovery()
    only_genshin: dict[GameId, tuple[CharacterRef, ...]] = {
        "genshin": (CharacterRef("genshin", 999999999, "test", "New Traveler"),)
    }
    with patch.object(discovery, "discover", return_value=only_genshin):
        run_edit_profile(
            config,
            secrets,
            FakeIO(),
            FakeBrowser(),
            discovery,
            requested_profile="main",
        )
    parsed = load_config(config)
    assert parsed.default_profile == original.default_profile == "alt"
    assert parsed.game_order == original.game_order
    assert parsed.profiles["alt"] == original.profiles["alt"]
    assert PlaintextStore(secrets).get("alt") == original_alt_secrets
    assert parsed.profiles["main"].games["hsr"] == GameSelection(False, 222222222)
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
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt in ("Character for hsr", "Character for zzz"):
                return "Skip this game"
            return super().choose(prompt, choices)

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
        run_edit_profile(
            config,
            secrets,
            FakeIO(),
            FakeBrowser(),
            discovery,
            requested_profile="main",
        )
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
            return "ltuid=123; ltoken=test-replacement-secret"

    only_genshin: dict[GameId, tuple[CharacterRef, ...]] = {
        "genshin": (CharacterRef("genshin", 999999999, "test", "New Traveler"),)
    }
    with patch.object(discovery, "discover", return_value=only_genshin):
        if outcome == "cancel":
            with pytest.raises(click.Abort):
                run_edit_profile(
                    config,
                    secrets,
                    ReplacementIO(cancel="Save"),
                    FakeBrowser(),
                    discovery,
                    requested_profile="main",
                )
        else:

            def fail_commit(*args: object) -> None:
                assert PlaintextStore(secrets).get("main") == parse_cookie_header(
                    "ltuid=123; ltoken=test-replacement-secret"
                )
                raise AppError("Simulated config failure")

            with patch("hoyo_cli.commands.init.commit_config", side_effect=fail_commit):
                with pytest.raises(AppError, match="Simulated config failure"):
                    run_edit_profile(
                        config,
                        secrets,
                        ReplacementIO(),
                        FakeBrowser(),
                        discovery,
                        requested_profile="main",
                    )
    assert (config.read_bytes(), secrets.read_bytes()) == original


def test_config_change_at_confirmation_is_not_overwritten(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original_secrets = secrets.read_bytes()

    class EditingIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt.startswith("Save"):
                with config.open("a") as stream:
                    stream.write("\n# concurrent user edit\n")
            return super().choose(prompt, choices)

    with pytest.raises(AppError, match="changed"):
        run_edit_profile(
            config,
            secrets,
            EditingIO(),
            FakeBrowser(),
            FakeDiscovery(),
            requested_profile="main",
        )
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
        action: str = "Update HoYoLAB session",
    ) -> None:
        self.name, self.backend, self.browser = name, backend, browser
        self.replace, self.cancel = replace, cancel
        self.action = action
        self.prompts: list[str] = []

    def choose(self, prompt: str, choices: Sequence[str]) -> str:
        self.prompts.append(prompt)
        if self.cancel and self.cancel in prompt:
            raise click.Abort()
        if prompt == "Profile name":
            return self.name
        if prompt == "Profile settings":
            return self.action
        if prompt == "Choose login":
            return "Add new HoYoLAB login"
        if prompt == "Credential backend":
            return self.backend
        if prompt == "HoYoLAB session":
            return choices[1] if self.browser else "Paste Cookie header"
        if prompt == "Browser recovery":
            return "Paste Cookie header"
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

    def list_profiles(self, source_id: str) -> tuple[BrowserProfile, ...]:
        return (BrowserProfile(source_id, source_id, "Test profile"),)


class FakeDiscovery:
    def discover(
        self, credentials: Credentials
    ) -> dict[GameId, tuple[CharacterRef, ...]]:
        return {
            "genshin": (CharacterRef("genshin", 111111111, "test", "Traveler"),),
            "hsr": (CharacterRef("hsr", 222222222, "test", "Trailblazer"),),
            "zzz": (CharacterRef("zzz", 333333333, "test", "Proxy"),),
        }


def test_browser_verification_retry_reuses_extracted_cookies(tmp_path: Path) -> None:
    class RetryIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Browser recovery":
                return "Retry session verification"
            return super().choose(prompt, choices)

    browser = FakeBrowser()
    discovery = FakeDiscovery()
    good = discovery.discover(parse_cookie_header("ltuid=123; ltoken=test-secret-123"))
    with patch.object(
        discovery, "discover", side_effect=[AuthenticationError("Expired"), good]
    ) as verify:
        run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            RetryIO(browser=True),
            browser,
            discovery,
        )
    assert browser.imported == ["edge"]
    assert verify.call_count == 2
    assert verify.call_args_list[0].args[0] is verify.call_args_list[1].args[0]


@pytest.mark.parametrize(
    "recovery",
    ["Retry browser import", "Choose another browser/profile", "Paste Cookie header"],
)
def test_selected_browser_failure_recovers_without_restarting_account(
    tmp_path: Path, recovery: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from hoyo_cli.auth.browser_import import browser_error

    class RecoveryIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Browser recovery":
                return recovery
            if (
                prompt == "HoYoLAB session"
                and recovery == "Choose another browser/profile"
            ):
                return choices[0] if len(browser.imported) else choices[1]
            return super().choose(prompt, choices)

    class FailingBrowser(FakeBrowser):
        def import_session(self, source_id: str) -> Credentials:
            self.imported.append(source_id)
            if len(self.imported) == 1:
                raise browser_error(ErrorCode.BROWSER_LOCKED)
            return parse_cookie_header("ltuid=123; ltoken=test-secret-123")

    browser = FailingBrowser()
    io = RecoveryIO(browser=True)
    run_init(
        tmp_path / "config.toml",
        tmp_path / "credentials.toml",
        io,
        browser,
        FakeDiscovery(),
    )
    assert (
        browser.imported
        == {
            "Retry browser import": ["edge", "edge"],
            "Choose another browser/profile": ["edge", "chrome"],
            "Paste Cookie header": ["edge"],
        }[recovery]
    )
    assert io.prompts.count("Profile name") == 1
    captured = capsys.readouterr()
    assert "database is locked" in captured.err
    assert "Close the selected browser fully" in captured.err
    assert "Session verified" in captured.out
    assert "test-secret-123" not in captured.out + captured.err
    assert "\x1b" not in captured.out + captured.err


def test_switch_profile_after_rejected_session_uses_new_credentials(
    tmp_path: Path,
) -> None:
    class ProfilesBrowser(FakeBrowser):
        def list_sessions(self) -> tuple[BrowserSession, ...]:
            return (BrowserSession("edge", "Edge", True),)

        def list_profiles(self, source_id: str) -> tuple[BrowserProfile, ...]:
            return (
                BrowserProfile("edge:0", "edge", "Personal"),
                BrowserProfile("edge:1", "edge", "Work"),
            )

        def import_session(self, source_id: str) -> Credentials:
            self.imported.append(source_id)
            token = "rejected-secret" if source_id == "edge:0" else "verified-secret"
            return parse_cookie_header(f"ltuid=123; ltoken={token}")

    class ProfilesIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "HoYoLAB session":
                return choices[0]
            if prompt == "Browser profile":
                return choices[1] if browser.imported else choices[0]
            if prompt == "Browser recovery":
                return "Choose another browser/profile"
            return super().choose(prompt, choices)

    browser = ProfilesBrowser()
    discovery = FakeDiscovery()
    seen: list[Credentials] = []

    def verify(credentials: Credentials) -> dict[GameId, tuple[CharacterRef, ...]]:
        seen.append(credentials)
        if credentials.fields["ltoken"] == "rejected-secret":
            raise AuthenticationError("Session rejected.")
        return FakeDiscovery().discover(credentials)

    with patch.object(discovery, "discover", side_effect=verify):
        run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            ProfilesIO(browser=True),
            browser,
            discovery,
        )
    assert browser.imported == ["edge:0", "edge:1"]
    assert len(seen) == 2 and seen[0] is not seen[1]
    stored = PlaintextStore(tmp_path / "credentials.toml").get("main")
    assert stored and stored.fields["ltoken"] == "verified-secret"


@pytest.mark.parametrize("stage", ["read", "verify", "recovery"])
@pytest.mark.parametrize(
    "signal", [click.Abort, KeyboardInterrupt, asyncio.CancelledError]
)
def test_browser_cancellation_preserves_existing_files(
    tmp_path: Path, stage: str, signal: type[BaseException]
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    before = config.read_bytes(), secrets.read_bytes()

    class CancelIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if stage == "recovery" and prompt == "Browser recovery":
                raise signal()
            return super().choose(prompt, choices)

    browser, discovery = FakeBrowser(), FakeDiscovery()
    with (
        patch.object(
            browser,
            "import_session",
            side_effect=signal()
            if stage == "read"
            else (BrowserImportError("Read failed") if stage == "recovery" else None),
            return_value=parse_cookie_header("ltuid=123; ltoken=test-secret-123"),
        ),
        patch.object(
            discovery,
            "discover",
            side_effect=signal() if stage == "verify" else None,
        ),
        patch("hoyo_cli.commands.init.open_store") as storage,
    ):
        with pytest.raises(signal):
            run_edit_profile(
                config,
                secrets,
                CancelIO(browser=True),
                browser,
                discovery,
                requested_profile="main",
            )
        storage.assert_not_called()
    assert (config.read_bytes(), secrets.read_bytes()) == before


def test_recovery_cancel_and_declined_save_never_persist_browser_credentials(
    tmp_path: Path,
) -> None:
    class CancelIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Browser recovery":
                return "Cancel (no changes)"
            return super().choose(prompt, choices)

    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    browser = FakeBrowser()
    with patch.object(
        browser, "import_session", side_effect=BrowserImportError("Read failed")
    ):
        with pytest.raises(click.Abort):
            run_init(config, secrets, CancelIO(browser=True), browser, FakeDiscovery())
    assert not config.exists() and not secrets.exists()
    with pytest.raises(click.Abort):
        run_init(
            config,
            secrets,
            FakeIO(browser=True, cancel="Save profile"),
            FakeBrowser(),
            FakeDiscovery(),
        )
    assert not config.exists() and not secrets.exists()


@pytest.mark.parametrize("width", [48, 70, 100])
def test_slow_browser_work_shows_stages_and_clears_before_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    width: int,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(progress, "PRESENTATION_DELAY", 0.005)
    stream = stream_io.StringIO()

    class PromptIO(FakeIO):
        def check_stopped(self) -> None:
            text = stream.getvalue()
            assert text.rfind("\x1b[?25h") >= text.rfind("\x1b[?25l")

        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            self.check_stopped()
            return super().choose(prompt, choices)

        def confirm(self, prompt: str) -> bool:
            self.check_stopped()
            return super().confirm(prompt)

        def secret(self, prompt: str) -> str:
            self.check_stopped()
            return super().secret(prompt)

    class SlowBrowser(FakeBrowser):
        def import_session(self, source_id: str) -> Credentials:
            time.sleep(0.03)
            return super().import_session(source_id)

    class SlowDiscovery(FakeDiscovery):
        def discover(
            self, credentials: Credentials
        ) -> dict[GameId, tuple[CharacterRef, ...]]:
            time.sleep(0.03)
            return super().discover(credentials)

    with patch.object(
        progress,
        "make_console",
        return_value=Console(file=stream, force_terminal=True, width=width),
    ):
        run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            PromptIO(browser=True),
            SlowBrowser(),
            SlowDiscovery(),
        )
    output = stream.getvalue()
    assert "Reading browser cookies" in output
    assert "Verifying HoYoLAB session" in output
    assert "games complete" not in output and "%" not in output
    assert output.endswith("\x1b[2K")


def test_initial_setup_one_uid_per_game(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    io = FakeIO()
    run_init(config, secrets, io, FakeBrowser(), FakeDiscovery())
    parsed = load_config(config)
    assert parsed.default_profile == "main"
    assert list(parsed.profiles["main"].games) == ["genshin", "hsr", "zzz"]
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
    assert load_config(config).default_profile == "Exact.Account"
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
    original = load_config(config).profiles["alt"]
    with config.open("a") as stream:
        stream.write('\n# user comment\n[custom]\nmarker="keep"\n')
    run_add_profile(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    assert load_config(config).profiles["alt"] == original
    assert "# user comment" in config.read_text()
    assert 'marker="keep"' in config.read_text()


def test_existing_credentials_not_silently_replaced(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original = config.read_bytes(), secrets.read_bytes()
    with pytest.raises(click.Abort):
        run_edit_profile(
            config,
            secrets,
            FakeIO(replace=False),
            FakeBrowser(),
            FakeDiscovery(),
            requested_profile="main",
        )
    assert (config.read_bytes(), secrets.read_bytes()) == original


@pytest.mark.parametrize("cancel", ["Profile", "HoYoLAB session", "Save"])
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
            if existing:
                run_edit_profile(
                    config,
                    secrets,
                    FakeIO(),
                    FakeBrowser(),
                    FakeDiscovery(),
                    requested_profile="main",
                )
            else:
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
    assert discovered["zzz"][0].label == "Test character (Test region)"
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
            if prompt == "Character for hsr":
                return "Skip this game"
            return super().choose(prompt, choices)

        def confirm(self, prompt: str) -> bool:
            if prompt == "Use the default game display order?":
                return False
            return super().confirm(prompt)

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
    assert parsed.profiles["main"].games["genshin"].uid == 111111112
    assert "hsr" not in parsed.profiles["main"].games


def test_explicit_storage_change_copies_saved_session_and_keeps_old_secrets(
    tmp_path: Path,
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original = secrets.read_bytes()
    store = KeyringStore(MemoryKeyring())
    with patch(
        "hoyo_cli.commands.init.open_store",
        side_effect=[PlaintextStore(secrets), store],
    ):
        run_edit_profile(
            config,
            secrets,
            FakeIO(backend="keyring", action="Credential storage"),
            FakeBrowser(),
            FakeDiscovery(),
            requested_profile="main",
        )
    assert load_config(config).logins["main"].credential_backend == "keyring"
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
            run_edit_profile(
                config,
                secrets,
                FakeIO(),
                FakeBrowser(),
                FakeDiscovery(),
                requested_profile="main",
            )
    assert (config.read_bytes(), secrets.read_bytes()) == original


def test_declining_save_preserves_existing_files(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original = config.read_bytes(), secrets.read_bytes()

    class DeclineSaveIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt.startswith("Save"):
                return "Cancel (no changes)"
            return super().choose(prompt, choices)

    with pytest.raises(click.Abort):
        run_edit_profile(
            config,
            secrets,
            DeclineSaveIO(),
            FakeBrowser(),
            FakeDiscovery(),
            requested_profile="main",
        )
    assert (config.read_bytes(), secrets.read_bytes()) == original


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("color_policy", ["redirected", "no-color", "NO_COLOR"])
def test_guided_setup_labels_identity_and_reviews_before_write(
    tmp_path: Path, runner: CliRunner, width: int, color_policy: str
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    discovery = FakeDiscovery()
    roles: dict[GameId, tuple[CharacterRef, ...]] = {
        "genshin": (
            CharacterRef("genshin", 111111111, "os_asia", "Traveler (Asia)"),
            CharacterRef("genshin", 111111112, "os_usa", "Traveler (America)"),
        ),
        "hsr": (CharacterRef("hsr", 222222222, "prod_official_asia", "Stelle (Asia)"),),
    }
    # Account, manual session, second Genshin role, HSR role, default order,
    # plaintext consent, then Enter to save the reviewed candidate.
    answers = "main\nn\nltuid=123; ltoken=test-secret-123\n2\n1\ny\n2\ny\n\n"
    with (
        patch("hoyo_cli.commands.init.config_path", return_value=config),
        patch("hoyo_cli.commands.init.credentials_path", return_value=secrets),
        patch("hoyo_cli.commands.init.HoYoLABDiscovery", return_value=discovery),
        patch.object(discovery, "discover", return_value=roles),
    ):
        result = runner.invoke(
            main,
            ["init", "--no-color"] if color_policy == "no-color" else ["init"],
            input=answers,
            env={
                "COLUMNS": str(width),
                "NO_COLOR": "1" if color_policy == "NO_COLOR" else None,
            },
        )
    assert result.exit_code == 0, result.output
    for step, label in enumerate(("Profile", "Games", "Storage", "Review"), 1):
        assert f"Step {step}/4" in result.output and label in result.output
    assert result.output.index("GENSHIN IMPACT") < result.output.index("Traveler")
    assert result.output.index("HONKAI: STAR RAIL") < result.output.index("Stelle")
    assert "Profile: main" in result.output
    assert "Asia" in result.output and "America" in result.output
    assert "os_asia" not in result.output and "prod_official_asia" not in result.output
    assert "Skip this game" in result.output and "Enable genshin?" not in result.output
    assert "No linked characters" in result.output
    review = result.output.split("Step 4/4", 1)[1]
    assert "UID 111111112" in review and "America" in review
    assert "plaintext" in review and "Default profile: main" in review
    assert "Game order:" in review and "Cancel (no changes)" in review
    assert "test-secret-123" not in result.output and "\x1b" not in result.output
    parsed = load_config(config)
    assert parsed.profiles["main"].games["genshin"].uid == 111111112
    assert len(parsed.profiles["main"].games) == 2


def test_skipping_game_does_not_select_or_enable_a_character(tmp_path: Path) -> None:
    class SkipIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Character for hsr":
                assert "Skip this game" in choices
                return "Skip this game"
            return super().choose(prompt, choices)

    config = tmp_path / "config.toml"
    io = SkipIO()
    run_init(config, tmp_path / "credentials.toml", io, FakeBrowser(), FakeDiscovery())
    account = load_config(config).profiles["main"]
    assert "hsr" not in account.games
    assert account.games["genshin"].enabled and account.games["zzz"].enabled
    assert not any(prompt.startswith("Enable") for prompt in io.prompts)


def test_narrow_review_keeps_short_character_and_uid_together(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from hoyo_cli.render import setup

    console = Console(width=48, color_system=None, markup=False, highlight=False)
    discovery = FakeDiscovery()
    characters = discovery.discover(
        parse_cookie_header("ltuid=123; ltoken=test-secret-123")
    )
    characters["zzz"] = (CharacterRef("zzz", 333333333, "test", "Proxy (Asia)"),)
    with (
        patch.object(setup, "make_console", return_value=console),
        patch.object(discovery, "discover", return_value=characters),
    ):
        run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            FakeIO(),
            FakeBrowser(),
            discovery,
        )
    review = capsys.readouterr().out.split("Step 4/4", 1)[1]
    assert "Proxy (Asia) | UID 333333333" in review


@pytest.mark.parametrize("raw", ["", "ltoken=test-secret-123", "invalid-secret"])
def test_manual_cookie_input_recovers_locally_without_discovery_on_bad_input(
    tmp_path: Path, raw: str, capsys: pytest.CaptureFixture[str]
) -> None:
    class RetryIO(FakeIO):
        def secret(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return raw if self.prompts.count(prompt) == 1 else super().secret(prompt)

        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Session recovery":
                return "Paste Cookie header again"
            return super().choose(prompt, choices)

    io, discovery = RetryIO(), FakeDiscovery()
    with patch.object(discovery, "discover", wraps=discovery.discover) as verify:
        run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            io,
            FakeBrowser(),
            discovery,
        )
    assert verify.call_count == 1 and io.prompts.count("Profile name") == 1
    output = capsys.readouterr()
    assert "Error:" in output.err
    assert "test-secret-123" not in output.out + output.err
    assert "invalid-secret" not in output.out + output.err


@pytest.mark.parametrize(
    "failure",
    [
        AuthenticationError("Expired"),
        NetworkError("Timed out"),
        {},
    ],
)
def test_manual_discovery_retry_keeps_account_and_in_memory_session(
    tmp_path: Path, failure: object
) -> None:
    class RetryIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Session recovery":
                return "Retry session verification"
            return super().choose(prompt, choices)

    discovery, io = FakeDiscovery(), RetryIO()
    good = discovery.discover(parse_cookie_header("ltuid=123; ltoken=test-secret-123"))
    with patch.object(discovery, "discover", side_effect=[failure, good]) as verify:
        run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            io,
            FakeBrowser(),
            discovery,
        )
    assert verify.call_count == 2
    assert verify.call_args_list[0].args[0] is verify.call_args_list[1].args[0]
    assert io.prompts.count("Full HoYoLAB Cookie header (hidden)") == 1
    assert io.prompts.count("Profile name") == 1


def test_final_review_precedes_writes_and_explicit_cancel_has_no_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class ReviewIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt.startswith("Save"):
                output = capsys.readouterr().out
                assert "Step 4/4" in output and "UID 333333333" in output
                assert "Credential storage: plaintext" in output
                assert not config.exists() and not secrets.exists()
                return "Cancel (no changes)"
            return super().choose(prompt, choices)

    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    with patch("hoyo_cli.commands.init.commit_config") as commit:
        with pytest.raises(click.Abort):
            run_init(config, secrets, ReviewIO(), FakeBrowser(), FakeDiscovery())
    commit.assert_not_called()
    assert not config.exists() and not secrets.exists()
    assert "Setup cancelled; no changes saved." in capsys.readouterr().out


@pytest.mark.parametrize(
    "stage", ["Character for genshin", "Credential backend", "Save"]
)
def test_forged_setup_selection_is_rejected_without_writes(
    tmp_path: Path, stage: str
) -> None:
    class ForgedIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt.startswith(stage):
                return "forged-out-of-menu"
            return super().choose(prompt, choices)

    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    with pytest.raises(AppError, match="Invalid setup selection"):
        run_init(config, secrets, ForgedIO(), FakeBrowser(), FakeDiscovery())
    assert not config.exists() and not secrets.exists()


def test_rejected_manual_session_can_change_source_without_restarting_account(
    tmp_path: Path,
) -> None:
    class SourceIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Session recovery":
                return "Change credential source"
            return super().choose(prompt, choices)

        def confirm(self, prompt: str) -> bool:
            if prompt.startswith("Import"):
                self.prompts.append(prompt)
                return self.prompts.count(prompt) == 2
            return super().confirm(prompt)

        def secret(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "ltuid=456; ltoken=rejected-secret"

    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    io, discovery, browser = SourceIO(browser=True), FakeDiscovery(), FakeBrowser()
    good = discovery.discover(parse_cookie_header("ltuid=123; ltoken=test-secret-123"))
    with patch.object(
        discovery, "discover", side_effect=[AuthenticationError("Expired"), good]
    ):
        run_init(config, secrets, io, browser, discovery)
    assert browser.imported == ["edge"]
    assert io.prompts.count("Profile name") == 1
    stored = PlaintextStore(secrets).get("main")
    assert stored and stored.fields["ltuid"] == "123"


@pytest.mark.parametrize(
    "signal", [click.Abort, KeyboardInterrupt, asyncio.CancelledError]
)
def test_manual_recovery_cancellation_preserves_existing_bytes(
    tmp_path: Path, signal: type[BaseException]
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    before = config.read_bytes(), secrets.read_bytes()

    class CancelIO(FakeIO):
        def secret(self, prompt: str) -> str:
            return ""

        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Session recovery":
                raise signal()
            return super().choose(prompt, choices)

    with patch("hoyo_cli.commands.init.open_store") as storage:
        with pytest.raises(signal):
            run_edit_profile(
                config,
                secrets,
                CancelIO(),
                FakeBrowser(),
                FakeDiscovery(),
                requested_profile="main",
            )
    storage.assert_not_called()
    assert (config.read_bytes(), secrets.read_bytes()) == before


def test_invalid_account_name_can_be_corrected_before_session_input(
    tmp_path: Path,
) -> None:
    class NameIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Profile name":
                self.prompts.append(prompt)
                return "\x1bwrong" if self.prompts.count(prompt) == 1 else "corrected"
            return super().choose(prompt, choices)

    config = tmp_path / "config.toml"
    run_init(
        config, tmp_path / "credentials.toml", NameIO(), FakeBrowser(), FakeDiscovery()
    )
    assert load_config(config).default_profile == "corrected"


def test_review_decline_does_not_put_credentials_in_keyring(tmp_path: Path) -> None:
    class DeclineIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt.startswith("Save"):
                return "Cancel (no changes)"
            return super().choose(prompt, choices)

    store = KeyringStore(MemoryKeyring())
    with (
        patch("hoyo_cli.commands.init.open_store", return_value=store),
        patch.object(store, "put") as put,
        patch.object(store, "checkpoint") as checkpoint,
    ):
        with pytest.raises(click.Abort):
            run_init(
                tmp_path / "config.toml",
                tmp_path / "credentials.toml",
                DeclineIO(backend="keyring"),
                FakeBrowser(),
                FakeDiscovery(),
            )
    put.assert_not_called()
    checkpoint.assert_not_called()


def test_backend_change_needs_separate_sensitive_consent(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    before = config.read_bytes(), secrets.read_bytes()

    class DeclineIO(FakeIO):
        def confirm(self, prompt: str) -> bool:
            if (
                prompt
                == "Change login credential backend for all linked profiles (main)?"
            ):
                return False
            return super().confirm(prompt)

    with patch(
        "hoyo_cli.commands.init.open_store", return_value=KeyringStore(MemoryKeyring())
    ):
        with pytest.raises(click.Abort):
            run_edit_profile(
                config,
                secrets,
                DeclineIO(backend="keyring", action="Credential storage"),
                FakeBrowser(),
                FakeDiscovery(),
                requested_profile="main",
            )
    assert (config.read_bytes(), secrets.read_bytes()) == before


@pytest.mark.parametrize("action", ["plaintext", "overwrite"])
def test_sensitive_consent_defaults_to_no_in_real_command(
    tmp_path: Path, runner: CliRunner, action: str
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    if action == "overwrite":
        run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    before = (
        config.read_bytes() if config.exists() else None,
        secrets.read_bytes() if secrets.exists() else None,
    )
    answers = (
        "3\n\n"
        if action == "overwrite"
        else ("main\nn\nltuid=123; ltoken=test-secret-123\n\n\n\n\n2\n\n")
    )
    with (
        patch("hoyo_cli.commands.init.config_path", return_value=config),
        patch("hoyo_cli.commands.init.credentials_path", return_value=secrets),
        patch("hoyo_cli.commands.init.HoYoLABDiscovery", return_value=FakeDiscovery()),
    ):
        result = runner.invoke(
            main,
            ["profile", "edit", "main"] if action == "overwrite" else ["init"],
            input=answers,
        )
    assert result.exit_code == 1, result.output
    assert (
        "[y/N]" in result.output
        and "Setup cancelled; no changes saved." in result.output
    )
    assert "Configured profile" not in result.output
    assert (
        config.read_bytes() if config.exists() else None,
        secrets.read_bytes() if secrets.exists() else None,
    ) == before


class SettingsIO(FakeIO):
    def __init__(self, *, action: str, change_game: str = "", cancel: str = "") -> None:
        super().__init__(action=action, cancel=cancel)
        self.change_game = change_game

    def choose(self, prompt: str, choices: Sequence[str]) -> str:
        if prompt == "Game settings for hsr":
            return self.change_game or choices[0]
        return super().choose(prompt, choices)

    def confirm(self, prompt: str) -> bool:
        if prompt.startswith("Replace login"):
            raise AssertionError("Settings must not request credential replacement")
        return super().confirm(prompt)


def test_local_settings_never_access_credentials_browser_or_hoyolab(
    tmp_path: Path,
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    run_add_profile(config, secrets, FakeIO(name="alt"), FakeBrowser(), FakeDiscovery())
    saved = load_config(config)
    save_profile(
        config,
        "main",
        saved.profiles["main"],
        make_default=True,
        game_order=("zzz", "hsr"),
    )
    config.write_text(
        "# Keep settings comment\ncustom = 'keep'\n" + config.read_text(),
        encoding="utf-8",
    )
    original = load_config(config)
    before_secrets = secrets.read_bytes()
    browser, discovery = FakeBrowser(), FakeDiscovery()
    io = SettingsIO(action="Games and characters", change_game="Disable this game")
    with (
        patch(
            "hoyo_cli.commands.init.open_store",
            side_effect=BackendUnavailable("Unavailable"),
        ) as store,
        patch.object(browser, "list_sessions") as sources,
        patch.object(browser, "import_session") as imported,
        patch.object(discovery, "discover") as discover,
    ):
        run_edit_profile(
            config, secrets, io, browser, discovery, requested_profile="main"
        )
    store.assert_not_called()
    sources.assert_not_called()
    imported.assert_not_called()
    discover.assert_not_called()
    updated = load_config(config)
    assert updated.profiles["main"].games["hsr"] == GameSelection(False, 222222222)
    assert updated.profiles["alt"] == original.profiles["alt"]
    assert "# Keep settings comment" in config.read_text()
    assert "custom = 'keep'" in config.read_text()
    assert updated.default_profile == original.default_profile == "main"
    assert updated.game_order == original.game_order == ("zzz", "hsr")
    assert secrets.read_bytes() == before_secrets
    assert not any("Replace" in prompt or "Import" in prompt for prompt in io.prompts)


@pytest.mark.parametrize("initially_enabled", [False, True])
def test_character_changes_verify_saved_session_without_resaving_secrets(
    tmp_path: Path, initially_enabled: bool
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original = load_config(config)
    games = dict(original.profiles["main"].games)
    games["hsr"] = GameSelection(initially_enabled, 222222222)
    save_profile(
        config,
        "main",
        ProfileSeed("plaintext", games),
        make_default=False,
        game_order=original.game_order,
    )
    before_secrets = secrets.read_bytes()
    store, browser, discovery = PlaintextStore(secrets), FakeBrowser(), FakeDiscovery()
    character = CharacterRef("hsr", 999999999, "test", "New Trailblazer")
    io = SettingsIO(
        action="Games and characters", change_game="Choose linked character"
    )
    with (
        patch("hoyo_cli.commands.init.open_store", return_value=store),
        patch.object(store, "get", wraps=store.get) as get,
        patch.object(store, "put") as put,
        patch.object(store, "checkpoint") as checkpoint,
        patch.object(
            discovery, "discover", return_value={"hsr": (character,)}
        ) as discover,
    ):
        run_edit_profile(
            config, secrets, io, browser, discovery, requested_profile="main"
        )
    get.assert_called_once_with("main")
    discover.assert_called_once_with(
        parse_cookie_header("ltuid=123; ltoken=test-secret-123")
    )
    put.assert_not_called()
    checkpoint.assert_not_called()
    assert browser.listed == 0 and browser.imported == []
    updated = load_config(config)
    assert updated.profiles["main"].games["hsr"] == GameSelection(True, character.uid)
    assert updated.profiles["main"].games["genshin"] == games["genshin"]
    assert updated.profiles["main"].games["zzz"] == games["zzz"]
    assert updated.default_profile == original.default_profile
    assert updated.game_order == original.game_order
    assert secrets.read_bytes() == before_secrets


@pytest.mark.parametrize(
    "failure", ["missing", "unavailable", "expired", "unlinked", "forged", "cancel"]
)
def test_failed_or_cancelled_character_change_preserves_config_and_secrets(
    tmp_path: Path, failure: str
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    original = config.read_bytes(), secrets.read_bytes()
    discovery, store = FakeDiscovery(), PlaintextStore(secrets)

    class InvalidSettingsIO(SettingsIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if failure == "cancel" and prompt.startswith("Save"):
                return "Cancel (no changes)"
            if failure == "forged" and prompt == "Character for hsr":
                return "Retained UID 222222222"
            return super().choose(prompt, choices)

    io = InvalidSettingsIO(
        action="Games and characters", change_game="Choose linked character"
    )
    with (
        patch("hoyo_cli.commands.init.open_store", return_value=store),
        patch.object(store, "get", wraps=store.get) as get,
        patch.object(store, "put") as put,
        patch.object(discovery, "discover", wraps=discovery.discover) as discover,
    ):
        if failure == "missing":
            get.return_value, get.side_effect = None, None
        elif failure == "unavailable":
            get.side_effect = BackendUnavailable("Unavailable")
        elif failure == "expired":
            discover.side_effect = AuthenticationError("Expired")
        elif failure == "unlinked":
            discover.return_value = {}
        with pytest.raises(click.Abort if failure == "cancel" else AppError):
            run_edit_profile(
                config, secrets, io, FakeBrowser(), discovery, requested_profile="main"
            )
    put.assert_not_called()
    if failure in ("missing", "unavailable"):
        discover.assert_not_called()
    assert (config.read_bytes(), secrets.read_bytes()) == original


def test_local_settings_cancel_and_config_failure_do_not_touch_secret_store(
    tmp_path: Path,
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    run_add_profile(config, secrets, FakeIO(name="alt"), FakeBrowser(), FakeDiscovery())
    before = config.read_bytes(), secrets.read_bytes()
    with patch("hoyo_cli.commands.init.open_store") as store:
        with pytest.raises(click.Abort):
            run_edit_profile(
                config,
                secrets,
                SettingsIO(
                    action="Games and characters",
                    change_game="Disable this game",
                    cancel="Save",
                ),
                FakeBrowser(),
                FakeDiscovery(),
                requested_profile="main",
            )
        with patch(
            "hoyo_cli.commands.init.commit_config",
            side_effect=AppError("Write failure"),
        ):
            with pytest.raises(AppError, match="Write failure"):
                run_edit_profile(
                    config,
                    secrets,
                    SettingsIO(
                        action="Games and characters", change_game="Disable this game"
                    ),
                    FakeBrowser(),
                    FakeDiscovery(),
                    requested_profile="main",
                )
    store.assert_not_called()
    assert (config.read_bytes(), secrets.read_bytes()) == before


def test_multiple_character_changes_share_one_discovery(
    tmp_path: Path,
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    before_secrets = secrets.read_bytes()
    discovery, store = FakeDiscovery(), PlaintextStore(secrets)
    linked = {
        game: (CharacterRef(game, index + 900000001, "test", f"New {game}"),)
        for index, game in enumerate(GAME_IDS)
    }

    class ChangeAllIO(SettingsIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt.startswith("Game settings"):
                return "Choose linked character"
            return super().choose(prompt, choices)

    with (
        patch("hoyo_cli.commands.init.open_store", return_value=store),
        patch.object(store, "get", wraps=store.get) as get,
        patch.object(store, "put") as put,
        patch.object(discovery, "discover", return_value=linked) as discover,
    ):
        run_edit_profile(
            config,
            secrets,
            ChangeAllIO(action="Games and characters"),
            FakeBrowser(),
            discovery,
            requested_profile="main",
        )
    get.assert_called_once_with("main")
    discover.assert_called_once()
    put.assert_not_called()
    updated = load_config(config).profiles["main"]
    assert {game: selection.uid for game, selection in updated.games.items()} == {
        game: characters[0].uid for game, characters in linked.items()
    }
    assert secrets.read_bytes() == before_secrets


def test_disabling_last_enabled_game_is_rejected_locally(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    saved = load_config(config)
    save_profile(
        config,
        "main",
        ProfileSeed("plaintext", {"hsr": GameSelection(True, 222222222)}),
        make_default=True,
        game_order=saved.game_order,
    )
    before = config.read_bytes(), secrets.read_bytes()
    with patch("hoyo_cli.commands.init.open_store") as store:
        with pytest.raises(AppError, match="at least one enabled game"):
            run_edit_profile(
                config,
                secrets,
                SettingsIO(
                    action="Games and characters", change_game="Disable this game"
                ),
                FakeBrowser(),
                FakeDiscovery(),
                requested_profile="main",
            )
    store.assert_not_called()
    assert (config.read_bytes(), secrets.read_bytes()) == before


@pytest.mark.parametrize("outcome", ["cancel", "write-failure", "interrupt", "save"])
def test_explicit_storage_change_has_consent_review_and_transactional_save(
    tmp_path: Path, outcome: str, capsys: pytest.CaptureFixture[str]
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    before = config.read_bytes(), secrets.read_bytes()
    source, target = PlaintextStore(secrets), KeyringStore(MemoryKeyring())
    target.put("main", parse_cookie_header("ltuid=456; ltoken=old-target-secret"))
    old_target = target.get("main")
    browser, discovery = FakeBrowser(), FakeDiscovery()
    capsys.readouterr()

    class StorageIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt.startswith("Save"):
                output = capsys.readouterr().out
                assert (
                    "Review changes" in output
                    and "Credential storage: keyring" in output
                )
                assert target.get("main") == old_target
                if outcome == "cancel":
                    return "Cancel (no changes)"
            return super().choose(prompt, choices)

    io = StorageIO(action="Credential storage", backend="keyring")
    with (
        patch("hoyo_cli.commands.init.open_store", side_effect=[source, target]),
        patch.object(browser, "list_sessions") as listed,
        patch.object(discovery, "discover") as discover,
        patch("hoyo_cli.commands.init.commit_config", wraps=commit_config) as commit,
    ):
        if outcome == "write-failure":
            commit.side_effect = AppError("Write failure")
        elif outcome == "interrupt":
            commit.side_effect = KeyboardInterrupt()
        if outcome == "save":
            run_edit_profile(
                config, secrets, io, browser, discovery, requested_profile="main"
            )
        else:
            with pytest.raises(
                click.Abort
                if outcome == "cancel"
                else KeyboardInterrupt
                if outcome == "interrupt"
                else AppError
            ):
                run_edit_profile(
                    config, secrets, io, browser, discovery, requested_profile="main"
                )
    listed.assert_not_called()
    discover.assert_not_called()
    assert (
        "Change login credential backend for all linked profiles (main)?" in io.prompts
    )
    assert "Replace existing stored credentials in the selected backend?" in io.prompts
    assert secrets.read_bytes() == before[1]
    if outcome == "save":
        assert target.get("main") == source.get("main")
        assert load_config(config).logins["main"].credential_backend == "keyring"
    else:
        assert target.get("main") == old_target
        assert config.read_bytes() == before[0]


@pytest.mark.parametrize("stage", ["Profile settings", "Game settings for hsr"])
def test_forged_existing_settings_selection_never_persists(
    tmp_path: Path, stage: str
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    before = config.read_bytes(), secrets.read_bytes()

    class ForgedIO(SettingsIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            return (
                "not-a-choice" if prompt == stage else super().choose(prompt, choices)
            )

    action = "Games and characters"
    with patch("hoyo_cli.commands.init.open_store") as store:
        with pytest.raises(AppError, match="Invalid setup selection"):
            run_edit_profile(
                config,
                secrets,
                ForgedIO(action=action),
                FakeBrowser(),
                FakeDiscovery(),
                requested_profile="main",
            )
    store.assert_not_called()
    assert (config.read_bytes(), secrets.read_bytes()) == before


@pytest.mark.parametrize("width", [48, 70, 100])
def test_cli_local_settings_work_with_unavailable_keyring_and_no_browser_reads(
    tmp_path: Path,
    runner: CliRunner,
    width: int,
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    run_add_profile(config, secrets, FakeIO(name="alt"), FakeBrowser(), FakeDiscovery())
    saved = load_config(config)
    save_profile(
        config,
        "main",
        ProfileSeed("keyring", saved.profiles["main"].games),
        make_default=True,
        game_order=GAME_IDS,
    )
    before_secrets = secrets.read_bytes()
    browser, discovery = FakeBrowser(), FakeDiscovery()

    def console(**kwargs: object) -> Console:
        return Console(width=width, color_system=None)

    with (
        patch("hoyo_cli.commands.init.config_path", return_value=config),
        patch("hoyo_cli.commands.init.credentials_path", return_value=secrets),
        patch("hoyo_cli.commands.init.SystemBrowserImporter", return_value=browser),
        patch("hoyo_cli.commands.init.HoYoLABDiscovery", return_value=discovery),
        patch(
            "hoyo_cli.commands.init.open_store",
            side_effect=BackendUnavailable("Unavailable"),
        ) as store,
        patch.object(discovery, "discover") as discover,
        patch(
            "hoyo_cli.render.setup.make_console",
            side_effect=console,
        ),
    ):
        result = runner.invoke(
            main,
            ["profile", "edit", "main", "--no-color"],
            input="1\n1\n2\n1\n1\n",
            terminal_width=width,
        )
    assert result.exit_code == 0, result.output
    assert (
        "Review changes" in result.output and "Configured profile main" in result.output
    )
    assert "test-secret-123" not in result.output and "\x1b" not in result.output
    assert "Replace login credentials" not in result.output
    store.assert_not_called()
    discover.assert_not_called()
    assert browser.listed == 0 and browser.imported == []
    updated = load_config(config)
    assert updated.profiles["main"].games["hsr"] == GameSelection(False, 222222222)
    assert updated.logins["main"].credential_backend == "keyring"
    assert secrets.read_bytes() == before_secrets
