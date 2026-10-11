"""Interactive setup: validated selection, explicit secrets, recoverable writes."""

import asyncio
from collections.abc import Callable, Sequence
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
from hoyo_cli.config.repository import commit_config, load_config, prepare_account
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


def _choose(io: SetupIO, prompt: str, choices: Sequence[str]) -> str:
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
    if requested is not None:
        if config is not None:
            resolve_account(config, requested)
        name = requested
    elif config is None:
        name = _new_account_name(io)
    else:
        create = "Create new account"
        while create in config.accounts:
            create += " (new)"
        name = _choose(io, "Account", (*config.accounts, create))
        if name == create:
            name = _new_account_name(io)
    if not _valid_account_name(name):
        raise ConfigError(
            "Account name must be non-empty and contain no control characters."
        )
    return name


def _valid_account_name(name: str) -> bool:
    return bool(name.strip()) and not any(ord(c) < 32 or ord(c) == 127 for c in name)


def _new_account_name(io: SetupIO) -> str:
    while True:
        name = _choose(io, "Account name", ())
        if _valid_account_name(name):
            return name
        click.echo("Account name must be non-empty and contain no control characters.")
        if (
            _choose(
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
    selected = _choose(io, "HoYoLAB session", (*choices, "Paste Cookie header"))
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
    selected = _choose(io, "Browser profile", (*profile_choices, "Paste Cookie header"))
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
    return _choose(
        io,
        "Browser recovery" if use_browser else "Session recovery",
        (*choices, "Cancel (no changes)"),
    )


def _backend(
    io: SetupIO, path: Path, name: str, presenter: SetupPresenter
) -> tuple[CredentialBackend, CredentialStore]:
    backend = _choose(io, "Credential backend", ("keyring", "plaintext"))
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


def _game_order(io: SetupIO, config: AppConfig | None) -> tuple[GameId, ...]:
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
        selected = _choose(io, "Next game in display order", remaining)
        # _choose has validated membership in the remaining canonical IDs.
        game = cast(GameId, selected)
        order.append(game)
        remaining.remove(game)
    return tuple(order)


def _run_init(
    config_file: Path,
    credentials_file: Path,
    io: SetupIO,
    browser: BrowserImporter,
    discovery: CharacterDiscovery,
    *,
    requested_account: str | None = None,
    no_color: bool = False,
) -> str:
    config = load_config(config_file) if config_file.exists() else None
    presenter = SetupPresenter(
        no_color=no_color, configured_color=config.display_color if config else "auto"
    )
    presenter.step(1, "Account")
    name = _account_name(io, config, requested_account)
    presenter.message(f"Account: {name}")
    previous = config.accounts.get(name) if config is not None else None
    if previous is not None and not io.confirm("Replace existing account credentials?"):
        raise click.Abort()
    credentials, discovered = _session(
        io,
        browser,
        discovery,
        no_color=no_color,
        configured_color=config.display_color if config else "auto",
    )
    presenter.step(2, "Games", name)
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
        choices = [character_choice(char) for char in characters]
        selected = _choose(io, f"Character for {game}", (*choices, "Skip this game"))
        if selected == "Skip this game":
            continue
        character = characters[choices.index(selected)]
        selected_characters[game] = character
        games[game] = GameSelection(True, character.uid)
    if not any(selection.enabled for selection in games.values()):
        raise ConfigError("Select at least one enabled game; no files were changed.")
    make_default = config is None or _confirm(
        io, f"Use {name} as the default account?", default=True
    )
    order = _game_order(io, config)
    presenter.step(3, "Storage", name)
    backend, store = _backend(io, credentials_file, name, presenter)
    if previous is not None and backend != previous.credential_backend:
        if not io.confirm("Change this account's credential backend?"):
            raise click.Abort()
    if previous is None and store.get(name) is not None:
        if not io.confirm("Replace existing stored credentials for this account name?"):
            raise click.Abort()
    prepared = prepare_account(
        config_file,
        name,
        AccountConfig(backend, games),
        make_default=make_default,
        game_order=order,
    )
    presenter.review(
        name=name,
        games=games,
        characters=selected_characters,
        backend=backend,
        default_account=name
        if make_default or config is None
        else config.default_account,
        order=order,
    )
    if (
        _choose(
            io,
            "Save account configuration and credentials?",
            ("Save", "Cancel (no changes)"),
        )
        != "Save"
    ):
        raise click.Abort()
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
    try:
        name = _run_init(
            config_file,
            credentials_file,
            io,
            browser,
            discovery,
            requested_account=requested_account,
            no_color=no_color,
        )
    except (click.Abort, KeyboardInterrupt, asyncio.CancelledError):
        click.echo("Setup cancelled; no changes saved.", color=False)
        raise
    click.echo(f"Configured account {name}. Run hoyo to continue.", color=False)


def configure_account(*, options: CommandOptions) -> None:
    try:
        run_init(
            config_path(),
            credentials_path(),
            ClickSetupIO(),
            SystemBrowserImporter(),
            HoYoLABDiscovery(),
            requested_account=options.account,
            no_color=options.no_color,
        )
    except (AppError, click.Abort):
        raise
    except Exception:
        raise AppError(
            "Account setup failed. Check configuration and credential storage "
            "before retrying."
        ) from None
