"""`bowl_fill` has two possible sources and the distinction is load-bearing.

With the Petkit cloud disabled -- which is this project's whole point -- the vendor never
refreshes its own `BOWL_FILL_1` word, so it reads as invalid forever (kibble `docs/34`). kibbled
therefore computes its own estimate from the camera and reports it as `bowl_fill_local`, and this
sensor shows whichever reading actually exists, saying which one it is.
"""

from __future__ import annotations

from kibble.api import FeederState
from kibble.sensor import SENSORS

BOWL_FILL = next(d for d in SENSORS if d.key == "bowl_fill")


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


def test_the_vendors_own_reading_wins_when_it_exists() -> None:
    state = _state(bowl_fill=46, bowl_fill_local=[23, 1789638583], bowl_fill_local_frame_unix=1789636208)

    assert BOWL_FILL.value(state) == 46
    assert BOWL_FILL.attributes(state) == {"source": "feeder"}


def test_kibbles_own_estimate_fills_in_when_the_vendor_has_none() -> None:
    """The cloud-disabled steady state: without this fallback the entity is permanently unknown."""
    state = _state(bowl_fill=None, bowl_fill_local=[23, 1789638583], bowl_fill_local_frame_unix=1789636208)

    assert BOWL_FILL.value(state) == 23
    attrs = BOWL_FILL.attributes(state)
    assert attrs["source"] == "kibble"
    # The frame's own time, not when the score was computed: it says when the bowl looked like
    # that, which is the only honest caption for a camera estimate of a bowl nobody has visited.
    assert attrs["measured_at"] == "2026-09-17T09:10:08+00:00"


def test_unknown_stays_unknown_with_neither_reading() -> None:
    state = _state()

    assert BOWL_FILL.value(state) is None
    assert BOWL_FILL.attributes(state)["source"] == "kibble"


# --- `bowl_empty`: the verdict an automation may act on -----------------------------------


def test_bowl_empty_is_unknown_when_the_feeder_has_not_reported_it() -> None:
    """The safety property. A feeder that has not taken an unobstructed reading (or predates
    the field entirely) must leave this unknown, because the automation on the other end
    dispenses food -- and `bowl_fill` being a small number is NOT the same fact: an empty bowl
    measures 0-8 on this device, so a naive threshold and this verdict disagree precisely in
    the band where it matters."""
    from kibble.binary_sensor import KibbleBowlEmptySensor
    from types import SimpleNamespace

    state = _state(bowl_fill=5)
    assert state.bowl_empty is None
    ent = object.__new__(KibbleBowlEmptySensor)
    ent.coordinator = SimpleNamespace(last_update_success=True, data=SimpleNamespace(state=state))
    assert ent.is_on is None
    assert ent.available is False


def test_bowl_empty_reports_the_feeders_verdict_and_carries_the_raw_score() -> None:
    from kibble.binary_sensor import KibbleBowlEmptySensor
    from types import SimpleNamespace

    state = _state(bowl_fill=5, bowl_empty=True, bowl_occluded=False)
    ent = object.__new__(KibbleBowlEmptySensor)
    ent.coordinator = SimpleNamespace(last_update_success=True, data=SimpleNamespace(state=state))
    assert ent.available is True
    assert ent.is_on is True
    # The raw score rides along as an attribute so the thresholds stay auditable from HA
    # without re-reading the daemon.
    assert ent.extra_state_attributes == {"occluded": False, "fill_score": 5}
