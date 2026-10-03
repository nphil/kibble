"""Feed, cancel and beep buttons.

One feed button per auger plus a combined one. The augers are independent motors — the feed
payload carries a separate amount byte for each — so they are separately controllable regardless
of whether the physical hopper divider is fitted. The beep button (LibreFeed-only, see
`light.py`'s `KibbleStatusLight`) plays the MCU buzzer once with the agent's own defaults.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import KibbleCueCooldownError, KibbleError
from .const import DOMAIN, HOPPER_1, HOPPER_2, HOPPER_BOTH, MAX_AMOUNT, MIN_AMOUNT, MIN_HOPPER_AMOUNT
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity, async_when_data_ready
from .errors import raise_agent_action_failed, raise_cue_cooldown
from .stacks import applies_to

# Writes are coordinator-mediated and serialised by api.py's own lock; see coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class KibbleFeedDescription(ButtonEntityDescription):
    """A feed button, the hopper it runs and the amount entity it reads."""

    hopper: str
    amount_key: str


FEEDS: tuple[KibbleFeedDescription, ...] = (
    KibbleFeedDescription(
        key="feed", translation_key="feed", hopper=HOPPER_BOTH, amount_key="feed_amount"
    ),
    KibbleFeedDescription(
        key="feed_hopper_1",
        translation_key="feed_hopper_1",
        hopper=HOPPER_1,
        amount_key="feed_amount_hopper_1",
    ),
    KibbleFeedDescription(
        key="feed_hopper_2",
        translation_key="feed_hopper_2",
        hopper=HOPPER_2,
        amount_key="feed_amount_hopper_2",
    ),
)


@dataclass(frozen=True, kw_only=True)
class KibbleMarkHopperFullDescription(ButtonEntityDescription):
    """A "mark as full" button and which hopper it applies to."""

    hopper: str


MARK_HOPPER_FULL: tuple[KibbleMarkHopperFullDescription, ...] = (
    KibbleMarkHopperFullDescription(key="hopper_1_full", translation_key="hopper_1_full", hopper=HOPPER_1),
    KibbleMarkHopperFullDescription(key="hopper_2_full", translation_key="hopper_2_full", hopper=HOPPER_2),
    KibbleMarkHopperFullDescription(key="hopper_full", translation_key="hopper_full", hopper=HOPPER_BOTH),
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
    entities: list[ButtonEntity] = [KibbleFeedButton(coordinator, d) for d in FEEDS]
    entities.append(KibbleCancelButton(coordinator))
    if applies_to(Platform.BUTTON, "beep", stack):
        entities.append(KibbleBeepButton(coordinator))
    if applies_to(Platform.BUTTON, "call_cats", stack):
        entities.append(KibbleCallCatsButton(coordinator))
    if applies_to(Platform.BUTTON, "replace_desiccant", stack):
        entities.append(KibbleReplaceDesiccantButton(coordinator))
    entities.extend(
        KibbleMarkHopperFullButton(coordinator, d)
        for d in MARK_HOPPER_FULL
        if applies_to(Platform.BUTTON, d.key, stack)
    )
    async_add_entities(entities)


class KibbleFeedButton(KibbleEntity, ButtonEntity):
    """Dispense the amount set on this button's companion amount control.

    The combined button (`feed`, hopper="both") reads `feed_amount`, whose own floor stays
    `MIN_AMOUNT` (number.py's `KibbleFeedAmount.native_min_value`) -- it can never resolve to
    0. The two per-hopper buttons (`feed_hopper_1`/`_2`) read `feed_amount_hopper_1`/`_2`,
    which now allow 0 ("nothing from this hopper", set from kibble-card.ts's dual-hopper hero)
    -- `async_press` refuses outright rather than silently clamping that up to one portion or
    sending the device a 0-portion feed."""

    entity_description: KibbleFeedDescription

    def __init__(self, coordinator, description: KibbleFeedDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    def _floor(self) -> int:
        """The combined button always means "dispense something"; a per-hopper button may
        legitimately be asked to dispense nothing at all -- see the class docstring."""
        return MIN_AMOUNT if self.entity_description.hopper == HOPPER_BOTH else MIN_HOPPER_AMOUNT

    def _amount(self) -> int:
        """Read the companion number entity, clamped to this button's own floor (see
        `_floor`) and `MAX_AMOUNT`. Falls back to the floor itself if the entity is missing or
        its state doesn't parse as a number."""
        floor = self._floor()
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "number",
            DOMAIN,
            f"{self.coordinator.data.state.serial}_{self.entity_description.amount_key}",
        )
        if entity_id and (state := self.hass.states.get(entity_id)) is not None:
            try:
                return max(floor, min(MAX_AMOUNT, int(float(state.state))))
            except ValueError:
                pass
        return floor

    async def async_press(self) -> None:
        amount = self._amount()
        if amount == 0:
            # Only reachable for a per-hopper button (`_floor` above) -- the combined button's
            # own floor never lets this resolve to 0. Never silently a no-op: a clear, translated
            # error so a dashboard/automation that presses this button finds out immediately
            # rather than assuming a feed happened.
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="feed_button_zero_amount"
            )
        try:
            await self.coordinator.async_feed(self.entity_description.hopper, amount)
        except KibbleError as err:
            raise_agent_action_failed("Feed", err)


class KibbleCancelButton(KibbleEntity, ButtonEntity):
    """Stop a dispense that is in progress."""

    _attr_translation_key = "cancel_feed"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "cancel_feed")

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_cancel_feed()
        except KibbleError as err:
            raise_agent_action_failed("Cancel", err)


class KibbleBeepButton(KibbleEntity, ButtonEntity):
    """Play the MCU buzzer once, with the agent's own default count/timing (`POST /beep`,
    LibreFeed-only -- unavailable, not broken, on the vendor stack, same `led`-presence
    marker `light.py`'s `KibbleStatusLight` uses: the buzzer has no polled state of its own
    to gate on)."""

    _attr_translation_key = "beep"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "beep")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.led is not None

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_beep()
        except KibbleError as err:
            raise_agent_action_failed("Beep", err)


class KibbleCallCatsButton(KibbleEntity, ButtonEntity):
    """Plays the feed cue on demand ("call the cats", `POST /cue`, LibreFeed-only -- same
    `led`-presence marker `KibbleBeepButton` uses: this action has no polled state of its own
    to gate on either). Does not dispense food. A press within `speaker::CALL_COOLDOWN` of the
    previous one is rejected by the agent as a `KibbleCueCooldownError`, surfaced with its own
    translated message rather than the generic action-failed one."""

    _attr_translation_key = "call_cats"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "call_cats")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.led is not None

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_call_cats()
        except KibbleCueCooldownError as err:
            raise_cue_cooldown(err)
        except KibbleError as err:
            raise_agent_action_failed("Call the cats", err)


class KibbleReplaceDesiccantButton(KibbleEntity, ButtonEntity):
    """Mark the desiccant pack as replaced (`POST /desiccant {"replaced": true}`,
    LibreFeed-only -- unavailable, not broken, on the vendor stack, whose equivalent counter
    is set from Petkit's cloud config, not the agent; see `api.py`'s `DesiccantState`)."""

    _attr_translation_key = "replace_desiccant"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "replace_desiccant")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.desiccant is not None

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_set_desiccant(replaced=True)
        except KibbleError as err:
            raise_agent_action_failed("Replace desiccant", err)


class KibbleMarkHopperFullButton(KibbleEntity, ButtonEntity):
    """Tells the daemon this hopper (or both) was just physically refilled to capacity,
    resetting its full/since-full counters so `sensor.*_hopper_N_remaining` starts counting
    down from a known point again (LibreFeed-only -- see `stacks.py`)."""

    entity_description: KibbleMarkHopperFullDescription

    def __init__(self, coordinator, description: KibbleMarkHopperFullDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_mark_hopper_full(self.entity_description.hopper)
        except KibbleError as err:
            raise_agent_action_failed("Mark hopper full", err)
