"""Interactive setup: validated selection, explicit secrets, recoverable writes."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Literal, Protocol, TypeVar, cast
from uuid import uuid4

import click

from hoyo_cli.auth.browser_import import (
    BrowserImporter,
    BrowserProfile,
    SystemBrowserImporter,
    browser_error,
)
from hoyo_cli.auth.credentials import (
    Credentials,
    CredentialStore,
    manual_credentials,
    open_store,
)
from hoyo_cli.commands import CommandOptions
from hoyo_cli.config.editor import (
    describe_editor,
    detected_editors,
    resolve_editor_command,
)
from hoyo_cli.config.models import (
    AppConfig,
    CredentialBackend,
    GameSelection,
    LoginConfig,
    ProfileConfig,
    resolve_login,
    resolve_profile,
    validate_editor_command,
    validate_name,
)
from hoyo_cli.config.paths import config_path, credentials_path
from hoyo_cli.config.repository import (
    commit_config,
    load_config_snapshot,
    prepare_profile,
)
from hoyo_cli.errors import (
    AppError,
    BackendUnavailable,
    ConfigError,
    CredentialError,
    ErrorCode,
)
from hoyo_cli.hoyolab.discovery import (
    CharacterDiscovery,
    CharacterRef,
    HoYoLABDiscovery,
)
from hoyo_cli.models.common import GAME_IDS, GameId
from hoyo_cli.render.errors import format_error
from hoyo_cli.render.progress import OperationProgress
from hoyo_cli.render.setup import SetupPresenter, character_choice

_Result = TypeVar("_Result")
_MANUAL_SOURCE = "Enter HoYoLAB cookie values (recommended)"
_BROWSER_SOURCE = "Import from browser (best effort)"
_MANUAL_ENTRY = "Enter ltuid_v2 / ltoken_v2 manually"
_MANUAL_RECOMMENDED = f"{_MANUAL_ENTRY} (recommended)"
_MANUAL_REENTRY = "Enter ltuid_v2 / ltoken_v2 again"


class SetupIO(Protocol):
    def choose(self, prompt: str, choices: Sequence[str]) -> str: ...
    def confirm(self, prompt: str) -> bool: ...
    def secret(self, prompt: str) -> str: ...


class ClickSetupIO:
    def __init__(self, presenter: SetupPresenter) -> None:
        self.presenter = presenter

    def choose(self, prompt: str, choices: Sequence[str]) -> str:
        if not choices:
            return click.prompt(self.presenter.prompt_label(prompt), type=str)
        cancel = "Cancel (no changes)"
        has_cancel = cancel in choices
        maximum = len(choices) if has_cancel else len(choices) + 1
        self.presenter.choices(choices if has_cancel else (*choices, cancel))
        index = click.prompt(
            self.presenter.prompt_label(prompt),
            type=click.IntRange(1, maximum),
            default=1,
        )
        if not has_cancel and index == maximum:
            raise click.Abort()
        return choices[index - 1]

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        return click.confirm(self.presenter.prompt_label(prompt), default=default)

    def secret(self, prompt: str) -> str:
        return click.prompt(
            self.presenter.prompt_label(prompt), hide_input=True, type=str
        )


def setup_io(io: SetupIO | None, presenter: SetupPresenter) -> SetupIO:
    """Use one presenter at the Click boundary, retaining injected fake I/O."""
    if io is None:
        return ClickSetupIO(presenter)
    if isinstance(io, ClickSetupIO):
        io.presenter = presenter
    return io


def choose_setup_option(io: SetupIO, prompt: str, choices: Sequence[str]) -> str:
    value = io.choose(prompt, choices)
    if choices and value not in choices:
        raise ConfigError("Invalid setup selection; no files were changed.")
    return value


def _confirm(io: SetupIO, prompt: str, *, default: bool = False) -> bool:
    # Existing SetupIO implementations keep their one-argument contract. Only
    # the terminal adapter needs to implement the Enter-key default policy.
    if isinstance(io, ClickSetupIO):
        return io.confirm(prompt, default=default)
    return io.confirm(prompt)


def _profile_name(
    io: SetupIO,
    presenter: SetupPresenter,
    config: AppConfig | None,
    requested: str | None,
) -> str:
    name = requested if requested is not None else _new_profile_name(io, presenter)
    if not _valid_profile_name(name):
        raise ConfigError(
            "Profile name must be non-empty and contain no control characters."
        )
    if config is not None and name in config.profiles:
        raise ConfigError(
            "A profile with this exact name already exists.",
            hint="Use hoyo profile edit <name> to update an existing profile.",
        )
    return name


def _valid_profile_name(name: str) -> bool:
    try:
        validate_name(name, "profile name")
    except ConfigError:
        return False
    return True


def _new_profile_name(io: SetupIO, presenter: SetupPresenter) -> str:
    while True:
        name = choose_setup_option(io, "Profile name", ())
        if _valid_profile_name(name):
            return name
        presenter.warning(
            "Profile name must be non-empty and contain no control characters."
        )
        if (
            choose_setup_option(
                io,
                "Profile recovery",
                ("Enter profile name again", "Cancel (no changes)"),
            )
            == "Cancel (no changes)"
        ):
            raise click.Abort()


def _observed_work(
    operation: Literal["browser_read", "session_verify"],
    work: Callable[[], _Result],
    *,
    no_color: bool,
    configured_color: str,
) -> _Result:
    async def run() -> _Result:
        async with OperationProgress(
            operation, no_color=no_color, configured_color=configured_color
        ):
            return await asyncio.to_thread(work)

    return asyncio.run(run())


def _browser_profile(
    io: SetupIO, browser: BrowserImporter, presenter: SetupPresenter
) -> BrowserProfile | None:
    sessions = browser.list_sessions()
    choices = [session.browser_label for session in sessions]
    if not sessions:
        presenter.warning(
            "No supported browser sources are available. "
            "Enter ltuid_v2 / ltoken_v2 manually."
        )
        return None
    if not any(session.supports_profiles for session in sessions):
        presenter.warning(
            "Browser/profile selection not available on this platform. "
            "The library chooses a profile; installed profiles and HoYoLAB "
            "login state have not been checked. Manual two-field entry is recommended.",
        )
    selected = choose_setup_option(io, "Browser source", (*choices, _MANUAL_ENTRY))
    if selected == _MANUAL_ENTRY:
        return None
    session = sessions[choices.index(selected)]
    profiles = browser.list_profiles(session.source_id)
    if not profiles:
        raise browser_error(ErrorCode.BROWSER_PROFILE_MISSING)
    if not session.supports_profiles:
        return profiles[0]
    presenter.message("Located browser profiles, not verified HoYoLAB sessions.")
    # Tokens disambiguate equal profile names without displaying private paths.
    profile_choices = [
        f"{p.label} [{p.source_id.split(':')[-1][:8]}]" for p in profiles
    ]
    selected = choose_setup_option(
        io, "Browser profile", (*profile_choices, _MANUAL_ENTRY)
    )
    if selected == _MANUAL_ENTRY:
        return None
    return profiles[profile_choices.index(selected)]


def _session_source(io: SetupIO) -> bool:
    return (
        choose_setup_option(io, "Login source", (_MANUAL_SOURCE, _BROWSER_SOURCE))
        == _BROWSER_SOURCE
    )


def _session(
    io: SetupIO,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    presenter: SetupPresenter,
    *,
    no_color: bool,
    configured_color: str,
) -> tuple[Credentials, dict[GameId, tuple[CharacterRef, ...]]]:
    use_browser = _session_source(io)
    profile: BrowserProfile | None = None
    credentials: Credentials | None = None
    while True:
        failure_code: ErrorCode | None = None
        try:
            if use_browser and profile is None:
                profile = _browser_profile(io, browser, presenter)
                if profile is None:
                    use_browser = False
            if credentials is None:
                if use_browser and profile is not None:
                    credentials = _observed_work(
                        "browser_read",
                        partial(browser.import_session, profile.source_id),
                        no_color=no_color,
                        configured_color=configured_color,
                    )
                    presenter.message("Cookies found. Verifying HoYoLAB session.")
                else:
                    credentials = manual_credentials(
                        io.secret("ltuid_v2 (HoYoLAB account ID, hidden)"),
                        io.secret("ltoken_v2 (session token, hidden)"),
                    )
            discovered = _observed_work(
                "session_verify",
                partial(discovery.discover, credentials),
                no_color=no_color,
                configured_color=configured_color,
            )
            presenter.success("Session verified.")
        except ConfigError:
            # Forged menu values or invalid adapter configuration are not a
            # recoverable session failure.
            raise
        except AppError as error:
            # Verification failures retain their classified cause. Unexpected
            # application exceptions and control signals leave this boundary.
            if error.code == ErrorCode.UNEXPECTED:
                raise
            failure_code = error.code
            presenter.warning(format_error(error), stderr=True)
            if error.code in (ErrorCode.AUTH_REJECTED, ErrorCode.API_REJECTED):
                presenter.warning(
                    "The session was not verified. Sign in to HoYoLAB again and "
                    "re-enter both cookie values, or choose browser import. "
                    "Some sessions or endpoints may require additional cookies.",
                    stderr=True,
                )
        else:
            if any(discovered.values()):
                return credentials, discovered
            presenter.warning(
                format_error(
                    ConfigError(
                        "No supported linked game characters were found "
                        "on this HoYoLAB account.",
                        hint="Retry discovery or choose a session with linked games.",
                    )
                ),
                stderr=True,
            )
        recovery = _session_recovery(
            io,
            use_browser=use_browser,
            has_credentials=credentials is not None,
            failure_code=failure_code,
        )
        if recovery == "Cancel (no changes)":
            raise click.Abort()
        if recovery in (_MANUAL_ENTRY, _MANUAL_RECOMMENDED):
            use_browser, profile, credentials = False, None, None
        if recovery == "Choose another browser/profile":
            profile, credentials = None, None
        if recovery == _MANUAL_REENTRY:
            credentials = None
        if recovery == "Change credential source":
            use_browser = _session_source(io)
            profile, credentials = None, None


def _session_recovery(
    io: SetupIO,
    *,
    use_browser: bool,
    has_credentials: bool,
    failure_code: ErrorCode | None = None,
) -> str:
    if use_browser and failure_code in (
        ErrorCode.BROWSER_ENCRYPTION_UNSUPPORTED,
        ErrorCode.BROWSER_DECRYPTION,
        ErrorCode.BROWSER_UNSUPPORTED,
        ErrorCode.BROWSER_PROFILE_MISSING,
    ):
        choices = (_MANUAL_RECOMMENDED, "Choose another browser/profile")
    elif use_browser:
        retry = (
            "Retry session verification" if has_credentials else "Retry browser import"
        )
        choices = (retry, "Choose another browser/profile", _MANUAL_ENTRY)
    elif has_credentials:
        choices = (
            "Retry session verification",
            "Change credential source",
            _MANUAL_REENTRY,
        )
    else:
        choices = (_MANUAL_REENTRY, "Change credential source")
    return choose_setup_option(
        io,
        "Browser recovery" if use_browser else "Session recovery",
        (*choices, "Cancel (no changes)"),
    )


def _backend(
    io: SetupIO, path: Path, name: str, presenter: SetupPresenter
) -> tuple[CredentialBackend, CredentialStore]:
    backend = choose_setup_option(io, "Credential backend", ("keyring", "plaintext"))
    selected: CredentialBackend = "keyring" if backend == "keyring" else "plaintext"
    store = open_store(selected, path)
    try:
        store.get(name)
    except BackendUnavailable:
        if selected != "keyring" or not io.confirm(
            "OS keyring unavailable. Use plaintext credentials instead?"
        ):
            raise
        selected = "plaintext"
        store = open_store(selected, path)
        store.get(name)
    if selected == "plaintext":
        presenter.warning(
            f"Warning: {path} will contain login credentials in plaintext. "
            "User-only permissions are requested; "
            "Windows uses inherited directory ACLs.",
        )
        if not io.confirm("Accept plaintext credential storage?"):
            raise click.Abort()
    return selected, store


def select_game_order(io: SetupIO, config: AppConfig | None) -> tuple[GameId, ...]:
    prompt = (
        "Keep the current game display order?"
        if config
        else "Use the default game display order?"
    )
    if _confirm(io, prompt, default=True):
        return config.game_order if config else GAME_IDS
    remaining = list(GAME_IDS)
    order: list[GameId] = []
    while remaining:
        selected = choose_setup_option(io, "Next game in display order", remaining)
        # choose_setup_option has validated membership in the remaining canonical IDs.
        game = cast(GameId, selected)
        order.append(game)
        remaining.remove(game)
    return tuple(order)


def select_editor(
    io: SetupIO, presenter: SetupPresenter, current: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    """Shared global picker; detection and review never launch an editor."""
    presets = detected_editors()
    presenter.current("Current editor: " + describe_editor(current))
    presenter.message(
        "Only detected presets are listed. Add an editor to PATH or use its full path."
    )
    while True:
        selected = choose_setup_option(
            io,
            "Editor",
            (
                "Keep current",
                *presets,
                "Use VISUAL/EDITOR",
                "Custom executable/arguments",
                "Cancel (no changes)",
            ),
        )
        if selected == "Cancel (no changes)":
            raise click.Abort()
        if selected == "Keep current":
            return current
        if selected == "Use VISUAL/EDITOR":
            return None
        try:
            if selected == "Custom executable/arguments":
                executable = choose_setup_option(
                    io, "Editor executable (full path or PATH name; no quotes)", ()
                )
                arguments = [executable]
                while io.confirm("Add an editor argument?"):
                    arguments.append(choose_setup_option(io, "Editor argument", ()))
                command = validate_editor_command(arguments)
            else:
                command = presets[selected]
            resolve_editor_command(command)
        except ConfigError as error:
            presenter.warning(error.message)
            presenter.message("Choose an available executable; no changes saved.")
            continue
        presenter.current("Selected editor: " + describe_editor(command))
        return command


def _character(
    io: SetupIO,
    game: GameId,
    characters: tuple[CharacterRef, ...],
) -> CharacterRef | None:
    choices = [character_choice(char) for char in characters]
    selected = choose_setup_option(
        io, f"Character for {game}", (*choices, "Skip this game")
    )
    return None if selected == "Skip this game" else characters[choices.index(selected)]


def _select_games(
    io: SetupIO,
    presenter: SetupPresenter,
    name: str,
    discovered: dict[GameId, tuple[CharacterRef, ...]],
    previous: ProfileConfig | None,
    *,
    staged: bool = True,
) -> tuple[dict[GameId, GameSelection], dict[GameId, CharacterRef]]:
    if staged:
        presenter.step(2, "Games", name)
    else:
        presenter.heading(f"Games · Profile: {name}")
    # A retained UID is recovery metadata, not evidence of ownership by the
    # selected session. Only a current discovery choice may enable a game.
    games: dict[GameId, GameSelection] = (
        {
            game: GameSelection(False, selection.uid)
            for game, selection in previous.games.items()
        }
        if previous is not None
        else {}
    )
    selected_characters: dict[GameId, CharacterRef] = {}
    for game in GAME_IDS:
        presenter.game(game)
        characters = discovered.get(game, ())
        if not characters:
            presenter.message("No linked characters; this game will stay disabled.")
            prior = previous.games.get(game) if previous is not None else None
            if prior is not None and prior.enabled:
                presenter.warning(
                    f"{game}: not linked to this session; will be disabled if saved. "
                    "Previous UID retained for manual recovery.",
                )
            continue
        character = _character(io, game, characters)
        if character is None:
            continue
        selected_characters[game] = character
        games[game] = GameSelection(True, character.uid)
    _require_enabled(games)
    return games, selected_characters


def _require_enabled(games: dict[GameId, GameSelection]) -> None:
    if not any(selection.enabled for selection in games.values()):
        raise ConfigError("Select at least one enabled game; no files were changed.")


@dataclass(frozen=True, repr=False)
class _SetupChanges:
    profile: ProfileConfig
    characters: dict[GameId, CharacterRef]
    login: LoginConfig | None = None
    secret_update: tuple[CredentialStore, Credentials] | None = None
    initial_order: tuple[GameId, ...] | None = None


def _new_profile(
    io: SetupIO,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    presenter: SetupPresenter,
    credentials_file: Path,
    name: str,
    config: AppConfig | None,
    *,
    no_color: bool,
) -> _SetupChanges:
    login_name = _choose_login(io, config) if config else None
    if login_name is not None:
        assert config is not None
        credentials = _saved_session(
            config, ProfileConfig(login_name, {}), credentials_file
        )
        discovered = _observed_work(
            "session_verify",
            partial(discovery.discover, credentials),
            no_color=no_color,
            configured_color=config.display_color,
        )
        presenter.message(f"Using saved login: {login_name}")
        games, characters = _select_games(io, presenter, name, discovered, None)
        return _SetupChanges(ProfileConfig(login_name, games), characters)
    credentials, discovered = _session(
        io,
        browser,
        discovery,
        presenter,
        no_color=no_color,
        configured_color=config.display_color if config else "auto",
    )
    games, characters = _select_games(io, presenter, name, discovered, None)
    order = select_game_order(io, None) if config is None else None
    presenter.step(3, "Storage", name)
    # New logins have their own stable identity, independent of future profile links.
    login_name = name
    if config is not None and (
        name in config.logins
        or any(login.credential_key == name for login in config.logins.values())
    ):
        login_name = f"{name}-{uuid4().hex}"
    backend, store = _backend(io, credentials_file, login_name, presenter)
    if store.get(login_name) is not None:
        if not io.confirm("Replace existing stored credentials for this login key?"):
            raise click.Abort()
    return _SetupChanges(
        ProfileConfig(login_name, games),
        characters,
        LoginConfig(backend, login_name),
        (store, credentials),
        order,
    )


def _choose_login(io: SetupIO, config: AppConfig) -> str | None:
    labels = {f"Use saved HoYoLAB login: {name}": name for name in config.logins}
    selected = choose_setup_option(
        io, "Choose login", (*labels, "Add new HoYoLAB login", "Cancel (no changes)")
    )
    if selected == "Cancel (no changes)":
        raise click.Abort()
    return labels.get(selected)


def _saved_session(
    config: AppConfig, profile: ProfileConfig, credentials_file: Path
) -> Credentials:
    login = resolve_login(config, profile)
    credentials = open_store(login.credential_backend, credentials_file).get(
        login.credential_key
    )
    if credentials is None:
        raise CredentialError(
            "No stored HoYoLAB session is available for this profile.",
            code=ErrorCode.CREDENTIALS_MISSING,
            hint="Run hoyo profile edit <name> and select Update HoYoLAB session.",
        )
    return credentials


def _edit_games(
    io: SetupIO,
    presenter: SetupPresenter,
    discovery: CharacterDiscovery,
    config: AppConfig,
    name: str,
    credentials_file: Path,
    *,
    no_color: bool,
) -> tuple[dict[GameId, GameSelection], dict[GameId, CharacterRef]]:
    profile = config.profiles[name]
    games = dict(profile.games)
    characters: dict[GameId, CharacterRef] = {}
    discovered: dict[GameId, tuple[CharacterRef, ...]] | None = None
    for game in GAME_IDS:
        presenter.game(game)
        current = games.get(game)
        choices = ["Keep current selection" if current else "Skip this game"]
        if current:
            presenter.current(
                f"Current: {'Enabled' if current.enabled else 'Disabled'} · "
                f"UID {current.uid} "
                "(retained selection, unverified)"
            )
            if current.enabled:
                choices.append("Disable this game")
        else:
            presenter.message("Not configured")
        choices.append("Choose linked character")
        action = choose_setup_option(io, f"Game settings for {game}", choices)
        if action == "Disable this game" and current:
            games[game] = GameSelection(False, current.uid)
        elif action == "Choose linked character":
            if discovered is None:
                credentials = _saved_session(config, profile, credentials_file)
                discovered = _observed_work(
                    "session_verify",
                    partial(discovery.discover, credentials),
                    no_color=no_color,
                    configured_color=config.display_color,
                )
                presenter.success("Saved session verified.")
            linked = discovered.get(game, ())
            if not linked:
                raise ConfigError(
                    f"No linked characters were found for {game} in the saved session.",
                    hint=(
                        "Keep this game disabled, or select Update HoYoLAB session "
                        "in hoyo profile edit <name>."
                    ),
                )
            character = _character(io, game, linked)
            if character is not None:
                games[game] = GameSelection(True, character.uid)
                characters[game] = character
            elif current:
                games[game] = GameSelection(False, current.uid)
    _require_enabled(games)
    return games, characters


def _existing_profile(
    io: SetupIO,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    presenter: SetupPresenter,
    credentials_file: Path,
    name: str,
    config: AppConfig,
    *,
    no_color: bool,
) -> _SetupChanges:
    action = choose_setup_option(
        io,
        "Profile settings",
        (
            "Games and characters",
            "Change linked login",
            "Update HoYoLAB session",
            "Credential storage",
            "Cancel (no changes)",
        ),
    )
    if action == "Cancel (no changes)":
        raise click.Abort()
    presenter.message(f"Change: {action}")
    profile = config.profiles[name]
    login = resolve_login(config, profile)
    updated_login: LoginConfig | None = None
    characters: dict[GameId, CharacterRef] = {}
    secret_update: tuple[CredentialStore, Credentials] | None = None
    if action == "Games and characters":
        games, characters = _edit_games(
            io,
            presenter,
            discovery,
            config,
            name,
            credentials_file,
            no_color=no_color,
        )
        profile = ProfileConfig(profile.login, games)
    elif action == "Change linked login":
        # The shared creation path verifies discovery and never retains enabled
        # selections from a different login.
        return _new_profile(
            io,
            browser,
            discovery,
            presenter,
            credentials_file,
            name,
            config,
            no_color=no_color,
        )
    elif action == "Update HoYoLAB session":
        _authorize_login_change(
            io, presenter, config, profile.login, "Replace login credentials"
        )
        credentials, discovered = _session(
            io,
            browser,
            discovery,
            presenter,
            no_color=no_color,
            configured_color=config.display_color,
        )
        previous_credentials = _saved_session(config, profile, credentials_file)
        if not previous_credentials.same_identity(credentials):
            raise CredentialError(
                "This session belongs to a different HoYoLAB identity; "
                "no changes saved.",
                hint=(
                    "Create a separate login using hoyo profile add [name], or "
                    "Change linked login in hoyo profile edit <name>."
                ),
            )
        games, characters = _select_games(
            io, presenter, name, discovered, profile, staged=False
        )
        profile = ProfileConfig(profile.login, games)
        # Only this profile is reviewed for character changes. Other linked
        # profiles retain their exact selections when the shared login refreshes.
        secret_update = (
            open_store(login.credential_backend, credentials_file),
            credentials,
        )
    elif action == "Credential storage":
        _authorize_login_change(
            io, presenter, config, profile.login, "Change login credential backend"
        )
        credentials = _saved_session(config, profile, credentials_file)
        backend, store = _backend(io, credentials_file, login.credential_key, presenter)
        if backend != login.credential_backend:
            if store.get(login.credential_key) is not None and not io.confirm(
                "Replace existing stored credentials in the selected backend?"
            ):
                raise click.Abort()
            updated_login = LoginConfig(backend, login.credential_key)
            secret_update = (store, credentials)
    return _SetupChanges(
        profile,
        characters,
        updated_login,
        secret_update,
    )


def _authorize_login_change(
    io: SetupIO, presenter: SetupPresenter, config: AppConfig, login: str, action: str
) -> None:
    affected = [
        name for name, profile in config.profiles.items() if profile.login == login
    ]
    presenter.message(f"Login: {login}")
    presenter.warning("Affected profiles: " + ", ".join(affected))
    if not io.confirm(f"{action} for all linked profiles ({', '.join(affected)})?"):
        raise click.Abort()


def _load_setup_config(config_file: Path) -> tuple[AppConfig | None, bytes | None]:
    try:
        return load_config_snapshot(config_file)
    except ConfigError as error:
        if error.code != ErrorCode.CONFIG_MISSING:
            raise
        return None, None


def _initialized_guidance() -> None:
    click.echo(
        "HoYo CLI is already set up.\n"
        "Add:      hoyo profile add\n"
        "Update:   hoyo profile edit <name>\n"
        "Switch:   hoyo profile use <name>\n"
        "Settings: hoyo config",
        color=False,
    )


def _validate_entrypoint(
    config: AppConfig | None, mode: Literal["first", "add", "edit"], name: str | None
) -> None:
    if mode == "first" and name is not None:
        raise ConfigError(
            "hoyo init does not accept a profile override.",
            hint="Run hoyo profile edit <name> to update an existing profile.",
        )
    if mode != "first" and config is None:
        raise ConfigError(
            "Configuration file not found.",
            code=ErrorCode.CONFIG_MISSING,
            hint="Run: hoyo init",
        )
    if config is not None and mode == "edit":
        if name is None:
            raise ConfigError(
                "An exact profile name is required.", hint="Run: hoyo profile"
            )
        try:
            resolve_profile(config, name)
        except ConfigError:
            raise ConfigError(
                "Unknown profile name; names must match exactly.",
                hint="Run: hoyo profile",
            ) from None
    if config is not None and mode == "add" and name in config.profiles:
        raise ConfigError(
            "A profile with this exact name already exists.",
            hint="Use hoyo profile edit <name> to update an existing profile.",
        )


def _run_setup(
    config_file: Path,
    credentials_file: Path,
    io: SetupIO,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    presenter: SetupPresenter,
    config: AppConfig | None,
    original: bytes | None,
    *,
    mode: Literal["first", "add", "edit"],
    requested_profile: str | None = None,
    no_color: bool = False,
) -> str:
    existing = mode == "edit"
    if existing:
        presenter.heading("Edit profile")
        assert config is not None and requested_profile is not None
        name = requested_profile
    else:
        presenter.heading("First profile setup" if mode == "first" else "Add profile")
        presenter.step(1, "Profile")
        name = _profile_name(io, presenter, config, requested_profile)
    presenter.message(f"Profile: {name}")
    changes = (
        _existing_profile(
            io,
            browser,
            discovery,
            presenter,
            credentials_file,
            name,
            config,
            no_color=no_color,
        )
        if config is not None and existing
        else _new_profile(
            io,
            browser,
            discovery,
            presenter,
            credentials_file,
            name,
            config,
            no_color=no_color,
        )
    )
    recovery = (
        "Retry hoyo init." if mode == "first" else f"Retry hoyo profile {mode} <name>."
    )
    editor = config.editor_command if config else None
    selected_login = changes.login
    if selected_login is None:
        assert config is not None
        selected_login = resolve_login(config, changes.profile)
    while True:
        prepared = prepare_profile(
            config_file,
            name,
            changes.profile,
            login=changes.login,
            make_default=config is None,
            game_order=changes.initial_order,
            editor_command=editor,
        )
        prepared = replace(prepared, conflict_hint=recovery)
        if prepared.original != original:
            raise ConfigError("Configuration changed during setup.", hint=recovery)
        presenter.review(
            name=name,
            games=changes.profile.games,
            characters=changes.characters,
            backend=selected_login.credential_backend,
            login=changes.profile.login,
            default_profile=config.default_profile if config else name,
            order=config.game_order if config else changes.initial_order or GAME_IDS,
            staged=not existing,
            editor=describe_editor(editor) if mode == "first" else None,
        )
        action = choose_setup_option(
            io,
            "Save profile configuration and credentials?"
            if changes.secret_update
            else "Save profile settings?",
            (
                "Save",
                "Cancel (no changes)",
                *(("Editor preference",) if mode == "first" else ()),
            ),
        )
        if action == "Save":
            break
        if action == "Cancel (no changes)":
            raise click.Abort()
        editor = select_editor(io, presenter, editor)
    if changes.secret_update is None:
        commit_config(prepared)
        return name
    store, credentials = changes.secret_update
    restore = store.checkpoint(selected_login.credential_key)
    try:
        store.put(selected_login.credential_key, credentials)
        commit_config(prepared)
    except BaseException:
        try:
            restore()
        except CredentialError:
            raise CredentialError(
                "Setup failed and credential rollback failed. Config was not saved. "
                "Repair the selected credential backend before retrying."
            ) from None
        raise
    return name


def _run_profile_setup(
    config_file: Path,
    credentials_file: Path,
    io: SetupIO | None,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    *,
    mode: Literal["first", "add", "edit"],
    requested_profile: str | None = None,
    no_color: bool = False,
) -> None:
    config, original = _load_setup_config(config_file)
    _validate_entrypoint(config, mode, requested_profile)
    if mode == "first" and config is not None:
        _initialized_guidance()
        return
    presenter = SetupPresenter(
        no_color=no_color, configured_color=config.display_color if config else "auto"
    )
    io = setup_io(io, presenter)
    try:
        name = _run_setup(
            config_file,
            credentials_file,
            io,
            browser,
            discovery,
            presenter,
            config,
            original,
            mode=mode,
            requested_profile=requested_profile,
            no_color=no_color,
        )
    except (click.Abort, KeyboardInterrupt, asyncio.CancelledError):
        presenter.message("Setup cancelled; no changes saved.")
        raise
    presenter.success(f"Configured profile {name}. Run hoyo to continue.")


def run_init(
    config_file: Path,
    credentials_file: Path,
    io: SetupIO | None,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    *,
    requested_profile: str | None = None,
    no_color: bool = False,
) -> None:
    _run_profile_setup(
        config_file,
        credentials_file,
        io,
        browser,
        discovery,
        mode="first",
        requested_profile=requested_profile,
        no_color=no_color,
    )


def run_add_profile(
    config_file: Path,
    credentials_file: Path,
    io: SetupIO | None,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    *,
    requested_profile: str | None = None,
    no_color: bool = False,
) -> None:
    _run_profile_setup(
        config_file,
        credentials_file,
        io,
        browser,
        discovery,
        mode="add",
        requested_profile=requested_profile,
        no_color=no_color,
    )


def run_edit_profile(
    config_file: Path,
    credentials_file: Path,
    io: SetupIO | None,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    *,
    requested_profile: str,
    no_color: bool = False,
) -> None:
    _run_profile_setup(
        config_file,
        credentials_file,
        io,
        browser,
        discovery,
        mode="edit",
        requested_profile=requested_profile,
        no_color=no_color,
    )


def configure_profile(
    *,
    options: CommandOptions,
    mode: Literal["first", "add", "edit"] = "first",
    name: str | None = None,
) -> None:
    try:
        config, _ = _load_setup_config(config_path())
        requested = options.profile if mode == "first" else name
        _validate_entrypoint(config, mode, requested)
        if mode == "first" and config is not None:
            _initialized_guidance()
            return
        components = (
            config_path(),
            credentials_path(),
            None,
            SystemBrowserImporter(),
            HoYoLABDiscovery(),
        )
        if mode == "edit":
            assert requested is not None
            run_edit_profile(
                *components, requested_profile=requested, no_color=options.no_color
            )
        elif mode == "add":
            run_add_profile(
                *components, requested_profile=requested, no_color=options.no_color
            )
        else:
            run_init(*components, no_color=options.no_color)
    except (AppError, click.Abort):
        raise
    except Exception:
        raise AppError(
            "Profile setup failed. Check configuration and credential storage "
            "before retrying."
        ) from None
