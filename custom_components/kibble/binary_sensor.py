"""Binary sensors for Kibble."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util, slugify

from .api import IdentifyResult
from .const import DEFAULT_SCAN_INTERVAL
from .coordinator import KibbleConfigEntry, KibbleCoordinator, VendorSighting
from .entity import KibbleEntity

# Read-only, coordinator-backed: nothing here writes to the device. See coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0

# How long a cat stays "present" after its last identification. An implementation choice, not
# a device-measured value -- see docs/27-cat-id.md's honesty section. Two independent sources
# feed it: Kibble's own classifier (`GET /identify`, every enrolled cat) and the vendor's
# on-device identifier (`track` detections, only for cats mapped through the `vendor_pet_ids`
# option). Both are discrete, event-driven identifications, not a dwell time, so each latches
# for this window.
#
# Sized to the delivery path, not to taste: a sighting reaches HA up to one poll late
# (`DEFAULT_SCAN_INTERVAL`, 45 s), so a window shorter than one poll could expire before it is
# ever displayed, and one exactly one poll long is visible for a single refresh at best. Two
# polls plus slack is the smallest window that guarantees the entity turns on and stays on
# across at least one full refresh. The check itself is a timestamp comparison at read time --
# no polling, no timers, no load -- so nothing is saved by going lower.
PRESENCE_WINDOW = timedelta(seconds=DEFAULT_SCAN_INTERVAL * 2 + 30)

# How long `KibbleVomitDetectedBinarySensor` treats a detection as "fresh" -- per the design
# contract, `GET /state`'s `vomit_detected_at` is the last time the vendor behaviour
# classifier's pose-history window crossed its own 0.9 threshold; the value itself never
# clears, so this window is what makes the entity latch off again after a real event.
VOMIT_FRESH_WINDOW = timedelta(minutes=10)


def vomit_is_fresh(detected_at: int | None, now: datetime) -> bool:
    """Whether `detected_at` (`vomit_detected_at`, unix seconds or `None` if never this boot)
    falls within [VOMIT_FRESH_WINDOW] of `now`. A free function, not a method, so the window
    boundary is directly unit-testable with no entity or coordinator involved -- mirrors
    `is_present` below."""
    return detected_at is not None and now - dt_util.utc_from_timestamp(detected_at) < VOMIT_FRESH_WINDOW


# Every boolean setting still without a writable plumbed key. `light`, `night`, `microphone`,
# `vomit_detection`, `pet_detection`, `move_detection`, `eat_detection`, `feed_picture`,
# `eat_video`, `food_warn`, `time_display`, `camera`, `light_mode`, `tone_mode`, `sound_enable`,
# `feed_sound`, `system_sound_enable`, `smart_frame` are controls instead -- see `switch.py`.
# What's left here is still out of scope: `manual_lock`'s MCU protocol is still undecoded --
# see LibreFeed's own `docs/06-entity-audit.md`. `move_track_enable` is skipped entirely: it
# has no `cjson_key` of its own (an undocumented neighbour of `move_detection`) and is not
# independently user-controllable, so it carries no dedicated entity.
SETTING_SENSORS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="manual_lock",
        translation_key="manual_lock",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


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


def last_seen(
    cat_name: str, identify: IdentifyResult, sightings: Sequence[VendorSighting]
) -> datetime | None:
    """When `cat_name` was most recently identified, by either source, or None if never this
    run. The same two inputs `is_present` latches on, so the two can never disagree."""
    candidates = [s.ts for s in sightings if s.cat == cat_name]
    if identify.cat == cat_name and identify.ts is not None:
        candidates.append(identify.ts)
    if not candidates:
        return None
    return dt_util.utc_from_timestamp(max(candidates))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        KibbleFeedingSensor(coordinator),
        KibbleEatingSensor(coordinator),
        KibbleReachableBinarySensor(coordinator),
        KibbleVomitDetectedBinarySensor(coordinator),
    ]
    entities.extend(KibbleSettingBinarySensor(coordinator, d) for d in SETTING_SENSORS)
    entities.extend(KibbleHopperEmptySensor(coordinator, d) for d in HOPPER_EMPTY_SENSORS)
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


class KibbleVomitDetectedBinarySensor(KibbleEntity, BinarySensorEntity):
    """Diagnostic readout for the vendor's own on-device behaviour classifier
    (`CPetkitAlgoBehaviorRec`, kibble tools/VISION-ABI.md §21): on for [VOMIT_FRESH_WINDOW]
    after `GET /state`'s `vomit_detected_at` last advanced, off otherwise. The underlying
    signal is entirely the vendor's own classifier at its own hardcoded 0.9 threshold -- the
    protocol exposes no sensitivity knob for it (unlike `eat`/`move`/`pet` detection, which do
    have one), so this entity has no companion `number` entity and never will short of a new
    protocol field.

    Unavailable, rather than a bare `off`, when `vomit_detected_at` is missing from `GET
    /state` altogether -- an agent old enough to predate this field never reports it, which is
    a different fact from "reported, never yet detected" (`None`/`null`). Checks `state.raw`
    directly for the same reason `event.py`'s `KibbleButtonEvent.available` does for
    `keys`/`last_key`.
    """

    _attr_translation_key = "vomit_detected"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "vomit_detected")

    @property
    def available(self) -> bool:
        return super().available and "vomit_detected_at" in self.coordinator.data.state.raw

    @property
    def is_on(self) -> bool:
        return vomit_is_fresh(self.coordinator.data.state.vomit_detected_at, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        at = self.coordinator.data.state.vomit_detected_at
        return {"vomit_detected_at": dt_util.utc_from_timestamp(at).isoformat() if at is not None else None}


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


class KibbleSettingBinarySensor(KibbleEntity, BinarySensorEntity):
    """One read-only boolean device setting, read from the feeder's shared config.

    Unavailable, rather than a bare `unknown`, when this setting's key is missing from
    `GET /config` altogether -- LibreFeed serves only `light`/`night`/`microphone` there
    today, so every other entry in `SETTING_SENSORS` names a vendor-only setting the agent
    genuinely does not have an opinion on, not a value that happens to be unset. Mirrors
    `light.py`'s `KibbleStatusLight.available`/`select.py`'s `KibbleStackSelect.available`."""

    entity_description: BinarySensorEntityDescription

    def __init__(self, coordinator, description: BinarySensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.key in self.coordinator.data.config

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.config.get(self.entity_description.key)
        return None if value is None else bool(value)


class KibbleCatPresentBinarySensor(KibbleEntity, RestoreEntity, BinarySensorEntity):
    """Whether this specific cat was the most recently identified visitor, recently enough to
    still call it present -- see [`is_present`]/[`PRESENCE_WINDOW`]. Created dynamically as
    `GET /cats` reports new cats (`async_setup_entry` above).

    The `last_seen` attribute is what the dashboard's cat tiles show ("Last here 2 hours
    ago"). The vendor's `track` sightings live only in the agent's memory, so after an agent
    restart there is nothing to derive it from until the next visit; the last value is
    restored across that gap via `RestoreEntity` and any newer live identification wins
    over it."""

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
        self._restored_last_seen: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._live_last_seen() is not None:
            return
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        restored = last_state.attributes.get("last_seen")
        if isinstance(restored, str):
            self._restored_last_seen = dt_util.parse_datetime(restored)

    def _live_last_seen(self) -> datetime | None:
        data = self.coordinator.data
        return last_seen(self._cat_name, data.identify, data.vendor_sightings)

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        return is_present(self._cat_name, data.identify, data.vendor_sightings, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        live = self._live_last_seen()
        seen = live
        if seen is None or (self._restored_last_seen is not None and self._restored_last_seen > seen):
            seen = self._restored_last_seen
        return {"last_seen": seen.isoformat() if seen is not None else None}
