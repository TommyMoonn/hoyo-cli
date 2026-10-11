"""Profile/login ownership and the legacy configuration boundary."""

from collections.abc import MutableMapping, Sequence
from pathlib import Path
from typing import cast
from unittest.mock import patch

import click
import pytest
import tomlkit
from click.testing import CliRunner
from conftest import AcceptanceEnvironment
from rich.console import Console
from test_init import FakeBrowser, FakeDiscovery, FakeIO, MemoryKeyring

from hoyo_cli.auth.credentials import Credentials, CredentialStore, parse_cookie_header
from hoyo_cli.auth.keyring_backend import KeyringStore
from hoyo_cli.auth.plaintext_backend import PlaintextStore
from hoyo_cli.cli import main
from hoyo_cli.commands.init import run_add_profile, run_edit_profile
from hoyo_cli.config.models import (
    GameSelection,
    LoginConfig,
    ProfileConfig,
    parse_config,
    resolve_login,
    resolve_profile,
)
from hoyo_cli.config.repository import (
    commit_config,
    load_config,
    prepare_default_profile,
    prepare_editor,
    save_profile,
)
from hoyo_cli.errors import AppError, ConfigError, CredentialError
from hoyo_cli.hoyolab.discovery import CharacterRef
from hoyo_cli.models.common import GameId

LEGACY = """# installation
version = 1
default_account = "main" # default comment
game_order = ["zzz", "genshin"]
future_root = "keep"
[display]
color = "never"
[editor]
command = ["future-editor", "--wait"]
[accounts.main]
credential_backend = "keyring" # backend comment
future_profile = "keep"
[accounts.main.games.genshin]
enabled = true
uid = 800000001 # character comment
[accounts.america]
credential_backend = "plaintext"
[accounts.america.games.genshin]
enabled = true
uid = 600000001
"""


def test_legacy_view_and_one_time_atomic_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(LEGACY, encoding="utf-8")
    original = path.read_bytes()
    config = load_config(path)
    assert path.read_bytes() == original
    assert config.version == 2
    assert config.default_profile == "main"
    assert resolve_login(config, config.profiles["main"]).credential_key == "main"
    assert config.profiles["america"].games["genshin"].uid == 600000001
    commit_config(prepare_default_profile(path, "america"))
    migrated = path.read_bytes()
    assert load_config(path).default_profile == "america"
    text = migrated.decode()
    for marker in (
        "installation",
        "default comment",
        "backend comment",
        "character comment",
        "future_root",
        "future_profile",
        "future-editor",
    ):
        assert marker in text
    assert "[accounts." not in text and "default_account" not in text
    assert prepare_default_profile(path, "america").content.encode() == migrated


def test_v2_shared_login_and_exact_profile_resolution() -> None:
    config = parse_config(
        tomlkit.parse("""version = 2
default_profile = "main"
game_order = []
[display]
color = "auto"
[logins.saved]
credential_backend = "keyring"
credential_key = "original-secret-key"
[profiles.main]
login = "saved"
[profiles.main.games.genshin]
enabled = true
uid = 800000001
[profiles.america]
login = "saved"
[profiles.america.games.genshin]
enabled = true
uid = 600000001
""")
    )
    _, profile = resolve_profile(config, "america")
    assert resolve_login(config, profile) is config.logins["saved"]
    with pytest.raises(ConfigError, match="existing profile exactly"):
        resolve_profile(config, "America")
    with pytest.raises(TypeError):
        cast(MutableMapping[GameId, GameSelection], config.profiles["main"].games)[
            "genshin"
        ] = profile.games["genshin"]


class RegionsDiscovery(FakeDiscovery):
    def discover(
        self, credentials: Credentials
    ) -> dict[GameId, tuple[CharacterRef, ...]]:
        return {
            "genshin": (
                CharacterRef("genshin", 800000001, "os_asia", "Traveler (Asia)"),
                CharacterRef("genshin", 600000001, "os_usa", "Traveler (America)"),
            )
        }


def seed_shared(tmp_path: Path) -> tuple[Path, Path]:
    path, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    login = LoginConfig("plaintext", "original-secret-key")
    for name, uid in (("main", 800000001), ("america", 600000001)):
        save_profile(
            path,
            name,
            ProfileConfig("saved", {"genshin": GameSelection(True, uid)}),
            login=login,
            make_default=name == "main",
            game_order=(),
        )
    PlaintextStore(secrets).put(
        login.credential_key, parse_cookie_header("ltuid=123; ltoken=synthetic-old")
    )
    return path, secrets


def test_reuse_saved_login_never_copies_or_rewrites_secrets(tmp_path: Path) -> None:
    path, secrets = seed_shared(tmp_path)
    before = secrets.read_bytes()
    store = PlaintextStore(secrets)

    class ReuseIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Choose login":
                return choices[0]
            if prompt == "Character for genshin":
                return choices[1]
            return super().choose(prompt, choices)

        def secret(self, prompt: str) -> str:
            pytest.fail("Saved-login reuse must not request cookies")

    with (
        patch("hoyo_cli.commands.init.open_store", return_value=store),
        patch.object(store, "get", wraps=store.get) as get,
        patch.object(store, "put") as put,
        patch.object(store, "checkpoint") as checkpoint,
        patch("hoyo_cli.commands.init._session") as session,
    ):
        run_add_profile(
            path,
            secrets,
            ReuseIO(),
            FakeBrowser(),
            RegionsDiscovery(),
            requested_profile="second",
        )
    get.assert_called_once_with("original-secret-key")
    put.assert_not_called()
    checkpoint.assert_not_called()
    session.assert_not_called()
    config = load_config(path)
    assert config.default_profile == "main"
    assert config.profiles["second"].login == "saved"
    assert config.profiles["second"].games["genshin"].uid == 600000001
    assert len(config.logins) == 1
    assert secrets.read_bytes() == before


@pytest.mark.parametrize("action", ["Update HoYoLAB session", "Credential storage"])
@pytest.mark.parametrize("outcome", ["decline", "cancel", "failure", "save"])
def test_shared_login_change_requires_consent_and_rolls_back(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    action: str,
    outcome: str,
) -> None:
    path, secrets = seed_shared(tmp_path)
    before = path.read_bytes(), secrets.read_bytes()
    other = load_config(path).profiles["america"]
    target = KeyringStore(MemoryKeyring())

    class ChangeIO(FakeIO):
        def confirm(self, prompt: str) -> bool:
            self.prompts.append(prompt)
            if "for all linked profiles" in prompt:
                assert "main, america" in prompt
                return outcome != "decline"
            return super().confirm(prompt)

    prompts = ChangeIO(
        action=action, backend="keyring", cancel="Save" if outcome == "cancel" else ""
    )
    source = PlaintextStore(secrets)

    def open_test_store(backend: str, path: Path) -> CredentialStore:
        return target if backend == "keyring" else source

    with (
        patch(
            "hoyo_cli.commands.init.open_store",
            side_effect=open_test_store,
        ),
        patch(
            "hoyo_cli.commands.init.commit_config",
            side_effect=AppError("Simulated failure") if outcome == "failure" else None,
            wraps=commit_config,
        ),
    ):
        if outcome in ("decline", "cancel", "failure"):
            with pytest.raises(AppError if outcome == "failure" else click.Abort):
                run_edit_profile(
                    path,
                    secrets,
                    prompts,
                    FakeBrowser(),
                    RegionsDiscovery(),
                    requested_profile="main",
                )
        else:
            run_edit_profile(
                path,
                secrets,
                prompts,
                FakeBrowser(),
                RegionsDiscovery(),
                requested_profile="main",
            )
    output = capsys.readouterr().out
    assert "Affected profiles: main, america" in output
    assert "synthetic-old" not in output and "test-secret-123" not in output
    assert load_config(path).profiles["america"] == other
    if outcome != "save":
        assert (path.read_bytes(), secrets.read_bytes()) == before
        assert target.get("original-secret-key") is None
    elif action == "Credential storage":
        assert target.get("original-secret-key") == source.get("original-secret-key")
        assert load_config(path).logins["saved"].credential_backend == "keyring"
        assert secrets.read_bytes() == before[1]


def test_different_identity_refresh_cannot_replace_shared_login(tmp_path: Path) -> None:
    path, secrets = seed_shared(tmp_path)
    before = path.read_bytes(), secrets.read_bytes()

    class OtherIdentityIO(FakeIO):
        def secret(self, prompt: str) -> str:
            return "456" if prompt.startswith("ltuid_v2") else "other-identity-secret"

    with pytest.raises(CredentialError, match="different HoYoLAB identity") as error:
        run_edit_profile(
            path,
            secrets,
            OtherIdentityIO(),
            FakeBrowser(),
            RegionsDiscovery(),
            requested_profile="main",
        )
    assert "456" not in str(error.value) and "other-identity-secret" not in str(
        error.value
    )
    assert (path.read_bytes(), secrets.read_bytes()) == before


@pytest.mark.parametrize("backend", ["keyring", "plaintext"])
def test_legacy_secret_keys_remain_readable_without_store_mutation(
    tmp_path: Path, backend: str
) -> None:
    path, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
    path.write_text(
        LEGACY.replace(
            'credential_backend = "keyring"', f'credential_backend = "{backend}"'
        )
    )
    store = (
        KeyringStore(MemoryKeyring())
        if backend == "keyring"
        else PlaintextStore(secrets)
    )
    credentials = parse_cookie_header("ltuid=123; ltoken=legacy-synthetic")
    store.put("main", credentials)
    with (
        patch.object(store, "put") as put,
        patch.object(store, "checkpoint") as checkpoint,
    ):
        config = load_config(path)
        assert (
            store.get(resolve_login(config, config.profiles["main"]).credential_key)
            == credentials
        )
        commit_config(prepare_editor(path, ("future-editor", "--wait")))
        assert (
            store.get(
                resolve_login(load_config(path), config.profiles["main"]).credential_key
            )
            == credentials
        )
    put.assert_not_called()
    checkpoint.assert_not_called()


@pytest.mark.parametrize("stage", ["conflict", "interrupt", "symlink"])
def test_legacy_upgrade_failure_preserves_bytes(tmp_path: Path, stage: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(LEGACY)
    original = path.read_bytes()
    prepared = prepare_default_profile(path, "america")
    if stage == "conflict":
        original += b"# external edit\n"
        path.write_bytes(original)
        with pytest.raises(ConfigError, match="changed"):
            commit_config(prepared)
    elif stage == "interrupt":
        with patch(
            "hoyo_cli.config.repository.os.replace", side_effect=KeyboardInterrupt
        ):
            with pytest.raises(KeyboardInterrupt):
                commit_config(prepared)
    else:
        with patch.object(Path, "is_symlink", return_value=True):
            with pytest.raises(ConfigError, match="symbolic link"):
                commit_config(prepared)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("command", ["init", "add", "edit"])
@pytest.mark.parametrize("width", [48, 70, 100])
def test_region_picker_and_review_in_real_cli(
    tmp_path: Path,
    runner: CliRunner,
    command: str,
    width: int,
) -> None:
    if command == "init":
        path, secrets = tmp_path / "config.toml", tmp_path / "credentials.toml"
        args = ["init"]
        answers = "main\n1\n123\nhidden-synthetic\n2\ny\n2\ny\n1\n"
    else:
        path, secrets = seed_shared(tmp_path)
        args = ["profile", command, "second" if command == "add" else "main"]
        answers = "1\n2\n1\n" if command == "add" else "1\n3\n2\n1\n1\n1\n"

    def console(**kwargs: object) -> Console:
        return Console(width=width, color_system=None, legacy_windows=False)

    with (
        patch("hoyo_cli.commands.init.config_path", return_value=path),
        patch("hoyo_cli.commands.init.credentials_path", return_value=secrets),
        patch(
            "hoyo_cli.commands.init.HoYoLABDiscovery", return_value=RegionsDiscovery()
        ),
        patch("hoyo_cli.render.setup.make_console", side_effect=console),
    ):
        result = runner.invoke(main, args, input=answers, terminal_width=width)
    assert result.exit_code == 0, result.output
    output = result.output
    assert "GENSHIN IMPACT" in output
    assert "Traveler (Asia)" in output and "Traveler (America)" in output
    assert "800000001" in output and "600000001" in output
    review = output.split("Review", 1)[1]
    assert "America" in review and "600000001" in review
    assert "os_asia" not in output and "os_usa" not in output
    assert "hidden-synthetic" not in output and "\x1b" not in output
    assert (
        load_config(path)
        .profiles["second" if command == "add" else "main"]
        .games["genshin"]
        .uid
        == 600000001
    )


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["profile"],
        ["profile", "add"],
        ["profile", "edit"],
        ["profile", "use"],
        ["config"],
        ["status"],
        ["check-in"],
        ["init"],
    ],
)
def test_help_has_profile_commands_and_clear_placeholders(
    runner: CliRunner, args: list[str]
) -> None:
    result = runner.invoke(main, [*args, "--help"])
    assert result.exit_code == 0
    assert "hoyo account" not in result.output and "--account" not in result.output
    assert all(word not in result.output.split() for word in ("NAME", "GAME", "PATH"))
    assert "[[" not in result.output and "[<name>]" not in result.output


def test_old_command_family_is_removed(runner: CliRunner) -> None:
    assert runner.invoke(main, ["account"]).exit_code == 2
    assert runner.invoke(main, ["--account", "main", "status"]).exit_code == 2


@pytest.mark.parametrize("different_uid", [False, True])
def test_shared_login_caches_do_not_cross_profiles(
    runner: CliRunner,
    acceptance_env: AcceptanceEnvironment,
    different_uid: bool,
) -> None:
    env = acceptance_env
    saved = load_config(env.config)
    uid = saved.profiles["main"].games["genshin"].uid
    save_profile(
        env.config,
        "alt",
        ProfileConfig(
            "main", {"genshin": GameSelection(True, uid + int(different_uid))}
        ),
        make_default=False,
        game_order=saved.game_order,
    )
    assert runner.invoke(main, ["--profile", "main", "genshin"]).exit_code == 0
    env.notes("genshin").side_effect = TimeoutError("synthetic-secret")
    result = runner.invoke(main, ["--profile", "alt", "genshin"])
    assert result.exit_code == 1, result.output
    assert "STALE" not in result.output and "Resin" not in result.output
    assert "synthetic-secret" not in result.output


def test_legacy_list_and_already_default_leave_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".hoyo/config.toml"
    path.parent.mkdir()
    path.write_bytes(LEGACY.replace("\n", "\r\n").encode())
    before = path.read_bytes()
    with patch("hoyo_cli.auth.credentials.open_store") as store:
        assert runner.invoke(main, ["profile"]).exit_code == 0
        result = runner.invoke(main, ["profile", "use", "main"])
        assert result.exit_code == 0 and "already the default" in result.output
        assert runner.invoke(main, ["config"]).exit_code == 0
    store.assert_not_called()
    assert path.read_bytes() == before


def test_missing_region_is_explicit_without_using_internal_code() -> None:
    from test_init import FakeClient, role

    from hoyo_cli.hoyolab.discovery import discover_characters

    upstream = role("hk4e_global").model_copy(update={"server_name": ""})
    with patch(
        "hoyo_cli.hoyolab.client.create_client", return_value=FakeClient([upstream])
    ):
        result = discover_characters(parse_cookie_header("ltuid=123; ltoken=synthetic"))
    character = result["genshin"][0]
    assert character.label == "Test character (Region unavailable)"
    assert "test-region" not in character.label


@pytest.mark.parametrize(
    "field,value", [("login", "missing"), ("login", ""), ("login", "\x85")]
)
def test_invalid_linked_login_is_rejected(
    tmp_path: Path, field: str, value: str
) -> None:
    path, _ = seed_shared(tmp_path)
    document = tomlkit.parse(path.read_text())
    document["profiles"]["main"][field] = value
    with pytest.raises(ConfigError):
        parse_config(document)


@pytest.mark.parametrize("save", [False, True])
def test_explicit_relink_reselects_characters_without_touching_other_profiles(
    tmp_path: Path,
    save: bool,
) -> None:
    path, secrets = seed_shared(tmp_path)
    other_login = LoginConfig("plaintext", "other-secret-key")
    save_profile(
        path,
        "other-owner",
        ProfileConfig("other-login", {"genshin": GameSelection(True, 600000009)}),
        login=other_login,
        make_default=False,
        game_order=(),
    )
    PlaintextStore(secrets).put(
        other_login.credential_key,
        parse_cookie_header("ltuid=456; ltoken=synthetic-other"),
    )
    before = path.read_bytes(), secrets.read_bytes()
    previous = load_config(path)

    class RelinkIO(FakeIO):
        def choose(self, prompt: str, choices: Sequence[str]) -> str:
            if prompt == "Choose login":
                return "Use saved HoYoLAB login: other-login"
            return super().choose(prompt, choices)

        def secret(self, prompt: str) -> str:
            pytest.fail("Explicitly linking a saved login must not ask for cookies")

    class OtherDiscovery(FakeDiscovery):
        def discover(
            self, credentials: Credentials
        ) -> dict[GameId, tuple[CharacterRef, ...]]:
            assert credentials.fields["ltuid"] == "456"
            return {
                "genshin": (
                    CharacterRef("genshin", 600000009, "os_usa", "Traveler (America)"),
                )
            }

    operation = RelinkIO(
        action="Change linked login", cancel="Save" if not save else ""
    )
    if save:
        run_edit_profile(
            path,
            secrets,
            operation,
            FakeBrowser(),
            OtherDiscovery(),
            requested_profile="main",
        )
        current = load_config(path)
        assert current.profiles["main"].login == "other-login"
        assert current.profiles["main"].games == {
            "genshin": GameSelection(True, 600000009)
        }
        assert current.profiles["america"] == previous.profiles["america"]
        assert current.profiles["other-owner"] == previous.profiles["other-owner"]
        assert current.default_profile == "main"
    else:
        with pytest.raises(click.Abort):
            run_edit_profile(
                path,
                secrets,
                operation,
                FakeBrowser(),
                OtherDiscovery(),
                requested_profile="main",
            )
        assert path.read_bytes() == before[0]
    assert secrets.read_bytes() == before[1]


def test_new_login_does_not_reuse_a_linked_secret_key(tmp_path: Path) -> None:
    path, secrets = seed_shared(tmp_path)
    original = load_config(path)
    save_profile(
        path,
        "other-owner",
        ProfileConfig("main", {"genshin": GameSelection(True, 800000001)}),
        login=LoginConfig("plaintext", "main"),
        make_default=False,
        game_order=(),
    )
    old_session = parse_cookie_header("ltuid=456; ltoken=keep-other-login")
    PlaintextStore(secrets).put("main", old_session)
    run_edit_profile(
        path,
        secrets,
        FakeIO(action="Change linked login"),
        FakeBrowser(),
        RegionsDiscovery(),
        requested_profile="main",
    )
    current = load_config(path)
    created = current.profiles["main"].login
    assert created not in original.logins and created != "main"
    assert current.logins[created].credential_key == created
    assert current.profiles["other-owner"].login == "main"
    assert PlaintextStore(secrets).get("main") == old_session
