"""Local-push companion for the dashboard cards: WebSocket commands over HA's own
`websocket_api`, distinct from `push.py`'s channel to the agent.

`kibble/timeline` and `kibble/cats` read straight off the coordinator's already-polled/pushed
`KibbleData` -- no extra agent round trip. `kibble/faces/pending` and `kibble/faces/samples`
call the agent on demand instead: pending-crop detail and a cat's full sample list are exactly
the "training" job's data, looked at rarely and in bulk, not worth carrying in every poll cycle
just so a WS read never has to await one (`DESIGN.md`'s "Data contracts" section is explicit
about this split). Every command takes `entry_id`; `_resolve_coordinator` is the one place that
turns a bad one into the right WS error instead of four copies of the same lookup.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .api import CatInfo, DetectionEvent, FeedRecord, KibbleError
from .const import CONF_VENDOR_PET_IDS, DOMAIN, HOPPER_1, HOPPER_2, HOPPER_BOTH, parse_vendor_pet_ids
from .coordinator import KibbleCoordinator

# `kibble/timeline`'s own cap -- matches DESIGN.md's "at most 100" verbatim.
MAX_TIMELINE_ITEMS = 100

ERR_FEEDER_UNREACHABLE = "feeder_unreachable"


@callback
def _resolve_coordinator(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> KibbleCoordinator | None:
    """Resolves `msg["entry_id"]` to its coordinator, or sends the right WS error and returns
    `None`. Shared by every `kibble/*` command below."""
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        connection.send_error(
            msg["id"], websocket_api.ERR_NOT_FOUND, f"Unknown config entry {msg['entry_id']}"
        )
        return None
    if entry.state is not ConfigEntryState.LOADED:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"{entry.title} is not loaded")
        return None
    return entry.runtime_data


def _vendor_cat(pet_id: str | None, pet_ids: Mapping[str, str]) -> str | None:
    return pet_ids.get(pet_id) if pet_id is not None else None


def _detection_item(event: DetectionEvent, pet_ids: Mapping[str, str]) -> dict[str, Any]:
    return {
        "kind": "detection",
        "ts": event.ts,
        "class": event.cls,
        "cat": event.cat,
        "pet_id": event.pet_id,
        "vendor_cat": _vendor_cat(event.pet_id, pet_ids),
        "image": event.image,
    }


def _feed_item(record: FeedRecord) -> dict[str, Any]:
    """`amount`/`hopper` are derived from `amount1`/`amount2` -- `agent/src/feed_capture.rs`'s
    `FeedRecord` carries per-hopper portions, not a combined amount or a hopper label. Both are
    `None` together only for a spontaneous/scheduled cycle with no manual-feed note to claim
    (`FeedCapture::start_cycle`'s fallback), i.e. genuinely unknown amounts, not zero.

    `outcome` is always `None`: a `FeedRecord` only ever exists after the feeder's feeding flag
    completes a full high-then-low cycle (`run_one_cycle`) -- this agent has no failed/
    cancelled `FeedRecord` variant, so there is nothing honest to report here yet. The key
    stays present so the row shape is stable for consumers that already read it."""
    if record.amount1 is None and record.amount2 is None:
        amount: int | None = None
        hopper: str | None = None
    else:
        amount = (record.amount1 or 0) + (record.amount2 or 0)
        has1, has2 = bool(record.amount1), bool(record.amount2)
        hopper = HOPPER_BOTH if has1 and has2 else HOPPER_1 if has1 else HOPPER_2 if has2 else None
    return {
        "kind": "feed",
        "ts": record.ts,
        "amount": amount,
        "hopper": hopper,
        "outcome": None,
        "before": record.before,
        "after": record.after,
    }


def timeline_items(
    events: Sequence[DetectionEvent], feeds: Sequence[FeedRecord], pet_ids: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Merges detections and feed cycles into DESIGN.md's `kibble/timeline` row shape, newest
    first, capped at `MAX_TIMELINE_ITEMS`."""
    items = [_detection_item(e, pet_ids) for e in events]
    items.extend(_feed_item(f) for f in feeds)
    items.sort(key=lambda item: item["ts"], reverse=True)
    return items[:MAX_TIMELINE_ITEMS]


def cats_items(cats: Sequence[CatInfo], pet_ids: Mapping[str, str]) -> list[dict[str, Any]]:
    """DESIGN.md's `kibble/cats` row shape: adds `vendor_pet_id` (the reverse of the
    `vendor_pet_ids` option -- `None` for a cat the operator hasn't mapped to a vendor id) and
    `color_index` (this cat's 0-based rank in name-sorted order, the same order the rows come
    back in -- one stable palette slot per enrolled cat, per DESIGN.md's colour tokens)."""
    reverse = {name: pet_id for pet_id, name in pet_ids.items()}
    ordered = sorted(cats, key=lambda c: c.name)
    return [
        {
            "name": cat.name,
            "samples": cat.samples,
            "last_seen": cat.last_seen,
            "avatar": cat.avatar,
            "vendor_pet_id": reverse.get(cat.name),
            "color_index": index,
        }
        for index, cat in enumerate(ordered)
    ]


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/timeline", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_timeline(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    pet_ids = parse_vendor_pet_ids(coordinator.entry.options.get(CONF_VENDOR_PET_IDS, ""))
    data = coordinator.data
    connection.send_result(
        msg["id"], {"items": timeline_items(data.events, data.feeds, pet_ids)}
    )


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/cats", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_cats(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    pet_ids = parse_vendor_pet_ids(coordinator.entry.options.get(CONF_VENDOR_PET_IDS, ""))
    connection.send_result(msg["id"], {"cats": cats_items(coordinator.data.cats, pet_ids)})


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/faces/pending", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_faces_pending(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    pet_ids = parse_vendor_pet_ids(coordinator.entry.options.get(CONF_VENDOR_PET_IDS, ""))
    try:
        crops = await coordinator.client.pending_faces()
    except KibbleError as err:
        connection.send_error(msg["id"], ERR_FEEDER_UNREACHABLE, str(err))
        return
    connection.send_result(
        msg["id"],
        {
            "crops": [
                {
                    "name": crop.name,
                    "ts": crop.ts,
                    "vendor_pet_id": crop.vendor_pet_id,
                    "vendor_cat": _vendor_cat(crop.vendor_pet_id, pet_ids),
                    "guess": (
                        {"cat": crop.guess.cat, "score": crop.guess.score}
                        if crop.guess is not None
                        else None
                    ),
                }
                for crop in crops
            ]
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/faces/samples",
        vol.Required("entry_id"): str,
        vol.Required("cat"): str,
    }
)
@websocket_api.async_response
async def ws_faces_samples(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        samples = await coordinator.client.faces_samples(msg["cat"])
    except KibbleError as err:
        connection.send_error(msg["id"], ERR_FEEDER_UNREACHABLE, str(err))
        return
    connection.send_result(
        msg["id"], {"samples": [{"name": s.name, "ts": s.ts} for s in samples]}
    )


@callback
def async_setup_websocket_api(hass: HomeAssistant) -> None:
    """Registers every `kibble/*` websocket command. Called once from `__init__.py`'s
    component-level `async_setup` -- commands are process-global, registering them per config
    entry would try to register the same command more than once."""
    websocket_api.async_register_command(hass, ws_timeline)
    websocket_api.async_register_command(hass, ws_cats)
    websocket_api.async_register_command(hass, ws_faces_pending)
    websocket_api.async_register_command(hass, ws_faces_samples)
