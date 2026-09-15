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

TIMEOUT = ClientTimeout(total=10)
# The fail-safe connect sequence (agent/src/wifi.rs) budgets up to ~30s for association plus a
# DHCP lease before rolling back; this request has to outlive that, not the default 10s every
# other (near-instant) call uses.
WIFI_CONNECT_TIMEOUT = ClientTimeout(total=35)


class KibbleError(Exception):
    """Any failure talking to the agent."""


class KibbleConnectionError(KibbleError):
    """The agent could not be reached."""


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
    raw: dict[str, Any]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FeederState:
        fill = data.get("bowl_fill") or [None, None]
        return cls(
            serial=str(data.get("serial", "")),
            firmware=str(data.get("firmware", "")),
            ble_firmware=int(data.get("ble_firmware") or 0),
            volume=int(data.get("volume") or 0),
            desiccant_days=int(data.get("desiccant_days") or 0),
            feeding=bool(data.get("feeding")),
            bowl_fill=(fill[0], fill[1] if len(fill) > 1 else None),
            event_counter=int(data.get("event_counter") or 0),
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

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ScheduleEntry:
        return cls(
            id=str(data.get("id", "")),
            time=str(data.get("time", "")),
            amount_l=int(data.get("amount_l") or 0),
            amount_r=int(data.get("amount_r") or 0),
            enabled=bool(data.get("enabled", True)),
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
    """One enrolled cat, as reported by `GET /cats` (`agent/src/faces.rs`'s `Gallery`)."""

    name: str
    samples: int
    last_seen: int | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CatInfo:
        return cls(
            name=str(data.get("name", "")),
            samples=int(data.get("samples") or 0),
            last_seen=data.get("last_seen"),
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
        timeout: ClientTimeout | None = None,
    ) -> Any:
        async with self._lock:
            try:
                async with self._session.request(
                    method, f"{self._base}{path}", json=payload, timeout=timeout or TIMEOUT
                ) as resp:
                    if resp.status == 404:
                        raise KibbleError(f"{path} not supported by this agent version")
                    body = await resp.json(content_type=None)
                    if resp.status >= 400:
                        detail = body.get("error", body) if isinstance(body, dict) else body
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

    async def pending_faces(self) -> list[str]:
        """Filenames of every crop still awaiting a human label (`GET /faces/pending`) --
        backs the diagnostic pending-count sensor."""
        return list(await self._request("GET", "/faces/pending"))

    async def add_cat(self, name: str) -> None:
        """Pre-register a cat with zero samples, so it appears in the label select's options
        before its first crop is ever labelled. The agent 400s for a reserved bucket name."""
        await self._request("POST", "/cats", {"name": name})

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
