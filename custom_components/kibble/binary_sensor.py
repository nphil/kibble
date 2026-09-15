"""Binary sensors for Kibble."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity


# Every boolean setting `agent/src/settings.rs` marks read-only. The three writable booleans
# (`light`, `night`, `microphone`) are controls instead -- see `switch.py`. `move_track_enable`
# is skipped: it has no `cjson_key` of its own (an undocumented neighbour of `move_detection`)
# and is not independently user-controllable, so it carries no dedicated entity.
SETTING_SENSORS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="time_display",
        translation_key="time_display",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="camera",
        translation_key="camera",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="move_detection",
        translation_key="move_detection",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="pet_detection",
        translation_key="pet_detection",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="eat_detection",
        translation_key="eat_detection",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="vomit_detection",
        translation_key="vomit_detection",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="feed_picture",
        translation_key="feed_picture",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="eat_video",
        translation_key="eat_video",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="sound_enable",
        translation_key="sound_enable",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="system_sound_enable",
        translation_key="system_sound_enable",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="feed_sound",
        translation_key="feed_sound",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="food_warn",
        translation_key="food_warn",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="light_mode",
        translation_key="light_mode",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="tone_mode",
        translation_key="tone_mode",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="manual_lock",
        translation_key="manual_lock",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="smart_frame",
        translation_key="smart_frame",
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
    entities: list[BinarySensorEntity] = [KibbleFeedingSensor(coordinator)]
    entities.extend(KibbleSettingBinarySensor(coordinator, d) for d in SETTING_SENSORS)
    async_add_entities(entities)


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


class KibbleSettingBinarySensor(KibbleEntity, BinarySensorEntity):
    """One read-only boolean device setting, read from the feeder's shared config."""

    entity_description: BinarySensorEntityDescription

    def __init__(self, coordinator, description: BinarySensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.config.get(self.entity_description.key)
        return None if value is None else bool(value)
