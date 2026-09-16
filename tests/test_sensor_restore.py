"""`sensor.KibbleVendorLastSeenPetSensor`'s `RestoreEntity` behaviour: `track` events live only
in the agent's in-memory ring (`ai.rs`'s `Feed`), so `vendor_sightings` is empty on every fresh
`kibbled` restart -- this sensor must restore its last known state/attributes across that gap
and keep showing them until a genuinely newer sighting arrives, and must still go unavailable
when the *feeder* itself is unreachable. Same duck-typed style as the rest of this suite: a
bare, `object.__new__`-constructed entity with only what each property reads set by hand, and
`async_get_last_state` replaced directly rather than exercising `RestoreEntity`'s own storage
machinery, which needs a real `HomeAssistant` core instance this repo has no fixture for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from kibble.coordinator import VendorSighting
from kibble.sensor import KibbleVendorLastSeenPetSensor


def _bare_sensor(vendor_sightings=(), *, last_update_success: bool = True) -> KibbleVendorLastSeenPetSensor:
    ent = object.__new__(KibbleVendorLastSeenPetSensor)
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(vendor_sightings=vendor_sightings),
        last_update_success=last_update_success,
    )
    ent._restored_value = None
    ent._restored_attrs = {}
    return ent


def _last_state(state: str, attributes: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(state=state, attributes=attributes or {})


async def _added_to_hass(ent: KibbleVendorLastSeenPetSensor, last_state) -> None:
    """Runs exactly `KibbleVendorLastSeenPetSensor.async_added_to_hass`'s own restore logic,
    without the `CoordinatorEntity`/`RestoreEntity` base-class chain a bare instance has no
    working `super()` for."""

    async def fake_last_state():
        return last_state

    ent.async_get_last_state = fake_last_state
    if ent._latest() is not None:
        return
    state = await ent.async_get_last_state()
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return
    ent._restored_value = state.state
    ent._restored_attrs = {
        key: value for key, value in state.attributes.items() if key in ("pet_id", "last_identified", "total_score")
    }


async def test_restores_last_known_sighting_after_a_restart_with_no_live_data() -> None:
    ent = _bare_sensor(vendor_sightings=())
    await _added_to_hass(
        ent,
        _last_state(
            "Kitty",
            {"pet_id": "101320712", "last_identified": "2026-09-15T12:00:00+00:00", "total_score": 1531.2, "unrelated": "x"},
        ),
    )
    assert ent.available is True
    assert ent.native_value == "Kitty"
    assert ent.extra_state_attributes == {
        "pet_id": "101320712",
        "last_identified": "2026-09-15T12:00:00+00:00",
        "total_score": 1531.2,
    }


@pytest.mark.parametrize("state", [STATE_UNAVAILABLE, STATE_UNKNOWN])
async def test_does_not_restore_from_an_unavailable_or_unknown_last_state(state: str) -> None:
    ent = _bare_sensor(vendor_sightings=())
    await _added_to_hass(ent, _last_state(state))
    assert ent.available is False
    assert ent.native_value is None


async def test_no_prior_state_and_no_live_sighting_is_genuinely_unavailable() -> None:
    ent = _bare_sensor(vendor_sightings=())
    await _added_to_hass(ent, None)
    assert ent.available is False


async def test_a_newer_live_sighting_wins_over_the_restored_value() -> None:
    ent = _bare_sensor(vendor_sightings=())
    await _added_to_hass(ent, _last_state("Kitty", {"pet_id": "101320712"}))
    assert ent.native_value == "Kitty"  # sanity: restore took effect first

    new_sighting = VendorSighting(ts=1789580500, pet_id="5", cat="Pancake", total_score=42.0)
    ent.coordinator.data = SimpleNamespace(vendor_sightings=(new_sighting,))
    assert ent.native_value == "Pancake"
    assert ent.extra_state_attributes["pet_id"] == "5"
    assert ent.extra_state_attributes["total_score"] == 42.0


async def test_a_live_sighting_already_present_at_startup_skips_the_restore_entirely() -> None:
    sighting = VendorSighting(ts=1, pet_id="5", cat="Pancake", total_score=None)
    ent = _bare_sensor(vendor_sightings=(sighting,))
    await _added_to_hass(ent, _last_state("Kitty", {"pet_id": "101320712"}))
    assert ent.native_value == "Pancake"
    assert ent._restored_value is None  # never even looked at the stored state


def test_an_unreachable_feeder_overrides_even_a_restored_value() -> None:
    ent = _bare_sensor(vendor_sightings=(), last_update_success=False)
    ent._restored_value = "Kitty"
    ent._restored_attrs = {"pet_id": "101320712"}
    assert ent.available is False
