"""Semantic setup rendering and unchanged Click input contracts, offline."""

import re
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner
from profile_fixtures import ProfileSeed, save_profile
from rich.text import Text
from test_init import FakeBrowser
from test_profiles import RegionsDiscovery, seed_shared

from hoyo_cli.auth.browser_import import browser_error
from hoyo_cli.cli import main
from hoyo_cli.commands.init import ClickSetupIO
from hoyo_cli.config.models import GameSelection
from hoyo_cli.config.repository import load_config
from hoyo_cli.errors import ErrorCode
from hoyo_cli.hoyolab.discovery import CharacterRef
from hoyo_cli.models.common import GAME_IDS
from hoyo_cli.render.setup import SetupPresenter


@pytest.fixture(autouse=True)
def terminal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a VT-capable terminal without forcing production consoles or
    # inheriting the host's TERM/NO_COLOR settings.
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("rich.console.detect_legacy_windows", lambda: False)


def plain(output: str) -> str:
    return Text.from_ansi(output).plain


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize("color", [False, True])
def test_semantic_presenter_is_literal_and_width_aware(width: int, color: bool) -> None:
    @click.command()
    def command() -> None:
        presenter = SetupPresenter(no_color=not color)
        presenter.heading("Create profile [red]main[/red]")
        presenter.step(1, "Login", "[red]main[/red]")
        presenter.game("genshin")
        presenter.choices(("Traveler (Asia) | UID 800000001", "Cancel (no changes)"))
        presenter.current("Current editor: C:/Program Files/[red]/editor.exe")
        presenter.warning("Unavailable; choose manual entry.")
        presenter.success("Session verified.")
        presenter.review(
            name="[red]main[/red]",
            games={"genshin": GameSelection(True, 800000001)},
            characters={
                "genshin": CharacterRef(
                    "genshin", 800000001, "os_asia", "Traveler (Asia)"
                )
            },
            backend="keyring",
            login="[bold]login[/bold]",
            default_profile="[red]main[/red]",
            order=GAME_IDS,
        )
        presenter.success("Configured profile [red]main[/red].")

    with patch("click.testing._NamedTextIOWrapper.isatty", return_value=True):
        result = CliRunner().invoke(
            command, env={"COLUMNS": str(width), "NO_COLOR": None}, color=True
        )
    assert result.exit_code == 0, result.output
    output = plain(result.output)
    assert "[red]main[/red]" in output and "[bold]login[/bold]" in output
    assert "1. Traveler (Asia) | UID 800000001" in output
    assert "Default" in output and "Current editor:" in output
    assert "2. Cancel (no changes)" in output
    assert "Step 1/4" in output and "Step 4/4" in output
    assert all(len(line) <= width for line in output.splitlines())
    assert ("\x1b[" in result.output) == color
    if color:
        assert "\x1b[32m" in result.output
        assert "\x1b[33m" in result.output
        assert "\x1b[1m" in result.output
        styled = Text.from_ansi(result.output)
        label_index = styled.plain.index("Traveler (Asia)")
        assert not any(
            span.start <= label_index < span.end and "bold" in str(span.style)
            for span in styled.spans
        )


@pytest.mark.parametrize("color", [False, True])
@pytest.mark.parametrize(
    "answers, choices, expected",
    [
        ("\n", ("[red]raw[/red]", "Second"), "[red]raw[/red]"),
        ("0\ntext\n8\n2\n", ("First", "Second"), "Second"),
        ("2\n", ("First", "Cancel (no changes)", "Third"), "Cancel (no changes)"),
        ("3\n", ("First", "Second"), None),
    ],
)
def test_click_choices_keep_defaults_indexes_and_cancellation(
    color: bool, answers: str, choices: Sequence[str], expected: str | None
) -> None:
    selected: list[str] = []

    @click.command()
    def command() -> None:
        selected.append(
            ClickSetupIO(SetupPresenter(no_color=not color)).choose(
                "Selection", choices
            )
        )

    with patch("click.testing._NamedTextIOWrapper.isatty", return_value=True):
        result = CliRunner().invoke(command, input=answers, color=True)
    assert result.exit_code == (1 if expected is None else 0), result.output
    assert selected == ([] if expected is None else [expected])
    assert "Selection [1]:" in plain(result.output)
    assert "Default" in plain(result.output)


@pytest.mark.parametrize("color", [False, True])
def test_click_secret_confirm_and_eof_contracts(color: bool) -> None:
    values: list[str | bool] = []

    @click.command()
    def command() -> None:
        adapter = ClickSetupIO(SetupPresenter(no_color=not color))
        values.append(adapter.secret("Private token"))
        values.append(adapter.confirm("Keep current?", default=True))
        values.append(adapter.confirm("Consent?"))
        adapter.choose("Name", ())

    with patch("click.testing._NamedTextIOWrapper.isatty", return_value=True):
        result = CliRunner().invoke(
            command, input="synthetic-secret-123\n\n\n", color=True
        )
    assert result.exit_code == 1
    assert values == ["synthetic-secret-123", True, False]
    assert "synthetic-secret-123" not in result.output
    assert "[Y/n]" in plain(result.output) and "[y/N]" in plain(result.output)


@pytest.mark.parametrize("route", ["order", "editor"])
@pytest.mark.parametrize(
    "mode", ["tty", "root", "group", "local", "NO_COLOR", "never", "pipe"]
)
def test_global_menus_share_policy_and_keep_config_bytes(
    tmp_path: Path, route: str, mode: str
) -> None:
    path = tmp_path / "config.toml"
    save_profile(
        path,
        "main",
        ProfileSeed("keyring", {"genshin": GameSelection(True, 800000001)}),
        make_default=True,
        game_order=GAME_IDS,
    )
    if mode == "never":
        path.write_text(path.read_text().replace('color = "auto"', 'color = "never"'))
    before = path.read_bytes()
    arguments = ["config", route]
    if mode == "root":
        arguments.insert(0, "--no-color")
    elif mode == "group":
        arguments.insert(1, "--no-color")
    elif mode == "local":
        arguments.append("--no-color")
    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch("hoyo_cli.commands.init.detected_editors", return_value={}),
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=mode != "pipe"),
    ):
        result = CliRunner().invoke(
            main,
            arguments,
            input="\n\n",
            color=True,
            env={"NO_COLOR": "1" if mode == "NO_COLOR" else None},
        )
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == before
    assert ("\x1b[" in result.output) == (mode == "tty")
    assert "Default" in plain(result.output)
    if route == "editor":
        assert "Current editor:" in plain(result.output)
    assert load_config(path).default_profile == "main"
    assert not re.search(r"\x1b\[[0-9;]*[JKH]", result.output)


@pytest.mark.parametrize("width", [48, 70, 100])
@pytest.mark.parametrize(
    "route, mode",
    [
        (route, mode)
        for route in ("init", "add", "edit", "browser")
        for mode in (
            "tty",
            "root",
            "local",
            "NO_COLOR",
            "never",
            "pipe",
            "stderr-pipe",
            "dumb",
        )
        if not (route in ("init", "browser") and mode == "never")
    ],
)
def test_synthetic_guided_flow_semantics_and_policy(
    tmp_path: Path, route: str, width: int, mode: str
) -> None:
    if route in ("init", "browser"):
        path, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
        arguments = ["init"]
        answers = "main\n"
        if route == "browser":
            # Selected browser/profile fails once; take direct manual recovery.
            answers += "2\n2\n1\n"
        else:
            answers += "1\n"
        answers += "123\nhidden-synthetic\n2\ny\n2\ny\n1\n"
    else:
        path, secrets = seed_shared(tmp_path)
        arguments = ["profile", route, "second" if route == "add" else "main"]
        answers = "1\n2\n1\n" if route == "add" else "1\n3\n2\n1\n1\n1\n"
        if mode == "never":
            path.write_text(
                path.read_text().replace('color = "auto"', 'color = "never"')
            )
    previous_order = load_config(path).game_order if path.exists() else GAME_IDS
    previous_secrets = secrets.read_bytes() if secrets.exists() else None
    browser = FakeBrowser()
    discovery = RegionsDiscovery()
    if mode == "root":
        arguments.insert(0, "--no-color")
    elif mode == "local":
        arguments.append("--no-color")

    def isatty(stream: object) -> bool:
        name = getattr(stream, "name", "")
        return not (
            mode == "pipe"
            and name == "<stdout>"
            or mode == "stderr-pipe"
            and name == "<stderr>"
        )

    with (
        patch("hoyo_cli.commands.init.config_path", return_value=path),
        patch("hoyo_cli.commands.init.credentials_path", return_value=secrets),
        patch("hoyo_cli.commands.init.HoYoLABDiscovery", return_value=discovery),
        patch("hoyo_cli.commands.init.SystemBrowserImporter", return_value=browser),
        patch.object(
            browser,
            "import_session",
            side_effect=browser_error(ErrorCode.BROWSER_ENCRYPTION_UNSUPPORTED),
        ) as reader,
        patch.object(discovery, "discover", wraps=discovery.discover) as verify,
        patch("click.testing._NamedTextIOWrapper.isatty", isatty),
    ):
        result = CliRunner().invoke(
            main,
            arguments,
            input=answers,
            color=True,
            env={
                "COLUMNS": str(width),
                "NO_COLOR": "1" if mode == "NO_COLOR" else None,
                "TERM": "dumb" if mode == "dumb" else "xterm-256color",
            },
        )
    assert result.exit_code == 0, result.output
    output = plain(result.stdout)
    assert all(len(line) <= width for line in output.splitlines()), [
        line for line in output.splitlines() if len(line) > width
    ]
    assert "Traveler (Asia)" in output and "Traveler (America)" in output
    assert "800000001" in output and "600000001" in output
    assert "os_asia" not in result.output and "os_usa" not in result.output
    assert "hidden-synthetic" not in result.output
    assert "Cancel (no changes)" in output and "Default" in output
    assert "Review" in output and "Configured profile" in output
    assert output.count("Profile settings [1]:") == (1 if route == "edit" else 0)
    allowed = mode in ("tty", "stderr-pipe")
    assert ("\x1b[" in result.stdout) == allowed
    assert not re.search(r"\x1b\[[0-9;?]*[JKHhl]", result.output)
    verify.assert_called_once()
    if route == "browser":
        reader.assert_called_once_with("edge")
        assert "Browser recovery" in output
        assert (
            "\x1b[33m" in result.stderr
            if mode == "tty"
            else "\x1b" not in result.stderr
        )
    else:
        reader.assert_not_called()
        assert result.stderr == ""
    config = load_config(path)
    assert config.default_profile == "main"
    assert config.game_order == previous_order
    assert config.profiles["second" if route == "add" else "main"].games[
        "genshin"
    ] == GameSelection(True, 600000001)
    if route in ("add", "edit"):
        assert secrets.read_bytes() == previous_secrets


@pytest.mark.parametrize("route", ["order", "editor"])
@pytest.mark.parametrize("save", [True, False])
def test_menu_success_requires_commit_and_cancel_keeps_bytes(
    tmp_path: Path, route: str, save: bool
) -> None:
    path = tmp_path / "config.toml"
    save_profile(
        path,
        "main",
        ProfileSeed("keyring", {"genshin": GameSelection(True, 800000001)}),
        make_default=True,
        game_order=GAME_IDS,
    )
    before = path.read_bytes()
    answers = ("n\n3\n1\n1\n" if route == "order" else "2\n") + (
        "1\n" if save else "2\n"
    )
    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch(
            "hoyo_cli.commands.init.detected_editors",
            return_value={"Synthetic editor": ("synthetic-editor", "--wait")},
        ),
        patch("hoyo_cli.commands.init.resolve_editor_command"),
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
    ):
        result = CliRunner().invoke(main, ["config", route], input=answers, color=True)
    assert result.exit_code == (0 if save else 1), result.output
    assert ("\x1b[32m" in result.output) == save
    assert (path.read_bytes() != before) == save
    assert (
        "cancelled; no changes saved" in result.output
        if not save
        else "Review" in result.output
    )


@pytest.mark.parametrize("width", [48, 70, 100])
def test_long_literal_choices_and_prompt_keep_numbers_cancel_and_default(
    width: int,
) -> None:
    label = "Traveler (" + "Long server name " * 8 + ") | UID 800000001"
    editor = "C:/Program Files/" + "[red]folder[/red]/" * 12 + "editor.exe"
    selected: list[str] = []

    @click.command()
    def command() -> None:
        presenter = SetupPresenter(no_color=True)
        adapter = ClickSetupIO(presenter)
        selected.append(adapter.choose("Character for genshin", (label,)))
        selected.append(
            adapter.choose(
                "Editor executable (full path or PATH name; no quotes)", (editor,)
            )
        )

    result = CliRunner().invoke(command, input="\n\n", env={"COLUMNS": str(width)})
    assert result.exit_code == 0, result.output
    assert selected == [label, editor]
    assert all(len(line) <= width for line in result.output.splitlines())
    assert result.output.count("2. Cancel (no changes)") == 2
    assert "  1. Traveler" in result.output and "  1. C:/Program" in result.output
    assert result.output.count("[1]:") == 2
    assert "UID 800000001" in result.output
    assert "[red]folder[/red]" in result.output
