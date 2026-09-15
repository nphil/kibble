"""Number controls.

Two different kinds of `number` entity live here. `AMOUNTS` are Home Assistant preferences,
not device settings: the feeder stores no "default portion" -- each dispense carries its own
amount in the command -- so they are plain `RestoreEntity`-backed numbers with no device round
trip. `KibbleVolumeNumber` is the opposite: a single device-backed setting (the only writable
integer in `agent/src/settings.rs`'s table), read from and written to the feeder's shared
config through the agent's `/config` endpoint, same as `switch.py`'s writable booleans.

There is one amount per auger plus a combined one. The two augers are independent motors with
their own byte in the feed payload, so they stay separately controllable whether or not the
physical hopper divider is fitted; without it both simply draw from one bin.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api import KibbleError
from .const import HOPPER_1, HOPPER_2, HOPPER_BOTH, MAX_AMOUNT, MIN_AMOUNT
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [
        KibbleFeedAmount(coordinator, description) for description in AMOUNTS
    ]
    entities.append(KibbleVolumeNumber(coordinator))
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


class KibbleVolumeNumber(KibbleEntity, NumberEntity):
    """Speaker volume, the device's own 0-9 app-scale setting (`agent/src/settings.rs`'s
    `volume` key) -- the only writable integer setting, read from and written to the
    feeder's shared config through the agent's `/config` endpoint."""

    _attr_translation_key = "volume"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_native_min_value = 0
    _attr_native_max_value = 9
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "volume")

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.data.config.get("volume")
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_set_config("volume", int(value))
        except KibbleError as err:
            raise_agent_action_failed("Set volume", err)
