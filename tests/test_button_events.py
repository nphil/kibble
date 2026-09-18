"""HA-side logic for the physical-button event entities (`GET /state`'s `last_key`,
LibreFeed-only): same fake-`self` style as `test_status_light.py` -- exercises the real,
unbound `KibbleButtonEvent`/`FeederState` methods without constructing a real Home-Assistant-
backed entity or coordinator.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, call

from kibble.api import FeederState, KeyEvent
from kibble.event import EVENT_LONG_PRESS, EVENT_PRESS, KibbleButtonEvent

# --- (1) GET /state's last_key parsed -> KeyEvent, None when absent -----------------------------


def test_from_json_parses_last_key_into_key_event() -> None:
    state = FeederState.from_json(
        {"serial": "abc", "last_key": {"node": 2, "event": 4, "at_ms": 12345}}
    )
    assert state.last_key == KeyEvent(node=2, event=4, at_ms=12345)


def test_from_json_last_key_is_none_when_absent() -> None:
    state = FeederState.from_json({"serial": "abc"})
    assert state.last_key is None


# --- (2) fires exactly once per (node, event, at_ms) change, never replays, ignores other nodes -


def _fake_button_event(node: int, last_key: KeyEvent | None) -> KibbleButtonEvent:
    """A real (uninitialized) `KibbleButtonEvent` with only what `_handle_coordinator_update`
    and `_current_for_node` read set by hand -- same `object.__new__` approach as
    `test_status_light.py`. `async_write_ha_state` is stubbed since these fakes are never
    attached to a real `hass`; `_trigger_event` is replaced with a `Mock` so the test can
    assert on exactly what fired, without a real `EventEntity`'s state machinery."""
    entity = object.__new__(KibbleButtonEvent)
    entity._node = node
    entity.coordinator = SimpleNamespace(
        data=SimpleNamespace(state=SimpleNamespace(last_key=last_key, raw={"last_key": {}}))
    )
    entity._last_seen = entity._current_for_node()
    entity.async_write_ha_state = lambda: None
    entity._trigger_event = Mock()
    return entity


def test_fires_once_on_change_never_replays_and_ignores_other_nodes() -> None:
    # Startup: `last_key` already names this node -- `_last_seen` is seeded from it at
    # construction time, so the first update that re-delivers the same value must not fire.
    entity = _fake_button_event(node=2, last_key=KeyEvent(node=2, event=4, at_ms=500))
    state = entity.coordinator.data.state

    entity._handle_coordinator_update()
    entity._trigger_event.assert_not_called()

    # A genuinely new press for this node fires once, with `at_ms` attached.
    state.last_key = KeyEvent(node=2, event=4, at_ms=1000)
    entity._handle_coordinator_update()
    entity._trigger_event.assert_called_once_with(EVENT_PRESS, {"at_ms": 1000})

    # Polling again with the exact same (node, event, at_ms) never fires a second time.
    entity._handle_coordinator_update()
    entity._trigger_event.assert_called_once()

    # Another button's event (different node) never fires this entity.
    state.last_key = KeyEvent(node=1, event=4, at_ms=2000)
    entity._handle_coordinator_update()
    entity._trigger_event.assert_called_once()

    # A new event for this node (long-press threshold reached) fires again, mapped correctly.
    state.last_key = KeyEvent(node=2, event=3, at_ms=3000)
    entity._handle_coordinator_update()
    assert entity._trigger_event.call_args_list == [
        call(EVENT_PRESS, {"at_ms": 1000}),
        call(EVENT_LONG_PRESS, {"at_ms": 3000}),
    ]


# --- (3) unavailable when the polled state has no last_key (vendor stack) -----------------------


def test_unavailable_when_raw_has_no_last_key() -> None:
    entity = object.__new__(KibbleButtonEvent)
    entity.coordinator = SimpleNamespace(
        last_update_success=True, data=SimpleNamespace(state=SimpleNamespace(raw={}))
    )
    assert entity.available is False

    entity.coordinator.data.state.raw = {"last_key": None}
    assert entity.available is True
