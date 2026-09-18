"""Write-path, availability, and bounds tests for the batch of settings entities converted
from read-only sensors/binary_sensors into real controls: `switch.py`'s ten new
`SwitchEntityDescription`s (`pet_detection`, `move_detection`, `eat_detection`,
`feed_picture`, `eat_video`, `food_warn`, `time_display`, `camera`, `light_mode`,
`tone_mode`) and `number.py`'s ten `SETTING_NUMBERS` (three sensitivities, one cadence, six
schedule-pair minutes). Same duck-typed, `object.__new__`-constructed style as
`test_vomit_detection.py`/`test_vendor_only_config_availability.py`.

Deliberately does not re-test `KibbleSettingSwitch.available`/`is_on`/generic toggle mechanics,
or the CONFIG-category/disabled-by-default rules, here -- those are the same shared class and
the same `test_entity_platform_rules.py` parametrization (which now also walks
`number.SETTING_NUMBERS`) that already cover every entry in `SWITCHES`/`SETTING_NUMBERS`
regardless of which key is plugged in. What *is* new and worth pinning per key: the exact
`/config` key/value pair each control writes (twenty hand-typed key strings are exactly where
a copy-paste typo would silently wire a control to the wrong setting) and the two behaviours
`KibbleSettingNumber` itself introduces (the HH:MM attribute, and unit-of-work availability).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kibble.const import (
    MAX_DETECT_INTERVAL_S,
    MAX_MINUTES_OF_DAY,
    MAX_SENSITIVITY,
    MIN_DETECT_INTERVAL_S,
    MIN_MINUTES_OF_DAY,
    MIN_SENSITIVITY,
)
from kibble.number import SETTING_NUMBERS, KibbleSettingNumber, _minutes_to_hhmm
from kibble.switch import SWITCHES, KibbleSettingSwitch

# The ten booleans this batch turned from a read-only binary_sensor into a real switch.
NEW_SWITCH_KEYS = (
    "pet_detection",
    "move_detection",
    "eat_detection",
    "feed_picture",
    "eat_video",
    "food_warn",
    "time_display",
    "camera",
    "light_mode",
    "tone_mode",
)

# The ten integers this batch turned from a read-only sensor into a real number, and each
# one's contract bounds -- the assignment's own Contract section, not invented here.
NEW_NUMBER_BOUNDS = {
    "pet_sensitivity": (MIN_SENSITIVITY, MAX_SENSITIVITY),
    "move_sensitivity": (MIN_SENSITIVITY, MAX_SENSITIVITY),
    "eat_sensitivity": (MIN_SENSITIVITY, MAX_SENSITIVITY),
    "detect_interval": (MIN_DETECT_INTERVAL_S, MAX_DETECT_INTERVAL_S),
    "detect_range_from": (MIN_MINUTES_OF_DAY, MAX_MINUTES_OF_DAY),
    "detect_range_till": (MIN_MINUTES_OF_DAY, MAX_MINUTES_OF_DAY),
    "light_range_from": (MIN_MINUTES_OF_DAY, MAX_MINUTES_OF_DAY),
    "light_range_till": (MIN_MINUTES_OF_DAY, MAX_MINUTES_OF_DAY),
    "tone_range_from": (MIN_MINUTES_OF_DAY, MAX_MINUTES_OF_DAY),
    "tone_range_till": (MIN_MINUTES_OF_DAY, MAX_MINUTES_OF_DAY),
}

# The six minutes-since-midnight pairs that render an HH:MM attribute; the three sensitivities
# plus detect_interval are plain magnitudes and must not.
RANGE_NUMBER_KEYS = (
    "detect_range_from",
    "detect_range_till",
    "light_range_from",
    "light_range_till",
    "tone_range_from",
    "tone_range_till",
)


def test_every_new_switch_key_is_actually_registered() -> None:
    """Sanity check on the test data above, not the source."""
    assert set(NEW_SWITCH_KEYS) <= {d.key for d in SWITCHES}


def test_every_new_number_key_is_actually_registered() -> None:
    """Unlike the switch check above, this one is exact: `SETTING_NUMBERS` has no pre-existing
    members from an earlier batch, so a number added there without a matching entry here
    should fail loudly instead of silently going untested."""
    assert set(NEW_NUMBER_BOUNDS) == {d.key for d in SETTING_NUMBERS}


# --- switch.KibbleSettingSwitch write path (the ten new booleans) -------------------------------


def _fake_switch(description, *, config: dict) -> SimpleNamespace:
    ent = object.__new__(KibbleSettingSwitch)
    ent.entity_description = description
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(config=config), last_update_success=True, async_set_config=AsyncMock()
    )
    return ent


@pytest.mark.parametrize("key", NEW_SWITCH_KEYS)
async def test_turn_on_writes_the_exact_key_with_value_1(key: str) -> None:
    description = next(d for d in SWITCHES if d.key == key)
    ent = _fake_switch(description, config={key: 0})
    await ent.async_turn_on()
    ent.coordinator.async_set_config.assert_awaited_once_with(key, 1)


@pytest.mark.parametrize("key", NEW_SWITCH_KEYS)
async def test_turn_off_writes_the_exact_key_with_value_0(key: str) -> None:
    description = next(d for d in SWITCHES if d.key == key)
    ent = _fake_switch(description, config={key: 1})
    await ent.async_turn_off()
    ent.coordinator.async_set_config.assert_awaited_once_with(key, 0)


# --- number.KibbleSettingNumber write path + bounds (the ten new settings numbers) --------------


def _fake_number(description, *, config: dict, last_update_success: bool = True) -> SimpleNamespace:
    ent = object.__new__(KibbleSettingNumber)
    ent.entity_description = description
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(config=config),
        last_update_success=last_update_success,
        async_set_config=AsyncMock(),
    )
    return ent


@pytest.mark.parametrize("key, bounds", sorted(NEW_NUMBER_BOUNDS.items()))
async def test_number_write_path_honours_its_own_contract_bounds(key: str, bounds: tuple[int, int]) -> None:
    """One test per new number control: its `native_min_value`/`native_max_value` are exactly
    the contract's bounds -- the same two values HA's own `number.set_value` service handler
    (`homeassistant.components.number.async_set_value`) reads to reject an out-of-range write,
    so pinning them here is pinning the enforcement itself -- and writing at that upper bound
    reaches `/config` as the exact `(key, value)` pair the device expects."""
    description = next(d for d in SETTING_NUMBERS if d.key == key)
    assert (description.native_min_value, description.native_max_value) == bounds

    ent = _fake_number(description, config={key: 1})
    await ent.async_set_native_value(description.native_max_value)
    ent.coordinator.async_set_config.assert_awaited_once_with(key, int(description.native_max_value))


async def test_number_write_path_forwards_a_falsy_zero_value_unchanged() -> None:
    """`0` is a real, present value the device may need (e.g. `detect_interval`'s own
    minimum) -- distinct from `None`. Guards against an `if value:` bug in
    `async_set_native_value` that would silently drop a zero write."""
    description = next(d for d in SETTING_NUMBERS if d.key == "detect_interval")
    ent = _fake_number(description, config={"detect_interval": 5})
    await ent.async_set_native_value(0)
    ent.coordinator.async_set_config.assert_awaited_once_with("detect_interval", 0)


@pytest.mark.parametrize("key", sorted(NEW_NUMBER_BOUNDS))
def test_number_is_available_and_reflects_the_config_value_when_the_key_is_present(key: str) -> None:
    description = next(d for d in SETTING_NUMBERS if d.key == key)
    ent = _fake_number(description, config={key: 7})
    assert ent.available is True
    assert ent.native_value == 7.0


def test_number_native_value_is_not_none_for_a_present_falsy_zero() -> None:
    """Only presence, not truthiness, gates `native_value`/`available` -- mirrors
    `test_vendor_only_config_availability.py`'s equivalent check for `KibbleSettingSensor`."""
    description = next(d for d in SETTING_NUMBERS if d.key == "detect_interval")
    ent = _fake_number(description, config={"detect_interval": 0})
    assert ent.available is True
    assert ent.native_value == 0.0


@pytest.mark.parametrize("key", sorted(NEW_NUMBER_BOUNDS))
def test_number_is_unavailable_when_its_key_is_absent_from_config(key: str) -> None:
    """The daemon slice that serves this key hasn't shipped -- absent, not present-and-zero."""
    description = next(d for d in SETTING_NUMBERS if d.key == key)
    ent = _fake_number(description, config={"light": 1})
    assert ent.available is False
    assert ent.native_value is None


def test_number_is_unavailable_when_the_feeder_is_unreachable_even_with_a_present_key() -> None:
    description = next(d for d in SETTING_NUMBERS if d.key == "pet_sensitivity")
    ent = _fake_number(description, config={"pet_sensitivity": 50}, last_update_success=False)
    assert ent.available is False


# --- number._minutes_to_hhmm (free function, boundary) + the range numbers' HH:MM attribute -----


def test_minutes_to_hhmm_at_midnight() -> None:
    assert _minutes_to_hhmm(0) == "00:00"


def test_minutes_to_hhmm_mid_morning() -> None:
    assert _minutes_to_hhmm(645) == "10:45"


def test_minutes_to_hhmm_last_minute_of_the_day() -> None:
    assert _minutes_to_hhmm(MAX_MINUTES_OF_DAY) == "23:59"


@pytest.mark.parametrize("key", RANGE_NUMBER_KEYS)
def test_range_number_exposes_the_hhmm_attribute(key: str) -> None:
    description = next(d for d in SETTING_NUMBERS if d.key == key)
    ent = _fake_number(description, config={key: 90})
    assert ent.extra_state_attributes == {"time": "01:30"}


@pytest.mark.parametrize("key", RANGE_NUMBER_KEYS)
def test_range_number_hhmm_attribute_is_none_when_the_key_is_absent(key: str) -> None:
    description = next(d for d in SETTING_NUMBERS if d.key == key)
    ent = _fake_number(description, config={})
    assert ent.extra_state_attributes == {"time": None}


@pytest.mark.parametrize("key", ("pet_sensitivity", "move_sensitivity", "eat_sensitivity", "detect_interval"))
def test_plain_magnitude_number_has_no_hhmm_attribute(key: str) -> None:
    description = next(d for d in SETTING_NUMBERS if d.key == key)
    ent = _fake_number(description, config={key: 50})
    assert ent.extra_state_attributes is None


def test_from_equal_to_till_is_a_valid_combination_meaning_always_active() -> None:
    """The contract's own semantics for a schedule pair: `from == till` means "always active",
    so nothing on the entity side should reject or special-case it."""
    from_desc = next(d for d in SETTING_NUMBERS if d.key == "light_range_from")
    till_desc = next(d for d in SETTING_NUMBERS if d.key == "light_range_till")
    ent_from = _fake_number(from_desc, config={"light_range_from": 480, "light_range_till": 480})
    ent_till = _fake_number(till_desc, config={"light_range_from": 480, "light_range_till": 480})
    assert ent_from.available is True
    assert ent_till.available is True
    assert ent_from.native_value == ent_till.native_value == 480.0
