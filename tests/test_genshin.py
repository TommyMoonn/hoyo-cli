"""Sanitized upstream-shaped notes are normalized without extra API calls."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import genshin
import pytest

from hoyo_cli.errors import UnsupportedDataError
from hoyo_cli.hoyolab.adapters.genshin import normalize_genshin

NOW = datetime(2026, 10, 10, 11, 31, tzinfo=UTC)


@pytest.fixture
def notes_payload() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((Path(__file__).parent / "fixtures/genshin_notes.json").read_text()),
    )


def test_genshin_compact_fields(notes_payload: dict[str, object]) -> None:
    status = normalize_genshin(notes_payload, now=NOW)
    assert (status.resin.current, status.resin.maximum) == (190, 200)
    assert status.resin.full_at == NOW + timedelta(seconds=4560)
    assert status.realm_currency is not None
    assert status.realm_currency.current == 930
    assert status.realm_currency.full_at == NOW + timedelta(seconds=115200)
    assert status.commissions.current == 4
    assert status.commission_reward_claimed
    assert status.weekly_discounts.current == 2


def test_genshin_expedition_details(notes_payload: dict[str, object]) -> None:
    status = normalize_genshin(notes_payload, now=NOW)
    assert len(status.expeditions) == 2
    assert status.expeditions[0].state == "Finished"
    assert status.expeditions[0].completes_at is None
    assert status.expeditions[1].completes_at == NOW + timedelta(seconds=8040)
    assert status.expeditions[1].label == "Expedition 2"
    assert "https://" not in repr(status)


@pytest.mark.parametrize("typed", [False, True])
def test_full_resin_currency_and_ready_transformer(
    notes_payload: dict[str, object],
    typed: bool,
) -> None:
    notes_payload.update(
        current_resin=200,
        current_home_coin=2400,
        resin_recovery_time="0",
        home_coin_recovery_time="0",
    )
    notes_payload["transformer"] = {
        "obtained": True,
        "recovery_time": {"Day": 0, "Hour": 0, "Minute": 0, "Second": 0},
    }
    upstream = (
        genshin.models.Notes.model_validate(notes_payload) if typed else notes_payload
    )
    status = normalize_genshin(upstream, now=NOW)
    assert status.resin.current == status.resin.maximum == 200
    assert status.resin.full_at is None
    assert status.realm_currency is not None
    assert status.realm_currency.current == status.realm_currency.maximum == 2400
    assert status.realm_currency.full_at is None
    assert status.transformer.state == "Ready"
    assert status.transformer.completes_at is None


@pytest.mark.parametrize("state, remaining", [("Ongoing", "0"), ("Finished", "60")])
def test_completed_expedition_has_no_inferred_completion_time(
    notes_payload: dict[str, object], state: str, remaining: str
) -> None:
    notes_payload["expeditions"] = [{"status": state, "remained_time": remaining}]
    activity = normalize_genshin(notes_payload, now=NOW).expeditions[0]
    assert activity.state == "Finished"
    assert activity.completes_at is None


def test_zero_recovery_preserves_nonfull_captured_amounts(
    notes_payload: dict[str, object],
) -> None:
    notes_payload.update(resin_recovery_time="0", home_coin_recovery_time="0")
    status = normalize_genshin(notes_payload, now=NOW)
    assert status.resin.current == 190 and status.resin.full_at is None
    assert status.realm_currency is not None
    assert status.realm_currency.current == 930
    assert status.realm_currency.full_at is None


def test_full_resources_do_not_infer_deadlines_from_inconsistent_countdowns(
    notes_payload: dict[str, object],
) -> None:
    notes_payload.update(current_resin=200, current_home_coin=2400)
    status = normalize_genshin(notes_payload, now=NOW)
    assert status.resin.current == 200 and status.resin.full_at is None
    assert status.realm_currency is not None
    assert status.realm_currency.current == 2400
    assert status.realm_currency.full_at is None


def test_transformer_cooldown_and_supported_details(
    notes_payload: dict[str, object],
) -> None:
    status = normalize_genshin(notes_payload, now=NOW)
    assert status.transformer.completes_at == NOW + timedelta(days=2, hours=4)
    assert status.daily_tasks is not None
    assert status.daily_tasks.reward_states == ("Claimed",)
    assert status.daily_tasks.stored_encounter_points == 12.5
    assert status.archon_quests is not None
    assert status.archon_quests.chapters == ("Chapter I: Test chapter",)


def test_missing_optional_detail_is_safe(notes_payload: dict[str, object]) -> None:
    for name in ("daily_task", "archon_quest_progress", "transformer"):
        del notes_payload[name]
    status = normalize_genshin(notes_payload, now=NOW)
    assert status.daily_tasks is None and status.archon_quests is None
    assert status.transformer.state == "Unavailable"


@pytest.mark.parametrize("name", ["daily_task", "archon_quest_progress"])
def test_unsupported_optional_detail_does_not_hide_daily_status(
    notes_payload: dict[str, object], name: str
) -> None:
    notes_payload[name] = {"changed": "test-secret-123"}
    status = normalize_genshin(notes_payload, now=NOW)
    assert status.resin.current == 190
    if name == "daily_task":
        assert status.daily_tasks is None
    else:
        assert status.archon_quests is None


def test_locked_transformer_and_realm_are_not_api_errors(
    notes_payload: dict[str, object],
) -> None:
    notes_payload["transformer"] = {"obtained": False}
    notes_payload.update(max_home_coin=0, current_home_coin=0)
    status = normalize_genshin(notes_payload, now=NOW)
    assert status.transformer.state == "Locked"
    assert status.realm_currency is None


def test_pinned_notes_model_is_also_accepted(notes_payload: dict[str, object]) -> None:
    notes = genshin.models.Notes.model_validate(notes_payload)
    assert normalize_genshin(notes, now=NOW) == normalize_genshin(
        notes_payload, now=NOW
    )


@pytest.mark.parametrize("value", [True, -1, "test-secret-123", None])
def test_changed_required_response_is_classified_without_raw_data(
    notes_payload: dict[str, object], value: object
) -> None:
    notes_payload["current_resin"] = value
    with pytest.raises(UnsupportedDataError) as caught:
        normalize_genshin(notes_payload, now=NOW)
    assert "test-secret-123" not in str(caught.value)
