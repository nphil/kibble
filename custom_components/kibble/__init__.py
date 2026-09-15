"""Kibble — local control for Petkit YumShare Dual feeders."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KibbleClient, KibbleError
from .const import (
    ATTR_AMOUNT,
    ATTR_CAT,
    ATTR_CAT_NAME,
    ATTR_CROP_ID,
    ATTR_ENABLED,
    ATTR_ENTRIES,
    ATTR_ENTRY_ID,
    ATTR_FEED_ID,
    ATTR_HOPPER,
    ATTR_HOPPER1_G,
    ATTR_HOPPER2_G,
    ATTR_PASSWORD,
    ATTR_SSID,
    ATTR_TIME,
    CONF_HOST,
    CONF_PORT,
    DOMAIN,
    HOPPER_BOTH,
    HOPPERS,
    MAX_AMOUNT,
    MAX_SCHEDULE_AMOUNT,
    MAX_SCHEDULE_ENTRIES,
    MIN_AMOUNT,
    MIN_SCHEDULE_AMOUNT,
    SERVICE_ADD_CAT,
    SERVICE_CANCEL_FEED,
    SERVICE_FEED,
    SERVICE_IDENTIFY,
    SERVICE_LABEL_FACE,
    SERVICE_SCHEDULE_ADD,
    SERVICE_SCHEDULE_REMOVE,
    SERVICE_SCHEDULE_SET,
    SERVICE_SCHEDULE_SET_ENABLED,
    SERVICE_WIFI_CONNECT,
)
from .coordinator import KibbleConfigEntry, KibbleCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
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

_SCHEDULE_AMOUNT = vol.All(
    vol.Coerce(int), vol.Range(min=MIN_SCHEDULE_AMOUNT, max=MAX_SCHEDULE_AMOUNT)
)

SCHEDULE_ENTRY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TIME): cv.time,
        vol.Required(ATTR_HOPPER1_G): _SCHEDULE_AMOUNT,
        vol.Required(ATTR_HOPPER2_G): _SCHEDULE_AMOUNT,
        vol.Optional(ATTR_ENABLED, default=True): cv.boolean,
        vol.Optional(ATTR_ENTRY_ID): cv.string,
    }
)

SCHEDULE_SET_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required(ATTR_ENTRIES): vol.All(
            cv.ensure_list, [SCHEDULE_ENTRY_SCHEMA], vol.Length(max=MAX_SCHEDULE_ENTRIES)
        ),
    }
)

SCHEDULE_ADD_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required(ATTR_TIME): cv.time,
        vol.Required(ATTR_HOPPER1_G): _SCHEDULE_AMOUNT,
        vol.Required(ATTR_HOPPER2_G): _SCHEDULE_AMOUNT,
        vol.Optional(ATTR_ENABLED, default=True): cv.boolean,
    }
)

SCHEDULE_REMOVE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required(ATTR_ENTRY_ID): cv.string,
    }
)

SCHEDULE_SET_ENABLED_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required(ATTR_ENTRY_ID): cv.string,
        vol.Required(ATTR_ENABLED): cv.boolean,
    }
)

WIFI_CONNECT_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required(ATTR_SSID): cv.string,
        vol.Optional(ATTR_PASSWORD): cv.string,
    }
)

LABEL_FACE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required(ATTR_CROP_ID): cv.string,
        vol.Required(ATTR_CAT): cv.string,
    }
)

ADD_CAT_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required(ATTR_CAT_NAME): cv.string,
    }
)

IDENTIFY_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})


def _entry_payload(entry: dict) -> dict:
    """Translates one validated schedule-entry dict (HA vocabulary) into kibbled's wire
    vocabulary: `hopper1_g`/`hopper2_g` -> `amount_l`/`amount_r`, `time` as `datetime.time` ->
    `"HH:MM"`."""
    payload = {
        "time": entry[ATTR_TIME].strftime("%H:%M"),
        "amount_l": entry[ATTR_HOPPER1_G],
        "amount_r": entry[ATTR_HOPPER2_G],
        "enabled": entry[ATTR_ENABLED],
    }
    if ATTR_ENTRY_ID in entry:
        payload["id"] = entry[ATTR_ENTRY_ID]
    return payload


async def async_setup_entry(hass: HomeAssistant, entry: KibbleConfigEntry) -> bool:
    """Set up one feeder."""
    client = KibbleClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT]
    )
    coordinator = KibbleCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
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
        except Exception as err:
            # Broader than the other handlers on purpose: a failed feed can now come from
            # either transport (KibbleError from Wi-Fi, or the BLE fallback's own exception
            # type from `custom_components/kibble/ble.py`) -- see `async_feed_with_fallback`.
            # The cause is preserved (`from err`) so the specific failure is still in the log.
            raise HomeAssistantError(f"Feed failed: {err}") from err

    async def handle_cancel(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_cancel_feed()
        except KibbleError as err:
            raise HomeAssistantError(f"Cancel failed: {err}") from err

    async def handle_schedule_set(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        entries = [_entry_payload(e) for e in call.data[ATTR_ENTRIES]]
        try:
            await coordinator.async_schedule_set(entries)
        except KibbleError as err:
            raise HomeAssistantError(f"Schedule replace failed: {err}") from err

    async def handle_schedule_add(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_schedule_add(
                call.data[ATTR_TIME].strftime("%H:%M"),
                call.data[ATTR_HOPPER1_G],
                call.data[ATTR_HOPPER2_G],
                call.data[ATTR_ENABLED],
            )
        except KibbleError as err:
            raise HomeAssistantError(f"Schedule add failed: {err}") from err

    async def handle_schedule_remove(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_schedule_remove(call.data[ATTR_ENTRY_ID])
        except KibbleError as err:
            raise HomeAssistantError(f"Schedule remove failed: {err}") from err

    async def handle_schedule_set_enabled(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_schedule_set_enabled(
                call.data[ATTR_ENTRY_ID], call.data[ATTR_ENABLED]
            )
        except KibbleError as err:
            raise HomeAssistantError(f"Schedule set-enabled failed: {err}") from err

    async def handle_wifi_connect(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_wifi_connect(
                call.data[ATTR_SSID], call.data.get(ATTR_PASSWORD)
            )
        except KibbleError as err:
            raise HomeAssistantError(f"Wi-Fi connect failed: {err}") from err

    async def handle_label_face(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_label_face(call.data[ATTR_CROP_ID], call.data[ATTR_CAT])
        except KibbleError as err:
            raise HomeAssistantError(f"Label face failed: {err}") from err

    async def handle_add_cat(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_add_cat(call.data[ATTR_CAT_NAME])
        except KibbleError as err:
            raise HomeAssistantError(f"Add cat failed: {err}") from err

    async def handle_identify(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            result = await coordinator.async_identify_now()
        except KibbleError as err:
            raise HomeAssistantError(f"Identify failed: {err}") from err
        return {
            "cat": result.cat,
            "score": result.score,
            "second_best": (
                {"cat": result.second_best.cat, "score": result.second_best.score}
                if result.second_best
                else None
            ),
            "crop": result.crop,
            "source": result.source,
        }

    hass.services.async_register(DOMAIN, SERVICE_FEED, handle_feed, FEED_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CANCEL_FEED, handle_cancel, CANCEL_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_SCHEDULE_SET, handle_schedule_set, SCHEDULE_SET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SCHEDULE_ADD, handle_schedule_add, SCHEDULE_ADD_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SCHEDULE_REMOVE, handle_schedule_remove, SCHEDULE_REMOVE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCHEDULE_SET_ENABLED,
        handle_schedule_set_enabled,
        SCHEDULE_SET_ENABLED_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_WIFI_CONNECT, handle_wifi_connect, WIFI_CONNECT_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_LABEL_FACE, handle_label_face, LABEL_FACE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ADD_CAT, handle_add_cat, ADD_CAT_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_IDENTIFY,
        handle_identify,
        IDENTIFY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def _async_options_updated(hass: HomeAssistant, entry: KibbleConfigEntry) -> None:
    """The stream source lives in options; re-create the camera entity with the new one."""
    await hass.config_entries.async_reload(entry.entry_id)
