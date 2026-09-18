"""HA-side logic for the three hour-range `text` entities (`detection_hours`,
`status_led_hours`, `do_not_disturb_hours`) that replaced six `number` entities -- see
`text.py`'s own module docstring. Same duck-typed, `object.__new__`-constructed style as
`test_settings_controls.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kibble.text import HOUR_RANGES, KibbleHourRangeText, format_hour_range, parse_hour_range


def _fake_text(description, *, config: dict, last_update_success: bool = True) -> KibbleHourRangeText:
    ent = object.__new__(KibbleHourRangeText)
    ent.entity_description = description
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(config=config),
        last_update_success=last_update_success,
        async_set_config=AsyncMock(),
    )
    return ent


# --- (1) format/parse round-trip, including midnight-crossing and equal start/end -------------


@pytest.mark.parametrize(
    "from_minutes, till_minutes, text",
    [
        (0, 0, "00:00-00:00"),  # equal start/end -- "always active", shown honestly, not a magic word
        (1320, 360, "22:00-06:00"),  # crosses midnight -- legitimate, not rejected (vendor's own toneMultiRange example)
        (480, 480, "08:00-08:00"),
    ],
)
def test_format_and_parse_round_trip(from_minutes: int, till_minutes: int, text: str) -> None:
    assert format_hour_range(from_minutes, till_minutes) == text
    assert parse_hour_range(text) == (from_minutes, till_minutes)


def test_parse_rejects_a_malformed_value() -> None:
    for bad in ("25:00-07:00", "7:00-07:00", "22:00-07:00extra", "garbage", ""):
        with pytest.raises(ValueError):
            parse_hour_range(bad)


# --- (2) each entity's exact pair of /config writes --------------------------------------------


@pytest.mark.parametrize("description", HOUR_RANGES, ids=lambda d: d.key)
async def test_set_value_writes_both_config_keys_in_order(description) -> None:
    ent = _fake_text(description, config={description.from_key: 0, description.till_key: 0})
    await ent.async_set_value("22:00-07:00")
    assert ent.coordinator.async_set_config.await_args_list == [
        ((description.from_key, 1320),),
        ((description.till_key, 420),),
    ]


# --- (3) availability: unavailable when either backing key is missing --------------------------


@pytest.mark.parametrize("description", HOUR_RANGES, ids=lambda d: d.key)
def test_available_when_both_keys_present(description) -> None:
    ent = _fake_text(description, config={description.from_key: 480, description.till_key: 1320})
    assert ent.available is True
    assert ent.native_value == "08:00-22:00"
    assert ent.extra_state_attributes == {"start_minute": 480, "end_minute": 1320}


@pytest.mark.parametrize("description", HOUR_RANGES, ids=lambda d: d.key)
def test_unavailable_when_the_till_key_is_missing(description) -> None:
    ent = _fake_text(description, config={description.from_key: 480})
    assert ent.available is False
    assert ent.native_value is None


@pytest.mark.parametrize("description", HOUR_RANGES, ids=lambda d: d.key)
def test_unavailable_when_the_from_key_is_missing(description) -> None:
    ent = _fake_text(description, config={description.till_key: 1320})
    assert ent.available is False


def test_every_hour_range_key_pair_is_registered_exactly_once() -> None:
    assert {d.key for d in HOUR_RANGES} == {
        "detection_hours",
        "status_led_hours",
        "do_not_disturb_hours",
    }
    assert {d.from_key for d in HOUR_RANGES} == {
        "detect_range_from",
        "light_range_from",
        "tone_range_from",
    }
    assert {d.till_key for d in HOUR_RANGES} == {
        "detect_range_till",
        "light_range_till",
        "tone_range_till",
    }
