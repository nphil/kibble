"""The feeder's white status LED -- exactly one entity for it, though the wire has two
overlapping sources (this used to also be a `switch.py` entity, `light`, driving the exact
same physical LED through a strict subset of what this entity already does -- Nitin's own
report, "a status LED and a Status light entity ... its not clear what the difference is",
is exactly the confusion that duplicate caused) and, on the LibreFeed route, two independent
channels expressed as the effect list rather than a second entity (Nitin,
again: "maybe we could just select the color within the light entity instead of exposing a
separate green LED?"). `camera`, `/led`'s third field, is a different physical LED with a
privacy meaning and keeps its own entity (`select.KibbleCameraIndicatorSelect`)."""

from __future__ import annotations

import colorsys
from typing import Any

from homeassistant.components.light import ATTR_EFFECT, ColorMode, LightEntity, LightEntityFeature
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import KibbleError
from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity
from .errors import raise_agent_action_failed
from .stacks import applies_to

# Writes are coordinator-mediated and serialised by api.py's own lock; see coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0

# This LED is two independent binary channels, and that is the whole of it: `white` (which the
# firmware can also blink or fast-blink, or drive itself in `auto`) and `green` (plain on/off).
# There is no red channel, no blue channel and no dimming, so every state the hardware can be
# in is one of the nine enumerated below plus "off".
#
# This used to be modelled as `ColorMode.RGB` with a quantiser mapping any requested colour to
# the nearest achievable pair. The quantiser was honest, but the entity was not: HA renders a
# full colour wheel for `ColorMode.RGB`, so the UI offered purple on a green-and-white LED and
# silently did something else (Nitin, 2026-09-19). An interface must not invite an action the
# device cannot take -- so colour is gone, and the achievable combinations are the effect list.
_EFFECT_AUTO = "auto"

# `GET /led`'s forced-`white` values: 0 off, 1 on, 2 blink, 3 fast blink; "auto" means the
# firmware drives the channel itself.
_WHITE_MODE_NAMES: dict[str | int, str] = {
    _EFFECT_AUTO: "auto",
    1: "white",
    2: "white blink",
    3: "white fast blink",
}


def _effect_name(white: str | int, green: int) -> str | None:
    """The effect string for one real `(white, green)` hardware state, or `None` for "off"
    (both channels dark -- `is_on` already says that, and HA has no effect for it).

    Total by construction: every reachable combination has a name, including ones this entity
    never writes itself (a caller poking `POST /led` directly can leave the LED in green +
    auto-white, and the entity must report that rather than a blank)."""
    white_name = _WHITE_MODE_NAMES.get(white)
    if green:
        return "green" if white_name is None else f"green + {white_name}"
    return white_name


# Effect string -> the `(white, green)` pair written for it. Built from the same table that
# reads state back, so the two can never drift out of agreement.
_STATE_BY_EFFECT: dict[str, tuple[str | int, int]] = {
    name: (white, green)
    for green in (0, 1)
    for white in (_EFFECT_AUTO, 1, 2, 3)
    if (name := _effect_name(white, green)) is not None
}
_STATE_BY_EFFECT["green"] = (0, 1)

# Ordered for the UI: plain white modes, then green, then the combinations.
_EFFECT_LIST = [
    _effect_name(_EFFECT_AUTO, 0),
    _effect_name(1, 0),
    _effect_name(2, 0),
    _effect_name(3, 0),
    "green",
    _effect_name(_EFFECT_AUTO, 1),
    _effect_name(1, 1),
    _effect_name(2, 1),
    _effect_name(3, 1),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if applies_to(Platform.LIGHT, "status_light", coordinator.data.detected_stack):
        async_add_entities([KibbleStatusLight(coordinator)])


class KibbleStatusLight(KibbleEntity, LightEntity):
    """The feeder's status LED -- one entity, two possible wire sources, chosen per poll,
    never both surfaced at once.

    LibreFeed's `GET`/`POST /led` is the full source when present (`_using_led` True). The
    hardware is two independent binary channels that can both be lit at once: `white` (which
    additionally has its own auto/on/blink/fast-blink modes -- see `effect`) and `green`
    (plain `0`/`1`). Rather than a second entity for `green`, both channels are expressed as
    this light's effect list: every combination the hardware can actually be in has exactly one
    effect, and nothing else is offered.

    | Effect | Wire state |
    |---|---|
    | `auto` / `white` / `white blink` / `white fast blink` | `{"white": auto\|1\|2\|3, "green": 0}` |
    | `green` | `{"white": 0, "green": 1}` |
    | `green + auto` / `green + white` / `green + white blink` / `green + white fast blink` | `{"white": auto\|1\|2\|3, "green": 1}` |

    This was a `ColorMode.RGB` picker until 2026-09-19, which made HA render a colour wheel on
    a two-channel green-and-white LED: the quantiser behind it was honest, but the UI invited
    purple and then did something else. Effects describe what the device can do and nothing it
    cannot, so the wheel is gone along with `rgb_color`.

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
        # On/off on BOTH stacks: the LED has no colour axis a user can steer, only the
        # discrete combinations in `effect_list`.
        return {ColorMode.ONOFF}

    @property
    def color_mode(self) -> ColorMode:
        return ColorMode.ONOFF

    @property
    def supported_features(self) -> LightEntityFeature:
        # Only `/led` can force blink/fast-blink/auto -- the `config["light"]` fallback is a
        # plain boolean, so effects are never on offer through it.
        return LightEntityFeature.EFFECT if self._using_led else LightEntityFeature(0)

    @property
    def effect_list(self) -> list[str] | None:
        return list(_EFFECT_LIST) if self._using_led else None

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
        led = self.coordinator.data.led
        return _effect_name(led.white, led.green)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if not self._using_led:
            await self._async_write_config(1)
            return
        effect = kwargs.get(ATTR_EFFECT)
        if effect is not None:
            # An effect names BOTH channels, so it is written as a pair: picking "green" must
            # actually turn the white channel off, or the LED ends up in a state the effect
            # list does not describe.
            white, green = _STATE_BY_EFFECT[effect]
            try:
                await self.coordinator.async_set_led(white=white, green=green)
            except KibbleError as err:
                raise_agent_action_failed("Set status light", err)
            return
        led = self.coordinator.data.led
        if led.white != 0 or led.green != 0:
            return
        try:
            await self.coordinator.async_set_led(white=1)
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
