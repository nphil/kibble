# Kibble's BLE feed frame, and the HA-side client that sends it

Design + implementation notes for the ESPHome-proxy side of BLE fallback
([docs/17-ble-fallback.md](17-ble-fallback.md) deferred this until three things existed; this
document is step 3, "an ESPHome-proxy-side client that speaks the Petkit frame format", plus
the frame itself and the proof that shipping it today is safe). Source binaries and method are
[docs/09-ble.md](09-ble.md); the proven LAN feed path this reuses is
[docs/14-feed-test.md](14-feed-test.md).

## TL;DR

| Question | Answer |
|---|---|
| What does the phone/host write to 0xAAA2? | A JSON object, `{"type": <int>, ...}` — confirmed by disassembly of `ctrl`'s `dispatch_handler_recv_ble_data` (§4.2 of `09-ble.md`), not a fixed binary header/length/checksum frame |
| Does stock `ctrl` do anything with a BLE feed request? | No — five `type` values are recognised (WiFi credential change/recovery, schedule read, version check, OTA end); nothing else, feed included, reaches an actuator |
| Is an unrecognised `type` safe to send? | Yes — disassembly-proven no-op: `free`/cleanup, return 0. No crash, no partial state change (§ "The safety proof" below) |
| Kibble's new `type` | `0x4B42` (19266 decimal, ASCII "KB") — outside the vendor's entire single-byte `0x6e`–`0x97` range on purpose |
| What does Kibble's frame carry? | The exact, already-proven 67-byte `feed_ctrl` struct from `14-feed-test.md`, base64-encoded, plus a length and a CRC16 that are Kibble's own addition |
| Can this be end-to-end tested today? | No — stock `ctrl` ignores the new type by design, and a Kibble `ctrl` replacement that acts on it doesn't exist yet. What's tested here is the transport: a real GATT connection through an ESPHome proxy (see "Live transport proof") |

## 1. What actually travels over the air

`docs/17-ble-fallback.md` §2 and `09-ble.md` §2.2–2.3 already establish the byte-pipe chain;
restated precisely because the exact hand-off point is where "the frame" lives:

```
Phone/HA  --GATT write, char 0xAAA2-->  T31 MCU
                                          │ T31's own BLE/GATT stack wraps the write's bytes,
                                          │ verbatim, as the payload of a UART frame:
                                          │   5A A5 | LEN | CMD=0x11 | SEQ | FLAGS(subaddr=0) |
                                          │   <the exact bytes written to 0xAAA2> | CRC16
                                          v
                                         `ble` (Linux, SoC)
                                          │ unwraps the UART frame (08-mcu.md §3.2), recovers
                                          │ payload = frame[7 .. len-2] -- byte-identical to
                                          │ what was written to 0xAAA2 -- and forwards it
                                          │ unmodified onto the internal bus:
                                          │   dispatch_send_msg(msg_id=0x100a, dst=1, payload)
                                          v
                                         `ctrl` (Linux, SoC)
                                          │ dispatch_handler_recv_ble_data (msg_id 0x100a):
                                          │   1. memcpy payload into a local buffer (max 552B)
                                          │   2. parse(payload)            -- ctrl vaddr 0x8bdb0
                                          │   3. type = get_object_item(parsed, "type")  -- 0x8bde0
                                          │   4. switch on type            -- see §2 below
```

The UART frame's `5A A5 | LEN | CMD | SEQ | FLAGS | ... | CRC16` structure (`08-mcu.md` §3.2,
`09-ble.md` §2.1) is real, but it is **internal to the MCU↔SoC UART link** — it is applied by
the T31's firmware when relaying a GATT write up to Linux, and stripped by `ble` before the
payload ever reaches `ctrl`. It is not something a BLE central writes or sees; a client on the
phone/HA side never touches it. **The thing actually written to 0xAAA2 is whatever `ctrl` ends
up parsing** — and per §4.2 of `09-ble.md`, that parse step's call signature
(`get_object_item(parsed, key)`, immediately following a generic `parse()` call) is a JSON
object, not a fixed binary header/length/type/payload/checksum record. This matters for anyone
extending this later: there is no wire-level length prefix to get right, and no vendor checksum
to reproduce — JSON's own `{...}` nesting is self-delimiting, and §5 below explains why Kibble's
frame still adds a length + checksum anyway (it's a new, Kibble-only layer, not a reproduction
of an existing one).

## 2. The vendor's five recognised `type` values

Disassembled in full in `09-ble.md` §4.2 (`ctrl` vaddr ≈`0x473d8`–`0x4760c`). The dispatch is a
plain `cmp`/`beq` chain on the parsed `type` field, exhaustively enumerable by reading the
branches directly:

```
type == 0x70 -> bl 0x46950
type == 0x6e -> bl 0x46520
type == 0x6f -> bl 0x46e98
type == 0x72 -> bl 0x45188   (then, if that call's result == 0: bl 0x461a8)
type == 0x97 -> memset(local, 0, 0x88); bl 0x453f8(payload, local)
                (then, if that call's result == 0: bl 0x461a8; bl 0x90c20(0xe); bl 0x4c8c4)
anything else -> free/cleanup, return 0 (no-op)
```

Five values: `0x6e`, `0x6f`, `0x70`, `0x72`, `0x97` (decimal 110, 111, 112, 114, 151) — WiFi
credential change/recovery, schedule read, version check, and OTA end, per the five BLE-sourced
`dispatch_handler_ble_*` names `ctrl` registers (`09-ble.md` §4.1). **None of the five branches
leads toward `dispatch_handler_feed`/`pk_ctrl_send_feed_event_msg`** — this is the disassembly-
level confirmation behind "no feed-over-BLE in stock firmware."

## 3. The safety proof: an unrecognised `type` is a no-op, not a crash

This is the load-bearing line, quoted directly from the branch listing above:

> `anything else -> free/cleanup, return 0 (no-op)`

Every `type` value that isn't one of the five listed falls through to this branch — there is no
sixth `cmp`, no default-case jump into unrelated code, and no branch that reaches a pointer
dereference on data specific to one of the five recognised types. `dispatch_handler_recv_ble_data`
still runs its shared prologue for any type (bounds-check and `memcpy` the payload, call the
generic `parse()`), so an oversized (>552 bytes) or malformed-JSON payload is already handled by
that existing code path regardless of `type` — Kibble's new value doesn't need its own bounds
checking to stay safe, it only needs to *not be one of the five `cmp` immediates*, which is true
by construction (§4 below).

Consequence: sending a Kibble-typed frame to a feeder still running **stock** `ctrl` is
indistinguishable, from `ctrl`'s point of view, from sending it a `type` that doesn't exist yet —
harmless today, and exactly the hook a future Kibble `ctrl` replacement needs to add one more
`cmp` for (§6).

## 4. Kibble's new `type`: `0x4B42`

```python
FEED_TYPE = 0x4B42   # ASCII "KB" ("Kibble"), decimal 19266
```

Chosen deliberately outside the vendor's known range, and in a way that survives more than
just "not currently used":

- All five vendor values are single-byte (`0x6e`–`0x97`, i.e. `≤ 0xFF`). `0x4B42 > 0xFF`, so it
  cannot collide with *any* single-byte value — including ones Petkit might allocate in a future
  firmware revision that this study has no visibility into.
- The parsed `type` is a JSON integer (a `cJSON_GetObjectItem`-shaped call, `09-ble.md` §4.2),
  not a wire-format byte with a fixed width — there is no format-level reason it has to be
  small, so nothing is exploited or unusual about using a larger value.
- `custom_components/kibble/frame.py` asserts `FEED_TYPE not in VENDOR_TYPES and FEED_TYPE >
  0xFF` at import time, and `tests/test_ble_frame.py::test_decoder_rejects_vendor_frames` checks
  the converse (all five vendor values are rejected by Kibble's own decoder) — the non-collision
  claim is enforced by code, not only asserted in this document.

## 5. Kibble's frame format

Reuses the vendor's envelope shape (a JSON object with an integer `type` key — §1) with a new
`type`, and adds a `len` + `crc16` that the vendor's own JSON layer does not have:

```json
{"type":19266,"len":67,"crc16":<u16>,"payload":"<base64 of the 67-byte feed_ctrl struct>"}
```

| Field | Meaning |
|---|---|
| `type` | Always `19266` (`0x4B42`) for a Kibble feed/cancel command |
| `len` | Byte length of the *decoded* payload (always 67 today — one struct shape) |
| `crc16` | CRC-16/CCITT-FALSE (poly `0x1021`, init `0xFFFF`, no reflection, no xorout) over the decoded payload bytes |
| `payload` | Base64 of the exact `feed_ctrl` struct `agent/src/bus.rs::FeedCtrl` already sends over the LAN path, proven by dispensing (`14-feed-test.md`) |

Why add a length and checksum when the vendor's own JSON layer has neither: the vendor's five
types are read-mostly or provisioning-adjacent operations; this one triggers a physical motor.
`len`/`crc16` let a future Kibble `ctrl` replacement reject a truncated or corrupted write
*before* it ever reaches `dispatch_send_msg(0x6004, ...)`, at the cost of two integers and one
`base64` call. This checksum is **not** claimed to match the MCU's own internal UART CRC16
(`08-mcu.md` §3.2 pins the polynomial family and init value from disassembly but not the
reflection convention, which needs a live capture to settle) — it doesn't need to, because it
protects a different, higher layer this client never shares with that one. CRC-16/CCITT-FALSE
was picked instead purely because it is a standard, precisely-specified variant with a public
test vector (CRC of ASCII `"123456789"` is `0x29B1`), which is what `frame.py`'s own docstring
and tests check against — not an attempt to match undocumented vendor behaviour.

### The embedded struct

Unchanged from the proven LAN path (`14-feed-test.md`, `agent/src/bus.rs::FeedCtrl`):

```c
struct feed_ctrl {          /* 67 bytes */
    uint8_t cancel;         /* +0   0 = dispense, 1 = cancel */
    char    id[64];         /* +1   feed-record id, NUL-padded */
    uint8_t amount1;        /* +65  hopper 1 */
    uint8_t amount2;        /* +66  hopper 2 */
};
```

Reusing this exact struct, byte for byte, means a future Kibble `ctrl` replacement's handling of
Kibble's new `type` is: base64-decode `payload`, check `len == 67` and the checksum, then call
the *same* already-working `dispatch_send_msg(0x6004, 8, payload, 67)` the LAN path calls today
(`main.rs::send_feed`) — no new struct-packing code on-device, and zero new risk to the proven
feed path, since it isn't touched.

`hopper`/`amount` (the HA service's vocabulary) → `(amount1, amount2)` uses the identical match
arms as `agent/src/main.rs::feed()`, reimplemented in `frame.py::hopper_amounts` since the BLE
path bypasses the agent entirely (there is no Rust code in the loop to do this translation for
a BLE-delivered command):

| `hopper` | `(amount1, amount2)` |
|---|---|
| `"1"` | `(amount, 0)` |
| `"2"` | `(0, amount)` |
| `"both"` | `(amount, amount)` |

### Worked example

`hopper="1", amount=1, id="kibbletest1"` (the exact scenario `14-feed-test.md` proved dispenses
food) encodes to:

```
payload (67 bytes, hex):
  00 6b 69 62 62 6c 65 74 65 73 74 31 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
  00 00 00 01 00
  ^cancel=0  ^-------- "kibbletest1" -------^ ^----------------- 53 zero bytes -----------------^ ^a1=1 ^a2=0

frame written to 0xAAA2 (computed by `frame.encode_feed_frame`, reproduced verbatim by
`tests/test_ble_frame.py::test_wire_bytes_match_the_proven_struct_layout`):
  {"type":19266,"len":67,"crc16":51595,"payload":"AGtpYmJsZXRlc3QxAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAA=="}
```

The 67-byte struct is byte-identical to the one `tools/kibble-msg.c` sent for
`/tmp/km 8 6004 1 feed:1:0:kibbletest1` in the live dispensing test — verified directly in
`tests/test_ble_frame.py::test_wire_bytes_match_the_proven_struct_layout`.

## 6. The plan for what the device must do with it (not yet built)

Out of scope for this change (no on-device `ctrl` replacement exists yet — see
`docs/17-ble-fallback.md` §4 item 1), recorded here so the next step has a concrete target:

1. In the new dispatcher's equivalent of `dispatch_handler_recv_ble_data` (msg_id `0x100a`),
   add one more branch: `type == 0x4B42`.
2. Base64-decode `payload`; reject (log + drop, do not `dispatch_send_msg`) if `len` or `crc16`
   don't match, or decode fails.
3. Call `dispatch_send_msg(0x6004, 8, payload, 67)` — the exact, proven call
   `agent/src/main.rs::send_feed` already makes for the LAN path. No new struct assembly.
4. Optionally notify the result back on 0xAAA1, e.g. `{"type":19266,"ok":true}` /
   `{"type":19266,"ok":false,"error":"..."}`. Nothing in this study found an existing ack
   convention for the vendor's own five types to match (`09-ble.md` §8 lists a passive-notify
   listen as still-open future work), so this would be a new, Kibble-defined micro-protocol, not
   a reproduction of one — until it exists, `ble.py` correctly treats "wrote the frame, no
   notify" as inconclusive rather than a failure (§8).
5. All five stock types keep working unmodified — this is one additional `cmp`, not a rewrite of
   the function.

## 7. The Home Assistant side

New files: `custom_components/kibble/frame.py` (the codec above — zero Home Assistant/bleak
dependency, `tests/test_ble_frame.py`), `custom_components/kibble/ble_fallback.py` (the Wi-Fi
first/BLE-fallback decision, also dependency-free, `tests/test_ble_fallback.py`), and
`custom_components/kibble/ble.py` (the real GATT transport, `tests/test_ble_transport.py` with
`bleak`/`homeassistant.components.bluetooth` mocked).

### Config

A new options-flow field, `ble_address` (`custom_components/kibble/config_flow.py`), validated
as a MAC address, alongside the existing `stream_url` field. Empty (the default) means no
fallback is attempted. **The feeder's BLE MAC is not yet known** — `BleAdvertise` (parallel
work, same session) is instrumenting `pktool bleadv` and the MCU's advertising lever; this field
gets filled in once a live capture confirms the address (see "Live transport proof" below).

### The fallback

`custom_components/kibble/coordinator.py::KibbleCoordinator.async_feed` now:

1. Tries the existing HTTP path (`KibbleClient.feed`, unchanged).
2. On `KibbleConnectionError` specifically (not any `KibbleError` — a 400/500 means Wi-Fi
   *worked* as a transport, so it is not a fallback scenario), and only if `ble_address` is
   configured, tries `ble.async_feed` through whichever Bluetooth proxy currently sees the
   feeder (`homeassistant.components.bluetooth.async_ble_device_from_address` +
   `bleak_retry_connector.establish_connection`).
3. Records which path was used/attempted on `self.control_path` (`"wifi"` / `"bluetooth"` /
   `"unreachable"`) regardless of the outcome, and always calls `async_update_listeners()` +
   `async_request_refresh()` so the sensor and the rest of the entity state update immediately.

The branching itself lives in `ble_fallback.async_feed_with_fallback`, independent of Home
Assistant or bleak, so it's tested directly with both transports mocked
(`tests/test_ble_fallback.py`):

| Wi-Fi | `ble_address` | Result |
|---|---|---|
| succeeds | — | `control_path="wifi"`, no error |
| fails (non-connection `KibbleError`, e.g. bad input) | — | `control_path="wifi"`, original error re-raised, **BLE not attempted** |
| `KibbleConnectionError` | not set | `control_path="unreachable"`, original error re-raised |
| `KibbleConnectionError` | set, BLE succeeds | `control_path="bluetooth"`, no error |
| `KibbleConnectionError` | set, BLE also fails | `control_path="bluetooth"` (Bluetooth was the attempted path), the BLE error re-raised |

`ble.py`'s own bleak-facing import (`from .ble import async_feed`) happens lazily, inside
`KibbleCoordinator._ble_feed`, specifically so a feeder with no `ble_address` configured never
pulls bleak/`homeassistant.components.bluetooth` into the running event loop.

### The "Control path" sensor

`custom_components/kibble/sensor.py::KibbleControlPathSensor` — a DIAGNOSTIC, `ENUM`-class
sensor (matching the existing `cloud_connection` sensor's shape) with options `wifi` / `bluetooth`
/ `unreachable`, reading `coordinator.control_path` directly. Unknown until the first feed call
after Home Assistant starts — there's nothing to report before then.

## 8. Live transport proof

Acceptance for this change is a real GATT connection through an ESPHome proxy — not a full feed
(stock `ctrl` ignores Kibble's `type` by design, §3, and dispensing over BLE is out of scope
regardless: no on-device handler exists yet, §6). `ble.py::async_probe(hass, address)` connects
via `bleak-retry-connector`/HA's `bluetooth` integration exactly like `async_feed` does, but only
reads back each characteristic's UUID and property bitmap — no write.

**Status: pending, and not for lack of trying.** `BleAdvertise` (parallel work, same session)
found and sent the bus message + UART frame that should switch advertising on, and confirmed
by three independent senders converging on identical bytes that the frame itself is correctly
formed — but a ~3-minute on-window, observed against a live, confirmed-forwarding ESPHome
proxy (same room as the feeder), saw **zero** `Petkit`/`D4SH` names and zero 0xAAA0–2 UUIDs.
That's inconclusive rather than a clean negative: the feeder may need another prerequisite
besides the message that was sent, or it may be advertising unnamed/bare in a way that isn't
distinguishable from ambient BLE noise without a proper before/after baseline (which this round
didn't have). `docs/26-ble-advertising.md` has the full writeup and is where the next attempt's
result will land. `docs/17-ble-fallback.md` §3 separately measured **zero** advertisements from
this feeder under normal Wi-Fi operation, so — trigger or no trigger — there was nothing to
connect to for this change's own window. Once a name/MAC is confirmed:

1. Fill in `ble_address` with the observed MAC.
2. Run `ble.async_probe(hass, address)` (or exercise it through the options flow + a manual
   service call) and confirm a successful connection plus a characteristic read.
3. That read also settles `09-ble.md` §1.2's still-open RX-vs-TX assignment: whichever of
   0xAAA1/0xAAA2 reports the `WRITE`/`WRITE NO RESPONSE` property is RX, whichever reports
   `NOTIFY` is TX — `async_probe`'s return value (`{uuid: "prop,prop,..."}`) answers this
   directly the first time it runs against real hardware.

This document will be updated with the actual characteristic dump once that capture happens.

## Open items

1. **RX/TX UUID assignment** (`0xAAA1` notify / `0xAAA2` write) follows the public Petkit-fountain
   convention cited in `09-ble.md` §1.2; not yet independently confirmed for this device by a
   live properties read (§8 settles this once advertising works).
2. **MTU / long-write behaviour is assumed, not proven.** A Kibble frame's JSON text (roughly
   90–120 bytes depending on `id` length) exceeds the default 20-byte ATT payload; this relies on
   either MTU negotiation or the GATT "prepare write / execute write" (long write) procedure,
   both handled below the application layer by the BLE stack and orthogonal to the bytes this
   client constructs — but neither has been observed working against this specific T31 GATT
   server. If a live write is ever silently truncated, that would show up as a `crc16`/`len`
   mismatch on the device side once a decoder exists there (§6), or as a shorter-than-expected
   read in `async_probe` today.
3. **No ack protocol exists on-device yet.** `ble.async_feed` returns `True`/`False` for
   "a notify arrived" / "it didn't within 5s", not success/failure of the feed itself — until §6
   is built, `False` is the expected, non-error outcome for every real call.
4. **Whether the GATT link requires pairing/bonding for a write to `0xAAA2` to be accepted** is
   unconfirmed (`09-ble.md` §6: no auth/token strings found in `ble`/`ctrl`/`ble.img`, but
   ATT/GATT-level bonding requirements live in an attribute-table field this static pass could
   not fully decode). `establish_connection`'s default `pair=False` is used; if a live write is
   rejected at the ATT layer, this is the first thing to check.
