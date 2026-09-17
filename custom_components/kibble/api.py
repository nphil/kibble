"""HTTP client for the on-device `kibbled` agent.

The agent speaks a small JSON API over plain HTTP on the feeder's own LAN address. It is a
single-client server (one accept loop, one request at a time) so this client serialises its
requests with a lock rather than pipelining them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp import ClientError, ClientTimeout

_LOGGER = logging.getLogger(__name__)

TIMEOUT = ClientTimeout(total=8)
# `coordinator.py`'s `_async_update_data` makes up to a dozen of these calls back-to-back
# under one aggregate `POLL_TIMEOUT` (see its module docstring) and the feeder's HTTP server
# is single-client -- a stuck call must fail fast enough that the *whole* poll cycle still
# finishes well inside `DEFAULT_SCAN_INTERVAL`, not just this one request inside its own
# window.
#
# 8s, not 4s: measured on the real device, a healthy `GET /state` takes 0.6-1.5s (serial HTTP
# server, sharing an ARM core with the vendor encoder at load ~8), and a request that is merely
# queued behind another consumer can exceed 4s while the feeder is perfectly fine. 4s therefore
# produced spurious failures; 8s distinguishes "busy" from "down" without letting one call eat
# the whole cycle.
#
# The fail-safe connect sequence (agent/src/wifi.rs) budgets up to ~30s for association plus a
# DHCP lease before rolling back; this request has to outlive that, not the short window every
# other (near-instant) call uses.
WIFI_CONNECT_TIMEOUT = ClientTimeout(total=35)


class KibbleError(Exception):
    """Any failure talking to the agent."""


class KibbleConnectionError(KibbleError):
    """The agent could not be reached."""


class KibbleSpeakerBusyError(KibbleError):
    """The speaker already has a writer: another kibbled-internal playback session, or (per
    `audioout.rs`'s `SpeakerOwner`) the vendor app's own call. `POST /speak` and
    `POST /clips/<name>/play` both 409 for exactly this reason -- raised distinctly so a
    caller can give a clear, specific message instead of a generic `KibbleError`."""


class KibbleMediaError(KibbleError):
    """A local failure resolving or converting HA media *before* ever reaching the agent --
    ffmpeg couldn't be started, timed out, or produced nothing; HA's own media-source
    resolution failing raises its own `HomeAssistantError` directly and never reaches this.
    Still just a `KibbleError` to every existing catch site, so a caller doesn't need a second
    `except` clause to tell "HA couldn't prepare this audio" from "the agent rejected it"."""


class KibbleNotFoundError(KibbleError):
    """A byte-fetch (`event_bytes`/`pending_bytes`/`sample_bytes`) named a crop the agent no
    longer has -- distinct from a generic `KibbleError` so `views.py` can 404 instead of 502
    (the crop is legitimately gone, e.g. relabelled or evicted under us; the agent itself is
    fine)."""


@dataclass(frozen=True, slots=True)
class FeederState:
    """One snapshot of the feeder, as reported by `GET /state`."""

    serial: str
    firmware: str
    ble_firmware: int
    volume: int
    desiccant_days: int
    feeding: bool
    bowl_fill: tuple[int | None, int | None]
    event_counter: int
    # Agent-process forensics, not device data: kibbled keeps these in tmpfs (`health.rs`), so
    # they reset to a single start on a feeder reboot. A count that climbs without a reboot is
    # the signal worth an alert -- it means the agent itself is dying and being restarted.
    agent_starts: int
    agent_last_start: int | None
    agent_last_exit_code: int | None
    raw: dict[str, Any]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FeederState:
        fill = data.get("bowl_fill") or [None, None]
        # `kibbled_last_exit_code` is null on a first, clean start -- a real 0 means "the
        # previous run exited successfully", which is a different fact, so neither collapses
        # into the other via `or`.
        last_start = data.get("kibbled_last_start_unix")
        exit_code = data.get("kibbled_last_exit_code")
        return cls(
            serial=str(data.get("serial", "")),
            firmware=str(data.get("firmware", "")),
            ble_firmware=int(data.get("ble_firmware") or 0),
            volume=int(data.get("volume") or 0),
            desiccant_days=int(data.get("desiccant_days") or 0),
            feeding=bool(data.get("feeding")),
            bowl_fill=(fill[0], fill[1] if len(fill) > 1 else None),
            event_counter=int(data.get("event_counter") or 0),
            agent_starts=int(data.get("kibbled_start_count") or 0),
            agent_last_start=int(last_start) if isinstance(last_start, (int, float)) else None,
            agent_last_exit_code=int(exit_code) if isinstance(exit_code, (int, float)) else None,
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """One feed-schedule entry, as kibbled caches it (not a device read -- see kibbled's
    `schedule.rs`; the MCU has no schedule read-back)."""

    id: str
    time: str  # "HH:MM", 24-hour
    amount_l: int
    amount_r: int
    enabled: bool
    #: When kibbled's scheduler will next fire this entry (unix seconds), `None` when disabled.
    next_fire_utc: int | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ScheduleEntry:
        next_fire = data.get("next_fire_utc")
        return cls(
            id=str(data.get("id", "")),
            time=str(data.get("time", "")),
            amount_l=int(data.get("amount_l") or 0),
            amount_r=int(data.get("amount_r") or 0),
            enabled=bool(data.get("enabled", True)),
            next_fire_utc=int(next_fire) if next_fire is not None else None,
        )


@dataclass(frozen=True, slots=True)
class ScheduleState:
    """One snapshot of the feed schedule, as reported by `GET /schedule`."""

    entries: tuple[ScheduleEntry, ...]
    last_modified: int
    raw: dict[str, Any]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ScheduleState:
        return cls(
            entries=tuple(ScheduleEntry.from_json(e) for e in data.get("entries", [])),
            last_modified=int(data.get("last_modified") or 0),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class CloudConnection:
    """One non-LAN TCP socket with a real remote peer, as reported by `GET /cloud`."""

    remote: str
    state: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CloudConnection:
        return cls(remote=str(data.get("remote", "")), state=str(data.get("state", "")))


@dataclass(frozen=True, slots=True)
class CloudState:
    """One snapshot of the Petkit-cloud kill switch, as reported by `GET /cloud`
    (`agent/src/cloud.rs`)."""

    enabled: bool
    last_error: str | None
    routes: tuple[str, ...]
    connections: tuple[CloudConnection, ...]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CloudState:
        return cls(
            enabled=bool(data.get("enabled", True)),
            last_error=data.get("last_error"),
            routes=tuple(str(r) for r in data.get("routes", [])),
            connections=tuple(CloudConnection.from_json(c) for c in data.get("connections", [])),
        )


@dataclass(frozen=True, slots=True)
class WifiNetwork:
    """One scanned Wi-Fi network, as reported by `GET /wifi/scan` -- already deduplicated by
    SSID (strongest signal kept) and with hidden SSIDs omitted (`agent/src/wifi.rs`)."""

    ssid: str
    bssid: str
    freq_mhz: int
    band: str
    signal_dbm: int
    security: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> WifiNetwork:
        return cls(
            ssid=str(data.get("ssid", "")),
            bssid=str(data.get("bssid", "")),
            freq_mhz=int(data.get("freq_mhz") or 0),
            band=str(data.get("band", "")),
            signal_dbm=int(data.get("signal_dbm") or 0),
            security=str(data.get("security", "")),
        )


@dataclass(frozen=True, slots=True)
class WifiState:
    """One snapshot of the feeder's Wi-Fi association, as reported by `GET /wifi`
    (`agent/src/wifi.rs`). `ssid`/`bssid`/`freq_mhz`/`band`/`signal_dbm`/`ip` are `None` while
    disconnected; `desired_ssid` is the network `wifi.json` wants (boot re-apply/reconcile keep
    pursuing it); `last_error` explains the most recent failed connect attempt, if any."""

    ssid: str | None
    bssid: str | None
    freq_mhz: int | None
    band: str | None
    signal_dbm: int | None
    ip: str | None
    state: str
    desired_ssid: str | None
    last_error: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> WifiState:
        return cls(
            ssid=data.get("ssid"),
            bssid=data.get("bssid"),
            freq_mhz=data.get("freq_mhz"),
            band=data.get("band"),
            signal_dbm=data.get("signal_dbm"),
            ip=data.get("ip"),
            state=str(data.get("state", "")),
            desired_ssid=data.get("desired_ssid"),
            last_error=data.get("last_error"),
        )


@dataclass(frozen=True, slots=True)
class CatInfo:
    """One enrolled cat, as reported by `GET /cats` (`agent/src/faces.rs`'s `Gallery`).

    `avatar` is the sample filename nearest that cat's running centroid -- the crop the cats
    card shows as the cat's round avatar (`GET /faces/samples/<cat>/<avatar>`) -- `None` for a
    pre-registered cat (`kibble.add_cat`) with zero samples yet."""

    name: str
    samples: int
    last_seen: int | None
    avatar: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CatInfo:
        return cls(
            name=str(data.get("name", "")),
            samples=int(data.get("samples") or 0),
            last_seen=data.get("last_seen"),
            avatar=data.get("avatar"),
        )


@dataclass(frozen=True, slots=True)
class IdentifyScore:
    """A cat/score pair -- `IdentifyResult.second_best`."""

    cat: str
    score: float

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> IdentifyScore:
        return cls(cat=str(data.get("cat", "")), score=float(data.get("score") or 0.0))


@dataclass(frozen=True, slots=True)
class IdentifyResult:
    """`GET /identify`: Kibble's own frozen-embedding classifier's opinion of the newest
    pending crop, or ground truth from the most recently labelled one once the review queue is
    empty -- `source` distinguishes the two ("classifier" vs "labelled"). `cat` is `None` only
    when nothing has ever been captured; once a crop exists it is a real name or the literal
    string `"unknown"` (the classifier ran but wasn't confident)."""

    cat: str | None
    score: float | None
    second_best: IdentifyScore | None
    crop: str | None
    source: str | None
    ts: int | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> IdentifyResult:
        second = data.get("second_best")
        return cls(
            cat=data.get("cat"),
            score=data.get("score"),
            second_best=IdentifyScore.from_json(second) if second else None,
            crop=data.get("crop"),
            source=data.get("source"),
            ts=data.get("ts"),
        )


@dataclass(frozen=True, slots=True)
class ReviewFace:
    """`GET /faces/current/info`: metadata for whichever crop `image.*_pending_face` is
    currently showing -- the oldest pending crop, or the most recently labelled one once the
    queue is empty (`agent/src/faces.rs`'s `review_target`)."""

    status: str  # "pending" | "labelled" | "none"
    name: str | None
    cat: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ReviewFace:
        return cls(
            status=str(data.get("status", "none")), name=data.get("name"), cat=data.get("cat")
        )


@dataclass(frozen=True, slots=True)
class PendingFace:
    """One crop still awaiting a human label, as reported by `GET /faces/pending` (newest
    last). `name` already encodes `ts` and `vendor_pet_id` (`{ts}-{petid|unknown}.jpg`) --
    they are pulled out as their own fields here so callers never have to re-parse the
    filename. A crop written before its `track` event lands starts as `vendor_pet_id: None`
    (filename suffix `-unknown`) and is renamed by the agent once a matching track arrives.
    `guess` is Kibble's own classifier's verdict for this exact crop, stored beside it at
    capture time -- `None` if the classifier had nothing to say (e.g. no cats enrolled yet)."""

    name: str
    ts: int
    vendor_pet_id: str | None
    guess: IdentifyScore | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PendingFace:
        guess = data.get("guess")
        vendor_pet_id = data.get("vendor_pet_id")
        return cls(
            name=str(data.get("name", "")),
            ts=int(data.get("ts") or 0),
            vendor_pet_id=str(vendor_pet_id) if vendor_pet_id is not None else None,
            guess=IdentifyScore.from_json(guess) if guess else None,
        )


@dataclass(frozen=True, slots=True)
class FaceSample:
    """One permanently-labelled sample in a cat's gallery, as reported by `GET
    /faces/samples/<cat>`."""

    name: str
    ts: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FaceSample:
        return cls(name=str(data.get("name", "")), ts=int(data.get("ts") or 0))


@dataclass(frozen=True, slots=True)
class ClipInfo:
    """One stored audio clip, as reported by `GET /clips` (`agent/src/clips.rs`'s own
    `ClipInfo`) -- already normalized and AAC-encoded on the agent side; `bytes` is the
    encoded size, not the original PCM's."""

    name: str
    bytes: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ClipInfo:
        return cls(name=str(data.get("name", "")), bytes=int(data.get("bytes") or 0))


@dataclass(frozen=True, slots=True)
class DetectionEvent:
    """One onboard-AI detection, as reported by `GET /events` (`agent/src/ai.rs`).

    `cls` is `visit` (a pet in frame), `eat` (feeding), `face` (a usable face crop) -- each
    with an `image` filename for `GET /events/<name>` -- or `track`: the vendor's own on-device
    identification, read from the feeder's shared config block. A `track` carries `pet_id`
    (the vendor's cloud pet id, as a string), `ts` = the vendor's own visit start time, and
    `total_score` (the vendor's own per-visit number: the sum of per-frame identification
    confidence over the tracked visit -- bigger means longer/steadier, not more probable).

    `score` is honestly `None` on every class: the vendor never computes a similarity this
    pipeline can observe, and no bounding box exists anywhere in its chain."""

    seq: int
    ts: int
    cls: str
    image: str | None
    cat: str | None
    score: float | None
    pet_id: str | None
    total_score: float | None

    @classmethod
    def from_json(cls_, data: dict[str, Any]) -> DetectionEvent:
        score = data.get("score")
        total_score = data.get("total_score")
        return cls_(
            seq=int(data.get("seq") or 0),
            ts=int(data.get("ts") or 0),
            cls=str(data.get("class", "")),
            image=data.get("image") or None,
            cat=data.get("cat") or None,
            score=float(score) if score is not None else None,
            pet_id=str(data["pet_id"]) if data.get("pet_id") is not None else None,
            total_score=float(total_score) if total_score is not None else None,
        )


@dataclass(frozen=True, slots=True)
class FeedRecord:
    """One feed cycle's before/after dish-snapshot pair, as reported by `GET /feeds`
    (`agent/src/feed_capture.rs`'s own `FeedRecord`). `before`/`after` are filenames for
    `GET /feeds/<name>`'s raw H.264 keyframe bytes -- `None` if that half of the pair was
    never captured (no cached keyframe available at that exact instant)."""

    ts: int
    id: str
    amount1: int | None
    amount2: int | None
    manual: bool
    before: str | None
    after: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FeedRecord:
        amount1 = data.get("amount1")
        amount2 = data.get("amount2")
        return cls(
            ts=int(data.get("ts") or 0),
            id=str(data.get("id", "")),
            amount1=None if amount1 is None else int(amount1),
            amount2=None if amount2 is None else int(amount2),
            manual=bool(data.get("manual")),
            before=data.get("before"),
            after=data.get("after"),
        )


class KibbleClient:
    """Talks to one feeder."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._lock = asyncio.Lock()

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        data: bytes | None = None,
        timeout: ClientTimeout | None = None,
        not_found_is_missing: bool = False,
    ) -> Any:
        """`payload` is sent as a JSON body; `data`, if given instead, is sent raw -- `/speak`
        and `PUT /clips/<name>` both take raw signed-16-bit-LE/mono/16kHz PCM with no envelope
        (mutually exclusive with `payload`; nothing here needs both at once).

        `not_found_is_missing`: a 404 here names a specific resource the caller asked for by
        id/name (an unknown cat, an unknown sample) rather than a route this agent version
        simply doesn't have -- raises `KibbleNotFoundError` (carrying the agent's own
        `{"error": ...}` message, e.g. "not found") instead of the default "not supported by
        this agent version" `KibbleError`, exactly like `_get_bytes` already does for crop
        fetches, so callers (and `websocket.py`'s WS error mapping) can tell "this cat/sample
        is gone" from "this agent is too old" apart."""
        async with self._lock:
            try:
                async with self._session.request(
                    method,
                    f"{self._base}{path}",
                    json=payload if data is None else None,
                    data=data,
                    timeout=timeout or TIMEOUT,
                ) as resp:
                    if resp.status == 404:
                        if not_found_is_missing:
                            # Confirmed shape (agent contract): `{"error": "not found"}` --
                            # read it so the WS error the card sees says something better than
                            # a bare path. Falls back to `path` if a future not-found route
                            # ever 404s with a non-JSON or unlabelled body.
                            try:
                                body = await resp.json(content_type=None)
                            except ValueError:
                                body = None
                            detail = body.get("error", body) if isinstance(body, dict) else body
                            raise KibbleNotFoundError(str(detail) if detail is not None else path)
                        raise KibbleError(f"{path} not supported by this agent version")
                    body = await resp.json(content_type=None)
                    if resp.status >= 400:
                        detail = body.get("error", body) if isinstance(body, dict) else body
                        # The speaker's exclusive-owner arbitration (`audioout.rs`'s
                        # `SpeakerOwner`) surfaces as 409 on exactly `/speak` and
                        # `/clips/<name>/play` -- distinct from every other rejected write so a
                        # caller can tell "busy, retry" from "malformed request".
                        if resp.status == 409:
                            raise KibbleSpeakerBusyError(str(detail))
                        raise KibbleError(str(detail))
                    # `GET /wifi/scan` returns a bare JSON array, every other endpoint an
                    # object -- only substitute the empty-object default for a truly absent
                    # body, never for a legitimately empty array (`body or {}` would silently
                    # turn `[]` into `{}`).
                    return body if body is not None else {}
            except TimeoutError as err:
                raise KibbleConnectionError(f"{self._base} timed out") from err
            except ClientError as err:
                raise KibbleConnectionError(f"{self._base}: {err}") from err

    async def _get_bytes(self, path: str) -> bytes:
        """Raw `GET` for one JPEG crop -- shares `_request`'s connection-serialising lock (the
        agent's HTTP server is effectively serial; see `TIMEOUT`'s comment above) but skips its
        JSON decoding. Every `*_bytes` method below funnels through this. Raises
        `KibbleNotFoundError` for a 404 (the crop was relabelled/evicted out from under a still-
        open card) so callers can 404 instead of treating it as the agent being unreachable."""
        async with self._lock:
            try:
                async with self._session.request(
                    "GET", f"{self._base}{path}", timeout=TIMEOUT
                ) as resp:
                    if resp.status == 404:
                        raise KibbleNotFoundError(path)
                    if resp.status >= 400:
                        raise KibbleError(f"{path}: HTTP {resp.status}")
                    return await resp.read()
            except TimeoutError as err:
                raise KibbleConnectionError(f"{self._base} timed out") from err
            except ClientError as err:
                raise KibbleConnectionError(f"{self._base}: {err}") from err

    async def state(self) -> FeederState:
        return FeederState.from_json(await self._request("GET", "/state"))

    async def config(self) -> dict[str, int]:
        """Every device setting's current value, as reported by `GET /config` (flat
        `{"key": value, ...}` -- see `agent/src/settings.rs`'s `SETTINGS` table)."""
        body = await self._request("GET", "/config")
        return {key: int(value) for key, value in body.items()}

    async def set_config(self, key: str, value: int) -> dict:
        """Write one writable setting. The agent 400s for any key that isn't writable."""
        return await self._request("POST", "/config", {"key": key, "value": value})

    async def feed(self, hopper: str, amount: int, feed_id: str | None = None) -> dict:
        payload: dict[str, Any] = {"hopper": hopper, "amount": amount}
        if feed_id:
            payload["id"] = feed_id
        return await self._request("POST", "/feed", payload)

    async def cancel_feed(self) -> dict:
        return await self._request("POST", "/feed/cancel")

    async def schedule(self) -> ScheduleState:
        return ScheduleState.from_json(await self._request("GET", "/schedule"))

    async def set_schedule(self, entries: list[dict[str, Any]]) -> ScheduleState:
        """Replace the whole table. `entries` items: `time`/`amount_l`/`amount_r`, optional
        `id`/`enabled`."""
        return ScheduleState.from_json(
            await self._request("PUT", "/schedule", {"entries": entries})
        )

    async def add_schedule_entry(
        self,
        time: str,
        amount_l: int,
        amount_r: int,
        enabled: bool = True,
        entry_id: str | None = None,
    ) -> ScheduleState:
        payload: dict[str, Any] = {
            "time": time,
            "amount_l": amount_l,
            "amount_r": amount_r,
            "enabled": enabled,
        }
        if entry_id:
            payload["id"] = entry_id
        return ScheduleState.from_json(await self._request("POST", "/schedule/entry", payload))

    async def remove_schedule_entry(self, entry_id: str) -> ScheduleState:
        return ScheduleState.from_json(
            await self._request("DELETE", f"/schedule/entry?id={quote(entry_id, safe='')}")
        )

    async def set_schedule_entry_enabled(self, entry_id: str, enabled: bool) -> ScheduleState:
        return ScheduleState.from_json(
            await self._request(
                "POST", "/schedule/entry/enabled", {"id": entry_id, "enabled": enabled}
            )
        )

    async def cloud(self) -> CloudState:
        return CloudState.from_json(await self._request("GET", "/cloud"))

    async def set_cloud(self, enabled: bool) -> CloudState:
        """Flip the Petkit-cloud kill switch. The agent fails safe (`agent/src/cloud.rs`): a
        disable that can't prove LAN reachability rolls itself back to enabled *and* returns
        an error for this request -- `KibbleError` here, same as any other rejected write.
        `GET /cloud` (via the next poll) reflects the rollback either way: `enabled: true`
        with `last_error` set to why."""
        return CloudState.from_json(
            await self._request("POST", "/cloud", {"enabled": enabled})
        )

    async def wifi(self) -> WifiState:
        return WifiState.from_json(await self._request("GET", "/wifi"))

    async def wifi_scan(self) -> list[WifiNetwork]:
        """Deduplicated (strongest per SSID), hidden SSIDs already omitted by the agent."""
        networks = await self._request("GET", "/wifi/scan")
        return [WifiNetwork.from_json(n) for n in networks]

    async def wifi_connect(self, ssid: str, psk: str | None = None) -> WifiState:
        """Fail-safe add+select on the agent (`agent/src/wifi.rs`): up to ~30s for association
        plus a DHCP lease, hence `WIFI_CONNECT_TIMEOUT` rather than the default. A rejected
        write (wrong password, no reachable AP, ...) raises `KibbleError` after the agent has
        already rolled itself back to the previous network -- same fail-safe-then-error shape
        as `set_cloud`. `psk` is never logged or echoed back; omit it to reconnect to an SSID
        the agent already has a saved password for."""
        payload: dict[str, Any] = {"ssid": ssid}
        if psk:
            payload["psk"] = psk
        return WifiState.from_json(
            await self._request("POST", "/wifi/connect", payload, timeout=WIFI_CONNECT_TIMEOUT)
        )

    async def wifi_forget(self, ssid: str) -> WifiState:
        """Removes a Kibble-added network; the agent 400s for the vendor's own network or the
        one currently providing connectivity."""
        return WifiState.from_json(await self._request("POST", "/wifi/forget", {"ssid": ssid}))

    async def cats(self) -> list[CatInfo]:
        return [CatInfo.from_json(c) for c in await self._request("GET", "/cats")]

    async def pending_faces(self) -> list[PendingFace]:
        """Every crop still awaiting a human label (`GET /faces/pending`), newest last. Backs
        both the diagnostic pending-count sensor (via `len()`) and `kibble/faces/pending`."""
        return [PendingFace.from_json(p) for p in await self._request("GET", "/faces/pending")]

    async def faces_samples(self, cat: str) -> list[FaceSample]:
        """Every permanently-labelled sample in `cat`'s gallery (`GET /faces/samples/<cat>`),
        backing `kibble/faces/samples`."""
        body = await self._request("GET", f"/faces/samples/{quote(cat, safe='')}")
        return [FaceSample.from_json(s) for s in body]

    async def add_cat(self, name: str) -> None:
        """Pre-register a cat with zero samples, so it appears in the label select's options
        before its first crop is ever labelled. The agent 400s for a reserved bucket name."""
        await self._request("POST", "/cats", {"name": name})

    async def delete_cat(self, name: str) -> dict:
        """`DELETE /cats/<name>`: removes the cat, every one of its labelled samples (and
        their `.emb` sidecars), and its trained classifier model outright. An unknown cat
        404s -- see `_request`'s `not_found_is_missing`."""
        return await self._request(
            "DELETE", f"/cats/{quote(name, safe='')}", not_found_is_missing=True
        )

    async def identify(self) -> IdentifyResult:
        return IdentifyResult.from_json(await self._request("GET", "/identify"))

    async def review_face(self) -> ReviewFace:
        return ReviewFace.from_json(await self._request("GET", "/faces/current/info"))

    async def label_face(self, crop_id: str, cat: str) -> None:
        """Moves a pending crop into `cat`'s permanent storage and feeds its embedding into
        that cat's running centroid (`agent/src/main.rs`'s `faces_label_post`)."""
        await self._request("POST", "/faces/label", {"name": crop_id, "cat": cat})

    async def unlabel_face(self, crop_id: str, cat: str) -> None:
        """The exact inverse of `label_face` -- moves a labelled crop back to pending and
        corrects the centroid. A full re-label is this followed by another `label_face`."""
        await self._request("POST", "/faces/unlabel", {"name": crop_id, "cat": cat})

    async def upload_face_sample(self, cat: str, jpeg: bytes) -> dict:
        """`POST /faces/upload?cat=<cat>`: `jpeg` is raw bytes the browser has already
        cropped to exactly 224x224 (`kibble-card`'s crop dialog) -- forwarded as-is, the same
        raw-body shape `speak`/`save_clip` already use. `cat` must already exist; 404s like
        `delete_cat`. Returns the agent's `{"name", "samples", "low_quality"?}` unchanged."""
        return await self._request(
            "POST",
            f"/faces/upload?cat={quote(cat, safe='')}",
            data=jpeg,
            not_found_is_missing=True,
        )

    async def delete_face_sample(self, cat: str, name: str) -> dict:
        """`DELETE /faces/samples/<cat>/<name>`: removes one already-labelled sample outright
        -- the counterpart to `unlabel_face` for a sample with no pending-queue entry to move
        back to (an uploaded photo never went through the pending review queue)."""
        return await self._request(
            "DELETE",
            f"/faces/samples/{quote(cat, safe='')}/{quote(name, safe='')}",
            not_found_is_missing=True,
        )

    async def event_bytes(self, name: str) -> bytes:
        """`GET /events/<name>`: one detection crop's raw JPEG bytes -- the timeline's image
        for every class (`visit`/`eat`/`face`), via the HTTP view's `kind="event"`."""
        return await self._get_bytes(f"/events/{quote(name, safe='')}")

    async def pending_bytes(self, name: str) -> bytes:
        """`GET /faces/pending/<name>`: one pending crop's raw JPEG bytes, via the HTTP view's
        `kind="pending"`."""
        return await self._get_bytes(f"/faces/pending/{quote(name, safe='')}")

    async def sample_bytes(self, cat: str, name: str) -> bytes:
        """`GET /faces/samples/<cat>/<name>`: one permanently-labelled sample's raw JPEG
        bytes, via the HTTP view's `kind="sample/<cat>"`."""
        return await self._get_bytes(
            f"/faces/samples/{quote(cat, safe='')}/{quote(name, safe='')}"
        )

    async def track_image_bytes(self, ts: int) -> bytes:
        """`GET /events/track/<ts>/image`: the JPEG of whichever `eat` (preferred) or `visit`
        event the agent judges paired with the `track` at `ts` -- the live image of the
        identified cat at the bowl, as opposed to a stored/trained sample. Raises
        `KibbleNotFoundError` when nothing qualifies, via the HTTP view's `kind="track"`."""
        return await self._get_bytes(f"/events/track/{ts}/image")

    async def clips(self) -> list[ClipInfo]:
        return [ClipInfo.from_json(c) for c in await self._request("GET", "/clips")]

    async def feeds(self) -> list[FeedRecord]:
        return [FeedRecord.from_json(f) for f in await self._request("GET", "/feeds")]

    async def events(self) -> list[DetectionEvent]:
        """`GET /events`: the agent's last 50 detections, oldest first. Rehydrated from disk on
        agent startup, so this survives a `kibbled` restart."""
        return [DetectionEvent.from_json(e) for e in await self._request("GET", "/events")]

    async def speak(self, pcm: bytes) -> dict:
        """`POST /speak`: plays `pcm` (raw signed-16-bit-LE/mono/16kHz, no container -- exactly
        `agent/src/main.rs`'s `pcm_from_body`) once through the speaker. Returns immediately
        with `{"samples","estimated_ms"}`; the agent runs the actual playback on its own
        spawned thread. Raises `KibbleSpeakerBusyError` (409) if the speaker already has a
        writer."""
        return await self._request("POST", "/speak", data=pcm)

    async def save_clip(self, name: str, pcm: bytes) -> dict:
        """`PUT /clips/<name>`: same raw PCM format as `speak`; the agent normalizes,
        AAC-encodes and stores it under `name`. Never 409s -- storing doesn't touch the
        speaker."""
        return await self._request("PUT", f"/clips/{quote(name, safe='')}", data=pcm)

    async def play_clip(self, name: str) -> dict:
        """`POST /clips/<name>/play`: plays an already-stored, already-encoded clip. Raises
        `KibbleSpeakerBusyError` (409) if the speaker already has a writer."""
        return await self._request("POST", f"/clips/{quote(name, safe='')}/play")
