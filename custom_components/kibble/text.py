"""Text controls for the feeder's three writable time-of-day range settings.

Each of `detect_range_from`/`_till` (`detection_hours`), `light_range_from`/`_till`
(`status_led_hours`), and `tone_range_from`/`_till` (`do_not_disturb_hours`) used to be two
separate `number.py` entities holding a raw minutes-since-midnight integer -- see that
module's docstring for what replaced them and why. Home Assistant has no native time-*range*
entity type, so a single `text` entity with a validated `"HH:MM-HH:MM"` pattern is the honest
one-control replacement: editable as one field, and the only shape that lets a user set both
ends in one write instead of two number entities that can independently drift out of sync
with each other between writes (Nitin: "Can we consolidate the 'detection hours end' and
detection hours start to one entity called detection hours and I just select the range for
detection? Same for all the other entities with a start and end").

Minutes since local midnight is the device's own encoding for every pair here. `from == till`
means "always active" -- the device's own convention, represented honestly below as e.g.
`"00:00-00:00"` rather than an invented magic word the device has no concept of. `from > till`
is a legitimate window crossing midnight (the vendor's own `toneMultiRange` documents exactly
this: "1320-360 wraps midnight"), so nothing here enforces `from <= till`; `start_minute`/
`end_minute` attributes expose the raw values for automations that need numeric access without
parsing the display string back apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from homeassistant.components.text import TextEntity, TextEntityDescription, TextMode
from homeassistant.const import EntityCategory, Platform, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api import KibbleError
from .const import MAX_HOPPER_FOOD_LENGTH
from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity, async_when_data_ready
from .errors import raise_agent_action_failed
from .stacks import applies_to

# Writes are coordinator-mediated and serialised by api.py's own lock; see coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0

# Exactly "HH:MM-HH:MM", each half a valid 24-hour time -- the format has no variable-width
# component, so `native_min`/`native_max` are both fixed at this length too.
_HOUR_RANGE_LENGTH = 11
_HOUR_RANGE_PATTERN = r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$"
_HOUR_RANGE_RE = re.compile(_HOUR_RANGE_PATTERN)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_hour_range(from_minutes: int, till_minutes: int) -> str:
    """The device's raw `from`/`till` minutes-since-midnight pair -> the displayed
    `"HH:MM-HH:MM"` string. `from == till` (the device's own convention for "always active")
    is rendered exactly like any other pair -- e.g. `"00:00-00:00"` -- never replaced with a
    word the device itself has no concept of."""
    return f"{_hhmm(from_minutes)}-{_hhmm(till_minutes)}"


def parse_hour_range(value: str) -> tuple[int, int]:
    """The inverse of `format_hour_range`. Raises `ValueError` for anything not exactly
    `"HH:MM-HH:MM"` with each half a valid 24-hour time -- `TextEntityDescription.pattern` is
    a UI-side hint, not a substitute for validating what this function actually receives (the
    `text.set_value` action's own service-level check is a separate, earlier gate; a caller
    invoking `async_set_value` directly bypasses it entirely)."""
    match = _HOUR_RANGE_RE.match(value)
    if match is None:
        raise ValueError(f"{value!r} is not a valid HH:MM-HH:MM range")
    from_hour, from_minute, till_hour, till_minute = (int(group) for group in match.groups())
    return from_hour * 60 + from_minute, till_hour * 60 + till_minute


@dataclass(frozen=True, kw_only=True)
class KibbleHourRangeTextDescription(TextEntityDescription):
    """One `from`/`till` device-setting pair (`/config`, minutes-since-midnight), rendered as
    a single `"HH:MM-HH:MM"` text control instead of two number entities."""

    from_key: str
    till_key: str


# The three time-of-day windows LibreFeed's `/config` exposes as `from`/`till` minute pairs.
# Each used to be two `number.py` entities (`_from`/`_till`); consolidated here into one
# `text` entity per window -- see the module docstring for why.
HOUR_RANGES: tuple[KibbleHourRangeTextDescription, ...] = (
    KibbleHourRangeTextDescription(
        key="detection_hours",
        translation_key="detection_hours",
        from_key="detect_range_from",
        till_key="detect_range_till",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        native_min=_HOUR_RANGE_LENGTH,
        native_max=_HOUR_RANGE_LENGTH,
        pattern=_HOUR_RANGE_PATTERN,
        mode=TextMode.TEXT,
    ),
    KibbleHourRangeTextDescription(
        key="status_led_hours",
        translation_key="status_led_hours",
        from_key="light_range_from",
        till_key="light_range_till",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        native_min=_HOUR_RANGE_LENGTH,
        native_max=_HOUR_RANGE_LENGTH,
        pattern=_HOUR_RANGE_PATTERN,
        mode=TextMode.TEXT,
    ),
    KibbleHourRangeTextDescription(
        key="do_not_disturb_hours",
        translation_key="do_not_disturb_hours",
        from_key="tone_range_from",
        till_key="tone_range_till",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        native_min=_HOUR_RANGE_LENGTH,
        native_max=_HOUR_RANGE_LENGTH,
        pattern=_HOUR_RANGE_PATTERN,
        mode=TextMode.TEXT,
    ),
)


@dataclass(frozen=True, kw_only=True)
class KibbleHopperFoodTextDescription(TextEntityDescription):
    """A local food-name label for one hopper -- meaningful only with the divider fitted (see
    `KibbleHopperFoodText`)."""

    hopper: int


# Local-only, never written to the feeder (docs/37-hopper-full.md) -- both stacks apply (no
# `ENTITY_STACKS` row for either key, so `applies_to` below defaults them to `_BOTH`).
HOPPER_FOODS: tuple[KibbleHopperFoodTextDescription, ...] = (
    KibbleHopperFoodTextDescription(
        key="hopper_1_food",
        translation_key="hopper_1_food",
        hopper=1,
        native_max=MAX_HOPPER_FOOD_LENGTH,
        mode=TextMode.TEXT,
    ),
    KibbleHopperFoodTextDescription(
        key="hopper_2_food",
        translation_key="hopper_2_food",
        hopper=2,
        native_max=MAX_HOPPER_FOOD_LENGTH,
        mode=TextMode.TEXT,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_when_data_ready(entry, lambda: _add_entities(entry, async_add_entities))


def _add_entities(
    entry: KibbleConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    stack = coordinator.data.detected_stack
    entities: list[TextEntity] = [
        KibbleHourRangeText(coordinator, d) for d in HOUR_RANGES if applies_to(Platform.TEXT, d.key, stack)
    ]
    entities.extend(
        KibbleHopperFoodText(coordinator, d) for d in HOPPER_FOODS if applies_to(Platform.TEXT, d.key, stack)
    )
    async_add_entities(entities)


class KibbleHourRangeText(KibbleEntity, TextEntity):
    """One `from`/`till` minutes-since-midnight device-setting pair, read from and written to
    the feeder's shared config through the agent's `/config` endpoint, rendered as a single
    `"HH:MM-HH:MM"` text control.

    Unavailable, rather than a bare `unknown`, when *either* backing key is missing from `GET
    /config` altogether -- a range with only one half known is not a real value. Mirrors
    `switch.py`'s `KibbleSettingSwitch.available`.

    Setting a new value parses both halves and writes both `/config` keys as two separate
    `async_set_config` calls -- there is no batched multi-key write in this API. If the second
    write fails after the first already succeeded, that failure is surfaced exactly like any
    other rejected write (`raise_agent_action_failed`) rather than silently leaving the window
    half-applied; the coordinator's own refresh (inside `async_set_config`) still picks up
    whichever key(s) the agent actually accepted, so `native_value` reflects the real,
    possibly inconsistent, on-device state rather than the value the caller asked for.

    `start_minute`/`end_minute` attributes expose the raw minutes-since-midnight values so
    automations keep numeric access without parsing the display string back apart. `from ==
    till` means "always active" (the device's own convention) and is shown as-is, e.g.
    `"00:00-00:00"` -- never replaced with a magic word the device has no concept of. `from >
    till` is a legitimate window crossing midnight (e.g. `"22:00-07:00"`), interpreted the
    same way the vendor's own `toneMultiRange` documents ("1320-360 wraps midnight"), so
    nothing here enforces `from <= till`.
    """

    entity_description: KibbleHourRangeTextDescription

    def __init__(
        self, coordinator: KibbleCoordinator, description: KibbleHourRangeTextDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    def _minutes(self) -> tuple[int, int] | None:
        config = self.coordinator.data.config
        from_value = config.get(self.entity_description.from_key)
        till_value = config.get(self.entity_description.till_key)
        if from_value is None or till_value is None:
            return None
        return int(from_value), int(till_value)

    @property
    def available(self) -> bool:
        return super().available and self._minutes() is not None

    @property
    def native_value(self) -> str | None:
        minutes = self._minutes()
        return None if minutes is None else format_hour_range(*minutes)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        minutes = self._minutes()
        if minutes is None:
            return None
        start_minute, end_minute = minutes
        return {"start_minute": start_minute, "end_minute": end_minute}

    async def async_set_value(self, value: str) -> None:
        try:
            from_minutes, till_minutes = parse_hour_range(value)
        except ValueError as err:
            raise_agent_action_failed(f"Set {self.entity_description.key}", err)
        description = self.entity_description
        try:
            # Both halves are written through the client directly, with ONE refresh afterwards,
            # rather than two `async_set_config` calls that each refresh: the interim refresh
            # publishes a half-applied window (setting "22:30-07:15" briefly rendered
            # "22:30-00:00" on a live feeder, because the `till` write had not landed yet), which
            # reads as a bug even though it settles a moment later. A failure on the second write
            # still surfaces -- and still leaves the window half-applied on the device, which is
            # why the error names the whole range rather than one key.
            await self.coordinator.client.set_config(description.from_key, from_minutes)
            await self.coordinator.client.set_config(description.till_key, till_minutes)
        except KibbleError as err:
            raise_agent_action_failed(f"Set {self.entity_description.key}", err)
        finally:
            await self.coordinator.async_request_refresh()


class KibbleHopperFoodText(KibbleEntity, TextEntity, RestoreEntity):
    """What is loaded in one hopper, as the user typed it -- local only, never sent to the
    feeder, and meaningful only with the divider fitted: two compartments can hold different
    foods, one shared bin cannot (docs/37-hopper-full.md). Restored across restarts and pushed
    onto the coordinator (`KibbleCoordinator.async_set_hopper_food`) the moment it is known --
    mirrors `switch.py`'s `KibbleHopperDividerSwitch` pushing `single_hopper` the same way --
    so ingest can freeze it onto a feed row the instant that row is first recorded.

    An empty string means unnamed and is always a valid value; there is no requirement to name
    either hopper."""

    entity_description: KibbleHopperFoodTextDescription

    def __init__(self, coordinator, description: KibbleHopperFoodTextDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._value = ""

    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self._value = last.state[: self.entity_description.native_max]
        self.coordinator.async_set_hopper_food(self.entity_description.hopper, self._value)

    @property
    def native_value(self) -> str:
        return self._value

    async def async_set_value(self, value: str) -> None:
        self._value = value
        self.coordinator.async_set_hopper_food(self.entity_description.hopper, value)
        self.async_write_ha_state()
