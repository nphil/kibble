"""`bowl_fill` has two possible camera-measured sources and the distinction is load-bearing.

With the Petkit cloud disabled -- which is this project's whole point -- the vendor never
refreshes its own `BOWL_FILL_1` word, so it reads as invalid forever (kibble `docs/34`). kibbled
therefore computes its own estimate from the camera and reports it as `bowl_fill_local`, and this
sensor shows whichever reading actually exists. Both camera paths report `source: "measured"` --
the third possible source, `"estimate"`, is `bowl_fill.py`'s own post-feed projection and is
covered by `test_bowl_fill.py` instead.
"""

from __future__ import annotations

from types import SimpleNamespace

from kibble.api import FeederState
from kibble.sensor import KibbleBowlFillSensor


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


def _sensor(state: FeederState, estimate=None) -> KibbleBowlFillSensor:
    """Same `object.__new__` approach as the rest of this suite -- only what `native_value`/
    `extra_state_attributes` actually read is set by hand."""
    ent = object.__new__(KibbleBowlFillSensor)
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(state=state),
        bowl_fill_estimate=estimate,
        bowl_fill_per_portion=lambda bucket: (4.0, 0),
    )
    return ent


def test_the_vendors_own_reading_wins_when_it_exists() -> None:
    state = _state(bowl_fill=46, bowl_fill_local=[23, 1789638583], bowl_fill_local_frame_unix=1789636208)
    ent = _sensor(state)

    assert ent.native_value == 46
    attrs = ent.extra_state_attributes
    assert attrs["source"] == "measured"
    assert "measured_at" not in attrs


def test_kibbles_own_estimate_fills_in_when_the_vendor_has_none() -> None:
    """The cloud-disabled steady state: without this fallback the entity is permanently unknown."""
    state = _state(bowl_fill=None, bowl_fill_local=[23, 1789638583], bowl_fill_local_frame_unix=1789636208)
    ent = _sensor(state)

    assert ent.native_value == 23
    attrs = ent.extra_state_attributes
    assert attrs["source"] == "measured"
    # The frame's own time, not when the score was computed: it says when the bowl looked like
    # that, which is the only honest caption for a camera estimate of a bowl nobody has visited.
    assert attrs["measured_at"] == "2026-09-17T09:10:08+00:00"


def test_unknown_stays_unknown_with_neither_reading() -> None:
    state = _state()
    ent = _sensor(state)

    assert ent.native_value is None
    assert ent.extra_state_attributes["source"] == "measured"


def test_an_active_estimate_overrides_the_raw_reading_and_reports_its_own_source() -> None:
    """While a post-feed projection is outstanding, it -- not whatever the camera currently
    reports -- is what the entity shows, tagged so the card can say "about N%"."""
    state = _state(bowl_fill=46)
    ent = _sensor(state, estimate=(58.4, {"fill_per_portion": [4.0, 4.0], "samples": [1, 0]}))

    assert ent.native_value == 58
    attrs = ent.extra_state_attributes
    assert attrs["source"] == "estimate"
    assert attrs["fill_per_portion"] == [4.0, 4.0]


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
