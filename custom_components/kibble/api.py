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


class KibbleClient:
    """Talks to one feeder."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._lock = asyncio.Lock()

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        async with self._lock:
            try:
                async with self._session.request(
                    method, f"{self._base}{path}", json=payload, timeout=TIMEOUT
                ) as resp:
                    if resp.status == 404:
                        raise KibbleError(f"{path} not supported by this agent version")
                    body = await resp.json(content_type=None)
                    if resp.status >= 400:
                        raise KibbleError(str(body.get("error", body)))
                    return body or {}
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
