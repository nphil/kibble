"""Binary sensors for Kibble."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([KibbleFeedingSensor(entry.runtime_data)])


class KibbleFeedingSensor(KibbleEntity, BinarySensorEntity):
    """Whether a dispense cycle is running right now.

    This is the device's own transient flag in shared memory, set the moment the motor
    starts and cleared when it stops — no cloud round trip, and it appears within a poll
    of the command rather than seconds later.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "feeding"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "feeding")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.state.feeding
