"""Shared plain-text error presentation for command and per-game failures."""

from hoyo_cli.errors import AppError


def format_error(error: AppError, *, context: str | None = None) -> str:
    summary = f"{context}: {error.message}" if context else error.message
    lines = [f"Error: {summary}"]
    if error.hint:
        # A direct command already has an action label, such as first-run setup.
        lines.append(
            error.hint if error.hint.startswith("Run: ") else f"Try: {error.hint}"
        )
    return "\n".join(lines)
