"""Kibble — local control for Petkit YumShare Dual feeders."""

from __future__ import annotations

import logging
import re

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KibbleClient, KibbleError, KibbleSpeakerBusyError
from .const import (
    ATTR_AMOUNT,
    ATTR_CAT,
    ATTR_CAT_NAME,
    ATTR_CLIP_NAME,
    ATTR_CROP_ID,
    ATTR_ENABLED,
    ATTR_ENTRIES,
    ATTR_ENTRY_ID,
    ATTR_FEED_ID,
    ATTR_HOPPER,
    ATTR_HOPPER1_G,
    ATTR_HOPPER2_G,
    ATTR_HOUR,
    ATTR_ID,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MINUTE,
    ATTR_PASSWORD,
    ATTR_SECONDS,
    ATTR_SSID,
    ATTR_TIME,
    CONF_HOST,
    CONF_PORT,
    DOMAIN,
    HOPPER_BOTH,
    HOPPERS,
    MAX_AMOUNT,
    MAX_CLIP_SECONDS,
    MAX_SCHEDULE_AMOUNT,
    MAX_SCHEDULE_ENTRIES,
    MIN_AMOUNT,
    MIN_CLIP_SECONDS,
    MIN_SCHEDULE_AMOUNT,
    SERVICE_ADD_CAT,
    SERVICE_CANCEL_FEED,
    SERVICE_FEED,
    SERVICE_IDENTIFY,
    SERVICE_LABEL_FACE,
    SERVICE_PLAY_CLIP,
    SERVICE_RECORD_CLIP,
    SERVICE_SAVE_CLIP,
    SERVICE_SCHEDULE_ADD,
    SERVICE_SCHEDULE_CARD_ADD,
    SERVICE_SCHEDULE_CARD_EDIT,
    SERVICE_SCHEDULE_CARD_REMOVE,
    SERVICE_SCHEDULE_CARD_TOGGLE,
    SERVICE_SCHEDULE_REMOVE,
    SERVICE_SCHEDULE_SET,
    SERVICE_SCHEDULE_SET_ENABLED,
    SERVICE_WIFI_CONNECT,
)
from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .errors import raise_agent_action_failed, raise_speaker_busy

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.IMAGE,
    Platform.MEDIA_PLAYER,
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

_SCHEDULE_CARD_HOUR = vol.All(vol.Coerce(int), vol.Range(min=0, max=23))
_SCHEDULE_CARD_MINUTE = vol.All(vol.Coerce(int), vol.Range(min=0, max=59))
_SCHEDULE_CARD_AMOUNT = vol.All(vol.Coerce(int), vol.Range(min=MIN_AMOUNT, max=MAX_AMOUNT))

SCHEDULE_CARD_ADD_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required(ATTR_ID): cv.string,
        vol.Required(ATTR_HOUR): _SCHEDULE_CARD_HOUR,
        vol.Required(ATTR_MINUTE): _SCHEDULE_CARD_MINUTE,
        vol.Required(ATTR_AMOUNT): _SCHEDULE_CARD_AMOUNT,
    }
)

SCHEDULE_CARD_EDIT_SCHEMA = SCHEDULE_CARD_ADD_SCHEMA

SCHEDULE_CARD_REMOVE_SCHEMA = vol.Schema(
    {vol.Required("device_id"): cv.string, vol.Required(ATTR_ID): cv.string}
)

SCHEDULE_CARD_TOGGLE_SCHEMA = SCHEDULE_CARD_REMOVE_SCHEMA

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

_CLIP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _valid_clip_name(value: str) -> str:
    """Mirrors `agent/src/clips.rs`'s `valid_name` so a bad name fails fast in HA with a clear
    voluptuous error instead of a generic agent 400."""
    if value in (".", "..") or not _CLIP_NAME_RE.match(value):
        raise vol.Invalid(
            "must be 1-64 characters: letters, digits, '-', '_', '.' only, and not '.' or '..'"
        )
    return value


SAVE_CLIP_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required(ATTR_CLIP_NAME): vol.All(cv.string, _valid_clip_name),
        vol.Required(ATTR_MEDIA_CONTENT_ID): cv.string,
    }
)

RECORD_CLIP_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required(ATTR_CLIP_NAME): vol.All(cv.string, _valid_clip_name),
        vol.Required(ATTR_SECONDS): vol.All(
            vol.Coerce(float), vol.Range(min=MIN_CLIP_SECONDS, max=MAX_CLIP_SECONDS)
        ),
    }
)

PLAY_CLIP_SCHEMA = vol.Schema(
    {vol.Required("device_id"): cv.string, vol.Required(ATTR_CLIP_NAME): cv.string}
)


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


async def _async_forward_platforms_isolated(
    hass: HomeAssistant, entry: KibbleConfigEntry, platforms: list[Platform]
) -> list[Platform]:
    """Forwards each platform in `platforms` on its own, isolated `async_forward_entry_setups`
    call, returning whichever ones actually loaded.

    One call per platform -- not one call for the whole list -- deliberately. HA's own
    implementation batches every platform passed to a single call under one
    `asyncio.gather(...)` with no `return_exceptions=True`; a single-element list is unaffected
    by (and just as efficient as) that batching, but it means one bad platform (a bad import, a
    bug in its `async_setup_entry`) fails only that call, not a `gather` shared with every other
    platform. This is the isolation defect #2 (a removed `UnitOfSignalStrength` import taking
    down all 51 entities) needed and did not have. A platform that fails is logged and skipped;
    the rest still load -- see `docs/30-quality-scale-audit.md` for exactly what is, and is
    not, achievable here.
    """
    loaded: list[Platform] = []
    for platform in platforms:
        try:
            await hass.config_entries.async_forward_entry_setups(entry, [platform])
        except Exception:  # noqa: BLE001 -- deliberately broad, see docstring
            _LOGGER.exception(
                "Setting up the %s platform failed; other Kibble platforms still loaded",
                platform,
            )
        else:
            loaded.append(platform)
    return loaded


async def async_setup_entry(hass: HomeAssistant, entry: KibbleConfigEntry) -> bool:
    """Set up one feeder.

    Platform setup is isolated per-platform by `_async_forward_platforms_isolated` (see its own
    docstring). If every platform fails there is nothing this entry usefully provides, so that
    case still fails setup outright.

    The coordinator's own first refresh, above, is deliberately NOT isolated the same way: with
    no data fetched yet, no platform has anything to show, so splitting that one failure nine
    ways would not add any real isolation -- it would just spread one "nothing works yet"
    outcome across nine try/except blocks. Its failure already raises `ConfigEntryNotReady`
    (via `async_config_entry_first_refresh`), which is the correct, standard signal either way.
    """
    client = KibbleClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT]
    )
    coordinator = KibbleCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    loaded = await _async_forward_platforms_isolated(hass, entry, PLATFORMS)
    coordinator.loaded_platforms = loaded
    if not loaded:
        raise ConfigEntryNotReady("No Kibble platform could be set up; see the log above")

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KibbleConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(
        entry, entry.runtime_data.loaded_platforms
    )


def _coordinator_for_device(hass: HomeAssistant, device_id: str) -> KibbleCoordinator:
    """Resolve a service call's target device to its coordinator."""
    registry = er.async_get(hass)
    for entry in hass.config_entries.async_entries(DOMAIN):
        entries = er.async_entries_for_config_entry(registry, entry.entry_id)
        if any(e.device_id == device_id for e in entries):
            return entry.runtime_data
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_device",
        translation_placeholders={"device_id": device_id},
    )


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
            raise_agent_action_failed("Feed", err)

    async def handle_cancel(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_cancel_feed()
        except KibbleError as err:
            raise_agent_action_failed("Cancel", err)

    async def handle_schedule_set(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        entries = [_entry_payload(e) for e in call.data[ATTR_ENTRIES]]
        try:
            await coordinator.async_schedule_set(entries)
        except KibbleError as err:
            raise_agent_action_failed("Schedule replace", err)

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
            raise_agent_action_failed("Schedule add", err)

    async def handle_schedule_remove(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_schedule_remove(call.data[ATTR_ENTRY_ID])
        except KibbleError as err:
            raise_agent_action_failed("Schedule remove", err)

    async def handle_schedule_set_enabled(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_schedule_set_enabled(
                call.data[ATTR_ENTRY_ID], call.data[ATTR_ENABLED]
            )
        except KibbleError as err:
            raise_agent_action_failed("Schedule set-enabled", err)

    async def handle_schedule_card_add(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        time_str = f"{call.data[ATTR_HOUR]:02d}:{call.data[ATTR_MINUTE]:02d}"
        try:
            await coordinator.async_schedule_card_add(
                call.data[ATTR_ID], time_str, call.data[ATTR_AMOUNT]
            )
        except KibbleError as err:
            raise_agent_action_failed("Schedule card add", err)

    async def handle_schedule_card_edit(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        time_str = f"{call.data[ATTR_HOUR]:02d}:{call.data[ATTR_MINUTE]:02d}"
        try:
            await coordinator.async_schedule_card_edit(
                call.data[ATTR_ID], time_str, call.data[ATTR_AMOUNT]
            )
        except KibbleError as err:
            raise_agent_action_failed("Schedule card edit", err)

    async def handle_schedule_card_remove(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_schedule_card_remove(call.data[ATTR_ID])
        except KibbleError as err:
            raise_agent_action_failed("Schedule card remove", err)

    async def handle_schedule_card_toggle(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_schedule_card_toggle(call.data[ATTR_ID])
        except KibbleError as err:
            raise_agent_action_failed("Schedule card toggle", err)

    async def handle_wifi_connect(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_wifi_connect(
                call.data[ATTR_SSID], call.data.get(ATTR_PASSWORD)
            )
        except KibbleError as err:
            raise_agent_action_failed("Wi-Fi connect", err)

    async def handle_label_face(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_label_face(call.data[ATTR_CROP_ID], call.data[ATTR_CAT])
        except KibbleError as err:
            raise_agent_action_failed("Label face", err)

    async def handle_add_cat(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_add_cat(call.data[ATTR_CAT_NAME])
        except KibbleError as err:
            raise_agent_action_failed("Add cat", err)

    async def handle_identify(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            result = await coordinator.async_identify_now()
        except KibbleError as err:
            raise_agent_action_failed("Identify", err)
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

    async def handle_save_clip(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_save_clip(
                call.data[ATTR_CLIP_NAME], call.data[ATTR_MEDIA_CONTENT_ID]
            )
        except KibbleError as err:
            raise_agent_action_failed("Save clip", err)

    async def handle_record_clip(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_record_clip(
                call.data[ATTR_CLIP_NAME], call.data[ATTR_SECONDS]
            )
        except KibbleError as err:
            raise_agent_action_failed("Record clip", err)

    async def handle_play_clip(call: ServiceCall) -> None:
        coordinator = _coordinator_for_device(hass, call.data["device_id"])
        try:
            await coordinator.async_play_clip(call.data[ATTR_CLIP_NAME])
        except KibbleSpeakerBusyError as err:
            raise_speaker_busy(err)
        except KibbleError as err:
            raise_agent_action_failed("Play clip", err)

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
        DOMAIN, SERVICE_SCHEDULE_CARD_ADD, handle_schedule_card_add, SCHEDULE_CARD_ADD_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SCHEDULE_CARD_EDIT, handle_schedule_card_edit, SCHEDULE_CARD_EDIT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCHEDULE_CARD_REMOVE,
        handle_schedule_card_remove,
        SCHEDULE_CARD_REMOVE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCHEDULE_CARD_TOGGLE,
        handle_schedule_card_toggle,
        SCHEDULE_CARD_TOGGLE_SCHEMA,
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
    hass.services.async_register(DOMAIN, SERVICE_SAVE_CLIP, handle_save_clip, SAVE_CLIP_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_RECORD_CLIP, handle_record_clip, RECORD_CLIP_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_PLAY_CLIP, handle_play_clip, PLAY_CLIP_SCHEMA)


async def _async_options_updated(hass: HomeAssistant, entry: KibbleConfigEntry) -> None:
    """The stream source lives in options; re-create the camera entity with the new one."""
    await hass.config_entries.async_reload(entry.entry_id)
