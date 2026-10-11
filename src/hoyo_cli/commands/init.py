"""Interactive setup: validated selection, explicit secrets, recoverable writes."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Literal, Protocol, TypeVar, cast

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
    open_store,
    parse_cookie_header,
)
from hoyo_cli.commands import CommandOptions
from hoyo_cli.config.models import (
    AccountConfig,
    AppConfig,
    CredentialBackend,
    GameSelection,
    resolve_account,
)
from hoyo_cli.config.paths import config_path, credentials_path
from hoyo_cli.config.repository import (
    commit_config,
    load_config_snapshot,
    prepare_account,
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


class SetupIO(Protocol):
    def choose(self, prompt: str, choices: Sequence[str]) -> str: ...
    def confirm(self, prompt: str) -> bool: ...
    def secret(self, prompt: str) -> str: ...


class ClickSetupIO:
    def __init__(self) -> None:
        self.presenter = SetupPresenter(no_color=True)

    def choose(self, prompt: str, choices: Sequence[str]) -> str:
        if not choices:
            return click.prompt(prompt, type=str)
        for index, choice in enumerate(choices, 1):
            self.presenter.message(f"  {index}. {choice}")
        cancel = "Cancel (no changes)"
        has_cancel = cancel in choices
        maximum = len(choices) if has_cancel else len(choices) + 1
        if not has_cancel:
            self.presenter.message(f"  {maximum}. {cancel}")
        index = click.prompt(prompt, type=click.IntRange(1, maximum), default=1)
        if not has_cancel and index == maximum:
            raise click.Abort()
        return choices[index - 1]

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        return click.confirm(prompt, default=default)

    def secret(self, prompt: str) -> str:
        return click.prompt(prompt, hide_input=True, type=str)


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


def _account_name(io: SetupIO, config: AppConfig | None, requested: str | None) -> str:
    name = requested if requested is not None else _new_account_name(io)
    if not _valid_account_name(name):
        raise ConfigError(
            "Account name must be non-empty and contain no control characters."
        )
    if config is not None and name in config.accounts:
        raise ConfigError(
            "An account with this exact name already exists.",
            hint="Use hoyo account edit NAME to update an existing account.",
        )
    return name


def _valid_account_name(name: str) -> bool:
    return bool(name.strip()) and not any(ord(c) < 32 or ord(c) == 127 for c in name)


def _new_account_name(io: SetupIO) -> str:
    while True:
        name = choose_setup_option(io, "Account name", ())
        if _valid_account_name(name):
            return name
        click.echo("Account name must be non-empty and contain no control characters.")
        if (
            choose_setup_option(
                io,
                "Account recovery",
                ("Enter account name again", "Cancel (no changes)"),
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


def _browser_profile(io: SetupIO, browser: BrowserImporter) -> BrowserProfile | None:
    sessions = browser.list_sessions()
    choices = [session.browser_label for session in sessions]
    if not sessions:
        click.echo("No supported browser sources are available. Use manual input.")
        return None
    selected = choose_setup_option(
        io, "HoYoLAB session", (*choices, "Paste Cookie header")
    )
    if selected == "Paste Cookie header":
        return None
    session = sessions[choices.index(selected)]
    profiles = browser.list_profiles(session.source_id)
    if not profiles:
        raise browser_error(ErrorCode.BROWSER_PROFILE_MISSING)
    if not session.supports_profiles:
        click.echo(
            "Profile selection is unavailable for this source on this platform. "
            "The reader selects a profile; manual Cookie header input is available.",
            color=False,
        )
        return profiles[0]
    # Tokens disambiguate equal profile names without displaying private paths.
    profile_choices = [f"{p.label} [{p.source_id}]" for p in profiles]
    selected = choose_setup_option(
        io, "Browser profile", (*profile_choices, "Paste Cookie header")
    )
    if selected == "Paste Cookie header":
        return None
    return profiles[profile_choices.index(selected)]


def _session(
    io: SetupIO,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    *,
    no_color: bool,
    configured_color: str,
) -> tuple[Credentials, dict[GameId, tuple[CharacterRef, ...]]]:
    use_browser = io.confirm("Import a HoYoLAB session from supported browsers?")
    profile: BrowserProfile | None = None
    credentials: Credentials | None = None
    while True:
        try:
            if use_browser and profile is None:
                profile = _browser_profile(io, browser)
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
                    click.echo("Cookies found. Verifying HoYoLAB session.", color=False)
                else:
                    credentials = parse_cookie_header(
                        io.secret("Full HoYoLAB Cookie header (hidden)")
                    )
            discovered = _observed_work(
                "session_verify",
                partial(discovery.discover, credentials),
                no_color=no_color,
                configured_color=configured_color,
            )
            click.echo("Session verified.", color=False)
        except ConfigError:
            # Forged menu values or invalid adapter configuration are not a
            # recoverable session failure.
            raise
        except AppError as error:
            # Verification failures retain their classified cause. Unexpected
            # application exceptions and control signals leave this boundary.
            if error.code == ErrorCode.UNEXPECTED:
                raise
            click.echo(format_error(error), err=True, color=False)
        else:
            if any(discovered.values()):
                return credentials, discovered
            click.echo(
                format_error(
                    ConfigError(
                        "No supported linked game characters were found "
                        "on this HoYoLAB account.",
                        hint="Retry discovery or choose a session with linked games.",
                    )
                ),
                err=True,
                color=False,
            )
        recovery = _session_recovery(
            io, use_browser=use_browser, has_credentials=credentials is not None
        )
        if recovery == "Cancel (no changes)":
            raise click.Abort()
        if recovery == "Paste Cookie header":
            use_browser, profile, credentials = False, None, None
        if recovery == "Choose another browser/profile":
            profile, credentials = None, None
        if recovery == "Paste Cookie header again":
            credentials = None
        if recovery == "Change credential source":
            use_browser = io.confirm(
                "Import a HoYoLAB session from supported browsers?"
            )
            profile, credentials = None, None


def _session_recovery(io: SetupIO, *, use_browser: bool, has_credentials: bool) -> str:
    if use_browser:
        retry = (
            "Retry session verification" if has_credentials else "Retry browser import"
        )
        choices = (retry, "Choose another browser/profile", "Paste Cookie header")
    elif has_credentials:
        choices = (
            "Retry session verification",
            "Change credential source",
            "Paste Cookie header again",
        )
    else:
        choices = ("Paste Cookie header again", "Change credential source")
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
        presenter.message(
            f"Warning: {path} will contain account credentials in plaintext. "
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
    previous: AccountConfig | None,
    *,
    staged: bool = True,
) -> tuple[dict[GameId, GameSelection], dict[GameId, CharacterRef]]:
    if staged:
        presenter.step(2, "Games", name)
    else:
        presenter.message(f"Games · Account: {name}")
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
                click.echo(
                    f"{game}: not linked to this session; will be disabled if saved. "
                    "Previous UID retained for manual recovery.",
                    color=False,
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
    account: AccountConfig
    characters: dict[GameId, CharacterRef]
    secret_update: tuple[CredentialStore, Credentials] | None = None
    initial_order: tuple[GameId, ...] | None = None


def _new_account(
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
    credentials, discovered = _session(
        io,
        browser,
        discovery,
        no_color=no_color,
        configured_color=config.display_color if config else "auto",
    )
    games, characters = _select_games(io, presenter, name, discovered, None)
    order = select_game_order(io, None) if config is None else None
    presenter.step(3, "Storage", name)
    backend, store = _backend(io, credentials_file, name, presenter)
    if store.get(name) is not None:
        if not io.confirm("Replace existing stored credentials for this account name?"):
            raise click.Abort()
    return _SetupChanges(
        AccountConfig(backend, games),
        characters,
        (store, credentials),
        order,
    )


def _saved_session(
    account: AccountConfig, credentials_file: Path, name: str
) -> Credentials:
    credentials = open_store(account.credential_backend, credentials_file).get(name)
    if credentials is None:
        raise CredentialError(
            "No stored HoYoLAB session is available for this account.",
            code=ErrorCode.CREDENTIALS_MISSING,
            hint="Run hoyo account edit NAME and select Update HoYoLAB session.",
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
    account = config.accounts[name]
    games = dict(account.games)
    characters: dict[GameId, CharacterRef] = {}
    discovered: dict[GameId, tuple[CharacterRef, ...]] | None = None
    for game in GAME_IDS:
        presenter.game(game)
        current = games.get(game)
        choices = ["Keep current selection" if current else "Skip this game"]
        if current:
            presenter.message(
                f"{'Enabled' if current.enabled else 'Disabled'} · UID {current.uid}"
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
                credentials = _saved_session(account, credentials_file, name)
                discovered = _observed_work(
                    "session_verify",
                    partial(discovery.discover, credentials),
                    no_color=no_color,
                    configured_color=config.display_color,
                )
                presenter.message("Saved session verified.")
            linked = discovered.get(game, ())
            if not linked:
                raise ConfigError(
                    f"No linked characters were found for {game} in the saved session.",
                    hint=(
                        "Keep this game disabled, or select Update HoYoLAB session "
                        "in hoyo account edit NAME."
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


def _existing_account(
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
        "Account settings",
        (
            "Games and characters",
            "Update HoYoLAB session",
            "Credential storage",
            "Cancel (no changes)",
        ),
    )
    if action == "Cancel (no changes)":
        raise click.Abort()
    presenter.message(f"Change: {action}")
    account = config.accounts[name]
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
        account = AccountConfig(account.credential_backend, games)
    elif action == "Update HoYoLAB session":
        if not io.confirm("Replace existing account credentials?"):
            raise click.Abort()
        credentials, discovered = _session(
            io,
            browser,
            discovery,
            no_color=no_color,
            configured_color=config.display_color,
        )
        games, characters = _select_games(
            io, presenter, name, discovered, account, staged=False
        )
        account = AccountConfig(account.credential_backend, games)
        secret_update = (
            open_store(account.credential_backend, credentials_file),
            credentials,
        )
    elif action == "Credential storage":
        if not io.confirm("Change this account's credential backend?"):
            raise click.Abort()
        credentials = _saved_session(account, credentials_file, name)
        backend, store = _backend(io, credentials_file, name, presenter)
        if backend != account.credential_backend:
            if store.get(name) is not None and not io.confirm(
                "Replace existing stored credentials in the selected backend?"
            ):
                raise click.Abort()
            account = AccountConfig(backend, account.games)
            secret_update = (store, credentials)
    return _SetupChanges(
        account,
        characters,
        secret_update,
    )


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
        "Add:      hoyo account add\n"
        "Update:   hoyo account edit NAME\n"
        "Switch:   hoyo account use NAME\n"
        "Settings: hoyo config",
        color=False,
    )


def _validate_entrypoint(
    config: AppConfig | None, mode: Literal["first", "add", "edit"], name: str | None
) -> None:
    if mode == "first" and name is not None:
        raise ConfigError(
            "hoyo init does not accept an account override.",
            hint="Run hoyo account edit NAME to update an existing account.",
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
                "An exact account name is required.", hint="Run: hoyo account"
            )
        try:
            resolve_account(config, name)
        except ConfigError:
            raise ConfigError(
                "Unknown account name; names must match exactly.",
                hint="Run: hoyo account",
            ) from None
    if config is not None and mode == "add" and name in config.accounts:
        raise ConfigError(
            "An account with this exact name already exists.",
            hint="Use hoyo account edit NAME to update an existing account.",
        )


def _run_setup(
    config_file: Path,
    credentials_file: Path,
    io: SetupIO,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    *,
    mode: Literal["first", "add", "edit"],
    requested_account: str | None = None,
    no_color: bool = False,
) -> str | None:
    config, original = _load_setup_config(config_file)
    _validate_entrypoint(config, mode, requested_account)
    if mode == "first" and config is not None:
        _initialized_guidance()
        return None
    presenter = SetupPresenter(
        no_color=no_color, configured_color=config.display_color if config else "auto"
    )
    existing = mode == "edit"
    if existing:
        presenter.message("Edit account")
        assert config is not None and requested_account is not None
        name = requested_account
    else:
        presenter.message("First account setup" if mode == "first" else "Add account")
        presenter.step(1, "Account")
        name = _account_name(io, config, requested_account)
    presenter.message(f"Account: {name}")
    changes = (
        _existing_account(
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
        else _new_account(
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
    prepared = prepare_account(
        config_file,
        name,
        changes.account,
        make_default=config is None,
        game_order=changes.initial_order,
    )
    recovery = (
        "Retry hoyo init." if mode == "first" else f"Retry hoyo account {mode} NAME."
    )
    prepared = replace(prepared, conflict_hint=recovery)
    if prepared.original != original:
        raise ConfigError("Configuration changed during setup.", hint=recovery)
    presenter.review(
        name=name,
        games=changes.account.games,
        characters=changes.characters,
        backend=changes.account.credential_backend,
        default_account=config.default_account if config else name,
        order=config.game_order if config else changes.initial_order or GAME_IDS,
        staged=not existing,
    )
    if (
        choose_setup_option(
            io,
            "Save account configuration and credentials?"
            if changes.secret_update
            else "Save account settings?",
            ("Save", "Cancel (no changes)"),
        )
        != "Save"
    ):
        raise click.Abort()
    if changes.secret_update is None:
        commit_config(prepared)
        return name
    store, credentials = changes.secret_update
    restore = store.checkpoint(name)
    try:
        store.put(name, credentials)
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


def _run_account_setup(
    config_file: Path,
    credentials_file: Path,
    io: SetupIO,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    *,
    mode: Literal["first", "add", "edit"],
    requested_account: str | None = None,
    no_color: bool = False,
) -> None:
    try:
        name = _run_setup(
            config_file,
            credentials_file,
            io,
            browser,
            discovery,
            mode=mode,
            requested_account=requested_account,
            no_color=no_color,
        )
    except (click.Abort, KeyboardInterrupt, asyncio.CancelledError):
        click.echo("Setup cancelled; no changes saved.", color=False)
        raise
    if name is not None:
        click.echo(f"Configured account {name}. Run hoyo to continue.", color=False)


def run_init(
    config_file: Path,
    credentials_file: Path,
    io: SetupIO,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    *,
    requested_account: str | None = None,
    no_color: bool = False,
) -> None:
    _run_account_setup(
        config_file,
        credentials_file,
        io,
        browser,
        discovery,
        mode="first",
        requested_account=requested_account,
        no_color=no_color,
    )


def run_add_account(
    config_file: Path,
    credentials_file: Path,
    io: SetupIO,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    *,
    requested_account: str | None = None,
    no_color: bool = False,
) -> None:
    _run_account_setup(
        config_file,
        credentials_file,
        io,
        browser,
        discovery,
        mode="add",
        requested_account=requested_account,
        no_color=no_color,
    )


def run_edit_account(
    config_file: Path,
    credentials_file: Path,
    io: SetupIO,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    *,
    requested_account: str,
    no_color: bool = False,
) -> None:
    _run_account_setup(
        config_file,
        credentials_file,
        io,
        browser,
        discovery,
        mode="edit",
        requested_account=requested_account,
        no_color=no_color,
    )


def configure_account(
    *,
    options: CommandOptions,
    mode: Literal["first", "add", "edit"] = "first",
    name: str | None = None,
) -> None:
    try:
        config, _ = _load_setup_config(config_path())
        requested = options.account if mode == "first" else name
        _validate_entrypoint(config, mode, requested)
        if mode == "first" and config is not None:
            _initialized_guidance()
            return
        components = (
            config_path(),
            credentials_path(),
            ClickSetupIO(),
            SystemBrowserImporter(),
            HoYoLABDiscovery(),
        )
        if mode == "edit":
            assert requested is not None
            run_edit_account(
                *components, requested_account=requested, no_color=options.no_color
            )
        elif mode == "add":
            run_add_account(
                *components, requested_account=requested, no_color=options.no_color
            )
        else:
            run_init(*components, no_color=options.no_color)
    except (AppError, click.Abort):
        raise
    except Exception:
        raise AppError(
            "Account setup failed. Check configuration and credential storage "
            "before retrying."
        ) from None
