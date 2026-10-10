"""Shared immutable command input contract."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandOptions:
    account: str | None = None
    detailed: bool = False
    no_color: bool = False
