"""`bowl_fill_1` has two possible sources and the distinction is load-bearing.

With the Petkit cloud disabled -- which is this project's whole point -- the vendor never
refreshes its own `BOWL_FILL_1` word, so it reads as invalid forever (kibble `docs/34`). kibbled
therefore computes its own estimate from the camera and reports it as `bowl_fill_local`, and this
sensor shows whichever reading actually exists, saying which one it is.
"""

from __future__ import annotations

from kibble.api import FeederState
from kibble.sensor import SENSORS

BOWL_FILL_1 = next(d for d in SENSORS if d.key == "bowl_fill_1")


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

    assert BOWL_FILL_1.value(state) == 46
    assert BOWL_FILL_1.attributes(state) == {"source": "feeder"}


def test_kibbles_own_estimate_fills_in_when_the_vendor_has_none() -> None:
    """The cloud-disabled steady state: without this fallback the entity is permanently unknown."""
    state = _state(bowl_fill=None, bowl_fill_local=[23, 1789638583], bowl_fill_local_frame_unix=1789636208)

    assert BOWL_FILL_1.value(state) == 23
    attrs = BOWL_FILL_1.attributes(state)
    assert attrs["source"] == "kibble"
    # The frame's own time, not when the score was computed: it says when the bowl looked like
    # that, which is the only honest caption for a camera estimate of a bowl nobody has visited.
    assert attrs["measured_at"] == "2026-09-17T09:10:08+00:00"


def test_unknown_stays_unknown_with_neither_reading() -> None:
    state = _state()

    assert BOWL_FILL_1.value(state) is None
    assert BOWL_FILL_1.attributes(state)["source"] == "kibble"
