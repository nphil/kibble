"""Per-hopper "food ran out" flags -- kibble docs/07-config.md.

`GET /state`'s `hopper_empty` mirrors the vendor's own low-food threshold: `ctrl`'s tone-alarm
gate and `ble`'s own warning-flag setter/clearer both treat a hopper's raw 0/1/2 level as a
problem below 2, not just at a literal 0 (`agent/src/state.rs::off::FOOD_1`/`FOOD_2`,
disassembly-proven against two independent vendor code paths). This covers both the API layer
(`FeederState.from_json`) and the entity layer (`KibbleHopperEmptySensor`, which just indexes
into it) -- the same split as `test_bowl_fill_source.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from kibble.api import FeederState
from kibble.binary_sensor import HOPPER_EMPTY_SENSORS, KibbleHopperEmptySensor

HOPPER_1_EMPTY = next(d for d in HOPPER_EMPTY_SENSORS if d.key == "hopper_1_empty")
HOPPER_2_EMPTY = next(d for d in HOPPER_EMPTY_SENSORS if d.key == "hopper_2_empty")


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


def _sensor(state: FeederState, description) -> SimpleNamespace:
    """A fake entity carrying just what `KibbleHopperEmptySensor.is_on` reads -- same pattern
    `test_cat_id.py` uses for `KibbleCatPresentBinarySensor`, avoiding a full coordinator/
    config-entry fixture for a property that only ever touches `coordinator.data.state`."""
    return SimpleNamespace(
        entity_description=description,
        coordinator=SimpleNamespace(data=SimpleNamespace(state=state)),
    )


def test_from_json_parses_both_hoppers() -> None:
    state = _state(hopper_empty=[False, True])
    assert state.hopper_empty == (False, True)


def test_from_json_defaults_to_unknown_when_the_key_is_missing() -> None:
    """The steady state on a payload from an agent build that predates this field -- must not
    be mistaken for "not empty"."""
    state = _state()
    assert state.hopper_empty == (None, None)


def test_from_json_preserves_a_null_per_hopper() -> None:
    """The boot-time "never reported yet" sentinel round-trips as `None`, not `False`."""
    state = _state(hopper_empty=[None, False])
    assert state.hopper_empty == (None, False)


def test_hopper_1_sensor_reads_its_own_slot_not_the_other_hoppers() -> None:
    state = _state(hopper_empty=[True, False])
    assert KibbleHopperEmptySensor.is_on.fget(_sensor(state, HOPPER_1_EMPTY)) is True


def test_hopper_2_sensor_reads_its_own_slot_not_the_other_hoppers() -> None:
    state = _state(hopper_empty=[True, False])
    assert KibbleHopperEmptySensor.is_on.fget(_sensor(state, HOPPER_2_EMPTY)) is False


def test_sensor_is_unknown_while_the_hopper_has_never_reported() -> None:
    state = _state(hopper_empty=[None, None])
    assert KibbleHopperEmptySensor.is_on.fget(_sensor(state, HOPPER_1_EMPTY)) is None


def test_descriptions_are_device_class_problem_with_on_meaning_empty() -> None:
    """The dashboard's default problem-sensor styling (and the assignment this shipped under)
    both depend on `on` meaning "needs attention" -- a regression here silently inverts every
    hopper's alert."""
    assert HOPPER_1_EMPTY.device_class == BinarySensorDeviceClass.PROBLEM
    assert HOPPER_2_EMPTY.device_class == BinarySensorDeviceClass.PROBLEM
