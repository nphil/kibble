# STUDY-bowl-fill.md — Refreshing the hopper-fill reading without the cloud (2026-09-16)

**Status: one specific attempt ruled out live, with reasons; the actual trigger still open.**

## The bug

`state.rs::off::BOWL_FILL_1/2` (`config_shm` offsets 9916/9920) mirror the T31 MCU's own
"Food Surplus Ctrl" sensor report (`08-mcu.md` UART CMD `0x19`). The vendor invalidates both to
`0xffffffff` at the start of every feed (`14-feed-test.md`) and — confirmed live this session, see
below — only refreshes them when a genuine Petkit-cloud round-trip reaches `ctrl`. With the cloud
blackholed by design, nothing ever asks again: the reading is stuck until something else prods the
MCU.

## What was tried

`ble`'s inbox message `0x601b` (`subchip_req_data` in `16-schedule.md`'s 30-entry table) is a
generic "forward this byte to the T31 MCU as a bare UART CMD" passthrough. Its handler was pulled
directly off the *live, currently-deployed* `ble` binary (`/app/bin/ble`, md5
`133ee0b50aecf9419ac64d0c150c8de5`) — read in small chunks over the feeder's telnet shell
(`dd bs=1 skip=N count=M | base64`, no local copy of the binary survives from earlier studies) and
disassembled locally with `capstone` (Thumb-2). Method and findings, in order of how they were
established:

1. **GOT base resolved to `0x50000`** — byte-for-byte the same value `26-ble-advertising.md` found
   independently (`ldr r4,[pc,#0x4b0]; add r4,pc` at `ble` vaddr `0x12496`/`0x124a6`). Exact match
   on a value nothing in this session assumed in advance — the strongest available cross-check that
   the resolution method (walk the registration block, backtrack `ldr rX,[pc,#imm]`+`add rX,pc` to
   a GOT slot, read the slot) is correct on this binary.
2. **Cross-validated the method against two already-documented addresses** before trusting it for
   anything new: `msg 0x6004` resolved to handler `0x16ecd` (`0x16ecc` + Thumb bit — exact match to
   `16-schedule.md`'s `dispatch_handler_ble_feed_ctrl@0x16ecc`), and `msg 0x600d` resolved to
   `0x16d79` (exact match to `26-ble-advertising.md`'s `dispatch_handler_ble_set_food_added`).
3. **`msg 0x601b` resolved to handler `ble` vaddr `0x176dc`.** Disassembled in full: reads its own
   4th argument (payload length) and, if nonzero, tail-calls a small wrapper at `0x1791c` with
   `r0 = payload[0]`. That wrapper (also disassembled in full, alongside its twin at `0x17930`)
   calls `build_and_send_uart_frame(cmd=r0, flag_bit6=1, subaddr=0, payload=NULL, len=0)` — **every
   byte of the bus message's payload past `[0]` is dropped**, and the wrapper hardcodes a
   NULL/zero-length UART payload regardless of what the caller had. So `subchip_req_data` can only
   ever send a *bare* command, never the MCU's normal payload for one.
4. **`build_and_send_uart_frame` itself (vaddr `0x16970`) was read directly**, not just inferred
   from `09-ble.md`'s pseudocode. Confirms that doc's structure byte for byte (sync `A55A`, `frame
   [4]=cmd` with **no translation**, `frame[6] = subaddr | 0x10 | (flag_bit6<<6)`, CRC16 trailer),
   and adds one new fact: **it special-cases `cmd == 0x19`** — an extra hex-dump-the-frame-to-log
   step before enqueueing, not present for any other `cmd` value except a similar special case for
   `cmd == 4`. The vendor singling out `0x19` here is corroborating (not proof) that this is the
   "Food Surplus Ctrl" command path.
5. **Resolved what first looked like a contradiction.** The feed handler's own call into this same
   function (through a *different* dedicated wrapper, `0x1795a`, which — unlike `0x1791c`/`0x17930`
   — does carry the real 67-byte `feed_ctrl` payload) uses `cmd = 5`, not the `0x0A` UART code
   `08-mcu.md`'s RX-side table names "Motor Run Config Cmd". These are not the same number: `0x0A`
   is what the *MCU* echoes back in its own acknowledgment frame (`ble`'s RX dispatch, a completely
   separate direction/table from the host's outbound `cmd` parameter here), not what the host sends
   to ask for a feed. `09-ble.md` §2.4 already flagged this exact link as untraced ("CMD 0x0A...
   its send call goes through one of two small CMD-parametrized wrapper stubs... not individually
   re-traced"); this session traced it, and the two numbers are independent. Practically: `cmd = 5`
   is confirmed *not* `0x19`, so sending `0x19` through `subchip_req_data` cannot be mistaken for a
   feed at this layer.

**Live test, twice, ~10 minutes apart with a full `kibbled` restart between them:** sent
`msg_id=0x601b, payload=[0x19]` to `ble`. Both times, `BOWL_FILL_1/2` went to `0xffffffff`
immediately (the same invalidate-at-start behavior the vendor's own feed path causes) and **never**
resolved to a real reading — not after 30s, not after a cumulative ~15 minutes of waiting. Net
effect: this reliably reproduces the bug's own symptom (invalidation) without ever completing the
measurement, and on the live device it actively regressed hopper 1 from a real, cloud-refreshed
reading to permanently invalid. **Reverted (`kibble` commit reverting the wiring); not shipped.**

## Why it most likely fails: the missing payload

`09-ble.md` §2.4's own scan of `ble`'s 21 direct calls into `build_and_send_uart_frame` lists
`0x19` with a **5-byte** payload — a real, non-bare request. `subchip_req_data` cannot carry that
payload (see point 3 above); whatever bare, 5-bytes-short frame it sends is not the shape the T31
firmware expects for a real surplus query, and the most likely explanation for the observed
behavior is that the MCU accepts the bare `0x19` far enough to reset its own surplus-tracking state
(hence the invalidation) but then has nothing to act on, and never reports back.

## What remains

The vendor's own dedicated 5-byte-payload `CMD 0x19` call site exists somewhere in `ble`'s `.text`
(confirmed by `09-ble.md`'s scan) but was not located this session — finding it needs either pulling
and disassembling much more of `ble`'s ~190 KB `.text` section over the telnet shell than this
session's transfer method (small `dd`+`base64` chunks, individually verified) covered in the
available time, or a way to capture the real bus message live. The latter *is* available and safe:
`ctrl`, `cloud`, and `watchdog` are all still running unmodified alongside `kibbled` (confirmed live,
`ps -ef`), so a real, briefly-enabled cloud round-trip (`POST /cloud {"enabled":true}`, confirmed
this session to actually refresh `bowl_fill` in ~5-6 minutes, then `{"enabled":false}` again) is the
device's own proven-working path — the missing piece is `strace` or equivalent on-device to capture
`ctrl`'s `mq_send` during that window, and the device's own busybox has no `strace` binary. Pulling
one over (statically-linked, matching the device's armv7 musl target) and running it read-only
during a deliberately-triggered cloud window is the next concrete step, not another disassembly
pass with the same tooling this session already pushed as far as it reasonably goes.

**Do not guess a 5-byte payload for `CMD 0x19` and send it bare through any mechanism that reaches
`build_and_send_uart_frame`** — it is a live, real MCU command on a feeder in service; nothing in
this session's disassembly rules out a payload-shape-dependent side effect worse than "no reading",
and the one thing *confirmed* safe here is that `subchip_req_data`'s bare (5-bytes-short) form does
not dispense and does not touch the feed/OTA/reset commands, not that every possible payload for
`0x19` is safe.
