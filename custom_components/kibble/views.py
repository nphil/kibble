"""HTTP view for the dashboard cards: archived evidence and training media.

`GET /api/kibble/{entry_id}/media/{asset}` serves whatever `store.resolve_asset_path` resolves
`asset` to under one config entry's store root -- both `media/<date>/<file>` (archived evidence)
and `training/<cat_slug>/<file>` (labelled samples) live under the same authenticated endpoint;
see `store.py`'s module docstring for the asset-id scheme. The route pattern captures `asset`
greedily (it may itself contain `/`, e.g. `2026-09-24/e1201-s1-body.jpg`); `resolve_asset_path`
is the only thing that decides whether a given id is actually safe to serve, never the route
match alone -- confirmed empirically: aiohttp's own URL normalization already 404s a literal
`..` segment before this view ever runs, and `resolve_asset_path` independently rejects any
resolved path that would still escape the store root.

Authenticated (`requires_auth = True`) and immutably cacheable: every asset id is content-
addressed by its capture/creation, never reused for different bytes, so a card can cache a
fetched crop forever.
"""

from __future__ import annotations

import logging
import uuid
from http import HTTPStatus
from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientResponse, ClientTimeout, web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import identity, media_processing
from .const import CONF_SCRYPTED_CLIPS_URL, DOMAIN
from .eating_clips import videoclip_url
from .store import FREE_DISK_FLOOR_BYTES

if TYPE_CHECKING:
    from .coordinator import KibbleCoordinator

_LOGGER = logging.getLogger(__name__)

CACHE_CONTROL = "private, max-age=31536000, immutable"

# Generous allowance over `media_processing.MAX_UPLOAD_BYTES` for multipart framing (boundary
# markers, the `file` part's own headers, any other form field) -- a fast upfront 413 when
# `Content-Length` already declares more than this, before any multipart parsing starts at all.
_MULTIPART_OVERHEAD_BYTES = 8 * 1024


def _resolve_coordinator(hass: HomeAssistant, entry_id: str) -> KibbleCoordinator | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data


async def _read_multipart_file(request: web.Request, cap: int) -> bytes:
    """Reads the single `file` field of a `multipart/form-data` POST, aborting the instant its
    bytes exceed `cap` -- read in bounded chunks so an unbounded chunked-encoding body can never
    be buffered past the cap in the first place, not just rejected after the fact. Raises
    `web.HTTPException` (413 too large, 400 missing field) on any rejection."""
    if request.content_length is not None and request.content_length > cap + _MULTIPART_OVERHEAD_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=cap, actual_size=request.content_length)
    reader = await request.multipart()
    total = 0
    chunks: list[bytes] = []
    async for field in reader:
        if field.name != "file":
            continue
        while True:
            chunk = await field.read_chunk(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise web.HTTPRequestEntityTooLarge(max_size=cap, actual_size=total)
            chunks.append(chunk)
        return b"".join(chunks)
    raise web.HTTPBadRequest(text="Missing 'file' field")


class KibbleMediaView(HomeAssistantView):
    """`GET /api/kibble/{entry_id}/media/{asset}`."""

    url = "/api/kibble/{entry_id}/media/{asset:.+}"
    name = "api:kibble:media"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, asset: str) -> web.Response:
        hass = request.app[KEY_HASS]
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        path = entry.runtime_data.store.asset_path(asset)
        if path is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            data = await hass.async_add_executor_job(path.read_bytes)
        except FileNotFoundError:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(
            body=data, content_type="image/jpeg", headers={"Cache-Control": CACHE_CONTROL}
        )


class KibbleUploadTrainingView(HomeAssistantView):
    """`POST /api/kibble/{entry_id}/upload/training/{cat}` -- multipart, one `file` field per
    request. The card uploads a batch as N independent requests with bounded concurrency, never
    one big multi-file request (`kibble-card`'s `lib/upload-queue.ts`) -- both because HA's own
    WebSocket message-size limit would break a large payload, and because one request per file
    is what makes "a failed file does not abort the batch" true by construction: there is no
    shared transaction for a bad file to poison.

    Runs the photo through the exact live-sample pipeline: `media_processing.
    process_upload_image` (downscale/orient/strip -- independent of whatever the client's own
    `lib/image-prep.ts` already attempted), then `identity.features_from` -- the *same* feature
    extractor `ingest.py` calls for a device-captured crop, reused verbatim rather than
    reimplemented -- a blank-image/no-model-yet baseline reject, the trained classifier's own
    `not_a_cat` verdict when there is a model to ask, and a near-duplicate reject against this
    cat's own existing training photos, before anything is ever persisted.

    Response body is always `{"status": ..., "message": ...}`, `status` one of `"added"`
    (+`"sample"`), `"duplicate"`, `"no_cat"`, or `"error"` (+`"reason"`) -- never a bare HTTP
    error for a rejection the user needs to see per-file, only for a request that is wrong at
    the transport level (bad entry/cat, too large, no file field)."""

    url = "/api/kibble/{entry_id}/upload/training/{cat}"
    name = "api:kibble:upload:training"
    requires_auth = True

    async def post(self, request: web.Request, entry_id: str, cat: str) -> web.Response:
        hass = request.app[KEY_HASS]
        coordinator = _resolve_coordinator(hass, entry_id)
        if coordinator is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        if not await coordinator.store.async_cat_exists(cat):
            return web.json_response(
                {"status": "error", "reason": "not_found", "message": "Unknown cat."},
                status=HTTPStatus.NOT_FOUND,
            )
        try:
            raw = await _read_multipart_file(request, media_processing.MAX_UPLOAD_BYTES)
        except web.HTTPRequestEntityTooLarge:
            return web.json_response(
                {"status": "error", "reason": "too_large", "message": "That file is too large."},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        except web.HTTPBadRequest as exc:
            return web.json_response(
                {"status": "error", "reason": "bad_image", "message": str(exc) or "Bad request."},
                status=HTTPStatus.BAD_REQUEST,
            )
        free = await coordinator.store.async_free_disk_bytes()
        if free < FREE_DISK_FLOOR_BYTES:
            return web.json_response(
                {
                    "status": "error", "reason": "disk_full",
                    "message": "Not enough free disk space to store new photos right now.",
                },
                status=HTTPStatus.INSUFFICIENT_STORAGE,
            )
        try:
            processed = await hass.async_add_executor_job(media_processing.process_upload_image, raw)
        except media_processing.UploadRejected as exc:
            return web.json_response(
                {"status": "error", "reason": exc.reason, "message": str(exc)}, status=HTTPStatus.BAD_REQUEST
            )
        blank = await hass.async_add_executor_job(identity.is_blank_image, processed)
        if blank:
            return web.json_response({"status": "no_cat", "message": "No cat found in this photo."})
        features = await hass.async_add_executor_job(identity.features_from, processed, None, None)
        if features.body_feat is None:
            return web.json_response({"status": "no_cat", "message": "No cat found in this photo."})
        verdict = await coordinator.engine.async_classify_one(features)
        if verdict is not None and verdict.label == identity.NOT_A_CAT:
            return web.json_response({"status": "no_cat", "message": "No cat found in this photo."})
        existing = await coordinator.store.async_training_feats_for_cat(cat, features.mode)
        dist = await hass.async_add_executor_job(identity.nearest_distance, features.body_feat, existing)
        if dist is not None and dist < identity.NEAR_DUPLICATE_DISTANCE:
            return web.json_response({"status": "duplicate", "message": "This looks like a photo already in training."})
        uid = f"{entry_id}-upload-{uuid.uuid4().hex}"
        sample = await coordinator.store.async_add_upload_training(
            cat=cat, uid=uid, data=processed, features=features
        )
        if sample is None:
            return web.json_response(
                {"status": "error", "reason": "not_found", "message": "Unknown cat."}, status=HTTPStatus.NOT_FOUND
            )
        hass.async_create_task(_async_training_added_followup(coordinator))
        return web.json_response({"status": "added", "message": "Added.", "sample": sample})


class KibbleUploadAvatarView(HomeAssistantView):
    """`POST /api/kibble/{entry_id}/upload/avatar/{cat}` -- multipart, one `file` field. Runs
    the same robust intake as training uploads (`media_processing.process_upload_image`); does
    NOT run the no-cat/dedupe gates that guard the training set -- a user choosing their own
    cat's avatar photo is deliberate intent, not bulk training data whose provenance needs
    guarding, and a chosen avatar never becomes a training sample on its own."""

    url = "/api/kibble/{entry_id}/upload/avatar/{cat}"
    name = "api:kibble:upload:avatar"
    requires_auth = True

    async def post(self, request: web.Request, entry_id: str, cat: str) -> web.Response:
        hass = request.app[KEY_HASS]
        coordinator = _resolve_coordinator(hass, entry_id)
        if coordinator is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        try:
            raw = await _read_multipart_file(request, media_processing.MAX_UPLOAD_BYTES)
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "That file is too large."}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        except web.HTTPBadRequest as exc:
            return web.json_response({"error": str(exc) or "Bad request."}, status=HTTPStatus.BAD_REQUEST)
        free = await coordinator.store.async_free_disk_bytes()
        if free < FREE_DISK_FLOOR_BYTES:
            return web.json_response({"error": "Not enough free disk space right now."}, status=HTTPStatus.INSUFFICIENT_STORAGE)
        try:
            processed = await hass.async_add_executor_job(media_processing.process_upload_image, raw)
        except media_processing.UploadRejected as exc:
            return web.json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        avatar = await coordinator.store.async_set_cat_avatar(cat, processed)
        if avatar is None:
            return web.json_response({"error": "Unknown cat."}, status=HTTPStatus.NOT_FOUND)
        hass.async_create_task(coordinator.async_refresh_identity_snapshot())
        return web.json_response({"avatar": avatar})


async def _async_training_added_followup(coordinator: KibbleCoordinator) -> None:
    """The slow half of a successful training upload: rebuild the model, re-classify
    unreviewed events, then push the identity snapshot -- the same fast/slow split
    `websocket.py`'s `kibble/label`/`kibble/sample/label` already use."""
    try:
        await coordinator.engine.async_rebuild()
        await coordinator.engine.async_reclassify_unreviewed(coordinator.retention_cutoff())
    except Exception:  # noqa: BLE001 -- must never leave the identity snapshot un-refreshed
        _LOGGER.exception("Upload training background rebuild failed")
    await coordinator.async_refresh_identity_snapshot()


# --- eating clips (docs/39-eating-clips.md) ----------------------------------------------------

# Bounds one read from Scrypted's socket, not the whole proxied transfer -- a client scrubbing
# slowly through a 15-minute clip must never be cut off just because the download took longer
# than some fixed total. `sock_connect` alone (LAN, Scrypted) would normally answer in
# milliseconds; 8s is generous headroom, matching `api.py`'s own connect-side reasoning.
CLIP_STREAM_TIMEOUT = ClientTimeout(sock_connect=8, sock_read=30)
_CLIP_CHUNK_BYTES = 64 * 1024


def _clip_response_headers(upstream: ClientResponse) -> dict[str, str]:
    """The response headers to mirror back to the card's `<video>`: `Content-Type` and
    (when present) `Content-Range`/`Content-Length` straight from Scrypted's own recorder
    webhook -- confirmed against its source to send exactly these on both a 200 (whole file)
    and a 206 (one `Range`). `Accept-Ranges` is set unconditionally rather than mirrored: the
    resource is always range-capable through this proxy regardless of which branch upstream
    took for this particular request."""
    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": "bytes",
    }
    content_length = upstream.headers.get("Content-Length")
    if content_length is not None:
        headers["Content-Length"] = content_length
    content_range = upstream.headers.get("Content-Range")
    if content_range is not None:
        headers["Content-Range"] = content_range
    return headers


class KibbleClipView(HomeAssistantView):
    """`GET /api/kibble/{entry_id}/clip/{event_uid}` -- proxies the Scrypted Events Recorder
    clip backing one eat session (`docs/39-eating-clips.md`), `Range` passthrough included, so
    the card's `<video>` can seek without this view ever buffering a whole clip into memory.

    Only ever serves an event `eating_clips.ClipLinker` itself already linked: `store.
    resolve_event_clip` is the only thing that decides whether `event_uid` names anything real,
    never the route match alone (mirrors `KibbleMediaView`'s own asset-id safety note) -- an
    unknown or never-linked uid 404s before a single byte is requested from Scrypted, so this
    can never be used to reach an attacker-chosen filename on the recorder (no SSRF).

    A 404 from THIS view can mean either of two different upstream facts: nothing was ever
    linked (`resolve_event_clip` found nothing), or Scrypted no longer has the file at all
    (`docs/39-eating-clips.md`'s "Findings" section: quota-based cleanup). The recorder's own
    source answers the second case with a plain HTTP 400 (an uncaught `stat()` ENOENT falling
    through to its generic webhook error handler), not a 404 -- so any non-2xx/206 upstream
    status is treated here as "the clip is gone", never pattern-matched to one specific code.
    That second case additionally clears the link (`store.clear_event_clip`) so a pruned clip
    is never retried by the card forever.
    """

    url = "/api/kibble/{entry_id}/clip/{event_uid}"
    name = "api:kibble:clip"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, event_uid: str) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        coordinator = _resolve_coordinator(hass, entry_id)
        if coordinator is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        base_url = (coordinator.entry.options.get(CONF_SCRYPTED_CLIPS_URL) or "").strip()
        if not base_url:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        clip = await coordinator.store.async_resolve_event_clip(event_uid)
        if clip is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        upstream_url = videoclip_url(base_url, clip["clip_id"])
        headers = {}
        range_header = request.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
        session = async_get_clientsession(hass)
        try:
            upstream = await session.get(upstream_url, headers=headers, timeout=CLIP_STREAM_TIMEOUT)
        except (ClientError, TimeoutError) as err:
            _LOGGER.debug("Clip fetch failed for %s: %s", event_uid, err)
            return web.Response(status=HTTPStatus.NOT_FOUND)
        async with upstream:
            if upstream.status not in (HTTPStatus.OK, HTTPStatus.PARTIAL_CONTENT):
                await coordinator.store.async_clear_event_clip(clip["owner_uid"])
                return web.Response(status=HTTPStatus.NOT_FOUND)
            response = web.StreamResponse(status=upstream.status, headers=_clip_response_headers(upstream))
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(_CLIP_CHUNK_BYTES):
                await response.write(chunk)
            await response.write_eof()
            return response
