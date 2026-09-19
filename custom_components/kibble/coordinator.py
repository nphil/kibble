"""Coordinator for a Kibble feeder: local push over the agent's WebSocket, with the HTTP poll
below as the fallback and the first-contact check.

## Push (docs/33-local-push.md)

After the first successful poll proves the HTTP API (`async_config_entry_first_refresh`,
rule `test-before-setup`), `async_start_push` opens the agent's push socket (`push.py`) in a
background task owned by the config entry. Mirrors `homeassistant.components.wled`'s
`_use_websocket`/`listen` line for line: while the socket is up `update_interval` is `None`
(no scheduled polls at all) and every frame lands through `async_set_updated_data`; the moment
it drops, `update_interval` is restored, an immediate refresh is requested, and the task
reconnects with jittered exponential backoff (1 s .. 60 s) for the life of the entry. One
listen task at a time -- a second `async_start_push` while one is alive is a no-op.

A dropped socket on its own never marks entities unavailable: the device may be fine and only
the socket died (HA restart, Wi-Fi blip). The fallback poll that follows decides, under the
exact policy below. Every received frame counts as a successful contact (resets the failure
counter) so `binary_sensor.reachable`'s meaning is unchanged. While connected, a 10-minute
`resync` asks the agent for a full snapshot -- one frame, far cheaper than a poll -- bounding
staleness for any field the agent might fail to mark.

An agent without push (older build; connection refused) is detected on the first attempt and
leaves the coordinator in plain polling mode with a single info log, not an error.

## Availability policy: why one failed poll no longer blanks every entity

The feeder's HTTP agent (`kibbled`) runs on a tiny ARM device behind what is, in practice, a
single-client HTTP server: it is shared with the Scrypted plugin and HomeKit, and one slow
consumer (an ad-hoc ~25s long-poll, observed live against this exact device) is enough to make
every other consumer's requests queue up and time out. That is routine contention for this
device, not evidence the feeder itself is down -- but the first version of this integration
treated it as exactly that: one failed poll raised `UpdateFailed`, which flipped
`DataUpdateCoordinator.last_update_success` to `False`, and because `CoordinatorEntity.available`
is simply `coordinator.last_update_success` (unmodified by `entity.py`), all 51 entities went
`unavailable` at once, mid-dashboard, seconds before the feeder answered normally again.

This coordinator now tells "the last poll failed" apart from "the feeder is down":

- `_fetch_all` makes the same dozen-ish sequential state/config/schedule/... calls as before,
  but the whole batch is bounded by one `POLL_TIMEOUT`, not by summing each call's own
  `api.TIMEOUT`. A single congested endpoint can no longer make one poll cycle balloon toward,
  or past, `DEFAULT_SCAN_INTERVAL` and pile up against the next scheduled one.
- On failure, `consecutive_failures` is incremented. Below `CONSECUTIVE_FAILURES_FOR_UNAVAILABLE`
  *and* as long as a prior good snapshot (`self.data`) exists, `_async_update_data` returns that
  stale snapshot instead of raising: `last_update_success` stays `True`, every entity keeps
  showing its last real value, and only `.consecutive_failures`/`.last_error` (read by the
  disabled-by-default "Feeder reachable" diagnostic binary sensor and by `diagnostics.py`) record
  that something is off. Three is deliberate, not arbitrary: the starved-server incident this
  policy exists for saw `GET /state` fail 3 of 5 attempts at a 10s timeout -- failures in ones
  and twos are this device's ordinary noise floor at a 10s poll interval, not an outage; three in
  a row is ~30s with *zero* successful contact despite three independent attempts, long enough
  that "busy" stops being the more likely explanation than "down". For a cat feeder specifically
  this is the right trade: bowl-fill percentage, Wi-Fi signal, the cached schedule and so on do
  not go stale in a way that matters over one or two 10s ticks, and showing a slightly-old value
  is far less disruptive than every entity blanking out and back while someone is looking at the
  dashboard. `feeding` -- the one field that changes on human timescales, mid-dispense -- is the
  entity most exposed to staleness here, and it self-corrects on the very next successful poll,
  same as it always has.
- At or above the threshold, `_async_update_data` raises `UpdateFailed` exactly as before -- HA's
  own `DataUpdateCoordinator` then flips `last_update_success` (logging once, at `error`,
  satisfying the `log-when-unavailable` quality-scale rule for free) and every entity correctly
  goes `unavailable`, because by this point it is no longer a guess. A repair issue
  (`feeder_unresponsive`) is also raised at this exact point -- the tolerance window below it has
  no issue and no user-visible unavailability, so this is the *first* moment the user needs
  telling, and it is the same moment `entity-unavailable` already made visible in the UI. Every
  raise past the threshold sets `UpdateFailed.retry_after` to a capped exponential backoff -- a
  feeder that has been down for minutes does not need polling every `DEFAULT_SCAN_INTERVAL`, and
  backing off reduces load on whatever eventually restarts it (the device or its network).
  `async_config_entry_first_refresh` is deliberately exempt from all of the above: with no prior
  snapshot to fall back on, a failure there is unconditionally raised on the first attempt, which
  HA turns into `ConfigEntryNotReady` (see `__init__.py`) -- correct, since there is nothing to
  show either way.
- Every HTTP call in this module funnels through one `KibbleClient`, which serialises them with
  its own `asyncio.Lock` (see `api.py`'s module docstring) -- there is only ever one Kibble
  request in flight against the feeder at a time, whether it originates from this coordinator's
  poll or from an entity's write (`kibble.feed`, a switch flip, ...); nothing here fans out
  several requests to the same, or different, endpoints concurrently. The one long-lived
  connection is the push socket, and it is deliberately on a *different port and thread* on
  the agent (`agent/src/push.rs`) so it can never hold the HTTP server's single connection slot.

## Stack-gated entities and reload-on-change (stacks.py)

Every platform's `async_setup_entry` calls `stacks.applies_to` before creating an entity, so
the running feeder userland (vendor kibbled or LibreFeed -- `KibbleData.detected_stack`, see
`_fetch_all`/`stacks.detect_stack`) only ever gets entities it can actually back, instead of
the full ~80-entity superset sitting mostly `unavailable`. `_check_stack_change`, called from
the successful branch of `_async_update_data` only, reloads the config entry the moment a poll
confirms a *different* stack than the last one it confirmed, so `async_setup_entry` reruns
against the new reading. A poll that merely fails (or that succeeds but leaves `detected_stack`
`None` -- an inconclusive `GET /mode`) never reaches that comparison, and never overwrites the
last confirmed stack either: the same tolerance-for-a-few-bad-cycles policy above already
covers the feeder rebooting through a stack switch, so this never mistakes "unreachable" for
"the other stack now".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, TypeVar

from homeassistant.components.ffmpeg import HAFFmpeg, get_ffmpeg_manager
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.components.media_source import async_resolve_media, is_media_source_id
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CatInfo,
    ClipInfo,
    CloudState,
    DesiccantState,
    DetectionEvent,
    FeederState,
    FeedRecord,
    IdentifyResult,
    KibbleClient,
    KibbleError,
    KibbleMediaError,
    KibbleNotFoundError,
    LedState,
    ReviewFace,
    ScheduleState,
    StackState,
    WifiNetwork,
    WifiState,
)
from .ble_fallback import async_feed_with_fallback
from .const import (
    CONF_BLE_ADDRESS,
    CONF_ENABLE_SCHEDULE_WRITES,
    CONF_HOST,
    CONF_STREAM_URL,
    CONF_VENDOR_PET_IDS,
    DEFAULT_RTSP_PATH,
    DEFAULT_RTSP_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_FEEDER_UNRESPONSIVE,
    parse_vendor_pet_ids,
)
from .push import Frame, KibblePush, KibblePushClosed, KibblePushUnsupported, merge_frame
from .stacks import Stack, detect_stack

_LOGGER = logging.getLogger(__name__)

# Bounds one whole `_fetch_all` batch (a dozen-ish sequential calls), regardless of how many of
# them there are or what each one's own `api.TIMEOUT` allows individually -- see the module
# docstring. Comfortably under `DEFAULT_SCAN_INTERVAL` so a stuck cycle aborts, rather than
# piling up against the next one.
#
# Sized from measurement, not taste: the feeder answers a single `GET /state` in 0.6-1.5s when
# healthy (its HTTP server is effectively serial and shares the box with the vendor's encoder at
# a load average around 8). A dozen sequential calls is therefore ~8-18s of honest work, so the
# original 8.0 guaranteed a timeout on every cycle and made every entity unavailable on a device
# that was answering perfectly. 25s leaves headroom under the 30s scan interval.
POLL_TIMEOUT = 25.0

# See the module docstring's reasoning: this is a count of poll cycles, not seconds.
CONSECUTIVE_FAILURES_FOR_UNAVAILABLE = 3

# Cap on `UpdateFailed.retry_after`'s exponential backoff once the feeder is confirmed down.
MAX_RETRY_AFTER = 60.0

# Push reconnect backoff bounds (seconds), jittered -- see the module docstring. The floor is
# short because the common drop is an agent restart (a few seconds); the cap keeps a feeder
# that is genuinely off the network from being knocked on more than once a minute.
PUSH_BACKOFF_MIN = 1.0
PUSH_BACKOFF_MAX = 60.0
# While connected, ask for a full snapshot this often: one small frame that bounds staleness
# for any field the agent might fail to mark. Far cheaper than a poll cycle.
PUSH_RESYNC_SECONDS = 600.0

type KibbleConfigEntry = ConfigEntry[KibbleCoordinator]


@dataclass(frozen=True, slots=True)
class VendorSighting:
    """One `track` detection resolved through the `vendor_pet_ids` option: the vendor's own
    on-device identification of `pet_id`, at the vendor's own visit start time `ts`. `cat` is
    the operator-assigned name, or `None` when the id is not in the option (surfaced raw,
    never guessed into a name)."""

    ts: int
    pet_id: str
    cat: str | None
    total_score: float | None


def vendor_sightings(
    events: Sequence[DetectionEvent], pet_ids: Mapping[str, str]
) -> tuple[VendorSighting, ...]:
    """Every `track` event, newest last, with its `pet_id` mapped to a cat name where the
    option names it. Pure so it's testable without a coordinator."""
    return tuple(
        VendorSighting(ts=e.ts, pet_id=e.pet_id, cat=pet_ids.get(e.pet_id), total_score=e.total_score)
        for e in sorted(events, key=lambda e: (e.ts, e.seq))
        if e.cls == "track" and e.pet_id is not None
    )


_T = TypeVar("_T")


async def _optional(coro: Awaitable[_T], default: _T) -> _T:
    """Await one of `_fetch_all`'s optional reads, substituting `default` for a 404
    (`KibbleNotFoundError`) -- LibreFeed serves only part of `kibbled`'s API today, and a route
    it hasn't grown yet must not fail the whole poll cycle any more than an old agent missing
    `GET /mode` does (see the `try`/`except` around `self.client.mode()` below). Any other
    `KibbleError` (connection refused, timeout, a real 5xx) still propagates unchanged and
    fails the poll exactly as before -- only "this route doesn't exist" is optional."""
    try:
        return await coro
    except KibbleNotFoundError:
        return default


@dataclass(frozen=True, slots=True)
class KibbleData:
    """Everything one poll cycle fetches: feeder telemetry, the schedule cache, the live
    device-settings snapshot, the Petkit-cloud kill switch's status, which feeder userland is
    running (`GET /mode`; `None` on an agent old enough not to have that route -- see
    `_fetch_all`), the current Wi-Fi association plus a fresh scan (`agent/src/wifi.rs`),
    every enrolled cat, the classifier's current identification, the crop the pending-face
    image entity is showing (`agent/src/faces.rs`'s `Gallery`/`review_target`/
    `identify_target`), every stored audio clip, and every before/after dish-snapshot record
    (`agent/src/feed_capture.rs`)."""

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
    events: tuple[DetectionEvent, ...]
    vendor_sightings: tuple[VendorSighting, ...]
    #: `None` on agents that predate `GET /mode` (the stack select is unavailable then).
    stack: StackState | None = None
    #: `None` on the vendor stack (`GET /led` is a LibreFeed-only route -- see `light.py`'s
    #: `KibbleStatusLight.available`).
    led: LedState | None = None
    #: `None` on the vendor stack (`GET /desiccant` is a LibreFeed-only route -- the vendor's
    #: equivalent counter is cloud-set, not agent-served; see `button.py`'s
    #: `KibbleReplaceDesiccantButton.available`).
    desiccant: DesiccantState | None = None
    #: The feeder userland this snapshot's `stack`/`state.raw` identify, or `None` if neither
    #: signal does -- see `stacks.detect_stack`'s own doc for exactly how. Every platform's
    #: `async_setup_entry` gates entity creation on this (via `stacks.applies_to`), and
    #: `KibbleCoordinator._check_stack_change` compares it across polls to decide whether the
    #: config entry needs reloading. Never touched by a push frame (`push.py`'s `_PARSERS` has
    #: no `"stack"`/`"detected_stack"` entry), so it only ever changes on a fresh poll.
    detected_stack: Stack | None = None


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


def _media_player_entity_id(hass: HomeAssistant, serial: str) -> str | None:
    """The one media_player entity `media_player.py`'s `async_setup_entry` always creates for
    this feeder (`entity.py`'s `unique_id` scheme: `f"{serial}_speaker"`), resolved through the
    entity registry rather than reconstructed from a slugified name -- a user rename must never
    break this. `None` only in the narrow window before that entity has ever registered (mid
    first setup); `media_source.async_resolve_media` treats that exactly like an omitted
    target, the same outcome `target_media_player=None` always meant."""
    return er.async_get(hass).async_get_entity_id("media_player", DOMAIN, f"{serial}_speaker")


async def _resolve_media_to_pcm(
    hass: HomeAssistant, media_content_id: str, entity_id: str | None
) -> bytes:
    """Turns an HA media reference -- a media-source URI (what `tts.speak` produces, among
    others) or a plain music URL -- into raw PCM. `media_source.async_resolve_media` first
    (the same boilerplate every core media_player integration uses for this exact step),
    passed `entity_id` (`_media_player_entity_id`, the feeder's own `KibbleSpeaker`) rather than
    leaving it at its `UNDEFINED` default -- an omitted `target_media_player` trips
    `homeassistant.helpers.frame`'s `report_usage` deprecation warning on every single call,
    logged from this integration's own domain, not from Home Assistant core.
    `async_process_play_media_url` after, so a same-instance URL (a local TTS/media file)
    picks up HA's own auth signature before ffmpeg fetches it over plain HTTP with no HA
    session of its own. A bad/unresolvable reference raises `Unresolvable`, already a
    `HomeAssistantError` -- left to propagate as-is rather than rewrapped."""
    if is_media_source_id(media_content_id):
        played = await async_resolve_media(hass, media_content_id, entity_id)
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
        # Which platforms actually finished `async_setup_entry` -- see `__init__.py`'s
        # per-platform forwarding loop. Populated once, after `async_setup_entry` forwards
        # every platform; `async_unload_entry` only unloads what is in here.
        self.loaded_platforms: list[Platform] = []
        # See the module docstring's availability policy.
        self.consecutive_failures = 0
        self.last_error: str | None = None
        # Push channel (module docstring). `_push_task` is the single-connection guard:
        # `async_start_push` is a no-op while it is alive.
        self._push: KibblePush | None = None
        self._push_task: asyncio.Task[None] | None = None
        self.push_connected = False
        self.push_reconnects = 0
        self.push_last_frame: float | None = None
        self.push_unsupported = False
        # `stacks.py`'s module docstring: the last stack a poll actually confirmed (never set
        # from an undetermined `None` reading -- see `_check_stack_change`), so the entry can
        # be reloaded exactly once when it genuinely changes, and never merely because one
        # cycle's `GET /mode` happened to fail.
        self._last_confirmed_stack: Stack | None = None

    @property
    def feeder_reachable(self) -> bool:
        """True iff the *most recent* contact (poll or push frame) succeeded outright --
        stricter than `.available` (`CoordinatorEntity.available`/`last_update_success`), which
        stays `True` through the tolerance window described in the module docstring. Backs the
        disabled-by-default "Feeder reachable" diagnostic binary sensor."""
        return self.consecutive_failures == 0

    # --- push ------------------------------------------------------------------------------

    @callback
    def async_start_push(self) -> None:
        """Start the push listener as an entry-owned background task. Idempotent: one task."""
        if self._push_task is not None and not self._push_task.done():
            return
        self._push_task = self.entry.async_create_background_task(
            self.hass, self._push_loop(), name="kibble push"
        )

    @callback
    def async_cancel_push(self) -> None:
        """Entry unload: cancel the listener task. The task's own `finally` closes the socket
        (`KibblePush.listen` closes on the way out), so nothing is left open."""
        task, self._push_task = self._push_task, None
        if task is not None:
            task.cancel()
        self._set_push_connected(False)

    async def async_stop_push(self, _event: Event | None = None) -> None:
        """HA stop: cancel and wait, then close the socket cleanly so the agent sees a close
        frame rather than a reset."""
        task, self._push_task = self._push_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._push is not None:
            await self._push.close()
            self._push = None
        self._set_push_connected(False)

    def _set_push_connected(self, connected: bool) -> None:
        if connected == self.push_connected:
            return
        self.push_connected = connected
        # wled: "Stop polling as long as we have a websocket"; restore + refresh on drop.
        self.update_interval = None if connected else timedelta(seconds=DEFAULT_SCAN_INTERVAL)

    async def _push_loop(self) -> None:
        """Connect, consume frames, reconnect with backoff. Runs for the life of the entry."""
        backoff = PUSH_BACKOFF_MIN
        while True:
            push = KibblePush(async_get_clientsession(self.hass), self.entry.data[CONF_HOST])
            self._push = push
            try:
                await self._consume(push)
                backoff = PUSH_BACKOFF_MIN  # a real session ran; start fresh next time
            except KibblePushUnsupported as err:
                if not self.push_unsupported:
                    _LOGGER.info("Feeder agent offers no push channel (%s); polling instead", err)
                self.push_unsupported = True
                return
            except KibblePushClosed as err:
                _LOGGER.log(
                    logging.DEBUG if self.push_reconnects else logging.INFO,
                    "Feeder push channel closed (%s); polling until it reconnects", err,
                )
            finally:
                self._push = None
                if self.push_connected:
                    self._set_push_connected(False)
                    # Pull data now rather than waiting a full fallback interval: the
                    # existing availability policy decides whether the *feeder* is down.
                    self.hass.async_create_task(self.async_request_refresh())
            self.push_reconnects += 1
            # Jittered exponential backoff, capped -- never a reconnect storm.
            await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
            backoff = min(backoff * 2, PUSH_BACKOFF_MAX)

    async def _consume(self, push: KibblePush) -> None:
        last_resync = self.hass.loop.time()
        async for frame in push.listen():
            self.push_last_frame = self.hass.loop.time()
            if frame.type == "hello":
                continue
            if frame.type in ("snapshot", "update"):
                if not self.push_connected:
                    self._set_push_connected(True)
                self._apply_frame(frame)
            if self.hass.loop.time() - last_resync >= PUSH_RESYNC_SECONDS:
                await push.resync()
                last_resync = self.hass.loop.time()

    @callback
    def _apply_frame(self, frame: Frame) -> None:
        """One frame -> `KibbleData`, through the same parsers as the poll. A frame is a
        successful contact: the failure counter resets exactly as a good poll would."""
        if self.data is None:
            return  # first refresh has not completed; the poll path will seed us
        pet_ids = parse_vendor_pet_ids(self.entry.options.get(CONF_VENDOR_PET_IDS, ""))
        data = merge_frame(self.data, frame)
        if "events" in frame.fields:
            data = replace(data, vendor_sightings=vendor_sightings(data.events, pet_ids))
        self._handle_poll_success()
        self.async_set_updated_data(data)

    async def _async_update_data(self) -> KibbleData:
        try:
            async with asyncio.timeout(POLL_TIMEOUT):
                data = await self._fetch_all()
        except (KibbleError, TimeoutError) as err:
            return self._handle_poll_failure(err)
        self._handle_poll_success()
        self._check_stack_change(data)
        return data

    def _check_stack_change(self, data: KibbleData) -> None:
        """Reloads the config entry the first time a poll confirms a DIFFERENT stack than the
        last one it confirmed -- so every platform's `async_setup_entry` reruns and rebuilds
        its entity set against `data.detected_stack` (`stacks.applies_to`).

        `data.detected_stack is None` (this cycle's poll didn't identify a stack) is always a
        no-op: it neither counts as a change nor overwrites `_last_confirmed_stack`, which is
        exactly what keeps a feeder rebooting mid-switch from being read as "changed" -- while
        it is unreachable, `_async_update_data`'s own failure path serves stale data and never
        calls this method at all (see its own call site); once it *does* answer again, either
        it reports the same stack as before (no-op below) or the new one (reload, below) --
        there is no reachable state in between that could trigger a spurious reload.

        The very first confirmation (`_last_confirmed_stack` still `None`, e.g. right after
        `async_config_entry_first_refresh`) only records a baseline; "changed from nothing" is
        not a change `async_setup_entry` needs to rerun for, since it already ran once against
        this exact first reading.

        Reloading is scheduled as a background task, never awaited here: `_async_update_data`
        is a bound method *of* the coordinator a reload would tear down and replace -- awaiting
        it inline would cancel this very call mid-flight. Mirrors `_push_loop`'s own
        `hass.async_create_task(self.async_request_refresh())` fire-and-forget pattern.
        """
        new_stack = data.detected_stack
        if new_stack is None:
            return
        if self._last_confirmed_stack is not None and self._last_confirmed_stack != new_stack:
            _LOGGER.info(
                "Feeder stack changed %s -> %s; reloading the config entry",
                self._last_confirmed_stack,
                new_stack,
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.entry.entry_id)
            )
        self._last_confirmed_stack = new_stack

    async def _fetch_all(self) -> KibbleData:
        """One full snapshot: every read this integration polls, back-to-back over the one
        connection `self.client` serialises (see `api.py`). Bounded from the outside by
        `POLL_TIMEOUT` in `_async_update_data`, not by summing each call's own `api.TIMEOUT`."""
        state = await self.client.state()
        schedule = await _optional(self.client.schedule(), ScheduleState.from_json({"entries": []}))
        config = await _optional(self.client.config(), {})
        cloud = await self.client.cloud()
        try:
            stack = await self.client.mode()
        except KibbleError:
            # An agent old enough to predate `GET /mode` -- the stack select goes unavailable
            # (see `select.py`'s `KibbleStackSelect.available`), not the whole poll cycle: a
            # single missing route on an old agent is not the "confirmed down" signal
            # `_async_update_data`'s own `KibbleError` handling exists for.
            stack = None
        led = await _optional(self.client.led(), None)
        desiccant = await _optional(self.client.desiccant(), None)
        wifi = await self.client.wifi()
        wifi_scan = tuple(await _optional(self.client.wifi_scan(), ()))
        cats = tuple(await _optional(self.client.cats(), ()))
        identify = await _optional(self.client.identify(), IdentifyResult.from_json({}))
        review_face = await _optional(self.client.review_face(), ReviewFace.from_json({}))
        pending_face_count = len(await _optional(self.client.pending_faces(), ()))
        clips = tuple(await _optional(self.client.clips(), ()))
        feeds = tuple(await _optional(self.client.feeds(), ()))
        events = tuple(await _optional(self.client.events(), ()))
        # A malformed option can't reach here: the options flow validates it before saving.
        pet_ids = parse_vendor_pet_ids(self.entry.options.get(CONF_VENDOR_PET_IDS, ""))
        detected_stack = detect_stack(
            mode_running=stack.running if stack is not None else None,
            state_stack_field=state.raw.get("stack"),
        )
        return KibbleData(
            state=state,
            schedule=schedule,
            config=config,
            cloud=cloud,
            stack=stack,
            detected_stack=detected_stack,
            led=led,
            desiccant=desiccant,
            wifi=wifi,
            wifi_scan=wifi_scan,
            cats=cats,
            identify=identify,
            review_face=review_face,
            pending_face_count=pending_face_count,
            clips=clips,
            feeds=feeds,
            events=events,
            vendor_sightings=vendor_sightings(events, pet_ids),
        )

    def _handle_poll_success(self) -> None:
        if self.consecutive_failures:
            _LOGGER.debug(
                "Feeder poll recovered after %s failed attempt(s)", self.consecutive_failures
            )
            self._async_clear_unresponsive_issue()
        self.consecutive_failures = 0
        self.last_error = None

    def _handle_poll_failure(self, err: Exception) -> KibbleData:
        """Below `CONSECUTIVE_FAILURES_FOR_UNAVAILABLE`, with a prior snapshot to fall back on:
        re-serve it so `last_update_success`/entity availability don't flip for what is, per the
        module docstring, this device's ordinary noise floor. At or past the threshold, or with
        no prior snapshot (the very first refresh -- see `async_config_entry_first_refresh`),
        raise so HA's own coordinator marks entities unavailable for real."""
        self.consecutive_failures += 1
        self.last_error = str(err) or repr(err)
        if self.data is not None and self.consecutive_failures < CONSECUTIVE_FAILURES_FOR_UNAVAILABLE:
            _LOGGER.warning(
                "Feeder poll %s/%s failed (%s); showing last-known values while it recovers",
                self.consecutive_failures,
                CONSECUTIVE_FAILURES_FOR_UNAVAILABLE - 1,
                self.last_error,
            )
            return self.data
        if self.data is not None:
            self._async_create_unresponsive_issue()
        raise UpdateFailed(self.last_error, retry_after=self._retry_after_seconds()) from err

    def _retry_after_seconds(self) -> float:
        """Exponential backoff once the feeder is confirmed down (at or past the tolerance
        threshold), capped at `MAX_RETRY_AFTER` -- polling a device that has not answered in
        three straight tries every `DEFAULT_SCAN_INTERVAL` regardless just adds load to
        whatever eventually restarts it."""
        overage = self.consecutive_failures - CONSECUTIVE_FAILURES_FOR_UNAVAILABLE
        return min(MAX_RETRY_AFTER, DEFAULT_SCAN_INTERVAL * (2 ** max(overage, 0)))

    def _async_create_unresponsive_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_FEEDER_UNRESPONSIVE}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_FEEDER_UNRESPONSIVE,
            translation_placeholders={
                "name": self.entry.title,
                "failures": str(self.consecutive_failures),
                "error": self.last_error or "",
            },
        )

    def _async_clear_unresponsive_issue(self) -> None:
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_FEEDER_UNRESPONSIVE}_{self.entry.entry_id}"
        )

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
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="schedule_writes_disabled",
            )

    def card_entries(self) -> list[ScheduleEntry]:
        """Every entry, enabled or not, in the one fixed order both the packed card state and
        the card-facing services share: the entry's position here is its card-visible id.
        Disabled entries keep their slot (the card just does not see them) so an index the card
        read stays valid across a toggle."""
        return sorted(self.data.schedule.entries, key=lambda e: (e.time, e.id))

    def resolve_card_entry_id(self, card_id: str) -> str:
        """An id from the schedule card is either an entry's real id or -- the card's own
        scheme -- the entry's index in `card_entries`."""
        entries = self.card_entries()
        if any(e.id == card_id for e in entries):
            return card_id
        if card_id.isdigit() and int(card_id) < len(entries):
            return entries[int(card_id)].id
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_schedule_entry",
            translation_placeholders={"entry_id": card_id},
        )

    async def async_schedule_card_add(self, time: str, amount: int) -> None:
        """One `amount` mirrored onto both `amount_l`/`amount_r` -- the same shared-bin
        simplification the primary Feed button already makes. The agent mints the id."""
        self._require_schedule_writes_enabled()
        await self.async_schedule_add(time, amount, amount, True, None)

    async def async_schedule_card_edit(self, card_id: str, time: str, amount: int) -> None:
        """No native edit exists -- `schedule.rs`'s `add` rejects a duplicate id
        (STUDY-schedule.md) -- so this removes and re-adds under the same id. Always
        refreshes, even on a failure between the two calls, so a partial edit is reflected
        immediately rather than waiting for the next poll."""
        self._require_schedule_writes_enabled()
        entry_id = self.resolve_card_entry_id(card_id)
        try:
            await self.client.remove_schedule_entry(entry_id)
            await self.client.add_schedule_entry(time, amount, amount, True, entry_id)
        finally:
            await self.async_request_refresh()

    async def async_schedule_card_remove(self, card_id: str) -> None:
        self._require_schedule_writes_enabled()
        await self.async_schedule_remove(self.resolve_card_entry_id(card_id))

    async def async_schedule_card_toggle(self, card_id: str) -> None:
        """Server-side toggle (docs/custom.md's `actions.toggle`): flips the entry's own
        current `enabled` state rather than taking one from the caller."""
        self._require_schedule_writes_enabled()
        entry_id = self.resolve_card_entry_id(card_id)
        entry = next(e for e in self.data.schedule.entries if e.id == entry_id)
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

    async def async_set_mode(self, mode: str) -> None:
        """Switch the running feeder userland (`"vendor"` or `"librefeed"`). Unlike
        `async_set_cloud`/`async_wifi_connect`, this does not refresh afterwards: the agent
        reboots ~1s after acknowledging the request (`agent/src/mode.rs`), so an immediate
        refresh would just race the reboot and surface as a spurious poll failure instead of
        the switch it actually is. The coordinator's normal poll cadence -- and its
        tolerance for a few failed cycles, see the module docstring -- picks the new state
        back up once the reboot completes."""
        await self.client.set_mode(mode)

    async def async_set_led(
        self,
        *,
        white: str | int | None = None,
        green: int | None = None,
        camera: str | int | None = None,
    ) -> None:
        """Write the status LED, then refresh so the new value reflects immediately. Mirrors
        `async_set_config`: unlike `async_set_mode`, there is no reboot to race here."""
        await self.client.set_led(white=white, green=green, camera=camera)
        await self.async_request_refresh()

    async def async_beep(
        self, *, count: int = 2, on_ms: int = 100, off_ms: int = 100
    ) -> dict:
        """Plays the MCU buzzer. Refreshes on any outcome -- same "always reconcile" shape as
        `async_speak`: there is no "is beeping" flag in `GET /state` either, and a 404 (vendor
        stack) or 400 (out-of-range) still deserves a fresh poll. Propagates `KibbleError` to
        the caller uncaught, same as every other `async_*` write here."""
        try:
            return await self.client.beep(count=count, on_ms=on_ms, off_ms=off_ms)
        finally:
            await self.async_request_refresh()

    async def async_set_desiccant(
        self,
        *,
        replaced: bool | None = None,
        days_left: int | None = None,
        interval_days: int | None = None,
    ) -> None:
        """Write the desiccant counter, then refresh so the new value reflects immediately --
        same "write then refresh" shape as `async_set_led`. The agent's `POST /desiccant`
        accepts only one field per call (`api.py`'s `set_desiccant`); `kibble.set_desiccant`
        lets an operator set `days_left` and `interval_days` in the same service call, so
        those two are issued as sequential writes here rather than exposing that one-field-
        per-call quirk to the caller. `replaced` (the button) is always given alone. Refreshes
        exactly once, after every requested write, even if an earlier one raised."""
        try:
            if replaced is not None:
                await self.client.set_desiccant(replaced=replaced)
            if days_left is not None:
                await self.client.set_desiccant(days_left=days_left)
            if interval_days is not None:
                await self.client.set_desiccant(interval_days=interval_days)
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

    # The face-store writes use `async_refresh`, not `async_request_refresh`: the latter is
    # debounced, so it returns before the new data exists. The cats card reads its own write
    # the moment the service call resolves (re-querying `kibble/cats` for the sample counts and
    # `kibble/faces/pending` for the queue), and with a debounced refresh it read the *previous*
    # snapshot -- a crop visibly moved into a cat's gallery while that cat still claimed
    # "0 samples". Hand-labelling is a handful of calls, not a burst, so coalescing buys
    # nothing here.
    async def async_label_face(self, crop_id: str, cat: str) -> None:
        await self.client.label_face(crop_id, cat)
        await self.async_refresh()

    async def async_unlabel_face(self, crop_id: str, cat: str) -> None:
        await self.client.unlabel_face(crop_id, cat)
        await self.async_refresh()

    async def async_add_cat(self, name: str) -> None:
        await self.client.add_cat(name)
        await self.async_refresh()

    async def async_delete_cat(self, name: str) -> dict:
        """Same immediate-refresh reasoning as the other face-store writes above: the cats
        card's own delete confirmation re-queries `kibble/cats` right after this resolves and
        must not still see the deleted cat."""
        result = await self.client.delete_cat(name)
        await self.async_refresh()
        return result

    async def async_upload_face_sample(self, cat: str, jpeg: bytes) -> dict:
        result = await self.client.upload_face_sample(cat, jpeg)
        await self.async_refresh()
        return result

    async def async_delete_face_sample(self, cat: str, name: str) -> dict:
        result = await self.client.delete_face_sample(cat, name)
        await self.async_refresh()
        return result

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
        `/clips/<name>`). Passes the feeder's own `KibbleSpeaker` entity id to `media_source.
        async_resolve_media` either way: it is the entity actually about to render this media
        for `async_play_media_content`, and the closest thing this integration has to "the
        target player" for `async_save_clip` too, since there is exactly one per feeder."""
        entity_id = _media_player_entity_id(self.hass, self.data.state.serial)
        return await _resolve_media_to_pcm(self.hass, media_content_id, entity_id)

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
