"""Validated notes primitives shared by the three pinned upstream adapters."""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from enum import Enum
from typing import TypeVar, cast

from hoyo_cli.models.common import ResourceStatus

T = TypeVar("T")


def mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError
    entries = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in entries):
        raise ValueError
    return cast(Mapping[str, object], entries)


def integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError
    return value


def boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError
    return value


def seconds(value: object) -> int:
    if isinstance(value, timedelta):
        result = int(value.total_seconds())
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        result = int(value)
    else:
        result = integer(value)
    if result < 0:
        raise ValueError
    return result


def resource(
    data: Mapping[str, object], current: str, maximum: str, recovery: str, now: datetime
) -> ResourceStatus:
    amount, limit = integer(data[current]), integer(data[maximum])
    remaining = seconds(data[recovery])
    # A countdown is a future deadline, never an inferred historical timestamp.
    full_at = (
        now + timedelta(seconds=remaining) if amount < limit and remaining > 0 else None
    )
    return ResourceStatus(amount, limit, full_at)


def safe_text(value: str) -> str:
    return "".join(char for char in value if char.isprintable())


def state(value: object, labels: Mapping[str, str]) -> str:
    key = value.value if isinstance(value, Enum) else value
    return labels.get(key, "Unavailable") if isinstance(key, str) else "Unavailable"


def optional(value: object, normalize: Callable[[object], T]) -> T | None:
    if value is None:
        return None
    try:
        return normalize(value)
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
