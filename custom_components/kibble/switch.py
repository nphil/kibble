"""Switches for the feeder's writable boolean settings.

`light`, `night`, `microphone`, `vomit_detection` were the original writable set (the same
keys the vendor's own `agent/src/settings.rs` also marked writable). kibbled deliberately left
every other boolean setting read-only rather than risk writing the vendor's `config_shm`
directly (see `binary_sensor.py`). LibreFeed owns its own `/config` now, so that caution no
longer applies -- this batch adds ten more vision.json- and settings.json-backed booleans real
feeder features gate on: `pet_detection`, `move_detection`, `eat_detection`, `feed_picture`,
`eat_video`, `food_warn`, `time_display`, `camera`, `light_mode`, `tone_mode` (see LibreFeed's
own `docs/06-entity-audit.md`, the `writable-after-plumbing` table). Every setting still
missing a plumbed key stays read-only and lives in `binary_sensor.py` instead, so this
platform never exposes a control surface the agent would reject.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import KibbleError
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity
from .errors import raise_agent_action_failed

# Writes are coordinator-mediated and serialised by api.py's own lock; see coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


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
    SwitchEntityDescription(
        key="vomit_detection",
        translation_key="vomit_detection",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="pet_detection",
        translation_key="pet_detection",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="move_detection",
        translation_key="move_detection",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="eat_detection",
        translation_key="eat_detection",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="feed_picture",
        translation_key="feed_picture",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="eat_video",
        translation_key="eat_video",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="food_warn",
        translation_key="food_warn",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="time_display",
        translation_key="time_display",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="camera",
        translation_key="camera",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="light_mode",
        translation_key="light_mode",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="tone_mode",
        translation_key="tone_mode",
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
    entities: list[SwitchEntity] = [KibbleSettingSwitch(coordinator, d) for d in SWITCHES]
    entities.append(KibbleCloudSwitch(coordinator))
    async_add_entities(entities)


class KibbleSettingSwitch(KibbleEntity, SwitchEntity):
    """One writable boolean device setting, read from and written to the feeder's shared
    config through the agent's `/config` endpoint.

    Unavailable, rather than a bare `unknown`, when this setting's key is missing from `GET
    /config` altogether -- e.g. `vomit_detection` on a daemon old enough to predate serving
    it. Mirrors `light.py`'s `KibbleStatusLight.available`/`select.py`'s
    `KibbleStackSelect.available`/`binary_sensor.py`'s
    `KibbleSettingBinarySensor.available`."""

    entity_description: SwitchEntityDescription

    def __init__(self, coordinator, description: SwitchEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.key in self.coordinator.data.config

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
            raise_agent_action_failed(f"Set {self.entity_description.key}", err)


class KibbleCloudSwitch(KibbleEntity, SwitchEntity):
    """The Petkit-cloud kill switch (`agent/src/cloud.rs`): routes everything but the LAN
    through a kernel route blackhole instead of the vendor's real default route, leaving
    Home Assistant, Scrypted and the router untouched. Enabled by default and CONFIG, not
    disabled-by-default like the settings switches above -- this is the privacy control the
    integration exists for, so it ships visible, matching the device's own out-of-the-box
    (cloud-enabled) behaviour."""

    _attr_translation_key = "cloud"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "cloud")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.cloud.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cloud = self.coordinator.data.cloud
        attrs: dict[str, Any] = {"routes": list(cloud.routes)}
        if cloud.last_error:
            attrs["last_error"] = cloud.last_error
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(False)

    async def _async_write(self, enabled: bool) -> None:
        try:
            await self.coordinator.async_set_cloud(enabled)
        except KibbleError as err:
            raise_agent_action_failed("Set Petkit cloud", err)
