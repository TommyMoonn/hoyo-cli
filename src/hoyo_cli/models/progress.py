"""Safe service observations, independent of terminal presentation."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias

from hoyo_cli.models.common import GameId

ProgressStage: TypeAlias = Literal[
    "fetching", "checking", "claiming", "verifying", "complete"
]
ProgressOutcome: TypeAlias = Literal[
    "fresh", "stale", "failed", "claimed", "already_checked_in"
]


@dataclass(frozen=True)
class ProgressEvent:
    game: GameId
    stage: ProgressStage
    outcome: ProgressOutcome | None = None


ProgressObserver: TypeAlias = Callable[[ProgressEvent], None]


def notify_progress(observer: ProgressObserver | None, event: ProgressEvent) -> None:
    """Observers are best effort; control signals still propagate to the caller."""
    if observer is not None:
        try:
            observer(event)
        except Exception:
            # Presentation failures must not change a read or repeat a mutation.
            # Never expose the observer's potentially sensitive exception text.
            pass
