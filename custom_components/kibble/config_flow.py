"""Config flow for Kibble."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KibbleClient, KibbleConnectionError, KibbleError
from .const import CONF_HOST, CONF_PORT, DEFAULT_PORT, DOMAIN

SCHEMA = vol.Schema(
    {vol.Required(CONF_HOST): str, vol.Optional(CONF_PORT, default=DEFAULT_PORT): int}
)


class KibbleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the feeder's address and prove the agent answers on it."""

    VERSION = 1

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
