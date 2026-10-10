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

_Result = TypeVar("_Result")


class SetupIO(Protocol):
    def choose(self, prompt: str, choices: Sequence[str]) -> str: ...
    def confirm(self, prompt: str) -> bool: ...
    def secret(self, prompt: str) -> str: ...


class ClickSetupIO:
    def choose(self, prompt: str, choices: Sequence[str]) -> str:
        if not choices:
            return click.prompt(prompt, type=str)
        for index, choice in enumerate(choices, 1):
            click.echo(f"  {index}. {choice}", color=False)
        click.echo(f"  {len(choices) + 1}. Cancel setup", color=False)
        index = click.prompt(prompt, type=click.IntRange(1, len(choices) + 1))
        if index == len(choices) + 1:
            raise click.Abort()
        return choices[index - 1]

    def confirm(self, prompt: str) -> bool:
        return click.confirm(prompt, default=False)

    def secret(self, prompt: str) -> str:
        return click.prompt(prompt, hide_input=True, type=str)


def _choose(io: SetupIO, prompt: str, choices: Sequence[str]) -> str:
    value = io.choose(prompt, choices)
    if choices and value not in choices:
        raise ConfigError("Invalid setup selection; no files were changed.")
    return value


def _account_name(io: SetupIO, config: AppConfig | None, requested: str | None) -> str:
    if requested is not None:
        if config is not None:
            resolve_account(config, requested)
        name = requested
    elif config is None:
        name = _choose(io, "Account name", ())
    else:
        create = "Create new account"
        while create in config.accounts:
            create += " (new)"
        name = _choose(io, "Account", (*config.accounts, create))
        if name == create:
            name = _choose(io, "Account name", ())
    if not name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ConfigError(
            "Account name must be non-empty and contain no control characters."
        )
    return name


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
    while use_browser:
        try:
            if profile is None:
                profile = _browser_profile(io, browser)
                if profile is None:
                    break
            if credentials is None:
                credentials = _observed_work(
                    "browser_read",
                    partial(browser.import_session, profile.source_id),
                    no_color=no_color,
                    configured_color=configured_color,
                )
                click.echo("Cookies found. Verifying HoYoLAB session.", color=False)
            discovered = _observed_work(
                "session_verify",
                partial(discovery.discover, credentials),
                no_color=no_color,
                configured_color=configured_color,
            )
            click.echo("Session verified.", color=False)
            return credentials, discovered
        except ConfigError:
            raise
        except AppError as error:
            # Verification failures retain their classified cause. Unexpected
            # application exceptions and control signals leave this boundary.
            if error.code == ErrorCode.UNEXPECTED:
                raise
            click.echo(format_error(error), err=True, color=False)
        retry = (
            "Retry session verification"
            if credentials is not None
            else "Retry browser import"
        )
        recovery = _choose(
            io,
            "Browser recovery",
            (
                retry,
                "Choose another browser/profile",
                "Paste Cookie header",
                "Cancel (no changes)",
            ),
        )
        if recovery == "Cancel (no changes)":
            raise click.Abort()
        if recovery == "Paste Cookie header":
            break
        if recovery == "Choose another browser/profile":
            profile, credentials = None, None
    credentials = parse_cookie_header(io.secret("Full HoYoLAB Cookie header (hidden)"))
    discovered = _observed_work(
        "session_verify",
        lambda: discovery.discover(credentials),
        no_color=no_color,
        configured_color=configured_color,
    )
    click.echo("Session verified.", color=False)
    return credentials, discovered


def _backend(
    io: SetupIO, path: Path, name: str
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
        click.echo(
            f"Warning: {path} will contain account credentials in plaintext. "
            "User-only permissions are requested; "
            "Windows uses inherited directory ACLs.",
            color=False,
        )
        if not io.confirm("Accept plaintext credential storage?"):
            raise click.Abort()
    return selected, store


def _game_order(io: SetupIO, config: AppConfig | None) -> tuple[GameId, ...]:
    if config is not None and io.confirm("Keep the current game display order?"):
        return config.game_order
    remaining = list(GAME_IDS)
    order: list[GameId] = []
    while remaining:
        selected = _choose(io, "Next game in display order", remaining)
        # _choose has validated membership in the remaining canonical IDs.
        game = cast(GameId, selected)
        order.append(game)
        remaining.remove(game)
    return tuple(order)


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
    config = load_config(config_file) if config_file.exists() else None
    name = _account_name(io, config, requested_account)
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
    if not any(discovered.values()):
        raise ConfigError(
            "No supported linked game characters were found on this HoYoLAB account."
        )
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
    for game in GAME_IDS:
        characters = discovered.get(game, ())
        if not characters:
            prior = previous.games.get(game) if previous is not None else None
            if prior is not None and prior.enabled:
                click.echo(
                    f"{game}: not linked to this session; will be disabled if saved. "
                    "Previous UID retained for manual recovery.",
                    color=False,
                )
            continue
        choices = [
            f"{char.label} | UID {char.uid} | {char.region}" for char in characters
        ]
        selected = _choose(io, f"Character for {game}", choices)
        character = characters[choices.index(selected)]
        games[game] = GameSelection(io.confirm(f"Enable {game}?"), character.uid)
    if not any(selection.enabled for selection in games.values()):
        raise ConfigError("Select at least one enabled game; no files were changed.")
    make_default = config is None or io.confirm(f"Use {name} as the default account?")
    order = _game_order(io, config)
    backend, store = _backend(io, credentials_file, name)
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
    if not io.confirm("Save account configuration and credentials?"):
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
