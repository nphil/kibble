"""Diagnostics support for Kibble.

Redacts anything that could identify or grant access to the physical feeder or its network:
its host/IP, the Scrypted rebroadcast URL (a bearer-token-shaped capability URL, not just a
host), the feeder's BLE MAC, Wi-Fi SSIDs/BSSIDs/IPs the feeder reports about itself, any future
auth token, and the feeder's own serial number. `async_redact_data` (HA's own diagnostics
helper) walks every nested dict/list, so this catches these keys wherever they show up --
including inside `FeederState.raw`, the agent's own unprocessed `GET /state` body, not only in
the fields this integration has parsed out of it.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_BLE_ADDRESS, CONF_HOST, CONF_STREAM_URL
from .coordinator import KibbleConfigEntry

TO_REDACT = {
    CONF_HOST,
    CONF_STREAM_URL,
    CONF_BLE_ADDRESS,
    "serial",
    "ssid",
    "bssid",
    "desired_ssid",
    "ip",
    "remote",
    "password",
    "psk",
    "token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: KibbleConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    vision_judge = coordinator.ingestor.vision_judge
    coral_status = coordinator.engine.coral_status

    return async_redact_data(
        {
            "entry_data": dict(entry.data),
            "entry_options": dict(entry.options),
            "coordinator": {
                # See coordinator.py's module docstring: `last_update_success` only goes
                # `False` past the tolerance window, `feeder_reachable` reflects the *most
                # recent* poll outright, and the two can legitimately disagree.
                "last_update_success": coordinator.last_update_success,
                "feeder_reachable": coordinator.feeder_reachable,
                "consecutive_failures": coordinator.consecutive_failures,
                "last_error": coordinator.last_error,
                "control_path": coordinator.control_path,
                "loaded_platforms": [platform.value for platform in coordinator.loaded_platforms],
            },
            "push": {
                "connected": coordinator.push_connected,
                "unsupported_by_agent": coordinator.push_unsupported,
                "reconnects": coordinator.push_reconnects,
                "seconds_since_last_frame": (
                    None
                    if coordinator.push_last_frame is None
                    else round(hass.loop.time() - coordinator.push_last_frame, 1)
                ),
                "update_interval_seconds": (
                    None
                    if coordinator.update_interval is None
                    else coordinator.update_interval.total_seconds()
                ),
            },
            # docs/40-vision-judge.md: `None` in every test fixture that never wires one up
            # (mirrors `Ingestor.vision_judge`'s own docstring); a real setup always has one.
            "vision_judge": {
                "enabled": vision_judge.enabled if vision_judge is not None else False,
                "model": vision_judge.model if vision_judge is not None else None,
                "last_error": vision_judge.last_error if vision_judge is not None else None,
                "verdict_counts": await coordinator.store.async_judge_diagnostics(),
                "cat_descriptions": await coordinator.store.async_cat_descriptions(),
            },
            # docs/41-coral-recognition.md: `coral_status` is `None` whenever `coralhub_url`
            # is unset for this entry -- the histogram recognizer is the only backend then,
            # same "off means off, no second system running" shape `vision_judge` has above.
            "coral": {
                "backend": coordinator.engine.backend,
                "configured": coral_status.configured if coral_status is not None else False,
                "available": coral_status.available if coral_status is not None else False,
                "last_error": coral_status.last_error if coral_status is not None else None,
            },
            "data": asdict(data) if data is not None else None,
        },
        TO_REDACT,
    )
