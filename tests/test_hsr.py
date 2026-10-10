"""Pinned Star Rail notes mapping, including optional system availability."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import genshin
import pytest

from hoyo_cli.errors import UnsupportedDataError
from hoyo_cli.hoyolab.adapters.hsr import normalize_hsr
from hoyo_cli.models.common import CountStatus

NOW = datetime(2026, 10, 10, 11, 31, tzinfo=UTC)


@pytest.fixture
def payload() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((Path(__file__).parent / "fixtures/hsr_notes.json").read_text()),
    )


def test_hsr_power_and_reserve(payload: dict[str, object]) -> None:
    status = normalize_hsr(payload, now=NOW)
    assert (status.power.current, status.power.maximum) == (211, 300)
    assert status.power.full_at == NOW + timedelta(seconds=32040)
    assert status.reserved_power == CountStatus(842, 2400)
    assert not status.reserve_full
    payload.update(
        current_stamina=300,
        stamina_recover_time=0,
        current_reserve_stamina=2400,
        is_reserve_stamina_full=True,
    )
    full = normalize_hsr(payload, now=NOW)
    assert full.power.full_at is None and full.reserve_full


def test_daily_training_assignments_and_weeklies(payload: dict[str, object]) -> None:
    status = normalize_hsr(payload, now=NOW)
    assert status.daily_training == CountStatus(500, 500)
    assert status.accepted_assignments == 2 and status.max_assignments == 4
    assert status.assignments[0].state == "Finished"
    assert status.assignments[0].completes_at is None
    assert status.assignments[1].completes_at == NOW + timedelta(seconds=8040)
    assert status.simulated_universe == CountStatus(14000, 18000)
    assert status.divergent_universe == CountStatus(800, 1000)
    assert status.echo_of_war == CountStatus(2, 3)


def test_du_unavailable_is_not_api_error(payload: dict[str, object]) -> None:
    del payload["rogue_tourn_weekly_unlocked"]
    assert normalize_hsr(payload).divergent_universe == "Unavailable"
    payload["rogue_tourn_weekly_unlocked"] = False
    assert normalize_hsr(payload).divergent_universe == "Locked"


def test_hsr_model_and_raw_payload_are_equivalent(payload: dict[str, object]) -> None:
    model = genshin.models.StarRailNote.model_validate(payload)
    assert normalize_hsr(model, now=NOW) == normalize_hsr(payload, now=NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_stamina", -1),
        ("is_reserve_stamina_full", "false"),
        ("expeditions", "changed"),
        ("accepted_epedition_num", 9),
    ],
)
def test_changed_core_notes_are_classified(
    payload: dict[str, object], field: str, value: object
) -> None:
    payload[field] = value
    with pytest.raises(UnsupportedDataError):
        normalize_hsr(payload)
