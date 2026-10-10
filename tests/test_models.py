"""Application-owned snapshots and deterministic local display times."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from hoyo_cli.models.common import CountStatus, ResourceStatus, TimedActivity
from hoyo_cli.models.genshin import GenshinStatus
from hoyo_cli.timefmt import (
    format_duration,
    format_full_at,
    format_local_timestamp,
)

NOW = datetime(2026, 10, 10, 18, 31, tzinfo=timezone(timedelta(hours=7)))


@pytest.mark.parametrize(
    ("seconds", "expected"), [(2700, "45m"), (4560, "1h 16m"), (187200, "2d 4h")]
)
def test_duration_45m_1h16m_2d4h(seconds: int, expected: str) -> None:
    assert format_duration(seconds) == expected


def test_full_resource_shows_full_not_0m() -> None:
    assert format_full_at(NOW, NOW) == "Full"
    assert format_full_at(None, NOW) == "Unavailable"
    assert format_full_at(NOW + timedelta(seconds=30), NOW).startswith("Full in <1m")


def test_local_time_same_day_and_other_day() -> None:
    assert format_local_timestamp(NOW.astimezone(UTC), NOW) == "18:31"
    assert format_local_timestamp(NOW + timedelta(days=1), NOW) == "Sun 18:31"
    assert format_local_timestamp(NOW + timedelta(days=9), NOW) == "Oct 19 · 18:31"
    assert format_full_at(NOW + timedelta(minutes=76), NOW) == "Full in 1h 16m · 19:47"


def test_status_models_are_frozen() -> None:
    resource = ResourceStatus(190, 200, NOW)
    with pytest.raises(FrozenInstanceError):
        attribute = "current"
        setattr(resource, attribute, 200)
    with pytest.raises(ValueError, match="timezone"):
        ResourceStatus(190, 200, NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        CountStatus(-1, 4)


def test_locked_system_is_valid_model() -> None:
    assert TimedActivity("Transformer", "Locked").state == "Locked"


def test_completed_states_do_not_require_historical_timestamps() -> None:
    resource = ResourceStatus(200, 200, None)
    assert resource.current == resource.maximum == 200
    assert resource.full_at is None
    for label, state in (("Expedition 1", "Finished"), ("Transformer", "Ready")):
        activity = TimedActivity(label, state)
        assert activity.state == state and activity.completes_at is None


def test_explicit_completion_timestamp_is_preserved() -> None:
    completed_at = NOW - timedelta(hours=1)
    assert (
        TimedActivity("Expedition 1", "Finished", completed_at).completes_at
        == completed_at
    )
    assert ResourceStatus(200, 200, completed_at).full_at == completed_at


def test_snapshot_does_not_derive_resource_amounts() -> None:
    snapshot = ResourceStatus(190, 200, NOW + timedelta(hours=1))
    format_full_at(snapshot.full_at, NOW + timedelta(hours=2))
    assert snapshot.current == 190
    status = GenshinStatus(
        resin=snapshot,
        realm_currency=None,
        commissions=CountStatus(4, 4),
        commission_reward_claimed=True,
        expeditions=(),
        max_expeditions=5,
        transformer=TimedActivity("Transformer", "Unavailable"),
        weekly_discounts=CountStatus(3, 3),
    )
    assert status.game == "genshin"
    with pytest.raises(FrozenInstanceError):
        attribute = "game"
        setattr(status, attribute, "hsr")
