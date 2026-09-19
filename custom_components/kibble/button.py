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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import KibbleError
from .const import DOMAIN, HOPPER_1, HOPPER_2, HOPPER_BOTH, MAX_AMOUNT, MIN_AMOUNT
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity
from .errors import raise_agent_action_failed
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    stack = coordinator.data.detected_stack
    entities: list[ButtonEntity] = [KibbleFeedButton(coordinator, d) for d in FEEDS]
    entities.append(KibbleCancelButton(coordinator))
    if applies_to(Platform.BUTTON, "beep", stack):
        entities.append(KibbleBeepButton(coordinator))
    if applies_to(Platform.BUTTON, "replace_desiccant", stack):
        entities.append(KibbleReplaceDesiccantButton(coordinator))
    async_add_entities(entities)


class KibbleFeedButton(KibbleEntity, ButtonEntity):
    """Dispense the amount set on this button's companion amount control."""

    entity_description: KibbleFeedDescription

    def __init__(self, coordinator, description: KibbleFeedDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    def _amount(self) -> int:
        """Read the companion number entity, falling back to one portion."""
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "number",
            DOMAIN,
            f"{self.coordinator.data.state.serial}_{self.entity_description.amount_key}",
        )
        if entity_id and (state := self.hass.states.get(entity_id)) is not None:
            try:
                return max(MIN_AMOUNT, min(MAX_AMOUNT, int(float(state.state))))
            except ValueError:
                pass
        return MIN_AMOUNT

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_feed(
                self.entity_description.hopper, self._amount()
            )
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
