"""Immutable primitives and result envelopes, independent of the API library."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, TypeAlias

from hoyo_cli.errors import AppError

if TYPE_CHECKING:
    from hoyo_cli.models.genshin import GenshinStatus
    from hoyo_cli.models.hsr import HSRStatus
    from hoyo_cli.models.zzz import ZZZStatus

GameId = Literal["genshin", "hsr", "zzz"]
GAME_IDS: tuple[GameId, ...] = ("genshin", "hsr", "zzz")


def require_aware(value: datetime | None) -> None:
    if value is not None and value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required.")


@dataclass(frozen=True)
class CountStatus:
    current: int
    maximum: int

    def __post_init__(self) -> None:
        if type(self.current) is not int or type(self.maximum) is not int:
            raise ValueError("Counts must be integers.")
        if self.current < 0 or self.maximum < 0:
            raise ValueError("Counts must be non-negative.")


@dataclass(frozen=True)
class ResourceStatus(CountStatus):
    full_at: datetime | None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.maximum == 0:
            raise ValueError("Resource maximum must be positive.")
        require_aware(self.full_at)


@dataclass(frozen=True)
class TimedActivity:
    label: str
    state: str
    completes_at: datetime | None = None

    def __post_init__(self) -> None:
        require_aware(self.completes_at)


GameStatus: TypeAlias = "GenshinStatus | HSRStatus | ZZZStatus"


@dataclass(frozen=True)
class CheckInState:
    already_checked_in: bool


@dataclass(frozen=True)
class CheckInReceipt:
    reward: str | None = None


@dataclass(frozen=True)
class CheckInResult:
    game: GameId
    outcome: Literal[
        "claimed", "already_checked_in", "claimed_verified", "unconfirmed", "failed"
    ]
    reward: str | None = None
    error: AppError | None = None


@dataclass(frozen=True)
class StatusResult:
    game: GameId
    status: GameStatus | None
    fetched_at: datetime | None
    stale: bool = False
    error: AppError | None = None
    cache_error: AppError | None = None

    def __post_init__(self) -> None:
        require_aware(self.fetched_at)
        if self.status is not None and self.status.game != self.game:
            raise ValueError("Result game does not match the status snapshot.")
        if self.status is not None and self.fetched_at is None:
            raise ValueError("A status snapshot needs its fetch timestamp.")
        if self.stale and (self.status is None or self.error is None):
            raise ValueError("Stale status needs a snapshot and its fresh-fetch error.")
