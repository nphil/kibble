"""Local-push companion for the dashboard cards: WebSocket commands over HA's own
`websocket_api`, distinct from `push.py`'s channel to the agent.

`kibble/timeline`, `kibble/cats`, and `kibble/calibration` read straight off the coordinator's
already-polled/pushed `KibbleData` -- no extra agent round trip. `kibble/faces/pending` and
`kibble/faces/samples` call the agent on demand instead: pending-crop detail and a cat's full
sample list are exactly the "training" job's data, looked at rarely and in bulk, not worth
carrying in every poll cycle just so a WS read never has to await one. `kibble/vision/last` is
on demand for the opposite reason: an open card polls it roughly once a second for its live
detection overlay, far more often than a poll cycle, not less. Every command takes `entry_id`;
`_resolve_coordinator` is the one place that turns a bad one into the right WS error instead of
four copies of the same lookup.

`kibble/cats/delete`, `kibble/faces/upload`, `kibble/faces/delete_sample`, and
`kibble/calibration/action` are the mutations here: each forwards straight to a
`KibbleCoordinator.async_*` write (which already refreshes on completion -- see
`coordinator.py`'s face-store-write comment, and `async_calibration_action`'s own note on why
it refreshes the same immediate way) and returns the agent's own JSON result unwrapped.
`_send_agent_error` is their shared failure mapping. The calibration wizard never dispenses
food through either command -- see `api.py`'s `calibration_action` docstring; a step here only
ever reads or bookkeeps a vision score the operator's own separate feed action already put in
the bowl.
"""

from __future__ import annotations

import base64
import binascii
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
    KibbleCalibrationBusyError,
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
# An animal is over the bowl right now (`KibbleCalibrationBusyError`, `POST /calibration`'s
# own 409 on the `point` action) -- distinct from `agent_rejected` so the calibration wizard
# can say "wait for the bowl to clear" instead of a generic failure.
ERR_CALIBRATION_BUSY = "calibration_busy"

# Mirrors the agent's own `GET /events/track/<ts>/image` pairing window exactly (eat preferred,
# else visit, within `[ts - LOOKBACK, ts + LOOKAHEAD]`, closest wins). Computed here too, ahead
# of ever fetching an image, purely so `kibble/timeline` can report -- per row, with no extra
# round trip -- whether an `identified` row has a live image to show at all (and which class it
# came from, for the card's "ate"/"was at the bowl" verb). The agent's own endpoint is still the
# one that actually resolves and serves the bytes; this only decides whether to point a row at
# it.
TRACK_PAIR_LOOKBACK_SECONDS = 300
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
        "image_kind": "track",
    }


def _labelled_face_item(event: DetectionEvent) -> dict[str, Any]:
    """A `face` crop Kibble's own classifier (or a human, via `kibble/faces/label`) filed under
    a cat -- the agent carries the name on the event itself (`ai::Feed::set_face_cat`). This is
    the only sighting evidence there is when the vendor cloud is off and no `track` ever
    arrives, and it is exactly what the cats tile's "last here" is measured from, so the timeline
    must show it too or the two disagree. The image is the event's own crop, served by the HTTP
    image view's `event` kind."""
    return {
        "kind": "identified",
        "ts": event.ts,
        "cat": event.cat,
        "paired_class": "face",
        "image": event.image,
        "image_kind": "event",
    }


def _direct_identified_item(event: DetectionEvent) -> dict[str, Any]:
    """A `visit`/`eat` row that already carries its own `cat` -- LibreFeed's own onboard
    identification (`ai::Feed`), which has no separate `track` event to pair against at all.
    Same `identified` row shape as `_identified_item`/`_labelled_face_item`: `paired_class` is
    the event's own class (`"eat"|"visit"`, so the card's "ate" vs "was here" verb still
    works), `image`/`image_kind` point at the event's own crop exactly like
    `_bare_detection_item` would. `score` -- LibreFeed's own identification confidence -- is
    carried through only when the event actually has one, so a vendor-shaped event (which
    never reaches this path -- see `timeline_items`) can never grow a spurious key."""
    item: dict[str, Any] = {
        "kind": "identified",
        "ts": event.ts,
        "cat": event.cat,
        "paired_class": event.cls,
        "image": event.image,
        "image_kind": "event",
    }
    if event.score is not None:
        item["score"] = event.score
    return item


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
        # False when the feeder dispensed but its MCU never returned the completed record --
        # the amount above is what was asked for, not what the hardware measured. The card
        # says so on the row rather than presenting a guess as a fact.
        "confirmed": record.confirmed,
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
    physical visit showing up twice is exactly the noise this split exists to remove. This is
    the vendor stack's shape; LibreFeed never emits a `track` at all, so `tracks`/`pairs`/
    `claimed_ids` are simply empty there and every LibreFeed event falls through to the loop
    below untouched.

    A `visit`/`eat` event not claimed by any track and carrying its own `cat` -- LibreFeed's
    onboard identification, never a vendor shape -- becomes an `identified` row too
    (`_direct_identified_item`), on either stack: a `track`-less feeder has no other way to
    ever surface who it saw. An unclaimed, uncatted `eat` stays its own `eat` row: a cat at
    the bowl nobody identified. An unclaimed, uncatted `visit` stays its own `visit` row,
    included only when `include_visits` is true (default `False`): a bare "a cat came by"
    with no identity and no feeding is the least useful row on the timeline -- note a catted
    `visit` is never "bare", so it is never subject to that gate. A `face` event produces a
    row only once it carries a `cat` (`_labelled_face_item`); an unlabelled one is
    `kibble/faces/*` training material, not timeline activity.
    """
    tracks = [e for e in events if e.cls == "track"]
    pairs = [_track_pair(track, events) for track in tracks]
    claimed_ids = {id(pair) for pair in pairs if pair is not None}

    items = [
        _identified_item(track, pair, pet_ids) for track, pair in zip(tracks, pairs, strict=True)
    ]
    for event in events:
        if event.cls == "face":
            if event.cat:
                items.append(_labelled_face_item(event))
            continue
        if event.cls not in ("eat", "visit") or id(event) in claimed_ids:
            continue
        if event.cat:
            items.append(_direct_identified_item(event))
        elif event.cls == "eat":
            items.append(_bare_detection_item(event, "eat"))
        elif include_visits:
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
    """Maps a `KibbleError` from a cat/face/calibration mutation to the right WS error code: a
    connection failure stays `feeder_unreachable` (the existing convention every other command
    already uses); a named cat/sample the agent reports missing is `not_found`; an animal over
    the bowl blocking a calibration point (`KibbleCalibrationBusyError`, `POST /calibration`'s
    own 409) is `calibration_busy`, distinct enough from a generic rejection that the wizard
    can say "wait for the bowl to clear" instead; anything else the agent rejected outright (a
    bad name, an invalid JPEG, a malformed calibration step, ...) is `agent_rejected` carrying
    the agent's own message."""
    if isinstance(err, KibbleConnectionError):
        connection.send_error(msg_id, ERR_FEEDER_UNREACHABLE, str(err))
    elif isinstance(err, KibbleNotFoundError):
        connection.send_error(msg_id, ERR_NOT_FOUND, str(err))
    elif isinstance(err, KibbleCalibrationBusyError):
        connection.send_error(msg_id, ERR_CALIBRATION_BUSY, str(err))
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
    try:
        jpeg = base64.b64decode(msg["jpeg_b64"], validate=True)
    except binascii.Error as err:
        # A malformed envelope, not an agent rejection -- the agent never sees this request.
        # Caught here (not left to `websocket_api`'s generic handler) so a bad payload gets a
        # clean `invalid_format` error instead of an "Unknown error" logged with a traceback.
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
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


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/vision/last", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_vision_last(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """`GET /vision/last` straight from the agent, on demand -- like `kibble/faces/pending`/
    `kibble/faces/samples` above, never through the coordinator's poll cycle, but for a
    different reason: an open card polls this roughly once a second to keep its live
    detection-box overlay in step with the video, far tighter than `DEFAULT_SCAN_INTERVAL`,
    and caching a fetch this frequent in `KibbleData` would mean either slowing every other
    entity's refresh to match or serving the overlay stale between polls.

    `KibbleNotFoundError` -- an agent old enough to predate this brand-new route -- folds into
    the same `{"frame": None}` reply as the agent's own "nothing analysed yet" `null`: the
    card has nothing to draw either way, so this is not `ERR_FEEDER_UNREACHABLE` like a real
    connection failure below."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        frame = await coordinator.client.vision_last()
    except KibbleNotFoundError:
        frame = None
    except KibbleError as err:
        connection.send_error(msg["id"], ERR_FEEDER_UNREACHABLE, str(err))
        return
    connection.send_result(msg["id"], {"frame": frame})


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/calibration", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_calibration(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """`GET /calibration` straight off the coordinator's already-polled `KibbleData.
    calibration` -- small, and changed only by the wizard's own actions rather than on the
    device's own clock, so this reads the poll cache exactly like `kibble/cats` above rather
    than fetching on demand like `kibble/faces/pending`/`kibble/vision/last`.

    `None` (an agent old enough to predate this route, or the vendor stack) reports as the
    same `{"hoppers": [null, null]}` shape a fresh LibreFeed daemon gives for two hoppers it
    has never calibrated -- the wizard has nothing to draw either way, so this is not a WS
    error like a real connection failure would be."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    calibration = coordinator.data.calibration
    connection.send_result(
        msg["id"], calibration if calibration is not None else {"hoppers": [None, None]}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/calibration/action",
        vol.Required("entry_id"): str,
        vol.Required("action"): str,
        vol.Required("hopper"): int,
        vol.Optional("portions"): int,
        vol.Optional("from"): int,
        vol.Optional("note"): str,
    }
)
@websocket_api.async_response
async def ws_calibration_action(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """One calibration-wizard step (`action` is `begin`/`point`/`full`/`inherit`/`clear`),
    forwarded to `POST /calibration` via `KibbleCoordinator.async_calibration_action` (which
    refreshes immediately on success, so the follow-up `kibble/calibration` read every wizard
    step makes never sees a stale curve -- see that method's own docstring). `portions`/
    `from`/`note` are forwarded exactly when `msg` carries them; the daemon itself validates
    which fields a given `action` needs and 400s for a wrong combination, the same trust-the-
    agent shape `set_led`/`set_desiccant` already use rather than re-validating here.

    `KibbleCalibrationBusyError` (409, an animal is over the bowl right now) maps to its own
    `calibration_busy` WS error via `_send_agent_error`, distinct from a generic
    `agent_rejected` so the wizard can tell "wait and retry" from "that step was wrong".

    Never dispenses anything: the operator's own feed control/app already put whatever is in
    the bowl there before calling this -- see `api.py`'s `calibration_action` docstring."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    fields = {key: msg[key] for key in ("portions", "from", "note") if key in msg}
    try:
        result = await coordinator.async_calibration_action(msg["action"], msg["hopper"], **fields)
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
    websocket_api.async_register_command(hass, ws_vision_last)
    websocket_api.async_register_command(hass, ws_calibration)
    websocket_api.async_register_command(hass, ws_calibration_action)
