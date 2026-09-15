"""Sensors for Kibble."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfSignalStrength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import CloudState, FeederState
from .ble_fallback import CONTROL_PATHS
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity


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
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="pet_sensitivity",
        translation_key="pet_sensitivity",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="eat_sensitivity",
        translation_key="eat_sensitivity",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="detect_interval",
        translation_key="detect_interval",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="detect_range_from",
        translation_key="detect_range_from",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="detect_range_till",
        translation_key="detect_range_till",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="selected_sound",
        translation_key="selected_sound",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="factor1",
        translation_key="factor1",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="factor2",
        translation_key="factor2",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="light_range_from",
        translation_key="light_range_from",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="light_range_till",
        translation_key="light_range_till",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="tone_range_from",
        translation_key="tone_range_from",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="tone_range_till",
        translation_key="tone_range_till",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="surplus_control",
        translation_key="surplus_control",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="surplus_standard",
        translation_key="surplus_standard",
        native_unit_of_measurement=PERCENTAGE,
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
    entities: list[SensorEntity] = [KibbleSensor(coordinator, d) for d in SENSORS]
    entities.extend(KibbleSettingSensor(coordinator, d) for d in SETTING_SENSORS)
    entities.append(KibbleScheduleSensor(coordinator))
    entities.append(KibbleCloudConnectionSensor(coordinator))
    entities.append(KibbleControlPathSensor(coordinator))
    entities.append(KibbleWifiNetworkSensor(coordinator))
    entities.append(KibbleWifiSignalSensor(coordinator))
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
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = UnitOfSignalStrength.DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "wifi_signal")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.wifi.signal_dbm
