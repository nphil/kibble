"""Polling coordinator for a Kibble feeder."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import FeederState, KibbleClient, KibbleError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type KibbleConfigEntry = ConfigEntry[KibbleCoordinator]


class KibbleCoordinator(DataUpdateCoordinator[FeederState]):
    """Keeps one feeder's state fresh."""

    def __init__(
        self, hass: HomeAssistant, entry: KibbleConfigEntry, client: KibbleClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client

    async def _async_update_data(self) -> FeederState:
        try:
            return await self.client.state()
        except KibbleError as err:
            raise UpdateFailed(str(err)) from err

    async def async_feed(self, hopper: str, amount: int, feed_id: str | None = None) -> None:
        """Dispense, then refresh so the feeding flag appears without waiting for the poll."""
        await self.client.feed(hopper, amount, feed_id)
        await self.async_request_refresh()

    async def async_cancel_feed(self) -> None:
        await self.client.cancel_feed()
        await self.async_request_refresh()
