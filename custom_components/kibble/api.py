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


class KibbleCalibrationBusyError(KibbleError):
    """`POST /calibration`'s `point` action 409s when it cannot trust the live bowl score
    right now: an animal is over the bowl (the common case -- a reading taken through a cat
    would poison the curve) or, more rarely, vision has not produced any bowl reading at all
    yet. Raised distinctly from `KibbleSpeakerBusyError` (a completely unrelated 409, on
    `/speak`/`/clips/<name>/play`) so a caller can show "wait for a clear bowl reading"
    instead of a generic failure -- both cases are real, transient, retry-later conditions,
    never a malformed request."""


class KibbleCueCooldownError(KibbleError):
    """`POST /cue`'s per-call debounce (`speaker::SpeakerOwner::try_start_call`,
    `speaker::CALL_COOLDOWN`) 429s a call attempted before the previous one's cooldown has
    elapsed. Raised distinctly from `KibbleSpeakerBusyError`/`KibbleCalibrationBusyError` (both
    409s, an unrelated resource) so a caller can say "try again in a moment" instead of a
    generic failure."""


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
class KeyEvent:
    """The feeder's most recent physical-button event, as reported by `GET /state`'s
    `last_key` (LibreFeed-only -- the vendor stack never populates this key; see
    `FeederState.raw`). `node` identifies which of the three buttons: `3` pairing/reset,
    `2` button "1" (hopper 1), `1` button "2" (hopper 2). `event` is the MCU's own code:
    `4` press, `1` short release, `3` long-press threshold reached (~2s held), `5` release
    after a long press. `at_ms` is milliseconds since the agent's own process start
    (monotonic, not wall-clock) -- only useful to tell two reports apart, never to compute
    an absolute time."""

    node: int
    event: int
    at_ms: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> KeyEvent:
        return cls(
            node=int(data.get("node") or 0),
            event=int(data.get("event") or 0),
            at_ms=int(data.get("at_ms") or 0),
        )


@dataclass(frozen=True, slots=True)
class FeederState:
    """One snapshot of the feeder, as reported by `GET /state`."""

    serial: str
    firmware: str
    ble_firmware: int
    volume: int
    desiccant_days: int
    feeding: bool
    #: `GET /state`'s `eating`: `media`'s own eat-in-progress flag (kibble docs/34 Part 9) --
    #: set the moment its detector decides a pet is eating, cleared when the meal ends.
    eating: bool
    #: `GET /state`'s `bowl_fill`: the feeder's own vision estimate of how full the bowl is
    #: (0-100), None while it has none. One reading for the whole bowl (kibble docs/34 Part 10).
    bowl_fill: int | None
    #: `GET /state`'s `bowl_empty`: LibreFeed's hysteretic verdict on whether the bowl is
    #: actually empty, as opposed to `bowl_fill` being a small number. `None` means the feeder
    #: has not yet taken an unobstructed reading (or predates the field) -- which an automation
    #: whose action is to dispense food must treat as "do not know", never as "empty".
    bowl_empty: bool | None
    #: True while an animal is over the bowl, so the two readings above are the last
    #: unobstructed ones rather than live.
    bowl_occluded: bool
    #: Per-hopper "is the food level at or below the vendor's own low-food threshold" flag --
    #: `GET /state`'s `hopper_empty`, `[hopper_1, hopper_2]`. `None` while the feeder has never
    #: reported a level for that hopper since its last boot (kibble docs/07-config.md).
    hopper_empty: tuple[bool | None, bool | None]
    #: `GET /state`'s `hopper_level`: the MCU's own three-way reading per hopper, 0 empty /
    #: 1 low / 2 ok, `None` until the MCU has reported one since boot.
    hopper_level: tuple[int | None, int | None]
    #: Per-hopper "mark as full" bookkeeping (`GET /state`'s `hopper_full_at`/
    #: `hopper_portions_since_full`/`hopper_full_to_low`, LibreFeed-only): the unix time it was
    #: last marked full, how many portions have dispensed from it since, and the portions the
    #: daemon has learned it takes to run from full down to the low-food sensor tripping. All
    #: `None` per hopper until it has been marked full at least once; `hopper_full_to_low`
    #: specifically stays `None` until the daemon has actually seen that hopper run all the way
    #: down to low after being marked -- it is a learned number, not a configured one.
    hopper_full_at: tuple[int | None, int | None]
    hopper_portions_since_full: tuple[int | None, int | None]
    hopper_full_to_low: tuple[int | None, int | None]
    #: Kibble's own bowl-fullness estimate, computed on-device from the camera by the same
    #: vendor vision model the feeder itself uses -- the vendor only runs that model while its
    #: cloud session is up (kibble docs/34), so this is the only reading that exists with the
    #: cloud disabled. `(percent, frame_unix)`: the frame's own timestamp, i.e. when the bowl
    #: actually looked like that.
    bowl_fill_local: tuple[int | None, int | None]
    event_counter: int
    # Agent-process forensics, not device data: kibbled keeps these in tmpfs (`health.rs`), so
    # they reset to a single start on a feeder reboot. A count that climbs without a reboot is
    # the signal worth an alert -- it means the agent itself is dying and being restarted.
    agent_starts: int
    agent_last_start: int | None
    agent_last_exit_code: int | None
    #: The feeder's most recent physical-button event (`GET /state`'s `last_key`), `None` if
    #: the feeder has never reported one this boot -- LibreFeed-only, see `KeyEvent`.
    last_key: KeyEvent | None
    #: The feeder's key-event ring (`GET /state`'s `keys`, oldest first, LibreFeed-only) --
    #: `event.py`'s `KibbleButtonEvent` diffs this against what it last saw so a poll interval
    #: landing between two events never loses one, the way reading `last_key` alone could.
    #: Falls back to a one-element tuple built from `last_key` for older LibreFeed builds that
    #: don't yet report `keys`; empty when the feeder has reported neither.
    keys: tuple[KeyEvent, ...]
    raw: dict[str, Any]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FeederState:
        hopper_empty = data.get("hopper_empty") or [None, None]
        hopper_level = data.get("hopper_level") or [None, None]
        local = data.get("bowl_fill_local") or [None, None]
        full_at = data.get("hopper_full_at") or [None, None]
        portions_since_full = data.get("hopper_portions_since_full") or [None, None]
        full_to_low = data.get("hopper_full_to_low") or [None, None]
        local_frame = data.get("bowl_fill_local_frame_unix")
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
            eating=bool(data.get("eating")),
            bowl_fill=data.get("bowl_fill"),
            bowl_empty=data.get("bowl_empty"),
            bowl_occluded=bool(data.get("bowl_occluded")),
            hopper_empty=(
                hopper_empty[0],
                hopper_empty[1] if len(hopper_empty) > 1 else None,
            ),
            hopper_level=(
                hopper_level[0] if len(hopper_level) > 0 else None,
                hopper_level[1] if len(hopper_level) > 1 else None,
            ),
            bowl_fill_local=(
                local[0],
                int(local_frame) if isinstance(local_frame, (int, float)) else None,
            ),
            hopper_full_at=(
                full_at[0] if len(full_at) > 0 else None,
                full_at[1] if len(full_at) > 1 else None,
            ),
            hopper_portions_since_full=(
                portions_since_full[0] if len(portions_since_full) > 0 else None,
                portions_since_full[1] if len(portions_since_full) > 1 else None,
            ),
            hopper_full_to_low=(
                full_to_low[0] if len(full_to_low) > 0 else None,
                full_to_low[1] if len(full_to_low) > 1 else None,
            ),
            event_counter=int(data.get("event_counter") or 0),
            agent_starts=int(data.get("kibbled_start_count") or 0),
            agent_last_start=int(last_start) if isinstance(last_start, (int, float)) else None,
            agent_last_exit_code=int(exit_code) if isinstance(exit_code, (int, float)) else None,
            last_key=KeyEvent.from_json(last_key) if (last_key := data.get("last_key")) else None,
            keys=(
                tuple(KeyEvent.from_json(k) for k in raw_keys)
                if (raw_keys := data.get("keys")) is not None
                else (KeyEvent.from_json(last_key),) if last_key else ()
            ),
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
class StackState:
    """Which feeder userland is running, as reported by `GET /mode` (the agent's counterpart
    to `agent/src/mode.rs`'s boot-time stack selection): the vendor's own Petkit stack, or the
    open LibreFeed replacement -- plus which one will be running after the next boot, and
    whether LibreFeed is even installed to switch to."""

    running: str
    next: str
    librefeed_installed: bool

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> StackState:
        return cls(
            running=str(data.get("running", "")),
            next=str(data.get("next", "")),
            librefeed_installed=bool(data.get("librefeed_installed", False)),
        )


@dataclass(frozen=True, slots=True)
class LedState:
    """The feeder's status LED, as reported by `GET /led` (LibreFeed-only -- the vendor stack
    doesn't serve this route; see `_request`'s `not_found_is_missing`). `white` is either the
    device's own automatic policy (`"auto"`) or a forced override: `0` off, `1` on, `2` blink,
    `3` fast blink. `green` is the second LED element, plain on/off. `camera` is the front
    camera-indicator LED: `"auto"` (on while a stream is being watched) or forced `0`/`1`."""

    white: str | int
    green: int
    camera: str | int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> LedState:
        white = data.get("white", "auto")
        camera = data.get("camera", "auto")
        return cls(
            white=white if white == "auto" else int(white),
            green=int(data.get("green") or 0),
            camera=camera if camera == "auto" else int(camera),
        )


@dataclass(frozen=True, slots=True)
class DesiccantState:
    """The feeder's desiccant-pack countdown, as reported by `GET /desiccant` (LibreFeed-only
    -- the vendor stack's equivalent counter is set from Petkit's cloud config, not served by
    the agent; see `_request`'s `not_found_is_missing`). `days_left` counts down to `0`;
    `replaced_unix` is when the pack was last marked replaced (unix seconds), the value
    `POST /desiccant {"replaced": true}` bumps; `interval_days` is the rated interval a
    replacement resets `days_left` to."""

    days_left: int
    replaced_unix: int
    interval_days: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DesiccantState:
        return cls(
            days_left=int(data.get("days_left") or 0),
            replaced_unix=int(data.get("replaced_unix") or 0),
            interval_days=int(data.get("interval_days") or 0),
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
class Face:
    """A sample's face crop and embedding, when the detector found one in that frame."""

    jpeg: str | None
    emb: str | None
    score: float | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Face:
        score = data.get("score")
        return cls(
            jpeg=data.get("jpeg"),
            emb=data.get("emb"),
            score=float(score) if score is not None else None,
        )


@dataclass(frozen=True, slots=True)
class OtherSubjectSample:
    """Another confirmed animal recorded in the same sampled frame."""

    sid: int | None
    box: tuple[float, float, float, float] | None
    score: float | None
    bowl: bool | None
    body: str | None
    face: Face | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OtherSubjectSample:
        box = data.get("box")
        score = data.get("score")
        return cls(
            sid=int(data["sid"]) if data.get("sid") is not None else None,
            box=tuple(float(v) for v in box) if isinstance(box, list) and len(box) == 4 and any(box) else None,
            score=float(score) if score is not None else None,
            bowl=bool(data["bowl"]) if "bowl" in data else None,
            body=data.get("body"),
            face=Face.from_json(data["face"]) if isinstance(data.get("face"), dict) else None,
        )
@dataclass(frozen=True, slots=True)
class Sample:
    """One sampled device frame and the crops associated with its selected subject."""

    k: int
    t: int
    box: tuple[float, float, float, float] | None
    score: float | None
    body: str | None
    face: Face | None
    sid: int | None = None
    bowl: bool | None = None
    others: tuple[OtherSubjectSample, ...] = ()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Sample:
        box = data.get("box")
        score = data.get("score")
        face = data.get("face")
        others = data.get("others")
        return cls(
            k=int(data.get("k") or 0),
            t=int(data.get("t") or 0),
            # Legacy [0, 0, 0, 0] means that the detector did not report a box.
            box=tuple(float(v) for v in box) if isinstance(box, list) and len(box) == 4 and any(box) else None,
            score=float(score) if score is not None else None,
            body=data.get("body"),
            face=Face.from_json(face) if isinstance(face, dict) else None,
            sid=int(data["sid"]) if data.get("sid") is not None else None,
            bowl=bool(data["bowl"]) if "bowl" in data else None,
            others=(
                tuple(OtherSubjectSample.from_json(item) for item in others if isinstance(item, dict))
                if isinstance(others, list)
                else ()
            ),
        )

@dataclass(frozen=True, slots=True)
class SubjectSummary:
    """The feeder's per-session timing summary for one confirmed subject."""

    sid: int
    first: int
    last: int
    eat_start: int | None
    bowl_s: float

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SubjectSummary:
        eat_start = data.get("eat_start")
        return cls(
            sid=int(data["sid"]),
            first=int(data.get("first") or 0),
            last=int(data.get("last") or 0),
            eat_start=int(eat_start) if eat_start is not None else None,
            bowl_s=float(data.get("bowl_s") or 0.0),
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "sid": self.sid, "first": self.first, "last": self.last,
            "eat_start": self.eat_start, "bowl_s": self.bowl_s,
        }
@dataclass(frozen=True, slots=True)
class DetectionEvent:
    """One visit or eat track, as reported by `GET /events` (docs/36-ai-pipeline.md's device
    contract -- `librefeedd`'s own tracker). Newest first, at most 256 rows, open tracks
    included. A legacy journal row predating this shape is served in the same shape: `samples`
    holds one entry built from the legacy face crop/embedding when both exist, else is empty,
    and `scene` carries the legacy scene/image name -- its assets keep their legacy names.

    Identity (`cat`, review, evidence) is no longer carried on the wire at all: HA's own
    identity engine and event journal (`store.py`) are the system of record for that now."""

    event_id: int
    seq: int
    ts: int
    end: int | None
    open: bool
    kind: str  # "visit" | "eat"
    eat_start: int | None
    scene: str | None
    samples: tuple[Sample, ...]
    image: str | None
    image_before: str | None
    image_after: str | None
    scene_k: int | None = None
    subjects: tuple[SubjectSummary, ...] | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DetectionEvent:
        end = data.get("end")
        eat_start = data.get("eat_start")
        samples = data.get("samples")
        scene_k = data.get("scene_k")
        subjects = data.get("subjects")
        return cls(
            event_id=int(data.get("event_id") or 0),
            seq=int(data.get("seq") or 0),
            ts=int(data.get("ts") or 0),
            end=int(end) if end is not None else None,
            open=bool(data.get("open")),
            kind=str(data.get("class", "")),
            eat_start=int(eat_start) if eat_start is not None else None,
            scene=data.get("scene"),
            samples=(
                tuple(Sample.from_json(s) for s in samples)
                if isinstance(samples, list)
                else ()
            ),
            image=data.get("image"),
            image_before=data.get("image_before"),
            image_after=data.get("image_after"),
            scene_k=int(scene_k) if scene_k is not None else None,
            subjects=(
                tuple(SubjectSummary.from_json(item) for item in subjects if isinstance(item, dict))
                if isinstance(subjects, list)
                else None
            ),
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
    #: Whether the feeder's MCU confirmed this dispense with its own completed record.
    #: `False` means the dispense demonstrably ran but its completion frame never arrived, so
    #: the amounts are the ones commanded rather than the ones measured. Absent on records
    #: from a feeder that predates the field, and those are all confirmed.
    confirmed: bool = True

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
            confirmed=bool(data.get("confirmed", True)),
        )


@dataclass(frozen=True, slots=True)
class SpoolStats:
    """`GET /spool`: the feeder's bounded transient evidence store (docs/36-ai-pipeline.md) --
    `used_bytes` against `cap_bytes` (8 MiB), `files` currently held, `evicted_total` since
    boot, and `opt_free_bytes`, the free-space floor the daemon refuses to write below."""

    used_bytes: int
    cap_bytes: int
    files: int
    evicted_total: int
    opt_free_bytes: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SpoolStats:
        return cls(
            used_bytes=int(data.get("used_bytes") or 0),
            cap_bytes=int(data.get("cap_bytes") or 0),
            files=int(data.get("files") or 0),
            evicted_total=int(data.get("evicted_total") or 0),
            opt_free_bytes=int(data.get("opt_free_bytes") or 0),
        )


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


#: One daemon detection-area rectangle set: a list of `[x1, y1, x2, y2]` normalized boxes.
VisionAreaMatrix = list[list[float]]


@dataclass(frozen=True, slots=True)
class VisionAreas:
    """The daemon's normalized ignore-mask rectangles, as reported by `GET /vision/areas`
    (LibreFeed-only route -- the vendor stack has no such concept). There is no "include"
    counterpart: a fixed one-bowl camera has nothing useful to positively scope, and the
    daemon's old `body_include_rois` silently dropped any detection outside it with no
    surfaced feedback -- removed end to end (daemon, this client, the card) rather than kept
    unused. See `kibble-card`'s `kibble-detection-areas-dialog.ts` header for the full
    rationale."""

    exclude: VisionAreaMatrix

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> VisionAreas:
        return cls(exclude=data.get("exclude") or [])


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
        nullable: bool = False,
        busy_error: type[KibbleError] = KibbleSpeakerBusyError,
        busy_status: int = 409,
    ) -> Any:
        """`payload` is sent as a JSON body; `data`, if given instead, is sent raw -- `/speak`
        and `PUT /clips/<name>` both take raw signed-16-bit-LE/mono/16kHz PCM with no envelope
        (mutually exclusive with `payload`; nothing here needs both at once).

        `not_found_is_missing`: a 404 here names either a specific resource the caller asked
        for by id/name (an unknown cat, an unknown sample) or an optional route this agent
        version simply doesn't serve yet (LibreFeed today only implements `/state`, `/feeds`,
        `/wifi`, `/cloud`, `/mode`, `/schedule`) -- either way it raises `KibbleNotFoundError`
        (carrying the agent's own `{"error": ...}` message when there is one, e.g. "not
        found", falling back to `path` otherwise) instead of the default "not supported by
        this agent version" `KibbleError`, exactly like `_get_bytes` already does for crop
        fetches, so callers -- `coordinator.py`'s `_fetch_all` for the optional reads, and
        `websocket.py`'s WS error mapping for the by-id ones -- can tell "this route/
        cat/sample doesn't exist" from "the agent is genuinely unreachable" apart.

        `nullable`: `GET /vision/last` is the one route whose 200 body is legitimately bare
        JSON `null` (no frame analysed yet) rather than always an object -- this keeps that
        `None` instead of falling into the empty-object substitution below, which exists only
        to normalise the routes that are genuinely always an object.

        `busy_error`: which typed error a 409 raises. Defaults to `KibbleSpeakerBusyError` --
        the speaker's exclusive-owner arbitration, on exactly `/speak` and
        `/clips/<name>/play`. `calibration_action` passes `KibbleCalibrationBusyError`
        instead: `POST /calibration`'s `point` action 409s for a completely unrelated reason
        (an animal is over the bowl right now), and the two "busy, retry" conditions need to
        stay tellable apart so a caller can show the right message for each."""
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
                        # A 409 here is always some exclusive-resource arbitration rejecting a
                        # write outright: the speaker's owner lock (`audioout.rs`'s
                        # `SpeakerOwner`, on exactly `/speak` and `/clips/<name>/play`) by
                        # default, or -- via `busy_error` -- `/calibration`'s "an animal is
                        # over the bowl right now" refusal. Distinct from every other rejected
                        # write so a caller can tell "busy, retry" from "malformed request".
                        if resp.status == busy_status:
                            raise busy_error(str(detail))
                        raise KibbleError(str(detail))
                    # `GET /wifi/scan` returns a bare JSON array, every other endpoint an
                    # object -- only substitute the empty-object default for a truly absent
                    # body, never for a legitimately empty array (`body or {}` would silently
                    # turn `[]` into `{}`) or, with `nullable`, a legitimately bare `null`.
                    if body is None and not nullable:
                        return {}
                    return body
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
        body = await self._request("GET", "/config", not_found_is_missing=True)
        return {key: int(value) for key, value in body.items()}

    async def set_config(self, key: str, value: int) -> dict:
        """Write one writable setting. The agent 400s for any key that isn't writable."""
        return await self._request("POST", "/config", {"key": key, "value": value})

    async def feed(
        self, hopper: str, amount: int, feed_id: str | None = None, amount2: int | None = None
    ) -> dict:
        payload: dict[str, Any] = {"hopper": hopper, "amount": amount}
        if feed_id:
            payload["id"] = feed_id
        # Omitted (not sent as 0 or duplicated) when absent -- the agent/daemon already
        # defaults amount2 to amount itself (compat.rs::feed), so leaving the key out keeps
        # every pre-existing caller's behaviour identical to before this field existed.
        if amount2 is not None:
            payload["amount2"] = amount2
        return await self._request("POST", "/feed", payload)

    async def cancel_feed(self) -> dict:
        return await self._request("POST", "/feed/cancel")

    async def schedule(self) -> ScheduleState:
        return ScheduleState.from_json(await self._request("GET", "/schedule", not_found_is_missing=True))

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

    async def mode(self) -> StackState:
        return StackState.from_json(await self._request("GET", "/mode"))

    async def set_mode(self, mode: str) -> dict:
        """Switch the running feeder userland (`"vendor"` or `"librefeed"`). The agent reboots
        ~1s after acknowledging this (`agent/src/mode.rs`) -- unlike `set_cloud`/
        `wifi_connect`, there is no rolled-back state to read back immediately, so this just
        returns the agent's raw ack (`{"ok": true, "next": ..., "rebooting": true}`);
        `coordinator.py`'s `async_set_mode` does not refresh afterwards for the same reason.
        A 400 with `{"error": ...}` means LibreFeed isn't installed to switch to."""
        return await self._request("POST", "/mode", {"mode": mode})

    async def led(self) -> LedState:
        return LedState.from_json(await self._request("GET", "/led", not_found_is_missing=True))

    async def set_led(
        self,
        *,
        white: str | int | None = None,
        green: int | None = None,
        camera: str | int | None = None,
    ) -> LedState:
        """Write one or more of the status LED's fields. The agent 400s for a bad `white`/
        `green`/`camera` value; a 404 here means the vendor stack is running (LibreFeed-only
        route -- `not_found_is_missing` on `led()` above, not here: a write that 404s is a real
        failure, not an optional read to fall back on)."""
        payload: dict[str, Any] = {}
        if white is not None:
            payload["white"] = white
        if green is not None:
            payload["green"] = green
        if camera is not None:
            payload["camera"] = camera
        return LedState.from_json(await self._request("POST", "/led", payload))

    async def beep(self, *, count: int = 2, on_ms: int = 100, off_ms: int = 100) -> dict:
        """`POST /beep`: plays `count` MCU buzzer beeps of `on_ms` each with `off_ms` gaps
        (LibreFeed-only -- the agent 400s for a value outside its own writable range). A 404
        here means the vendor stack is running; like `set_led`, this write is not passed
        `not_found_is_missing` -- a 404 on a write is a real failure, not an optional read to
        fall back on. Returns the agent's own `{"ok", "count", "on_ms", "off_ms"}` ack."""
        return await self._request(
            "POST", "/beep", {"count": count, "on_ms": on_ms, "off_ms": off_ms}
        )


    async def call_cats(self) -> dict:
        """`POST /cue`: plays the fixed feed cue through the feeder's speaker on demand
        (LibreFeed-only -- absent from kibbled's own route table, same footing as `/beep`). Does
        not dispense food. 429s with `{"ok":false,"error":"cooldown","retry_after_ms":n}` while
        the previous call's `speaker::CALL_COOLDOWN` is still running -- raised as
        `KibbleCueCooldownError`, not the default `KibbleSpeakerBusyError`, since this has
        nothing to do with the speaker's own owner-lock arbitration. A 404 here means the vendor
        stack is running; like `beep`, this write is not passed `not_found_is_missing` -- a 404
        on a write is a real failure, not an optional read to fall back on."""
        return await self._request("POST", "/cue", {}, busy_error=KibbleCueCooldownError, busy_status=429)
    async def desiccant(self) -> DesiccantState:
        return DesiccantState.from_json(
            await self._request("GET", "/desiccant", not_found_is_missing=True)
        )

    async def set_desiccant(
        self,
        *,
        replaced: bool | None = None,
        days_left: int | None = None,
        interval_days: int | None = None,
    ) -> DesiccantState:
        """`POST /desiccant`: the agent accepts exactly one of `{"replaced": true}`,
        `{"days_left": N}`, `{"interval_days": N}` per call and 400s for anything else
        (LibreFeed-only, `agent/src/compat.rs`) -- `coordinator.py`'s `async_set_desiccant`
        never gives more than one of these at once, so this simply forwards whichever single
        field the caller passed, the same trust-the-caller shape as `set_led`. A 404 here
        means the vendor stack is running -- like `set_led`, this write is not passed
        `not_found_is_missing`, since a 404 on a write is a real failure, not an optional read
        to fall back on. Returns the new snapshot, the same shape as `desiccant()`."""
        payload: dict[str, Any] = {}
        if replaced is not None:
            payload["replaced"] = replaced
        if days_left is not None:
            payload["days_left"] = days_left
        if interval_days is not None:
            payload["interval_days"] = interval_days
        return DesiccantState.from_json(await self._request("POST", "/desiccant", payload))

    async def wifi(self) -> WifiState:
        return WifiState.from_json(await self._request("GET", "/wifi"))

    async def wifi_scan(self) -> list[WifiNetwork]:
        """Deduplicated (strongest per SSID), hidden SSIDs already omitted by the agent."""
        networks = await self._request("GET", "/wifi/scan", not_found_is_missing=True)
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

    async def mark_hopper_full(self, hopper: str) -> dict:
        """`POST /hopper/full`: tells the daemon this hopper was just physically refilled to
        capacity, resetting `hopper_full_at`/`hopper_portions_since_full` to start counting
        from now (LibreFeed-only, same footing as `/beep`/`/desiccant`: a 404 here is a real
        failure, not an optional read to fall back on). `hopper` is `"1"`/`"2"`/`"both"`."""
        return await self._request("POST", "/hopper/full", {"hopper": hopper})

    async def spool_stats(self) -> SpoolStats:
        """`GET /spool`: the feeder's bounded transient evidence store's current usage."""
        return SpoolStats.from_json(await self._request("GET", "/spool", not_found_is_missing=True))
    async def asset_bytes(self, name: str) -> bytes:
        """Fetch one evidence asset -- a body/face crop, `.emb` sidecar, scene frame, or
        before/after frame -- from the feeder's bounded spool (`GET /events/<name>`)."""
        return await self._get_bytes(f"/events/{quote(name, safe='')}")

    async def delete_asset(self, name: str) -> None:
        """Acknowledge that HA has durably archived this asset (`DELETE /events/<name>`); the
        agent removes it from the spool. A 404 here means it is already gone (already
        acknowledged, or evicted under spool pressure before HA got to it) -- not an error."""
        await self._request("DELETE", f"/events/{quote(name, safe='')}", not_found_is_missing=True)

    async def feed_bytes(self, name: str) -> bytes:
        """`GET /feeds/<name>`: one dish-snapshot's raw bytes. LibreFeed's own
        `daemon/src/feeds.rs::read_feed_file` already serves a real JPEG; a feeder still running
        the vendor kibbled stack instead serves a raw H.264 keyframe (`agent/src/
        feed_capture.rs`) that needs further decoding -- see `image.py`'s `_feed_snapshot_jpeg`
        (told apart by magic bytes, not by guessing which stack is running) for which. Routed
        through this client's own locked, timed-out `_get_bytes`, exactly like every other
        passthrough kind, rather than an independent fetch -- see that method's own doc on why
        that matters against this agent's single-client HTTP server."""
        return await self._get_bytes(f"/feeds/{quote(name, safe='')}")

    async def clips(self) -> list[ClipInfo]:
        return [ClipInfo.from_json(c) for c in await self._request("GET", "/clips", not_found_is_missing=True)]

    async def feeds(self) -> list[FeedRecord]:
        return [FeedRecord.from_json(f) for f in await self._request("GET", "/feeds", not_found_is_missing=True)]

    async def events(self) -> list[DetectionEvent]:
        """`GET /events`: newest first, at most 256 rows, open tracks included. Rehydrated
        from disk on agent startup, so this survives a `librefeedd` restart."""
        return [
            DetectionEvent.from_json(e)
            for e in await self._request("GET", "/events", not_found_is_missing=True)
        ]

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

    async def vision_last(self) -> dict[str, Any] | None:
        """`GET /vision/last`: the daemon's most recently analysed frame -- already-deduplicated
        frame-fraction detection boxes, the currently open track's cat identification, and the
        `detection_overlay` config echo -- or `None` while nothing has been analysed yet (a
        genuine bare JSON `null` body, not an absent one; `nullable` keeps `_request` from
        coercing that into `{}` the way every other, always-an-object endpoint wants). The
        body is returned unchanged -- this is straight passthrough for the card to render,
        not a shape this integration otherwise understands.

        `not_found_is_missing`: this route is brand new, so an agent old enough to predate it
        404s outright, exactly like every other optional route -- `websocket.py`'s
        `ws_vision_last`, which calls this directly on every card poll instead of going
        through the coordinator (see that module's docstring), folds the resulting
        `KibbleNotFoundError` into the same `{"frame": None}` reply as a genuine empty frame."""
        return await self._request("GET", "/vision/last", not_found_is_missing=True, nullable=True)

    async def vision_areas(self) -> VisionAreas:
        """GET /vision/areas: the daemon's ignore-mask detection rectangles."""
        return VisionAreas.from_json(await self._request("GET", "/vision/areas"))

    async def set_vision_areas(self, exclude: VisionAreaMatrix) -> VisionAreas:
        """Replace the ignore-mask set through POST /vision/areas."""
        return VisionAreas.from_json(await self._request("POST", "/vision/areas", {"exclude": exclude}))

    async def vision_bowl_roi(self) -> list[float]:
        """The daemon's `bowl_roi` -- a single `[x1,y1,x2,y2]` zone (separate from
        `VisionAreas`) that gates eat detection and food-occlusion, read from the full
        `GET /vision` config (`bowl_roi` has no dedicated route of its own; unlike
        `body_include_rois`/`body_exclusion_rois` it was never routed through
        `/vision/areas`). Falls back to `[]` for an agent old enough to predate the field --
        the card treats an empty/malformed value as "use the daemon's own default"."""
        body = await self._request("GET", "/vision")
        roi = body.get("bowl_roi")
        return [float(v) for v in roi] if isinstance(roi, list) else []

    async def set_vision_bowl_roi(self, bowl_roi: list[float]) -> list[float]:
        """Sets just `bowl_roi` through `POST /vision`'s merge-not-replace contract (every
        other vision.json field -- `pet_detection`, `eat_hold_s`, and so on -- is left as-is)."""
        body = await self._request("POST", "/vision", {"bowl_roi": bowl_roi})
        roi = body.get("bowl_roi")
        return [float(v) for v in roi] if isinstance(roi, list) else bowl_roi

    async def calibration(self) -> dict:
        """`GET /calibration`: both hoppers' bowl-fill calibration curves (LibreFeed-only --
        `not_found_is_missing`, since this route is even newer than `/vision/last` and an
        agent old enough to predate it 404s exactly the same way). Returned as the agent's raw
        JSON (`{"hoppers": [hopper_or_null, hopper_or_null]}`) rather than a parsed dataclass:
        `coordinator.py` caches it verbatim on `KibbleData.calibration`, and both the card
        (`websocket.py`'s `kibble/calibration`) and the per-hopper sensors (`sensor.py`) read
        it as plain JSON, so there is no intermediate Python shape anything here benefits
        from -- unlike, say, `FeederState`, nothing needs to combine this with other fields or
        recompute a derived value more than once per poll."""
        return await self._request("GET", "/calibration", not_found_is_missing=True)

    async def calibration_action(self, action: str, hopper: int, **fields: Any) -> dict:
        """`POST /calibration`: one step of the bowl-fill calibration wizard --
        `action="begin"` starts a fresh curve for `hopper` (discarding any previous one,
        optional `note`), `action="point"` records the bowl's *current* vision score at
        `portions` dispensed so far, `action="full"` marks an already-recorded `portions` as
        the full point, `action="inherit"` copies the other hopper's finished curve
        (`fields["from"]`), and `action="clear"` forgets this hopper's calibration outright.
        `fields` passes straight through into the JSON body alongside `action`/`hopper` --
        deliberately generic rather than one keyword per action, since the five actions above
        share almost no fields and the agent itself is the single source of truth for which
        combination a given `action` needs (400s for a wrong one, same as every other write
        here).

        This method -- and everything upstream of it, `coordinator.py`'s
        `async_calibration_action` and `websocket.py`'s `kibble/calibration/action` -- never
        dispenses food. Every `point` reading is of whatever the operator already put in the
        bowl with their own, separate, deliberate `kibble.feed`/feed-control action; a
        calibration step only ever reads the vision score or writes bookkeeping about it.

        Raises `KibbleCalibrationBusyError` (409) if an animal is over the bowl right now --
        `point`'s own refusal to record a reading taken through a cat, distinct from
        `KibbleSpeakerBusyError`'s completely unrelated 409 (see `_request`'s `busy_error`). A
        404 here is a real failure (an agent old enough to predate this route), not passed
        `not_found_is_missing`: unlike the `GET` above, a deliberate wizard action that finds
        no route to act on is not an optional read to quietly fall back on -- same reasoning
        as `set_led`/`set_desiccant`."""
        payload: dict[str, Any] = {"action": action, "hopper": hopper, **fields}
        return await self._request(
            "POST", "/calibration", payload, busy_error=KibbleCalibrationBusyError
        )
