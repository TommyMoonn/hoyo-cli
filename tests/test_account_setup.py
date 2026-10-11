"""Phase 2 command ownership and transactional account setup regressions."""

from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner
from test_init import FakeBrowser, FakeDiscovery, FakeIO, run_init

from hoyo_cli.auth.credentials import Credentials
from hoyo_cli.auth.plaintext_backend import PlaintextStore
from hoyo_cli.cli import main
from hoyo_cli.commands.init import run_add_profile, run_edit_profile
from hoyo_cli.config.repository import load_config
from hoyo_cli.errors import AppError
from hoyo_cli.hoyolab.discovery import CharacterRef
from hoyo_cli.models.common import GameId


def initialize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config, secrets = (
        tmp_path / ".hoyo/config.toml",
        tmp_path / ".hoyo/credentials.toml",
    )
    run_init(config, secrets, FakeIO(), FakeBrowser(), FakeDiscovery())
    config.write_bytes(
        config.read_bytes()
        + b'\n# Future preference\n[editor]\ncommand = ["future-editor", "--wait"]\n'
    )
    return config, secrets


def test_initialized_init_is_read_only_and_never_opens_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    config, secrets = initialize(tmp_path, monkeypatch)
    before = config.read_bytes(), secrets.read_bytes()
    with (
        patch("hoyo_cli.commands.init.ClickSetupIO") as io,
        patch("hoyo_cli.commands.init.SystemBrowserImporter") as browser,
        patch("hoyo_cli.commands.init.HoYoLABDiscovery") as discovery,
        patch("hoyo_cli.commands.init.open_store") as store,
    ):
        result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    for command in (
        "hoyo profile add",
        "hoyo profile edit <name>",
        "hoyo profile use <name>",
        "hoyo config",
    ):
        assert command in result.output
    for dependency in (io, browser, discovery, store):
        dependency.assert_not_called()
    assert (config.read_bytes(), secrets.read_bytes()) == before


def test_add_help_marks_name_optional_without_opening_setup(runner: CliRunner) -> None:
    with patch("hoyo_cli.commands.init.configure_profile") as setup:
        result = runner.invoke(main, ["profile", "add", "--help"])
    assert result.exit_code == 0 and "[name]" in result.stdout
    setup.assert_not_called()


@pytest.mark.parametrize("named", [False, True])
def test_add_creates_only_new_account_preserving_default_and_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner, named: bool
) -> None:
    config, secrets = initialize(tmp_path, monkeypatch)
    config.write_bytes(
        config.read_bytes()
        .replace(
            b'game_order = ["genshin", "hsr", "zzz"]',
            b'game_order = ["zzz", "genshin"] # preferred order',
        )
        .replace(b'color = "auto"', b'color = "never" # display policy')
    )
    before = load_config(config)
    io = FakeIO(name="alt")
    with (
        patch("hoyo_cli.commands.init.ClickSetupIO", return_value=io),
        patch(
            "hoyo_cli.commands.init.SystemBrowserImporter", return_value=FakeBrowser()
        ),
        patch("hoyo_cli.commands.init.HoYoLABDiscovery", return_value=FakeDiscovery()),
    ):
        result = runner.invoke(main, ["profile", "add", *(["alt"] if named else [])])
    assert result.exit_code == 0, result.output
    after = load_config(config)
    assert after.default_profile == before.default_profile == "main"
    assert after.game_order == before.game_order
    assert after.display_color == before.display_color == "never"
    assert after.profiles["main"] == before.profiles["main"]
    assert "alt" in after.profiles and secrets.exists()
    assert '[editor]\ncommand = ["future-editor", "--wait"]' in config.read_text()
    assert ("Profile name" in io.prompts) is not named
    assert not any("default" in prompt or "order" in prompt for prompt in io.prompts)


@pytest.mark.parametrize("name", ["main", "missing", "Main"])
def test_duplicate_add_and_unknown_edit_fail_before_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner, name: str
) -> None:
    config, secrets = initialize(tmp_path, monkeypatch)
    before = config.read_bytes(), secrets.read_bytes()
    action = "add" if name == "main" else "edit"
    with (
        patch("hoyo_cli.commands.init.SystemBrowserImporter") as browser,
        patch("hoyo_cli.commands.init.HoYoLABDiscovery") as discovery,
        patch("hoyo_cli.commands.init.open_store") as store,
    ):
        result = runner.invoke(main, ["profile", action, name])
    assert result.exit_code == 2, result.output
    assert (
        "hoyo profile edit" in result.stderr
        if action == "add"
        else "hoyo profile" in result.stderr
    )
    for dependency in (browser, discovery, store):
        dependency.assert_not_called()
    assert (config.read_bytes(), secrets.read_bytes()) == before


def test_edit_alt_preserves_main_default_preferences_and_other_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, secrets = initialize(tmp_path, monkeypatch)
    run_add_profile(config, secrets, FakeIO(name="alt"), FakeBrowser(), FakeDiscovery())
    before = config.read_bytes(), secrets.read_bytes()

    class DisableIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Game settings for hsr":
                return "Disable this game"
            return super().choose(prompt, choices)

    with patch("hoyo_cli.commands.init.open_store") as store:
        run_edit_profile(
            config,
            secrets,
            DisableIO(action="Games and characters"),
            FakeBrowser(),
            FakeDiscovery(),
            requested_profile="alt",
        )
    store.assert_not_called()
    assert config.read_bytes() == before[0].replace(
        b"[profiles.alt.games.hsr]\nenabled = true",
        b"[profiles.alt.games.hsr]\nenabled = false",
    )
    assert secrets.read_bytes() == before[1]
    assert load_config(config).default_profile == "main"


@pytest.mark.parametrize("stage", ["selection", "confirmation"])
def test_order_conflict_retains_external_change_and_never_accesses_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner, stage: str
) -> None:
    config, secrets = initialize(tmp_path, monkeypatch)
    original = config.read_bytes(), secrets.read_bytes()
    external = original[0] + b"# External change\n"

    class EditingIO(FakeIO):
        def confirm(self, prompt: str) -> bool:
            if stage == "selection":
                config.write_bytes(external)
            return False

        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt.startswith("Save"):
                if stage == "confirmation":
                    config.write_bytes(external)
                return "Save"
            return choices[-1]

    with (
        patch("hoyo_cli.commands.config.ClickSetupIO", return_value=EditingIO()),
        patch("hoyo_cli.commands.init.open_store") as store,
    ):
        result = runner.invoke(main, ["config", "order"])
    assert result.exit_code == 2 and "changed" in result.stderr
    assert "hoyo config order" in result.stderr
    assert config.read_bytes() == external
    assert secrets.read_bytes() == original[1]
    store.assert_not_called()


@pytest.mark.parametrize("answers", ["\n\n", "n\n4\n", "n\n3\n2\n1\n2\n"])
def test_order_keep_or_cancel_does_not_replace_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner, answers: str
) -> None:
    config, secrets = initialize(tmp_path, monkeypatch)
    before = config.read_bytes(), secrets.read_bytes()
    with patch("hoyo_cli.config.repository.os.replace") as replace:
        result = runner.invoke(main, ["config", "order"], input=answers)
    assert result.exit_code == (0 if answers == "\n\n" else 1), result.output
    replace.assert_not_called()
    assert (config.read_bytes(), secrets.read_bytes()) == before


def test_edit_menu_has_only_account_actions_and_preserves_global_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    config, secrets = initialize(tmp_path, monkeypatch)
    before = config.read_bytes(), secrets.read_bytes()

    class EditIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Profile settings":
                assert tuple(choices) == (
                    "Games and characters",
                    "Change linked login",
                    "Update HoYoLAB session",
                    "Credential storage",
                    "Cancel (no changes)",
                )
                return "Games and characters"
            if prompt == "Game settings for hsr":
                return "Disable this game"
            return super().choose(prompt, choices)

    with (
        patch("hoyo_cli.commands.init.ClickSetupIO", return_value=EditIO()),
        patch("hoyo_cli.commands.init.open_store") as store,
    ):
        result = runner.invoke(main, ["profile", "edit", "main"])
    assert result.exit_code == 0, result.output
    store.assert_not_called()
    assert config.read_bytes() == before[0].replace(
        b"[profiles.main.games.hsr]\nenabled = true",
        b"[profiles.main.games.hsr]\nenabled = false",
    )
    assert secrets.read_bytes() == before[1]


def test_global_order_is_targeted_and_never_resolves_account_or_reads_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    config, secrets = initialize(tmp_path, monkeypatch)
    before = config.read_bytes(), secrets.read_bytes()
    with (
        patch("hoyo_cli.commands.init.open_store") as store,
        patch("hoyo_cli.commands.config.resolve_profile") as resolve,
        patch("hoyo_cli.commands.init.SystemBrowserImporter") as browser,
        patch("hoyo_cli.commands.init.HoYoLABDiscovery") as discovery,
    ):
        result = runner.invoke(
            main, ["--profile", "unknown", "config", "order"], input="n\n3\n2\n1\n1\n"
        )
    assert result.exit_code == 0, result.output
    assert config.read_bytes() == before[0].replace(
        b'game_order = ["genshin", "hsr", "zzz"]',
        b'game_order = ["zzz", "hsr", "genshin"]',
    )
    assert secrets.read_bytes() == before[1]
    for dependency in (store, resolve, browser, discovery):
        dependency.assert_not_called()


@pytest.mark.parametrize(
    "arguments", [["init", "--profile", "main"], ["--profile", "main", "init"]]
)
def test_init_account_override_is_rejected_with_edit_guidance(
    runner: CliRunner, arguments: list[str]
) -> None:
    result = runner.invoke(main, arguments)
    assert result.exit_code == 2
    assert (
        "hoyo profile edit <name>" in result.output
        or "No such option '--profile'" in result.output
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["init"],
        ["profile", "add", "alt"],
        ["profile", "edit", "main"],
        ["config", "order"],
    ],
)
def test_invalid_config_is_never_treated_as_fresh_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    arguments: list[str],
) -> None:
    config, secrets = initialize(tmp_path, monkeypatch)
    config.write_bytes(b'bad = [ "private-marker"')
    before = config.read_bytes(), secrets.read_bytes()
    with patch("hoyo_cli.commands.init.open_store") as store:
        result = runner.invoke(main, arguments)
    assert result.exit_code == 2 and "Invalid TOML" in result.stderr
    assert "private-marker" not in result.output
    store.assert_not_called()
    assert (config.read_bytes(), secrets.read_bytes()) == before


@pytest.mark.parametrize("mode", ["add", "edit"])
@pytest.mark.parametrize(
    "failure",
    ["cancel", "verification", "credential-interrupt", "conflict-during-discovery"],
)
def test_account_setup_failures_preserve_exact_account_and_secret_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, failure: str
) -> None:
    config, secrets = initialize(tmp_path, monkeypatch)
    original = config.read_bytes(), secrets.read_bytes()
    discovery, store = FakeDiscovery(), PlaintextStore(secrets)
    io = FakeIO(
        name="alt" if mode == "add" else "main",
        cancel="Save" if failure == "cancel" else "",
    )
    operation = run_add_profile if mode == "add" else run_edit_profile
    expected = (
        click.Abort
        if failure == "cancel"
        else KeyboardInterrupt
        if failure == "credential-interrupt"
        else AppError
    )
    actual_put = store.put
    actual_discover = discovery.discover

    def interrupted_put(name: str, credentials: Credentials) -> None:
        # Interrupt after the actual secret write to exercise exact rollback.
        actual_put(name, credentials)
        raise KeyboardInterrupt()

    def changed_discovery(
        credentials: Credentials,
    ) -> dict[GameId, tuple[CharacterRef, ...]]:
        config.write_bytes(original[0] + b"# Concurrent edit during verification\n")
        return actual_discover(credentials)

    with (
        patch("hoyo_cli.commands.init.open_store", return_value=store),
        patch.object(
            store,
            "put",
            side_effect=interrupted_put
            if failure == "credential-interrupt"
            else actual_put,
        ),
        patch.object(
            discovery,
            "discover",
            side_effect=AppError("Verification failed")
            if failure == "verification"
            else changed_discovery
            if failure == "conflict-during-discovery"
            else actual_discover,
        ),
    ):
        with pytest.raises(expected):
            operation(
                config,
                secrets,
                io,
                FakeBrowser(),
                discovery,
                requested_profile="alt" if mode == "add" else "main",
            )
    assert secrets.read_bytes() == original[1]
    assert config.read_bytes() == original[0] + (
        b"# Concurrent edit during verification\n"
        if failure == "conflict-during-discovery"
        else b""
    )
    assert set(load_config(config).profiles) == {"main"}


@pytest.mark.parametrize(
    "arguments",
    [["profile", "add", "alt"], ["profile", "edit", "main"], ["config", "order"]],
)
def test_management_commands_require_initial_setup_without_creating_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    arguments: list[str],
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with patch("hoyo_cli.commands.init.open_store") as store:
        result = runner.invoke(main, arguments)
    assert result.exit_code == 2 and "Run: hoyo init" in result.stderr
    assert not (tmp_path / ".hoyo").exists()
    store.assert_not_called()
