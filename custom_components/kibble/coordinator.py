"""Polling coordinator for a Kibble feeder."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import FeederState, KibbleClient, KibbleError, ScheduleState
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type KibbleConfigEntry = ConfigEntry[KibbleCoordinator]


@dataclass(frozen=True, slots=True)
class KibbleData:
    """Everything one poll cycle fetches: feeder telemetry, the schedule cache, and the
    live device-settings snapshot."""

    state: FeederState
    schedule: ScheduleState
    config: dict[str, int]


class KibbleCoordinator(DataUpdateCoordinator[KibbleData]):
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

    async def _async_update_data(self) -> KibbleData:
        try:
            state = await self.client.state()
            schedule = await self.client.schedule()
            config = await self.client.config()
            return KibbleData(state=state, schedule=schedule, config=config)
        except KibbleError as err:
            raise UpdateFailed(str(err)) from err

    async def async_feed(self, hopper: str, amount: int, feed_id: str | None = None) -> None:
        """Dispense, then refresh so the feeding flag appears without waiting for the poll."""
        await self.client.feed(hopper, amount, feed_id)
        await self.async_request_refresh()

    async def async_cancel_feed(self) -> None:
        await self.client.cancel_feed()
        await self.async_request_refresh()

    async def async_set_config(self, key: str, value: int) -> None:
        """Write one device setting, then refresh so the new value reflects immediately."""
        await self.client.set_config(key, value)
        await self.async_request_refresh()

    async def async_schedule_set(self, entries: list[dict[str, Any]]) -> None:
        await self.client.set_schedule(entries)
        await self.async_request_refresh()

    async def async_schedule_add(
        self, time: str, amount_l: int, amount_r: int, enabled: bool = True
    ) -> None:
        await self.client.add_schedule_entry(time, amount_l, amount_r, enabled)
        await self.async_request_refresh()

    async def async_schedule_remove(self, entry_id: str) -> None:
        await self.client.remove_schedule_entry(entry_id)
        await self.async_request_refresh()

    async def async_schedule_set_enabled(self, entry_id: str, enabled: bool) -> None:
        await self.client.set_schedule_entry_enabled(entry_id, enabled)
        await self.async_request_refresh()
