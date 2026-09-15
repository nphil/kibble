"""Sensors for Kibble."""

from __future__ import annotations

import logging
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
    EntityCategory,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import ClipInfo, CloudState, FeederState, ScheduleEntry
from .ble_fallback import CONTROL_PATHS
from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity

_LOGGER = logging.getLogger(__name__)

# Read-only, coordinator-backed: nothing here writes to the device. See coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class KibbleSensorDescription(SensorEntityDescription):
    """A sensor and how to read it out of a state snapshot."""

    value: Callable[[FeederState], int | str | None]


SENSORS: tuple[KibbleSensorDescription, ...] = (
    KibbleSensorDescription(
        key="bowl_fill_1",
        translation_key="bowl_fill_1",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: s.bowl_fill[0],
    ),
    KibbleSensorDescription(
        key="bowl_fill_2",
        translation_key="bowl_fill_2",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: s.bowl_fill[1],
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


# Every integer setting `agent/src/settings.rs` marks read-only, minus `c_time`: that
# field's own description says it is "folded into the schedule surface rather than getting
# its own HA entity" -- it is the same value `KibbleScheduleSensor` below already reports as
# its `last_modified` attribute, so a second, disabled-by-default duplicate here would add
# nothing.
#
# `selected_sound` and `surplus_control` are plain integers here, not `select`: neither has
# a confirmed option list. settings.rs says so directly -- `selected_sound`'s "valid id range
# not recovered by this study" and `surplus_control`'s own state values are undocumented even
# in Localkit's own app schema. A `select` needs a real option list to render; these don't
# have one.
SETTING_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="move_sensitivity",
        translation_key="move_sensitivity",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="pet_sensitivity",
        translation_key="pet_sensitivity",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="eat_sensitivity",
        translation_key="eat_sensitivity",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="detect_interval",
        translation_key="detect_interval",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="detect_range_from",
        translation_key="detect_range_from",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="detect_range_till",
        translation_key="detect_range_till",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="selected_sound",
        translation_key="selected_sound",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
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
    SensorEntityDescription(
        key="light_range_from",
        translation_key="light_range_from",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="light_range_till",
        translation_key="light_range_till",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="tone_range_from",
        translation_key="tone_range_from",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="tone_range_till",
        translation_key="tone_range_till",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="surplus_control",
        translation_key="surplus_control",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="surplus_standard",
        translation_key="surplus_standard",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [KibbleSensor(coordinator, d) for d in SENSORS]
    entities.extend(KibbleSettingSensor(coordinator, d) for d in SETTING_SENSORS)
    entities.append(KibbleScheduleSensor(coordinator))
    entities.append(KibbleScheduleCardStateSensor(coordinator))
    entities.append(KibbleCloudConnectionSensor(coordinator))
    entities.append(KibbleControlPathSensor(coordinator))
    entities.append(KibbleWifiNetworkSensor(coordinator))
    entities.append(KibbleWifiSignalSensor(coordinator))
    entities.append(KibbleLastSeenPetSensor(coordinator))
    entities.append(KibbleIdentificationScoreSensor(coordinator))
    entities.append(KibblePendingFacesSensor(coordinator))
    entities.append(KibbleClipsSensor(coordinator))
    async_add_entities(entities)


class KibbleSensor(KibbleEntity, SensorEntity):
    """One value read from the feeder's shared config."""

    entity_description: KibbleSensorDescription

    def __init__(self, coordinator, description: KibbleSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | str | None:
        return self.entity_description.value(self.coordinator.data.state)


class KibbleSettingSensor(KibbleEntity, SensorEntity):
    """One read-only integer device setting, read from the feeder's shared config."""

    entity_description: SensorEntityDescription

    def __init__(self, coordinator, description: SensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

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
                }
                for e in schedule.entries
            ],
            "last_modified": schedule.last_modified,
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

    An id containing `,` or `;` would corrupt the packed grammar; such an entry is skipped
    (logged, not raised) rather than emitted broken.

    Entries are packed soonest-first (ascending `time`, matching the plain fallback list) until
    the next one would push the packed string past HA's 255-character state limit; anything
    left over is silently dropped from *this* entity only -- `sensor.…_schedule`'s own
    `entries` attribute (the fallback list's source) is unaffected and always complete.

    Returns `(packed_state, entries_packed, entries_eligible)`.
    """
    eligible = sorted((e for e in entries if e.enabled), key=lambda e: e.time)
    parts: list[str] = []
    packed = ""
    for entry in eligible:
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
        if "," in entry.id or ";" in entry.id:
            _LOGGER.warning(
                "schedule entry id %r contains ',' or ';'; omitting from the card state",
                entry.id,
            )
            continue
        amount = max(entry.amount_l, entry.amount_r)
        piece = f"{entry.id},{hour},{minute},{amount},{STATUS_PENDING}"
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
        packed, _packed_count, _eligible = pack_schedule_card_state(
            self.coordinator.data.schedule.entries
        )
        return packed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _packed, packed_count, eligible = pack_schedule_card_state(
            self.coordinator.data.schedule.entries
        )
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
    """Kibble's own frozen-embedding classifier's most recent opinion (`GET /identify`) --
    state is the cat's name, the literal `"unknown"` if the classifier ran but wasn't
    confident, or unavailable if nothing has ever been captured. `source` (an attribute)
    distinguishes a live classifier guess from ground truth carried over from the most
    recently labelled crop once the review queue is empty. `docs/27-cat-id.md` documents
    measured accuracy and the cold-start behaviour this can show with very little labelled
    data -- treat a low-sample-count identification as a guess, not a fact."""

    _attr_translation_key = "last_seen_pet"

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "last_seen_pet")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.identify.cat

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self.coordinator.data.identify
        attrs: dict[str, Any] = {}
        if result.source is not None:
            attrs["source"] = result.source
        if result.score is not None:
            attrs["score"] = result.score
        if result.second_best is not None:
            attrs["second_best_cat"] = result.second_best.cat
            attrs["second_best_score"] = result.second_best.score
        if result.ts is not None:
            attrs["last_identified"] = dt_util.utc_from_timestamp(result.ts).isoformat()
        return attrs


class KibbleIdentificationScoreSensor(KibbleEntity, SensorEntity):
    """The raw cosine-similarity score behind `last_seen_pet`'s current identification --
    troubleshooting/tuning only (e.g. seeing how close a borderline call was), disabled by
    default per house rule 4."""

    _attr_translation_key = "identification_score"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "identification_score")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.identify.score


class KibblePendingFacesSensor(KibbleEntity, SensorEntity):
    """How many captured face crops are still awaiting a human label -- diagnostic (house rule
    4: disabled by default), not something Nitin needs to watch routinely."""

    _attr_translation_key = "pending_faces"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "pending_faces")

    @property
    def native_value(self) -> int:
        return self.coordinator.data.pending_face_count


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
