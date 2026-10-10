"""Behavioral coverage for the installed CLI and command grammar."""

from importlib.metadata import version
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from hoyo_cli.cli import main


def test_help_identifies_hoyo(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Usage: hoyo" in result.output


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
        (["init"], "hoyo_cli.commands.init.run_init", {}),
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
        [],
        ["status"],
        ["genshin"],
        ["hsr"],
        ["zzz"],
        ["check-in"],
        ["check-in", "genshin"],
        ["init"],
        ["config"],
        ["config", "--edit"],
    ],
)
def test_unimplemented_commands_fail_cleanly(
    runner: CliRunner, arguments: list[str]
) -> None:
    result = runner.invoke(main, arguments)
    assert result.exit_code == 2
    assert "not configured yet" in result.output.lower()
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
