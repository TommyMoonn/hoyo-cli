"""Two private cookie values share the existing verification and save boundary."""

from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner
from test_init import FakeBrowser, FakeDiscovery, FakeIO
from test_profiles import seed_shared

from hoyo_cli.auth.credentials import Credentials, manual_credentials
from hoyo_cli.auth.plaintext_backend import PlaintextStore
from hoyo_cli.cli import main
from hoyo_cli.commands.init import run_add_profile, run_edit_profile, run_init
from hoyo_cli.config.repository import load_config
from hoyo_cli.errors import AppError, AuthenticationError, CredentialError, ErrorCode

ID_PROMPT = "ltuid_v2 (HoYoLAB account ID, hidden)"
TOKEN_PROMPT = "ltoken_v2 (session token, hidden)"
MANUAL = "Enter HoYoLAB cookie values (recommended)"
BROWSER = "Import from browser (best effort)"
REENTER = "Enter ltuid_v2 / ltoken_v2 again"


@pytest.mark.parametrize(
    "identifier", ["", " ", "0", "000", "-1", "+1", "1.5", "12x", "１２", "1 2"]
)
def test_manual_id_validation_is_local_and_redacted(identifier: str) -> None:
    with pytest.raises(CredentialError) as error:
        manual_credentials(identifier, "synthetic-token")
    assert "synthetic-token" not in str(error.value)


@pytest.mark.parametrize(
    "token", ["", " ", "has space", "x;y", "x\ny", "x\x00y", "x\x7fy"]
)
def test_manual_token_validation_is_local_and_redacted(token: str) -> None:
    with pytest.raises(CredentialError) as error:
        manual_credentials("987654321", token)
    assert "987654321" not in str(error.value)
    assert "has space" not in str(error.value)


def test_manual_pair_trims_edges_and_preserves_internal_token_characters() -> None:
    credentials = manual_credentials(" \t987654321\r\n", " \ttest-token/+== \n")
    assert dict(credentials.fields) == {
        "ltuid_v2": "987654321",
        "ltoken_v2": "test-token/+==",
    }
    assert "987654321" not in repr(credentials)
    assert "test-token" not in repr(credentials)


def test_decimal_identity_validation_does_not_depend_on_integer_parsing() -> None:
    identifier = "9" * 5000
    credentials = manual_credentials(identifier, "synthetic-token")
    assert credentials.same_identity(manual_credentials("00" + identifier, "new-token"))
    with pytest.raises(CredentialError):
        manual_credentials("0" * 5000, "synthetic-token")


@pytest.mark.parametrize("mode", ["first", "add", "edit"])
def test_all_login_entrypoints_request_two_values_and_verify_before_save(
    tmp_path: Path, mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    before: tuple[bytes, bytes] | None = None
    if mode != "first":
        config, secrets = seed_shared(tmp_path)
        before = config.read_bytes(), secrets.read_bytes()
    prompts = FakeIO(name="new-profile")
    discovery = FakeDiscovery()
    expected = Credentials({"ltuid_v2": "123", "ltoken_v2": "test-secret-123"})

    def verify(credentials: Credentials) -> object:
        assert credentials == expected
        if before:
            assert (config.read_bytes(), secrets.read_bytes()) == before
        else:
            assert not config.exists() and not secrets.exists()
        return FakeDiscovery().discover(credentials)

    with patch.object(discovery, "discover", side_effect=verify) as call:
        if mode == "first":
            run_init(config, secrets, prompts, FakeBrowser(), discovery)
        elif mode == "add":
            run_add_profile(
                config,
                secrets,
                prompts,
                FakeBrowser(),
                discovery,
                requested_profile="new-profile",
            )
        else:
            run_edit_profile(
                config,
                secrets,
                prompts,
                FakeBrowser(),
                discovery,
                requested_profile="main",
            )
    call.assert_called_once()
    assert [p for p in prompts.prompts if p in (ID_PROMPT, TOKEN_PROMPT)] == [
        ID_PROMPT,
        TOKEN_PROMPT,
    ]
    current = load_config(config)
    profile_name = "main" if mode == "edit" else "new-profile"
    key = current.logins[current.profiles[profile_name].login].credential_key
    assert PlaintextStore(secrets).get(key) == expected
    output = capsys.readouterr()
    assert "test-secret-123" not in output.out + output.err
    assert "Cookie header" not in output.out + output.err


def test_reentry_replaces_both_values_after_rejected_session(tmp_path: Path) -> None:
    class ReenterIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Session recovery":
                return REENTER
            return super().choose(prompt, choices)

        def secret(self, prompt: str) -> str:
            self.prompts.append(prompt)
            first = self.prompts.count(prompt) == 1
            if prompt == ID_PROMPT:
                return "456" if first else "123"
            return "rejected-token" if first else "test-secret-123"

    prompts, discovery = ReenterIO(), FakeDiscovery()
    accepted = Credentials({"ltuid_v2": "123", "ltoken_v2": "test-secret-123"})
    with patch.object(
        discovery,
        "discover",
        side_effect=[
            AuthenticationError("Session rejected."),
            FakeDiscovery().discover(accepted),
        ],
    ) as verify:
        run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            prompts,
            FakeBrowser(),
            discovery,
        )
    assert [dict(c.args[0].fields) for c in verify.call_args_list] == [
        {"ltuid_v2": "456", "ltoken_v2": "rejected-token"},
        dict(accepted.fields),
    ]
    assert prompts.prompts.count(ID_PROMPT) == prompts.prompts.count(TOKEN_PROMPT) == 2


@pytest.mark.parametrize("stage", [ID_PROMPT, TOKEN_PROMPT, "Session recovery"])
def test_cancelled_partial_pair_never_changes_shared_login(
    tmp_path: Path, stage: str
) -> None:
    config, secrets = seed_shared(tmp_path)
    before = config.read_bytes(), secrets.read_bytes()

    class CancelIO(FakeIO):
        def secret(self, prompt: str) -> str:
            if prompt == stage:
                raise click.Abort()
            return "" if stage == "Session recovery" else super().secret(prompt)

        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == stage:
                return "Cancel (no changes)"
            return super().choose(prompt, choices)

    with pytest.raises(click.Abort):
        run_edit_profile(
            config,
            secrets,
            CancelIO(),
            FakeBrowser(),
            FakeDiscovery(),
            requested_profile="main",
        )
    assert (config.read_bytes(), secrets.read_bytes()) == before


def test_terminal_pair_is_hidden_and_source_choice_is_explicit(tmp_path: Path) -> None:
    config, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    with (
        patch("hoyo_cli.commands.init.config_path", return_value=config),
        patch("hoyo_cli.commands.init.credentials_path", return_value=secrets),
        patch("hoyo_cli.commands.init.HoYoLABDiscovery", return_value=FakeDiscovery()),
    ):
        result = CliRunner().invoke(
            main,
            ["init", "--no-color"],
            input="main\n1\n987654321\nprivate-synthetic-token\n1\n1\n1\ny\n2\ny\n1\n",
        )
    assert result.exit_code == 0, result.output
    assert MANUAL in result.output and BROWSER in result.output
    assert result.output.index(ID_PROMPT) < result.output.index(TOKEN_PROMPT)
    assert (
        "987654321" not in result.output
        and "private-synthetic-token" not in result.output
    )
    assert "Cookie header" not in result.output and "\x1b[" not in result.output


@pytest.mark.parametrize("stage", ["HoYoLAB session", "Browser profile"])
def test_browser_picker_can_switch_to_manual_before_import(
    tmp_path: Path, stage: str
) -> None:
    class PickerIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == stage:
                return "Enter ltuid_v2 / ltoken_v2 manually"
            return super().choose(prompt, choices)

    browser = FakeBrowser()
    if stage == "Browser profile":
        from hoyo_cli.auth.browser_import import BrowserSession

        with patch.object(
            browser,
            "list_sessions",
            return_value=(BrowserSession("edge", "Edge", True),),
        ):
            run_init(
                tmp_path / "config.toml",
                tmp_path / "credentials.toml",
                PickerIO(browser=True),
                browser,
                FakeDiscovery(),
            )
    else:
        run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            PickerIO(browser=True),
            browser,
            FakeDiscovery(),
        )
    assert browser.imported == []
    assert PlaintextStore(tmp_path / "credentials.toml").get(
        "main"
    ) == manual_credentials("123", "test-secret-123")


@pytest.mark.parametrize("code", [ErrorCode.AUTH_REJECTED, ErrorCode.API_REJECTED])
def test_valid_pair_rejected_by_hoyolab_is_never_saved(
    tmp_path: Path, code: ErrorCode, capsys: pytest.CaptureFixture[str]
) -> None:
    config, secrets = seed_shared(tmp_path)
    before = config.read_bytes(), secrets.read_bytes()

    class RejectIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Session recovery":
                assert "Change credential source" in choices and REENTER in choices
                return "Cancel (no changes)"
            return super().choose(prompt, choices)

    discovery = FakeDiscovery()
    with patch.object(
        discovery, "discover", side_effect=AppError("Session rejected.", code=code)
    ):
        with pytest.raises(click.Abort):
            run_edit_profile(
                config,
                secrets,
                RejectIO(),
                FakeBrowser(),
                discovery,
                requested_profile="main",
            )
    assert (config.read_bytes(), secrets.read_bytes()) == before
    output = capsys.readouterr()
    assert (
        "additional cookies" in output.err and "Sign in to HoYoLAB again" in output.err
    )
    assert "test-secret-123" not in output.out + output.err


@pytest.mark.parametrize("values", [("bad-id", "valid-token"), ("123", "bad token")])
def test_invalid_pair_reentry_verifies_only_the_replacement(
    tmp_path: Path, values: tuple[str, str]
) -> None:
    class RetryIO(FakeIO):
        def secret(self, prompt: str) -> str:
            if self.prompts.count(prompt) == 0:
                self.prompts.append(prompt)
                return values[0] if prompt == ID_PROMPT else values[1]
            return super().secret(prompt)

        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Session recovery":
                return REENTER
            return super().choose(prompt, choices)

    prompts, discovery = RetryIO(), FakeDiscovery()
    with patch.object(discovery, "discover", wraps=discovery.discover) as verify:
        run_init(
            tmp_path / "config.toml",
            tmp_path / "credentials.toml",
            prompts,
            FakeBrowser(),
            discovery,
        )
    verify.assert_called_once_with(manual_credentials("123", "test-secret-123"))
    assert prompts.prompts.count(ID_PROMPT) == prompts.prompts.count(TOKEN_PROMPT) == 2
