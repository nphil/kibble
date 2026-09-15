"""Sensors for Kibble."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import FeederState
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
