"""Number controls.

`AMOUNTS` are Home Assistant preferences, not device settings: the feeder stores no "default
portion" -- each dispense carries its own amount in the command -- so they are plain
`RestoreEntity`-backed numbers with no device round trip.

There is one amount per auger plus a combined one, because the feed payload carries a separate
byte per auger (`b[65]`, `b[66]` -- see `agent/src/feed.rs`).

On *this* unit, however, per-auger targeting is not honoured: a command sent with
`amount_l=1, amount_r=0` was observed to spin BOTH augers, so a "hopper 1" dispense delivers
roughly twice the requested portions into the single (divider-less) bowl. The encoding is
correct -- verified byte-for-byte against the vendor's own commands -- so this is firmware
behaviour, not a bug here. Treat the combined `feed_amount` as the control that means what it
says and the two per-hopper amounts as advisory until someone refits the divider and re-tests.

`SETTING_NUMBERS`, by contrast, are real device settings: writable integers read from and
written to the feeder's shared config through the agent's `/config` endpoint, the same
round trip `switch.py`'s `KibbleSettingSwitch` already uses for booleans. Three are
sensitivities (0-100, higher means more sensitive), one is a cadence in seconds, and six are
time-of-day minutes-since-midnight pairs (`from`/`till`, equal means "always active") --
see LibreFeed's own `docs/06-entity-audit.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api import KibbleError
from .const import (
    HOPPER_1,
    HOPPER_2,
    HOPPER_BOTH,
    MAX_AMOUNT,
    MAX_DETECT_INTERVAL_S,
    MAX_MINUTES_OF_DAY,
    MAX_SENSITIVITY,
    MIN_AMOUNT,
    MIN_DETECT_INTERVAL_S,
    MIN_MINUTES_OF_DAY,
    MIN_SENSITIVITY,
)
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity
from .errors import raise_agent_action_failed


# Writes are coordinator-mediated and serialised by api.py's own lock; see coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class KibbleAmountDescription(NumberEntityDescription):
    """An amount control and the hopper its companion button dispenses from."""

    hopper: str


AMOUNTS: tuple[KibbleAmountDescription, ...] = (
    KibbleAmountDescription(
        key="feed_amount", translation_key="feed_amount", hopper=HOPPER_BOTH
    ),
    KibbleAmountDescription(
        key="feed_amount_hopper_1", translation_key="feed_amount_hopper_1", hopper=HOPPER_1
    ),
    KibbleAmountDescription(
        key="feed_amount_hopper_2", translation_key="feed_amount_hopper_2", hopper=HOPPER_2
    ),
)


@dataclass(frozen=True, kw_only=True)
class KibbleSettingNumberDescription(NumberEntityDescription):
    """A writable integer device setting read from and written to the feeder's shared config
    through the agent's `/config` endpoint. Mirrors `switch.py`'s `SwitchEntityDescription`
    use for booleans.

    `hhmm_attribute` renders the value as a human `HH:MM` extra-state attribute -- for the six
    schedule pairs below, whose value is minutes since local midnight, not a plain magnitude.
    """

    hhmm_attribute: bool = False


def _minutes_to_hhmm(minutes: int) -> str:
    """`645` -> `"10:45"` -- the device's own minutes-since-midnight encoding for its three
    schedule pairs, rendered so a dashboard can show a time instead of a raw integer."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


SETTING_NUMBERS: tuple[KibbleSettingNumberDescription, ...] = (
    KibbleSettingNumberDescription(
        key="pet_sensitivity",
        translation_key="pet_sensitivity",
        native_min_value=MIN_SENSITIVITY,
        native_max_value=MAX_SENSITIVITY,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    KibbleSettingNumberDescription(
        key="move_sensitivity",
        translation_key="move_sensitivity",
        native_min_value=MIN_SENSITIVITY,
        native_max_value=MAX_SENSITIVITY,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    KibbleSettingNumberDescription(
        key="eat_sensitivity",
        translation_key="eat_sensitivity",
        native_min_value=MIN_SENSITIVITY,
        native_max_value=MAX_SENSITIVITY,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    KibbleSettingNumberDescription(
        key="detect_interval",
        translation_key="detect_interval",
        native_min_value=MIN_DETECT_INTERVAL_S,
        native_max_value=MAX_DETECT_INTERVAL_S,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    KibbleSettingNumberDescription(
        key="detect_range_from",
        translation_key="detect_range_from",
        native_min_value=MIN_MINUTES_OF_DAY,
        native_max_value=MAX_MINUTES_OF_DAY,
        native_step=1,
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        hhmm_attribute=True,
    ),
    KibbleSettingNumberDescription(
        key="detect_range_till",
        translation_key="detect_range_till",
        native_min_value=MIN_MINUTES_OF_DAY,
        native_max_value=MAX_MINUTES_OF_DAY,
        native_step=1,
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        hhmm_attribute=True,
    ),
    KibbleSettingNumberDescription(
        key="light_range_from",
        translation_key="light_range_from",
        native_min_value=MIN_MINUTES_OF_DAY,
        native_max_value=MAX_MINUTES_OF_DAY,
        native_step=1,
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        hhmm_attribute=True,
    ),
    KibbleSettingNumberDescription(
        key="light_range_till",
        translation_key="light_range_till",
        native_min_value=MIN_MINUTES_OF_DAY,
        native_max_value=MAX_MINUTES_OF_DAY,
        native_step=1,
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        hhmm_attribute=True,
    ),
    KibbleSettingNumberDescription(
        key="tone_range_from",
        translation_key="tone_range_from",
        native_min_value=MIN_MINUTES_OF_DAY,
        native_max_value=MAX_MINUTES_OF_DAY,
        native_step=1,
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        hhmm_attribute=True,
    ),
    KibbleSettingNumberDescription(
        key="tone_range_till",
        translation_key="tone_range_till",
        native_min_value=MIN_MINUTES_OF_DAY,
        native_max_value=MAX_MINUTES_OF_DAY,
        native_step=1,
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        hhmm_attribute=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [
        KibbleFeedAmount(coordinator, description) for description in AMOUNTS
    ]
    entities.extend(KibbleSettingNumber(coordinator, d) for d in SETTING_NUMBERS)
    async_add_entities(entities)


class KibbleFeedAmount(KibbleEntity, NumberEntity, RestoreEntity):
    """How many portions the matching feed button dispenses."""

    entity_description: KibbleAmountDescription

    _attr_native_min_value = MIN_AMOUNT
    _attr_native_max_value = MAX_AMOUNT
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, description: KibbleAmountDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._value = float(MIN_AMOUNT)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            try:
                self._value = float(last.state)
            except ValueError:
                pass

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = value
        self.async_write_ha_state()


class KibbleSettingNumber(KibbleEntity, NumberEntity):
    """One writable integer device setting, read from and written to the feeder's shared
    config through the agent's `/config` endpoint. Mirrors `switch.py`'s
    `KibbleSettingSwitch`.

    Unavailable, rather than a bare `unknown`, when this setting's key is missing from `GET
    /config` altogether -- e.g. on a daemon old enough to predate serving it. Mirrors
    `switch.py`'s `KibbleSettingSwitch.available`/`sensor.py`'s
    `KibbleSettingSensor.available`."""

    entity_description: KibbleSettingNumberDescription

    def __init__(self, coordinator, description: KibbleSettingNumberDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.key in self.coordinator.data.config

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.data.config.get(self.entity_description.key)
        return None if value is None else float(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self.entity_description.hhmm_attribute:
            return None
        value = self.coordinator.data.config.get(self.entity_description.key)
        return {"time": _minutes_to_hhmm(int(value)) if value is not None else None}

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_set_config(self.entity_description.key, int(value))
        except KibbleError as err:
            raise_agent_action_failed(f"Set {self.entity_description.key}", err)
