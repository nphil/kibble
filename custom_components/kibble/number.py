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
round trip `switch.py`'s `KibbleSettingSwitch` already uses for booleans. Two are genuinely
continuous percentages (`pet_sensitivity`/`move_sensitivity` -- NanoDet confidence and the
luma-delta motion bar), one is a cadence in seconds. The three time-of-day
minutes-since-midnight `from`/`till` pairs that used to live here (`detect_range_from/_till`,
`light_range_from/_till`, `tone_range_from/_till`) are gone: each pair is now one `text.py`
entity (`"HH:MM-HH:MM"`, `text.KibbleHourRangeText`), not two number entities that could
independently drift out of sync with each other between writes -- see that module's own
docstring. `eat_sensitivity` is also gone from here as a percentage: only 10 distinct
eat-hold times exist behind its 0..100 wire range, so a percentage implied false precision --
see `KibbleEatHoldNumber`'s own docstring for the honest seconds control that replaced it
(same wire key, converted both ways). See LibreFeed's own `docs/06-entity-audit.md` for the
rest.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, EntityCategory, Platform, UnitOfTime
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
    MAX_SENSITIVITY,
    MAX_SURPLUS_STANDARD,
    MIN_AMOUNT,
    MIN_DETECT_INTERVAL_S,
    MIN_SENSITIVITY,
    MIN_SURPLUS_STANDARD,
)
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity
from .errors import raise_agent_action_failed
from .stacks import applies_to


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
    use for booleans."""


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
        key="surplus_standard",
        translation_key="surplus_standard",
        native_min_value=MIN_SURPLUS_STANDARD,
        native_max_value=MAX_SURPLUS_STANDARD,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.BOX,
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
    stack = coordinator.data.detected_stack
    entities: list[NumberEntity] = [
        KibbleFeedAmount(coordinator, description) for description in AMOUNTS
    ]
    entities.extend(
        KibbleSettingNumber(coordinator, d) for d in SETTING_NUMBERS if applies_to(Platform.NUMBER, d.key, stack)
    )
    if applies_to(Platform.NUMBER, "eating_hold", stack):
        entities.append(KibbleEatHoldNumber(coordinator))
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

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_set_config(self.entity_description.key, int(value))
        except KibbleError as err:
            raise_agent_action_failed(f"Set {self.entity_description.key}", err)


def eat_sensitivity_to_hold_s(sensitivity: int) -> int:
    """`/config`'s `eat_sensitivity` (0..100, higher = more sensitive) -> the eat-hold time in
    seconds (1..10, shorter = more sensitive). Mirrors the daemon's own authoritative
    `daemon/src/vision.rs::eat_sensitivity_to_hold_s` -- verified against Rust's `f32::round()`
    (round-half-away-from-zero) across the whole 0..100 domain, including the one exact .5
    tie (`sensitivity == 50`), so plain `round()` here is not a divergent reimplementation."""
    clamped = max(0, min(100, sensitivity))
    return max(1, min(10, round(10 - clamped * 9 / 100)))


def eat_hold_s_to_eat_sensitivity(hold_s: int) -> int:
    """Inverse of `eat_sensitivity_to_hold_s`, mirroring the daemon's own
    `eat_hold_s_to_eat_sensitivity`. Exact at both mappings' shared boundaries (`0<->10`,
    `100<->1`) and at the shared default (`78<->3`) -- not every one of `eat_sensitivity`'s
    101 values is recoverable from `eat_hold_s`'s 10, see `KibbleEatHoldNumber`'s own
    docstring for why the control is honestly seconds, not a percentage."""
    clamped = max(1, min(10, hold_s))
    return max(0, min(100, round((10 - clamped) * 100 / 9)))


class KibbleEatHoldNumber(KibbleEntity, NumberEntity):
    """How many continuous seconds a body must overlap the bowl ROI before the eat state
    machine latches "eating" (`daemon/src/vision.rs`'s `VisionConfig::eat_hold_s`). The wire
    key stays `/config`'s `eat_sensitivity` -- LibreFeed's `/config` contract is 0..100, and
    reusing the vendor's own key/range avoids inventing a second, redundant setting for the
    same underlying knob -- but the control surface here is seconds, not a percentage: only
    10 distinct hold times exist behind that 0..100 range (`eat_sensitivity_to_hold_s`'s
    domain), so a percentage slider (as this used to be, `SETTING_NUMBERS`' old
    `eat_sensitivity` entry) implied 101 levels of precision the device does not have --
    `eat_sensitivity` 74 through 78 are all literally the same 3-second hold, and a percentage
    could not say so. Unlike `pet_sensitivity`/`move_sensitivity`, whose 0..100 percentage
    really is continuous (NanoDet confidence and the luma-delta motion bar), this one is not,
    so it gets an honest seconds control instead, not a data-driven `SETTING_NUMBERS` entry
    (those pass the wire value straight through; this one converts both ways).

    Unavailable, rather than a bare `unknown`, when `eat_sensitivity` is missing from `GET
    /config` altogether. Mirrors `KibbleSettingNumber.available`."""

    _attr_translation_key = "eating_hold"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_native_min_value = 1
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "eating_hold")

    @property
    def available(self) -> bool:
        return super().available and "eat_sensitivity" in self.coordinator.data.config

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.data.config.get("eat_sensitivity")
        return None if value is None else float(eat_sensitivity_to_hold_s(int(value)))

    async def async_set_native_value(self, value: float) -> None:
        sensitivity = eat_hold_s_to_eat_sensitivity(int(value))
        try:
            await self.coordinator.async_set_config("eat_sensitivity", sensitivity)
        except KibbleError as err:
            raise_agent_action_failed("Set eating_hold", err)
