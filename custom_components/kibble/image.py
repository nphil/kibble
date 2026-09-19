"""Image entities for Kibble: the pending-face crop to label next, and the before/after dish
snapshot pair from the most recent feed cycle (`agent/src/feed_capture.rs`)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from homeassistant.components.ffmpeg import HAFFmpeg, get_ffmpeg_manager
from homeassistant.components.image import ImageEntity, ImageEntityDescription
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import DetectionEvent, FeedRecord, KibbleError, ReviewFace
from .const import CONF_HOST, CONF_PORT
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity
from .stacks import applies_to

_LOGGER = logging.getLogger(__name__)

# Read-only, coordinator-backed. See coordinator.py's module docstring and the
# parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class KibbleDishImageDescription(ImageEntityDescription):
    """One half of the before/after dish-snapshot pair."""

    side: str


DISH_IMAGES: tuple[KibbleDishImageDescription, ...] = (
    KibbleDishImageDescription(key="dish_before", translation_key="dish_before", side="before"),
    KibbleDishImageDescription(key="dish_after", translation_key="dish_after", side="after"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    stack = entry.runtime_data.data.detected_stack
    entities: list[ImageEntity] = []
    if applies_to(Platform.IMAGE, "pending_face", stack):
        entities.append(KibblePendingFaceImage(hass, entry))
    if applies_to(Platform.IMAGE, "last_detection_image", stack):
        entities.append(KibbleLastDetectionImage(hass, entry))
    entities.extend(
        KibbleDishImage(hass, entry, description)
        for description in DISH_IMAGES
        if applies_to(Platform.IMAGE, description.key, stack)
    )
    async_add_entities(entities)


def _image_url(entry: KibbleConfigEntry, review: ReviewFace, pending_face_count: int) -> str | None:
    """The agent's `GET /faces/current` URL, with the crop's status/name *and* the current
    pending-queue length folded into a cache-busting query parameter -- `agent/src/main.rs`
    ignores the parameter's value, but `ImageEntity` only refetches (and only bumps its "last
    updated" timestamp, which is this entity's *state*) when this URL *string* itself changes,
    so encoding *which* crop is showing into it is what makes a new crop actually appear
    without a manual refresh. `pending_face_count` matters on its own: `review_face` names only
    the oldest pending crop (or the most recent label once the queue is empty), so a new crop
    arriving behind it, or an unlabel that isn't the current one, changes the *count* without
    changing *review_face* at all -- cards watching this entity's state to know when to refetch
    `kibble/faces/pending` need every pending-list mutation to bump it, not only ones that
    reshuffle the front of the queue. `None` (no URL at all) only when nothing has ever been
    captured."""
    if review.name is None:
        return None
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    cache_key = quote(f"{review.status}-{review.name}-{pending_face_count}", safe="")
    return f"http://{host}:{port}/faces/current?id={cache_key}"


class KibblePendingFaceImage(KibbleEntity, ImageEntity):
    """The oldest pending crop awaiting a label, or the most recently labelled one once the
    queue is empty (`agent/src/faces.rs`'s `review_target`) -- so the picture is never blank
    once caught up. Paired with `select.cat_feeder_label_face`, which acts on the same crop."""

    _attr_translation_key = "pending_face"

    def __init__(self, hass: HomeAssistant, entry: KibbleConfigEntry) -> None:
        KibbleEntity.__init__(self, entry.runtime_data, "pending_face")
        ImageEntity.__init__(self, hass)
        self._entry = entry
        data = entry.runtime_data.data
        self._attr_image_url = _image_url(entry, data.review_face, data.pending_face_count)
        self._attr_image_last_updated = dt_util.utcnow()

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data
        url = _image_url(self._entry, data.review_face, data.pending_face_count)
        if url != self._attr_image_url:
            self._attr_image_url = url
            self._cached_image = None
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        review = self.coordinator.data.review_face
        attrs = {"status": review.status}
        if review.cat is not None:
            attrs["cat"] = review.cat
        return attrs


def _detection_url(entry: KibbleConfigEntry, name: str) -> str:
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    return f"http://{host}:{port}/events/{quote(name, safe='')}"


class KibbleLastDetectionImage(KibbleEntity, ImageEntity):
    """The crop from the feeder's most recent onboard-AI detection that produced one
    (`GET /events`, classes `visit`/`eat`/`face`). A `track` event -- the vendor's own
    identification -- carries no crop and must not blank this out, so the newest event *with an
    image* wins, not the newest event.

    Already a JPEG on the device, so unlike the dish snapshots this needs no H.264 transcode --
    the URL is handed straight to Home Assistant. `image_last_updated` uses the detection's own
    capture timestamp, so the frontend refetches exactly when a new detection lands rather than
    on every poll."""

    _attr_translation_key = "last_detection"

    def __init__(self, hass: HomeAssistant, entry: KibbleConfigEntry) -> None:
        KibbleEntity.__init__(self, entry.runtime_data, "last_detection_image")
        ImageEntity.__init__(self, hass)
        self._entry = entry
        self._event: DetectionEvent | None = None
        self._apply(entry.runtime_data.data.events)

    def _apply(self, events: tuple[DetectionEvent, ...]) -> None:
        with_image = [e for e in events if e.image]
        event = max(with_image, key=lambda e: (e.ts, e.seq)) if with_image else None
        if event is None:
            self._attr_image_url = None
            self._attr_image_last_updated = None
            self._event = None
            return
        self._event = event
        self._attr_image_url = _detection_url(self._entry, event.image)
        self._attr_image_last_updated = dt_util.utc_from_timestamp(event.ts)

    @callback
    def _handle_coordinator_update(self) -> None:
        previous = self._attr_image_url
        self._apply(self.coordinator.data.events)
        if self._attr_image_url != previous:
            self._cached_image = None
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        if self._event is None:
            return {}
        return {"class": self._event.cls, "cat": self._event.cat}


def _latest_dish_snapshot(
    feeds: tuple[FeedRecord, ...], side: str
) -> tuple[str | None, datetime | None]:
    """The most recent feed cycle's `side` ('before'/'after') snapshot filename and its real
    capture timestamp -- `(None, None)` if nothing has ever been captured for that half of the
    pair. Both dish entities key off the *same* latest record (never independently "the latest
    record that happens to have my side"), so a mismatched pair -- one entity showing feed #10's
    shot next to the other showing feed #7's -- can't happen."""
    if not feeds:
        return None, None
    record = feeds[-1]  # GET /feeds is oldest-first (agent/src/feed_capture.rs's list_records)
    name = record.before if side == "before" else record.after
    if name is None:
        return None, None
    return name, dt_util.utc_from_timestamp(record.ts)


def _feed_snapshot_url(entry: KibbleConfigEntry, name: str) -> str:
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    return f"http://{host}:{port}/feeds/{quote(name, safe='')}"


def _h264_to_jpeg_args(url: str) -> tuple[list[str], str, str]:
    """The exact ffmpeg invocation shape for `_h264_keyframe_to_jpeg`, split out so its
    parameter selection is testable without a real ffmpeg binary. `-f h264` must be on the
    *input* side -- the agent's URL has no file extension/container for ffmpeg's prober to key
    off -- which is why this can't just call `ffmpeg.async_get_image` (its `extra_cmd` only
    lands after `-i`); this drives `HAFFmpeg` directly instead, the exact primitive
    `async_get_image` is itself built on."""
    return ["-frames:v", "1", "-c:v", "mjpeg"], f"-f h264 -i {url}", "-f image2pipe -"


async def _h264_keyframe_to_jpeg(hass: HomeAssistant, url: str) -> bytes | None:
    """One `GET /feeds/<name>` H.264 keyframe (SPS+PPS+IDR access unit -- `agent/src/
    feed_capture.rs`, a still-vendor-stack agent only -- see `_feed_snapshot_jpeg`'s doc for how
    that's told apart from LibreFeed's own already-JPEG answer) decoded to a JPEG via HA's own
    ffmpeg helper, per that module's own doc comment ("Home Assistant ... decodes them to a
    displayable image"). Returns `None` (not an exception) on any ffmpeg failure -- an image
    entity degrading to "no picture right now" is the normal, supported outcome, the same as
    `ImageEntity`'s own built-in URL fetch already does for a bad response."""
    manager = get_ffmpeg_manager(hass)
    decoder = HAFFmpeg(manager.binary)
    cmd, input_source, output = _h264_to_jpeg_args(url)
    if not await decoder.open(cmd=cmd, input_source=input_source, output=output):
        _LOGGER.warning("ffmpeg could not open %s", url)
        return None
    try:
        async with asyncio.timeout(15):
            jpeg, _stderr = await decoder.process.communicate()
    except (TimeoutError, ValueError):
        _LOGGER.warning("ffmpeg timed out decoding %s", url)
        decoder.kill()
        return None
    finally:
        await decoder.close(0)
    return jpeg or None


# A JPEG stream's first two bytes are always its SOI marker, `FF D8` -- true regardless of which
# optional segment (APP0/JFIF, APP1/Exif, ...) comes next, and sufficient on its own to identify
# one. A raw H.264 Annex-B access unit (`agent/src/feed_capture.rs`'s own format) never starts
# this way: its first NAL unit's start code is `00 00 00 01` or `00 00 01`.
_JPEG_MAGIC = b"\xff\xd8"


def _is_jpeg(data: bytes) -> bool:
    return data[:2] == _JPEG_MAGIC


async def _feed_snapshot_jpeg(hass: HomeAssistant, entry: KibbleConfigEntry, name: str) -> bytes | None:
    """One `GET /feeds/<name>` dish snapshot as a JPEG, regardless of which stack answers it.
    LibreFeed's own `daemon/src/feeds.rs::read_feed_file` already serves a real JPEG; a feeder
    still running the vendor kibbled stack serves a raw H.264 keyframe instead (`agent/src/
    feed_capture.rs`) that still needs `_h264_keyframe_to_jpeg`'s ffmpeg decode. The two are
    told apart by `_is_jpeg`'s magic-byte check on the actual bytes on the wire -- never by
    guessing which stack is running, since `kibble.set_mode` can switch stacks without Home
    Assistant ever being told.

    Fetched through this entry's own `KibbleClient.feed_bytes` -- the same locked, timed-out
    path every other passthrough kind uses -- rather than ffmpeg's own independent URL fetch,
    so this request is properly serialised against the feeder's single-client HTTP server too
    (`api.py`'s module docstring). A failure decoding a genuine H.264 keyframe is reported as
    `None`, never an exception -- see `_h264_keyframe_to_jpeg`'s own doc; a failure fetching the
    bytes in the first place is *not* caught here, so it propagates as the same `KibbleError`
    every other passthrough kind raises, for callers (`views.py`'s `kind="feed"`) to map to the
    same 404/502 they already do."""
    raw = await entry.runtime_data.client.feed_bytes(name)
    if _is_jpeg(raw):
        return raw
    return await _h264_keyframe_to_jpeg(hass, _feed_snapshot_url(entry, name))


class KibbleDishImage(KibbleEntity, ImageEntity):
    """One half (`side`: 'before'/'after') of the dish snapshot pair for the most recent feed
    cycle. `async_image` fetches it through `_feed_snapshot_jpeg` -- a ready-made JPEG on
    LibreFeed, still a raw H.264 keyframe needing an on-demand ffmpeg decode on the vendor
    kibbled stack -- rather than `ImageEntity`'s own built-in URL fetch, which requires the URL
    to directly return a recognized image content type up front."""

    entity_description: KibbleDishImageDescription
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: KibbleConfigEntry,
        description: KibbleDishImageDescription,
    ) -> None:
        KibbleEntity.__init__(self, entry.runtime_data, description.key)
        ImageEntity.__init__(self, hass)
        self.entity_description = description
        self._entry = entry
        self._side = description.side
        self._name, self._attr_image_last_updated = _latest_dish_snapshot(
            entry.runtime_data.data.feeds, description.side
        )
        self._jpeg: bytes | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        name, last_updated = _latest_dish_snapshot(self.coordinator.data.feeds, self._side)
        if name != self._name:
            self._name = name
            self._attr_image_last_updated = last_updated
            self._jpeg = None
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        if self._name is None:
            return None
        if self._jpeg is None:
            try:
                self._jpeg = await _feed_snapshot_jpeg(self.hass, self._entry, self._name)
            except KibbleError as err:
                # An image entity degrading to "no picture right now" is the normal, supported
                # outcome -- see `_h264_keyframe_to_jpeg`'s own doc for the ffmpeg-decode half
                # of this same contract.
                _LOGGER.warning("Could not fetch dish snapshot %s: %s", self._name, err)
                return None
        return self._jpeg
