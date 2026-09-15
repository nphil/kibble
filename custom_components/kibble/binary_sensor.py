"""Binary sensors for Kibble."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util, slugify

from .api import IdentifyResult
from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity

# How long a cat stays "present" after its last confident identification. An implementation
# choice, not a device-measured value -- see docs/27-cat-id.md's honesty section. There is no
# live, continuous detection feed to derive presence from (agent/src/ai.rs's own doc: score/
# pet_id/box are unreachable without replacing ctrl), so this latches on the classifier's own
# discrete, event-driven identifications instead of a real dwell time.
PRESENCE_WINDOW = timedelta(minutes=15)


# Every boolean setting `agent/src/settings.rs` marks read-only. The three writable booleans
# (`light`, `night`, `microphone`) are controls instead -- see `switch.py`. `move_track_enable`
# is skipped: it has no `cjson_key` of its own (an undocumented neighbour of `move_detection`)
# and is not independently user-controllable, so it carries no dedicated entity.
SETTING_SENSORS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="time_display",
        translation_key="time_display",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="camera",
        translation_key="camera",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="move_detection",
        translation_key="move_detection",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="pet_detection",
        translation_key="pet_detection",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="eat_detection",
        translation_key="eat_detection",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="vomit_detection",
        translation_key="vomit_detection",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="feed_picture",
        translation_key="feed_picture",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="eat_video",
        translation_key="eat_video",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="sound_enable",
        translation_key="sound_enable",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="system_sound_enable",
        translation_key="system_sound_enable",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="feed_sound",
        translation_key="feed_sound",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="food_warn",
        translation_key="food_warn",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="light_mode",
        translation_key="light_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="tone_mode",
        translation_key="tone_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="manual_lock",
        translation_key="manual_lock",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="smart_frame",
        translation_key="smart_frame",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


def is_present(cat_name: str, identify: IdentifyResult, now: datetime) -> bool:
    """Whether `cat_name` was the most recently identified visitor, recently enough to still
    call it present. A free function (not a method) so it's directly unit-testable with no
    entity or coordinator involved."""
    if identify.cat != cat_name or identify.ts is None:
        return False
    seen_at = dt_util.utc_from_timestamp(identify.ts)
    return now - seen_at < PRESENCE_WINDOW


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [KibbleFeedingSensor(coordinator)]
    entities.extend(KibbleSettingBinarySensor(coordinator, d) for d in SETTING_SENSORS)
    async_add_entities(entities)

    # Per-cat presence entities are created dynamically from `GET /cats` -- there is no fixed
    # list at integration setup, since cats are enrolled over time by labelling crops.
    known_cats: set[str] = set()

    @callback
    def _add_new_cats() -> None:
        new = [cat.name for cat in coordinator.data.cats if cat.name not in known_cats]
        if not new:
            return
        known_cats.update(new)
        async_add_entities([KibbleCatPresentBinarySensor(coordinator, name) for name in new])

    entry.async_on_unload(coordinator.async_add_listener(_add_new_cats))
    _add_new_cats()  # cats already known at setup time


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


class KibbleCatPresentBinarySensor(KibbleEntity, BinarySensorEntity):
    """Whether this specific cat was the most recently identified visitor, recently enough to
    still call it present -- see [`is_present`]/[`PRESENCE_WINDOW`]. Created dynamically as
    `GET /cats` reports new cats (`async_setup_entry` above)."""

    _attr_translation_key = "cat_present"

    def __init__(self, coordinator: KibbleCoordinator, cat_name: str) -> None:
        super().__init__(coordinator, f"cat_present_{slugify(cat_name)}")
        self._cat_name = cat_name
        self._attr_translation_placeholders = {"cat_name": cat_name}

    @property
    def is_on(self) -> bool:
        return is_present(self._cat_name, self.coordinator.data.identify, dt_util.utcnow())
