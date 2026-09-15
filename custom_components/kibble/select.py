"""Wi-Fi network selection for Kibble."""

from __future__ import annotations

from homeassistant.components.persistent_notification import async_create
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import KibbleError
from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([KibbleWifiSelect(entry.runtime_data)])


class KibbleWifiSelect(KibbleEntity, SelectEntity):
    """Options are the feeder's own scan (`GET /wifi/scan`), refreshed every coordinator poll;
    the current SSID is always included so it can render as selected even if a given scan
    doesn't currently see it (weak signal, mid-roam, ...).

    `GET /wifi` exposes no list of which SSIDs the agent already holds a password for, only the
    *current* SSID and the *desired* one (`wifi.json` -- see `agent/src/wifi.rs`); those are the
    only two SSIDs this entity can be sure a direct reconnect will work for, since a `psk`-less
    `POST /wifi/connect` only succeeds by reusing an existing Kibble-managed network entry.
    Selecting either of those reconnects immediately. Anything else has no known password to
    send -- a `select` cannot prompt for one inline -- so this fires a persistent notification
    pointing at the `kibble.wifi_connect` service instead of guessing.
    """

    _attr_translation_key = "wifi"

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "wifi")

    @property
    def options(self) -> list[str]:
        options = [network.ssid for network in self.coordinator.data.wifi_scan]
        current = self.coordinator.data.wifi.ssid
        if current and current not in options:
            options.append(current)
        return options

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.wifi.ssid

    async def async_select_option(self, option: str) -> None:
        wifi = self.coordinator.data.wifi
        if option not in (wifi.ssid, wifi.desired_ssid):
            async_create(
                self.hass,
                (
                    f'Kibble doesn\'t have a saved password for "{option}" yet. Use the '
                    "**Kibble: Connect to Wi-Fi network** action (Developer tools \u2192 "
                    "Actions, or an automation) with the password, then select it here again."
                ),
                title="Kibble Wi-Fi",
                notification_id=f"{self.unique_id}_needs_password",
            )
            return
        try:
            await self.coordinator.async_wifi_connect(option)
        except KibbleError as err:
            raise HomeAssistantError(f"Connect to {option} failed: {err}") from err
