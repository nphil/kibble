"""The agent's local-push channel: a WebSocket on port 8766 carrying the same JSON bodies the
HTTP `GET` endpoints serve, sent when they change (`agent/src/push.rs`,
`docs/33-local-push.md`).

Two layers, kept apart so the interesting one is testable without a socket:

- `merge_frame` / `snapshot_from_frame`: pure functions turning a decoded frame into a
  `KibbleData`, using exactly the `from_json` parsers `api.KibbleClient` uses for the
  corresponding `GET`. Whole fields only, never partial diffs -- the agent guarantees each
  field is the complete `GET` body, so "what does this field contain" has one answer.
- `KibblePush.listen`: the transport. Connects through HA's shared aiohttp session (rule
  `inject-websession`), yields decoded frames, and raises `KibblePushClosed` when the socket
  ends for any reason. It never reconnects itself -- the coordinator owns that policy
  (`coordinator.py`), mirroring how `wled`'s coordinator drives its library's `listen()`.

Protocol (proto 1):
    {"type":"hello","proto":1,"seq":N}
    {"type":"snapshot","seq":N,"fields":{...every field...}}
    {"type":"update","seq":N,"fields":{...changed fields...}}
Client -> server: `{"type":"resync"}` asks for a fresh snapshot. aiohttp answers the agent's
pings itself; `heartbeat` makes the client send its own and drop a socket that stops
answering, which is what turns a silently dead path into a reconnect.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
import logging
from typing import Any

import aiohttp

from .api import (
    CatInfo,
    ClipInfo,
    CloudState,
    DetectionEvent,
    FeedRecord,
    FeederState,
    IdentifyResult,
    ReviewFace,
    ScheduleState,
    WifiNetwork,
    WifiState,
)

_LOGGER = logging.getLogger(__name__)

PUSH_PORT = 8766
PROTO = 1
# The agent pings every 30 s and drops a client silent for 90 s; the client-side heartbeat
# is the mirror image so a half-open socket is noticed from this end too, well inside the
# agent's own timeout.
HEARTBEAT_SECONDS = 30.0
# Nothing (data, ping, pong) for this long means the path is dead even if the socket isn't.
RECEIVE_TIMEOUT_SECONDS = 90.0
CONNECT_TIMEOUT_SECONDS = 10.0
# Largest plausible frame: a full snapshot with 50 events and a wide Wi-Fi scan is ~20 KB.
MAX_FRAME_BYTES = 256 * 1024


class KibblePushClosed(Exception):
    """The push socket ended (cleanly or not). Carries the reason for the log line."""


class KibblePushUnsupported(KibblePushClosed):
    """The agent answered but does not speak this protocol (HTTP handshake rejected, or a
    `hello` with another `proto`). Permanent for that agent build: the coordinator stays in
    polling mode and stops trying. A refused/unreachable port is NOT this -- see `listen`."""


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded server frame. `proto` is only present on `hello`."""

    type: str
    seq: int
    fields: Mapping[str, Any]
    proto: int | None = None


# Field name (as the agent's `push::Field::name`, == `KibbleData` attribute) -> parser of the
# exact `GET` body for that field. Kept as a table so a missing entry is a test failure, not a
# silently ignored field. `pending_faces` is the raw `GET /faces/pending` list; `KibbleData`
# stores its length, as the poll path does.
_PARSERS: dict[str, Callable[[Any], Any]] = {
    "state": FeederState.from_json,
    "schedule": ScheduleState.from_json,
    "config": lambda body: {key: int(value) for key, value in body.items()},
    "cloud": CloudState.from_json,
    "wifi": WifiState.from_json,
    "wifi_scan": lambda body: tuple(WifiNetwork.from_json(n) for n in body),
    "cats": lambda body: tuple(CatInfo.from_json(c) for c in body),
    "identify": IdentifyResult.from_json,
    "review_face": ReviewFace.from_json,
    "pending_faces": lambda body: len(body),
    "clips": lambda body: tuple(ClipInfo.from_json(c) for c in body),
    "feeds": lambda body: tuple(FeedRecord.from_json(f) for f in body),
    "events": lambda body: tuple(DetectionEvent.from_json(e) for e in body),
}
# `KibbleData` attribute for each frame field where the two names differ.
_ATTR = {"pending_faces": "pending_face_count"}
SNAPSHOT_FIELDS = frozenset(_PARSERS)


def parse_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Frame `fields` -> `{KibbleData attribute: parsed value}`. Unknown field names are
    ignored (a newer agent may add some); a `null` body (the agent could not serialise that
    field just now) is skipped so the previous value stands -- the same "keep the last good
    value" rule the poll path applies to a failed cycle."""
    parsed: dict[str, Any] = {}
    for name, body in fields.items():
        parser = _PARSERS.get(name)
        if parser is None or body is None:
            continue
        parsed[_ATTR.get(name, name)] = parser(body)
    return parsed


def decode_frame(raw: str) -> Frame | None:
    """Text -> `Frame`, or `None` for anything that is not a well-formed server frame."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        return None
    fields = obj.get("fields")
    proto = obj.get("proto")
    return Frame(
        type=obj["type"],
        seq=int(obj.get("seq") or 0),
        fields=fields if isinstance(fields, dict) else {},
        proto=int(proto) if isinstance(proto, int) else None,
    )


class KibblePush:
    """One connection's worth of the push channel. Create per attempt; not reusable."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int = PUSH_PORT) -> None:
        self._session = session
        self._url = f"ws://{host}:{port}/"
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def listen(self) -> AsyncIterator[Frame]:
        """Connect and yield frames until the socket ends. The first frame is always `hello`;
        a `hello` with an unknown `proto` closes the socket and raises `KibblePushUnsupported`."""
        try:
            self._ws = await self._session.ws_connect(
                self._url,
                heartbeat=HEARTBEAT_SECONDS,
                receive_timeout=RECEIVE_TIMEOUT_SECONDS,
                timeout=aiohttp.ClientWSTimeout(ws_close=CONNECT_TIMEOUT_SECONDS),
                max_msg_size=MAX_FRAME_BYTES,
                autoping=True,
            )
        except aiohttp.WSServerHandshakeError as err:
            # The port answered HTTP but refused the upgrade: an agent that is not ours or
            # predates push. Permanent for this agent build -- the coordinator stops trying.
            raise KibblePushUnsupported(f"handshake rejected: {err.status}") from err
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            # Includes "connection refused": also what a *restarting* agent looks like for a
            # few seconds, so this is never treated as permanent -- the coordinator retries
            # with backoff, and the fallback poll (plain HTTP) decides availability meanwhile.
            raise KibblePushClosed(f"connect failed: {err}") from err

        ws = self._ws
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    frame = decode_frame(msg.data)
                    if frame is None:
                        _LOGGER.debug("Ignoring malformed push frame: %.80s", msg.data)
                        continue
                    if frame.type == "hello" and frame.proto != PROTO:
                        await ws.close(code=aiohttp.WSCloseCode.PROTOCOL_ERROR)
                        raise KibblePushUnsupported(f"agent push proto {frame.proto}, need {PROTO}")
                    yield frame
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise KibblePushClosed(f"socket error: {ws.exception()}")
            raise KibblePushClosed(f"closed by agent (code {ws.close_code})")
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise KibblePushClosed(str(err)) from err
        finally:
            await self.close()

    async def resync(self) -> None:
        """Ask the agent for a fresh full snapshot (one frame, one reply)."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_str('{"type":"resync"}')

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            await ws.close()


def merge_frame(current: Any, frame: Frame) -> Any:
    """Apply an `update`/`snapshot` frame to a `KibbleData`, returning the new one. `current`
    is typed loosely to keep this module free of a coordinator import; it is always
    `KibbleData`. `vendor_sightings` is re-derived by the caller (it depends on the config
    entry's options, which this module does not see)."""
    parsed = parse_fields(frame.fields)
    return replace(current, **parsed) if parsed else current
