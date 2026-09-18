"""Status LED for the feeder (`GET`/`POST /led`; LibreFeed-only -- see `api.py`'s `LedState`
and `_request`'s `not_found_is_missing`)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_EFFECT, ColorMode, LightEntity, LightEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import KibbleError
from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity
from .errors import raise_agent_action_failed

# Writes are coordinator-mediated and serialised by api.py's own lock; see coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0

_EFFECT_AUTO = "auto"
_EFFECT_ON = "on"
_EFFECT_BLINK = "blink"
_EFFECT_FAST = "fast"

# Maps GET /led's forced-`white` integers (0 off, 1 on, 2 blink, 3 fast blink) to this entity's
# `effect` strings; "auto" is handled separately since it is never one of the forced integers.
_EFFECT_BY_WHITE = {1: _EFFECT_ON, 2: _EFFECT_BLINK, 3: _EFFECT_FAST}
_WHITE_BY_EFFECT = {_EFFECT_ON: 1, _EFFECT_BLINK: 2, _EFFECT_FAST: 3}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([KibbleStatusLight(coordinator)])


class KibbleStatusLight(KibbleEntity, LightEntity):
    """The feeder's status LED (`GET`/`POST /led`, LibreFeed-only -- unavailable, not broken,
    on the vendor stack where this route 404s; see `coordinator.py`'s `_fetch_all` storing
    `None` for `data.led` on exactly that 404, the same pattern `select.py`'s
    `KibbleStackSelect` uses for `GET /mode`).

    `white` is either the device's own automatic policy (`"auto"`) or a forced override (`0`
    off, `1` on, `2` blink, `3` fast blink); `effect` exposes that override -- including
    `"auto"` itself, so the UI can show which regime is active, not just whether the LED is
    lit. `is_on` treats `"auto"` as on: the device is actively managing the LED, not dark.
    `green`, the second LED element, is attribute-only for now -- not enough of a control
    surface on its own to earn a second entity."""

    _attr_translation_key = "status_light"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = [_EFFECT_AUTO, _EFFECT_ON, _EFFECT_BLINK, _EFFECT_FAST]

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "status_light")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.led is not None

    @property
    def is_on(self) -> bool | None:
        led = self.coordinator.data.led
        return None if led is None else led.white != 0

    @property
    def effect(self) -> str | None:
        led = self.coordinator.data.led
        if led is None:
            return None
        return _EFFECT_AUTO if led.white == "auto" else _EFFECT_BY_WHITE.get(led.white)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        led = self.coordinator.data.led
        return {} if led is None else {"green": bool(led.green)}

    async def async_turn_on(self, **kwargs: Any) -> None:
        effect = kwargs.get(ATTR_EFFECT)
        if effect is not None:
            white: str | int = _EFFECT_AUTO if effect == _EFFECT_AUTO else _WHITE_BY_EFFECT[effect]
        else:
            led = self.coordinator.data.led
            if led is not None and led.white != 0:
                return
            white = 1
        try:
            await self.coordinator.async_set_led(white=white)
        except KibbleError as err:
            raise_agent_action_failed("Set status light", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.async_set_led(white=0)
        except KibbleError as err:
            raise_agent_action_failed("Set status light", err)
