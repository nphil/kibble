"""Polling coordinator for a Kibble feeder."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CloudState, FeederState, KibbleClient, KibbleError, ScheduleState, WifiNetwork, WifiState
from .ble_fallback import async_feed_with_fallback
from .const import CONF_BLE_ADDRESS, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type KibbleConfigEntry = ConfigEntry[KibbleCoordinator]


@dataclass(frozen=True, slots=True)
class KibbleData:
    """Everything one poll cycle fetches: feeder telemetry, the schedule cache, the live
    device-settings snapshot, the Petkit-cloud kill switch's status, and the current Wi-Fi
    association plus a fresh scan (`agent/src/wifi.rs`)."""

    state: FeederState
    schedule: ScheduleState
    config: dict[str, int]
    cloud: CloudState
    wifi: WifiState
    wifi_scan: tuple[WifiNetwork, ...]


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
        self.entry = entry
        # Which transport last actually carried (or was attempted for) a feed command; the
        # "Control path" diagnostic sensor reads this directly. `None` until the first feed.
        self.control_path: str | None = None

    async def _async_update_data(self) -> KibbleData:
        try:
            state = await self.client.state()
            schedule = await self.client.schedule()
            config = await self.client.config()
            cloud = await self.client.cloud()
            wifi = await self.client.wifi()
            wifi_scan = tuple(await self.client.wifi_scan())
            return KibbleData(
                state=state,
                schedule=schedule,
                config=config,
                cloud=cloud,
                wifi=wifi,
                wifi_scan=wifi_scan,
            )
        except KibbleError as err:
            raise UpdateFailed(str(err)) from err

    def _ble_feed(
        self, hopper: str, amount: int, feed_id: str | None
    ) -> Callable[[], Awaitable[None]] | None:
        """A zero-arg BLE feed attempt, or None if no `ble_address` is configured. Imports
        `.ble` lazily -- a feeder with no BLE fallback set up should never need bleak/
        Home Assistant's `bluetooth` component loaded just to feed over Wi-Fi."""
        address = self.entry.options.get(CONF_BLE_ADDRESS)
        if not address:
            return None

        async def _attempt() -> None:
            from .ble import async_feed as ble_async_feed

            await ble_async_feed(
                self.hass, address, hopper=hopper, amount=amount, feed_id=feed_id
            )

        return _attempt

    async def async_feed(self, hopper: str, amount: int, feed_id: str | None = None) -> None:
        """Dispense over Wi-Fi; on a connection error, fall back to BLE if `ble_address` is
        configured (`docs/25-ble-feed-frame.md`). Always refreshes afterwards and always
        records which transport was used/attempted on `self.control_path` -- the "Control
        path" sensor -- even when the call ultimately fails."""

        async def _wifi_feed() -> None:
            await self.client.feed(hopper, amount, feed_id)

        ble_feed = self._ble_feed(hopper, amount, feed_id)
        outcome = await async_feed_with_fallback(_wifi_feed, ble_feed)
        self.control_path = outcome.control_path
        self.async_update_listeners()
        await self.async_request_refresh()
        if outcome.error is not None:
            raise outcome.error

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

    async def async_set_cloud(self, enabled: bool) -> None:
        """Flip the Petkit-cloud kill switch. A disable can fail safe and roll itself back
        (`agent/src/cloud.rs`) -- this always refreshes, even when `set_cloud` raises, so the
        switch reflects the actual outcome (including a rollback's `last_error`) immediately
        instead of waiting for the next poll."""
        try:
            await self.client.set_cloud(enabled)
        finally:
            await self.async_request_refresh()

    async def async_wifi_connect(self, ssid: str, password: str | None = None) -> None:
        """Fail-safe Wi-Fi switch (`agent/src/wifi.rs`) -- always refreshes, even when
        `wifi_connect` raises, so a rollback's `last_error` and the (unchanged) live SSID
        appear immediately instead of waiting for the next poll. Mirrors `async_set_cloud`."""
        try:
            await self.client.wifi_connect(ssid, password)
        finally:
            await self.async_request_refresh()

    async def async_wifi_forget(self, ssid: str) -> None:
        await self.client.wifi_forget(ssid)
        await self.async_request_refresh()
