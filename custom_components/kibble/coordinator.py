"""Polling coordinator for a Kibble feeder."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.ffmpeg import HAFFmpeg, get_ffmpeg_manager
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.components.media_source import async_resolve_media, is_media_source_id
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CatInfo,
    ClipInfo,
    CloudState,
    FeederState,
    FeedRecord,
    IdentifyResult,
    KibbleClient,
    KibbleError,
    KibbleMediaError,
    ReviewFace,
    ScheduleState,
    WifiNetwork,
    WifiState,
)
from .ble_fallback import async_feed_with_fallback
from .const import (
    CONF_BLE_ADDRESS,
    CONF_ENABLE_SCHEDULE_WRITES,
    CONF_HOST,
    CONF_STREAM_URL,
    DEFAULT_RTSP_PATH,
    DEFAULT_RTSP_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

type KibbleConfigEntry = ConfigEntry[KibbleCoordinator]


@dataclass(frozen=True, slots=True)
class KibbleData:
    """Everything one poll cycle fetches: feeder telemetry, the schedule cache, the live
    device-settings snapshot, the Petkit-cloud kill switch's status, the current Wi-Fi
    association plus a fresh scan (`agent/src/wifi.rs`), every enrolled cat, the classifier's
    current identification, the crop the pending-face image entity is showing
    (`agent/src/faces.rs`'s `Gallery`/`review_target`/`identify_target`), every stored audio
    clip, and every before/after dish-snapshot record (`agent/src/feed_capture.rs`)."""

    state: FeederState
    schedule: ScheduleState
    config: dict[str, int]
    cloud: CloudState
    wifi: WifiState
    wifi_scan: tuple[WifiNetwork, ...]
    cats: tuple[CatInfo, ...]
    identify: IdentifyResult
    review_face: ReviewFace
    pending_face_count: int
    clips: tuple[ClipInfo, ...]
    feeds: tuple[FeedRecord, ...]


def _rtsp_url(entry: KibbleConfigEntry) -> str:
    """The feeder's live RTSP source for anything that needs its audio/video, honoring the
    same single-consumer rule `camera.py` documents: Scrypted's rebroadcast if configured
    (one video consumer only -- opening a second direct session costs the SoC a thread and a
    TCP writer it doesn't have to spare), otherwise the device's own substream directly."""
    configured = entry.options.get(CONF_STREAM_URL)
    if configured:
        return configured
    host = entry.data[CONF_HOST]
    return f"rtsp://{host}:{DEFAULT_RTSP_PORT}{DEFAULT_RTSP_PATH}"


def _pcm_convert_args(*, duration: float | None = None) -> list[str]:
    """ffmpeg output-side args that turn whatever `-i` decoded into raw signed 16-bit little-
    endian mono 16kHz PCM -- exactly what `POST /speak`/`PUT /clips/<name>` both take as a raw
    body (`agent/src/main.rs`'s `pcm_from_body`). `duration` bounds how much of a *live* source
    to keep (`record_clip`'s RTSP capture, as an ffmpeg output-side `-t`); omitted for a
    one-shot URL/file fetch that already ends on its own."""
    args = ["-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", "-f", "s16le"]
    if duration is not None:
        args = [*args, "-t", f"{duration:.3f}"]
    return args


async def _pcm_from_ffmpeg(
    hass: HomeAssistant, source: str, *, duration: float | None = None
) -> bytes:
    """Runs `source` (a URL or RTSP mount ffmpeg fetches/demuxes/decodes itself -- never
    pre-downloaded by this integration, same "let ffmpeg do the protocol work" approach
    `camera.py`'s snapshot path already uses) through HA's own ffmpeg helper and returns raw
    PCM. `duration` is `record_clip`'s capture length; a generous fixed margin on top bounds
    the one-shot download path against a stalled/slow server."""
    manager = get_ffmpeg_manager(hass)
    runner = HAFFmpeg(manager.binary)
    is_open = await runner.open(
        cmd=_pcm_convert_args(duration=duration), input_source=source, output="-"
    )
    if not is_open:
        raise KibbleMediaError(f"ffmpeg could not open {source!r}")
    try:
        async with asyncio.timeout((duration or 0) + 20):
            pcm, _stderr = await runner.process.communicate()
    except (TimeoutError, ValueError) as err:
        runner.kill()
        raise KibbleMediaError(f"ffmpeg timed out reading {source!r}") from err
    finally:
        await runner.close(0)
    if not pcm:
        raise KibbleMediaError(f"ffmpeg produced no audio from {source!r}")
    return pcm


async def _resolve_media_to_pcm(hass: HomeAssistant, media_content_id: str) -> bytes:
    """Turns an HA media reference -- a media-source URI (what `tts.speak` produces, among
    others) or a plain music URL -- into raw PCM. `media_source.async_resolve_media` first
    (the same boilerplate every core media_player integration uses for this exact step);
    `async_process_play_media_url` after, so a same-instance URL (a local TTS/media file)
    picks up HA's own auth signature before ffmpeg fetches it over plain HTTP with no HA
    session of its own. A bad/unresolvable reference raises `Unresolvable`, already a
    `HomeAssistantError` -- left to propagate as-is rather than rewrapped."""
    if is_media_source_id(media_content_id):
        played = await async_resolve_media(hass, media_content_id)
        media_content_id = played.url
    url = async_process_play_media_url(hass, media_content_id)
    return await _pcm_from_ffmpeg(hass, url)


class KibbleCoordinator(DataUpdateCoordinator[KibbleData]):
    """Keeps one feeder's state fresh."""

    def __init__(
        self, hass: HomeAssistant, entry: KibbleConfigEntry, client: KibbleClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.entry = entry
        # Which transport last actually carried (or was attempted for) a feed command; the
        # "Control path" diagnostic sensor reads this directly. `None` until the first feed.
        self.control_path: str | None = None

    async def _async_update_data(self) -> KibbleData:
        try:
            state = await self.client.state()
            schedule = await self.client.schedule()
            config = await self.client.config()
            cloud = await self.client.cloud()
            wifi = await self.client.wifi()
            wifi_scan = tuple(await self.client.wifi_scan())
            cats = tuple(await self.client.cats())
            identify = await self.client.identify()
            review_face = await self.client.review_face()
            pending_face_count = len(await self.client.pending_faces())
            clips = tuple(await self.client.clips())
            feeds = tuple(await self.client.feeds())
            return KibbleData(
                state=state,
                schedule=schedule,
                config=config,
                cloud=cloud,
                wifi=wifi,
                wifi_scan=wifi_scan,
                cats=cats,
                identify=identify,
                review_face=review_face,
                pending_face_count=pending_face_count,
                clips=clips,
                feeds=feeds,
            )
        except KibbleError as err:
            raise UpdateFailed(str(err)) from err

    def _ble_feed(
        self, hopper: str, amount: int, feed_id: str | None
    ) -> Callable[[], Awaitable[None]] | None:
        """A zero-arg BLE feed attempt, or None if no `ble_address` is configured. Imports
        `.ble` lazily -- a feeder with no BLE fallback set up should never need bleak/
        Home Assistant's `bluetooth` component loaded just to feed over Wi-Fi."""
        address = self.entry.options.get(CONF_BLE_ADDRESS)
        if not address:
            return None

        async def _attempt() -> None:
            from .ble import async_feed as ble_async_feed

            await ble_async_feed(
                self.hass, address, hopper=hopper, amount=amount, feed_id=feed_id
            )

        return _attempt

    async def async_feed(self, hopper: str, amount: int, feed_id: str | None = None) -> None:
        """Dispense over Wi-Fi; on a connection error, fall back to BLE if `ble_address` is
        configured (`docs/25-ble-feed-frame.md`). Always refreshes afterwards and always
        records which transport was used/attempted on `self.control_path` -- the "Control
        path" sensor -- even when the call ultimately fails."""

        async def _wifi_feed() -> None:
            await self.client.feed(hopper, amount, feed_id)

        ble_feed = self._ble_feed(hopper, amount, feed_id)
        outcome = await async_feed_with_fallback(_wifi_feed, ble_feed)
        self.control_path = outcome.control_path
        self.async_update_listeners()
        await self.async_request_refresh()
        if outcome.error is not None:
            raise outcome.error

    async def async_cancel_feed(self) -> None:
        await self.client.cancel_feed()
        await self.async_request_refresh()

    async def async_set_config(self, key: str, value: int) -> None:
        """Write one device setting, then refresh so the new value reflects immediately."""
        await self.client.set_config(key, value)
        await self.async_request_refresh()

    async def async_schedule_set(self, entries: list[dict[str, Any]]) -> None:
        await self.client.set_schedule(entries)
        await self.async_request_refresh()

    async def async_schedule_add(
        self,
        time: str,
        amount_l: int,
        amount_r: int,
        enabled: bool = True,
        entry_id: str | None = None,
    ) -> None:
        await self.client.add_schedule_entry(time, amount_l, amount_r, enabled, entry_id)
        await self.async_request_refresh()

    async def async_schedule_remove(self, entry_id: str) -> None:
        await self.client.remove_schedule_entry(entry_id)
        await self.async_request_refresh()

    async def async_schedule_set_enabled(self, entry_id: str, enabled: bool) -> None:
        await self.client.set_schedule_entry_enabled(entry_id, enabled)
        await self.async_request_refresh()

    def _require_schedule_writes_enabled(self) -> None:
        """Refuses every schedule-card write path (`schedule_card_add`/`_edit`/`_remove`/
        `_toggle`) until an operator has explicitly opted in via the `enable_schedule_writes`
        option -- off by default. The MCU's per-entry schedule time encoding is still
        unconfirmed (docs/schedule.md); a wrong table could dispense at the wrong time or
        amount, so nothing on this path may reach the device before that is resolved and an
        operator has said so."""
        if not self.entry.options.get(CONF_ENABLE_SCHEDULE_WRITES, False):
            raise HomeAssistantError(
                "schedule writing is disabled until the time encoding is confirmed — "
                "see docs/schedule"
            )

    async def async_schedule_card_add(self, entry_id: str, time: str, amount: int) -> None:
        """One `amount` mirrored onto both `amount_l`/`amount_r` -- the same shared-bin
        simplification the primary Feed button already makes."""
        self._require_schedule_writes_enabled()
        await self.async_schedule_add(time, amount, amount, True, entry_id)

    async def async_schedule_card_edit(self, entry_id: str, time: str, amount: int) -> None:
        """No native edit exists -- `schedule.rs`'s `add` rejects a duplicate id
        (STUDY-schedule.md) -- so this removes and re-adds under the same id. Always
        refreshes, even on a failure between the two calls, so a partial edit is reflected
        immediately rather than waiting for the next poll."""
        self._require_schedule_writes_enabled()
        try:
            await self.client.remove_schedule_entry(entry_id)
            await self.client.add_schedule_entry(time, amount, amount, True, entry_id)
        finally:
            await self.async_request_refresh()

    async def async_schedule_card_remove(self, entry_id: str) -> None:
        self._require_schedule_writes_enabled()
        await self.async_schedule_remove(entry_id)

    async def async_schedule_card_toggle(self, entry_id: str) -> None:
        """Server-side toggle (docs/custom.md's `actions.toggle`): flips the entry's own
        current `enabled` state rather than taking one from the caller."""
        self._require_schedule_writes_enabled()
        entry = next((e for e in self.data.schedule.entries if e.id == entry_id), None)
        if entry is None:
            raise HomeAssistantError(f"no schedule entry with id {entry_id!r}")
        await self.async_schedule_set_enabled(entry_id, not entry.enabled)

    async def async_set_cloud(self, enabled: bool) -> None:
        """Flip the Petkit-cloud kill switch. A disable can fail safe and roll itself back
        (`agent/src/cloud.rs`) -- this always refreshes, even when `set_cloud` raises, so the
        switch reflects the actual outcome (including a rollback's `last_error`) immediately
        instead of waiting for the next poll."""
        try:
            await self.client.set_cloud(enabled)
        finally:
            await self.async_request_refresh()

    async def async_wifi_connect(self, ssid: str, password: str | None = None) -> None:
        """Fail-safe Wi-Fi switch (`agent/src/wifi.rs`) -- always refreshes, even when
        `wifi_connect` raises, so a rollback's `last_error` and the (unchanged) live SSID
        appear immediately instead of waiting for the next poll. Mirrors `async_set_cloud`."""
        try:
            await self.client.wifi_connect(ssid, password)
        finally:
            await self.async_request_refresh()

    async def async_wifi_forget(self, ssid: str) -> None:
        await self.client.wifi_forget(ssid)
        await self.async_request_refresh()

    async def async_label_face(self, crop_id: str, cat: str) -> None:
        await self.client.label_face(crop_id, cat)
        await self.async_request_refresh()

    async def async_unlabel_face(self, crop_id: str, cat: str) -> None:
        await self.client.unlabel_face(crop_id, cat)
        await self.async_request_refresh()

    async def async_add_cat(self, name: str) -> None:
        await self.client.add_cat(name)
        await self.async_request_refresh()

    async def async_identify_now(self) -> IdentifyResult:
        """Force an immediate `GET /identify` (bypassing the poll cache) and refresh so the
        `last_seen_pet`/presence entities reflect it right away. Returns the result for the
        `kibble.identify` action's response data."""
        result = await self.client.identify()
        await self.async_request_refresh()
        return result

    async def async_speak(self, pcm: bytes) -> dict:
        """Plays already-prepared `pcm` once through the speaker. Refreshes on any outcome --
        a 409 can't change device state, and a successful speak doesn't show up in any polled
        field either (there is no "is speaking" flag anywhere in `GET /state`) -- kept only for
        the same "always reconcile" shape every other write in this coordinator follows.
        Propagates `KibbleSpeakerBusyError`/`KibbleError` to the caller uncaught, same as every
        other `async_*` write here."""
        try:
            return await self.client.speak(pcm)
        finally:
            await self.async_request_refresh()

    async def async_resolve_and_convert(self, media_content_id: str) -> bytes:
        """Turns an HA media reference into raw PCM -- the shared first half of both
        `async_play_media_content` (posts it to `/speak`) and `async_save_clip` (puts it to
        `/clips/<name>`)."""
        return await _resolve_media_to_pcm(self.hass, media_content_id)

    async def async_play_media_content(self, media_content_id: str) -> dict:
        """Resolves an HA media reference to PCM and plays it once via `async_speak`. Returns
        the agent's `{"samples","estimated_ms"}` so the caller (the media_player entity) can
        track the transient "playing" state honestly, from the agent's own real duration for
        the exact bytes just sent -- not a guess."""
        pcm = await self.async_resolve_and_convert(media_content_id)
        return await self.async_speak(pcm)

    async def async_save_clip(self, name: str, media_content_id: str) -> None:
        pcm = await self.async_resolve_and_convert(media_content_id)
        try:
            await self.client.save_clip(name, pcm)
        finally:
            await self.async_request_refresh()

    async def async_record_clip(self, name: str, seconds: float) -> None:
        """Records `seconds` from the feeder's own live RTSP mic track and stores it as a
        named clip. Shortest reliable path chosen: ffmpeg demuxes/decodes the existing AAC mic
        track directly off the already-published RTSP mount (`-t` bounds the capture) -- no
        separate record-then-convert step, no new agent endpoint, the same "let ffmpeg do the
        protocol work" approach this module's own download path and `camera.py`'s snapshot
        path both already use."""
        pcm = await _pcm_from_ffmpeg(self.hass, _rtsp_url(self.entry), duration=seconds)
        try:
            await self.client.save_clip(name, pcm)
        finally:
            await self.async_request_refresh()

    async def async_play_clip(self, name: str) -> None:
        try:
            await self.client.play_clip(name)
        finally:
            await self.async_request_refresh()
