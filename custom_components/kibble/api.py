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

    async def feed(self, hopper: str, amount: int, feed_id: str | None = None) -> dict:
        payload: dict[str, Any] = {"hopper": hopper, "amount": amount}
        if feed_id:
            payload["id"] = feed_id
        return await self._request("POST", "/feed", payload)

    async def cancel_feed(self) -> dict:
        return await self._request("POST", "/feed/cancel")
