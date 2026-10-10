"""Interactive setup: validated selection, explicit secrets, recoverable writes."""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

import click

from hoyo_cli.auth.browser_import import BrowserImporter, SystemBrowserImporter
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
    BrowserImportError,
    ConfigError,
    CredentialError,
)
from hoyo_cli.hoyolab.discovery import CharacterDiscovery, HoYoLABDiscovery
from hoyo_cli.models.common import GAME_IDS, GameId


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


def _session(io: SetupIO, browser: BrowserImporter) -> Credentials:
    if io.confirm("Import a HoYoLAB session from supported browsers?"):
        try:
            sessions = browser.list_sessions()
            choices = [
                f"{session.browser_label} [{session.source_id}]" for session in sessions
            ]
            selected = _choose(io, "HoYoLAB session", (*choices, "Paste Cookie header"))
            if selected != "Paste Cookie header":
                return browser.import_session(
                    sessions[choices.index(selected)].source_id
                )
        except BrowserImportError:
            click.echo(
                "Browser import failed. Manual Cookie header input is available.",
                color=False,
            )
    return parse_cookie_header(io.secret("Full HoYoLAB Cookie header (hidden)"))


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
) -> None:
    config = load_config(config_file) if config_file.exists() else None
    name = _account_name(io, config, requested_account)
    previous = config.accounts.get(name) if config is not None else None
    if previous is not None and not io.confirm("Replace existing account credentials?"):
        raise click.Abort()
    credentials = _session(io, browser)
    discovered = discovery.discover(credentials)
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
        )
    except (AppError, click.Abort):
        raise
    except Exception:
        raise AppError(
            "Account setup failed. Check configuration and credential storage "
            "before retrying."
        ) from None
