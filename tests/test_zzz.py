"""Verified ZZZ notes fields and ordinary unavailable system states."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import genshin
import pytest

from hoyo_cli.errors import UnsupportedDataError
from hoyo_cli.hoyolab.adapters.zzz import normalize_zzz
from hoyo_cli.models.common import CountStatus
from hoyo_cli.models.zzz import TempleStatus

NOW = datetime(2026, 10, 10, 11, 31, tzinfo=UTC)


@pytest.fixture
def payload() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((Path(__file__).parent / "fixtures/zzz_notes.json").read_text()),
    )


def test_zzz_battery_and_engagement(payload: dict[str, object]) -> None:
    status = normalize_zzz(payload, now=NOW)
    assert (status.battery.current, status.battery.maximum) == (89, 240)
    assert status.battery.full_at == NOW + timedelta(seconds=55260)
    assert status.engagement == CountStatus(400, 400)


def test_scratch_card_and_video_store(payload: dict[str, object]) -> None:
    status = normalize_zzz(payload)
    assert status.scratch_card == "Done"
    assert status.video_store == "Revenue available"
    payload.update(card_sign="CardSignNo", vhs_sale={"sale_state": "SaleStateNo"})
    status = normalize_zzz(payload)
    assert status.scratch_card == "Not done" and status.video_store == "Waiting to open"


def test_hollow_zero_weeklies_member_card(payload: dict[str, object]) -> None:
    status = normalize_zzz(payload, now=NOW)
    assert status.hollow_zero_bounties == CountStatus(4, 5)
    assert status.hollow_zero_points == CountStatus(600, 800)
    assert status.weekly_tasks == CountStatus(8000, 8000)
    assert status.bounties_reset_at == NOW + timedelta(days=1)
    assert status.weekly_reset_at == NOW + timedelta(days=1)
    assert status.member_card.state == "Active"
    assert status.member_card.completes_at == NOW + timedelta(days=2)


def test_temple_supported_details(payload: dict[str, object]) -> None:
    status = normalize_zzz(payload, now=NOW)
    temple = status.temple
    assert isinstance(temple, TempleStatus)
    assert temple.auto_work is not None
    assert [
        activity.state
        for activity in (
            temple.bench,
            temple.shelves,
            temple.expedition,
            temple.auto_work,
        )
    ] == [
        "Can produce",
        "Sold out",
        "Expedition complete",
        "Running",
    ]
    assert temple.auto_work.completes_at == NOW + timedelta(hours=1)
    assert temple.level == 12
    assert temple.currency == CountStatus(1000, 3000)


def test_level_only_temple_does_not_prove_a_lock(payload: dict[str, object]) -> None:
    payload["temple_running"] = {"level": 0}
    assert normalize_zzz(payload).temple == "Unavailable"


def test_valid_zero_level_temple_retains_reported_management(
    payload: dict[str, object],
) -> None:
    temple = cast(dict[str, object], payload["temple_running"])
    temple["level"] = 0
    status = normalize_zzz(payload)
    assert isinstance(status.temple, TempleStatus)
    assert status.temple.level == 0
    assert status.temple.bench.state == "Can produce"


@pytest.mark.parametrize("missing", ["omitted", "null"])
def test_missing_zzz_states_remain_unknown(
    payload: dict[str, object], missing: str
) -> None:
    for field in (
        "card_sign",
        "vhs_sale",
        "bounty_commission",
        "survey_points",
        "weekly_task",
        "member_card",
        "temple_running",
    ):
        if missing == "null":
            payload[field] = None
        else:
            payload.pop(field)
    status = normalize_zzz(payload)
    assert status.bounties_state == "Unavailable"
    assert status.hollow_zero_bounties is None
    assert status.hollow_zero_points is None
    assert status.weekly_tasks is None
    assert status.scratch_card == status.video_store == "Unavailable"
    assert status.member_card.state == status.temple == "Unavailable"


@pytest.mark.parametrize("unlock", [None, True])
def test_missing_unlock_flag_does_not_change_bounty_quantities(
    payload: dict[str, object], unlock: bool | None
) -> None:
    payload["bounty_commission"] = {
        "num": 0,
        "total": 8000,
        "refresh_time": 86400,
        "unlock": unlock,
    }
    status = normalize_zzz(payload)
    assert status.hollow_zero_bounties == CountStatus(0, 8000)
    assert status.bounties_state == "Available"


def test_missing_optional_zzz_system(payload: dict[str, object]) -> None:
    for key in (
        "bounty_commission",
        "survey_points",
        "weekly_task",
        "member_card",
        "temple_running",
    ):
        payload.pop(key)
    status = normalize_zzz(payload)
    assert status.hollow_zero_bounties is None and status.weekly_tasks is None
    assert status.member_card.state == "Unavailable" and status.temple == "Unavailable"


def test_zzz_model_and_raw_payload_are_equivalent(payload: dict[str, object]) -> None:
    model = genshin.models.ZZZNotes.model_validate(deepcopy(payload))
    assert normalize_zzz(model, now=NOW) == normalize_zzz(payload, now=NOW)


@pytest.mark.parametrize(
    "field,value", [("energy", {}), ("vitality", {"current": -1, "max": 400})]
)
def test_changed_core_notes_are_classified(
    payload: dict[str, object], field: str, value: object
) -> None:
    payload[field] = value
    with pytest.raises(UnsupportedDataError):
        normalize_zzz(payload)


@pytest.mark.parametrize(
    "field", ["hollow_zero", "weekly_task", "vhs_sale", "member_card", "temple_running"]
)
def test_changed_optional_system_does_not_hide_core_status(
    payload: dict[str, object], field: str
) -> None:
    payload[field] = {"changed": "test-secret-123"}
    status = normalize_zzz(payload)
    assert status.battery.current == 89 and status.engagement.current == 400


def test_unknown_management_states_are_unavailable(payload: dict[str, object]) -> None:
    temple = cast(dict[str, object], payload["temple_running"])
    temple.update(
        bench_state="new-state", shelve_state="new-state", expedition_state="new-state"
    )
    status = normalize_zzz(payload)
    assert isinstance(status.temple, TempleStatus)
    assert all(
        activity.state == "Unavailable"
        for activity in (
            status.temple.bench,
            status.temple.shelves,
            status.temple.expedition,
        )
    )


def test_full_battery_and_completed_auto_work_have_no_inferred_timestamps(
    payload: dict[str, object],
) -> None:
    payload["energy"] = {"progress": {"current": 240, "max": 240}, "restore": 0}
    temple = cast(dict[str, object], payload["temple_running"])
    temple["auto_work"] = {
        "is_auto_work_running": False,
        "auto_work_ended": True,
        "left_ts": 0,
    }
    status = normalize_zzz(payload)
    assert status.battery.full_at is None
    assert isinstance(status.temple, TempleStatus)
    assert status.temple.auto_work is not None
    assert status.temple.auto_work.state == "Finished"
    assert status.temple.auto_work.completes_at is None


def test_bounties_locked_and_member_card_inactive(payload: dict[str, object]) -> None:
    payload["bounty_commission"] = {"unlock": False}
    payload["member_card"] = {
        "member_card_state": "MemberCardStateNo",
        "exp_time": "0",
        "is_open": False,
    }
    status = normalize_zzz(payload)
    assert status.bounties_state == "Locked" and status.hollow_zero_bounties is None
    assert (
        status.member_card.state == "Inactive"
        and status.member_card.completes_at is None
    )
