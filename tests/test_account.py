"""Offline account listing, targeted default writes, and command isolation."""

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from hoyo_cli.cli import main
from hoyo_cli.config import repository
from hoyo_cli.errors import ConfigError


@pytest.fixture
def account_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".hoyo/config.toml"
    path.parent.mkdir()
    path.write_text(
        """# Account settings
version = 1
default_account = "moonn" # selected account
game_order = ["zzz", "genshin"]
custom = "private-marker"

[display]
color = "auto"
custom = "keep"

[accounts.moonn]
credential_backend = "keyring" # credential reference
custom = "private-marker"

[accounts.moonn.games.genshin]
enabled = true
uid = 111111111 # character selection

[accounts.alt]
credential_backend = "plaintext"

[accounts.alt.games.zzz]
enabled = false
uid = 444444444
""",
        encoding="utf-8",
    )
    return path


@contextmanager
def forbid_authentication() -> Generator[None, None, None]:
    """Guard every existing entrypoint to secrets, discovery, status, and claims."""
    targets = (
        "hoyo_cli.commands.init.open_store",
        "hoyo_cli.commands.status.open_store",
        "hoyo_cli.commands.check_in.open_store",
        "hoyo_cli.auth.credentials.open_store",
        "hoyo_cli.auth.keyring_backend.KeyringStore.get",
        "hoyo_cli.auth.keyring_backend.KeyringStore.put",
        "hoyo_cli.auth.plaintext_backend.PlaintextStore.get",
        "hoyo_cli.auth.plaintext_backend.PlaintextStore.put",
        "hoyo_cli.commands.init.SystemBrowserImporter",
        "hoyo_cli.commands.init.HoYoLABDiscovery",
        "hoyo_cli.commands.status.GenshinPyGateway",
        "hoyo_cli.commands.check_in.GenshinPyGateway",
    )
    with ExitStack() as stack:
        guards = [
            stack.enter_context(patch(target, side_effect=AssertionError(target)))
            for target in targets
        ]
        yield
        for guard in guards:
            guard.assert_not_called()


@pytest.mark.parametrize("width", [48, 70, 100])
def test_listing_is_stable_and_marks_persisted_default_without_side_effects(
    runner: CliRunner, account_file: Path, width: int
) -> None:
    original = account_file.read_bytes()
    with forbid_authentication():
        first = runner.invoke(main, ["account"], terminal_width=width)
        second = runner.invoke(main, ["--account", "alt", "account"])
    assert first.exit_code == second.exit_code == 0, first.output
    assert first.stdout == second.stdout
    assert "  moonn  Default" in first.stdout
    assert "  alt\n" in first.stdout
    assert first.stdout.index("  moonn") < first.stdout.index("  alt")
    assert "hoyo account use NAME" in first.stdout
    assert "private-marker" not in first.output and "\x1b" not in first.output
    assert first.stderr == ""
    assert account_file.read_bytes() == original


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_switch_changes_only_default_and_never_reads_credentials(
    runner: CliRunner, account_file: Path, line_ending: bytes
) -> None:
    original = account_file.read_bytes().replace(b"\r\n", b"\n")
    original = original.replace(b"\n", line_ending)
    account_file.write_bytes(original)
    with forbid_authentication():
        result = runner.invoke(main, ["account", "use", "alt"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "Default account: alt\n" and result.stderr == ""
    assert account_file.read_bytes() == original.replace(
        b'default_account = "moonn"', b'default_account = "alt"', 1
    )
    assert not (account_file.parent / "credentials.toml").exists()


def test_already_current_does_not_replace_file(
    runner: CliRunner, account_file: Path
) -> None:
    original = account_file.read_bytes()
    modified = account_file.stat().st_mtime_ns
    with forbid_authentication(), patch.object(repository.os, "replace") as replace:
        result = runner.invoke(main, ["account", "use", "moonn"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "moonn is already the default account.\n"
    replace.assert_not_called()
    assert account_file.read_bytes() == original
    assert account_file.stat().st_mtime_ns == modified


@pytest.mark.parametrize("name", ["missing", "Alt", " alt", "alt ", "\x1bsecret"])
def test_unknown_names_are_exact_and_safe(
    runner: CliRunner, account_file: Path, name: str
) -> None:
    original = account_file.read_bytes()
    with forbid_authentication():
        result = runner.invoke(main, ["account", "use", name])
    assert result.exit_code == 2
    assert "exact" in result.stderr and "Run: hoyo account" in result.stderr
    assert result.stdout == ""
    assert "private-marker" not in result.output and "\x1b" not in result.output
    assert account_file.read_bytes() == original


@pytest.mark.parametrize("arguments", [["account"], ["account", "use", "alt"]])
@pytest.mark.parametrize("invalid", [None, b'bad = [ "private-marker"', b"version = 2"])
def test_missing_or_invalid_configuration_is_not_created_or_overwritten(
    runner: CliRunner, account_file: Path, arguments: list[str], invalid: bytes | None
) -> None:
    if invalid is None:
        account_file.unlink()
    else:
        account_file.write_bytes(invalid)
    with forbid_authentication():
        result = runner.invoke(main, arguments)
    assert result.exit_code == 2 and result.stdout == ""
    assert (
        "Run: hoyo init" in result.stderr
        if invalid is None
        else "Invalid" in result.stderr
    )
    assert "private-marker" not in result.output and "Traceback" not in result.output
    assert (
        account_file.read_bytes() == invalid
        if invalid is not None
        else not account_file.exists()
    )


def test_prepared_default_is_validated_and_does_not_write(account_file: Path) -> None:
    original = account_file.read_bytes()
    prepared = repository.prepare_default_account(account_file, "alt")
    assert prepared.original == original
    assert prepared.content.encode() == original.replace(
        b'default_account = "moonn"', b'default_account = "alt"', 1
    )
    assert account_file.read_bytes() == original
    for unknown in ("Alt", "missing"):
        with pytest.raises(ConfigError, match="exact"):
            repository.prepare_default_account(account_file, unknown)


@pytest.mark.parametrize("operation", ["fsync", "replace"])
@pytest.mark.parametrize("interruption", [OSError, KeyboardInterrupt])
def test_interrupted_switch_preserves_original_and_cleans_temporary_file(
    runner: CliRunner,
    account_file: Path,
    operation: str,
    interruption: type[BaseException],
) -> None:
    original = account_file.read_bytes()
    with (
        forbid_authentication(),
        patch.object(
            repository.os, operation, side_effect=interruption("private-marker")
        ),
    ):
        result = runner.invoke(main, ["account", "use", "alt"])
    assert result.exit_code == (2 if interruption is OSError else 1)
    if interruption is OSError:
        assert "Unable to save configuration" in result.stderr
    assert "Default account: alt" not in result.output
    assert "private-marker" not in result.output and "Traceback" not in result.output
    assert account_file.read_bytes() == original
    assert list(account_file.parent.iterdir()) == [account_file]


def test_write_conflict_retains_external_edit_and_has_command_recovery(
    runner: CliRunner, account_file: Path
) -> None:
    external = account_file.read_bytes() + b"# External edit\n"

    def concurrent_edit(prepared: repository.PreparedConfig) -> None:
        account_file.write_bytes(external)
        repository.commit_config(prepared)

    with patch("hoyo_cli.commands.account.commit_config", side_effect=concurrent_edit):
        result = runner.invoke(main, ["account", "use", "alt"])
    assert result.exit_code == 2
    assert "changed" in result.stderr and "hoyo account use NAME" in result.stderr
    assert "hoyo init" not in result.stderr
    assert account_file.read_bytes() == external
    assert list(account_file.parent.iterdir()) == [account_file]


@pytest.mark.parametrize("stage", ["prepare", "commit"])
def test_default_transaction_refuses_detected_symlink(
    account_file: Path, stage: str
) -> None:
    original = account_file.read_bytes()
    if stage == "prepare":
        with patch.object(Path, "is_symlink", return_value=True):
            with pytest.raises(ConfigError, match="symbolic link"):
                repository.prepare_default_account(account_file, "alt")
    else:
        prepared = repository.prepare_default_account(account_file, "alt")
        with patch.object(Path, "is_symlink", return_value=True):
            with pytest.raises(ConfigError, match="symbolic link"):
                repository.commit_config(prepared)
    assert account_file.read_bytes() == original


@pytest.mark.parametrize("arguments", [["account"], ["account", "use", "alt"]])
def test_unreadable_configuration_reports_safe_error(
    runner: CliRunner, account_file: Path, arguments: list[str]
) -> None:
    with patch.object(
        Path, "read_bytes", side_effect=PermissionError("private-marker")
    ):
        result = runner.invoke(main, arguments)
    assert result.exit_code == 2
    assert "Unable to read" in result.stderr
    assert "private-marker" not in result.output and "Traceback" not in result.output


def test_root_help_explains_persistent_and_temporary_account_selection(
    runner: CliRunner,
) -> None:
    result = runner.invoke(main, ["--help"], terminal_width=100)
    assert result.exit_code == 0
    assert "hoyo account use NAME" in result.output
    assert "persistent default" in result.output
    assert "one invocation" in result.output
