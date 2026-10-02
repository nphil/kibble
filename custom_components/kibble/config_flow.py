"""Config flow for Kibble."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector
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
    CONF_STREAM_ENTITY,
    CONF_CORALHUB_TOKEN,
    CONF_CORALHUB_URL,
    CONF_ENABLE_SCHEDULE_WRITES,
    CONF_HOST,
    CONF_PORT,
    CONF_STREAM_URL,
    CONF_RETENTION_DAYS,
    CONF_SCRYPTED_CLIPS_URL,
    CONF_VISION_JUDGE_MODEL,
    CONF_VISION_JUDGE_URL,
    DEFAULT_PORT,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_VISION_JUDGE_MODEL,
    DOMAIN,
    RETENTION_OPTIONS,
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
    """Video source, BLE fallback address, the schedule-card write gate, HA-side evidence
    retention, the Scrypted eating-clips server and the vision judge -- all filled in after
    initial setup.

    `stream_entity`: another camera entity that already carries this feeder's video -- one
    published by Scrypted, Frigate, go2rtc, anything. Preferred over `stream_url` because HA
    re-resolves it every time it is asked, so a rebroadcast port reassigned on restart is
    picked up with no reconfiguration (a pinned URL is not: Scrypted's rebroadcast port is
    ephemeral, and the camera went black exactly that way on 2026-09-19).

    `stream_url`: a fixed RTSP URL, for a genuinely stable source.

    Both empty is a first-class configuration, not a fallback for the unlucky: the camera then
    streams straight from the feeder. **No video hub is required to use this integration.**

    `ble_address`: the feeder's BLE MAC, once a Bluetooth proxy has actually seen it advertise
    (`docs/25-ble-feed-frame.md`). Empty = `kibble.feed` reports "unreachable" instead of
    trying a BLE fallback when the agent's HTTP API can't be reached.

    `enable_schedule_writes`: off by default. The MCU's per-entry schedule time encoding is
    still unconfirmed (docs/schedule.md) -- until an operator flips this on deliberately, the
    `schedule_card_add`/`_edit`/`_remove`/`_toggle` services refuse to write anything.

    `retention_days`: how long HA keeps archived evidence, the event journal and training
    (docs/36-ai-pipeline.md) -- applied to the HA store only, on the next purge (startup and
    hourly); there is no feeder-side call, since the feeder itself keeps no long-term history
    at all.

    `scrypted_clips_url`: Scrypted's own HTTP origin, e.g. `http://192.168.1.69:11080`
    (`docs/39-eating-clips.md`). Empty (the default) turns the whole eating-clips feature off:
    no lookups, no retries, no clip ever offered on the timeline. Stored with any trailing
    slash removed, so `eating_clips.py`'s URL builders never have to guard against a doubled
    slash. Changing it reloads the entry (`_async_options_updated`), same as every other
    option here -- a stale `ClipLinker` pointed at the old value never lingers.

    `vision_judge_url`: the judge model's own OpenAI-compatible base URL, e.g.
    `http://192.168.1.69:9292/v1` (`docs/40-vision-judge.md`). Empty (the default) turns the
    whole second-opinion-judge feature off: no requests, no description-filling, no verdict
    ever recorded -- same "empty means off" shape as `scrypted_clips_url`, and stored the same
    way (trailing slash removed). `vision_judge_model`: the model alias to request; defaults to
    `qwen3-vl-4b`, the bake-off's own persistent llama-swap model id.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            url = (user_input.get(CONF_STREAM_URL) or "").strip()
            stream_entity = (user_input.get(CONF_STREAM_ENTITY) or "").strip()
            address = (user_input.get(CONF_BLE_ADDRESS) or "").strip().upper()
            enable_schedule_writes = bool(user_input.get(CONF_ENABLE_SCHEDULE_WRITES, False))
            clips_url = (user_input.get(CONF_SCRYPTED_CLIPS_URL) or "").strip().rstrip("/")
            judge_url = (user_input.get(CONF_VISION_JUDGE_URL) or "").strip().rstrip("/")
            judge_model = (user_input.get(CONF_VISION_JUDGE_MODEL) or "").strip() or DEFAULT_VISION_JUDGE_MODEL
            coralhub_url = (user_input.get(CONF_CORALHUB_URL) or "").strip().rstrip("/")
            coralhub_token = (user_input.get(CONF_CORALHUB_TOKEN) or "").strip()
            try:
                retention_days = int(user_input.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS))
            except (TypeError, ValueError):
                retention_days = DEFAULT_RETENTION_DAYS
            errors: dict[str, str] = {}
            if url and not url.startswith(("rtsp://", "rtsps://")):
                errors[CONF_STREAM_URL] = "not_rtsp"
            if address and not _MAC_RE.match(address):
                errors[CONF_BLE_ADDRESS] = "not_mac"
            if retention_days not in RETENTION_OPTIONS:
                errors[CONF_RETENTION_DAYS] = "invalid_retention"
            if clips_url and not clips_url.startswith(("http://", "https://")):
                errors[CONF_SCRYPTED_CLIPS_URL] = "not_http"
            if judge_url and not judge_url.startswith(("http://", "https://")):
                errors[CONF_VISION_JUDGE_URL] = "not_http"
            if coralhub_url and not coralhub_url.startswith(("http://", "https://")):
                errors[CONF_CORALHUB_URL] = "not_http"
            if errors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(
                        url, stream_entity, address, enable_schedule_writes, retention_days,
                        clips_url, judge_url, judge_model, coralhub_url, coralhub_token,
                    ),
                    errors=errors,
                )
            return self.async_create_entry(
                data={
                    CONF_STREAM_ENTITY: stream_entity,
                    CONF_STREAM_URL: url,
                    CONF_BLE_ADDRESS: address,
                    CONF_ENABLE_SCHEDULE_WRITES: enable_schedule_writes,
                    CONF_RETENTION_DAYS: retention_days,
                    CONF_SCRYPTED_CLIPS_URL: clips_url,
                    CONF_VISION_JUDGE_URL: judge_url,
                    CONF_VISION_JUDGE_MODEL: judge_model,
                    CONF_CORALHUB_URL: coralhub_url,
                    CONF_CORALHUB_TOKEN: coralhub_token,
                }
            )
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(
                options.get(CONF_STREAM_URL, ""),
                options.get(CONF_STREAM_ENTITY, ""),
                options.get(CONF_BLE_ADDRESS, ""),
                options.get(CONF_ENABLE_SCHEDULE_WRITES, False),
                options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS),
                options.get(CONF_SCRYPTED_CLIPS_URL, ""),
                options.get(CONF_VISION_JUDGE_URL, ""),
                options.get(CONF_VISION_JUDGE_MODEL, DEFAULT_VISION_JUDGE_MODEL),
                options.get(CONF_CORALHUB_URL, ""),
                options.get(CONF_CORALHUB_TOKEN, ""),
            ),
        )

    @staticmethod
    def _schema(
        stream_url: str,
        stream_entity: str,
        ble_address: str,
        enable_schedule_writes: bool,
        retention_days: int,
        scrypted_clips_url: str,
        vision_judge_url: str,
        vision_judge_model: str,
        coralhub_url: str,
        coralhub_token: str,
    ) -> vol.Schema:
        return vol.Schema(
            {
                # Entity first: it is the source that survives a video hub restarting, and the
                # one most installs should use. `stream_url` stays for a genuinely fixed URL,
                # and leaving BOTH empty is a first-class configuration -- the feeder's own
                # stream, no hub required.
                vol.Optional(CONF_STREAM_ENTITY, default=stream_entity): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="camera")
                ),
                vol.Optional(CONF_STREAM_URL, default=stream_url): str,
                vol.Optional(CONF_BLE_ADDRESS, default=ble_address): str,
                vol.Optional(
                    CONF_ENABLE_SCHEDULE_WRITES, default=enable_schedule_writes
                ): bool,
                vol.Required(CONF_RETENTION_DAYS, default=retention_days): vol.In(RETENTION_OPTIONS),
                vol.Optional(CONF_SCRYPTED_CLIPS_URL, default=scrypted_clips_url): str,
                vol.Optional(CONF_VISION_JUDGE_URL, default=vision_judge_url): str,
                vol.Optional(CONF_VISION_JUDGE_MODEL, default=vision_judge_model): str,
                vol.Optional(CONF_CORALHUB_URL, default=coralhub_url): str,
                vol.Optional(
                    CONF_CORALHUB_TOKEN, default=coralhub_token
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
