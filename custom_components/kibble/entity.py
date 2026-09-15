"""Shared entity base for Kibble."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import KibbleCoordinator


class KibbleEntity(CoordinatorEntity[KibbleCoordinator]):
    """An entity belonging to one feeder."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: KibbleCoordinator, key: str) -> None:
        super().__init__(coordinator)
        state = coordinator.data
        self._attr_unique_id = f"{state.serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, state.serial)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name="Cat Feeder",
            serial_number=state.serial,
            sw_version=state.firmware,
        )
