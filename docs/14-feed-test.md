# Feed test — local command, end-to-end (2026-09-15 05:48 UTC)

**Result: PROVEN.** A message sent by our own process on the internal bus dispensed food, with no cloud involved.

## Corrected wire format (supersedes the 16-byte envelope in STUDY-dispatch.md)
`ctrl!dispatch_send_msg` @ 0x80b00 disassembled instruction by instruction:

    memset(buf, 0, 0x220)            /* 544 = mq msgsize */
    *(u16*)(buf+0) = msg_id          /* HALFWORD, not u32 */
    *(u16*)(buf+2) = src             /* HALFWORD, read from a global, NOT the r1 arg */
    if (len) { if (len >= 0x21c) len = 0x21c; memcpy(buf+4, payload, len); }
    mq_send(mq, buf, len + 4, 0)     /* prio 0 */

So the on-wire message is **4 bytes {u16 msg_id, u16 src} + payload**, max payload 540.
`dst` is not in the message at all — it selects the queue: `open_mqueue()` @ 0x8090c formats `"/msg_dispatch_%u"`.
C signature: `dispatch_send_msg(u32 msg_id, int dst, void *payload, u32 len)`.
The earlier "16-byte {msg_id, src, dst, msg_len} envelope" was wrong; anything built on it would not have worked.

## Feed payload (67 bytes) — recovered from both builders
`ctrl!dispatch_handler_feed` @ 0x44f98 is a pure pass-through: `memcpy(stack, arg_payload, 0x43)` then
`dispatch_send_msg(0x6004, 8, stack, 0x43)`. The struct is built at 0x44020 from the cloud JSON command
`feed_realtime` (D4SH keys `id`, `amount1`, `amount2`; D4H uses `amount`; `feed_realtime_cancel` sets byte 0 = 1):

    struct feed_ctrl {          /* 67 bytes */
        uint8_t cancel;         /* +0   0 = dispense, 1 = cancel (set at 0x441aa) */
        char    id[64];         /* +1   feed-record id, memcpy'd at 0x440f8 */
        uint8_t amount1;        /* +65  hopper 1 (strb [r5,#0x41] @ 0x44128) */
        uint8_t amount2;        /* +66  hopper 2 (strb [r5,#0x42] @ 0x4413a) */
    };

## The test
Tool: `tools/kibble-msg.c`, cross-compiled static armv7 (Debian container on Unraid, gcc-arm-linux-gnueabihf),
pulled onto the device over the LAN to `/tmp/km`. Nothing on the device's flash was modified.

    /tmp/km 8 6004 1 feed:1:0:kibbletest1
    send /msg_dispatch_8 msg_id=0x6004 src=1 payload=67 total=71:
      04 60 01 00 | 00 6b 69 62 62 6c 65 74 65 73 74 31 00 ... 00 01 00
    ok

## Evidence it dispensed
| time (UTC) | observation |
|---|---|
| 05:48:27.x | `mq_send` returns ok |
| 05:48:28.8 | **HA `binary_sensor...feeding` -> `on`** (cloud round-trip, i.e. the device itself reported a feed cycle) |
| 05:48:38.9 | `feeding` -> `off` (10-second motor cycle) |

1.8 s from our local message to the device reporting a real feed. The stock cloud path was never used —
in fact the Petkit cloud session was expired at the time, so a cloud-initiated feed was impossible.

## config_shm deltas attributable to the feed (excluding watchdog toggles and known drift bytes)
| offset | before -> after | reading |
|---|---|---|
| 9916 (u32) | 21 -> 0xffffffff | food-bowl/dispense amount invalidated at feed start (HA `food_bowl_fill` had shown 21 then 0) |
| 9920 (u32) | 21 -> 0xffffffff | second hopper's counterpart |
| 9940-9941 | changed | timestamp/sequence |
| 10184 | 8 -> 9 -> 10 | monotonic event counter |
| 10216, 10218-10219 | changed | feed/MCU status words |
| **10238** | **0 -> 1 -> 0** | transient **feeding-in-progress flag** (likely `state.ble.sta_data.feed_sta`) |

10238 is the flag Kibble should surface as the `feeding` binary sensor: it is local, immediate, and needs no cloud.

## What this establishes for Kibble
The agent does not need to reverse any crypto, touch the MCU UART, or impersonate the cloud. To dispense it writes
71 bytes to `/msg_dispatch_8`. `ble`, `media`, `alg` and the watchdog stay stock.
