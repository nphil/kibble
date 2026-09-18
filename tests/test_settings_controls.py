"""Write-path, availability, and bounds tests for the batches of settings entities converted
from read-only sensors/binary_sensors into real controls: `switch.py`'s
`SwitchEntityDescription`s (`pet_detection`, `move_detection`, `eat_detection`,
`feed_picture`, `eat_video`, `food_warn`, `time_display`, `camera`, `light_mode`,
`tone_mode`, `sound_enable`, `feed_sound`, `system_sound_enable`, `smart_frame`),
`number.py`'s `SETTING_NUMBERS` (two genuinely continuous percentages, one cadence, and
`surplus_standard`) plus its hand-written `KibbleEatHoldNumber` (the `eat_sensitivity` wire
key, converted to/from a seconds hold time), and `select.py`'s `SETTING_SELECTS`
(`selected_sound`, `surplus_control`). Same duck-typed, `object.__new__`-constructed style as
`test_vomit_detection.py`/`test_vendor_only_config_availability.py`.

Deliberately does not re-test `KibbleSettingSwitch`/`KibbleSettingSelect`'s
`available`/generic mechanics, or the CONFIG-category/disabled-by-default rules, here -- those
are the same shared classes and the same `test_entity_platform_rules.py` parametrization
(which walks `switch.SWITCHES`/`number.SETTING_NUMBERS`/`select.SETTING_SELECTS`) that already
cover every entry regardless of which key is plugged in. What *is* new and worth pinning per
key: the exact `/config` key/value pair each control writes (hand-typed key strings are
exactly where a copy-paste typo would silently wire a control to the wrong setting), and --
for the two new selects -- the exact option-list content/order (a swapped pair of options
would silently send the wrong device value for a user's selection).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kibble.const import (
    MAX_DETECT_INTERVAL_S,
    MAX_SENSITIVITY,
    MAX_SURPLUS_STANDARD,
    MIN_DETECT_INTERVAL_S,
    MIN_SENSITIVITY,
    MIN_SURPLUS_STANDARD,
)
from kibble.number import (
    SETTING_NUMBERS,
    KibbleEatHoldNumber,
    KibbleSettingNumber,
    eat_hold_s_to_eat_sensitivity,
    eat_sensitivity_to_hold_s,
)
from kibble.select import SETTING_SELECTS, KibbleSettingSelect
from kibble.switch import SWITCHES, KibbleSettingSwitch

# The booleans converted from a read-only binary_sensor into a real switch, across every batch.
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
    "sound_enable",
    "feed_sound",
    "system_sound_enable",
    "smart_frame",
)

# The integers converted from a read-only sensor into a real number, across both batches, and
# each one's contract bounds -- the assignment's own Contract section, not invented here.
# `eat_sensitivity` is deliberately absent: it is no longer a data-driven `SETTING_NUMBERS`
# entry at all (see `KibbleEatHoldNumber`'s own tests below).
NEW_NUMBER_BOUNDS = {
    "pet_sensitivity": (MIN_SENSITIVITY, MAX_SENSITIVITY),
    "move_sensitivity": (MIN_SENSITIVITY, MAX_SENSITIVITY),
    "detect_interval": (MIN_DETECT_INTERVAL_S, MAX_DETECT_INTERVAL_S),
    "surplus_standard": (MIN_SURPLUS_STANDARD, MAX_SURPLUS_STANDARD),
}

# The integers converted from a read-only sensor into a real select (sound/surplus batch).
NEW_SELECT_KEYS = ("selected_sound", "surplus_control")


def test_every_new_switch_key_is_actually_registered() -> None:
    """Sanity check on the test data above, not the source."""
    assert set(NEW_SWITCH_KEYS) <= {d.key for d in SWITCHES}


def test_every_new_number_key_is_actually_registered() -> None:
    """Unlike the switch check above, this one is exact: `SETTING_NUMBERS` has no pre-existing
    members that aren't part of this batch."""
    assert set(NEW_NUMBER_BOUNDS) == {d.key for d in SETTING_NUMBERS}


def test_every_new_select_key_is_actually_registered() -> None:
    """Same exactness as the number check above: `SETTING_SELECTS` has no pre-existing members
    either."""
    assert set(NEW_SELECT_KEYS) == {d.key for d in SETTING_SELECTS}


# --- switch.KibbleSettingSwitch write path -------------------------------------------------


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


# --- number.KibbleSettingNumber write path + bounds -----------------------------------------


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
    the assignment's own contract bounds, and a write at the max bound sends exactly that
    integer."""
    description = next(d for d in SETTING_NUMBERS if d.key == key)
    assert (description.native_min_value, description.native_max_value) == bounds
    ent = _fake_number(description, config={key: bounds[0]})
    await ent.async_set_native_value(float(description.native_max_value))
    ent.coordinator.async_set_config.assert_awaited_once_with(key, int(description.native_max_value))


async def test_number_write_path_forwards_a_falsy_zero_value_unchanged() -> None:
    """`0` is a real, present value the device may need (e.g. `detect_interval`'s own
    "detection always on" reading), not a sentinel this write path may special-case away."""
    description = next(d for d in SETTING_NUMBERS if d.key == "detect_interval")
    ent = _fake_number(description, config={"detect_interval": 5})
    await ent.async_set_native_value(0.0)
    ent.coordinator.async_set_config.assert_awaited_once_with("detect_interval", 0)


@pytest.mark.parametrize("key", sorted(NEW_NUMBER_BOUNDS))
def test_number_is_available_and_reflects_the_config_value_when_the_key_is_present(key: str) -> None:
    description = next(d for d in SETTING_NUMBERS if d.key == key)
    ent = _fake_number(description, config={key: 7})
    assert ent.available is True
    assert ent.native_value == 7.0


def test_number_native_value_is_not_none_for_a_present_falsy_zero() -> None:
    """Only presence, not truthiness, gates `native_value`/`available`."""
    description = next(d for d in SETTING_NUMBERS if d.key == "detect_interval")
    ent = _fake_number(description, config={"detect_interval": 0})
    assert ent.available is True
    assert ent.native_value == 0.0


@pytest.mark.parametrize("key", sorted(NEW_NUMBER_BOUNDS))
def test_number_is_unavailable_when_its_key_is_absent_from_config(key: str) -> None:
    """The daemon slice that serves this key hasn't shipped -- absent, not present-and-zero."""
    description = next(d for d in SETTING_NUMBERS if d.key == key)
    ent = _fake_number(description, config={})
    assert ent.available is False
    assert ent.native_value is None


def test_number_is_unavailable_when_the_feeder_is_unreachable_even_with_a_present_key() -> None:
    description = next(d for d in SETTING_NUMBERS if d.key == "pet_sensitivity")
    ent = _fake_number(description, config={"pet_sensitivity": 50}, last_update_success=False)
    assert ent.available is False


# --- number.KibbleEatHoldNumber: eat_sensitivity <-> eat_hold_s conversion + write path ---------


def _fake_eat_hold(*, config: dict, last_update_success: bool = True) -> KibbleEatHoldNumber:
    ent = object.__new__(KibbleEatHoldNumber)
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(config=config),
        last_update_success=last_update_success,
        async_set_config=AsyncMock(),
    )
    return ent


@pytest.mark.parametrize(
    "hold_s, sensitivity",
    [(1, 100), (3, 78), (10, 0)],
)
def test_eat_sensitivity_and_hold_s_round_trip_at_the_documented_points(hold_s: int, sensitivity: int) -> None:
    """Mirrors the daemon's own `eat_sensitivity_to_hold_s`/`eat_hold_s_to_eat_sensitivity`
    round-trip tests exactly: 1<->100, 3<->78 (today's fixed default), 10<->0."""
    assert eat_sensitivity_to_hold_s(sensitivity) == hold_s
    assert eat_hold_s_to_eat_sensitivity(hold_s) == sensitivity


@pytest.mark.parametrize(
    "hold_s, sensitivity",
    [(1, 100), (3, 78), (10, 0)],
)
def test_eat_hold_number_native_value_reflects_the_config_sensitivity(hold_s: int, sensitivity: int) -> None:
    ent = _fake_eat_hold(config={"eat_sensitivity": sensitivity})
    assert ent.available is True
    assert ent.native_value == float(hold_s)


@pytest.mark.parametrize(
    "hold_s, sensitivity",
    [(1, 100), (3, 78), (10, 0)],
)
async def test_eat_hold_number_write_path_posts_the_exact_converted_sensitivity(hold_s: int, sensitivity: int) -> None:
    ent = _fake_eat_hold(config={"eat_sensitivity": 78})
    await ent.async_set_native_value(float(hold_s))
    ent.coordinator.async_set_config.assert_awaited_once_with("eat_sensitivity", sensitivity)


def test_eat_hold_number_is_unavailable_when_eat_sensitivity_is_absent_from_config() -> None:
    ent = _fake_eat_hold(config={})
    assert ent.available is False
    assert ent.native_value is None


def test_eat_hold_number_is_unavailable_when_the_feeder_is_unreachable_even_with_a_present_key() -> None:
    ent = _fake_eat_hold(config={"eat_sensitivity": 78}, last_update_success=False)
    assert ent.available is False


# --- select.KibbleSettingSelect write path + availability (selected_sound, surplus_control) -----


def _fake_select(description, *, config: dict, last_update_success: bool = True) -> SimpleNamespace:
    ent = object.__new__(KibbleSettingSelect)
    ent.entity_description = description
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(config=config),
        last_update_success=last_update_success,
        async_set_config=AsyncMock(),
    )
    return ent


@pytest.mark.parametrize("key", NEW_SELECT_KEYS)
async def test_select_option_writes_the_exact_index_for_the_chosen_option(key: str) -> None:
    """Selecting the option at index 1 must write exactly `(key, 1)` -- proves the
    option-string-to-index mapping, not just that some write happened."""
    description = next(d for d in SETTING_SELECTS if d.key == key)
    ent = _fake_select(description, config={key: 0})
    await ent.async_select_option(description.select_options[1])
    ent.coordinator.async_set_config.assert_awaited_once_with(key, 1)


@pytest.mark.parametrize("key", NEW_SELECT_KEYS)
def test_select_is_available_and_reflects_the_config_value_when_the_key_is_present(key: str) -> None:
    description = next(d for d in SETTING_SELECTS if d.key == key)
    ent = _fake_select(description, config={key: 0})
    assert ent.available is True
    assert ent.current_option == description.select_options[0]


@pytest.mark.parametrize("key", NEW_SELECT_KEYS)
def test_select_is_unavailable_when_its_key_is_absent_from_config(key: str) -> None:
    """The daemon slice that serves this key hasn't shipped -- absent, not present-and-zero."""
    description = next(d for d in SETTING_SELECTS if d.key == key)
    ent = _fake_select(description, config={})
    assert ent.available is False
    assert ent.current_option is None


@pytest.mark.parametrize("key", NEW_SELECT_KEYS)
def test_select_is_unavailable_when_the_persisted_value_is_out_of_range(key: str) -> None:
    """A daemon-side option-list shrink (or a hand-edited settings.json) must never index-error
    -- it must go unavailable instead."""
    description = next(d for d in SETTING_SELECTS if d.key == key)
    ent = _fake_select(description, config={key: 999})
    assert ent.available is False
    assert ent.current_option is None


def test_select_is_unavailable_when_the_feeder_is_unreachable_even_with_a_present_key() -> None:
    description = next(d for d in SETTING_SELECTS if d.key == "surplus_control")
    ent = _fake_select(description, config={"surplus_control": 0}, last_update_success=False)
    assert ent.available is False


def test_surplus_control_has_exactly_the_three_documented_modes_in_order() -> None:
    """Pins `surplus_control`'s option list content AND order against the assignment's own
    contract: index 0/1/2 must mean off/warn/skip, not any other order."""
    description = next(d for d in SETTING_SELECTS if d.key == "surplus_control")
    assert description.select_options == ("Off", "Warn only", "Skip feed")


def test_selected_sound_has_at_least_two_genuinely_distinct_options() -> None:
    """A select with fewer than two options is not a real choice."""
    description = next(d for d in SETTING_SELECTS if d.key == "selected_sound")
    assert len(description.select_options) >= 2
    assert len(set(description.select_options)) == len(description.select_options), "no two options may share a label"
