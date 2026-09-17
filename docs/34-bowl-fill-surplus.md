# STUDY-bowl-fill.md — Refreshing the hopper-fill reading without the cloud (2026-09-17)

**Status: not wired as an unconditional feature this session, but one genuine, disassembly-proven-
safe lever exists and was live-tested: `surplus_control` (`config_shm` offset 3880), writable
through `kibbled`'s own settings path (Part 4). It cannot bootstrap `BOWL_FILL_1` from the invalid
state (proven, and confirmed live) but may be able to keep an already-real reading fresh locally —
untested this session, held for cross-agent device-sharing coordination; see "What remains" item 3.
The real vendor `CMD 0x19` payload is fully decoded and its only two senders are proven internal to
`ble` (Part 1); the write that actually lands a real value in `BOWL_FILL_1` in the first place was
not found in `ble`, `ctrl`, or `cloud` despite disassembling all three in full (Parts 1-3).**

## The bug, restated precisely

`state.rs::off::BOWL_FILL_1/2` (`config_shm` offsets 9916/9920) mirror the T31 MCU's own "Food
Surplus Ctrl" sensor report (`08-mcu.md` UART CMD `0x19`). The vendor invalidates both to
`0xffffffff` at the start of every feed (`14-feed-test.md`) and only refreshes `BOWL_FILL_1` when a
genuine, actively-connected Petkit-cloud session is present — confirmed twice more this session,
byte-exact (see "Empirical confirmation" below). With the cloud blackholed by design, nothing ever
refreshes it locally.

## Part 1 — `ble`: the real `CMD 0x19` sender, fully decoded [HIGH]

Pulled the full, currently-running `/app/bin/ble` this session (198,732 bytes over the feeder's
telnet shell, chunked `dd`+`base64`, md5 `133ee0b50aecf9419ac64d0c150c8de5` — byte-identical to the
copy `09-ble.md`/`16-schedule.md` analysed and to the previous session's live pull, so every address
below lines up with those docs without re-deriving anything already trusted). Disassembled the
entire 119,768-byte `.text` section with `capstone` (Thumb-2, `skipdata` enabled to step over
inline literal pools) — 50,732 instructions, matching the scale of prior passes.

**Method cross-check (required before trusting anything new):** re-derived `ble`'s GOT base
(`0x50000`, same PC-relative-pair idiom as `16-schedule.md`) and its 30-entry inbound dispatch
table by tallying every `bl` targeting the registration function (`0x25510`, called exactly 30
times) — this reproduced `16-schedule.md`'s exact 30-entry `(msg_id, handler)` table byte for byte,
including `0x6004`→`ble_feed_ctrl`@`0x16ecc` and `0x600d`→`ble_set_food_added`@`0x16d79`, the two
already-known addresses this task asked to validate against. Separately, decoded the T31→host UART
`TBH` (table-branch-halfword) RX dispatch table at `ble` vaddr `0x15b20` end to end (28 entries) and
got `CMD 0x04`→`0x15caa` and `CMD 0x0A`→`0x15f22`, both **exact** matches to `16-schedule.md`'s and
`09-ble.md`'s independently-obtained values. Four independent known-good values, four exact matches
— the two resolution methods (GOT-relative call-site scan, TBH table decode) are both trustworthy
on this binary.

### 1a. The 21 direct calls into `build_and_send_uart_frame` (`0x16970`), CMD `0x19` located

Scanned every `bl #0x16970` in `.text`: **21 call sites**, matching `09-ble.md` §2.4's count exactly.
The `cmd == 0x19` one is at `ble` vaddr `0x1812e`, inside a small dedicated function starting at
`0x180ac` — **this function, not `subchip_req_data`, is the vendor's real "Food Surplus Ctrl"
sender.** Full disassembly, `0x180ac`–`0x18135`:

```
build_ble_food_surplus_ctrl(u32 case /* r0 */, u32 control /* r1 */, u32 unused /* r3, passthrough */):
    if case == 0:                                  # 0x180c2
        buf[0]   = (u8)control                     # strb.w r1, [sp,#0x18]   @0x180c8
        buf[1:5] = clock_gettime(CLOCK_REALTIME) - 43200   # 4 bytes, LE, UNALIGNED store @0x180d8
                    # (0x24e0e: push;movs r0,#0;mov r1,sp;blx clock_gettime-PLT(0x12468);
                    #  ldr r0,[sp] -- i.e. time(NULL)-equivalent. 43200 = 0xa8c0 = 12h, subtracted
                    #  by "sub.w r0,r0,#0xa800; subs r0,#0xc0" @0x180d2/0x180d6)
        if log_level > 5: <hex-dump-the-request to the log>   # gated, cosmetic only
        build_and_send_uart_frame(cmd=0x19, flag_bit6=0, subaddr=0, payload=&buf, len=5)  # @0x1812e
        return result
    elif case == 1:                                 # 0x1815e
        build_and_send_uart_frame(cmd=0x19, flag_bit6=0, subaddr=0, payload=NULL, len=0)  # bare
        return result
    else:                                            # 0x180bc
        return control    # no-op passthrough, nothing sent
```

**The real, 5-byte, non-bare `CMD 0x19` payload is therefore:**

| Byte | Meaning | Source |
|---|---|---|
| 0 | "control" byte — `0` or `1` observed as the only values any caller ever passes (see 1b/1c) | caller's own 2nd argument, truncated to u8 |
| 1–4 | `(unix_time_now − 43200)`, u32 **little-endian** | `clock_gettime(CLOCK_REALTIME)` at send time, minus a fixed 12 h |

`flag_bit6=0` here (vs. `flag_bit6=1` for `subchip_req_data`'s bare send, §2 below) — a real,
observable difference in frame byte 6 between the vendor's native send and the one bus-reachable
path, on top of the payload-length difference already known.

### 1b. Both call sites into `0x180ac` — and why they can never fire under this device's settings

Only **one** direct caller (`bl #0x180ac` ×2, at `0x1347e`/`0x1349c`) plus one tail-call from a
sibling function (§1c). The caller is `ble`'s own periodic housekeeping task at `0x13468`,
registered — confirmed by decoding the call to `ble`'s internal task-scheduler `0x2657c` — as
**task id `0xb`, period `1000 ms`** (i.e. runs once a second, for the life of the `ble` process):

```
every 1000ms:
    state = surplus_state()                       # 0x14e54, see below
    if state != persisted_byte:
        build_ble_food_surplus_ctrl(case=0, control=state)   # REAL 5-byte send
        persisted_byte = state
    else:
        tick_count += 1
        if tick_count >= 600:                       # 600 * 1s = 10 minutes
            build_ble_food_surplus_ctrl(case=1, control=0)    # BARE send, no payload
            tick_count = 0
```

`surplus_state()` (`0x14e54`) reads two `config_shm` fields via `g_config`'s own GOT slot (the
same 2-level PC-relative→GOT→pointer idiom used everywhere else in this codebase):

```c
u32 surplus_control  = *(g_config + 0xf28);   // = config_shm offset 3880 — usr.app_conf.surplusControl
u32 surplus_standard = *(g_config + 0xf2c);   // = config_shm offset 3884 — usr.app_conf.surplusStandard
u32 bowl_fill_1       = *(g_config + 0x26bc); // = config_shm offset 9916 — state.off::BOWL_FILL_1 itself
if (surplus_control == 0 || surplus_standard == 0) return 1;   // unconditional short-circuit
return (surplus_control > bowl_fill_1) ? 1 : 0;
```

The offsets `0xf28`/`0xf2c` were resolved to the literal cJSON keys **`"surplusControl"`** and
**`"surplusStandard"`** by tracing the one function in `ble` that writes them (`0x28680`, a loader
for a JSON `"app_conf"` sub-object out of **`/opt/user.conf`** — confirmed by the string `access()`
call on that exact path at the loader's entry, `0x292ac`/`0x292b2`). **Both fields were captured
live this session, from a real `config_shm` dump, at `surplusControl = 0`, `surplusStandard = 2`**
— matching the identical values found independently in a `ctrl` process heap dump pulled earlier
this session. Because `surplusControl == 0`, `surplus_state()` **unconditionally returns `1`,
every single tick, regardless of `bowl_fill_1`** — the ticker's persisted byte settles to `1` after
its first tick and can then never "change" again, so its real-payload branch is, under the device's
current settings, reachable **at most once per `ble` process lifetime** (right after `ble` starts).
The rest of the time it only ever sends the bare/empty variant every 10 minutes — the same shape
already proven (by the prior session and independently confirmed by this session's own
disassembly of `subchip_req_data`'s wrapper) to *not* produce a real MCU reply.

**This ticker is not gated by, or connected to, any bus message or the cloud at all.** It runs
identically whether `ctrl`/`cloud` are chatty or silent. It is not the explanation for the
cloud-correlated refresh.

### 1c. The second path: `ble`'s own UART-RX handler for an *incoming* `CMD 0x19` frame

The `TBH` table (§ above, `CMD 0x19` → handler `0x167d4`) decodes to:

```
on receiving a CMD=0x19 frame from the T31 MCU:
    if log_level > 4: <log the frame's byte[7]>
    mode = <the frame's own flags/subaddr nibble>       # r7, set by the shared RX prologue
    record = frame_payload + 7                          # r5 += 7
    if mode == 1: <copy (frame_len-9) bytes from record onward via 0x19958>   # not further traced
    build_ble_food_surplus_ctrl_general(mode, record)     # tail-call, see below
```

...which tail-calls a second, parameterised twin of §1b's ticker body, `0x14e88` (confirmed to be
the *only* caller: one `bl #0x14e88` in the whole binary, at `0x1682e`, exactly this RX handler):

```c
u32 surplus_ble_food_surplus_ctrl_general(u32 mode, u8 *record /* [state:u8][ts_adj:u32] */) {
    u32 state = surplus_state();                 // 0x14e54, same as §1b
    if (mode == 0) {
        build_ble_food_surplus_ctrl(case=0, control=state);   // unconditional real send
    } else if (mode == 1) {
        record->ts_adj += 43200;                              // undo the -43200, in place
        if (record->state_byte != state)
            build_ble_food_surplus_ctrl(case=0, control=state);  // send only if changed
    }
    // any other mode: no-op
}
```

So **receiving a `CMD 0x19` frame from the MCU can itself trigger `ble` to send another `CMD 0x19`
request** — a self-contained request/ack loop between `ble` and the T31, with no `ctrl` or `cloud`
participation at this layer. This still does not explain what makes the *very first* `CMD 0x19`
frame in such a chain appear — that is opaque TC32 MCU firmware (`ble.img`), not disassemblable by
the tooling available this session (`08-mcu.md` §2 item 6 already flags this limitation). **[MED]**:
the call graph above is disassembly-proven; *why* the MCU emits an unsolicited/first `CMD 0x19` at
all, and whether it is influenced by unrelated UART traffic ctrl sends on cloud-connect (e.g. an
RTC refresh, `16-schedule.md` §3.2), is not established either way this session.

### 1d. No bus message reaches either path — confirmed, not assumed

`ble`'s own 30-entry inbound dispatch table (`0x6001`–`0x601e`, re-derived fresh this session, §
above) was checked entry-by-entry against both `0x13468` and `0x14e88`/`0x180ac`: **no entry calls
either.** The one message that *does* reach the frame-builder for an arbitrary `cmd` value,
`subchip_req_data` (`0x601b`), was re-disassembled fresh this session end to end and matches the
prior session's finding exactly: its wrapper (`0x1791c`) hardcodes `flag_bit6=1, payload=NULL,
len=0` — the bare, wrong-flag, already-tested-unsafe variant, not `0x180ac`'s real 5-byte one.
**There is no `ctrl`→`ble` bus message, in this build, that reaches the real payload path.**

## Part 2 — `ctrl`: reads and invalidates `BOWL_FILL_1`, a real setter not located [MED]

Pulled the full, currently-running `/app/bin/ctrl` (742,148 bytes, md5
`c645c0665da2cf73db93ffa8d9d0ea68` — matches live `/app/bin/ctrl`) and disassembled all of `.text`:
**211,108 instructions**, matching `STUDY-config.md`'s independent count over the same binary
exactly (`ctrl`'s `.text`/`.rodata`/`.got` section boundaries also matched `16-schedule.md`'s
figures byte for byte) — strong confirmation this is the identical build every prior study used.

Searched `.text` for every reference to `config_shm` offset `9916` (`0x26bc`, `BOWL_FILL_1`) and
`9920` (`0x26c0`, `BOWL_FILL_2`), by both the 16-bit `movw` immediate form and any instruction whose
own memory-operand displacement equals that value. **Exactly two hits, both at the same site
shape, neither a "set to a real value":**

1. **Read**, `ctrl` vaddr `0x3187e` — copies `BOWL_FILL_1` into a local "current device state"
   struct (alongside ~10 other `config_shm` fields — MAC-ish bytes, MCU-status bytes, the `FEEDING`
   flag, a `usr.app_conf`-derived bool, `dev.mac_info`...). That struct feeds a **delta-JSON**
   builder a few dozen bytes later (`0x31c66`–`0x31d38`) which only emits a field into a `"sta"`
   status object when it differs from a shadow/previous copy — and the key it emits `BOWL_FILL_1`
   under, confirmed by a direct string cross-reference, is **`"bowl"`** (cast **signed**, so
   `0xffffffff` prints as `-1` — exact match to the literal `"bowl":-1` seen in an early `ctrl` heap
   dump this session). This is outbound reporting (`ctrl` telling the cloud the current value), the
   opposite direction from what this task needs.
2. **Write**, `ctrl` vaddr `0x395be`/`0x395c4` — stores `0xffffffff` into **both** `BOWL_FILL_1` and
   `BOWL_FILL_2` unconditionally, gated by "are we within ~59 seconds of a stored timestamp"
   (`subs r0,#0x3b; cmp r0,r3; bgt <skip>`) — this is the feed-triggered **invalidation**
   (`14-feed-test.md`'s already-documented behaviour), not a setter.

**No third reference — a genuine "set `BOWL_FILL_1` to a measured value" write — was found in
`ctrl` by this method.** This is explicitly **not proof `ctrl` never does it**: the search can only
see writes through the *raw* `g_config`-relative absolute offset (`movw #0x26bc` or an equivalent
direct displacement). `ctrl`'s own report-builder (item 1 above) demonstrably uses an
*intermediate* base-pointer pattern for its neighbouring fields (`str r2,[r4,#0xNN]` against a
`r4` established once, far away) — the exact shape that would make a real setter invisible to a
search for the literal `0x26bc`. Chased two named candidates from `ctrl`'s `(dispatch_call_func)`
name table — `net_dev_get_device_info`, `net_dev_state_report` (both very plausible names for "the
cloud asked for our state") — but could not resolve either string's own call site with this
session's PC-relative xref method (unlike every other string resolved this session, a wide
window did not find it either — this name table is evidently walked by a different, table-driven
mechanism this pass didn't reverse). **[MED confidence]** that `ctrl` is not the setter; **[HIGH
confidence]** on everything it *was* shown to do (the two hits above, both fully disassembled).

## Part 3 — `cloud`: maps the shared memory, no reference found either [MED/INFERENCE]

Pulled the full, currently-running `/app/bin/cloud` (202,980 bytes, md5
`7bfe15e907927c121704fac933658b3f`) and disassembled `.text` (50,271 instructions). `cloud` does
import `shm_open`/`mmap`/`g_config` and references the literal string `"config_shm"`, so it can and
almost certainly does map the same shared memory as every other process. Searched it the same way
as `ctrl`: **zero references to offset `9916`/`9920`**, by any of the three methods used (16-bit
`movw` immediate, direct memory-operand displacement, or a raw 32-bit literal-pool word matching
either value anywhere in `.text`). `cloud`'s own `(dispatch_call_func)` name table is entirely
about **video/event cloud storage** (`cloud_uploader_main_task`, `cloud_cvr_indate_timer`,
`upload_cvr_stop`, `event_queue_detect`, `check_update_token_by_indate`...) — nothing
property/state/surplus-shaped. **[MED/INFERENCE]** `cloud` is not the setter either, same blind-spot
caveat as `ctrl` above (an intermediate-pointer write would be invisible to this search).

**Not checked this session, for lack of remaining time budget:** `media` (also confirmed by
`STUDY-config.md` to map `config_shm`). It is the one of the four non-`watchdog` processes this
document has never actually disassembled. Flagged as the single most promising next static-analysis
step — see "What remains".

## Empirical confirmation (byte-exact `config_shm` diff across a real refresh) [HIGH]

Captured full 11,952-byte `/dev/shm/config_shm` dumps (`dd`+`base64` over the feeder's telnet shell,
saved locally, byte-diffed) across two independently-triggered cloud-enable windows this session.

**Run 2 (the tight one, snapshotting every ~10 s):** cloud enabled at `t=0`; polled `GET /state`
and dumped `config_shm` on every poll. `bowl_fill` stayed `[null, null]` through `t=125.5s`; at
`t=136.4s` it read `[49, null]` — a transition inside an **11-second bracket**. The full
byte-for-byte diff between the `t=125` and `t=136` dumps, over the *entire* 11,952-byte structure:

```
[9916:9920) len=4   before=ffffffff  after=31000000     <- BOWL_FILL_1: 0xffffffff -> 49, LE u32
```

...and nothing else, once the already-known-and-unrelated watchdog liveness toggles
(`10284`/`10288`/`10296`/`10300`, `state.off::ALIVE_*`, confirmed fluctuating identically in a
cloud-disabled control diff taken minutes earlier) are excluded. Two more dumps 8–9 s apart
afterward (`t=145`, `t=154`) show `BOWL_FILL_1` **stable at `49`**, no drift.

**This directly answers every question the assignment posed about the write:**

- **No measurement timestamp.** Nothing in the 11,952-byte structure changes alongside
  `BOWL_FILL_1` except itself. `EVENT_COUNTER` (10184), `FEEDING` (10238), and every other
  documented "status word" were unchanged across this exact transition.
- **No validity/freshness flag.** Same evidence — there is nothing to check *except* the value's
  own distance from the `0xffffffff` sentinel. `kibbled`'s existing "is it `0xffffffff`" check is
  already the only signal that exists on the wire.
- **`BOWL_FILL_2` (hopper 2) never changes.** Confirmed `0xffffffff` at every single sample across
  both full runs (`before`, 13 intermediate polls, the transition, and both follow-ups) — this
  device's own behaviour is that hopper 2 is never populated by this mechanism, full stop, not a
  gap in this study's observation window.
- **The value does not expire on a short fixed timer.** It was still `49` at the *end* of this
  session's active work (many minutes after the capture), but — consistent with the assignment's
  own prior-session note and this session's *first* (accidentally long-running) capture, in which
  `bowl_fill` had reverted to `null` again after an extended gap with no feed in between — it does
  eventually go stale on its own. No exact TTL was measured either session.
- **Genuine cloud connectivity is required, confirmed live**, not just the local route flag: `GET
  /cloud`'s `connections` list showed real `ESTABLISHED`/`TIME_WAIT` TCP sessions to
  `47.88.20.79:443`/`47.88.52.254:443` (Alibaba-Cloud-hosted, consistent with the already-documented
  Alink/Alibaba IoT backend) and DNS lookups to `8.8.8.8`/`199.85.126.10` throughout the window
  before the transition — this is a real network round trip, not a local timer.

A first, accidental long-running capture this session (kept enabled far longer than intended due to
a tooling issue, corrected mid-session — cloud was disabled the moment it was noticed and re-verified
disabled at the end) also produced a refresh, to the identical value `49`, in a comparable window —
two independent, reproducible observations.

## What was NOT done, and why

**Nothing was wired into `kibbled`.** The assignment's own bar — "If (and only if) you have
positively identified the message AND can argue from the disassembly that it cannot actuate a
motor/OTA/reset" — was not met, for a more fundamental reason than safety: **no message reaches the
real sender at all.** `0x180ac` (the only function that ever builds a real 5-byte `CMD 0x19`
payload) has exactly two callers, both internal to `ble`'s own timer/UART-RX plumbing (§1b/§1c),
neither reachable from any of `ble`'s 30 registered inbound bus messages. The one bus message that
*is* reachable (`subchip_req_data`) was independently re-confirmed this session, by fresh
disassembly, to reproduce exactly the prior session's already-recorded unsafe result (bare payload,
wrong `flag_bit6`, invalidates without ever refreshing) — so there was also nothing new to
re-test live. No `surplus.rs` module, rate limiter, or bus send was written: doing so with no
message that could ever actually reach the vendor's real sender would be dead code with a
misleading name, not a working feature.

## Part 4 — `surplusControl` via `kibbled`'s own settings path: safe, but a confirmed no-op while invalid [HIGH]

`agent/src/settings.rs` already mapped `surplus_control` (`config_shm` offset 3880) from an
earlier, independent settings-write study (`docs/15-settings-write.md`), which fully disassembled
`ctrl`'s cloud/local `property/set` handler. That study's own table already names this exact field
as ble's `usr.app_conf.surplusControl` (§1b) — but shipped `writable: false`, with a documented
reason: Localkit's own app schema calls it **read-only** ("Leftover food detection state"), unlike
its sibling `surplusStandard` (0–100, the real user-facing "Leftover food threshold", also
`writable: false` but for no stated reason). `ctrl`'s own write site for `surplusControl` (this
session independently re-disassembled it at vaddr `0x3f4c6`, exact match to that study's own
finding) is byte-for-byte the same shape as every other benign `usr.app_conf.*` setting: `read
cJSON valueint → compare to current → str.w the new value → dead self-message (msg_id 2, dst=1,
no registered handler)`. Nothing else. `ctrl`'s cloud backend evidently *can* push a value here
(the write site is real, reachable code) even though the phone app's own UI doesn't expose it —
consistent with it being firmware/cloud-computed derived state day to day, not a literal user input,
which is presumably why a previous session left it non-writable rather than a safety finding.

**Exhaustive safety re-check before touching it, per the assignment's own bar:** every
`dispatch_send_msg` call site in `ctrl` was enumerated (93 total) and its `msg_id` immediate read
directly. **Exactly two** ever send `0x6004` (`BLE_FEED_CTRL`, the only ctrl→ble feed trigger that
exists): `dispatch_handler_feed` itself (`0x44fb4` — confirmed **zero direct callers anywhere in
ctrl**, i.e. only reachable via an *externally*-arriving msg `0x100f`, never internally) and the
`"feed_realtime"` cloud action command (`0x4414c`). Neither call site is anywhere near
`surplusControl`'s write (`0x3f4c6`), the surplus-report read (`0x3187e`, Part 2), or `ble`'s
resulting `CMD 0x19` send (Part 1). **No path from a nonzero `surplusControl`, through anything
this session could find, reaches feed, motor, OTA, or reset.**

### The live test

Flipped `surplus_control` to `writable: true` (with the rationale above recorded in its
`description`), deployed (`feeder_deploy_kibbled`, built md5 == running md5), confirmed cloud still
`enabled:false` post-restart. `POST /config {"key":"surplus_control","value":30}` with the device
at rest (`feeding:false`, `bowl_fill:[null,null]`) — then polled `GET /state` every 2s for 51s
(watching `feeding` for an immediate-abort trigger that never fired) and captured a full
`config_shm` diff across the window. Result: **`feeding` stayed `false` for the entire window**;
**`bowl_fill` never changed** (stayed `[null, null]`); the *only* offset-3880-adjacent change in
the diff was the write itself (`00 -> 1e` at 3880). Every other changed byte in the diff (a handful
of watchdog-toggle and detection-telemetry offsets already known to drift on their own, per the
earlier baseline-noise diff in Part "Empirical confirmation") is unrelated background device
activity, not a consequence of this write.

**This is a safe, but *predicted and confirmed* no-op, not an inconclusive result** — it follows
directly from a fact this session verified down to the raw instruction bytes, not just the
`movle`/`movgt` mnemonic text: `ble`'s comparison (`0x14e70`–`0x14e78`) is a **signed** `cmp`+`ite
le` (condition codes `14`=LE, `13`=GT — genuinely signed, not `ls`/`hi`). `BOWL_FILL_1`'s invalid
sentinel `0xffffffff` reads as **`-1`** under that comparison, so `surplus_control(K) > -1` is true
for any realistic non-negative `K` — the exact same result `ble`'s ticker already settles on
forever while `surplusControl == 0` (Part 1b). With no prior real `BOWL_FILL_1` value to compare
against, there is no signed threshold write that creates an edge without going deliberately
out-of-domain (a value that reads negative when reinterpreted signed, e.g. `0xfffffff0`) — which
this session chose not to attempt, since it exercises the field for a shape nothing in this
firmware would ever produce, on a field this session cannot fully vouch for beyond the one `ble`
consumer traced. `surplus_control` was restored to `0` immediately after the test
(`POST /config {"key":"surplus_control","value":0}`, confirmed via `GET /config` readback).

**The one still-open, more promising variant** (not attempted this session, deferred for a device-
sharing conflict with a concurrent capture from a sibling session): with a *real* `BOWL_FILL_1`
value already in place (seeded the usual way, via the cloud-toggle trick), set `surplus_control` to
something *below* that real value *before* it expires. That is a genuine signed transition
(positive vs. a lower positive threshold, no sentinel involved) and, unlike this test, could
plausibly flip `ble`'s persisted ticker state and fire a real `CMD 0x19` send independent of the
cloud — testing whether `surplus_control` can *keep* an already-seeded reading fresh locally, not
whether it can bootstrap one from nothing (the disassembly in Part 1b already answers that: it
cannot, the comparison needs a real prior value to be meaningful).

## What remains — the concrete next step

1. The write that lands a real value in `BOWL_FILL_1` in the first place is in one of: (a) `ctrl`
   or `cloud`, behind an intermediate-pointer addressing pattern this session's xref tooling cannot
   see (both binaries are now fully pulled and disassembled locally — `/tmp/ctrl_full.bin`,
   `/tmp/cloud_full.bin`, matching live md5s — so a future pass needs *better tooling*, not another
   pull: a real decompiler (`radare2`/Ghidra) with proper data-flow tracking would resolve every
   base-pointer chain `STUDY-config.md` §6.2 already flagged this codebase needs one for); or (b)
   `media`, not inspected this session at all. Check `media` first — a single, cheap disassembly
   pass with the same tooling already proven on the other three binaries.
2. A live packet capture (`strace`/pulling a static build onto the device to watch `mq_send`, or a
   UART tap) remains the fallback if static analysis stalls again — unlike the prior session's
   assessment, this is no longer the *only* path forward, since the ble-side mechanism is now fully
   mapped; it's specifically the `ctrl`/`cloud`/`media`-side "who actually writes the number"
   question that would benefit from it.
3. **Finish Part 4's deferred experiment**: with a real `BOWL_FILL_1` value in place (seed it via
   the cloud-toggle trick), set `surplus_control` to a value below it, before it expires, and watch
   for an independent (cloud-off) `CMD 0x19` send / value change. This is a five-minute experiment
   with all the tooling already built this session — it was deferred only because a sibling agent
   was mid-capture on the same physical device when this session ran out of time, not for any
   unresolved safety or technical concern.

**Do not send `CMD 0x19` (in any shape) to `ble` through `subchip_req_data`/`0x601b` — this remains
independently re-confirmed, twice now, to invalidate `BOWL_FILL_1` without ever completing a
measurement**, and this session additionally confirmed *why*: that path can only ever reach the
bare/`flag_bit6=1` variant, never the real 5-byte/`flag_bit6=0` one `0x180ac` builds. Do not attempt
to reach `0x180ac`/`0x13468`/`0x14e88` by any other locally-available bus message either — none
exists in this build. **Writing `surplusControl`/`surplusStandard` (offsets 3880/3884) through
`kibbled`'s own `settings.rs`/`persist.rs` path is safe and was live-tested this session** (Part 4)
— that is not the "poking shared-memory bytes directly" this document previously warned against:
it is the exact same `flock(/tmp/config.lock, LOCK_EX)`-guarded, single `str.w`-at-a-time write
`ctrl`'s own settings handler performs for this field, going through `kibbled`'s already-proven
write+reconcile machinery, not a bypass of it. What remains genuinely unproven is only whether it
*helps*: with `BOWL_FILL_1` invalid, it provably cannot (Part 4's signed-sentinel finding); with a
real value already in place, it might (Part 4's untested variant). Do not, however, write a
deliberately out-of-domain (negative-when-signed) value to `surplus_control` to force an edge from
the invalid state — that exercises a shape nothing in this firmware would ever legitimately
produce, on a field this document cannot fully vouch for beyond the one `ble` consumer traced.
