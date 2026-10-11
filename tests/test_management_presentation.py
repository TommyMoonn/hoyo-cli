"""Offline management transcripts, stream routing and terminal policy."""

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from profile_fixtures import ProfileSeed, save_profile
from rich.text import Text
from test_init import FakeBrowser
from test_profiles import RegionsDiscovery

from hoyo_cli.cli import main
from hoyo_cli.config.editor import ResolvedEditor
from hoyo_cli.config.models import GameSelection
from hoyo_cli.config.repository import load_config
from hoyo_cli.errors import ConfigError
from hoyo_cli.render.errors import format_error


@pytest.fixture(autouse=True)
def terminal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("rich.console.detect_legacy_windows", lambda: False)


@pytest.fixture
def management_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".hoyo/config.toml"
    for name in ("main", "[red]long profile with literal brackets[/red]"):
        save_profile(
            path,
            name,
            ProfileSeed(
                "keyring",
                {
                    "genshin": GameSelection(True, 800000001),
                    "zzz": GameSelection(False, 100000001),
                },
            ),
            make_default=name == "main",
            game_order=("zzz", "genshin"),
        )
    return path


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("route", ["profile", "config", "init", "use", "error"])
@pytest.mark.parametrize(
    "mode", ["tty", "root", "group", "local", "NO_COLOR", "never", "pipe", "dumb", "CI"]
)
def test_management_transcripts_share_policy_and_preserve_data(
    management_config: Path, width: int, route: str, mode: str
) -> None:
    path = management_config
    if mode == "never":
        path.write_text(path.read_text().replace('color = "auto"', 'color = "never"'))
    before = path.read_bytes()
    arguments = {
        "profile": ["profile"],
        "config": ["config"],
        "init": ["init"],
        "use": ["profile", "use", "main"],
        "error": ["profile", "use", "[red]unknown[/red]"],
    }[route]
    if mode == "root":
        arguments.insert(0, "--no-color")
    elif mode == "group":
        arguments.insert(1, "--no-color")
    elif mode == "local":
        arguments.append("--no-color")
    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=mode != "pipe"),
        patch("hoyo_cli.auth.keyring_backend.KeyringStore.get") as secrets,
        patch("hoyo_cli.hoyolab.client.create_client") as network,
    ):
        result = CliRunner().invoke(
            main,
            arguments,
            color=True,
            env={
                "COLUMNS": str(width),
                "NO_COLOR": "1" if mode == "NO_COLOR" else None,
                "CI": "1" if mode == "CI" else None,
                "TERM": "dumb" if mode == "dumb" else "xterm-256color",
            },
        )
    assert result.exit_code == (2 if route == "error" else 0), result.output
    output = Text.from_ansi(result.output).plain
    assert all(len(line) <= width for line in output.splitlines()), output
    assert ("\x1b[" in result.output) == (mode == "tty" and route != "use")
    assert path.read_bytes() == before
    secrets.assert_not_called()
    network.assert_not_called()
    if route in ("profile", "config"):
        if mode == "tty":
            styled = Text.from_ansi(result.stdout)
            value_index = styled.plain.index("800000001")
            assert not any(
                span.start <= value_index < span.end and "bold" in str(span.style)
                for span in styled.spans
            )
        compact = " ".join(output.split())
        for value in (
            "Default",
            "Login:",
            "Enabled",
            "Disabled",
            "Not configured",
            "800000001",
            "100000001",
            "[red]long profile with literal brackets[/red]",
            "hoyo profile edit <name>",
        ):
            assert value in compact
        assert "Asia" not in output and "America" not in output
        if route == "config":
            assert "GLOBAL PREFERENCES" in output and "Editor executable:" in output
            assert "zzz, genshin" in output
    if route == "error":
        assert result.stdout == ""
        assert "Error:" in output and "Run: hoyo profile" in output
        if mode == "tty":
            assert "\x1b[31m" in result.stderr and "\x1b[2m" in result.stderr
    else:
        assert result.stderr == ""
    if route == "use":
        assert "already the default" in output and "\x1b[32m" not in result.output


@pytest.mark.parametrize("color", [True, False])
def test_default_save_and_noop_retain_transaction_semantics(
    management_config: Path, color: bool
) -> None:
    name = "[red]long profile with literal brackets[/red]"
    arguments = ["profile", "use", name] + ([] if color else ["--no-color"])
    with patch("click.testing._NamedTextIOWrapper.isatty", return_value=True):
        first = CliRunner().invoke(main, arguments, color=True, env={"COLUMNS": "100"})
        saved = management_config.read_bytes()
        second = CliRunner().invoke(main, arguments, color=True, env={"COLUMNS": "100"})
    assert first.exit_code == second.exit_code == 0
    assert load_config(management_config).default_profile == name
    assert management_config.read_bytes() == saved
    assert ("\x1b[32m" in first.stdout) == color
    assert "already the default" in second.stdout and "\x1b[32m" not in second.stdout


@pytest.mark.parametrize("stdout_tty, stderr_tty", [(True, False), (False, True)])
def test_error_redirected_streams_are_plain(
    management_config: Path, stdout_tty: bool, stderr_tty: bool
) -> None:
    def isatty(stream: object) -> bool:
        return stdout_tty if getattr(stream, "name", "") == "<stdout>" else stderr_tty

    with patch("click.testing._NamedTextIOWrapper.isatty", isatty):
        result = CliRunner().invoke(main, ["profile", "use", "unknown"], color=True)
    assert result.exit_code == 2 and not result.stdout
    assert "\x1b" not in result.stderr


def test_error_summary_and_recovery_match_plain_formatter(
    management_config: Path,
) -> None:
    error = ConfigError("Safe [literal] summary.", hint="Run: hoyo config editor")
    with (
        patch("hoyo_cli.commands.config.run_config", side_effect=error),
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
    ):
        result = CliRunner().invoke(
            main, ["config"], color=True, env={"COLUMNS": "100"}
        )
    assert result.exit_code == error.exit_code and not result.stdout
    assert Text.from_ansi(result.stderr).plain.rstrip() == format_error(error)
    assert "\x1b" not in format_error(error)


def test_click_usage_and_help_remain_standard(management_config: Path) -> None:
    runner = CliRunner()
    invalid = runner.invoke(main, ["profile", "use"])
    assert invalid.exit_code == 2 and invalid.stderr.startswith("Usage:")
    for command in (
        [],
        ["profile"],
        ["profile", "use"],
        ["profile", "add"],
        ["config"],
    ):
        result = runner.invoke(main, [*command, "--help"])
        assert result.exit_code == 0 and result.stdout.startswith("Usage: hoyo")
        assert "NAME" not in result.output and "\x1b" not in result.output


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("outcome", ["validated", "unconfirmed", "failure"])
def test_editor_outcomes_use_shared_semantics_and_preserve_bytes(
    management_config: Path, width: int, outcome: str
) -> None:
    before = management_config.read_bytes()
    with (
        patch(
            "hoyo_cli.commands.config.resolve_editor",
            return_value=ResolvedEditor(
                ("synthetic-editor",), outcome != "unconfirmed"
            ),
        ),
        patch(
            "hoyo_cli.commands.config.subprocess.run",
            return_value=CompletedProcess([], 5 if outcome == "failure" else 0),
        ) as launch,
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
    ):
        result = CliRunner().invoke(
            main, ["config", "--edit"], color=True, env={"COLUMNS": str(width)}
        )
    assert result.exit_code == (1 if outcome == "failure" else 0), result.output
    assert management_config.read_bytes() == before
    launch.assert_called_once_with(
        ["synthetic-editor", str(management_config)], check=False, shell=False
    )
    output = Text.from_ansi(result.output).plain
    assert all(len(line) <= width for line in output.splitlines())
    if outcome == "failure":
        assert "\x1b[31m" in result.stderr and not result.stdout
        assert "Edits were preserved" in " ".join(output.split())
    elif outcome == "validated":
        assert "\x1b[32m" in result.stdout and not result.stderr
        assert "validated after editor closed" in " ".join(output.split())
    else:
        assert "unconfirmed" in output and "\x1b[32m" not in result.stdout


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("color", [True, False])
def test_synthetic_management_lifecycle_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, width: int, color: bool
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path, secrets = tmp_path / ".hoyo/config.toml", tmp_path / ".hoyo/credentials.toml"
    transcript: list[str] = []

    def invoke(arguments: list[str], answers: str = "", exit_code: int = 0) -> str:
        result = CliRunner().invoke(
            main,
            ([] if color else ["--no-color"]) + arguments,
            input=answers,
            color=True,
            env={"COLUMNS": str(width)},
        )
        assert result.exit_code == exit_code, result.output
        assert "synthetic-private-token" not in result.output
        assert "\x1b" not in result.output or color
        transcript.append(Text.from_ansi(result.output).plain)
        return result.output

    with (
        patch(
            "hoyo_cli.commands.init.HoYoLABDiscovery", return_value=RegionsDiscovery()
        ),
        patch(
            "hoyo_cli.commands.init.SystemBrowserImporter", return_value=FakeBrowser()
        ),
        patch("hoyo_cli.commands.init.detected_editors", return_value={}),
        patch(
            "hoyo_cli.hoyolab.client.create_client",
            side_effect=AssertionError("Offline"),
        ),
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
    ):
        invoke(["init"], "main\n1\n123\nsynthetic-private-token\n2\ny\n2\ny\n1\n")
        invoke(["profile", "add", "america"], "1\n1\n1\n")
        original_secrets = secrets.read_bytes()
        before = path.read_bytes()
        invoke(["profile"])
        assert path.read_bytes() == before
        invoke(["profile", "use", "america"])
        switched = path.read_bytes()
        invoke(["profile", "use", "america"])
        invoke(["config"])
        invoke(["config", "editor"], "1\n1\n")
        invoke(["init"])
        assert path.read_bytes() == switched
        invoke(["config", "order"], "n\n3\n1\n1\n1\n")
        changed = path.read_bytes()
        invoke(["config", "order"], "\n2\n", exit_code=1)
        invoke(["profile", "use", "unknown"], exit_code=2)
        invoke(["config", "--edit"], exit_code=2)
        assert path.read_bytes() == changed
    assert secrets.read_bytes() == original_secrets
    config = load_config(path)
    assert config.default_profile == "america"
    assert config.game_order == ("zzz", "genshin", "hsr")
    assert len(config.logins) == 1
    output = "\n".join(transcript)
    assert all(len(line) <= width for line in output.splitlines())
    for value in (
        "Configured profile",
        "PROFILES",
        "Default profile:",
        "GLOBAL PREFERENCES",
        "Editor preference",
        "cancelled",
        "Error:",
    ):
        assert value in output
