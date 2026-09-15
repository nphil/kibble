"""Feed and cancel buttons.

One feed button per auger plus a combined one. The augers are independent motors — the feed
payload carries a separate amount byte for each — so they are separately controllable regardless
of whether the physical hopper divider is fitted.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import KibbleError
from .const import DOMAIN, HOPPER_1, HOPPER_2, HOPPER_BOTH, MAX_AMOUNT, MIN_AMOUNT
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity
from .errors import raise_agent_action_failed

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
    entities: list[ButtonEntity] = [KibbleFeedButton(coordinator, d) for d in FEEDS]
    entities.append(KibbleCancelButton(coordinator))
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
