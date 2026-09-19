"""Physical button events for the feeder (`GET /state`'s `keys` ring, LibreFeed-only -- see
`api.py`'s `KeyEvent` and `FeederState.keys`; the vendor stack never populates it, so these
entities go unavailable on it rather than reporting nothing ever happened).

The feeder has three physical buttons: the recessed pairing/reset button, and one labelled
button per hopper. LibreFeed's MCU daemon keeps the last 16 key events (oldest first) and
also the newest one as `last_key`; a poll interval that lands between a short press's
press-and-release, or between two buttons, would lose events if only `last_key` were read.
Each entity therefore diffs the ring against the last snapshot it saw and fires every event
for its own node it has not fired yet, in order. Events are identified by `(event, at_ms)`,
never by position, so a reboot (at_ms restarts) or the ring wrapping cannot replay or skip.
"""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity
from .stacks import applies_to

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
    stack = coordinator.data.detected_stack
    candidates = [
        ("button_pairing", _NODE_PAIRING),
        ("button_1", _NODE_BUTTON_1),
        ("button_2", _NODE_BUTTON_2),
    ]
    async_add_entities(
        KibbleButtonEvent(coordinator, key, node)
        for key, node in candidates
        if applies_to(Platform.EVENT, key, stack)
    )


class KibbleButtonEvent(KibbleEntity, EventEntity):
    """One physical button, tracked by `node` inside the shared `keys` ring.

    `_seen` starts as whatever the ring already held for this node at construction time
    specifically so the first coordinator update after startup -- which re-delivers those same
    events -- never fires anything that happened before HA was watching.
    """

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = [EVENT_PRESS, EVENT_LONG_PRESS, EVENT_RELEASE]

    def __init__(self, coordinator: KibbleCoordinator, key: str, node: int) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._node = node
        self._seen = set(self._events_for_node())

    def _events_for_node(self) -> list[tuple[int, int]]:
        """`(event, at_ms)` for this node, oldest first."""
        return [(k.event, k.at_ms) for k in self.coordinator.data.state.keys if k.node == self._node]

    @property
    def available(self) -> bool:
        raw = self.coordinator.data.state.raw
        return super().available and ("keys" in raw or "last_key" in raw)

    @callback
    def _handle_coordinator_update(self) -> None:
        current = self._events_for_node()
        for event_code, at_ms in current:
            if (event_code, at_ms) in self._seen:
                continue
            event_type = _EVENT_TYPE_BY_CODE.get(event_code)
            if event_type is not None:
                self._trigger_event(event_type, {"at_ms": at_ms})
        self._seen = set(current)
        super()._handle_coordinator_update()
