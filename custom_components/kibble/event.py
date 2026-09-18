"""Physical button events for the feeder (`GET /state`'s `last_key`, LibreFeed-only -- see
`api.py`'s `KeyEvent` and `FeederState.last_key`; the vendor stack never populates this key,
so these entities go unavailable on it rather than reporting nothing ever happened).

The feeder has three physical buttons: the recessed pairing/reset button, and one labelled
button per hopper. `kibbled` only ever remembers the *most recent* one -- `last_key` is a
single slot, overwritten on every press -- so a poll interval that lands between a short
press's press-and-release (both well under the 45s scan interval) only ever observes the
final state, never both edges. That is an accepted limitation (see this integration's
`event.py` assignment notes), not a bug: this module fires whatever `last_key` reports the
moment it changes, once per change, and never replays a value already seen.
"""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity

# Read-only, coordinator-backed: nothing here writes to the device. See coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0

# `last_key.node` values (agent/src/... MCU protocol) -- which of the three physical buttons.
_NODE_PAIRING = 3
_NODE_BUTTON_1 = 2
_NODE_BUTTON_2 = 1

# This platform's own event-type vocabulary, collapsing the MCU's four raw `event` codes
# (4 press, 1 short release, 3 long-press threshold reached, 5 release after a long press) --
# releases of either kind are indistinguishable to anything downstream, so both map to the
# same "release" type.
EVENT_PRESS = "press"
EVENT_LONG_PRESS = "long_press"
EVENT_RELEASE = "release"

_EVENT_TYPE_BY_CODE = {
    4: EVENT_PRESS,
    3: EVENT_LONG_PRESS,
    1: EVENT_RELEASE,
    5: EVENT_RELEASE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            KibbleButtonEvent(coordinator, "button_pairing", _NODE_PAIRING),
            KibbleButtonEvent(coordinator, "button_1", _NODE_BUTTON_1),
            KibbleButtonEvent(coordinator, "button_2", _NODE_BUTTON_2),
        ]
    )


class KibbleButtonEvent(KibbleEntity, EventEntity):
    """One physical button, tracked by `last_key.node`.

    `last_key` is one slot shared by all three buttons, so every instance of this class sees
    every poll's value and only reacts to the ones naming its own `node`. `_last_seen` starts
    at whatever `last_key` already held at construction time (not `None`) specifically so the
    first coordinator update after startup -- which re-delivers that same unchanged value --
    never fires a stale event for something that happened before HA was watching.
    """

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = [EVENT_PRESS, EVENT_LONG_PRESS, EVENT_RELEASE]

    def __init__(self, coordinator: KibbleCoordinator, key: str, node: int) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._node = node
        self._last_seen = self._current_for_node()

    def _current_for_node(self) -> tuple[int, int, int] | None:
        last_key = self.coordinator.data.state.last_key
        if last_key is None or last_key.node != self._node:
            return None
        return (last_key.node, last_key.event, last_key.at_ms)

    @property
    def available(self) -> bool:
        return super().available and "last_key" in self.coordinator.data.state.raw

    @callback
    def _handle_coordinator_update(self) -> None:
        # A miss (another button's node, or no button pressed yet this boot) leaves
        # `_last_seen` untouched -- it only ever tracks the last snapshot that actually named
        # this entity's own node, never "nothing" from an unrelated update.
        current = self._current_for_node()
        if current is not None and current != self._last_seen:
            event_type = _EVENT_TYPE_BY_CODE.get(current[1])
            if event_type is not None:
                self._trigger_event(event_type, {"at_ms": current[2]})
            self._last_seen = current
        super()._handle_coordinator_update()
