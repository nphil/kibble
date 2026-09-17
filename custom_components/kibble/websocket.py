"""Local-push companion for the dashboard cards: WebSocket commands over HA's own
`websocket_api`, distinct from `push.py`'s channel to the agent.

`kibble/timeline` and `kibble/cats` read straight off the coordinator's already-polled/pushed
`KibbleData` -- no extra agent round trip. `kibble/faces/pending` and `kibble/faces/samples`
call the agent on demand instead: pending-crop detail and a cat's full sample list are exactly
the "training" job's data, looked at rarely and in bulk, not worth carrying in every poll cycle
just so a WS read never has to await one. Every command takes `entry_id`; `_resolve_coordinator`
is the one place that turns a bad one into the right WS error instead of four copies of the same
lookup.

`kibble/cats/delete`, `kibble/faces/upload`, and `kibble/faces/delete_sample` are the three
mutations here: each forwards straight to a `KibbleCoordinator.async_*` write (which already
refreshes on completion -- see `coordinator.py`'s face-store-write comment) and returns the
agent's own JSON result unwrapped. `_send_agent_error` is their shared failure mapping.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .api import (
    CatInfo,
    DetectionEvent,
    FeedRecord,
    KibbleConnectionError,
    KibbleError,
    KibbleNotFoundError,
)
from .const import CONF_VENDOR_PET_IDS, DOMAIN, HOPPER_1, HOPPER_2, HOPPER_BOTH, parse_vendor_pet_ids
from .coordinator import KibbleCoordinator

# `kibble/timeline`'s own cap -- matches DESIGN.md's "at most 100" verbatim.
MAX_TIMELINE_ITEMS = 100

ERR_FEEDER_UNREACHABLE = "feeder_unreachable"
# A named cat/sample the agent reports it doesn't have (`KibbleNotFoundError`) -- distinct from
# "the feeder itself is unreachable" so the card can say "that cat is already gone" instead of
# a generic connectivity banner.
ERR_NOT_FOUND = "not_found"
# Any other write the agent rejected outright (bad name, bad JPEG, ...): a real `KibbleError`
# that is neither a connection failure nor a not-found.
ERR_AGENT_REJECTED = "agent_rejected"

# Mirrors the agent's own `GET /events/track/<ts>/image` pairing window exactly (eat preferred,
# else visit, within `[ts - LOOKBACK, ts + LOOKAHEAD]`, closest wins). Computed here too, ahead
# of ever fetching an image, purely so `kibble/timeline` can report -- per row, with no extra
# round trip -- whether an `identified` row has a live image to show at all (and which class it
# came from, for the card's "ate"/"was at the bowl" verb). The agent's own endpoint is still the
# one that actually resolves and serves the bytes; this only decides whether to point a row at
# it.
TRACK_PAIR_LOOKBACK_SECONDS = 5
TRACK_PAIR_LOOKAHEAD_SECONDS = 120


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


def _track_pair(track: DetectionEvent, events: Sequence[DetectionEvent]) -> DetectionEvent | None:
    """The `eat` event closest to `track.ts` within the pairing window, or the closest `visit`
    in the same window if no `eat` qualifies, or `None`. Computed independently per track: two
    tracks close enough together can legitimately claim the same `eat`/`visit`, exactly as the
    agent's own per-request lookup would for either one's own `ts`."""
    window_start = track.ts - TRACK_PAIR_LOOKBACK_SECONDS
    window_end = track.ts + TRACK_PAIR_LOOKAHEAD_SECONDS
    for cls in ("eat", "visit"):
        candidates = [e for e in events if e.cls == cls and window_start <= e.ts <= window_end]
        if candidates:
            return min(candidates, key=lambda e: abs(e.ts - track.ts))
    return None


def _identified_item(
    track: DetectionEvent, pair: DetectionEvent | None, pet_ids: Mapping[str, str]
) -> dict[str, Any]:
    """One `track` row: `cat` is always a display-ready name (falling back to "Unknown cat"
    for a `pet_id` the `vendor_pet_ids` option hasn't named), never `None` -- there is no "no
    identification" case for a `kind:"identified"` row, only an unmapped one. `image` is the
    bare `ts` for the HTTP image view's `kind="track"` (which re-resolves and serves the same
    pairing live, from the agent) -- `None` when no `eat`/`visit` qualifies, so the card never
    points a thumbnail at a guaranteed 404. `paired_class` names which one so the card can pick
    "ate" vs "was at the bowl" without re-deriving the window logic itself."""
    return {
        "kind": "identified",
        "ts": track.ts,
        "cat": _vendor_cat(track.pet_id, pet_ids) or "Unknown cat",
        "paired_class": pair.cls if pair is not None else None,
        "image": str(track.ts) if pair is not None else None,
    }


def _bare_detection_item(event: DetectionEvent, kind: str) -> dict[str, Any]:
    """A `visit`/`eat` row no `track` claimed as its pairing image (see `timeline_items`).
    `image` is the bare `GET /events/<name>` filename, for the HTTP image view's `kind="event"`
    -- unchanged from every class's image reference before this row shape split by kind."""
    return {"kind": kind, "ts": event.ts, "image": event.image}


def _feed_item(record: FeedRecord) -> dict[str, Any]:
    """`amount`/`hopper` are derived from `amount1`/`amount2` -- `agent/src/feed_capture.rs`'s
    `FeedRecord` carries per-hopper portions, not a combined amount or a hopper label. Both are
    `None` together only for a cycle with no claimable amount at all (genuinely unknown, not
    zero) -- increasingly rare now that the agent associates scheduled cycles with the
    scheduler's own configured amounts too, not just manual feeds."""
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
        "before": record.before,
        "after": record.after,
        "manual": record.manual,
    }


def timeline_items(
    events: Sequence[DetectionEvent],
    feeds: Sequence[FeedRecord],
    pet_ids: Mapping[str, str],
    *,
    include_visits: bool = False,
) -> list[dict[str, Any]]:
    """Merges detections and feed cycles into `kibble/timeline`'s row shape, newest first,
    capped at `MAX_TIMELINE_ITEMS`.

    Every `track` becomes an `identified` row (`_identified_item`) naming the vendor-resolved
    cat, paired via `_track_pair` with the nearest qualifying `eat`/`visit` for its live
    thumbnail. Whichever single `eat`/`visit` a track actually claims is dropped from also
    appearing as its own bare row -- the identified row already carries its image, and the same
    physical visit showing up twice is exactly the noise this split exists to remove.

    An unclaimed `eat` stays its own `eat` row: a cat at the bowl the vendor never identified.
    An unclaimed `visit` stays its own `visit` row, included only when `include_visits` is true
    (default `False`): a bare "a cat came by" with no identity and no feeding is the least
    useful row on the timeline. `face` events never produce a row -- they exist purely as
    `kibble/faces/*` training material, not timeline activity.
    """
    tracks = [e for e in events if e.cls == "track"]
    pairs = [_track_pair(track, events) for track in tracks]
    claimed_ids = {id(pair) for pair in pairs if pair is not None}

    items = [
        _identified_item(track, pair, pet_ids) for track, pair in zip(tracks, pairs, strict=True)
    ]
    for event in events:
        if event.cls == "eat" and id(event) not in claimed_ids:
            items.append(_bare_detection_item(event, "eat"))
        elif include_visits and event.cls == "visit" and id(event) not in claimed_ids:
            items.append(_bare_detection_item(event, "visit"))
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


@callback
def _send_agent_error(
    connection: websocket_api.ActiveConnection, msg_id: int, err: KibbleError
) -> None:
    """Maps a `KibbleError` from a cat/face mutation to the right WS error code: a connection
    failure stays `feeder_unreachable` (the existing convention every other command already
    uses); a named cat/sample the agent reports missing is `not_found`; anything else the agent
    rejected outright (a bad name, an invalid JPEG, ...) is `agent_rejected` carrying the
    agent's own message."""
    if isinstance(err, KibbleConnectionError):
        connection.send_error(msg_id, ERR_FEEDER_UNREACHABLE, str(err))
    elif isinstance(err, KibbleNotFoundError):
        connection.send_error(msg_id, ERR_NOT_FOUND, str(err))
    else:
        connection.send_error(msg_id, ERR_AGENT_REJECTED, str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/timeline",
        vol.Required("entry_id"): str,
        vol.Optional("include_visits", default=False): bool,
    }
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
        msg["id"],
        {
            "items": timeline_items(
                data.events, data.feeds, pet_ids, include_visits=msg["include_visits"]
            )
        },
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
    {
        vol.Required("type"): "kibble/cats/delete",
        vol.Required("entry_id"): str,
        vol.Required("name"): str,
    }
)
@websocket_api.async_response
async def ws_cats_delete(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        result = await coordinator.async_delete_cat(msg["name"])
    except KibbleError as err:
        _send_agent_error(connection, msg["id"], err)
        return
    connection.send_result(msg["id"], result)


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


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/faces/upload",
        vol.Required("entry_id"): str,
        vol.Required("cat"): str,
        vol.Required("jpeg_b64"): str,
    }
)
@websocket_api.async_response
async def ws_faces_upload(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """`jpeg_b64` is the browser's already-cropped-to-224x224 JPEG, base64-encoded for the WS
    JSON envelope -- decoded here, back to raw bytes, before ever reaching the agent."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    jpeg = base64.b64decode(msg["jpeg_b64"], validate=True)
    try:
        result = await coordinator.async_upload_face_sample(msg["cat"], jpeg)
    except KibbleError as err:
        _send_agent_error(connection, msg["id"], err)
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/faces/delete_sample",
        vol.Required("entry_id"): str,
        vol.Required("cat"): str,
        vol.Required("name"): str,
    }
)
@websocket_api.async_response
async def ws_faces_delete_sample(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The cats card's own remove action for an uploaded sample (`upload-*.jpg`), which has no
    pending-queue entry for `unlabel_face` to move it back to."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        result = await coordinator.async_delete_face_sample(msg["cat"], msg["name"])
    except KibbleError as err:
        _send_agent_error(connection, msg["id"], err)
        return
    connection.send_result(msg["id"], result)


@callback
def async_setup_websocket_api(hass: HomeAssistant) -> None:
    """Registers every `kibble/*` websocket command. Called once from `__init__.py`'s
    component-level `async_setup` -- commands are process-global, registering them per config
    entry would try to register the same command more than once."""
    websocket_api.async_register_command(hass, ws_timeline)
    websocket_api.async_register_command(hass, ws_cats)
    websocket_api.async_register_command(hass, ws_cats_delete)
    websocket_api.async_register_command(hass, ws_faces_pending)
    websocket_api.async_register_command(hass, ws_faces_samples)
    websocket_api.async_register_command(hass, ws_faces_upload)
    websocket_api.async_register_command(hass, ws_faces_delete_sample)
