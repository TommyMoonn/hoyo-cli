"""Editor argv, executable detection, and shell-free launch resolution."""

import ctypes
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from hoyo_cli.config.models import validate_editor_command
from hoyo_cli.errors import ConfigError

EDITOR_HINT = (
    "Choose hoyo config editor, or set VISUAL/EDITOR to an available executable."
)
PRESETS = (
    ("VS Code", ("code", "--wait")),
    ("Zed", ("zed", "--wait")),
    ("Zed", ("zeditor", "--wait")),
    ("Sublime Text", ("subl", "--wait")),
    ("Notepad", ("notepad.exe",)),
)


def parse_editor_arguments(command: str) -> tuple[str, ...]:
    """Parse environment strings with the platform's quoting rules, never a shell."""
    try:
        validate_editor_command((command,))
        if not command.strip():
            raise ValueError
        if os.name != "nt":
            return validate_editor_command(shlex.split(command))
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        parse = shell32.CommandLineToArgvW
        parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
        release = kernel32.LocalFree
        release.argtypes = [ctypes.c_void_p]
        release.restype = ctypes.c_void_p
        count = ctypes.c_int()
        arguments = parse(command.lstrip(), ctypes.byref(count))
        if not arguments:
            raise OSError
        try:
            return validate_editor_command(
                tuple(str(arguments[index]) for index in range(count.value))
            )
        finally:
            release(arguments)
    except (OSError, ValueError, ConfigError):
        raise ConfigError("Invalid editor arguments.", hint=EDITOR_HINT) from None


@dataclass(frozen=True)
class ResolvedEditor:
    arguments: tuple[str, ...]
    waits: bool
    node_cli: bool = False


def resolve_editor_command(command: tuple[str, ...]) -> ResolvedEditor:
    command = validate_editor_command(command)
    executable = shutil.which(command[0])
    if executable is None:
        raise ConfigError(
            "Editor executable is unavailable. Add it to PATH or use its full path.",
            hint=EDITOR_HINT,
        )
    name = Path(executable).stem.lower()
    waits = name in {"code", "zed", "zeditor", "subl"} and "--wait" in command[1:]
    if Path(executable).suffix.lower() in {".cmd", ".bat"}:
        # VS Code's Windows shim runs its bundled Electron executable as Node.
        # Resolve that installation layout directly; never interpret batch text.
        if os.name == "nt" and Path(executable).name.lower() == "code.cmd":
            root = Path(executable).parent.parent
            runtime = root / "Code.exe"
            cli = root / "resources/app/out/cli.js"
            candidates = (
                [cli]
                if cli.is_file()
                else [
                    path
                    for path in root.glob("*/resources/app/out/cli.js")
                    if path.is_file()
                ]
            )
            if runtime.is_file() and len(candidates) == 1:
                return ResolvedEditor(
                    (str(runtime), str(candidates[0]), *command[1:]),
                    waits,
                    node_cli=True,
                )
        raise ConfigError(
            "Editor requires a shell launcher. Choose its native executable.",
            hint=EDITOR_HINT,
        )
    # The Windows Code.exe GUI does not provide the CLI's wait marker protocol.
    if os.name == "nt" and name == "code":
        waits = False
    return ResolvedEditor((executable, *command[1:]), waits)


def detected_editors() -> dict[str, tuple[str, ...]]:
    detected: dict[str, tuple[str, ...]] = {}
    for label, command in PRESETS:
        if label in detected or (label == "Notepad" and os.name != "nt"):
            continue
        try:
            resolve_editor_command(command)
        except (ConfigError, OSError):
            continue
        detected[label] = command
    return detected


def resolve_editor(saved: tuple[str, ...] | None) -> ResolvedEditor:
    if saved is not None:
        return resolve_editor_command(saved)
    for name in ("VISUAL", "EDITOR"):
        value = os.environ.get(name)
        if value and value.strip():
            return resolve_editor_command(parse_editor_arguments(value))
    raise ConfigError("No editor configured.", hint=EDITOR_HINT)


def describe_editor(command: tuple[str, ...] | None) -> str:
    if command is None:
        return "Use VISUAL/EDITOR"
    return repr(list(command))


def editor_available(command: tuple[str, ...] | None) -> bool:
    try:
        resolve_editor(command)
    except (ConfigError, OSError):
        return False
    return True
