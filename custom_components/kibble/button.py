"""Feed and cancel buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import KibbleError
from .const import DOMAIN, HOPPER_BOTH, MAX_AMOUNT, MIN_AMOUNT
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([KibbleFeedButton(coordinator), KibbleCancelButton(coordinator)])


class KibbleFeedButton(KibbleEntity, ButtonEntity):
    """Dispense the amount currently set on the feed-amount control."""

    _attr_translation_key = "feed"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "feed")

    def _amount(self) -> int:
        """Read the companion number entity, defaulting to one portion."""
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "number", DOMAIN, f"{self.coordinator.data.serial}_feed_amount"
        )
        if entity_id and (state := self.hass.states.get(entity_id)) is not None:
            try:
                return max(MIN_AMOUNT, min(MAX_AMOUNT, int(float(state.state))))
            except ValueError:
                pass
        return MIN_AMOUNT

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_feed(HOPPER_BOTH, self._amount())
        except KibbleError as err:
            raise HomeAssistantError(f"Feed failed: {err}") from err


class KibbleCancelButton(KibbleEntity, ButtonEntity):
    """Stop a dispense that is in progress."""

    _attr_translation_key = "cancel_feed"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "cancel_feed")

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_cancel_feed()
        except KibbleError as err:
            raise HomeAssistantError(f"Cancel failed: {err}") from err
