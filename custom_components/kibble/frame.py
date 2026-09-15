"""Kibble's BLE application-layer frame.

See `docs/25-ble-feed-frame.md` for the full derivation; summary here for maintainers:

The feeder's T31 dispenser MCU is a byte pipe (`docs/17-ble-fallback.md`) -- whatever the
phone/host writes to GATT characteristic 0xAAA2 is relayed verbatim over UART CMD 0x11 to the
SoC's `ble` process, which forwards it unmodified onto the internal dispatch bus as
`msg_id 0x100a` (`RECV_BLE_DATA`, `agent/src/bus.rs`) to `ctrl`. Stock `ctrl`'s handler for that
message (`dispatch_handler_recv_ble_data`, disassembled in `docs/09-ble.md` SS4.2) JSON-parses
the bytes and switches on an integer `type` field, recognising exactly five values
(0x6e/0x6f/0x70/0x72/0x97) and treating everything else as a no-op -- confirmed by disassembly,
not inferred (see `docs/25-ble-feed-frame.md`).

Kibble reuses that same envelope -- a JSON object with an integer `type` key -- with a new
`type` value (`FEED_TYPE`, chosen well outside the vendor's single-byte 0x6e-0x97 cluster) that
a future Kibble `ctrl` replacement will map straight onto the already-proven 67-byte feed
struct (`docs/14-feed-test.md`, `agent/src/bus.rs::FeedCtrl`). `len`/`crc16` are Kibble's own
addition on top of that envelope -- the vendor's JSON layer has no checksum of its own (the
CRC16 documented for the UART link protects a lower, unrelated layer this client never
touches) -- so a corrupted or truncated write is rejected before anything reaches the bus.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
import time
from dataclasses import dataclass

# GATT UUIDs, 0000xxxx-0000-1000-8000-00805f9b34fb base (docs/09-ble.md SS1.2). RX (phone/host
# writes here) / TX (device notifies here) follow the community Petkit-fountain assignment;
# not yet independently confirmed for this device by a live characteristic-properties read --
# see docs/25-ble-feed-frame.md "Open items".
SERVICE_UUID = "0000aaa0-0000-1000-8000-00805f9b34fb"
CHAR_WRITE_UUID = "0000aaa2-0000-1000-8000-00805f9b34fb"
CHAR_NOTIFY_UUID = "0000aaa1-0000-1000-8000-00805f9b34fb"

# Vendor `type` values `dispatch_handler_recv_ble_data` recognises (docs/09-ble.md SS4.2):
# WiFi-credential change, schedule read, version check, OTA end, and one more WiFi-adjacent
# op. Kept here so FEED_TYPE's non-collision is asserted by code, not only claimed in prose.
VENDOR_TYPES = frozenset({0x6E, 0x6F, 0x70, 0x72, 0x97})

# Kibble's own type: ASCII "KB" ("Kibble"), decimal 19266. Deliberately > 0xFF -- the vendor's
# entire known range is single-byte -- so it cannot collide even if Petkit allocates more
# small types in a future firmware revision.
FEED_TYPE = 0x4B42
assert FEED_TYPE not in VENDOR_TYPES and FEED_TYPE > 0xFF  # noqa: S101 -- invariant, not a check

# `struct feed_ctrl` (agent/src/bus.rs::FeedCtrl, proven by dispensing -- docs/14-feed-test.md):
# u8 cancel, char id[64], u8 amount1, u8 amount2.
_FEED_STRUCT = struct.Struct("<B64sBB")
FEED_STRUCT_LEN = _FEED_STRUCT.size
assert FEED_STRUCT_LEN == 67  # noqa: S101 -- invariant, not a check

# Leaves room for the struct's own trailing NUL, matching `FeedCtrl::encode`'s `min(63)`.
MAX_FEED_ID_LEN = 63


class FrameError(ValueError):
    """A frame could not be built, or did not survive decoding intact."""


@dataclass(frozen=True, slots=True)
class FeedFrame:
    """One decoded Kibble feed frame -- the fields `agent/src/bus.rs::FeedCtrl` needs."""

    cancel: bool
    feed_id: str
    amount1: int
    amount2: int


def hopper_amounts(hopper: str, amount: int) -> tuple[int, int]:
    """`hopper`/`amount` (the HA service's vocabulary) -> `(amount1, amount2)`.

    Mirrors `agent/src/main.rs::feed()`'s match arms exactly. The HTTP path's translation
    happens on-device, in the agent; the BLE path bypasses the agent entirely, so this is not
    reusing that logic, it is a second copy of it -- kept honest by `test_ble_frame.py`.
    """
    if hopper == "1":
        return amount, 0
    if hopper == "2":
        return 0, amount
    if hopper == "both":
        return amount, amount
    raise FrameError(f'hopper must be "1", "2" or "both", got {hopper!r}')


def default_feed_id() -> str:
    """Same shape as `agent/src/main.rs`'s fallback id, so a feed logged without an explicit
    id looks the same regardless of which transport actually carried it."""
    return f"kibble-{int(time.time())}"


def _pack_feed_struct(*, cancel: bool, feed_id: str, amount1: int, amount2: int) -> bytes:
    id_bytes = feed_id.encode("ascii", errors="replace")
    if len(id_bytes) > MAX_FEED_ID_LEN:
        raise FrameError(f"feed id {feed_id!r} is {len(id_bytes)} bytes, max {MAX_FEED_ID_LEN}")
    for name, value in (("amount1", amount1), ("amount2", amount2)):
        if not 0 <= value <= 255:
            raise FrameError(f"{name}={value} does not fit in one byte")
    return _FEED_STRUCT.pack(1 if cancel else 0, id_bytes, amount1, amount2)


def _crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no xorout): a standard,
    precisely-specified variant with well-known test vectors (CRC of ASCII "123456789" is
    0x29B1), used here as Kibble's own integrity check on its own new envelope fields.

    Not claimed to match the MCU's internal UART CRC16 (docs/08-mcu.md SS3.2 pins the
    polynomial family and init value from disassembly but not the reflection convention,
    which needs a live capture to settle) -- that CRC protects a different, lower layer this
    client never touches directly, and this checksum does not need to match it to be useful.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_feed_frame(
    *, cancel: bool = False, feed_id: str | None = None, amount1: int, amount2: int
) -> bytes:
    """Build the bytes Kibble writes to 0xAAA2 for one feed (or cancel) command."""
    payload = _pack_feed_struct(
        cancel=cancel, feed_id=feed_id or default_feed_id(), amount1=amount1, amount2=amount2
    )
    frame = {
        "type": FEED_TYPE,
        "len": len(payload),
        "crc16": _crc16_ccitt_false(payload),
        "payload": base64.b64encode(payload).decode("ascii"),
    }
    return json.dumps(frame, separators=(",", ":")).encode("utf-8")


def decode_feed_frame(data: bytes) -> FeedFrame:
    """Parse and validate a Kibble feed frame -- the inverse of `encode_feed_frame`.

    Not needed by the HA client at runtime (it only ever encodes), but a wire format nobody
    has written a decoder for is untested by construction; this is that decoder, exercised by
    the round-trip test, and doubles as a reference for Kibble's future `ctrl` replacement.
    """
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise FrameError(f"not valid UTF-8 JSON: {err}") from err
    if not isinstance(obj, dict):
        raise FrameError(f"frame is a JSON {type(obj).__name__}, not an object")
    if obj.get("type") != FEED_TYPE:
        raise FrameError(f"type {obj.get('type')!r} is not a Kibble feed frame ({FEED_TYPE:#x})")
    try:
        payload = base64.b64decode(obj["payload"], validate=True)
    except KeyError as err:
        raise FrameError("frame has no 'payload' field") from err
    except binascii.Error as err:
        raise FrameError(f"'payload' is not valid base64: {err}") from err
    declared_len = obj.get("len")
    if declared_len != len(payload):
        raise FrameError(f"'len' says {declared_len}, decoded payload is {len(payload)} bytes")
    if len(payload) != FEED_STRUCT_LEN:
        raise FrameError(f"payload is {len(payload)} bytes, feed_ctrl is {FEED_STRUCT_LEN}")
    computed = _crc16_ccitt_false(payload)
    if obj.get("crc16") != computed:
        raise FrameError(f"checksum mismatch: frame says {obj.get('crc16')}, computed {computed}")
    cancel, id_field, amount1, amount2 = _FEED_STRUCT.unpack(payload)
    feed_id = id_field.split(b"\0", 1)[0].decode("ascii", errors="replace")
    return FeedFrame(cancel=bool(cancel), feed_id=feed_id, amount1=amount1, amount2=amount2)
