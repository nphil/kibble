"""Binary sensors for Kibble."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

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
from .coordinator import KibbleConfigEntry, KibbleCoordinator, VendorSighting
from .entity import KibbleEntity

# Read-only, coordinator-backed: nothing here writes to the device. See coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0

# How long a cat stays "present" after its last identification. An implementation choice, not
# a device-measured value -- see docs/27-cat-id.md's honesty section. Two independent sources
# feed it: Kibble's own classifier (`GET /identify`, every enrolled cat) and the vendor's
# on-device identifier (`track` detections, only for cats mapped through the `vendor_pet_ids`
# option -- on this feeder that is the one cat enrolled in the Petkit app). Both are discrete,
# event-driven identifications, not a dwell time, so each latches for this window.
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


def is_present(
    cat_name: str,
    identify: IdentifyResult,
    sightings: Sequence[VendorSighting],
    now: datetime,
) -> bool:
    """Whether `cat_name` was identified -- by Kibble's classifier as the most recent visitor,
    or by the vendor's on-device identifier under its mapped pet id -- recently enough to
    still call it present. A free function (not a method) so it's directly unit-testable with
    no entity or coordinator involved."""
    if identify.cat == cat_name and identify.ts is not None:
        if now - dt_util.utc_from_timestamp(identify.ts) < PRESENCE_WINDOW:
            return True
    return any(
        s.cat == cat_name and now - dt_util.utc_from_timestamp(s.ts) < PRESENCE_WINDOW
        for s in sightings
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        KibbleFeedingSensor(coordinator),
        KibbleReachableBinarySensor(coordinator),
    ]
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


class KibbleReachableBinarySensor(KibbleEntity, BinarySensorEntity):
    """Whether the *most recent* poll reached the feeder at all -- `coordinator.
    feeder_reachable`, stricter than every other entity's `available`
    (`coordinator.last_update_success`, which the module docstring explains only goes
    `False` after several consecutive misses). This entity is the one place that difference
    is directly visible: it goes `off` at the very first missed poll, exactly the window
    where every other entity is still quietly showing its last known value, so a user who
    enables it can see "starting to have trouble" before anything actually goes unavailable
    for real -- and it is the one entity that deliberately does NOT go unavailable itself
    when the feeder is confirmed down (see its `available` override below), so it stays
    informative at exactly the moment every other entity stops being.
    """

    _attr_translation_key = "reachable"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "reachable")

    @property
    def is_on(self) -> bool:
        return self.coordinator.feeder_reachable

    @property
    def available(self) -> bool:
        """Always available -- see the class docstring. Never gated on
        `coordinator.last_update_success`; the entity's entire purpose is to keep reporting
        through the window where that would otherwise hide it."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "consecutive_failures": self.coordinator.consecutive_failures,
            "last_error": self.coordinator.last_error,
        }


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
        # Display-only capitalisation. The agent stores a cat's name verbatim (it is also the
        # directory name under /opt/kibble/faces), so a lowercase name produced a lowercase
        # friendly name -- "Cat Feeder pending present" -- which reads as a typo next to every
        # other sentence-case entity. Matching on `self._cat_name` stays exact; only the label
        # changes, and an already-capitalised or multi-word name is left alone.
        display = cat_name[:1].upper() + cat_name[1:] if cat_name else cat_name
        self._attr_translation_placeholders = {"cat_name": display}

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        return is_present(self._cat_name, data.identify, data.vendor_sightings, dt_util.utcnow())
