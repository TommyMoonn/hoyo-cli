"""Offline preferred-editor schema, transactions, selection, and launch behavior."""

import os
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from subprocess import CompletedProcess, list2cmdline
from typing import cast
from unittest.mock import patch

import click
import pytest
import tomlkit
from click.testing import CliRunner
from test_config import config_document as config_document
from test_init import FakeBrowser, FakeDiscovery, FakeIO
from tomlkit.toml_document import TOMLDocument

from hoyo_cli.cli import main
from hoyo_cli.commands.config import edit_config, run_editor
from hoyo_cli.commands.init import run_init, select_editor
from hoyo_cli.config.editor import (
    detected_editors,
    parse_editor_arguments,
    resolve_editor,
    resolve_editor_command,
)
from hoyo_cli.config.models import parse_config
from hoyo_cli.config.repository import commit_config, load_config, prepare_editor
from hoyo_cli.errors import ConfigError
from hoyo_cli.render.management import ManagementPresenter
from hoyo_cli.render.setup import SetupPresenter


def available_executable(name: str) -> str:
    return name


def only_executable(expected: str, name: str) -> str | None:
    return name if name == expected else None


def test_old_config_has_no_saved_editor(config_document: TOMLDocument) -> None:
    assert parse_config(config_document).editor_command is None


def test_editor_argv_is_immutable(config_document: TOMLDocument) -> None:
    config_document["editor"] = {"command": ["zed", "--wait"]}
    assert parse_config(config_document).editor_command == ("zed", "--wait")


@pytest.mark.parametrize(
    "command",
    [[], "zed --wait", [1], [""], ["zed", ""], ["zed\n"], ["zed\0"], ["zed\x7f"]],
)
def test_bad_saved_argv_is_rejected(
    config_document: TOMLDocument, command: object
) -> None:
    config_document["editor"] = {"command": command}
    with pytest.raises(ConfigError, match="editor.command"):
        parse_config(config_document)


def test_saved_preference_overrides_environment(
    tmp_path: Path,
    config_document: TOMLDocument,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_document["editor"] = {"command": ["zed", "--wait"]}
    path = tmp_path / "config.toml"
    path.write_text(tomlkit.dumps(config_document), encoding="utf-8")
    monkeypatch.setenv("VISUAL", "other-editor")
    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch("shutil.which", side_effect=available_executable),
        patch("subprocess.run", return_value=CompletedProcess([], 0)) as launch,
    ):
        result = runner.invoke(main, ["config", "--edit"])
    assert result.exit_code == 0, result.output
    assert launch.call_args.args[0] == ["zed", "--wait", str(path)]
    assert launch.call_args.kwargs["shell"] is False
    assert "validated" in result.output.lower()


def test_invalid_waited_edit_is_preserved(
    tmp_path: Path, config_document: TOMLDocument, runner: CliRunner
) -> None:
    config_document["editor"] = {"command": ["zed", "--wait"]}
    path = tmp_path / "config.toml"
    path.write_text(tomlkit.dumps(config_document), encoding="utf-8")

    def edit(*args: object, **kwargs: object) -> CompletedProcess[str]:
        path.write_text("version = [", encoding="utf-8")
        return CompletedProcess([], 0)

    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch("shutil.which", side_effect=available_executable),
        patch("subprocess.run", side_effect=edit),
    ):
        result = runner.invoke(main, ["config", "--edit"])
    assert result.exit_code == 2, result.output
    assert "TOML" in result.output and "preserved" in result.output
    assert path.read_text() == "version = ["


def test_config_editor_cancels_without_secret_access(
    tmp_path: Path, config_document: TOMLDocument, runner: CliRunner
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(tomlkit.dumps(config_document), encoding="utf-8")
    original = path.read_bytes()
    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch("hoyo_cli.commands.init.open_store") as secrets,
    ):
        result = runner.invoke(main, ["config", "editor"], input="")
    assert result.exit_code == 1, result.output
    assert "Editor cancelled" in result.output
    assert path.read_bytes() == original
    secrets.assert_not_called()


class EditorIO:
    def __init__(self, selection: str, *, save: bool = True) -> None:
        self.selection = selection
        self.save = save
        self.choices: list[str] = []

    def choose(self, prompt: str, choices: Sequence[str]) -> str:
        if prompt == "Editor":
            self.choices = list(choices)
            return self.selection
        if prompt == "Save editor preference?":
            return "Save" if self.save else "Cancel (no changes)"
        raise AssertionError(prompt)

    def confirm(self, prompt: str) -> bool:
        raise AssertionError(prompt)

    def secret(self, prompt: str) -> str:
        raise AssertionError("Editor selection must not request secrets")


def test_editor_picker_saves_clears_and_cancels(
    tmp_path: Path, config_document: TOMLDocument
) -> None:
    path = tmp_path / "config.toml"
    config_document["editor"] = {"command": ["old-editor"], "future": "retained"}
    path.write_bytes(tomlkit.dumps(config_document).encode())
    original = path.read_bytes()
    with patch("shutil.which", side_effect=partial(only_executable, "zed")):
        io = EditorIO("Zed")
        run_editor(path, io)
        assert "VS Code" not in io.choices and "Sublime Text" not in io.choices
        assert load_config(path).editor_command == ("zed", "--wait")
        changed = path.read_bytes()
        with pytest.raises(click.Abort):
            run_editor(path, EditorIO("Use VISUAL/EDITOR", save=False))
        assert path.read_bytes() == changed
        run_editor(path, EditorIO("Use VISUAL/EDITOR"))
    assert load_config(path).editor_command is None
    assert path.read_bytes() == original.replace(b'command = ["old-editor"]\n', b"")


@pytest.mark.parametrize("name", ["code", "zed", "zeditor", "subl", "notepad.exe"])
def test_each_native_preset_resolves_exact_argv(name: str) -> None:
    arguments = (name,) if name == "notepad.exe" else (name, "--wait")
    with patch("shutil.which", return_value=name):
        resolved = resolve_editor_command(arguments)
    assert resolved.arguments == arguments
    expected_wait = name != "notepad.exe" and not (os.name == "nt" and name == "code")
    assert resolved.waits is expected_wait


def test_linux_zeditor_alias_is_detected_without_notepad() -> None:
    with (
        patch("hoyo_cli.config.editor.os.name", "posix"),
        patch("shutil.which", side_effect=partial(only_executable, "zeditor")),
    ):
        assert detected_editors() == {"Zed": ("zeditor", "--wait")}


@pytest.mark.parametrize(
    "command",
    [
        ("/opt/My Editor/bin/edit", "two words", 'a"quote'),
        (
            r"C:\Program Files\Editor\edit.exe",
            "two words",
            'a"quote',
            "trailing " + chr(92),
        ),
    ],
)
def test_custom_path_and_arguments_are_not_reparsed(command: tuple[str, ...]) -> None:
    with patch("shutil.which", return_value=command[0]):
        assert resolve_editor_command(command).arguments == command


@pytest.mark.skipif(os.name != "nt", reason="Native Windows argv rules")
def test_windows_environment_quoted_argv_roundtrips() -> None:
    arguments = [r"C:\Program Files\Editor\edit.exe", 'literal"quote', "end \\"]
    assert parse_editor_arguments(list2cmdline(arguments)) == tuple(arguments)


def test_unix_environment_quotes_and_invalid_argv() -> None:
    with patch("hoyo_cli.config.editor.os.name", "posix"):
        assert parse_editor_arguments("'/editor path/edit' --title='two words'") == (
            "/editor path/edit",
            "--title=two words",
        )
        with pytest.raises(ConfigError):
            parse_editor_arguments("'unclosed")


@pytest.mark.parametrize("visual", [None, "", "  ", "visual --wait"])
def test_environment_priority(
    visual: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    if visual is not None:
        monkeypatch.setenv("VISUAL", visual)
    monkeypatch.setenv("EDITOR", "secondary")
    with patch("shutil.which", side_effect=available_executable):
        assert resolve_editor(None).arguments[0] == (
            "visual" if visual == "visual --wait" else "secondary"
        )


def test_missing_saved_executable_does_not_silently_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISUAL", "zed")
    with patch("shutil.which", side_effect=partial(only_executable, "zed")):
        with pytest.raises(ConfigError, match="unavailable") as error:
            resolve_editor(("moved-editor",))
    assert "hoyo config editor" in str(error.value.hint)


@pytest.mark.parametrize("versioned", [False, True])
@pytest.mark.skipif(os.name != "nt", reason="VS Code Windows native CLI layout")
def test_windows_code_shim_uses_native_cli_without_a_shell(
    tmp_path: Path, versioned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "VS Code with spaces"
    shim = root / "bin/code.cmd"
    cli = root / (
        "0123456789/resources/app/out/cli.js"
        if versioned
        else "resources/app/out/cli.js"
    )
    runtime = root / "Code.exe"
    shim.parent.mkdir(parents=True)
    cli.parent.mkdir(parents=True)
    shim.write_text("uninterpreted batch file")
    cli.write_text("synthetic CLI")
    runtime.touch()
    monkeypatch.setenv("VSCODE_DEV", "must-not-propagate")
    before = dict(os.environ)
    with (
        patch("shutil.which", return_value=str(shim)),
        patch("subprocess.run", return_value=CompletedProcess([], 0)) as launch,
    ):
        editor = resolve_editor_command(("code", "--wait"))
        assert editor.waits and editor.node_cli
        assert edit_config(tmp_path / "config.toml", editor) == 0
    assert launch.call_args.args[0] == [
        str(runtime),
        str(cli),
        "--wait",
        str(tmp_path / "config.toml"),
    ]
    assert launch.call_args.kwargs["shell"] is False
    assert launch.call_args.kwargs["env"]["ELECTRON_RUN_AS_NODE"] == "1"
    assert "VSCODE_DEV" not in launch.call_args.kwargs["env"]
    assert dict(os.environ) == before


@pytest.mark.parametrize("name", ["custom.cmd", "custom.bat", "code.cmd"])
def test_unresolvable_batch_commands_are_rejected(name: str, tmp_path: Path) -> None:
    with patch("shutil.which", return_value=str(tmp_path / "bin" / name)):
        with pytest.raises(ConfigError, match="shell launcher"):
            resolve_editor_command((name, "--wait"))


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_editor_targeted_update_preserves_exact_unrelated_bytes(
    tmp_path: Path, config_document: TOMLDocument, line_ending: bytes
) -> None:
    config_document["editor"] = {"command": ["old"], "future": "retained"}
    original = (
        tomlkit.dumps(config_document)
        .encode()
        .replace(b'command = ["old"]', b'command = ["old"] # keep comment')
        .replace(b"\n", line_ending)
    )
    path = tmp_path / "config.toml"
    path.write_bytes(original)
    prepared = prepare_editor(path, ("zed", "--wait"))
    assert path.read_bytes() == original
    commit_config(prepared)
    assert path.read_bytes() == original.replace(b'["old"]', b'["zed", "--wait"]')


@pytest.mark.parametrize("failure", ["conflict", "interrupt", "symlink", "invalid"])
def test_editor_transaction_failures_preserve_config(
    tmp_path: Path, config_document: TOMLDocument, failure: str
) -> None:
    path = tmp_path / "config.toml"
    original = tomlkit.dumps(config_document).encode()
    path.write_bytes(original)
    prepared = prepare_editor(path, ("zed", "--wait"))
    if failure == "conflict":
        original += b"\n# concurrent change\n"
        path.write_bytes(original)
        with pytest.raises(ConfigError, match="changed"):
            commit_config(prepared)
    elif failure == "interrupt":
        with patch("os.replace", side_effect=OSError("private-marker")):
            with pytest.raises(ConfigError, match="Unable to save"):
                commit_config(prepared)
        assert not list(tmp_path.glob("*.tmp"))
    elif failure == "symlink":
        with patch.object(Path, "is_symlink", return_value=True):
            with pytest.raises(ConfigError, match="symbolic link"):
                prepare_editor(path, ("zed",))
            with pytest.raises(ConfigError, match="symbolic link"):
                commit_config(prepared)
    else:
        with pytest.raises(ConfigError):
            prepare_editor(path, cast(tuple[str, ...], ("zed", 42)))
    assert path.read_bytes() == original


def test_selection_conflict_before_review_is_detected(
    tmp_path: Path, config_document: TOMLDocument
) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(tomlkit.dumps(config_document).encode())

    class ConflictIO(EditorIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Editor":
                path.write_bytes(path.read_bytes() + b"\n# concurrent\n")
            return super().choose(prompt, choices)

    with pytest.raises(ConfigError, match="changed during editor selection"):
        run_editor(path, ConflictIO("Use VISUAL/EDITOR"))
    assert path.read_bytes().endswith(b"# concurrent\n")


def test_keep_current_performs_no_write(
    tmp_path: Path, config_document: TOMLDocument
) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(tomlkit.dumps(config_document).encode())
    with patch("hoyo_cli.commands.config.commit_config") as commit:
        run_editor(path, EditorIO("Keep current"))
    commit.assert_not_called()


def test_custom_picker_enters_path_and_each_argument_without_parsing() -> None:
    executable = r"C:\Program Files\Editor\edit.exe"

    class CustomIO(EditorIO):
        def __init__(self) -> None:
            super().__init__("Custom executable/arguments")
            self.arguments = iter(["two words", 'a"quote'])
            self.add = iter([True, True, False])

        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt.startswith("Editor executable"):
                return executable
            if prompt == "Editor argument":
                return next(self.arguments)
            return super().choose(prompt, choices)

        def confirm(self, prompt: str) -> bool:
            assert prompt == "Add an editor argument?"
            return next(self.add)

    with (
        patch(
            "shutil.which",
            side_effect=partial(only_executable, executable),
        ),
        patch("subprocess.run") as launch,
    ):
        command = select_editor(CustomIO(), SetupPresenter(no_color=True), None)
    assert command == (executable, "two words", 'a"quote')
    launch.assert_not_called()


@pytest.mark.parametrize("save", [True, False])
def test_first_run_editor_selection_is_reviewed_and_saved_atomically(
    tmp_path: Path, save: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    class FirstRunIO(FakeIO):
        selected = False

        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Save profile configuration and credentials?":
                assert "Editor preference" in choices
                if not self.selected:
                    self.selected = True
                    return "Editor preference"
                return "Save" if save else "Cancel (no changes)"
            if prompt == "Editor":
                assert "Zed" in choices
                return "Zed"
            return super().choose(prompt, choices)

    config, credentials = tmp_path / "config.toml", tmp_path / "credentials.toml"
    io = FirstRunIO()
    with (
        patch("shutil.which", side_effect=partial(only_executable, "zed")),
        patch("subprocess.run") as launch,
    ):
        if save:
            run_init(config, credentials, io, FakeBrowser(), FakeDiscovery())
            assert load_config(config).editor_command == ("zed", "--wait")
            assert credentials.exists()
        else:
            with pytest.raises(click.Abort):
                run_init(config, credentials, io, FakeBrowser(), FakeDiscovery())
            assert not config.exists() and not credentials.exists()
    launch.assert_not_called()
    output = capsys.readouterr().out
    assert "Editor: ['zed', '--wait']" in output
    assert "test-secret-123" not in output


@pytest.mark.parametrize("command", [("custom",), ("notepad.exe",), ("zed",)])
def test_nonblocking_editor_does_not_claim_completed_validation(
    tmp_path: Path,
    config_document: TOMLDocument,
    runner: CliRunner,
    command: tuple[str, ...],
) -> None:
    config_document["editor"] = {"command": list(command)}
    path = tmp_path / "config.toml"
    path.write_bytes(tomlkit.dumps(config_document).encode())
    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch("shutil.which", side_effect=available_executable),
        patch("subprocess.run", return_value=CompletedProcess([], 0)),
        patch("hoyo_cli.commands.config.load_config", wraps=load_config) as read,
    ):
        result = runner.invoke(main, ["config", "--edit"])
    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "unconfirmed" in output and "run hoyo config" in output
    assert "validated" not in result.output
    assert read.call_count == 1


def test_inspection_marks_unavailable_saved_executable(
    config_document: TOMLDocument,
) -> None:
    config_document["editor"] = {"command": ["missing-editor"]}
    with patch("shutil.which", return_value=None):
        presenter = ManagementPresenter(no_color=True)
        with presenter.console.capture() as captured:
            presenter.config(parse_config(config_document), Path("config.toml"))
        result = captured.get()
    assert "missing-editor" in result and "Editor executable: unavailable" in result


@pytest.mark.parametrize("value", ["zed", ["zed"], 5])
def test_editor_requires_a_table(config_document: TOMLDocument, value: object) -> None:
    config_document["editor"] = value
    with pytest.raises(ConfigError, match="editor must be a table"):
        parse_config(config_document)


def test_cleared_preference_keeps_unknown_editor_fields(
    config_document: TOMLDocument,
) -> None:
    config_document["editor"] = {"future": "retained"}
    assert parse_config(config_document).editor_command is None


@pytest.mark.parametrize("failure", ["launch", "exit", "schema", "missing"])
def test_edit_failures_are_actionable_and_preserve_user_edits(
    tmp_path: Path, config_document: TOMLDocument, runner: CliRunner, failure: str
) -> None:
    config_document["editor"] = {"command": ["zed", "--wait"]}
    path = tmp_path / "config.toml"
    path.write_bytes(tomlkit.dumps(config_document).encode())
    expected = path.read_bytes()
    if failure == "schema":
        expected = expected.replace(b"version = 2", b"version = 3")

    def edit(*args: object, **kwargs: object) -> CompletedProcess[str]:
        if failure == "launch":
            raise OSError("private-diagnostic")
        if failure == "schema":
            path.write_bytes(expected)
        return CompletedProcess([], 9 if failure == "exit" else 0)

    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch(
            "shutil.which",
            side_effect=available_executable if failure != "missing" else None,
            return_value=None,
        ),
        patch("subprocess.run", side_effect=edit) as launch,
    ):
        result = runner.invoke(main, ["config", "--edit"])
    assert result.exit_code == (1 if failure == "exit" else 2), result.output
    assert "hoyo config" in " ".join(result.output.split())
    assert "".join(str(path).split()) in "".join(result.output.split())
    assert (
        "private-diagnostic" not in result.output and "Traceback" not in result.output
    )
    assert path.read_bytes() == expected
    if failure == "missing":
        launch.assert_not_called()
    else:
        assert launch.call_args.kwargs["shell"] is False


def test_config_editor_cli_changes_only_global_editor_without_account_resolution(
    tmp_path: Path,
    config_document: TOMLDocument,
    runner: CliRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(tomlkit.dumps(config_document).encode())
    original = load_config(path)
    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch("shutil.which", side_effect=partial(only_executable, "zed")),
        patch("hoyo_cli.commands.init.open_store") as store,
        patch("subprocess.run") as launch,
    ):
        result = runner.invoke(
            main,
            ["--profile", "unknown", "config", "editor", "--no-color"],
            input="2\n1\n",
        )
    assert result.exit_code == 0, result.output
    assert "Review editor" in result.output and "['zed', '--wait']" in result.output
    assert "\x1b[" not in result.output
    after = load_config(path)
    assert after.editor_command == ("zed", "--wait")
    assert after.profiles == original.profiles
    assert (after.default_profile, after.game_order, after.display_color) == (
        original.default_profile,
        original.game_order,
        original.display_color,
    )
    store.assert_not_called()
    launch.assert_not_called()


def test_custom_picker_recovers_from_missing_executable_without_launching() -> None:
    class RecoveryIO(EditorIO):
        attempted = False

        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Editor":
                if self.attempted:
                    return "Use VISUAL/EDITOR"
                self.attempted = True
                return "Custom executable/arguments"
            if prompt.startswith("Editor executable"):
                return "missing-executable"
            return super().choose(prompt, choices)

        def confirm(self, prompt: str) -> bool:
            return False

    with patch("shutil.which", return_value=None), patch("subprocess.run") as launch:
        assert (
            select_editor(RecoveryIO(""), SetupPresenter(no_color=True), None) is None
        )
    launch.assert_not_called()


def test_editor_conflict_after_confirmation_preserves_concurrent_change(
    tmp_path: Path, config_document: TOMLDocument
) -> None:
    config_document["editor"] = {"command": ["old"]}
    path = tmp_path / "config.toml"
    path.write_bytes(tomlkit.dumps(config_document).encode())

    class ConflictIO(EditorIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Save editor preference?":
                path.write_bytes(path.read_bytes() + b"\n# after review\n")
            return super().choose(prompt, choices)

    with pytest.raises(ConfigError, match="changed before saving"):
        run_editor(path, ConflictIO("Use VISUAL/EDITOR"))
    assert load_config(path).editor_command == ("old",)
    assert path.read_bytes().endswith(b"# after review\n")
