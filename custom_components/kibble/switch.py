"""Switches for the feeder's writable boolean settings.

Of `agent/src/settings.rs`'s 37 device settings, three booleans are confirmed writable
(`light`, `night`, `microphone` -- the same three keys `POST /config` accepts). Every other
boolean setting is read-only and lives in `binary_sensor.py` instead, so this platform never
exposes a control surface the agent would reject.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import KibbleError
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity

SWITCHES: tuple[SwitchEntityDescription, ...] = (
    SwitchEntityDescription(
        key="night",
        translation_key="night",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="light",
        translation_key="light",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="microphone",
        translation_key="microphone",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(KibbleSettingSwitch(coordinator, d) for d in SWITCHES)


class KibbleSettingSwitch(KibbleEntity, SwitchEntity):
    """One writable boolean device setting, read from and written to the feeder's shared
    config through the agent's `/config` endpoint."""

    entity_description: SwitchEntityDescription

    def __init__(self, coordinator, description: SwitchEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.config.get(self.entity_description.key)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(0)

    async def _async_write(self, value: int) -> None:
        try:
            await self.coordinator.async_set_config(self.entity_description.key, value)
        except KibbleError as err:
            raise HomeAssistantError(
                f"Set {self.entity_description.key} failed: {err}"
            ) from err
