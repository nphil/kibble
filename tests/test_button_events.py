"""HA-side logic for the physical-button event entities (`GET /state`'s `keys` ring,
LibreFeed-only): same fake-`self` style as `test_status_light.py` -- exercises the real,
unbound `KibbleButtonEvent`/`FeederState` methods without constructing a real Home-Assistant-
backed entity or coordinator.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, call

from kibble.api import FeederState, KeyEvent
from kibble.event import EVENT_LONG_PRESS, EVENT_PRESS, KibbleButtonEvent

# --- (1) GET /state's keys ring parsed -> tuple[KeyEvent, ...], falling back to last_key, else ()


def test_from_json_parses_keys_ring_oldest_first() -> None:
    state = FeederState.from_json(
        {
            "serial": "abc",
            "keys": [
                {"node": 2, "event": 4, "at_ms": 1000},
                {"node": 1, "event": 4, "at_ms": 2000},
            ],
        }
    )
    assert state.keys == (
        KeyEvent(node=2, event=4, at_ms=1000),
        KeyEvent(node=1, event=4, at_ms=2000),
    )


def test_from_json_falls_back_to_last_key_when_keys_absent() -> None:
    # Older LibreFeed builds that don't yet report the ring -- the single most-recent event
    # still surfaces as a one-element tuple so this entity keeps working on them.
    state = FeederState.from_json(
        {"serial": "abc", "last_key": {"node": 2, "event": 4, "at_ms": 12345}}
    )
    assert state.keys == (KeyEvent(node=2, event=4, at_ms=12345),)


def test_from_json_keys_is_empty_when_neither_reported() -> None:
    state = FeederState.from_json({"serial": "abc"})
    assert state.keys == ()


# --- (2) fires every unseen (event, at_ms) for its node, in ring order, seeded from what the ----
# --- ring held at construction; never replays, ignores other nodes ------------------------------


def _fake_button_event(node: int, keys: tuple[KeyEvent, ...]) -> KibbleButtonEvent:
    """A real (uninitialized) `KibbleButtonEvent` with only what `_handle_coordinator_update`
    and `_events_for_node` read set by hand -- same `object.__new__` approach as
    `test_status_light.py`. `async_write_ha_state` is stubbed since these fakes are never
    attached to a real `hass`; `_trigger_event` is replaced with a `Mock` so the test can
    assert on exactly what fired, without a real `EventEntity`'s state machinery."""
    entity = object.__new__(KibbleButtonEvent)
    entity._node = node
    entity.coordinator = SimpleNamespace(
        data=SimpleNamespace(state=SimpleNamespace(keys=keys, raw={"keys": []}))
    )
    entity._seen = set(entity._events_for_node())
    entity.async_write_ha_state = lambda: None
    entity._trigger_event = Mock()
    return entity


def test_fires_once_on_change_never_replays_and_ignores_other_nodes() -> None:
    # Startup: the ring already holds one event for this node -- `_seen` is seeded from it at
    # construction time, so the first update that re-delivers the same ring must not fire.
    entity = _fake_button_event(node=2, keys=(KeyEvent(node=2, event=4, at_ms=500),))
    state = entity.coordinator.data.state

    entity._handle_coordinator_update()
    entity._trigger_event.assert_not_called()

    # A genuinely new press for this node fires once, with `at_ms` attached.
    state.keys = (*state.keys, KeyEvent(node=2, event=4, at_ms=1000))
    entity._handle_coordinator_update()
    entity._trigger_event.assert_called_once_with(EVENT_PRESS, {"at_ms": 1000})

    # Polling again with the exact same ring never fires a second time.
    entity._handle_coordinator_update()
    entity._trigger_event.assert_called_once()

    # Another button's event (different node) never fires this entity.
    state.keys = (*state.keys, KeyEvent(node=1, event=4, at_ms=2000))
    entity._handle_coordinator_update()
    entity._trigger_event.assert_called_once()

    # A new event for this node (long-press threshold reached) fires again, mapped correctly.
    state.keys = (*state.keys, KeyEvent(node=2, event=3, at_ms=3000))
    entity._handle_coordinator_update()
    assert entity._trigger_event.call_args_list == [
        call(EVENT_PRESS, {"at_ms": 1000}),
        call(EVENT_LONG_PRESS, {"at_ms": 3000}),
    ]


def test_fires_every_unseen_ring_event_in_order_when_several_land_between_polls() -> None:
    # A poll interval landing between two full press/release cycles must not lose the first
    # one -- both fire, in ring order, on the single update that observes them together.
    entity = _fake_button_event(node=2, keys=())

    entity.coordinator.data.state.keys = (
        KeyEvent(node=2, event=4, at_ms=100),
        KeyEvent(node=1, event=4, at_ms=150),  # other node, never fires here
        KeyEvent(node=2, event=1, at_ms=200),
    )
    entity._handle_coordinator_update()

    assert entity._trigger_event.call_args_list == [
        call(EVENT_PRESS, {"at_ms": 100}),
        call("release", {"at_ms": 200}),
    ]


# --- (3) unavailable when the polled state has neither a keys ring nor last_key (vendor stack) --


def test_unavailable_when_raw_has_neither_keys_nor_last_key() -> None:
    entity = object.__new__(KibbleButtonEvent)
    entity.coordinator = SimpleNamespace(
        last_update_success=True, data=SimpleNamespace(state=SimpleNamespace(raw={}))
    )
    assert entity.available is False

    entity.coordinator.data.state.raw = {"last_key": None}
    assert entity.available is True

    entity.coordinator.data.state.raw = {"keys": []}
    assert entity.available is True
