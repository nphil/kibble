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
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
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
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ClipInfo,
    CloudState,
    DesiccantState,
    DetectionEvent,
    FeederState,
    FeedRecord,
    KibbleClient,
    KibbleError,
    KibbleMediaError,
    KibbleNotFoundError,
    LedState,
    ScheduleState,
    StackState,
    WifiNetwork,
    WifiState,
)
from .ble_fallback import async_feed_with_fallback
from . import bowl_fill
from .const import (
    CONF_BLE_ADDRESS,
    CONF_ENABLE_SCHEDULE_WRITES,
    CONF_HOST,
    CONF_RETENTION_DAYS,
    CONF_STREAM_URL,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_RTSP_PATH,
    DEFAULT_RTSP_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HOPPER_1,
    HOPPER_BOTH,
    ISSUE_FEEDER_UNRESPONSIVE,
    MAX_AMOUNT,
)
from .ingest import IdentityEngine, Ingestor
from .store import DeviceIdentitySummary, KibbleStore
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
    every stored audio clip, every before/after dish-snapshot record (`agent/src/
    feed_capture.rs`), every onboard visit/eat track, and each hopper's bowl-fill calibration
    curve (`GET /calibration`, LibreFeed-only -- see `calibration` below).

    `identity` is not part of the poll at all: it is HA's own identity engine's read of the
    store (`store.identity_summary`), pushed in by `KibbleCoordinator.
    async_refresh_identity_snapshot` whenever ingest or a store mutation could have changed it,
    and simply carried forward unchanged by every poll/push cycle in between -- see
    `_fetch_all`/`_apply_frame`."""

    state: FeederState
    schedule: ScheduleState
    config: dict[str, int]
    cloud: CloudState
    wifi: WifiState
    wifi_scan: tuple[WifiNetwork, ...]
    clips: tuple[ClipInfo, ...]
    feeds: tuple[FeedRecord, ...]
    events: tuple[DetectionEvent, ...]
    identity: DeviceIdentitySummary
    #: `None` on agents that predate `GET /mode` (the stack select is unavailable then).
    stack: StackState | None = None
    #: `None` on the vendor stack (`GET /led` is a LibreFeed-only route -- see `light.py`'s
    #: `KibbleStatusLight.available`).
    led: LedState | None = None
    #: `None` on the vendor stack (`GET /desiccant` is a LibreFeed-only route -- the vendor's
    #: equivalent counter is cloud-set, not agent-served; see `button.py`'s
    #: `KibbleReplaceDesiccantButton.available`).
    desiccant: DesiccantState | None = None
    #: Both hoppers' bowl-fill calibration curves, as the agent's raw JSON (`{"hoppers":
    #: [...]}`) -- `None` on the vendor stack, or a LibreFeed agent old enough to predate this
    #: route (`GET /calibration` is LibreFeed-only -- see `sensor.py`'s
    #: `KibbleCalibrationSensor.available`). Kept as the untyped dict `calibration()` returns,
    #: not a parsed dataclass: `websocket.py`'s `kibble/calibration` forwards it to the card
    #: unchanged, and `sensor.py`'s per-hopper state/attributes read it as plain JSON -- there
    #: is no second consumer here that would benefit from an intermediate Python shape.
    calibration: dict[str, Any] | None = None
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
        self,
        hass: HomeAssistant,
        entry: KibbleConfigEntry,
        client: KibbleClient,
        store: KibbleStore,
        engine: IdentityEngine,
        ingestor: Ingestor,
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
        self.store = store
        self.engine = engine
        self.ingestor = ingestor
        # Which transport last actually carried (or was attempted for) a feed command; the
        # "Control path" diagnostic sensor reads this directly. `None` until the first feed.
        self.control_path: str | None = None
        # The hopper divider is out: one shared bin (`switch.py`'s `KibbleHopperDividerSwitch`
        # owns and restores this). Every feed amount then means the whole serving from that bin.
        self.single_hopper = False
        # Local food-name labels for each hopper (`text.py`'s `KibbleHopperFoodText`), pushed
        # in the same way as `single_hopper` above. Only meaningful in dual mode; index 0/1 is
        # hopper 1/2. `None` means unnamed.
        self._hopper_food: list[str | None] = [None, None]
        # Whether `ingest.py`'s auto-learn may add new training samples on its own
        # (`switch.py`'s `KibbleAutoLearnSwitch` owns and restores this, same local-only
        # pattern as `single_hopper` above). Human labels and uploads always still train
        # regardless -- this only gates samples nobody reviewed.
        self.auto_learn_enabled = True
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
        # Ingest (docs/36-ai-pipeline.md): one pass in flight at a time, same guard shape as
        # the poll/push paths' own single-in-flight discipline.
        self._ingest_task: asyncio.Task[None] | None = None
        # Bowl-fill estimate (bowl_fill.py): the last real (non-estimated) camera reading, each
        # hopper's learned fill-per-portion rate (seeded from the store below), before/after
        # brackets still waiting on the camera to confirm, the estimate currently overriding
        # `sensor.py`'s `KibbleBowlFillSensor`, and when that override should stop trusting
        # itself over a real reading that has since caught up (`None` once a fresh reading
        # supersedes it). All local-only, never round-tripped to the device -- same footing as
        # `single_hopper`/`_hopper_food` above.
        self._bowl_fill_last_measured: float | None = None
        self._bowl_fill_learned: dict[str, tuple[float, int]] = {}
        self._bowl_fill_pending: list[bowl_fill.PendingFillSample] = []
        self._bowl_fill_estimate: tuple[float, dict[str, Any]] | None = None
        self._bowl_fill_clear_after: float | None = None

    async def async_setup_store(self) -> None:
        """Opens the SQLite store and loads any existing training into the identity engine.
        Called once, before the first refresh, so ingest can classify from it immediately."""
        await self.store.async_setup()
        for bucket in ("hopper1", "hopper2"):
            learned = await self.store.async_get_bowl_fill_learning(bucket)
            if learned is not None:
                self._bowl_fill_learned[bucket] = learned
        await self.engine.async_rebuild()

    def async_start_retention(self) -> Callable[[], None]:
        """Runs one retention purge now (background task) and schedules an hourly one for the
        life of the entry -- docs/36-ai-pipeline.md: "Runs at startup and hourly". Returns the
        unsubscribe callback for `entry.async_on_unload`."""
        self.hass.async_create_task(self._async_purge())
        return async_track_time_interval(self.hass, self._async_purge_scheduled, timedelta(hours=1))

    async def _async_purge_scheduled(self, _now: datetime) -> None:
        await self._async_purge()

    def retention_days(self) -> int:
        return self.entry.options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)

    def retention_cutoff(self) -> int:
        """The unix timestamp below which an event/feed is outside retention -- the one place
        this conversion happens, shared by the purge job and every reclassify-after-training-
        change call (`websocket.py`)."""
        return int(time.time()) - self.retention_days() * 86400

    async def _async_purge(self) -> None:
        try:
            await self.store.async_purge(self.retention_days())
        except Exception:  # noqa: BLE001 -- a purge failure must never crash the schedule
            _LOGGER.exception("Kibble retention purge failed")

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
        data = merge_frame(self.data, frame)
        self._handle_poll_success()
        self.async_set_updated_data(data)
        if "events" in frame.fields or "feeds" in frame.fields:
            self._schedule_ingest(data.events, data.feeds)

    async def _async_update_data(self) -> KibbleData:
        try:
            async with asyncio.timeout(POLL_TIMEOUT):
                data = await self._fetch_all()
        except (KibbleError, TimeoutError) as err:
            return self._handle_poll_failure(err)
        self._handle_poll_success()
        self._check_stack_change(data)
        self._schedule_ingest(data.events, data.feeds)
        return data

    def _schedule_ingest(
        self, events: Sequence[DetectionEvent], feeds: Sequence[FeedRecord]
    ) -> None:
        if self._ingest_task is None or self._ingest_task.done():
            self._ingest_task = self.hass.async_create_task(self._async_run_ingest(events, feeds))

    async def _async_run_ingest(
        self, events: Sequence[DetectionEvent], feeds: Sequence[FeedRecord]
    ) -> None:
        """Runs off the coordinator path -- see `ingest.Ingestor`'s own module docstring for
        the durable-write-then-ack ordering and idempotency this relies on."""
        try:
            await self.ingestor.async_ingest(events, feeds)
        except Exception:  # noqa: BLE001 -- one bad ingest pass must never crash the poll loop
            _LOGGER.exception("Kibble ingest failed")
            return
        await self.async_refresh_identity_snapshot()

    async def async_refresh_identity_snapshot(self) -> None:
        """Recomputes the identity summary the cat entities read and pushes it through the
        normal coordinator update path -- the same mechanism `kibble/timeline/subscribe`
        already listens on, so a label, a `kibble/cats/add`/`/delete`, or a
        `kibble/training/remove` all reach subscribers and entities the same way an ingest
        pass does."""
        summary = await self.store.async_identity_summary()
        if self.data is not None:
            self.async_set_updated_data(replace(self.data, identity=summary))

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
        calibration = await _optional(self.client.calibration(), None)
        wifi = await self.client.wifi()
        wifi_scan = tuple(await _optional(self.client.wifi_scan(), ()))
        clips = tuple(await _optional(self.client.clips(), ()))
        feeds = tuple(await _optional(self.client.feeds(), ()))
        events = tuple(await _optional(self.client.events(), ()))
        detected_stack = detect_stack(
            mode_running=stack.running if stack is not None else None,
            state_stack_field=state.raw.get("stack"),
        )
        # Identity is never part of the poll -- it is pushed in separately by ingest/store
        # mutations (see `async_refresh_identity_snapshot`) and simply carried forward
        # unchanged here, the same way `push.merge_frame` never touches it either.
        identity = self.data.identity if self.data is not None else DeviceIdentitySummary.empty()
        return KibbleData(
            state=state,
            schedule=schedule,
            config=config,
            cloud=cloud,
            stack=stack,
            detected_stack=detected_stack,
            led=led,
            desiccant=desiccant,
            calibration=calibration,
            wifi=wifi,
            wifi_scan=wifi_scan,
            clips=clips,
            feeds=feeds,
            events=events,
            identity=identity,
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
        self, hopper: str, amount: int, feed_id: str | None, amount2: int | None = None
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
                self.hass,
                address,
                hopper=hopper,
                amount=amount,
                feed_id=feed_id,
                amount2=amount2,
            )

        return _attempt

    async def async_feed(
        self,
        hopper: str,
        amount: int,
        feed_id: str | None = None,
        amount2: int | None = None,
    ) -> None:
        """Dispense over Wi-Fi; on a connection error, fall back to BLE if `ble_address` is
        configured (`docs/25-ble-feed-frame.md`). Always refreshes afterwards and always
        records which transport was used/attempted on `self.control_path` -- the "Control
        path" sensor -- even when the call ultimately fails.

        `amount2`, when given, is hopper 2's own share of a `hopper="both"` split feed --
        `None` (the default) keeps every pre-existing caller's behaviour of dispensing
        `amount` from each side (both the agent/daemon and `api.feed` default it to `amount`
        themselves when omitted). Ignored outright for a single-hopper feed (`hopper` is never
        "both" by the time it reaches the client below).

        With the divider out, "both" would run both dispensers under one bin and serve twice
        the amount asked for, so it goes through dispenser 1 alone instead."""
        if self.single_hopper and hopper == HOPPER_BOTH:
            hopper = HOPPER_1

        async def _wifi_feed() -> None:
            await self.client.feed(hopper, amount, feed_id, amount2)

        ble_feed = self._ble_feed(hopper, amount, feed_id, amount2)
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

    def card_amounts(self, amount: int) -> tuple[int, int]:
        """The per-dispenser split for one schedule-card `amount`: mirrored onto both sides with
        the divider in (the same per-side meaning the primary Feed button has), all from
        dispenser 1 with it out, where `amount` is the whole serving from the one bin."""
        return (amount, 0) if self.single_hopper else (amount, amount)

    async def async_schedule_card_add(self, time: str, amount: int) -> None:
        """The agent mints the id; see `card_amounts` for the per-side split."""
        self._require_schedule_writes_enabled()
        amount_l, amount_r = self.card_amounts(amount)
        await self.async_schedule_add(time, amount_l, amount_r, True, None)

    async def async_schedule_card_edit(self, card_id: str, time: str, amount: int) -> None:
        """No native edit exists -- `schedule.rs`'s `add` rejects a duplicate id
        (STUDY-schedule.md) -- so this removes and re-adds under the same id. Always
        refreshes, even on a failure between the two calls, so a partial edit is reflected
        immediately rather than waiting for the next poll."""
        self._require_schedule_writes_enabled()
        entry_id = self.resolve_card_entry_id(card_id)
        try:
            await self.client.remove_schedule_entry(entry_id)
            amount_l, amount_r = self.card_amounts(amount)
            await self.client.add_schedule_entry(time, amount_l, amount_r, True, entry_id)
        finally:
            await self.async_request_refresh()

    async def async_set_auto_learn_enabled(self, enabled: bool) -> None:
        """Local-only, no feeder round trip -- see `auto_learn_enabled`'s own comment."""
        self.auto_learn_enabled = enabled
        self.async_update_listeners()

    async def async_set_single_hopper(self, single: bool) -> None:
        """Switch between two compartments and one shared bin. Scheduled feeds are converted
        so each one keeps serving the same total amount of food: two sides' portions pour into
        dispenser 1 when the divider comes out, and split back across both sides (the odd
        portion on side 1) when it goes back in. The feeder runs its schedule on its own and
        cannot know about the divider, so this is the only place that keeps it consistent.
        Skipped, with a warning, while schedule writes are turned off in the options."""
        if single == self.single_hopper:
            return
        self.single_hopper = single
        self.async_update_listeners()
        if not self.entry.options.get(CONF_ENABLE_SCHEDULE_WRITES, False):
            if self.data.schedule.entries:
                _LOGGER.warning(
                    "Hopper divider changed, but schedule writes are off: scheduled feeds were not converted"
                )
            return
        try:
            for entry in self.data.schedule.entries:
                total = entry.amount_l + entry.amount_r
                if single:
                    amount_l, amount_r = min(total, MAX_AMOUNT), 0
                else:
                    amount_l, amount_r = (total + 1) // 2, total // 2
                if (amount_l, amount_r) == (entry.amount_l, entry.amount_r):
                    continue
                await self.client.remove_schedule_entry(entry.id)
                await self.client.add_schedule_entry(entry.time, amount_l, amount_r, entry.enabled, entry.id)
        finally:
            await self.async_request_refresh()

    def hopper_food(self, n: int) -> str | None:
        """The user-typed food name for hopper `n` (1 or 2), or `None` when unnamed. Only
        meaningful in dual mode -- kept current by `text.py`'s `KibbleHopperFoodText`, the
        same way `single_hopper` above is kept current by the divider switch."""
        return self._hopper_food[n - 1]

    @callback
    def async_set_hopper_food(self, n: int, value: str) -> None:
        """Pushed by `KibbleHopperFoodText` on restore and on every write. Notifies listeners
        so `kibble/timeline/subscribe` re-renders any feed row that falls back to this name for
        an unnamed hopper (`websocket.py`'s `_feed_view`)."""
        name = value or None
        if name == self._hopper_food[n - 1]:
            return
        self._hopper_food[n - 1] = name
        self.async_update_listeners()

    def bowl_fill_per_portion(self, bucket: str) -> tuple[float, int]:
        """`bucket`'s current fill-per-portion rate and how many real samples produced it --
        the learned EWMA once at least one real sample exists, else the calibration-or-constant
        default (`bowl_fill.default_fill_per_portion`) with a `0` sample count."""
        learned = self._bowl_fill_learned.get(bucket)
        if learned is not None:
            return learned
        hopper_index = 0 if bucket == "hopper1" else 1
        hoppers = (self.data.calibration or {}).get("hoppers") or []
        entry = hoppers[hopper_index] if hopper_index < len(hoppers) else None
        return (bowl_fill.default_fill_per_portion(entry), 0)

    @property
    def bowl_fill_estimate(self) -> tuple[float, dict[str, Any]] | None:
        """`(value, attrs)` currently overriding `sensor.py`'s `KibbleBowlFillSensor`, or `None`
        while nothing is -- see `bowl_fill.py`'s module doc for the full lifecycle."""
        return self._bowl_fill_estimate

    @callback
    def bowl_fill_settle_pending(self) -> None:
        """Every ingest pass' bracket bookkeeping (`bowl_fill.py`): taints outstanding brackets
        the instant eating is observed, resolves whatever just reached its own settle deadline
        into a learned EWMA sample (persisted to the store in the background), and expires
        anything the camera never confirmed. Also clears the currently-displayed estimate back
        to `None` once its own settle window has passed and a real reading is available --
        independent of whether any particular bracket exists or resolved cleanly, so a "both
        hopper" feed's estimate (which registers no bracket at all) is never left stuck forever.
        Called once per `Ingestor.async_ingest` pass, before that pass looks at any individual
        feed."""
        now = time.time()
        if self.data is None or self.data.state is None:
            return
        state = self.data.state
        if state.eating:
            bowl_fill.mark_eating_seen(self._bowl_fill_pending)
        still_pending, resolved = bowl_fill.resolve_ready(self._bowl_fill_pending, now, state.bowl_fill)
        self._bowl_fill_pending = bowl_fill.expire_stale(still_pending, now)
        for bucket, sample in resolved:
            learned = bowl_fill.ewma_update(self._bowl_fill_learned.get(bucket), sample)
            self._bowl_fill_learned[bucket] = learned
            self.hass.async_create_task(
                self.store.async_set_bowl_fill_learning(bucket, learned[0], learned[1])
            )
        if (
            self._bowl_fill_estimate is not None
            and self._bowl_fill_clear_after is not None
            and now >= self._bowl_fill_clear_after
            and state.bowl_fill is not None
        ):
            # The camera has had a fair settle window since the last feed applied, and has a
            # real reading available: that reading is authoritative again, regardless of
            # whether it happened to resolve (or even have) a learning bracket of its own.
            self._bowl_fill_estimate = None
            self._bowl_fill_clear_after = None
            self.async_update_listeners()
        if state.bowl_fill is not None:
            self._bowl_fill_last_measured = state.bowl_fill

    @callback
    def async_apply_bowl_fill_feed(self, feed: FeedRecord) -> None:
        """Registers this brand-new feed's immediate estimate (and, for a single-sided
        dispense, a before/after bracket for `bowl_fill_settle_pending` to eventually learn
        from) -- called by `Ingestor._ingest_feed` exactly once per feed uid, the same "first
        INSERT only" moment that freezes `amount1`/`amount2`/`food1`/`food2`/`single` onto that
        feed's own store row. A "both" feed still moves the estimate (using both buckets' own
        current rates), but registers no bracket: a single shared delta can't be cleanly split
        back into two per-food rates, so it would teach neither bucket anything trustworthy. Any
        bracket already in flight is superseded (poisoned, never learned from) by this feed's
        own dispense landing before that bracket's own window closes."""
        baseline = self._bowl_fill_estimate[0] if self._bowl_fill_estimate is not None else self._bowl_fill_last_measured
        if baseline is None:
            return  # nothing measured yet at all -- no floor to add portions onto
        sides = ((1, feed.amount1 or 0), (2, feed.amount2 or 0))
        active = [(hopper, portions) for hopper, portions in sides if portions > 0]
        if not active:
            return
        bowl_fill.mark_superseded(self._bowl_fill_pending)
        delta = sum(portions * self.bowl_fill_per_portion(f"hopper{hopper}")[0] for hopper, portions in active)
        if len(active) == 1:
            hopper, portions = active[0]
            self._bowl_fill_pending.append(
                bowl_fill.PendingFillSample(
                    bucket=f"hopper{hopper}",
                    fill_before=baseline,
                    portions=portions,
                    ready_at=time.time() + bowl_fill.SETTLE_SECONDS,
                )
            )
        fill_per_portion_1, samples_1 = self.bowl_fill_per_portion("hopper1")
        fill_per_portion_2, samples_2 = self.bowl_fill_per_portion("hopper2")
        self._bowl_fill_estimate = (
            bowl_fill.estimate_value(baseline, delta),
            {
                "fill_per_portion": [round(fill_per_portion_1, 2), round(fill_per_portion_2, 2)],
                "samples": [samples_1, samples_2],
            },
        )
        self._bowl_fill_clear_after = time.time() + bowl_fill.SETTLE_SECONDS
        self.async_update_listeners()

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

    async def async_call_cats(self) -> dict:
        """Plays the feed cue on demand ("call the cats") without dispensing anything. Same
        "always reconcile" shape as `async_beep`/`async_speak`: no "cue is playing" flag exists
        in `GET /state` to poll for, and even a 429 (cooldown) or 404 (vendor stack) deserves a
        fresh poll. Propagates `KibbleError` (including `KibbleCueCooldownError`) to the caller
        uncaught, same as every other `async_*` write here."""
        try:
            return await self.client.call_cats()
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

    async def async_mark_hopper_full(self, hopper: str) -> None:
        """Tells the daemon `hopper` (`"1"`/`"2"`/`"both"`) was just physically refilled to
        capacity, then refreshes so `sensor.*_hopper_N_remaining` reflects the reset counters
        immediately."""
        try:
            await self.client.mark_hopper_full(hopper)
        finally:
            await self.async_request_refresh()

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

    async def async_calibration_action(self, action: str, hopper: int, **fields: Any) -> dict:
        """One calibration-wizard step (`api.py`'s `calibration_action`), immediately
        refreshed on success -- same `async_refresh` (not the debounced `async_request_
        refresh`) as the face-store writes above, and for the identical reason: the wizard
        re-queries `kibble/calibration` right after this resolves to show the just-recorded
        point/curve, and a debounced refresh would still be serving the previous snapshot
        when that read lands.

        Unlike `async_beep`/`async_speak`/`async_wifi_connect`, this does NOT refresh in a
        `finally` -- every failure mode here (`KibbleCalibrationBusyError`'s 409, a malformed-
        step 400, an old agent's 404) is a clean no-op on the daemon's own side (`point`
        "refuses rather than recording", per its own contract), unlike a Wi-Fi write that can
        roll itself back mid-failure, so there is nothing for a refresh to reconcile when this
        raises -- and skipping it means a cat wandering over the bowl mid-wizard doesn't also
        force a full poll on every retry. Propagates `KibbleCalibrationBusyError`/`KibbleError`
        to the caller uncaught, same as every other `async_*` write here.

        Never dispenses food -- see `api.py`'s `calibration_action` docstring."""
        result = await self.client.calibration_action(action, hopper, **fields)
        await self.async_refresh()
        return result
