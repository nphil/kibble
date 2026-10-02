"""Sensors for Kibble."""

from __future__ import annotations

import logging
from datetime import datetime
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util, slugify

from . import autolearn
from .api import ClipInfo, CloudState, DetectionEvent, FeederState, ScheduleEntry
from .ble_fallback import CONTROL_PATHS
from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity
from .stacks import applies_to

_LOGGER = logging.getLogger(__name__)

# Read-only, coordinator-backed: nothing here writes to the device. See coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class KibbleSensorDescription(SensorEntityDescription):
    """A sensor and how to read it out of a state snapshot."""

    value: Callable[[FeederState], int | str | None]
    #: Extra state attributes for this sensor, or `None` for the common "just a value" case.
    attributes: Callable[[FeederState], dict[str, Any]] | None = None


HOPPER_LEVELS = ["empty", "low", "ok"]


def _hopper_level_name(level: int | None) -> str | None:
    return HOPPER_LEVELS[level] if level is not None and 0 <= level < len(HOPPER_LEVELS) else None


def hopper_remaining(full_to_low: int | None, portions_since_full: int | None) -> int | None:
    """Portions estimated left in a hopper before it should hit its low-food threshold, or
    `None` while the daemon hasn't learned that hopper's full-to-low capacity yet (or it was
    never marked full)."""
    if full_to_low is None or portions_since_full is None:
        return None
    return max(0, full_to_low - portions_since_full)


@dataclass(frozen=True, kw_only=True)
class KibbleHopperRemainingDescription(SensorEntityDescription):
    """A per-hopper "portions left" sensor and which slot of `FeederState`'s full/since-full
    tuples it reads."""

    index: int


HOPPER_REMAINING_SENSORS: tuple[KibbleHopperRemainingDescription, ...] = (
    KibbleHopperRemainingDescription(
        key="hopper_1_remaining",
        translation_key="hopper_1_remaining",
        index=0,
        native_unit_of_measurement="portions",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    KibbleHopperRemainingDescription(
        key="hopper_2_remaining",
        translation_key="hopper_2_remaining",
        index=1,
        native_unit_of_measurement="portions",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


SENSORS: tuple[KibbleSensorDescription, ...] = (
    KibbleSensorDescription(
        # The MCU's own three-way hopper reading (docs/07-config.md §10) -- the closest thing
        # this feeder has to a hopper gauge; the `_empty` binary sensors are its collapsed form.
        key="hopper_1_level",
        translation_key="hopper_1_level",
        device_class=SensorDeviceClass.ENUM,
        options=HOPPER_LEVELS,
        value=lambda s: _hopper_level_name(s.hopper_level[0]),
    ),
    KibbleSensorDescription(
        key="hopper_2_level",
        translation_key="hopper_2_level",
        device_class=SensorDeviceClass.ENUM,
        options=HOPPER_LEVELS,
        value=lambda s: _hopper_level_name(s.hopper_level[1]),
    ),
    KibbleSensorDescription(
        key="desiccant_days",
        translation_key="desiccant_days",
        native_unit_of_measurement=UnitOfTime.DAYS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda s: s.desiccant_days,
    ),
    KibbleSensorDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda s: s.firmware,
    ),
    KibbleSensorDescription(
        key="ble_firmware",
        translation_key="ble_firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda s: s.ble_firmware,
    ),
)


# Every integer setting still without a writable plumbed key, minus `c_time`: that field's
# own description says it is "folded into the schedule surface rather than getting its own HA
# entity" -- it is the same value `KibbleScheduleSensor` below already reports as its
# `last_modified` attribute, so a second, disabled-by-default duplicate here would add
# `move_sensitivity`/`pet_sensitivity`/`detect_interval`/`surplus_standard` are writable
# `number` entities instead -- see `number.py`'s `SETTING_NUMBERS`; `eat_sensitivity` is also
# a writable `number` (`number.py`'s `KibbleEatHoldNumber`) but converted to a seconds hold
# time, not passed through as a raw percentage -- see that class's own docstring.
# `detect_range_from/_till`/`light_range_from/_till`/`tone_range_from/_till` are writable
# `text` entities instead -- see `text.py`'s `HOUR_RANGES`. `selected_sound`/`surplus_control`
# are writable `select` entities instead -- see `select.py`'s `SETTING_SELECTS`. What's left
# (`factor1`/`factor2`) stays out of scope: the vendor's grams-per-calibration-unit formula was
# never recovered (`docs/06-entity-audit.md`'s "Not possible, and why"), so the key can round-trip
# but has no defined effect worth exposing as a control.
SETTING_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="factor1",
        translation_key="factor1",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="factor2",
        translation_key="factor2",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


@dataclass(frozen=True, kw_only=True)
class KibbleCalibrationDescription(SensorEntityDescription):
    """A per-hopper bowl-fill calibration-state sensor and which slot of `GET /calibration`'s
    `hoppers` array it reads."""

    hopper: int


CALIBRATION_STATES = ["measured", "inherited", "uncalibrated"]

# Not `EntityCategory.DIAGNOSTIC`, and enabled by default: unlike `agent_starts`/`firmware`/
# `ble_firmware` above, this is not forensic/maintenance data about the integration itself --
# it is the fact that decides whether `bowl_fill`'s reading means anything for THIS hopper's
# food right now, the same operational tier as `hopper_1_level`/`hopper_2_level` (also plain,
# enabled-by-default sensors, not diagnostics). Hiding it behind the opt-in Diagnostics
# section would bury exactly the signal an operator needs right after setup or a food change:
# an uncalibrated hopper silently makes every portion-based reading of it meaningless, and
# visibility outside the card and in automations is the whole reason this exists as a sensor.
CALIBRATION_SENSORS: tuple[KibbleCalibrationDescription, ...] = (
    KibbleCalibrationDescription(
        key="bowl_fill_calibration_hopper_1",
        translation_key="bowl_fill_calibration_hopper_1",
        device_class=SensorDeviceClass.ENUM,
        options=CALIBRATION_STATES,
        hopper=0,
    ),
    KibbleCalibrationDescription(
        key="bowl_fill_calibration_hopper_2",
        translation_key="bowl_fill_calibration_hopper_2",
        device_class=SensorDeviceClass.ENUM,
        options=CALIBRATION_STATES,
        hopper=1,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    stack = coordinator.data.detected_stack
    entities: list[SensorEntity] = [KibbleSensor(coordinator, d) for d in SENSORS]
    entities.extend(KibbleSettingSensor(coordinator, d) for d in SETTING_SENSORS)
    entities.extend(
        KibbleCalibrationSensor(coordinator, d)
        for d in CALIBRATION_SENSORS
        if applies_to(Platform.SENSOR, d.key, stack)
    )
    entities.extend(
        KibbleHopperRemainingSensor(coordinator, d)
        for d in HOPPER_REMAINING_SENSORS
        if applies_to(Platform.SENSOR, d.key, stack)
    )
    # One-off, non-description-driven sensors -- `(key, entity)` so every one of them still
    # goes through `applies_to`, the same as the data-driven lists above, per `stacks.py`'s
    # "no scattered `if stack == ...`" rule. Building each entity is cheap (no I/O), so nothing
    # is lost constructing the handful that end up filtered out.
    one_offs: list[tuple[str, SensorEntity]] = [
        ("schedule", KibbleScheduleSensor(coordinator)),
        ("schedule_card_state", KibbleScheduleCardStateSensor(coordinator)),
        ("next_feed", KibbleNextFeedSensor(coordinator)),
        ("bowl_fill", KibbleBowlFillSensor(coordinator)),
        ("cloud_connection", KibbleCloudConnectionSensor(coordinator)),
        ("control_path", KibbleControlPathSensor(coordinator)),
        ("wifi_network", KibbleWifiNetworkSensor(coordinator)),
        ("wifi_signal", KibbleWifiSignalSensor(coordinator)),
        ("last_seen_pet", KibbleLastSeenPetSensor(coordinator)),
        ("clips", KibbleClipsSensor(coordinator)),
        ("last_detection", KibbleLastDetectionSensor(coordinator)),
        ("detections_today", KibbleDetectionsTodaySensor(coordinator)),
        ("agent_starts", KibbleAgentStartsSensor(coordinator)),
        ("recognition", KibbleOverallRecognitionSensor(coordinator)),
    ]
    entities.extend(entity for key, entity in one_offs if applies_to(Platform.SENSOR, key, stack))
    async_add_entities(entities)

    # Per-cat sensors are created dynamically from the identity engine's roster -- there is no
    # fixed list at integration setup, since cats are enrolled over time. Mirrors
    # binary_sensor.py's per-cat presence sensor.
    known_cats: set[str] = set()

    @callback
    def _add_new_cats() -> None:
        new = [name for name in coordinator.data.identity.cats if name not in known_cats]
        if not new:
            return
        known_cats.update(new)
        async_add_entities(
            entity
            for name in new
            for entity in (
                KibbleCatLastSeenSensor(coordinator, name),
                KibbleCatLastMealSensor(coordinator, name),
                KibbleCatMealsTodaySensor(coordinator, name),
                KibbleCatRecognitionSensor(coordinator, name),
            )
        )

    entry.async_on_unload(coordinator.async_add_listener(_add_new_cats))
    _add_new_cats()  # cats already known at setup time


class KibbleHopperRemainingSensor(KibbleEntity, SensorEntity):
    """Portions left in hopper `entity_description.index` before it should hit its low-food
    threshold. Unknown until the hopper has been marked full at least once and the daemon has
    learned its full-to-low capacity."""

    entity_description: KibbleHopperRemainingDescription

    def __init__(
        self, coordinator: KibbleCoordinator, description: KibbleHopperRemainingDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data.state
        idx = self.entity_description.index
        return hopper_remaining(state.hopper_full_to_low[idx], state.hopper_portions_since_full[idx])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.data.state
        idx = self.entity_description.index
        full_at = state.hopper_full_at[idx]
        return {
            "full_at": dt_util.utc_from_timestamp(full_at).isoformat() if full_at is not None else None,
            "portions_since_full": state.hopper_portions_since_full[idx],
            "full_to_low": state.hopper_full_to_low[idx],
        }


class KibbleSensor(KibbleEntity, SensorEntity):
    """One value read from the feeder's shared config."""

    entity_description: KibbleSensorDescription

    def __init__(self, coordinator, description: KibbleSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | str | None:
        return self.entity_description.value(self.coordinator.data.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        hook = self.entity_description.attributes
        return hook(self.coordinator.data.state) if hook is not None else None


class KibbleSettingSensor(KibbleEntity, SensorEntity):
    """One read-only integer device setting, read from the feeder's shared config.

    Unavailable, rather than a bare `unknown`, when this setting's key is missing from
    `GET /config` altogether -- an agent old enough to predate serving it. Today both stacks'
    `/config` report every `SETTING_SENSORS` key here (`factor1`/`factor2`, the vendor's own
    hopper calibration factors -- round-tripped by LibreFeed too, per its own
    `docs/06-entity-audit.md`, "the key can round-trip, but the vendor's grams formula is
    unrecovered"), so this specific check exists for agent-version gaps, not stack ones --
    see `stacks.py` for entities that ARE gated by which stack is running. Mirrors
    `light.py`'s `KibbleStatusLight.available`/`select.py`'s `KibbleStackSelect.available`."""

    entity_description: SensorEntityDescription

    def __init__(self, coordinator, description: SensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.key in self.coordinator.data.config

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.config.get(self.entity_description.key)


class KibbleScheduleSensor(KibbleEntity, SensorEntity):
    """The feed schedule kibbled has cached and last pushed to the device.

    kibbled owns the only readable copy of the schedule -- the MCU has no read-back for it -- so
    this always reflects what kibbled last successfully wrote, not a live device query. kibbled
    writes its cache before every send, so this stays accurate even if a send itself fails.
    """

    _attr_translation_key = "schedule"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "schedule")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.schedule.entries)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        schedule = self.coordinator.data.schedule
        return {
            "entries": [
                {
                    "id": e.id,
                    "time": e.time,
                    "amount_l": e.amount_l,
                    "amount_r": e.amount_r,
                    "enabled": e.enabled,
                    "next_fire": _iso_or_none(e.next_fire_utc),
                }
                for e in schedule.entries
            ],
            "last_modified": schedule.last_modified,
        }


def _iso_or_none(ts: int | None) -> str | None:
    return dt_util.utc_from_timestamp(ts).isoformat() if ts is not None else None


def next_feed_at(entries: Sequence[ScheduleEntry]) -> int | None:
    """The soonest fire across enabled entries, straight from kibbled's own scheduler (which
    already knows about the entry's `since_utc`, so a just-added entry whose time passed today
    correctly answers tomorrow). `None` when nothing is enabled."""
    fires = [e.next_fire_utc for e in entries if e.enabled and e.next_fire_utc is not None]
    return min(fires) if fires else None


class KibbleNextFeedSensor(KibbleEntity, SensorEntity):
    """When the next scheduled feed will happen -- a real timestamp, so the dashboard can say
    "tomorrow 5:25 PM" instead of parroting the first entry's clock time, and automations can
    trigger on it. Unknown while no entry is enabled; `paused`/`entries` tell why."""

    _attr_translation_key = "next_feed"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "next_feed")

    @property
    def native_value(self) -> datetime | None:
        ts = next_feed_at(self.coordinator.data.schedule.entries)
        return dt_util.utc_from_timestamp(ts) if ts is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entries = self.coordinator.data.schedule.entries
        return {
            "enabled_count": sum(1 for e in entries if e.enabled),
            "paused_count": sum(1 for e in entries if not e.enabled),
            "times": [e.time for e in sorted(entries, key=lambda e: e.time) if e.enabled],
        }


# HA enforces a 255-character limit on a sensor's own state string.
MAX_STATE_LENGTH = 255

# dispenser-schedule-card's `device.type: custom` adapter has no per-entry dispatch tracking
# yet on our side, so every packed entry reports this constant status code. Per that card's
# own `status_map` convention (docs/custom.md) and the config kibble-card already targets
# (`kibble-schedule-summary.ts`): `0 -> dispensed, 1 -> failed, 2 -> pending, 3 -> dispensing`.
# The card's own client-side logic still derives "skipped" correctly from "pending" plus a
# past dispense time alone.
STATUS_PENDING = 2


def pack_schedule_card_state(entries: Sequence[ScheduleEntry]) -> tuple[str, int, int]:
    """Packs enabled schedule entries into the packed, regex-parseable string
    `dispenser-schedule-card`'s `device.type: custom` adapter reads from an entity's own
    *state* (docs/custom.md): `"id,hour,minute,amount,status;..."`. Verified directly against
    that card's own parser, `status_pattern`:
    `(?<id>[^,]+),(?<hour>[0-9]{1,2}),(?<minute>[0-9]{1,2}),(?<amount>[0-9]{1,2}),(?<status>[0-9]);?`
    -- also the exact config `kibble-schedule-summary.ts` already builds against
    `sensor.…_schedule_card_state`.

    Disabled entries are omitted outright: this format's `status_map` has only
    dispensed/failed/pending/dispensing, no per-entry "disabled" code, and this integration
    configures no adapter-wide `switch:` either -- packing a disabled entry as "pending" would
    misleadingly claim it will still fire, so it is left out instead. An all-disabled table
    therefore packs to `""`, same as "no schedule set".

    One `amount` per entry, not two: `max(amount_l, amount_r)`, the same shared-bin
    simplification `kibble.feed`'s `hopper="both"` path already makes now the physical divider
    is removed -- on every write path through this adapter the two are mirrored equal anyway.

    The packed id is the entry's *index* in `KibbleCoordinator.card_entries` (all entries, by
    time), not the agent's string id: the card parses ids as integers, mints the next free one
    for a new entry, and sends them back on edit/remove/toggle; the card-facing services map an
    index back to the entry. `entries` must be that same full, ordered list.

    Entries are packed soonest-first (ascending `time`, matching the plain fallback list) until
    the next one would push the packed string past HA's 255-character state limit; anything
    left over is silently dropped from *this* entity only -- `sensor.…_schedule`'s own
    `entries` attribute (the fallback list's source) is unaffected and always complete.

    Returns `(packed_state, entries_packed, entries_eligible)`.
    """
    eligible = [(index, e) for index, e in enumerate(entries) if e.enabled]
    parts: list[str] = []
    packed = ""
    for index, entry in eligible:
        try:
            hour_str, minute_str = entry.time.split(":", 1)
            hour, minute = int(hour_str), int(minute_str)
        except (ValueError, AttributeError):
            _LOGGER.warning(
                "schedule entry %r has an unparseable time %r; omitting from the card state",
                entry.id,
                entry.time,
            )
            continue
        amount = max(entry.amount_l, entry.amount_r)
        piece = f"{index},{hour},{minute},{amount},{STATUS_PENDING}"
        candidate = ";".join([*parts, piece])
        if len(candidate) > MAX_STATE_LENGTH:
            break
        parts.append(piece)
        packed = candidate
    return packed, len(parts), len(eligible)


class KibbleScheduleCardStateSensor(KibbleEntity, SensorEntity):
    """Feeds `dispenser-schedule-card`'s `device.type: custom` adapter (see
    `pack_schedule_card_state`) so the Kibble card's schedule summary can embed that card
    instead of falling back to its own plain list -- `kibble-card` README's "Schedule card
    integration". A second, purpose-built entity because that adapter reads the packed string
    from an entity's own *state*, and `sensor.…_schedule`'s state is deliberately the entry
    count (its rich list lives in an attribute the card's regex adapter never reads).

    Not diagnostic/config -- the card depends on it directly to render at all.
    """

    _attr_translation_key = "schedule_card_state"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "schedule_card_state")

    @property
    def native_value(self) -> str:
        packed, _packed_count, _eligible = pack_schedule_card_state(self.coordinator.card_entries())
        return packed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _packed, packed_count, eligible = pack_schedule_card_state(self.coordinator.card_entries())
        return {
            "entries_packed": packed_count,
            "entries_eligible": eligible,
            "truncated": packed_count < eligible,
        }


_CLOUD_CONNECTION_STATES = ("connected", "blocked", "unreachable")


def _cloud_connection_state(cloud: CloudState) -> str:
    """`enabled` reflects the kill switch; a non-LAN `ESTABLISHED` socket reflects whether
    the feeder is *actually* exchanging traffic with Petkit's cloud right now -- the two can
    disagree (switch on but nothing connected yet after a fresh boot or a fail-safe
    rollback, or switch off but a lingering `TIME_WAIT` socket that isn't `ESTABLISHED`)."""
    if not cloud.enabled:
        return "blocked"
    if any(c.state == "ESTABLISHED" for c in cloud.connections):
        return "connected"
    return "unreachable"


class KibbleCloudConnectionSensor(KibbleEntity, SensorEntity):
    """Whether the feeder is actually exchanging traffic with Petkit's cloud right now, from
    a live read of non-LAN sockets (`agent/src/cloud.rs`'s `GET /cloud`) gated by the kill
    switch -- not just an echo of the switch's own position."""

    _attr_translation_key = "cloud_connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(_CLOUD_CONNECTION_STATES)

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "cloud_connection")

    @property
    def native_value(self) -> str:
        return _cloud_connection_state(self.coordinator.data.cloud)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cloud = self.coordinator.data.cloud
        return {"connections": [{"remote": c.remote, "state": c.state} for c in cloud.connections]}


class KibbleControlPathSensor(KibbleEntity, SensorEntity):
    """Which transport the most recent `kibble.feed` call used or attempted:

    - `wifi`: the agent's HTTP API answered (the normal case).
    - `bluetooth`: Wi-Fi was unreachable and a BLE fallback was attempted through an ESPHome
      Bluetooth proxy (`docs/25-ble-feed-frame.md`) -- reported regardless of whether the
      fallback itself succeeded, since Bluetooth was the path actually used.
    - `unreachable`: Wi-Fi was unreachable and no `ble_address` is configured.

    Unknown until the first feed call after Home Assistant starts -- there is nothing to
    report before then.
    """

    _attr_translation_key = "control_path"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(CONTROL_PATHS)

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "control_path")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.control_path


class KibbleWifiNetworkSensor(KibbleEntity, SensorEntity):
    """The feeder's current Wi-Fi association (`agent/src/wifi.rs`'s `GET /wifi`). State is the
    SSID (`None`/unknown while disconnected); bssid/band/signal/ip ride along as attributes so
    the one entity carries the full picture without four separate sensors."""

    _attr_translation_key = "wifi"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "wifi")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.wifi.ssid

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        wifi = self.coordinator.data.wifi
        return {
            "bssid": wifi.bssid,
            "band": wifi.band,
            "signal_dbm": wifi.signal_dbm,
            "ip": wifi.ip,
        }


class KibbleWifiSignalSensor(KibbleEntity, SensorEntity):
    """Live received-signal strength of the feeder's current Wi-Fi association, from
    `signal_poll` (or the last scan's entry for the current bssid -- see `agent/src/wifi.rs`)."""

    _attr_translation_key = "wifi_signal"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "wifi_signal")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.wifi.signal_dbm


class KibbleLastSeenPetSensor(KibbleEntity, SensorEntity):
    """The cat identified in the newest event HA's own identity engine could name
    (docs/36-ai-pipeline.md) -- state is unavailable until anything has ever been identified.
    No live device call: this reads straight off the store, the same engine that names every
    row on the timeline, so the two can never disagree."""

    _attr_translation_key = "last_seen_pet"

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "last_seen_pet")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.identity.last_seen_pet

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ts = self.coordinator.data.identity.last_seen_pet_ts
        if ts is None:
            return {}
        return {"last_identified": dt_util.utc_from_timestamp(ts).isoformat()}


def _cat_display_name(cat_name: str) -> str:
    """Display-only capitalisation -- the store keeps a cat's name exactly as entered, so a
    lowercase name would otherwise read as a typo next to every other sentence-case entity."""
    return cat_name[:1].upper() + cat_name[1:] if cat_name else cat_name


class KibbleCatLastSeenSensor(KibbleEntity, SensorEntity):
    """When this cat was last identified at the bowl, from any visit or eat."""

    _attr_translation_key = "cat_last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: KibbleCoordinator, cat_name: str) -> None:
        super().__init__(coordinator, f"cat_last_seen_{slugify(cat_name)}")
        self._cat_name = cat_name
        self._attr_translation_placeholders = {"cat_name": _cat_display_name(cat_name)}

    @property
    def native_value(self) -> datetime | None:
        stats = self.coordinator.data.identity.cats.get(self._cat_name)
        return dt_util.utc_from_timestamp(stats.last_seen) if stats and stats.last_seen else None


class KibbleCatLastMealSensor(KibbleEntity, SensorEntity):
    """When this cat was last identified eating."""

    _attr_translation_key = "cat_last_meal"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: KibbleCoordinator, cat_name: str) -> None:
        super().__init__(coordinator, f"cat_last_meal_{slugify(cat_name)}")
        self._cat_name = cat_name
        self._attr_translation_placeholders = {"cat_name": _cat_display_name(cat_name)}

    @property
    def native_value(self) -> datetime | None:
        stats = self.coordinator.data.identity.cats.get(self._cat_name)
        return dt_util.utc_from_timestamp(stats.last_meal) if stats and stats.last_meal else None


class KibbleCatMealsTodaySensor(KibbleEntity, SensorEntity):
    """How many meals this cat has been identified at since local midnight. Filtered at read
    time against every eat in the last 48h rather than a count fixed at the last ingest pass,
    so the day boundary rolls over on its own -- same pattern as
    `KibbleDetectionsTodaySensor`."""

    _attr_translation_key = "cat_meals_today"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: KibbleCoordinator, cat_name: str) -> None:
        super().__init__(coordinator, f"cat_meals_today_{slugify(cat_name)}")
        self._cat_name = cat_name
        self._attr_translation_placeholders = {"cat_name": _cat_display_name(cat_name)}

    @property
    def native_value(self) -> int:
        stats = self.coordinator.data.identity.cats.get(self._cat_name)
        if stats is None:
            return 0
        start = dt_util.start_of_local_day()
        return sum(1 for ts in stats.recent_meals if dt_util.utc_from_timestamp(ts) >= start)


class KibbleCatRecognitionSensor(KibbleEntity, SensorEntity):
    """How well the identity engine recognises this cat, 0-100% -- confidence-weighted rolling
    accuracy against real human reviews (or, before there is enough of those, the classifier's
    own mean guess confidence as a labelled estimate), scaled down by how much training data
    backs it so a handful of perfect samples never reads as "done"
    (`autolearn.recognition_score`). Watch it climb, then turn `switch.*_auto_learn` off once
    it is near full.

    No `state_class`: per-cat entities come and go with the roster (enrolled, deleted, maybe
    re-enrolled later as a fresh identity), and a `MEASUREMENT` history surviving that is
    exactly the orphaned long-term-statistics row Home Assistant's own guidance warns against
    for dynamic entities."""

    _attr_translation_key = "cat_recognition"
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: KibbleCoordinator, cat_name: str) -> None:
        super().__init__(coordinator, f"cat_recognition_{slugify(cat_name)}")
        self._cat_name = cat_name
        self._attr_translation_placeholders = {"cat_name": _cat_display_name(cat_name)}

    @property
    def native_value(self) -> int:
        stats = self.coordinator.data.identity.cats.get(self._cat_name)
        return stats.recognition_score if stats else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self.coordinator.data.identity.cats.get(self._cat_name)
        if stats is None:
            return {"training_samples": 0, "uploads": 0, "basis": "estimate", "learning_state": "learning"}
        return {
            "training_samples": stats.training_samples,
            "uploads": stats.training_uploads,
            "basis": stats.recognition_basis,
            "learning_state": stats.learning_state,
        }


class KibbleOverallRecognitionSensor(KibbleEntity, SensorEntity):
    """The weakest-recognised enrolled cat's score, not an average -- "turn training off once
    it is near full" means every cat, not most of them (`autolearn.overall_recognition_score`).
    `None` (unknown) with nothing enrolled yet."""

    _attr_translation_key = "recognition"
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "recognition")

    @property
    def native_value(self) -> int | None:
        scores = [stats.recognition_score for stats in self.coordinator.data.identity.cats.values()]
        return autolearn.overall_recognition_score(scores)


class KibbleClipsSensor(KibbleEntity, SensorEntity):
    """How many audio clips are stored on the feeder (`kibble.save_clip`/`kibble.record_clip`),
    with each clip's name and encoded size as an attribute -- diagnostic (house rule 4:
    disabled by default)."""

    _attr_translation_key = "clips"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "clips")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.clips)

    @property
    def extra_state_attributes(self) -> dict[str, list[dict[str, Any]]]:
        clips: tuple[ClipInfo, ...] = self.coordinator.data.clips
        return {"clips": [{"name": c.name, "bytes": c.bytes} for c in clips]}


def _latest_detection(events: Sequence[DetectionEvent]) -> DetectionEvent | None:
    """The newest detection, or `None` if the agent has seen none.

    `GET /events` is oldest-first, but sort defensively rather than trusting order: the agent
    rehydrates this list from disk at startup and a future change to that ordering should not
    silently make this entity report a stale event."""
    if not events:
        return None
    return max(events, key=lambda e: (e.ts, e.seq))


class KibbleLastDetectionSensor(KibbleEntity, SensorEntity):
    """When the feeder's onboard AI last saw a visit or eat track.

    Exists because a detection could previously be captured perfectly and remain completely
    invisible in Home Assistant -- the agent held the events and the crops, but nothing surfaced
    them. The state is the timestamp (so it renders as "2 minutes ago"); the kind and best body
    crop's filename ride along as attributes. Deliberately identity-agnostic -- see
    `sensor.*_last_seen_pet`/`image.*_last_detection` for the named version."""

    _attr_translation_key = "last_detection"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "last_detection")

    @property
    def native_value(self) -> datetime | None:
        event = _latest_detection(self.coordinator.data.events)
        if event is None or not event.ts:
            return None
        return dt_util.utc_from_timestamp(event.ts)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        event = _latest_detection(self.coordinator.data.events)
        if event is None:
            return {}
        return {"kind": event.kind, "image": event.image}


class KibbleDetectionsTodaySensor(KibbleEntity, SensorEntity):
    """How many detections the agent has recorded since local midnight, by kind.

    Counts from the agent's own event list (capped at its newest 256 -- docs/36-ai-pipeline.md),
    so a very busy day reports "at least this many" rather than a true total -- stated in the
    attributes instead of being quietly wrong."""

    _attr_translation_key = "detections_today"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "detections_today")

    def _today(self) -> list[DetectionEvent]:
        start = dt_util.start_of_local_day()
        return [
            e
            for e in self.coordinator.data.events
            if e.ts and dt_util.utc_from_timestamp(e.ts) >= start
        ]

    @property
    def native_value(self) -> int:
        return len(self._today())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        today = self._today()
        by_kind: dict[str, int] = {}
        for e in today:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        return {
            "by_kind": by_kind,
            # True when the agent's own 256-event cap may be hiding older detections from today.
            "capped": len(self.coordinator.data.events) >= 256,
        }


class KibbleAgentStartsSensor(KibbleEntity, SensorEntity):
    """How many times kibbled has started since the feeder last booted.

    A steady 1 is the healthy reading. Anything higher means the agent exited and was restarted
    by the vendor's app supervisor, which is worth knowing because nothing else surfaces it: the
    counters live in tmpfs (`/tmp/kibble-health.json`) precisely so we never write restart logs
    to the feeder's NAND, and they reset on reboot. `last_exit_code` is the previous run's exit
    status -- null on a clean first start, and the field to look at when the count climbs.
    """

    _attr_translation_key = "agent_starts"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    # Deliberately no `state_class`: the counter resets to 1 whenever the feeder reboots, so a
    # long-term sum would be meaningless, and a statistic id here would be stranded the moment
    # this (disabled-by-default) entity is turned back off.

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "agent_starts")

    @property
    def native_value(self) -> int:
        return self.coordinator.data.state.agent_starts

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.data.state
        return {
            "last_start": dt_util.utc_from_timestamp(state.agent_last_start).isoformat()
            if state.agent_last_start
            else None,
            "last_exit_code": state.agent_last_exit_code,
        }


# --- Bowl-fill calibration (LibreFeed-only) --------------------------------------------------


def _calibration_state(hopper: dict[str, Any] | None) -> str:
    """Classifies one hopper's `GET /calibration` entry (`coordinator.py`'s `KibbleData.
    calibration`, the agent's raw `{"hoppers": [...]}` JSON) into this sensor's three-way
    state.

    `source` is either the literal string `"measured"` (this hopper recorded its own curve --
    `begin` then one or more `point`s) or an object `{"inherited_from": N}` (copied wholesale
    from hopper `N` via the `inherit` action) -- a parser that only checks `source ==
    "measured"` and calls everything else "uncalibrated" silently mis-reports every inherited
    hopper as never calibrated, exactly the bug this exists to avoid. `None` (the daemon's own
    "this hopper has never been calibrated" marker) is `uncalibrated`."""
    if hopper is None:
        return "uncalibrated"
    source = hopper.get("source")
    if isinstance(source, dict) and "inherited_from" in source:
        return "inherited"
    return "measured"


def _calibration_attributes(hopper: dict[str, Any] | None) -> dict[str, Any]:
    """The wizard's own bookkeeping for one hopper -- empty while `hopper` is `None` (nothing
    recorded yet, see `_calibration_state`)."""
    if hopper is None:
        return {}
    return {
        "full_portions": hopper.get("full_portions"),
        "full_score": hopper.get("full_score"),
        "points": len(hopper.get("points") or ()),
        "measured_at": _iso_or_none(hopper.get("measured_at")),
        "note": hopper.get("note"),
    }


class KibbleBowlFillSensor(KibbleEntity, SensorEntity):
    """`bowl_fill`: the camera-measured reading when one exists, overridden by an immediate
    post-feed projection (`bowl_fill.py`, `KibbleCoordinator.async_apply_bowl_fill_feed`) until
    the next camera assessment supersedes it. Same `key`/unique id/unit/state class as the
    entity this replaced (a bespoke class purely because the estimate override needs more logic
    than the generic `KibbleSensorDescription.value` callable shape allows) -- no statistics are
    orphaned by this change.

    `source` is `"estimate"` while an unresolved post-feed projection is showing, else
    `"measured"` for either underlying camera path (the vendor's own reading, or Kibble's local
    fallback when the vendor's has gone stale -- see the module's old `bowl_fill` docstring in
    git history for why that fallback exists); `measured_at` still rides along for the local
    path specifically. `fill_per_portion`/`samples` are always `[hopper1, hopper2]`, regardless
    of which bucket the current reading actually came from."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "bowl_fill"

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "bowl_fill")

    def _fill_per_portion_attrs(self) -> dict[str, Any]:
        fpp1, samples1 = self.coordinator.bowl_fill_per_portion("hopper1")
        fpp2, samples2 = self.coordinator.bowl_fill_per_portion("hopper2")
        return {"fill_per_portion": [round(fpp1, 2), round(fpp2, 2)], "samples": [samples1, samples2]}

    @property
    def native_value(self) -> int | None:
        estimate = self.coordinator.bowl_fill_estimate
        if estimate is not None:
            return round(estimate[0])
        state = self.coordinator.data.state
        return state.bowl_fill if state.bowl_fill is not None else state.bowl_fill_local[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        estimate = self.coordinator.bowl_fill_estimate
        if estimate is not None:
            return {"source": "estimate", **estimate[1]}
        state = self.coordinator.data.state
        attrs: dict[str, Any] = {"source": "measured", **self._fill_per_portion_attrs()}
        if state.bowl_fill is None:
            attrs["measured_at"] = _iso_or_none(state.bowl_fill_local[1])
        return attrs


class KibbleCalibrationSensor(KibbleEntity, SensorEntity):
    """Whether hopper `entity_description.hopper`'s bowl-fill vision score has been calibrated
    against real dispensed portions -- `measured` (this hopper ran its own curve), `inherited`
    (copied from the other hopper), or `uncalibrated` (never run). The calibration wizard
    behind `websocket.py`'s `kibble/calibration`/`kibble/calibration/action` is the only thing
    that ever changes this; nothing here dispenses or reads the bowl on its own.

    Unavailable, not merely `uncalibrated`, when the whole route is missing (`GET
    /calibration` 404ing -- the vendor stack, or a LibreFeed build old enough to predate it):
    that is "we don't know", a materially different fact from "we asked and nothing is
    recorded yet". Mirrors `button.py`'s `KibbleReplaceDesiccantButton.available`."""

    entity_description: KibbleCalibrationDescription

    def __init__(
        self, coordinator: KibbleCoordinator, description: KibbleCalibrationDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    def _hopper(self) -> dict[str, Any] | None:
        calibration = self.coordinator.data.calibration
        if calibration is None:
            return None
        hoppers = calibration.get("hoppers") or []
        index = self.entity_description.hopper
        return hoppers[index] if index < len(hoppers) else None

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.calibration is not None

    @property
    def native_value(self) -> str:
        return _calibration_state(self._hopper())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return _calibration_attributes(self._hopper())
