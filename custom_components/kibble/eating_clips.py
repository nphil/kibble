"""Links a closed eat session to the Scrypted clip that recorded it, and the pure pieces that
decide *which* clip -- see `docs/39-eating-clips.md` for the full design.

Pipeline: the Kibble mixin on the pet camera (Scrypted device 240) emits an `ObjectDetector`
event for every poll while a cat eats; Scrypted's Events Recorder plugin (triggered by that
same Animal detection) records one MP4 spanning the meal, appearing on disk (atomic rename)
only once its own post-roll finishes. Two plain, unauthenticated LAN endpoints expose it:

- `GET {base_url}/endpoint/@nphil/kibble-scrypted/public/clips?start=<ms>&end=<ms>` ->
  `[{"videoId", "startTime", "endTime", "duration", "detectionClasses"}]`, times in ms.
- `GET {base_url}/endpoint/@apocaliss92/scrypted-events-recorder/public/videoclip?params=
  <urlencoded {"deviceId": SCRYPTED_DEVICE_ID, "filename": videoId}>` -> the MP4 itself, Range
  passthrough (`views.KibbleClipView` proxies this one; `ClipLinker` only ever calls the first).

`ClipLinker` owns nothing durable itself: every outcome lands in the store via
`KibbleStore.async_set_event_clip`, so losing the in-memory retry schedule on a restart loses
nothing that `async_relink_recent`'s own sweep cannot recover.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_SCRYPTED_CLIPS_URL

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .store import KibbleStore

_LOGGER = logging.getLogger(__name__)

# The pet camera in Scrypted's own device registry (`@scrypted/onvif`) -- confirmed live,
# 2026-09-25 (`docs/39-eating-clips.md`'s "Findings" section). Device 238 is the old RTSP
# device of the same camera and is never the one recording; nothing here is configurable
# per-install because nothing else about this deployment's Scrypted is either.
SCRYPTED_DEVICE_ID = "240"

# A clip's on-disk filename, confirmed against the recorder's own source
# (`@apocaliss92/scrypted-events-recorder` 0.0.51's bundled `main.nodejs.js`,
# `getVideoClipName`/`videoClipRegex`, read 2026-09-25 via
# `/mnt/nvme/appdata/scrypted/plugins/@apocaliss92/scrypted-events-recorder/zip/unzipped/` on
# beastnas): `` `${startTime}_${endTime}_${bits}` `` where `startTime`/`endTime` are bare
# `Date.now()`-style millisecond epoch numbers (13 digits until the year 2286, never zero-
# padded) and `bits` is a FIXED 10-character array of "0"/"1" (`new Array(10).fill(0)`) even
# though only 7 detection classes are ever indexed into it (Motion=0, Person=1, Vehicle=2,
# Animal=3, Face=4, Plate=5, Package=6) -- indices 7-9 are always "0", reserved. The list
# endpoint's `videoId` is exactly this string, with no extension (checked live 2026-09-25; the
# recorder's `videoclip` webhook serves it as is). Strict on purpose: this is the one gate
# against ever handing an attacker-chosen string to the recorder's own webhook (no SSRF, no
# path traversal -- the pattern has no `.` or `/` anywhere a real match could hide one).
VIDEO_ID_RE = re.compile(r"^\d{13}_\d{13}_[01]{10}$")


def is_valid_video_id(video_id: str) -> bool:
    """Whether `video_id` is safe to hand to the recorder's `videoclip` webhook at all --
    `ClipLinker`'s and `views.KibbleClipView`'s only gate against ever forwarding something
    that is not actually one of the recorder's own filenames."""
    return bool(VIDEO_ID_RE.match(video_id))


@dataclass(frozen=True, slots=True)
class ClipCandidate:
    """One recorded eating clip, as returned by the Scrypted clips-list endpoint -- only the
    fields `best_overlap_clip` actually needs."""

    video_id: str
    start_ms: int
    end_ms: int


def parse_clip_candidates(payload: Any) -> list[ClipCandidate]:
    """Parses the clips-list endpoint's JSON body into `ClipCandidate`s, dropping (never
    raising on) anything malformed -- an unexpected shape from a server this integration does
    not control must never crash the ingest path that ultimately calls this. A `videoId`
    failing `is_valid_video_id` is dropped here too: nothing past this function ever sees an id
    that would fail that gate."""
    if not isinstance(payload, list):
        return []
    out: list[ClipCandidate] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        video_id = item.get("videoId")
        if not isinstance(video_id, str) or not is_valid_video_id(video_id):
            continue
        try:
            start_ms = int(item.get("startTime"))
            end_ms = int(item.get("endTime"))
        except (TypeError, ValueError):
            continue
        if end_ms <= start_ms:
            continue
        out.append(ClipCandidate(video_id=video_id, start_ms=start_ms, end_ms=end_ms))
    return out


def _overlap_ms(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def best_overlap_clip(
    candidates: Sequence[ClipCandidate], session_start_ms: int, session_end_ms: int
) -> ClipCandidate | None:
    """The recorded clip that best represents one eat session: the candidate whose own span
    overlaps the session's own REAL `[session_start_ms, session_end_ms)` the most -- not the
    padded window `ClipLinker` queried Scrypted over. That padding exists only to catch a clip
    whose pre/post-roll starts before or ends after the session's own timestamps (clock skew
    between the feeder and Scrypted, or the recorder's own `postEventSeconds` tail); once
    candidates are in hand, scoring against the session's own true bounds is what correctly
    picks the right one over a neighbouring session's clip that the same padded query also
    happened to return (a clip merely touching the edge of the window scores 0 or near it,
    while the clip that actually contains the session scores its full length). Ties (equal
    overlap) resolve to the earliest-starting clip, for a deterministic pick. `None` when
    nothing overlaps at all -- a session with no recording (Events Recorder never triggered, or
    the clip was already pruned by quota) must never link to an unrelated neighbour just
    because the query returned something."""
    best: ClipCandidate | None = None
    best_overlap = 0
    for cand in candidates:
        overlap = _overlap_ms(cand.start_ms, cand.end_ms, session_start_ms, session_end_ms)
        if overlap <= 0:
            continue
        if best is None or overlap > best_overlap or (overlap == best_overlap and cand.start_ms < best.start_ms):
            best = cand
            best_overlap = overlap
    return best


def _strip_trailing_slash(base_url: str) -> str:
    return base_url[:-1] if base_url.endswith("/") else base_url


def clips_list_url(base_url: str, start_ms: int, end_ms: int) -> str:
    """The `@nphil/kibble-scrypted` clips-list endpoint for one time window, in ms."""
    base = _strip_trailing_slash(base_url)
    return f"{base}/endpoint/@nphil/kibble-scrypted/public/clips?start={start_ms}&end={end_ms}"


def videoclip_url(base_url: str, video_id: str) -> str:
    """The recorder's own `videoclip` webhook for one already-validated `video_id`. Never call
    this with a `video_id` that has not passed `is_valid_video_id` -- the params blob is the
    only thing standing between this and an arbitrary filename read on the recorder's host."""
    base = _strip_trailing_slash(base_url)
    params = json.dumps({"deviceId": SCRYPTED_DEVICE_ID, "filename": video_id})
    return f"{base}/endpoint/@apocaliss92/scrypted-events-recorder/public/videoclip?params={quote(params)}"


CLIP_LOOKUP_TIMEOUT = ClientTimeout(total=8)
# `docs/39-eating-clips.md`'s own window: wide enough to catch a clip whose pre-roll starts
# before the session's own `start` (the recorder's prebuffer) or whose post-roll ends after
# the session's own `end` (`postEventSeconds`), without being so wide it routinely pulls in an
# unrelated neighbouring meal's clip too (`best_overlap_clip` disambiguates if it does).
QUERY_PAD_BEFORE_MS = 30_000
QUERY_PAD_AFTER_MS = 60_000

# Offsets from the moment an eat event closes (NOT from the previous attempt) -- the first
# attempt runs immediately, at close. Chosen from the recorder's own source (same file as
# `VIDEO_ID_RE`'s comment, its ffmpeg-process `onClose` handler): once the recording process
# itself exits, the recorder polls up to 10 times, 5s apart, for its own output file to even
# become accessible on disk before it renames it into place -- up to 50s of pure flush lag on
# top of `postEventSeconds` (>=15s) after the feeder's own eat-end. So the immediate attempt
# routinely finds nothing yet; +30s catches the common case once flush lag is done; +2m/+10m
# are backstops for a slow disk, a busy Scrypted, or a recording the recorder itself kept
# extending well past our own event's close (re-triggered within `minDelayBetweenClips`). No
# separate "reindex" wait is needed on the read side: the recorder lists clips with a live
# directory scan on every call, not a cached index, so a file that exists is already visible.
RETRY_DELAYS_S: tuple[float, ...] = (30.0, 120.0, 600.0)

# How far back `async_relink_recent` looks at startup for a closed-but-unlinked eat session --
# generous enough to cover any realistic HA downtime without re-scanning the whole event
# journal on every restart.
RELINK_LOOKBACK_S = 24 * 60 * 60


class ClipLinker:
    """Links a closed eat session to its Scrypted clip in the background, surviving both "not
    indexed yet" (retries) and a full HA restart (`async_relink_recent`)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: KibbleStore,
        *,
        retry_delays_s: Sequence[float] = RETRY_DELAYS_S,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._store = store
        self._retry_delays_s = tuple(retry_delays_s)
        self._base_url = _strip_trailing_slash((entry.options.get(CONF_SCRYPTED_CLIPS_URL) or "").strip())
        self._pending: dict[str, asyncio.Task[None]] = {}

    @property
    def enabled(self) -> bool:
        """Off by default (`docs/39-eating-clips.md`): an empty option means no lookups, no
        retries and no startup sweep -- an installation with no Scrypted clips server
        configured sees zero behaviour change from this feature existing at all."""
        return bool(self._base_url)

    def schedule_link(self, uid: str, start_ts: int, end_ts: int) -> None:
        """Starts (or leaves running) the background retry arc for one top-level eat event's
        `uid`, given its own `start`/`end` (unix seconds). Safe to call repeatedly for the same
        still-unlinked, still-closed event: a call while an earlier one is still retrying is a
        no-op, so a device that keeps re-reporting the same closed event every poll never
        restarts its own retry schedule from the top."""
        if not self.enabled:
            return
        existing = self._pending.get(uid)
        if existing is not None and not existing.done():
            return
        task = self._entry.async_create_background_task(
            self._hass, self._link_with_retries(uid, start_ts, end_ts), name=f"kibble clip link {uid}"
        )
        self._pending[uid] = task

    async def _link_with_retries(self, uid: str, start_ts: int, end_ts: int) -> None:
        session_start_ms = start_ts * 1000
        session_end_ms = end_ts * 1000
        if await self._try_link_once(uid, session_start_ms, session_end_ms):
            return
        elapsed = 0.0
        for offset in self._retry_delays_s:
            await asyncio.sleep(max(0.0, offset - elapsed))
            elapsed = offset
            if await self._try_link_once(uid, session_start_ms, session_end_ms):
                return
        _LOGGER.debug("No Scrypted clip found for %s after %.0fs; giving up", uid, elapsed)

    async def _try_link_once(self, uid: str, session_start_ms: int, session_end_ms: int) -> bool:
        try:
            candidates = await self._fetch_candidates(session_start_ms, session_end_ms)
        except (ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Clip lookup failed for %s: %s", uid, err)
            return False
        best = best_overlap_clip(candidates, session_start_ms, session_end_ms)
        if best is None:
            return False
        await self._store.async_set_event_clip(
            uid, clip_id=best.video_id, clip_start_ms=best.start_ms, clip_end_ms=best.end_ms
        )
        return True

    async def _fetch_candidates(self, session_start_ms: int, session_end_ms: int) -> list[ClipCandidate]:
        url = clips_list_url(
            self._base_url, session_start_ms - QUERY_PAD_BEFORE_MS, session_end_ms + QUERY_PAD_AFTER_MS
        )
        session = async_get_clientsession(self._hass)
        async with session.get(url, timeout=CLIP_LOOKUP_TIMEOUT) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        return parse_clip_candidates(payload)

    async def async_relink_recent(self) -> None:
        """Restart-survival sweep (`docs/39-eating-clips.md`): re-schedules linking for every
        closed eat session from the last `RELINK_LOOKBACK_S` that still has none. Called once,
        at setup -- never periodic, since anything a later sweep would find is already covered
        by `schedule_link`'s own retries firing off the live ingest path."""
        if not self.enabled:
            return
        cutoff = int(time.time()) - RELINK_LOOKBACK_S
        for uid, start_ts, end_ts in await self._store.async_events_needing_clip_link(cutoff):
            self.schedule_link(uid, start_ts, end_ts)

    async def async_cancel(self) -> None:
        """Cancels every still-running retry arc -- `entry.async_on_unload`'s own cleanup, so
        unloading the entry never leaves a background task pointed at a closed store."""
        pending = [t for t in self._pending.values() if not t.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
