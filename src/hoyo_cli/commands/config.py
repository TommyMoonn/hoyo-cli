"""Non-secret config inspection and explicitly selected editor launching."""

import ctypes
import os
import shlex
import subprocess
from pathlib import Path

import click

from hoyo_cli.commands import CommandOptions
from hoyo_cli.config.models import AppConfig, ordered_games, resolve_account
from hoyo_cli.config.paths import config_path
from hoyo_cli.config.repository import load_config
from hoyo_cli.errors import AppError, ConfigError


def show_config(config: AppConfig, path: Path) -> str:
    lines = [
        f"Config: {path}",
        f"Default account: {config.default_account}",
        f"Game order: {', '.join(config.game_order) or '(canonical fallback)'}",
        f"Display color: {config.display_color}",
    ]
    for name, account in config.accounts.items():
        lines.extend(
            [
                "",
                f"Account: {name}",
                f"  Credential backend: {account.credential_backend}",
            ]
        )
        any_enabled = False
        for game in ordered_games(config):
            selection = account.games.get(game)
            if selection is not None and selection.enabled:
                lines.append(f"  {game}: enabled, UID {selection.uid}")
                any_enabled = True
            elif selection is not None:
                lines.append(f"  {game}: disabled, UID {selection.uid}")
            else:
                lines.append(f"  {game}: not configured")
        if not any_enabled:
            lines.append("  No enabled games")
    lines.extend(
        ["", "Run: hoyo init to change settings or update your HoYoLAB session."]
    )
    return "\n".join(lines)


def _editor_arguments(editor: str) -> list[str]:
    if "\0" in editor:
        raise ValueError("Editor command contains a null character.")
    if os.name != "nt":
        return shlex.split(editor)
    # Use Windows' own argv parser so quoted paths and backslashes retain meaning.
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    parse = shell32.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    release = kernel32.LocalFree
    release.argtypes = [ctypes.c_void_p]
    release.restype = ctypes.c_void_p
    count = ctypes.c_int()
    arguments = parse(editor.lstrip(), ctypes.byref(count))
    if not arguments:
        raise OSError("Unable to parse editor command.")
    try:
        return [str(arguments[index]) for index in range(count.value)]
    finally:
        release(arguments)


def edit_config(path: Path, editor: str | None) -> int:
    if editor is None or not editor.strip():
        raise ConfigError(
            f"No editor configured. Set VISUAL or EDITOR.\nConfig: {path}"
        )
    try:
        arguments = _editor_arguments(editor)
        if not arguments or not arguments[0]:
            raise ValueError("Editor executable is empty.")
        result = subprocess.run([*arguments, str(path)], check=False, shell=False)
    except (OSError, ValueError):
        raise ConfigError(
            f"Unable to launch configured editor. Set VISUAL or EDITOR.\nConfig: {path}"
        ) from None
    return result.returncode


def run_config(*, options: CommandOptions, edit: bool) -> None:
    path = config_path()
    config = load_config(path)
    resolve_account(config, options.account)
    if edit:
        editor = next(
            (
                value
                for name in ("VISUAL", "EDITOR")
                if (value := os.environ.get(name)) and value.strip()
            ),
            None,
        )
        exit_code = edit_config(path, editor)
        if exit_code != 0:
            raise AppError(f"Editor exited with status {exit_code}.\nConfig: {path}")
    else:
        click.echo(show_config(config, path), color=False)
