"""The feed-amount control.

The amount is a Home Assistant preference, not a device setting: the feeder has no stored
"default portion" — each dispense carries its own amount in the command. Keeping it as a
`number` gives the dashboard a dial and the feed button something to read.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MAX_AMOUNT, MIN_AMOUNT
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([KibbleFeedAmount(entry.runtime_data)])


class KibbleFeedAmount(KibbleEntity, NumberEntity, RestoreEntity):
    """How many portions the feed button dispenses."""

    _attr_translation_key = "feed_amount"
    _attr_native_min_value = MIN_AMOUNT
    _attr_native_max_value = MAX_AMOUNT
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "feed_amount")
        self._value = 1.0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            try:
                self._value = float(last.state)
            except ValueError:
                pass

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = value
        self.async_write_ha_state()
