"""The feeder's white status LED -- exactly one entity for it, though the wire has two
overlapping sources (this used to also be a `switch.py` entity, `light`, driving the exact
same physical LED through a strict subset of what this entity already does -- Nitin's own
report, "a status LED and a Status light entity ... its not clear what the difference is",
is exactly the confusion that duplicate caused) and, on the LibreFeed route, two independent
channels folded into one `ColorMode.RGB` colour picker rather than a second entity (Nitin,
again: "maybe we could just select the color within the light entity instead of exposing a
separate green LED?"). `camera`, `/led`'s third field, is a different physical LED with a
privacy meaning and keeps its own entity (`select.KibbleCameraIndicatorSelect`)."""

from __future__ import annotations

import colorsys
from typing import Any

from homeassistant.components.light import ATTR_EFFECT, ATTR_RGB_COLOR, ColorMode, LightEntity, LightEntityFeature
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

# The only three colours this LED can actually produce, and what `rgb_color` reports for each
# -- read back from real `/led` state, never from the last-requested colour, so a user who
# asks for an unachievable colour sees what the device actually did. `_RGB_BOTH` is a paler
# green than `_RGB_GREEN` alone: white and green mixing raises the apparent lightness while
# diluting the saturation, which is also why it is the nearest achievable match for most
# colours this LED cannot reproduce at all (see `_quantize_rgb` below).
_RGB_WHITE = (255, 255, 255)
_RGB_GREEN = (0, 255, 0)
_RGB_BOTH = (170, 255, 170)

# `_quantize_rgb`'s thresholds, in HSV terms (`colorsys.rgb_to_hsv`'s 0..1 scale). Below
# `_SATURATION_WHITE_MAX` a colour is indistinguishable from grey/white to the eye regardless
# of hue -- pretending a faint tint is achievable would be a lie in the other direction.
# `_GREEN_HUE_MIN`/`_MAX` (75deg..165deg) is this LED's green band; at or above
# `_SATURATION_GREEN_MIN` inside that band a colour is unambiguously green. Everything else --
# any hue this LED has no channel for at all (red, blue, purple, ...), or a green too washed
# out to call "green-ish" -- quantises to both channels, the visually brightest and closest
# overall achievable combination.
_SATURATION_WHITE_MAX = 0.15
_SATURATION_GREEN_MIN = 0.5
_GREEN_HUE_MIN = 75 / 360
_GREEN_HUE_MAX = 165 / 360


def _quantize_rgb(rgb: tuple[int, int, int]) -> tuple[int, int]:
    """A requested `rgb_color` -> the nearest achievable `(white, green)` forced-value pair.
    See the module-level threshold constants above for the exact table; this is deliberately
    a free function so the mapping is unit-testable without an entity or coordinator."""
    hue, saturation, _ = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
    if saturation < _SATURATION_WHITE_MAX:
        return (1, 0)
    if saturation >= _SATURATION_GREEN_MIN and _GREEN_HUE_MIN <= hue <= _GREEN_HUE_MAX:
        return (0, 1)
    return (1, 1)


def _rgb_for_channels(white_on: bool, green_on: bool) -> tuple[int, int, int] | None:
    """The inverse direction: which real, `/led`-confirmed channel combination -> which of
    the three achievable colours `rgb_color` reports. `None` when neither channel is lit --
    honest silence, not a fabricated "last colour" for a light that is currently off."""
    if white_on and green_on:
        return _RGB_BOTH
    if white_on:
        return _RGB_WHITE
    if green_on:
        return _RGB_GREEN
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([KibbleStatusLight(coordinator)])


class KibbleStatusLight(KibbleEntity, LightEntity):
    """The feeder's status LED -- one entity, two possible wire sources, chosen per poll,
    never both surfaced at once.

    LibreFeed's `GET`/`POST /led` is the full source when present (`_using_led` True). The
    hardware is two independent binary channels that can both be lit at once: `white` (which
    additionally has its own auto/on/blink/fast-blink modes -- see `effect`) and `green`
    (plain `0`/`1`). Rather than a second entity for `green`, this light offers `ColorMode.RGB`
    and quantises any requested colour to the nearest combination the hardware can actually
    produce -- there is no red or blue channel, so most colours are not achievable as
    requested:

    | Requested colour | Forced write | Reported `rgb_color` |
    |---|---|---|
    | grey/white (saturation < 0.15) | `{"white": 1, "green": 0}` | `(255, 255, 255)` |
    | green-ish (saturation >= 0.5, hue in the green band) | `{"white": 0, "green": 1}` | `(0, 255, 0)` |
    | anything else (red, blue, purple, a washed-out green, ...) | `{"white": 1, "green": 1}` | `(170, 255, 170)` |

    `rgb_color` is always read back from the real `/led` state (`_rgb_for_channels`), never
    from the last-requested colour, so asking for purple honestly shows the pale green both
    channels together actually produce, not a lie that purple "worked". `effect` -- the white
    channel's own auto/on/blink/fast-blink modes -- is orthogonal to colour and unaffected by
    it: setting an effect never touches `green`, and picking a colour never touches whichever
    forced/auto mode `white` was already in beyond what the colour itself requires.

    The vendor stack serves no `/led` route at all (`coordinator.py`'s `_fetch_all` stores
    `None` for `data.led` on exactly that 404); its only lever for this LED is the plain
    boolean `light` device setting (`GET`/`POST /config`), which the agent's own
    `persist_setting` (`daemon/src/compat.rs`) turns into `led auto` or `led 0` on the MCU.
    This entity falls back to `config["light"]` in exactly that case (`_using_led` False):
    plain on/off, no colour, no effects -- that fallback is what used to be a second,
    overlapping `switch.py` entity (`SWITCHES`' old `light` key), removed in the same change
    that added this fallback, so there is exactly one entity per physical LED again, on
    either stack.

    `available` is true if *either* source is present. Every property/write below branches
    on `_using_led` explicitly, rather than leaving the choice implicit."""

    _attr_translation_key = "status_light"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "status_light")

    @property
    def _using_led(self) -> bool:
        """Whether `/led` is this poll's live source (LibreFeed). `False` means the vendor
        stack, and `config["light"]` is the fallback for every property/write below."""
        return self.coordinator.data.led is not None

    @property
    def available(self) -> bool:
        return super().available and (self._using_led or "light" in self.coordinator.data.config)

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        return {ColorMode.RGB} if self._using_led else {ColorMode.ONOFF}

    @property
    def color_mode(self) -> ColorMode:
        return ColorMode.RGB if self._using_led else ColorMode.ONOFF

    @property
    def supported_features(self) -> LightEntityFeature:
        # Only `/led` can force blink/fast-blink/auto -- the `config["light"]` fallback is a
        # plain boolean, so effects are never on offer through it.
        return LightEntityFeature.EFFECT if self._using_led else LightEntityFeature(0)

    @property
    def effect_list(self) -> list[str] | None:
        return [_EFFECT_AUTO, _EFFECT_ON, _EFFECT_BLINK, _EFFECT_FAST] if self._using_led else None

    @property
    def is_on(self) -> bool | None:
        if self._using_led:
            led = self.coordinator.data.led
            return led.white != 0 or led.green != 0
        value = self.coordinator.data.config.get("light")
        return None if value is None else bool(value)

    @property
    def effect(self) -> str | None:
        if not self._using_led:
            return None
        white = self.coordinator.data.led.white
        return _EFFECT_AUTO if white == "auto" else _EFFECT_BY_WHITE.get(white)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        if not self._using_led:
            return None
        led = self.coordinator.data.led
        return _rgb_for_channels(led.white != 0, led.green != 0)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if not self._using_led:
            await self._async_write_config(1)
            return
        rgb = kwargs.get(ATTR_RGB_COLOR)
        if rgb is not None:
            target_white, target_green = _quantize_rgb(rgb)
            try:
                await self.coordinator.async_set_led(white=target_white, green=target_green)
            except KibbleError as err:
                raise_agent_action_failed("Set status light", err)
            return
        effect = kwargs.get(ATTR_EFFECT)
        if effect is not None:
            white: str | int = _EFFECT_AUTO if effect == _EFFECT_AUTO else _WHITE_BY_EFFECT[effect]
        else:
            led = self.coordinator.data.led
            if led.white != 0:
                return
            white = 1
        try:
            await self.coordinator.async_set_led(white=white)
        except KibbleError as err:
            raise_agent_action_failed("Set status light", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if not self._using_led:
            await self._async_write_config(0)
            return
        try:
            # Both channels off -- otherwise a lit `green` alone would leave `is_on` true
            # right after a caller asked this entity to turn off.
            await self.coordinator.async_set_led(white=0, green=0)
        except KibbleError as err:
            raise_agent_action_failed("Set status light", err)

    async def _async_write_config(self, value: int) -> None:
        try:
            await self.coordinator.async_set_config("light", value)
        except KibbleError as err:
            raise_agent_action_failed("Set status light", err)
