"""Config flow for Kibble."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KibbleClient, KibbleConnectionError, KibbleError
from .const import (
    CONF_BLE_ADDRESS,
    CONF_ENABLE_SCHEDULE_WRITES,
    CONF_HOST,
    CONF_PORT,
    CONF_STREAM_URL,
    DEFAULT_PORT,
    DOMAIN,
)

SCHEMA = vol.Schema(
    {vol.Required(CONF_HOST): str, vol.Optional(CONF_PORT, default=DEFAULT_PORT): int}
)

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


class KibbleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the feeder's address and prove the agent answers on it."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KibbleOptionsFlow:
        return KibbleOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = KibbleClient(
                async_get_clientsession(self.hass),
                user_input[CONF_HOST],
                user_input[CONF_PORT],
            )
            try:
                state = await client.state()
            except KibbleConnectionError:
                errors["base"] = "cannot_connect"
            except KibbleError:
                errors["base"] = "unknown"
            else:
                # The feeder's serial is stable across reboots and address changes.
                await self.async_set_unique_id(state.serial)
                self._abort_if_unique_id_configured(updates=dict(user_input))
                return self.async_create_entry(title="Cat Feeder", data=user_input)

        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)


class KibbleOptionsFlow(OptionsFlow):
    """Video source, BLE fallback address, and the schedule-card write gate -- all filled in
    after initial setup.

    `stream_url`: the feeder serves video to one consumer only (Scrypted); this is the
    rebroadcast URL the camera entity consumes instead of hitting the device a second time.
    Empty = use the device.

    `ble_address`: the feeder's BLE MAC, once a Bluetooth proxy has actually seen it advertise
    (`docs/25-ble-feed-frame.md`). Empty = `kibble.feed` reports "unreachable" instead of
    trying a BLE fallback when the agent's HTTP API can't be reached.

    `enable_schedule_writes`: off by default. The MCU's per-entry schedule time encoding is
    still unconfirmed (docs/schedule.md) -- until an operator flips this on deliberately, the
    `schedule_card_add`/`_edit`/`_remove`/`_toggle` services refuse to write anything.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            url = (user_input.get(CONF_STREAM_URL) or "").strip()
            address = (user_input.get(CONF_BLE_ADDRESS) or "").strip().upper()
            enable_schedule_writes = bool(user_input.get(CONF_ENABLE_SCHEDULE_WRITES, False))
            errors: dict[str, str] = {}
            if url and not url.startswith(("rtsp://", "rtsps://")):
                errors[CONF_STREAM_URL] = "not_rtsp"
            if address and not _MAC_RE.match(address):
                errors[CONF_BLE_ADDRESS] = "not_mac"
            if errors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(url, address, enable_schedule_writes),
                    errors=errors,
                )
            return self.async_create_entry(
                data={
                    CONF_STREAM_URL: url,
                    CONF_BLE_ADDRESS: address,
                    CONF_ENABLE_SCHEDULE_WRITES: enable_schedule_writes,
                }
            )

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(
                options.get(CONF_STREAM_URL, ""),
                options.get(CONF_BLE_ADDRESS, ""),
                options.get(CONF_ENABLE_SCHEDULE_WRITES, False),
            ),
        )

    @staticmethod
    def _schema(stream_url: str, ble_address: str, enable_schedule_writes: bool) -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional(CONF_STREAM_URL, default=stream_url): str,
                vol.Optional(CONF_BLE_ADDRESS, default=ble_address): str,
                vol.Optional(
                    CONF_ENABLE_SCHEDULE_WRITES, default=enable_schedule_writes
                ): bool,
            }
        )
