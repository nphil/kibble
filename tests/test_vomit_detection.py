"""Vomit-detection toggle (`switch.vomit_detection`) and diagnostic readout
(`binary_sensor.vomit_detected`): the switch's write path and availability when LibreFeed's
`/config` doesn't yet serve the key, and the diagnostic's [VOMIT_FRESH_WINDOW] boundary plus
unavailability when `GET /state` doesn't yet serve `vomit_detected_at`. Same duck-typed,
`object.__new__`-constructed style as `test_vendor_only_config_availability.py`/
`test_hopper_empty.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.util import dt as dt_util
from kibble.api import FeederState
from kibble.binary_sensor import VOMIT_FRESH_WINDOW, KibbleVomitDetectedBinarySensor, vomit_is_fresh
from kibble.switch import SWITCHES, KibbleSettingSwitch

VOMIT_SWITCH_DESCRIPTION = next(d for d in SWITCHES if d.key == "vomit_detection")

_NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


def _state(**overrides) -> FeederState:
    payload = {
        "serial": "S",
        "firmware": "895",
        "ble_firmware": 159,
        "volume": 9,
        "desiccant_days": 100,
        "feeding": False,
        "bowl_fill": None,
        "event_counter": 0,
    }
    payload.update(overrides)
    return FeederState.from_json(payload)


def _seconds_ago(seconds: int, *, now: datetime = _NOW) -> int:
    return int((now - timedelta(seconds=seconds)).timestamp())


def _real_seconds_ago(seconds: int) -> int:
    return int((datetime.now(timezone.utc) - timedelta(seconds=seconds)).timestamp())


# --- switch.KibbleSettingSwitch (vomit_detection) -----------------------------------------------


def _fake_switch(*, config: dict, last_update_success: bool = True) -> SimpleNamespace:
    ent = object.__new__(KibbleSettingSwitch)
    ent.entity_description = VOMIT_SWITCH_DESCRIPTION
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(config=config),
        last_update_success=last_update_success,
        async_set_config=AsyncMock(),
    )
    return ent


async def test_turn_on_writes_the_vomit_detection_key_through_async_set_config() -> None:
    ent = _fake_switch(config={"vomit_detection": 0})
    await ent.async_turn_on()
    ent.coordinator.async_set_config.assert_awaited_once_with("vomit_detection", 1)


async def test_turn_off_writes_the_vomit_detection_key_through_async_set_config() -> None:
    ent = _fake_switch(config={"vomit_detection": 1})
    await ent.async_turn_off()
    ent.coordinator.async_set_config.assert_awaited_once_with("vomit_detection", 0)


def test_switch_is_available_and_reflects_the_config_value_when_the_key_is_present() -> None:
    ent = _fake_switch(config={"vomit_detection": 1})
    assert ent.available is True
    assert ent.is_on is True


def test_switch_is_unavailable_when_vomit_detection_key_is_absent_from_config() -> None:
    """The daemon slice that serves this key hasn't shipped -- absent, not present-and-off."""
    ent = _fake_switch(config={"light": 1, "night": 0, "microphone": 1})
    assert ent.available is False
    assert ent.is_on is None


def test_switch_is_unavailable_when_the_feeder_is_unreachable_even_with_a_present_key() -> None:
    ent = _fake_switch(config={"vomit_detection": 1}, last_update_success=False)
    assert ent.available is False


# --- binary_sensor.vomit_is_fresh (free function, boundary) -------------------------------------


def test_vomit_is_fresh_true_just_inside_the_ten_minute_window() -> None:
    assert VOMIT_FRESH_WINDOW == timedelta(minutes=10)
    assert vomit_is_fresh(_seconds_ago(9 * 60 + 59), _NOW) is True


def test_vomit_is_fresh_false_just_outside_the_ten_minute_window() -> None:
    assert vomit_is_fresh(_seconds_ago(10 * 60 + 1), _NOW) is False


def test_vomit_is_fresh_false_when_never_detected() -> None:
    assert vomit_is_fresh(None, _NOW) is False


# --- binary_sensor.KibbleVomitDetectedBinarySensor -----------------------------------------------


def _fake_binary_sensor(*, state: FeederState, last_update_success: bool = True) -> SimpleNamespace:
    ent = object.__new__(KibbleVomitDetectedBinarySensor)
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(state=state), last_update_success=last_update_success
    )
    return ent


def test_binary_sensor_is_on_just_inside_the_freshness_window() -> None:
    state = _state(vomit_detected_at=_real_seconds_ago(9 * 60 + 59))
    ent = _fake_binary_sensor(state=state)
    assert ent.available is True
    assert ent.is_on is True


def test_binary_sensor_is_off_just_outside_the_freshness_window() -> None:
    state = _state(vomit_detected_at=_real_seconds_ago(10 * 60 + 1))
    ent = _fake_binary_sensor(state=state)
    assert ent.available is True
    assert ent.is_on is False


def test_binary_sensor_is_off_when_never_detected_but_still_available() -> None:
    """`vomit_detected_at` present-and-null -- a real "never yet" fact, not a missing field."""
    state = _state(vomit_detected_at=None)
    ent = _fake_binary_sensor(state=state)
    assert ent.available is True
    assert ent.is_on is False


def test_binary_sensor_is_unavailable_when_vomit_detected_at_is_absent_from_state() -> None:
    """The daemon slice that serves this field hasn't shipped -- absent, not never-detected."""
    state = _state()  # no vomit_detected_at key in the payload at all
    ent = _fake_binary_sensor(state=state)
    assert ent.available is False


def test_binary_sensor_is_unavailable_when_the_feeder_is_unreachable_even_with_a_present_key() -> None:
    state = _state(vomit_detected_at=_real_seconds_ago(60))
    ent = _fake_binary_sensor(state=state, last_update_success=False)
    assert ent.available is False


def test_binary_sensor_exposes_vomit_detected_at_as_an_iso_timestamp_attribute() -> None:
    ts = _real_seconds_ago(60)
    state = _state(vomit_detected_at=ts)
    ent = _fake_binary_sensor(state=state)
    assert ent.extra_state_attributes == {"vomit_detected_at": dt_util.utc_from_timestamp(ts).isoformat()}


def test_binary_sensor_exposes_none_attribute_when_never_detected() -> None:
    state = _state(vomit_detected_at=None)
    ent = _fake_binary_sensor(state=state)
    assert ent.extra_state_attributes == {"vomit_detected_at": None}
