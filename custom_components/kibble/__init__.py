"""Kibble — local control for Petkit YumShare Dual feeders."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KibbleClient, KibbleError
from .const import (
    ATTR_AMOUNT,
    ATTR_FEED_ID,
    ATTR_HOPPER,
    CONF_HOST,
    CONF_PORT,
    DOMAIN,
    HOPPER_BOTH,
    HOPPERS,
    MAX_AMOUNT,
    MIN_AMOUNT,
    SERVICE_CANCEL_FEED,
    SERVICE_FEED,
)
from .coordinator import KibbleConfigEntry, KibbleCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
]

FEED_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional(ATTR_HOPPER, default=HOPPER_BOTH): vol.In(HOPPERS),
        vol.Required(ATTR_AMOUNT): vol.All(
            vol.Coerce(int), vol.Range(min=MIN_AMOUNT, max=MAX_AMOUNT)
        ),
        vol.Optional(ATTR_FEED_ID): cv.string,
    }
)

CANCEL_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: KibbleConfigEntry) -> bool:
    """Set up one feeder."""
    client = KibbleClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT]
    )
    coordinator = KibbleCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KibbleConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _coordinator_for_device(hass: HomeAssistant, device_id: str) -> KibbleCoordinator:
    """Resolve a service call's target device to its coordinator."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        registry = er.async_get(hass)
        entries = er.async_entries_for_config_entry(registry, entry.entry_id)
        if any(e.device_id == device_id for e in entries):
            return entry.runtime_data
    raise HomeAssistantError(f"{device_id} is not a Kibble feeder")


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_FEED):
        return

    async def handle_feed(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_feed(
                call.data[ATTR_HOPPER], call.data[ATTR_AMOUNT], call.data.get(ATTR_FEED_ID)
            )
        except KibbleError as err:
            raise HomeAssistantError(f"Feed failed: {err}") from err

    async def handle_cancel(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_cancel_feed()
        except KibbleError as err:
            raise HomeAssistantError(f"Cancel failed: {err}") from err

    hass.services.async_register(DOMAIN, SERVICE_FEED, handle_feed, FEED_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CANCEL_FEED, handle_cancel, CANCEL_SCHEMA)
