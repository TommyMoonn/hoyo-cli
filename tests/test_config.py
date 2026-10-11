"""Offline configuration schema and account-selection tests."""

import os
from collections.abc import MutableMapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from subprocess import CompletedProcess, list2cmdline
from typing import cast
from unittest.mock import patch

import pytest
import tomlkit
from click.testing import CliRunner
from profile_fixtures import ProfileSeed, save_profile
from tomlkit.toml_document import TOMLDocument

from hoyo_cli.cli import main
from hoyo_cli.commands.config import edit_config, show_config
from hoyo_cli.config import repository
from hoyo_cli.config.editor import ResolvedEditor, parse_editor_arguments
from hoyo_cli.config.models import (
    GameSelection,
    ProfileConfig,
    enabled_games,
    parse_config,
    resolve_profile,
)
from hoyo_cli.config.paths import cache_root, config_path, credentials_path
from hoyo_cli.config.repository import (
    commit_config,
    load_config,
    prepare_game_order,
)
from hoyo_cli.errors import ConfigError
from hoyo_cli.models.common import GameId


def _fake_executable(name: str) -> str:
    return name


def test_save_refuses_detected_symlink_before_io(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    account = ProfileSeed("keyring", {"genshin": GameSelection(True, 111111111)})
    with patch.object(Path, "is_symlink", return_value=True):
        with pytest.raises(ConfigError, match="symbolic link"):
            save_profile(path, "main", account, make_default=True, game_order=())
    assert not path.exists()


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_order_transaction_changes_only_order_and_preserves_comments(
    tmp_path: Path, config_document: TOMLDocument, line_ending: bytes
) -> None:
    path = tmp_path / "config.toml"
    original = (
        tomlkit.dumps(config_document)
        .replace(
            'game_order = ["zzz", "genshin"]',
            'game_order = ["zzz", "genshin"] # display order',
        )
        .encode()
        .replace(b"\n", line_ending)
    )
    path.write_bytes(original)
    prepared = prepare_game_order(path, ("hsr", "zzz", "genshin"))
    assert path.read_bytes() == original
    assert prepared.original == original
    commit_config(prepared)
    assert path.read_bytes() == original.replace(
        b'game_order = ["zzz", "genshin"]', b'game_order = ["hsr", "zzz", "genshin"]'
    )


@pytest.mark.parametrize("order", [("zzz", "zzz"), ("unknown",)])
def test_invalid_order_candidate_is_rejected_before_writing(
    tmp_path: Path, config_document: TOMLDocument, order: tuple[str, ...]
) -> None:
    path = tmp_path / "config.toml"
    original = tomlkit.dumps(config_document).encode()
    path.write_bytes(original)
    with pytest.raises(ConfigError):
        prepare_game_order(path, cast(tuple[GameId, ...], order))
    assert path.read_bytes() == original


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_interrupted_order_write_retains_exact_original_and_cleans_temporary_file(
    tmp_path: Path,
    config_document: TOMLDocument,
    failure: type[BaseException],
    operation: str,
) -> None:
    path = tmp_path / "config.toml"
    original = tomlkit.dumps(config_document).encode()
    path.write_bytes(original)
    prepared = prepare_game_order(path, ("hsr", "zzz", "genshin"))
    with patch.object(repository.os, operation, side_effect=failure("private-marker")):
        with pytest.raises(ConfigError if failure is OSError else KeyboardInterrupt):
            commit_config(prepared)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("stage", ["prepare", "commit"])
def test_order_transaction_refuses_detected_symlink(
    tmp_path: Path, config_document: TOMLDocument, stage: str
) -> None:
    path = tmp_path / "config.toml"
    original = tomlkit.dumps(config_document).encode()
    path.write_bytes(original)
    prepared = prepare_game_order(path, ("hsr", "zzz", "genshin"))
    with patch.object(Path, "is_symlink", return_value=True):
        with pytest.raises(ConfigError, match="symbolic link"):
            if stage == "prepare":
                prepare_game_order(path, ("hsr", "zzz", "genshin"))
            else:
                commit_config(prepared)
    assert path.read_bytes() == original


@pytest.mark.parametrize("dangling", [False, True])
def test_save_refuses_symlink_without_changing_target(
    tmp_path: Path, config_document: TOMLDocument, dangling: bool
) -> None:
    target = tmp_path / "managed.toml"
    original = tomlkit.dumps(config_document).encode()
    if not dangling:
        target.write_bytes(original)
    path = tmp_path / "config.toml"
    try:
        path.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Symlink creation is unavailable: {error}")
    account = parse_config(config_document).profiles["main"]
    with pytest.raises(ConfigError, match="symbolic link"):
        save_profile(path, "main", account, make_default=True, game_order=())
    assert path.is_symlink()
    assert target.read_bytes() == original if not dangling else not target.exists()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.fixture
def config_document() -> TOMLDocument:
    return tomlkit.parse(
        """# Keep this comment
version = 2
default_profile = "main"
game_order = ["zzz", "genshin"]
unknown_root = "private-marker"

[display]
color = "auto"
unknown_display = "keep"

[logins.main]
credential_backend = "keyring" # backend comment
credential_key = "main"
[profiles.main]
login = "main"
unknown_account = "private-marker"

[profiles.main.games.genshin]
enabled = true
uid = 111111111 # UID comment
unknown_game = "keep"

[profiles.main.games.hsr]
enabled = true
uid = 222222222

[profiles.main.games.zzz]
enabled = false
uid = 333333333

[logins.alt]
credential_backend = "plaintext"
credential_key = "alt"
[profiles.alt]
login = "alt"

[profiles.alt.games.zzz]
enabled = true
uid = 444444444
"""
    )


def test_v1_example_parses(config_document: TOMLDocument) -> None:
    config = parse_config(config_document)
    assert config.version == 2
    assert config.default_profile == "main"
    assert config.game_order == ("zzz", "genshin")
    assert config.display_color == "auto"
    assert config.profiles["main"].games["hsr"].uid == 222222222
    assert config.logins["alt"].credential_backend == "plaintext"


def test_exact_account_match(config_document: TOMLDocument) -> None:
    config = parse_config(config_document)
    assert resolve_profile(config, "alt") == ("alt", config.profiles["alt"])
    assert resolve_profile(config, None)[0] == "main"
    for requested in ("a", "Alt", " main", "unknown"):
        with pytest.raises(ConfigError):
            resolve_profile(config, requested)


def test_missing_default_does_not_fall_back(config_document: TOMLDocument) -> None:
    config_document["default_profile"] = "missing"
    with pytest.raises(ConfigError, match="default_profile"):
        parse_config(config_document)


def test_game_order_appends_enabled_unlisted_games(
    config_document: TOMLDocument,
) -> None:
    config = parse_config(config_document)
    assert enabled_games(config, config.profiles["main"]) == ("genshin", "hsr")
    config_document["game_order"] = []
    config_document["profiles"]["main"]["games"]["zzz"]["enabled"] = True
    config = parse_config(config_document)
    assert enabled_games(config, config.profiles["main"]) == ("genshin", "hsr", "zzz")


@pytest.mark.parametrize("order", [["hsr", "hsr"], ["unknown"], [True], "genshin"])
def test_duplicate_or_unknown_game_order_fails(
    config_document: TOMLDocument, order: object
) -> None:
    config_document["game_order"] = order
    with pytest.raises(ConfigError, match="game_order"):
        parse_config(config_document)


@pytest.mark.parametrize("uid", ["private-marker", True, 0, -1, 1.5])
def test_bad_uid_reports_field_path(config_document: TOMLDocument, uid: object) -> None:
    config_document["profiles"]["main"]["games"]["hsr"]["uid"] = uid
    with pytest.raises(ConfigError, match=r"profiles.main.games.hsr.uid") as caught:
        parse_config(config_document)
    assert caught.value.exit_code == 2
    assert "private-marker" not in str(caught.value)


def test_no_enabled_games_is_error(config_document: TOMLDocument) -> None:
    for game in ("genshin", "hsr", "zzz"):
        config_document["profiles"]["main"]["games"][game]["enabled"] = False
    config = parse_config(config_document)
    with pytest.raises(ConfigError, match="enabled games"):
        enabled_games(config, config.profiles["main"])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("version", True),
        ("version", 3),
        ("default_profile", ""),
        ("default_profile", 1),
        ("profiles", []),
        ("display", "private-marker"),
    ],
)
def test_invalid_root_fields_are_classified(
    config_document: TOMLDocument, key: str, value: object
) -> None:
    config_document[key] = value
    with pytest.raises(ConfigError, match=key):
        parse_config(config_document)


@pytest.mark.parametrize(
    "key", ["version", "default_profile", "game_order", "display", "profiles"]
)
def test_missing_required_fields(config_document: TOMLDocument, key: str) -> None:
    del config_document[key]
    with pytest.raises(ConfigError, match=key):
        parse_config(config_document)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("display", "color", "private-marker"),
        ("profile", "credential_backend", "private-marker"),
        ("game", "enabled", "yes"),
        ("game", "enabled", 1),
    ],
)
def test_nested_fields_are_validated(
    config_document: TOMLDocument, section: str, field: str, value: object
) -> None:
    table = config_document["display"]
    if section == "profile":
        table = config_document["logins"]["main"]
    elif section == "game":
        table = config_document["profiles"]["main"]["games"]["hsr"]
    table[field] = value
    with pytest.raises(ConfigError, match=field) as caught:
        parse_config(config_document)
    assert "private-marker" not in str(caught.value)


def test_config_models_are_immutable(config_document: TOMLDocument) -> None:
    config = parse_config(config_document)
    with pytest.raises(FrozenInstanceError):
        config.__setattr__("version", 2)
    with pytest.raises(TypeError):
        cast(MutableMapping[str, ProfileConfig], config.profiles)["other"] = (
            config.profiles["main"]
        )
    with pytest.raises(TypeError):
        cast(MutableMapping[GameId, GameSelection], config.profiles["main"].games)[
            "zzz"
        ] = config.profiles["main"].games["hsr"]


def test_config_paths_use_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert config_path() == tmp_path / ".hoyo" / "config.toml"
    assert credentials_path() == tmp_path / ".hoyo" / "credentials.toml"
    assert config_path(tmp_path / "other") == tmp_path / "other/.hoyo/config.toml"
    assert not (tmp_path / ".hoyo").exists()


def test_cache_path_uses_platformdirs(tmp_path: Path) -> None:
    with patch(
        "hoyo_cli.config.paths.platformdirs.user_cache_path", return_value=tmp_path
    ) as lookup:
        assert cache_root() == tmp_path
    lookup.assert_called_once_with("hoyo", appauthor=False)


def test_round_trip_preserves_comments_unknown_keys_and_order(
    tmp_path: Path, config_document: TOMLDocument
) -> None:
    path = tmp_path / "config.toml"
    original = tomlkit.dumps(config_document)
    path.write_text(original, encoding="utf-8")
    account = ProfileSeed(
        "plaintext",
        {
            "genshin": GameSelection(False, 999999999),
            "hsr": GameSelection(True, 222222222),
        },
    )
    save_profile(path, "main", account, make_default=False, game_order=("hsr",))
    updated = path.read_text(encoding="utf-8")
    assert "# Keep this comment" in updated
    assert "# backend comment" in updated
    assert "# UID comment" in updated
    assert original[original.index("[profiles.alt]") :] in updated
    document = tomlkit.parse(updated)
    assert document["unknown_root"] == "private-marker"
    assert document["display"]["unknown_display"] == "keep"
    assert document["profiles"]["main"]["unknown_account"] == "private-marker"
    assert document["profiles"]["main"]["games"]["genshin"]["unknown_game"] == "keep"
    assert updated.index("[profiles.main]") < updated.index("[profiles.alt]")
    config = load_config(path)
    assert config.profiles["main"].games["genshin"].uid == 999999999
    assert config.profiles["alt"].games["zzz"].uid == 444444444
    assert config.game_order == ("hsr",)


@pytest.mark.parametrize(
    "invalid", ["version = [", 'version = 2\nsecret = "private-marker"']
)
def test_invalid_existing_config_is_not_rewritten(
    tmp_path: Path, config_document: TOMLDocument, invalid: str
) -> None:
    path = tmp_path / "config.toml"
    original = invalid.encode("utf-8")
    path.write_bytes(original)
    with pytest.raises(ConfigError):
        save_profile(
            path,
            "main",
            parse_config(config_document).profiles["main"],
            make_default=True,
            game_order=("hsr",),
        )
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("operation", ["replace", "fsync"])
def test_write_interruption_preserves_original_bytes(
    tmp_path: Path, config_document: TOMLDocument, operation: str
) -> None:
    path = tmp_path / "config.toml"
    original = tomlkit.dumps(config_document).encode("utf-8")
    path.write_bytes(original)
    with patch(
        f"hoyo_cli.config.repository.os.{operation}",
        side_effect=OSError("private-marker"),
    ):
        with pytest.raises(ConfigError) as caught:
            save_profile(
                path,
                "main",
                parse_config(config_document).profiles["main"],
                make_default=True,
                game_order=("hsr",),
            )
    assert "private-marker" not in str(caught.value)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_new_config_creates_only_non_secret_fields(tmp_path: Path) -> None:
    path = tmp_path / ".hoyo/config.toml"
    account = ProfileSeed("keyring", {"genshin": GameSelection(True, 111111111)})
    save_profile(path, "Main.Exact", account, make_default=False, game_order=())
    config = load_config(path)
    assert config.default_profile == "Main.Exact"
    assert config.profiles["Main.Exact"].games == account.games
    assert config.display_color == "auto"
    assert enabled_games(config, config.profiles["Main.Exact"]) == ("genshin",)
    assert not credentials_path(tmp_path).exists()


def test_save_invalid_new_account_preserves_original(
    tmp_path: Path, config_document: TOMLDocument
) -> None:
    path = tmp_path / "config.toml"
    original = tomlkit.dumps(config_document).encode("utf-8")
    path.write_bytes(original)
    account = ProfileSeed("keyring", {"genshin": GameSelection(True, 0)})
    with pytest.raises(ConfigError):
        save_profile(path, "main", account, make_default=True, game_order=("genshin",))
    assert path.read_bytes() == original


def test_read_never_appends_missing_order_to_file(
    tmp_path: Path, config_document: TOMLDocument
) -> None:
    path = tmp_path / "config.toml"
    original = tomlkit.dumps(config_document).replace("\n", "\r\n").encode("utf-8")
    path.write_bytes(original)
    config = load_config(path)
    assert enabled_games(config, config.profiles["main"]) == ("genshin", "hsr")
    assert path.read_bytes() == original


@pytest.mark.parametrize("content", [None, b'bad = [ "private-marker"', b"\xff\xfe"])
def test_load_failures_report_path_without_raw_values(
    tmp_path: Path, content: bytes | None
) -> None:
    path = tmp_path / "config.toml"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert str(path) in str(caught.value)
    assert "private-marker" not in str(caught.value)


def test_config_shows_paths_names_and_uids_not_secrets(
    tmp_path: Path, config_document: TOMLDocument
) -> None:
    config_document["profiles"]["main"]["ltoken"] = "private-marker"
    config_document["display"]["secret"] = "private-marker"
    output = show_config(parse_config(config_document), tmp_path / "config.toml")
    for expected in (
        str(tmp_path / "config.toml"),
        "main",
        "alt",
        "keyring",
        "plaintext",
        "111111111",
        "222222222",
        "444444444",
        "zzz, genshin",
    ):
        assert expected in output
    assert "private-marker" not in output
    assert "ltoken" not in output
    assert "zzz: disabled, UID 333333333" in output
    assert "genshin: not configured" in output
    assert "hoyo config order" in output and "hoyo profile edit <name>" in output


@pytest.mark.parametrize("width", [48, 70, 100])
def test_config_inspection_preserves_long_names_and_disabled_selections_without_secrets(
    runner: CliRunner,
    tmp_path: Path,
    config_document: TOMLDocument,
    width: int,
) -> None:
    name = "A long account name [with brackets] and several servers"
    config_document["profiles"][name] = config_document["profiles"].pop("main")
    config_document["default_profile"] = name
    config = tmp_path / "config.toml"
    config.write_text(tomlkit.dumps(config_document), encoding="utf-8")
    before = config.read_bytes()
    with (
        patch("hoyo_cli.commands.config.config_path", return_value=config),
        patch("hoyo_cli.auth.credentials.open_store") as store,
        patch("hoyo_cli.auth.keyring_backend.KeyringStore.get") as keyring_get,
        patch("hoyo_cli.auth.plaintext_backend.PlaintextStore.get") as plaintext_get,
    ):
        result = runner.invoke(main, ["config", "--no-color"], terminal_width=width)
    assert result.exit_code == 0, result.output
    assert f"Default profile: {name}" in result.output
    assert f"Profile: {name}" in result.output
    assert "zzz: disabled, UID 333333333" in result.output
    assert "hsr: not configured" in result.output
    assert (
        "hoyo config order" in result.output
        and "hoyo profile edit <name>" in result.output
    )
    assert "private-marker" not in result.output and "\x1b" not in result.output
    store.assert_not_called()
    keyring_get.assert_not_called()
    plaintext_get.assert_not_called()
    assert config.read_bytes() == before


def test_config_edit_invokes_selected_editor(tmp_path: Path) -> None:
    path = tmp_path / "config with spaces.toml"
    path.write_text("version = 1", encoding="utf-8")
    with patch(
        "hoyo_cli.commands.config.subprocess.run", return_value=CompletedProcess([], 0)
    ) as launch:
        editor = ResolvedEditor(
            parse_editor_arguments('fake-editor --wait --title "two words"'), False
        )
        assert edit_config(path, editor) == 0
    launch.assert_called_once_with(
        ["fake-editor", "--wait", "--title", "two words", str(path)],
        check=False,
        shell=False,
    )


def test_empty_editor_arguments_are_classified() -> None:
    with pytest.raises(ConfigError, match="Invalid editor arguments"):
        parse_editor_arguments("")


def test_editor_launch_error_is_classified(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("version = 1", encoding="utf-8")
    with patch(
        "hoyo_cli.commands.config.subprocess.run",
        side_effect=FileNotFoundError("private-marker"),
    ):
        with pytest.raises(ConfigError) as caught:
            edit_config(path, ResolvedEditor(("missing-editor",), False))
    assert str(path) in str(caught.value)
    assert "private-marker" not in str(caught.value)


def test_editor_shell_metacharacters_remain_arguments(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    with patch(
        "hoyo_cli.commands.config.subprocess.run", return_value=CompletedProcess([], 0)
    ) as launch:
        edit_config(
            path,
            ResolvedEditor(
                parse_editor_arguments("fake-editor ; other-command"), False
            ),
        )
    assert launch.call_args.args[0] == ["fake-editor", ";", "other-command", str(path)]
    assert launch.call_args.kwargs["shell"] is False


def test_config_cli_reads_only_config(
    runner: CliRunner, tmp_path: Path, config_document: TOMLDocument
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(tomlkit.dumps(config_document), encoding="utf-8")
    secrets = tmp_path / "credentials.toml"
    secrets.write_text('ltoken = "private-marker"', encoding="utf-8")
    original = path.read_bytes()
    original_open = Path.open
    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch("pathlib.Path.open", autospec=True, side_effect=original_open) as reads,
    ):
        result = runner.invoke(main, ["config", "--no-color"])
    reads.assert_called_once()
    assert reads.call_args.args[0] == path
    assert result.exit_code == 0, result.output
    assert "main" in result.output
    assert "444444444" in result.output
    assert "private-marker" not in result.output
    assert "\x1b[" not in result.output
    assert path.read_bytes() == original


def test_config_cli_checks_exact_account(
    runner: CliRunner, tmp_path: Path, config_document: TOMLDocument
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(tomlkit.dumps(config_document), encoding="utf-8")
    with patch("hoyo_cli.commands.config.config_path", return_value=path):
        valid = runner.invoke(main, ["--profile", "alt", "config"])
        invalid = runner.invoke(main, ["config", "--profile", "a"])
    assert valid.exit_code == 0
    assert invalid.exit_code == 2
    assert "--profile" in invalid.output
    assert "Traceback" not in invalid.output


def test_invalid_config_exits_2_without_traceback(
    runner: CliRunner, tmp_path: Path
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('private = [ "private-marker"', encoding="utf-8")
    original = path.read_bytes()
    with patch("hoyo_cli.commands.config.config_path", return_value=path):
        result = runner.invoke(main, ["config"])
    assert result.exit_code == 2
    assert str(path) in result.output
    assert "private-marker" not in result.output
    assert "Traceback" not in result.output
    assert path.read_bytes() == original


def test_config_cli_editor_precedence(
    runner: CliRunner,
    tmp_path: Path,
    config_document: TOMLDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(tomlkit.dumps(config_document), encoding="utf-8")
    monkeypatch.setenv("VISUAL", "visual-editor --wait")
    monkeypatch.setenv("EDITOR", "secondary-editor")
    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch("shutil.which", side_effect=_fake_executable),
        patch(
            "hoyo_cli.commands.config.subprocess.run",
            return_value=CompletedProcess([], 0),
        ) as launch,
    ):
        result = runner.invoke(main, ["config", "--edit"])
    assert result.exit_code == 0, result.output
    assert launch.call_args.args[0] == ["visual-editor", "--wait", str(path)]


def test_config_cli_no_editor_is_actionable(
    runner: CliRunner,
    tmp_path: Path,
    config_document: TOMLDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(tomlkit.dumps(config_document), encoding="utf-8")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    with patch("hoyo_cli.commands.config.config_path", return_value=path):
        result = runner.invoke(main, ["config", "--edit"])
    assert result.exit_code == 2
    assert str(path) in result.output
    assert "VISUAL" in result.output
    assert "Traceback" not in result.output


def test_resolver_never_substitutes_another_default(
    config_document: TOMLDocument,
) -> None:
    config = replace(parse_config(config_document), default_profile="missing")
    with pytest.raises(ConfigError, match="default_profile"):
        resolve_profile(config, None)
    assert resolve_profile(config, "alt")[0] == "alt"


def test_unknown_game_configuration_is_rejected(config_document: TOMLDocument) -> None:
    config_document["profiles"]["main"]["games"]["unknown"] = {
        "enabled": True,
        "uid": 1,
    }
    with pytest.raises(ConfigError, match=r"profiles.main.games"):
        parse_config(config_document)


def test_model_snapshots_do_not_retain_mutable_inputs(
    config_document: TOMLDocument,
) -> None:
    games: dict[GameId, GameSelection] = {"genshin": GameSelection(True, 111111111)}
    account = ProfileConfig("main", games)
    games.clear()
    assert "genshin" in account.games
    accounts = {"main": account}
    config = replace(parse_config(config_document), profiles=accounts)
    accounts.clear()
    assert "main" in config.profiles


def test_noop_save_preserves_line_endings_and_formatting(
    tmp_path: Path, config_document: TOMLDocument
) -> None:
    path = tmp_path / "config.toml"
    original = tomlkit.dumps(config_document).replace("\n", "\r\n").encode("utf-8")
    path.write_bytes(original)
    config = load_config(path)
    save_profile(
        path,
        "main",
        config.profiles["main"],
        make_default=False,
        game_order=config.game_order,
    )
    assert path.read_bytes() == original


def test_replace_uses_a_closed_same_directory_temporary_file(
    tmp_path: Path, config_document: TOMLDocument
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(tomlkit.dumps(config_document), encoding="utf-8")
    with patch(
        "hoyo_cli.config.repository.os.replace", wraps=os.replace
    ) as replace_file:
        save_profile(
            path,
            "alt",
            parse_config(config_document).profiles["alt"],
            make_default=True,
            game_order=("zzz",),
        )
    source, destination = replace_file.call_args.args
    assert source.parent == destination.parent == tmp_path
    assert not source.exists()
    assert load_config(path).default_profile == "alt"


@pytest.mark.parametrize("uid", [True, 1.0])
def test_save_rejects_same_value_with_wrong_type(
    tmp_path: Path, config_document: TOMLDocument, uid: object
) -> None:
    config_document["profiles"]["main"]["games"]["genshin"]["uid"] = 1
    path = tmp_path / "config.toml"
    original = tomlkit.dumps(config_document).encode("utf-8")
    path.write_bytes(original)
    account = ProfileSeed("keyring", {"genshin": GameSelection(True, cast(int, uid))})
    with pytest.raises(ConfigError, match="uid"):
        save_profile(path, "main", account, make_default=False, game_order=("genshin",))
    assert path.read_bytes() == original


def test_inspection_handles_accounts_with_no_enabled_games(
    config_document: TOMLDocument, tmp_path: Path
) -> None:
    for game in ("genshin", "hsr", "zzz"):
        config_document["profiles"]["main"]["games"][game]["enabled"] = False
    output = show_config(parse_config(config_document), tmp_path / "config.toml")
    assert "Profile: main" in output
    assert "No enabled games" in output


@pytest.mark.skipif(os.name != "nt", reason="Native Windows command-line parsing")
@pytest.mark.parametrize(
    "arguments",
    [
        [r"C:\Tools\editor.exe", "--wait"],
        [
            r"C:\Program Files\Editor\editor.exe",
            "--title=two words",
            'quoted"argument',
            "path with trailing \\",
        ],
    ],
)
def test_windows_editor_quotes_and_backslashes(
    tmp_path: Path, arguments: list[str]
) -> None:
    path = tmp_path / "config file.toml"
    with patch(
        "hoyo_cli.commands.config.subprocess.run", return_value=CompletedProcess([], 0)
    ) as launch:
        edit_config(
            path, ResolvedEditor(parse_editor_arguments(list2cmdline(arguments)), False)
        )
    assert launch.call_args.args[0] == [*arguments, str(path)]


def test_posix_editor_quoting_uses_an_argument_vector(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    with (
        patch("hoyo_cli.config.editor.os.name", "posix"),
        patch(
            "hoyo_cli.commands.config.subprocess.run",
            return_value=CompletedProcess([], 0),
        ) as launch,
    ):
        edit_config(
            path,
            ResolvedEditor(
                parse_editor_arguments("'editor path' --title='two words'"), False
            ),
        )
    assert launch.call_args.args[0] == ["editor path", "--title=two words", str(path)]


def test_invalid_editor_command_does_not_launch(tmp_path: Path) -> None:
    with patch("hoyo_cli.commands.config.subprocess.run") as launch:
        with pytest.raises(ConfigError):
            parse_editor_arguments("editor\0secret")
    launch.assert_not_called()


def test_editor_nonzero_exit_is_a_clean_command_failure(
    runner: CliRunner,
    tmp_path: Path,
    config_document: TOMLDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(tomlkit.dumps(config_document), encoding="utf-8")
    monkeypatch.setenv("VISUAL", "fake-editor")
    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch("shutil.which", side_effect=_fake_executable),
        patch(
            "hoyo_cli.commands.config.subprocess.run",
            return_value=CompletedProcess([], 9),
        ),
    ):
        result = runner.invoke(main, ["config", "--edit"])
    assert result.exit_code == 1
    assert "status 9" in result.output
    assert str(path) in result.output
    assert "Traceback" not in result.output


def test_invalid_config_is_not_sent_to_editor(
    runner: CliRunner, tmp_path: Path
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("version = [", encoding="utf-8")
    with (
        patch("hoyo_cli.commands.config.config_path", return_value=path),
        patch("hoyo_cli.commands.config.subprocess.run") as launch,
    ):
        result = runner.invoke(main, ["config", "--edit"])
    assert result.exit_code == 2
    launch.assert_not_called()
