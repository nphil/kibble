"""Image entities for Kibble: the newest identified crop, and the before/after dish snapshot
pair from the most recent feed cycle (`agent/src/feed_capture.rs`)."""

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
from homeassistant.util import dt as dt_util, slugify

from .api import FeedRecord, KibbleError
from .const import CONF_HOST, CONF_PORT
from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity
from .stacks import applies_to
from .store import DeviceIdentitySummary, _utc_date

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
    if applies_to(Platform.IMAGE, "last_detection_image", stack):
        entities.append(KibbleLastDetectionImage(hass, entry))
    entities.extend(
        KibbleDishImage(hass, entry, description)
        for description in DISH_IMAGES
        if applies_to(Platform.IMAGE, description.key, stack)
    )
    async_add_entities(entities)

    if not applies_to(Platform.IMAGE, "cat_avatar", stack):
        return

    # Per-cat avatar entities are created dynamically from the identity engine's roster, same
    # as `binary_sensor.py`'s `KibbleCatPresentBinarySensor` -- there is no fixed list at
    # integration setup, since cats are enrolled over time.
    coordinator = entry.runtime_data
    known_cats: set[str] = set()

    @callback
    def _add_new_cats() -> None:
        new = [name for name in coordinator.data.identity.cats if name not in known_cats]
        if not new:
            return
        known_cats.update(new)
        async_add_entities([KibbleCatAvatarImage(hass, coordinator, name) for name in new])

    entry.async_on_unload(coordinator.async_add_listener(_add_new_cats))
    _add_new_cats()  # cats already known at setup time


class KibbleLastDetectionImage(KibbleEntity, ImageEntity):
    """The best body crop of the newest event HA's own identity engine could name -- the same
    engine `sensor.*_last_seen_pet` reads (`store.identity_summary`'s `last_detection_thumb`),
    so the two can never disagree.

    Overrides `async_image` to read the already-archived crop straight off the store rather
    than routing through an external URL and HA's remote-image proxy: the bytes are already
    local, so there is nothing to fetch."""

    _attr_translation_key = "last_detection"

    def __init__(self, hass: HomeAssistant, entry: KibbleConfigEntry) -> None:
        KibbleEntity.__init__(self, entry.runtime_data, "last_detection_image")
        ImageEntity.__init__(self, hass)
        self._asset_id: str | None = None
        self._apply(entry.runtime_data.data.identity)

    def _apply(self, identity: DeviceIdentitySummary) -> None:
        thumb = identity.last_detection_thumb
        asset_id = thumb["id"] if thumb else None
        if asset_id == self._asset_id:
            return
        self._asset_id = asset_id
        self._cached_image = None
        self._attr_image_last_updated = (
            dt_util.utc_from_timestamp(identity.last_seen_pet_ts)
            if identity.last_seen_pet_ts is not None
            else None
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._apply(self.coordinator.data.identity)
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"cat": self.coordinator.data.identity.last_seen_pet}

    async def async_image(self) -> bytes | None:
        if self._asset_id is None:
            return None
        path = self.coordinator.store.asset_path(self._asset_id)
        if path is None:
            return None
        try:
            return await self.hass.async_add_executor_job(path.read_bytes)
        except FileNotFoundError:
            return None



class KibbleCatAvatarImage(KibbleEntity, ImageEntity):
    """This cat's current avatar photo -- a custom pick if the user set one, else the newest
    trained photo (`store._avatar_state`), or nothing at all until either exists. Created
    dynamically as the identity engine's cat roster grows, exactly like `binary_sensor.py`'s
    `KibbleCatPresentBinarySensor` (see that class's own docstring); reads the same cached
    `CatStats.avatar`/`avatar_updated` that `store.identity_summary` already computes per cat
    (`_avatar_state`, shared with `kibble/cats`'s own `avatar` field), so this entity's picture
    and the card's never disagree and this entity never makes its own live store call."""

    _attr_translation_key = "cat_avatar"

    def __init__(self, hass: HomeAssistant, coordinator: KibbleCoordinator, cat_name: str) -> None:
        KibbleEntity.__init__(self, coordinator, f"cat_avatar_{slugify(cat_name)}")
        ImageEntity.__init__(self, hass)
        self._cat_name = cat_name
        # Display-only capitalisation -- see `KibbleCatPresentBinarySensor`'s own comment.
        display = cat_name[:1].upper() + cat_name[1:] if cat_name else cat_name
        self._attr_translation_placeholders = {"cat_name": display}
        self._asset_id: str | None = None
        self._apply(coordinator.data.identity)

    def _apply(self, identity: DeviceIdentitySummary) -> None:
        stats = identity.cats.get(self._cat_name)
        asset_id = stats.avatar if stats else None
        if asset_id == self._asset_id:
            return
        self._asset_id = asset_id
        self._cached_image = None
        updated_ts = stats.avatar_updated if stats else None
        self._attr_image_last_updated = (
            dt_util.utc_from_timestamp(updated_ts) if updated_ts is not None else None
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._apply(self.coordinator.data.identity)
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        if self._asset_id is None:
            return None
        path = self.coordinator.store.asset_path(self._asset_id)
        if path is None:
            return None
        try:
            return await self.hass.async_add_executor_job(path.read_bytes)
        except FileNotFoundError:
            return None

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
    # By timestamp, never by position. This read `feeds[-1]` on the strength of the vendor
    # agent returning oldest-first; LibreFeed returns newest-first, so both dish entities spent
    # days pinned to the OLDEST record on the device -- a chime test from 2026-09-18 -- while
    # every real feed came and went. `sensor.py` already sorts defensively for exactly this
    # reason; this was the one place still trusting an agent's ordering.
    record = max(feeds, key=lambda r: r.ts)
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
        if self._jpeg is None and self._attr_image_last_updated is not None:
            # Ingest archives feed photos into HA's own store and acknowledges them, after which
            # the feeder no longer has them: the archived copy is the one to show.
            date = _utc_date(int(self._attr_image_last_updated.timestamp()))
            path = self.coordinator.store.asset_path(f"{date}/{self._name}")
            if path is not None:
                try:
                    self._jpeg = await self.hass.async_add_executor_job(path.read_bytes)
                except FileNotFoundError:
                    pass
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
