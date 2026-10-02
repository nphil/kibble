"""Binary sensors for Kibble."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util, slugify

from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity
from .stacks import applies_to

# Read-only, coordinator-backed: nothing here writes to the device. See coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


# `light`, `night`, `microphone`, `pet_detection`, `move_detection`,
# `eat_detection`, `feed_picture`, `eat_video`, `food_warn`, `time_display`, `camera`,
# `light_mode`, `tone_mode`, `sound_enable`, `feed_sound`, `system_sound_enable`, `smart_frame`
# are controls instead -- see `switch.py`. `move_track_enable` carries no entity at all: it has
# no `cjson_key` of its own (an undocumented neighbour of `move_detection`) and is not
# independently user-controllable.

@dataclass(frozen=True, kw_only=True)
class KibbleHopperEmptyDescription(BinarySensorEntityDescription):
    """A per-hopper "food ran out" flag and which slot of `FeederState.hopper_empty` it reads."""

    index: int


# The feeder's own low-food threshold, mirrored locally -- kibble docs/07-config.md: `ctrl`'s
# tone-alarm gate and `ble`'s warning-flag setter/clearer both fire whenever a hopper's raw
# 0/1/2 level reads below 2, so that is what "empty" means here too, not just a literal 0.
HOPPER_EMPTY_SENSORS: tuple[KibbleHopperEmptyDescription, ...] = (
    KibbleHopperEmptyDescription(
        key="hopper_1_empty",
        translation_key="hopper_1_empty",
        device_class=BinarySensorDeviceClass.PROBLEM,
        index=0,
    ),
    KibbleHopperEmptyDescription(
        key="hopper_2_empty",
        translation_key="hopper_2_empty",
        device_class=BinarySensorDeviceClass.PROBLEM,
        index=1,
    ),
)




class KibbleBowlEmptySensor(KibbleEntity, BinarySensorEntity):
    """Whether the bowl is actually empty, per the feeder's own hysteretic verdict.

    Exists because `sensor.*_bowl_fill` cannot answer the question an automation asks. That
    number is a relative vision score, not a fraction of capacity: measured on the real device
    (2026-09-19) an EMPTY bowl reads 0-8 rather than 0 -- the detector scores the bowl's own
    texture and shadow -- while a small dispensed portion reads 15-21. Nothing has ever
    measured what a full bowl scores, so the top of the range means nothing yet. Gating a
    dispense on `bowl_fill < 10` therefore looks reasonable and is guesswork; this verdict is
    the measured one, with a dead band and agreement across readings behind it, and it ignores
    the 63-81 spikes a cat's head in the bowl produces.

    `None` (unknown) until the feeder has taken an unobstructed reading. An automation that
    dispenses food must require `is_state(..., 'on')` rather than `not is_state(..., 'off')`,
    so that "do not know" never feeds the cat.
    """

    _attr_translation_key = "bowl_empty"

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "bowl_empty")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.state.bowl_empty is not None

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.state.bowl_empty

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.data.state
        return {"occluded": state.bowl_occluded, "fill_score": state.bowl_fill}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    stack = coordinator.data.detected_stack
    entities: list[BinarySensorEntity] = [
        KibbleFeedingSensor(coordinator),
        KibbleEatingSensor(coordinator),
        KibbleReachableBinarySensor(coordinator),
    ]
    if applies_to(Platform.BINARY_SENSOR, "bowl_empty", stack):
        entities.append(KibbleBowlEmptySensor(coordinator))
    entities.extend(
        KibbleHopperEmptySensor(coordinator, d)
        for d in HOPPER_EMPTY_SENSORS
        if applies_to(Platform.BINARY_SENSOR, d.key, stack)
    )
    async_add_entities(entities)

    if not applies_to(Platform.BINARY_SENSOR, "cat_present", stack):
        return

    # Per-cat presence entities are created dynamically from the identity engine's roster --
    # there is no fixed list at integration setup, since cats are enrolled over time.
    known_cats: set[str] = set()

    @callback
    def _add_new_cats() -> None:
        new = [name for name in coordinator.data.identity.cats if name not in known_cats]
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


class KibbleEatingSensor(KibbleEntity, BinarySensorEntity):
    """Whether a pet is eating at the bowl right now, per the feeder's own vision detector.

    `media` raises this flag when its eat state machine fires (the same verdict the
    Petkit app's "ate" notifications come from) and clears it when the meal ends --
    typically a minute or two later. Read straight from the device's shared memory, so
    it works with the cloud disabled.
    """

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_translation_key = "eating"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "eating")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.state.eating


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


class KibbleHopperEmptySensor(KibbleEntity, BinarySensorEntity):
    """Whether one hopper's food-level sensor is at or below the vendor's own low-food
    threshold (`agent/src/state.rs::off::FOOD_1`/`FOOD_2`, kibble docs/07-config.md).

    The device reports three raw levels (0 empty, 1 low, 2 full/ok), not a plain boolean --
    this collapses to `True` for 0 and 1, matching the exact threshold the feeder's own
    firmware uses internally to decide "sound the low-food alert" (two independent vendor
    code paths agree on it, disassembly-proven, see the offset doc comment). `None` while
    the byte still holds the boot-time "never reported yet" sentinel.
    """

    entity_description: KibbleHopperEmptyDescription

    def __init__(self, coordinator: KibbleCoordinator, description: KibbleHopperEmptyDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.state.hopper_empty[self.entity_description.index]


class KibbleCatPresentBinarySensor(KibbleEntity, BinarySensorEntity):
    """Whether this specific cat is the subject of a currently open visit or eat track.

    A live query (`store.identity_summary`'s `CatStats.present`), not a time-decay window:
    the device tells HA exactly when a track opens and closes (docs/36-ai-pipeline.md), so
    presence needs no latching heuristic of its own, and nothing needs restoring across an HA
    restart either -- the store itself is the durable record. Created dynamically as the
    identity engine's cat roster grows (`async_setup_entry` above).

    `last_seen`/`last_ate` are ISO timestamps: any visit or eat, and specifically the newest
    eat, this cat was identified in."""

    _attr_translation_key = "cat_present"

    def __init__(self, coordinator: KibbleCoordinator, cat_name: str) -> None:
        super().__init__(coordinator, f"cat_present_{slugify(cat_name)}")
        self._cat_name = cat_name
        # Display-only capitalisation -- the store keeps a cat's name exactly as entered, so a
        # lowercase name would otherwise read as a typo next to every other sentence-case
        # entity. Matching on `self._cat_name` stays exact; only the label changes.
        display = cat_name[:1].upper() + cat_name[1:] if cat_name else cat_name
        self._attr_translation_placeholders = {"cat_name": display}

    @property
    def is_on(self) -> bool:
        stats = self.coordinator.data.identity.cats.get(self._cat_name)
        return stats.present if stats is not None else False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self.coordinator.data.identity.cats.get(self._cat_name)
        last_seen = (
            dt_util.utc_from_timestamp(stats.last_seen).isoformat()
            if stats is not None and stats.last_seen is not None
            else None
        )
        last_ate = (
            dt_util.utc_from_timestamp(stats.last_meal).isoformat()
            if stats is not None and stats.last_meal is not None
            else None
        )
        return {"last_seen": last_seen, "last_ate": last_ate}
