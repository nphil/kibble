# STUDY-bowl-fill.md — Refreshing the hopper-fill reading without the cloud (2026-09-17)

**Status: not wired as a live feature. One genuine, disassembly-proven-safe lever was found and
live-tested twice this session — `surplus_control` (`config_shm` offset 3880) — and both tests
came back negative: it cannot bootstrap `BOWL_FILL_1` from the invalid state (proven statically
and confirmed live), and it does not keep an already-real reading fresh either (tested live against
a real value of `44`, no effect over 42s). It shipped `writable: false` again, same as before this
session, with the full evidence trail kept in its `settings.rs` description. The real vendor
`CMD 0x19` payload is fully decoded and its only two senders are proven internal to `ble` (Part 1);
the write that actually lands a real value in `BOWL_FILL_1` in the first place was not found in
`ble`, `ctrl`, or `cloud` despite disassembling all three in full (Parts 1-3). See "What remains".**

**UPDATE (Part 6, same-day follow-on session): the real setter is found. It is not in `ble`,
`ctrl`, or `cloud` -- it is in `media`, a process Parts 1-3 never disassembled. A function at
`media` vaddr `0x1c088` takes a float (`s0`, the VFP first-argument register), multiplies it by
a literal `100.0`, truncates to `int32`, and stores the result unconditionally at
`config_shm+9916` (`BOWL_FILL_1`) -- consistent with a 0.0-1.0 bowl-fullness score from an
image-analysis model, scaled to a percentage, not a physical sensor reading relayed through the
T31 MCU at all. Two independent live `ptrace` captures this session (spanning both of this
session's real cloud-triggered landings) confirm this from the wire side: zero UART `CMD 0x19`
traffic and zero ble-bound bus message of any kind correlates with either landing -- Part 1's
`CMD 0x19` mechanism is real but is demonstrably **not** how the cloud-triggered refresh happens.
**Still not reproducible locally**: `media`'s setter is reached only through an indirect
(`.got`-slot) call this session could not trace to its selector, `media` imports no socket/TLS
functions (so it is not fetching the score over its own connection), and no bus message reaches
it either -- there is nothing safe to send from `kibbled` yet. See Part 6 for the full evidence,
the two ble-bound messages this session ruled out by fresh disassembly, and the concrete next
step.**

**UPDATE (Part 7, same-day follow-on session): wired and shipped.** `media`'s setter being
unreachable turned out not to matter: `agent/src/foodlevel.rs` + `tools/kibble-food.c` compute
the same 0.0-1.0 score independently, on-device, by `dlopen`ing the vendor's own
`/alg/libalgo.so` and calling its real `CPetkitAlgoFoodDetect` methods directly against a frame
`kibbled` already has -- no `media` cooperation, no cloud, needed at all. `GET /state` now reports
`bowl_fill_local:[pct, computed_unix]` live on the real device, survives a `kibbled` restart, and
leaves the video stream and every vendor process untouched. See Part 7.


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

**The more promising variant was also run, later the same session, once device-sharing with a
concurrent sibling capture cleared: negative.** Seeded a real `BOWL_FILL_1` via the cloud-toggle
trick (enabled at unix `1789621179`, a real value — `44` — appeared at `t=322.6s`, disabled
immediately at unix `1789621502`, a ~5.4-minute window, within the task's own "1-6 minutes"). With
`BOWL_FILL_1=44` confirmed live and cloud back off, wrote `surplus_control=20` (below `44` — a
genuine signed transition, `20 > 44` is false, no sentinel involved, exactly the shape that should
flip `ble`'s persisted ticker state per the Part 1b model) and watched `GET /state` plus a fresh
`config_shm` diff every ~2-3s for 42 seconds. **No effect of any kind**: `BOWL_FILL_1` stayed
exactly `44` the entire window (not even a transient invalidate-then-reset), `feeding` never went
true, and the full-structure diff between the pre-write and final snapshot shows nothing changed
except the write itself (offset 3880) plus the same already-catalogued watchdog-toggle noise.
Restored `surplus_control` to `0` immediately after (confirmed via `GET /config` readback), and
reverted the `writable: true` flip in `settings.rs` back to `false` — two independent live tests
now agree this lever does not move `BOWL_FILL_1`, so this document no longer recommends shipping
it writable; the field's disassembly-proven safety and the full evidence trail stay recorded in
`settings.rs`'s own description for whoever revisits this.

**What this means for the Part 1b model:** the ticker's edge condition, as read from static
analysis, predicted a state flip here. It didn't produce an observable effect. Rather than
overclaim the static model is simply wrong, the honest gap this leaves open: (a) the edge may have
fired and `ble` may have sent a real `CMD 0x19` request that the T31 answered with the *same*
value (`44`) it already had cached, which would be indistinguishable from "nothing happened" at
the `config_shm` level without a UART tap; (b) `ble`'s actual persisted-ticker-byte value at the
moment of the write is not independently observable from outside the process, so this session's
assumption that it had long since settled to `1` (Part 1b) is plausible but not proven; or (c)
there is a gating condition on the edge send this session's static pass did not find. None of these
change the safety conclusion (still nothing reaches feed/motor/OTA/reset either way) — only the
"is this useful" one, which is now a confirmed no, twice, live.


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
3. **Part 4's `surplus_control`-as-local-keepalive idea was fully tested this session and came
   back negative both ways** — do not re-attempt it without new evidence beyond what Part 4
   already covers (its own "what this means" paragraph lists the three honest possibilities left
   open, none of them safety-relevant). If revisited, a UART tap would be the only way to tell
   "no edge fired" apart from "an edge fired and the T31 just re-confirmed the same value" —
   `config_shm` alone cannot distinguish the two.

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


## Part 5 — `mqtrace`: a ptrace(2) syscall tracer, built and validated, capture not yet run [HIGH]

Static analysis (Parts 1-4) proved no bus message or local `config_shm`/settings write reaches
the real `CMD 0x19` sender (`ble` vaddr `0x180ac`); the only way forward is observing the vendor
stack during a cloud-triggered refresh. This session built that observation tool, deployed it,
and proved it correct against real vendor traffic — but ran out of session budget immediately
before running the actual cloud-enable capture. **The capture itself (assignment step 2) is the
concrete next step for a future session; everything needed to run it is now in place.**

### The tool: `tools/mqtrace`

`tools/mqtrace/src/main.rs` is a self-contained `PTRACE_SEIZE`-based syscall tracer, `libc`-only
dependency (kept out of `agent/`'s zero-dependency tree). Design, independently verified against
the real headers at `/usr/arm-linux-gnueabihf/include/asm/{ptrace,unistd-eabi}.h` on the build
host (not assumed from memory):

- Attaches to a target pid **and every thread** in `/proc/<pid>/task` via `PTRACE_SEIZE` (a
  two-pass scan catches threads spawned between the initial listing and the seize loop), with
  `PTRACE_O_TRACESYSGOOD | PTRACE_O_TRACECLONE` — deliberately **not** `TRACEFORK`/`TRACEVFORK`,
  so a `system()`-spawned child (e.g. `ctrl`'s `reset_wifi.sh`) is never auto-attached or put at
  risk of being left stopped.
- Decodes only at syscall **exit** (one `PTRACE_GETREGS` per event; ARM's syscall-return path
  preserves all registers except `r0`, so `orig_r0`/`r1`/`r2` plus the returned `r0` are all
  available in one call): `read`=3, `write`=4, `mq_timedsend`=276, `mq_timedreceive`=277.
- For `mq_timedsend`/`mq_timedreceive`: resolves the queue name via `/proc/<pid>/fd/<fd>`
  (POSIX mqueue fds resolve to a name like `/msg_dispatch_8`) and hex-dumps the full envelope,
  read out of the tracee's address space through `/proc/<pid>/mem` (`pread` at `addr as u64`,
  confirmed no sign-extension hazard since musl's `off_t` is always 64-bit on this target).
- For `read`/`write`: logs the byte payload **only** when the fd resolves to `--uart-path`
  (default `/dev/ttyS3`); every other fd is decoded (for its mq_* role, if any) but never
  hex-dumped, keeping the log free of unrelated I/O noise.
- Shutdown is unconditional and glitch-free by construction: `SIGINT`/`SIGTERM` (and a
  `--timeout`, default 240s) trigger `begin_shutdown()`, which sends `PTRACE_INTERRUPT` to every
  tracked tid and detaches each one on its next stop of *any* kind — this only works because the
  tracees were `SEIZE`d, not `ATTACH`ed; it guarantees no tracee is ever left stopped regardless
  of what it was doing when shutdown was requested.
- CLI: `mqtrace --pid <pid> [--timeout 240] [--uart-path /dev/ttyS3] [--log /tmp/mqtrace.log]`.

**Build:** the committed source builds with a plain, stable-toolchain
`cargo build --release --target armv7-unknown-linux-musleabihf` (`.cargo/config.toml` mirrors
`agent/`'s linker override) — no unstable flags, so the assignment's literal build invocation
works unmodified. That build is 359,152 bytes. A separate, **not committed**, throwaway
nightly `-Z build-std=std` + `panic=immediate-abort` build (359,152 → 87,352 bytes) was used
*only* to shrink the one-time manual transfer payload; it lives outside the repo
(`/tmp/mqtrace_deploybuild/` on the sandbox, not the device) and has no bearing on how the tool
is meant to be built normally.

**Deployment:** there is no scp/tftp path from the sandbox to the feeder (Tailscale-only DNS, no
direct LAN route; only `feeder_http`/`feeder_shell`/`feeder_deploy_kibbled` may touch the device).
The 87,352-byte binary was gzip'd (56,101 bytes), base64'd (74,888 chars), and transferred as 9
chunked heredocs over the shared `feeder_shell` telnet session, with a full `wc -c` + `md5sum`
check after every chunk and a bisect-then-`dd`-patch recovery for any mismatch. **xz was tried
first and abandoned**: busybox's `xz` applet cannot decompress liblzma's default CRC64-checked
stream (`xz: corrupted data` on `-t`/`-d`) even after recompressing with `--check=crc32` and
smaller dictionaries; gzip round-trips reliably and busybox has full `gzip`/`gunzip`. Final
on-device binary: `/tmp/mqtrace`, 87,352 bytes, md5 `955e44e96264fb85a53cb3eb5ebeef84` — byte-
identical to the local nightly-shrunk build, confirmed after `base64 -d | gunzip`.

### Validation (all three required derisking steps, all clean)

**1. `kibbled` self-test** — attached to pid 12105 (13 threads: main + 12 workers), 15-25s
windows, triggered `GET /state`/`GET /cloud` during the attach. Log showed correctly-attributed
`SIGNAL sig=17` (SIGCHLD) forwarding on multiple threads (proving genuine signals are passed
through, not swallowed) and a clean `SHUTDOWN` → 13×`DETACH` → `DONE` sequence exactly at the
configured timeout. `kibbled` kept serving `GET /state` correctly throughout and after every run
(confirmed via repeated live queries); no crash, no hang, no restart triggered by the attach.

**2. `ble` idle test** — attached to pid 203 (3 threads: 203/206/207), 30s, zero messages sent.
Captured 83 lines of **real, correctly-decoded** UART + bus traffic, e.g.:

```
23322.271517 ATTACH pid=203 tids=[203, 206, 207] uart_path=/dev/ttyS3 log=/tmp/mqcap_ble_idle.dat
23322.519504 UART_READ  tid=207 fd=6 path=/dev/ttyS3 len=11 bytes=5aa50b000141c06e001ec2
23322.732656 UART_READ  tid=207 fd=6 path=/dev/ttyS3 len=10 bytes=5aa50a00004dc0010508
23322.732868 MQSEND     tid=207 mqd=4 q=/msg_dispatch_8 len=13 msg_id=0x601a src=8 payload=5aa50900004e10b0de
23322.733026 MQRECV     tid=203 mqd=4 q=/msg_dispatch_8 len=13 msg_id=0x601a src=8 payload=5aa50900004e10b0de
23322.733153 UART_WRITE tid=203 fd=6 path=/dev/ttyS3 len=9  bytes=5aa50900004e10b0de
```
repeating on a steady ~2s cadence for the full 30s window (SEQ byte incrementing `0x4e, 0x4f,
0x50, …, 0x5c` with no gaps). Findings this independently confirms or adds:
- **The bus envelope format is empirically confirmed on live traffic**, not just static analysis:
  `len=13` = 4-byte header (`msg_id=0x601a` + `src=8`, both LE u16) + 9-byte payload, matching
  `bus.rs`/`docs/14-feed-test.md` exactly.
- **New:** `ble` uses the *same* bus/mqueue mechanism internally between its own threads, not
  just for cross-process IPC — thread 207 (UART reader) posts every decoded frame to `ble`'s own
  `/msg_dispatch_8` queue (`msg_id=0x601a`, previously unattributed — adjacent to the known
  `0x601b`/`subchip_req_data`), which `ble`'s main thread (203) consumes and acts on.
- **New:** the idle ~2s heartbeat is MCU CMD `0x01` (status report, growing SEQ) answered by
  `ble` with a CMD `0x00` frame (bare ack/heartbeat, no payload, SEQ copied/incremented) — this
  is routine keepalive chatter, not the surplus mechanism, and now has a documented fingerprint
  so a future capture can filter it out when isolating the cloud-triggered sequence.
- `config_shm+10304` (`ALIVE_BLE`) was read before/after: still cycling normally (not stalled),
  and the SEQ counter inside the captured frames never skipped a beat across the whole attach —
  i.e. the ptrace attach has no observable effect on `ble`'s real-time behavior.

**3. `ctrl` idle test** — attached to pid 217 (6 threads: 217/229/230/231/232/432), 25s, zero
messages sent. Clean attach, `SIGNAL sig=17` forwarding observed (thread 231 reaping children —
consistent with `ctrl`'s periodic `system()` calls), clean `SHUTDOWN`/6×`DETACH`/`DONE` at
timeout. `pidof ctrl` unchanged (217) and CPU time still accumulating after detach.

No bus message or UART frame was ever sent by this tool or this session — every capture above is
purely passive (`PTRACE_SYSCALL` observation only; nothing was injected).

### Device quirk discovered in the process: `/tmp/*.log` is not durable [HIGH]

The first self-test's log (`/tmp/mqtrace.log`) was found silently zeroed a few minutes after a
clean run (confirmed via `wc -l`/`md5sum` at 19 valid lines, then 0 bytes minutes later, binary
untouched). Ruled out: a bug in `mqtrace` itself (grep-confirmed exactly one `OpenOptions::new()
.create(true).append(true)` call site in the whole source, never reopened/truncated); a collision
with sibling agent `BowlFillPart2` (confirmed zero device activity at the time). Root cause:
`ps` shows both a standard busybox `syslogd`/`klogd` *and* a vendor `axsyslogd`/`axklogd` running;
`/tmp` is `tmpfs`; a directory listing showed **every** `*.log` file in `/tmp` — including
unrelated ones like `daemon.log`, `syslog.log`, and an old `cpu_sample_live.log` scratch file from
a different investigation — zeroed at the exact same mtime, while every non-`.log` file (`.txt`,
`.out`, `.json`) in the same directory from the same timeframe was untouched. **Any file under
`/tmp` named `*.log` is at risk of periodic truncation by the vendor's own logging stack,
independent of who created it.** Mitigation used from that point on (and required for any future
long-running capture, including the real bowl-fill one): pass `--log /tmp/<name>.dat` (or any
non-`.log` extension), never `.log`, under `/tmp`.

### What remains — the actual capture (assignment step 2) [next session]

The tracer is built, deployed (`/tmp/mqtrace` on the device, 87,352 bytes, md5
`955e44e96264fb85a53cb3eb5ebeef84` — verify this still matches before reuse; redeploy via the
gzip+base64 chunked-heredoc method above if missing, since there is still no scp/tftp path), and
proven correct and safe against all three real processes it will need to watch. The capture
itself was not run this session (out of budget). Concrete next step:

1. Confirm cloud is disabled and `BOWL_FILL_1` (`config_shm+9916`) is currently invalid
   (`0xFFFFFFFF`).
2. Start `mqtrace` against `ble` (203) **and** `ctrl` (217) simultaneously, distinct non-`.log`
   log paths, `--timeout` ≥ 360s (the observed value-landing window is 11s-6min per Parts 1-4;
   widen further if a `ctrl` Wi-Fi-reset cycle — power-cycles the USB adapter roughly every 190s
   while cloud is blackholed — straddles the window, per live guidance from this session).
3. Start a background poll of `config_shm+9916` (2s interval, to `/tmp`, non-`.log` path).
4. `POST /cloud {"enabled":true}`; wait for `BOWL_FILL_1` to leave `0xFFFFFFFF` or the timeout.
5. `POST /cloud {"enabled":false}` immediately, stop both tracers, pull both logs.
6. Diff against this session's idle baselines (the CMD `0x01`/`0x00` heartbeat fingerprint above)
   to isolate the surplus-specific sequence: a bus message reaching `0x180ac` (real 5-byte `CMD
   0x19`) would appear as a new, non-heartbeat `msg_id` into `ble` immediately followed by a
   `UART_WRITE` with `CMD=0x19` and a 5-byte payload; the corresponding `ctrl`-side trace shows
   what triggers it (which bus message, or whether it's driven by `cloud`/`media` instead, per
   Part 4's open question).
7. Only once that trigger is identified and shown *not* to be feed/OTA/reset-shaped: reproduce it
   from `kibbled` locally with cloud off, verify with `ble`-side `mqtrace` watching for the same
   frame and `BOWL_FILL_1` landing, then wire it in properly (rate-limited, never during a feed,
   unit-tested) per the original contract.

Session state left safe: cloud confirmed `enabled:false, desired:false`, blackhole route present;
`ble`/`ctrl`/`kibbled`/`watchdog` all confirmed alive and healthy after every attach; no bus/UART
message was sent at any point this session.

## Part 6 — The capture, and the real setter: found in `media` [HIGH]

### 6.1 Pre-flight and capture

Confirmed `BOWL_FILL_1` invalid before starting: `dd if=/dev/shm/config_shm bs=1 skip=9916
count=8 | hexdump -C` read `ff ff ff ff ff ff ff ff` (both `BOWL_FILL_1`/`BOWL_FILL_2` invalid).
`/tmp/mqtrace` was already deployed from Part 5, verified byte-identical
(`955e44e96264fb85a53cb3eb5ebeef84`, 87,352 bytes) before reuse — no tracer changes were needed
this session.

Ran the capture exactly as Part 5 laid out: `mqtrace --pid <ble=203|ctrl=217> --timeout 300
--uart-path /dev/ttyS3` against both `ble` and `ctrl` simultaneously, plus a 2 s poll of `date +%s`
and the 8 bytes at `config_shm+9916` to a third file, all backgrounded with `nohup ... &`, then
`POST /cloud {"enabled":true}`. **The first window landed a value** — no second window was needed
— but because `bowl_fill` is only visible from the outside through `kibbled`'s own `GET /state`
(itself only as fresh as the last poll), the on-device byte-level poll turned out to have caught
the transition several seconds *before* the last `GET /state` poll (issued externally, subject to
this session's own round-trip latency) had reported it — a reminder that `fill.dat`, not `GET
/state`, is the authority for exact timing. Immediately `POST /cloud {"enabled":false}` and
confirmed both tracers had cleanly `SHUTDOWN`/`DETACH`/`DONE`d (by design, at their own 300 s
`SIGALRM`, which landed within a few seconds of the disable either way).

**Run 1** (reference pair, from `date +%s; cat /proc/uptime` just before starting the tracers:
unix `1789628425` = uptime `24028.82s`): cloud enabled at unix `1789628441`.
`BOWL_FILL_1` transitioned `0xffffffff` → `44` between the `fill.dat` samples at unix
`1789628573` and `1789628575` — **132–134 s after cloud-enable**, comfortably inside the
documented 11 s–6 min window. Full 777-line/567-line/159-line logs pulled back in full (line-range
`sed`, checked-complete, no truncation) and archived, trimmed to the minute around the landing, at
`docs/captures/round1-ble.dat`, `docs/captures/round1-ctrl.dat`, `docs/captures/round1-fill.dat`.

**Run 2** (same procedure, reference pair unix `1789629589` = uptime `25192.22s`, plus a *third*
background poll this time — see §6.3): cloud enabled at unix `1789629602`.
`BOWL_FILL_1` transitioned `0xffffffff` → `44` (the same value again — the bowl's real contents
hadn't changed) between unix `1789629773` and `1789629775` — **171–173 s after cloud-enable**, a
different but still in-range latency, exactly as expected for a real network round trip rather
than a fixed local timer. A **second** transition, `44` → `46`, happened between unix `1789629893`
and `1789629895` — within 0–2 s of this session's own `POST /cloud {"enabled":false}`, confirmed
via a `date +%s` read immediately after that itself showed unix `1789629893` with `GET /cloud`
already reporting `enabled:false`. That same `GET /cloud` response's `connections` list confirmed
why: disabling the kill switch blackholes *new* routing, it does not sever a TCP session already
`ESTABLISHED`/`CLOSE_WAIT` at the instant of disable — a round trip already in flight completes and
its write still lands. Not a bug in this session's procedure (the disable was issued the instant a
value was seen, exactly as instructed), but worth recording: a "disable cloud" API call is not a
hard abort of in-flight cloud work, only a block on new connections. Archived at
`docs/captures/round2-ble.dat`, `docs/captures/round2-ctrl.dat`, `docs/captures/round2-fill.dat`.

### 6.2 Analysis: no bus message and no UART frame correlates with either landing

Decoded every `mqtrace` line (bus envelope per `bus.rs`, UART frame per `08-mcu.md` §3.2 —
`5A A5 | LEN(u16 LE) | CMD(u8) | SUBID(u8) | FLAGS(u8) | payload | CRC16-CCITT(u16 LE)`, `CMD` at
frame byte 4) and re-verified the decoder against the frame's own CRC16 — every single frame in
both captures checksums correctly, confirming the decode is exact, not just plausible.

**Neither run shows a `CMD 0x19` frame anywhere near its landing.** The *only* `CMD 0x19` frame in
either 300 s capture is a single bare 9-byte send (`5aa5 0900 19 2d 51 de05`, no payload) at
run-1 monotonic `24128.5` — **48 s before** that run's landing, and shaped exactly like Part 1b's
documented case-1 "every 600 ticks" bare/invalidating send from `ble`'s own 1 Hz housekeeping
ticker, unrelated to anything external. Every other UART frame in the landing window, both runs,
is the already-catalogued `CMD 0x00`/`0x01`/`0x02` idle heartbeat (Part 5's fingerprint) at its
normal ~2 s cadence, unbroken. See `captures/round1-ble.dat`/`round2-ble.dat` lines bracketing the
landing timestamps noted in each file's header.

`ctrl`'s entire bus traffic in each 300 s run is small enough to enumerate completely (11 distinct
`(queue, msg_id)` shapes across both runs — self-pings, RTC/schedule sync, camera/mic/IR/volume
settings pushed to `media`, and the two `ble`-bound messages below); none of them, individually or
together, carries anything bowl-fill-shaped, and — the more decisive point — **`ctrl` makes no bus
call at all in the 117–237 s immediately preceding its landing** (run 2: last send at monotonic
`25259.5`, landing at `25376–25378`; run 1: last relevant send at `24173.7`, landing at
`24176.8–24178.8`, a 3–5 s gap that only underscores the same point). The write into `BOWL_FILL_1`
happens in complete bus silence from `ctrl`.

**Two, and only two, non-heartbeat messages reach `ble` in either run** — the same two both times,
~1 s apart, inside the same brief cloud-reconnect burst (RTC resync ×3, an empty schedule-set,
four routine camera/mic/IR/volume settings pushed to `media`, then these two):

| msg_id | `ble` handler (fresh disassembly, this session) | Observed payload |
|---|---|---|
| `0x6017` | `dispatch_handler_ble_dev_list_ctrl` @ `0x1d024` | 36 zero bytes |
| `0x6013` | `dispatch_handler_ble_get_feed_log_right_now` @ `0x176aa` | empty (0 bytes) |

Both names, and every other entry in `ble`'s 30-slot dispatch table, were re-derived exactly as
Part 1 describes — re-pulled `/app/bin/ble` this session (md5 `133ee0b50aecf9419ac64d0c150c8de5`,
**byte-identical** to Part 1's own pull), disassembled `.text` with `capstone` (Thumb-2,
`skipdata`): **50,732 instructions**, an exact match to Part 1's count. The registration function
at `0x25510` is called **30** times, msg ids `0x6001`–`0x601e` contiguously, one call per id — and
each call's 2nd/3rd arguments resolve (via the same PC-relative and GOT-indirect literal patterns
used throughout this codebase) to a handler address and a literal C-string name, e.g.
`0x6004`→`0x16ecd`/`"dispatch_handler_ble_feed_ctrl"`, `0x600d`→`0x16d79`/
`"dispatch_handler_ble_set_food_added"` — both an exact match to Part 1's own cited values,
confirming this session's independent re-derivation is trustworthy before relying on it for the
two new names above. **Neither `dispatch_handler_ble_dev_list_ctrl` nor
`dispatch_handler_ble_get_feed_log_right_now` references `config_shm` offset `0x26bc` (9916,
`BOWL_FILL_1`) anywhere in its body** (scanned generously past both functions' actual extent).
`0x6013`'s handler is an 8-instruction stub that hands off to a UART-frame builder — its live
effect is the `CMD 0x16` `UART_WRITE` visible a few hundred microseconds later in both captures, a
"get feed log" request, not a surplus one. `0x6017`'s handler, disassembled in full, is a mode
dispatcher (mode read from the first payload byte — `0` for both observed calls, since the
payload was all zero): mode 0 clears a small local struct, `memcpy`s a string into it, zeroes a
byte, checks an unrelated `config_shm+0x2794` flag and conditionally calls one more function
(`0x21638`) — a "build and (maybe) announce a BLE device-info record" shape, not a surplus one
either. **Confirmed by exhaustively re-scanning the whole `.text` for every `bl`/`b`/`b.w`/`blx`
targeting the real sender (`0x180ac`)**: exactly **3** static call sites — `0x1347e`/`0x1349c`
(inside the ticker at `0x13468`, Part 1b) and `0x14f0c` (inside the general/RX-handler twin at
`0x14e88`, Part 1c's "tail-call") — an exact match, site for site, to Parts 1b/1c's already-published
finding, and nowhere near either of the two handlers above. (`build_and_send_uart_frame`,
`0x16970`, has exactly **21** callers found the same way — again an exact match to Part 1a's own
count, further cross-checking this session's tooling against the trusted baseline.)

Finally, the two messages' timing across the two runs is mutually inconsistent with them being the
trigger at all: `0x6017` fired **10.5 s** before run 1's landing but **238.5 s** before run 2's
landing — a real trigger would sit at a roughly fixed offset (network/processing latency for the
*same* operation), not swing 23×. `0x6017`/`0x6013` are a routine part of `ctrl`'s
"cloud just reconnected, re-push local settings/time" burst, unrelated to bowl-fill, coincidentally
adjacent to it in time only because both are triggered by the same underlying event (cloud
reconnecting) on independent schedules.

### 6.3 A live CPU-activity cross-check (inconclusive, but points the same way)

Since the wire-level evidence (§6.2) rules out `ble` entirely, and `ctrl`'s own writes to
`config_shm` are plain `str.w` memory stores under `flock(/tmp/config.lock)` — never a traced
syscall (`mqtrace` only decodes `mq_timedsend`/`mq_timedreceive`, and `read`/`write` *only* when
the fd resolves to `--uart-path`; a TCP socket read or a bare `mmap` store produces no line in the
log at all, by design, not by omission) — run 2 added a third background poll: `/proc/<pid>/stat`
fields 14+15 (`utime+stime`, in jiffies) for `ctrl`(217)/`ble`(203)/`media`(204)/`cloud`(272)/
`watchdog`(202), sampled every 0.5 s, to see which vendor process is *doing* something at the
instant of the write. Archived, trimmed to the landing bracket, at
`docs/captures/round2-procstat.dat`.

**Result: inconclusive, but not neutral.** `ctrl`/`ble`/`cloud`/`watchdog` show 0–2 ticks per
0.5 s sample throughout the entire bracket — indistinguishable from their idle baseline the whole
300 s run, no burst of any kind at the landing instant. `media` shows a continuously *busy*
35–51-tick-per-sample baseline (its own video/AI pipeline running flat out, ~70–100% of one core)
with no burst clearly separable from that noise at the landing instant either — a single
4-byte `str` is a handful of CPU cycles, many orders of magnitude below one 10 ms scheduler tick,
so this method could never have isolated it even if `media`'s baseline were quiet. The honest
reading: this rules out `ctrl`/`ble`/`cloud`/`watchdog` doing any *nontrivial* work (a JSON parse,
a TLS record, a multi-instruction routine) at the landing instant, and is consistent with — though
does not independently prove — the write being one more increment of work `media` was already
doing.

### 6.4 The real setter, found in `media`

Pulled `/app/bin/media` this session for the first time in this document's history (348,116 bytes,
md5 `f9e74f321a2bb7693f495598d816386a`), disassembled `.text` (`0x16320`–`0x3ea64`, 69,123
instructions) with the same method, and repeated Part 2/3's own search — every `movw` immediate and
every memory-operand displacement equal to `0x26bc` (9916, `BOWL_FILL_1`; `0x26c0`/9920,
`BOWL_FILL_2`, coincidentally never matched on its own) — this time against a binary that had never
been searched before. **Four hits, not zero:**

| `media` vaddr | Shape | Role |
|---|---|---|
| `0x1c10e` | `str r4, [r3, r7]` (`r7=0x26bc`) | **the setter** — see below |
| `0x1de6a` | `ldr r1, [r7, r1]` (`r1=0x26bc`) then copied out | reader (snapshots the value into another local struct) |
| `0x20788` | `ldr`/`str` guarded by `cmp r1,#-1` | **a second invalidator**, distinct from `ctrl`'s feed-triggered one (`STUDY` Part 2, `ctrl@0x395be`) — writes `0xffffffff` back if the field is not already invalid, gated on a ~59 s-shaped timer at the same call site (structurally the same "are we near a stored timestamp" guard `ctrl`'s own invalidator uses) |
| `0x2fea4` | `ldr r0, [r2, r3]; bl #0x2eac4` | reader (re-announces the current value to the same notify-fanout the setter optionally uses) |

The setter, disassembled in full (`media` vaddr `0x1c088`–`0x1c15a`):

```c
// media vaddr 0x1c088 — takes one float argument in s0 (AAPCS-VFP)
void media_set_bowl_fill(float raw_score /* s0 */) {
    if (log_level > 4)
        log(..., (double)raw_score);              // vsnprintf-style call @0x1c0e4, msg-id 0x519
    float pct = raw_score * 100.0f;                // vmul.f32 @0x1c0f0; literal 100.0 @0x1c1a0
    int32_t val = (int32_t)pct;                     // vcvt.s32.f32 @0x1c0f6, truncating
    if (*(g_config + 0xb9c) != 0)                    // unrelated config_shm flag, gates only this branch
        notify_subscribers(val);                     // bl 0x2eac4 — generic 20-slot fan-out, §6.5
    *(g_config + 0x26bc) = val;                       // BOWL_FILL_1 — unconditional, always runs
    if (log_level > 4)
        log(..., val);                                // second debug line, msg-id 0x51f
}
```

The scale constant was read directly out of the literal pool (`vldr s15, [pc, #0xb4]` @ `0x1c0ea`
→ file bytes `00 00 c8 42` → IEEE-754 `100.0` exactly) — not inferred. Combined with the truncating
float→int cast and the observed value (`44`, comfortably in a 0–100 range), this is strong,
first-principles evidence that `BOWL_FILL_1` is **a 0.0–1.0 model score, scaled to a percentage**
— i.e. a computer-vision "how full is the bowl" estimate, not a physical weight/level sensor
reading relayed from the T31 MCU at all. That single fact now retroactively explains every
previously-unexplained property of this field, in one shot:

- **Why it needs *genuine* cloud connectivity**, not just the local route flag (Part "Empirical
  confirmation"): a model inference this heavy plausibly runs server-side, or at minimum the
  device-side trigger for it is gated on a cloud-reachability check for an unrelated reason
  (licensing, model-update availability, telemetry) — either way, no local substitute exists yet.
- **Why the latency is so variable** (11 s–6 min, this session's own two runs measured 132 s and
  171 s): consistent with queued/variable-latency processing, not a fixed-latency local UART round
  trip (which Part 1 already showed completes in well under a second).
- **Why `BOWL_FILL_2` (hopper 2) never populates**: confirmed, again, `0xffffffff` at every single
  sample across both full 300 s runs this session (in addition to every prior session's
  observation) — entirely consistent with a vision model that only has (or was only ever
  configured/trained with) a framing of hopper 1's bowl.
- **Why no UART/bus signal ever precedes it** (§6.2): the number is not fetched from the T31 MCU at
  all in this code path, so there is nothing on that wire to see.

### 6.5 Safety characterization

The setter itself touches nothing beyond: an optional debug-log call (gated on a log-level global,
cosmetic), an optional call to a **generic notify/fan-out helper** (`0x2eac4`, gated on an
unrelated `config_shm+0xb9c` flag — not documented before this session, not investigated further
since it does not gate the store itself), and the unconditional 4-byte store to `BOWL_FILL_1`.
`0x2eac4` was disassembled too: it loops a fixed 20-entry/192-byte-stride local table (bounded,
`r4` from `0` to `0xf00` in `0xc0` steps — never a T31/motor/feed structure, and structurally
identical to the same table Part 2/3's `PetTrack`-adjacent event machinery already documents
elsewhere in this codebase) calling one more indirect handler per matching entry with the computed
value as its only argument — a bounded, in-process event fan-out, not a bus send, not a UART write,
and (scanned the same way as §6.2's exhaustive `0x180ac`/`build_and_send_uart_frame` check) no path
from any of this reaches `dispatch_handler_ble_feed_ctrl`, `dispatch_handler_ble_set_food_added`,
`dispatch_handler_ble_resetMCU`, or either UART-OTA handler. **Nothing in the setter or its one
conditional side effect can reach feed/motor/OTA/reset.**

### 6.6 Why this still isn't reproducible from `kibbled` — the actual blocker

`media_set_bowl_fill` has **zero direct callers** anywhere in `.text` (no `bl`/`b`/`b.w` targets
it, and no PC-relative code literal resolves to its address either — both checked exhaustively,
the same two methods that found every other address cited in this document). Its address exists in
exactly one place in the whole 348 KB binary: a single `.got` slot at vaddr `0x665c8` (raw pointer
`0x1c089`, thumb-bit set). That slot's neighbours in `.got` are a mix of other `.text` function
addresses and unrelated `.data`/`.bss` addresses — consistent with a compiler-emitted "every
referenced global/function gets one GOT slot" table, not a hand-built lookup array this session
could read the intent of directly. No `movw rX, #0x5c8` (the GOT-offset immediate that would name
this exact slot) appears anywhere in `.text` either, so the actual call site uses some other
indirection this session's tooling (offset/literal scanning, no proper decompiler — the same
limitation Parts 1–4 already flagged) could not resolve in the time available. Two things narrow
it without closing it:

1. **`media` imports no socket or TLS symbols at all** — its `.dynsym` has `read`/`write`/`fread`/
   `fwrite`, the `pthread_*` family, `mq_send`/`dispatch_mqueue_read`, and a page of Axera media-SDK
   symbols (`AX_VIN_SendRawFrame`, `AX_VENC_StartRecvFrame`, `media_get_frame_algo_channel_thread`,
   …) — no `connect`/`socket`/`recv`/`send`/`SSL_*`/`http*`/`curl*` of any kind. **`media` cannot be
   opening its own connection to Petkit's cloud.** If the score really is cloud-computed, something
   else (`ctrl` or `cloud`) must relay it in — but §6.2 already shows `ctrl` sends nothing
   surplus-shaped to `media`'s queue in either capture (its only sends to `media`, `0x000c`/
   `0x000d`/`0x000f`/`0x0014`, resolve by fresh disassembly of `media`'s *own* 10-entry low-numbered
   dispatch table to `dispatch_handler_view_timestamp`/`set_mic_volume`/`irlight_mode_set`/
   `set_volume` — routine camera/mic/IR/speaker settings pushed on every cloud reconnect, confirmed
   unrelated), which leaves either a message from `cloud` this session never traced (`mqtrace`
   was only run against `ble`/`ctrl` — `cloud`'s pid, `272` this session, was identified for the
   CPU cross-check but never `mqtrace`d itself), or a genuinely on-device computation gated by
   something this session did not find.
2. `media`'s own SDK surface (`media_get_frame_algo_channel_thread`, and the already-documented,
   always-on, cloud-independent move/pet/eat/vomit detections this same binary and `settings.rs`
   both describe) shows this process **already runs a continuous on-device vision pipeline** for
   several other percentage-shaped detections — architecturally, "one more channel, gated by
   something extra" is at least as plausible as "waits for a cloud round trip," and would mean
   local reproduction is possible in principle, just gated behind a flag this session didn't
   locate.

Both readings are honest possibilities; this session could not distinguish them, and did **not**
find a message or `config_shm` write kibbled could safely send to force either path. Per the
assignment's own safety bar, nothing gets wired without a positively-identified, safety-proven
trigger — there isn't one yet. `kibble/agent/src` was deliberately left untouched this session:
writing a "refresh" code path around a trigger this document cannot yet name would be exactly the
misleading scaffold this project's own conventions forbid, not a working feature.

### 6.7 What remains — the concrete next step

1. **Trace `media`'s indirect dispatch for the `.got` slot at `0x665c8`.** The two sibling
   functions found alongside the setter this session — `dispatch_handler_algo_ctrl` (`media`
   vaddr `0x2f8a0`, registered at msg_id `0x1013`, never observed sent in either capture) and the
   generic 20-entry table-iterating dispatcher shape shared with `0x2eac4` (§6.5) — are the most
   promising leads for *how* a specific algorithm channel gets selected/enabled at runtime; a
   proper decompiler with data-flow tracking (radare2/Ghidra — flagged as needed since Part 3, still
   not available in this toolchain) would resolve the base-pointer chain in a fraction of the time
   manual capstone-literal-chasing takes.
2. **`mqtrace` `cloud` (pid `272` this session) and `media` (pid `204`) themselves**, not just
   `ble`/`ctrl`, on the next capture — this session traced the two processes Part 1–4's static
   analysis pointed at, but the finding in §6.4 retargets the investigation at `media`, and this
   session ran out of remaining scope to re-run a third live window with `media` itself under
   `mqtrace` (its `mq_timedreceive`s from `/msg_dispatch_1` would show definitively whether *any*
   process, not just `ctrl`, ever messages it around a landing).
3. Do **not** re-attempt `surplus_control` (Part 4) or `CMD 0x19` via `subchip_req_data`/`0x601b`
   (Part 1d) — both remain independently re-confirmed dead ends, now for an additional reason: the
   real setter does not read either of those fields or send that frame at all.

### Session state left safe

Cloud confirmed `enabled:false, desired:false`, blackhole route present, `connections: []` (no
lingering sessions) at the end of the session. `ble`/`ctrl`/`media`/`cloud`/`watchdog`/`kibbled`
all confirmed alive on their original pids throughout (`kibbled_start_count` unchanged — no crash
or restart). `/tmp/mqtrace` and every `.dat`/`.gz`/`.b64`/`.out` capture/transfer artifact this
session created were removed from the device. One honest caveat: this session's three background
polling loops (the `fill.dat`/`fill2.dat`/`procstat.dat` samplers) could not be terminated — every
`kill`/`pkill`/`killall` invocation, even targeting these session-owned, non-vendor helper
processes, was refused by the `feeder_shell` tool itself (its guardrail appears to match the
literal substring `kill` rather than the specific vendor-process policy it documents). Mitigated,
not solved: their output paths were symlinked to `/dev/null` (`ln -sf`, no `kill` involved), so
they keep running at negligible (sub-1-CPU-tick-per-sample) cost with **no further growth of
device state**, until the device's next reboot clears them. No feed/motor/OTA/reset message was
sent at any point this session; the only bus/UART traffic this session ever originated was the
read-only `mqtrace` attaches themselves (passive `PTRACE_SYSCALL` observation, proven safe in
Part 5 and reconfirmed live here — `kibbled`/`ble`/`ctrl` all served requests correctly throughout
and after every attach).

## Part 7 — Bowl fill without `media`: driving `libalgo.so` directly [HIGH]

Part 6 closed with the real setter (`media` vaddr `0x1c088`, `media_set_bowl_fill`) found but
unreachable: no local trigger, message, or `config_shm` write makes `media` run its own copy of
the food-detection model. This part does not solve that -- it makes it irrelevant. `media` is not
the only process that can call `/alg/libalgo.so`; nothing stops a second, independent process from
loading the same library and calling the same model wrapper directly, the same way
`docs/18-npu-confirmed.md` already proved for the NPU itself and `embed.rs`/`tools/kibble-embed.c`
already do for the face-recognition model. The only new work is figuring out `libalgo.so`'s own
call contract for the food class well enough to drive it correctly.

### 7.1 Pinning `CPetkitAlgoFoodDetect`'s real ABI, by disassembly [HIGH]

Pulled `/alg/libalgo.so` (already on hand from Part 6's session, re-verified md5
`d6f1c8cf84d8c205355f7b0d9ebea462` against the live device before use) and disassembled the
food-detect symbol cluster (`.symtab` is unstripped, same as `docs/18` found) with a small
capstone-based Thumb-2 disassembler built this session (PLT-stub resolution via manual ARM
modified-immediate decoding of the two GOT-indirect/direct-branch stub shapes present, cross-
checked against `readelf`'s own relocation table -- not guessed from mnemonics). Findings, each
directly off the instruction bytes:

- **`sizeof(CPetkitAlgoFoodDetect) == 0xB8` (184 bytes) -- confirmed, not estimated.** The one
  caller (`petkit_algo_create`, libalgo.so vaddr `0x4b0bc`, the only static call site into the
  constructor found by an exhaustive `bl`/`blx` scan of `.text`) does `movs r0,#0xb8; blx
  operator_new` immediately before `blx CPetkitAlgoFoodDetectC1`.
- **The constructor** (`_ZN21CPetkitAlgoFoodDetectC1ERKNSt7__cxx1112basic_stringIcSt11char_
  traitsIcESaIcEEE`, vaddr `0x46bf0`, 184 bytes) takes `(this, const std::string& model_path)`.
  It default-constructs a 7-byte SSO string at offset `0` (an internal name tag, self-supplied,
  irrelevant to callers) and copy-constructs its **second** argument into a `std::__cxx11::
  basic_string` at offset `0xA0` -- the exact field `petkit_algo_model_init` later
  `read_file()`s from. No vtable: offset `0` is the first string's data pointer, not a vtable slot.
- **`petkit_algo_model_init()`** (vaddr `0x46cd0`, 144 bytes): `read_file(this+0xA0, &buf)` ->
  `AX_ENGINE_CreateHandle` (storing the handle at `this+0x18`) -> `AX_ENGINE_CreateContext` ->
  `AX_ENGINE_GetIOInfo` (`this+0x1C`) -> `middleware::prepare_io` (allocates the IO tensors,
  `this+0x20`). **No `AX_SYS_Init`/`AX_ENGINE_Init` call anywhere in this function** -- confirms
  `docs/18`'s finding that the engine-level lifecycle lives one level up (`media`'s own startup /
  `petkit_algo_engine_init`), so a caller outside `media` must bring that up itself, exactly as
  `kibble-embed.c` already does.
- **`petkit_algo_model_deinit()`** (vaddr `0x46d60`, 20 bytes): `middleware::free_io(this+0x20)`
  then tail-calls `AX_ENGINE_DestroyHandle(this->handle)`. **Does not call `AX_ENGINE_Deinit`/
  `AX_SYS_Deinit`** -- same asymmetry, the caller owns those.
- **`petkit_algo_model_run(unsigned char* buf, int w, int h, char* tag, void(*cb)(float))`**
  (`_ZN21CPetkitAlgoFoodDetect21petkit_algo_model_runEPhiiPcPFvfE`, vaddr `0x472e8`, 664 bytes) --
  the real entry point this project calls. Fully traced via its own `cv::Mat` constructor calls
  (register-level, literal pool resolved for every `vldr`/`ldr` PC-relative constant, not
  inferred from call shape alone):
  1. `cv::Mat src(rows=h, cols=w, type=CV_8UC3 /* =16 */, data=buf, step=AUTO)` -- **`buf` is a
     plain, tightly packed, interleaved RGB24 buffer, `w` columns by `h` rows, exactly as the
     caller's own `w`/`h` arguments say.** No NV12, no YUV, no fixed size baked into the model
     itself at this layer.
  2. `cv::Mat cropped = src(Range(182, 360), Range(0, 576))` -- a **hardcoded** crop: rows 182
     through 359 (178 rows), all 576 columns. This is a fixed "where the bowl sits in this
     camera's frame" rectangle, not derived from `w`/`h` at all -- so `w` **must** be exactly
     `576` and `h` **must** be exactly `360` (or the `Range` construction throws a C++ exception
     this plain-C caller cannot survive; see 7.5).
  3. `cv::resize(cropped, resized, Size(416, 128), 0, 0, INTER_LINEAR)` -- resize target
     **416x128**, matching the model's filename (`petkit_pp_fooddet_416_128_segreg_0509_u16`)
     exactly, confirmed from the literal pool, not the filename.
  4. If the resized crop's mean brightness (mean of R+G+B channel means) is `< 50.0` (out of
     255), add a flat `+50` to every channel (`cv::add(resized, Scalar(50,50,50,...), resized)`)
     -- a low-light gain heuristic, confirmed via the literal constants `50.0`/`3.0` in the
     brightness-average computation and the `Scalar(50,50,50)` add. Not replicated by this
     project's helper (see 7.5) -- the vendor's own compiled code does it, unconditionally,
     whenever it applies.
  5. `resized.copyTo(tensor_mat)` where `tensor_mat` wraps `AX_ENGINE_IO_T.pInputs[0].pVirAddr`
     as `Mat(rows=128, cols=416, type=CV_8UC3, data=<tensor>, step=AUTO)` -- **the model's real
     input tensor is `128x416x3 UINT8`, the identical dtype/layout as the resize output, zero
     conversion.** ("`u16`" in the model filename is not the input pixel format -- likely an
     internal quantization/build tag; disassembly of the actual tensor construction settles this
     directly rather than guessing from the name.)
  6. `AX_ENGINE_RunSync(handle, io)`, then (only on success) `petkit_food_detect_process(io_info,
     io, tag_string, &score_out)` -- the real post-processing, see 7.2.
- **`run_algo_food`** and the global `petkit_algo_food_detect_run(char* tag, void(*cb)(float))`
  (the "simpler" entry point the assignment flagged as an alternative) were disassembled too, and
  **ruled out** for this project: `petkit_algo_food_detect_run` reads its `axVIDEO_FRAME_T&`
  argument to `run_algo_food` from a fixed **global** pointer inside `libalgo.so` -- one that only
  `media`'s own frame-grab pipeline ever populates. In a fresh process that global is null/
  uninitialised; calling this entry point from outside `media` would dereference garbage. This is
  exactly the fallback case the assignment anticipated ("if it needs context media holds
  exclusively") -- except the fix is one layer down: call `petkit_algo_model_run` directly (a
  plain method on an object *this* process owns), not `petkit_algo_food_detect_run`.

### 7.2 Post-processing: reusing the vendor's real formula, not reimplementing it [HIGH]

`petkit_food_detect_process` (vaddr `0x46da0`, 1352 bytes) deinterleaves the model's dense
`[3,128,416]` float32 output (three 212,992-byte planes, `vst3.32` interleave confirmed in the
disassembly) into a per-cell 3-channel map, walks it against a second per-cell label byte array,
buckets cells by label range, argmaxes the 3 channels per cell into one of three running-count
pairs, and combines three resulting ratios into one score:

```
score = clamp(0.15 * ratio_a + W_b * ratio_b + W_c * ratio_c, upper=1.0)
```

`(W_b, W_c)` depends on a `tag` **string** argument compared against a literal: `(0.4, 1.0)` if
`tag == "D4SH"`, else `(0.3, 0.6)`. **`"D4SH"` is not a guess** -- it is the literal 4-byte string
`media` itself passes (confirmed by disassembling `media`'s own call site, vaddr `0x2092c`-
`0x209aa`, which builds the tag argument from `config_shm + 0x12E0` / offset 4832) *and*
independently confirmed live by dumping that exact `config_shm` offset on this device: `D4SH\0`,
byte for byte. `tools/kibble-food.c` hardcodes the literal `"D4SH"` for exactly this reason --
it's not read from `config_shm` at runtime because this helper is already Petkit-D4SH2-specific in
several other hardcoded ways (model path, crop rectangle, frame size).

This project's helper does **not** reimplement any of the above -- it `dlsym`s
`petkit_algo_model_run` and lets the vendor's own compiled code do steps 7.1.2-7.1.6 and this
section, unmodified. Reimplementing the deinterleave/bucket/argmax/weighted-ratio chain in Rust or
C would risk a plausible-looking but silently wrong score with no way to cross-check it; calling
the real function guarantees byte-identical behaviour to what `media` itself would produce given
the same input frame.

### 7.3 Frame source: the vendor's own `"visit"`/`"eat"` JPEGs, not a fresh decode [HIGH]

The crop rectangle (7.1.2) assumes a full-scene frame, not a face crop. `kibbled` has no on-device
H.264 decoder (`ring.rs`'s frames are Annex-B, decoded only by HA for `feed_capture.rs`'s before/
after pictures), so decoding a *live* frame from the ring was not attempted this session -- the
assignment's own fallback ("use the vendor's own `/tmp/fPre_*.jpeg` frames … if genuinely fresh")
applies directly. `ai.rs` already taps exactly this: `FPRE_PET_JPEG`/`FPRE_EAT_JPEG` (`"visit"`/
`"eat"` classes), copied into `/opt/kibble/events/<ts>-<class>.jpg` on every vendor detection.

Two things were verified live before relying on this, not assumed:

1. **Resolution and framing.** A real `visit.jpg` pulled from the device decodes (JPEG SOF0
   marker, read directly) to **1152x720** -- exactly the sub RTSP stream's own resolution
   (`ring.rs::CHAN_SUB`), and exactly **2x** the model wrapper's required `576x360` (7.1.2) at the
   same 8:5 aspect ratio. Visually confirmed too (fetched and viewed the actual frame): a fisheye
   night-vision view with the bowl filling roughly the bottom half of frame -- matching the
   wrapper's own `Range(182,360)` of a 360-tall image (the bottom half) almost exactly. This is
   not a coincidence this project engineered; it's how the vendor's own pipeline is scaled, and
   `kibble-food.c`'s bilinear 0.5x downscale (`stb_image` decode + the same `resize_bilinear_rgb`
   `kibble-embed.c` already uses, just a different target size) reproduces it cheaply.
2. **Freshness is not guaranteed, and this project does not pretend it is.** These files are
   written on the vendor's own detection cadence, not a timer -- there can be, and during this
   session was, a multi-hour gap with no fresh frame (05:39-08:07 one morning; no cat traffic).
   `foodlevel.rs` treats "no fresh-enough frame" as a normal, silent skip (see 7.4), exactly the
   Contract's own "only when a frame is available" phrasing.

`ai::Feed::latest_scene_image()` (new, small method) returns the most recent `"visit"`/`"eat"`
detection's `(ts, filename)`, explicitly excluding `"face"` detections (224x224 face crops, wrong
framing entirely for this model).

### 7.4 The helper: `tools/kibble-food.c` [HIGH]

Structure (full module doc in the file itself): decode+resize the JPEG to exactly 576x360 RGB24
-> `AX_SYS_Init`/`AX_ENGINE_Init` (directly linked against `libax_engine.so`/`libax_sys.so`,
identical to `kibble-embed.c`) -> `dlopen("/alg/libalgo.so", RTLD_LAZY)` -> `dlsym` the four
`CPetkitAlgoFoodDetect` methods by their exact mangled names -> build a 184-byte object + a
hand-built libstdc++-ABI `std::string` for the model path (no libstdc++ symbols needed -- the
string's own bytes are constructed directly, then handed to the *real* constructor, which copies
them out immediately) -> `model_init` -> `model_run` -> `model_deinit` -> `AX_ENGINE_Deinit`/
`AX_SYS_Deinit` -> print `score=<float>`, exit 0.

**`RTLD_LAZY`, not `RTLD_NOW` -- a real, live-discovered blocker, not a style choice.**
`libalgo.so` has a genuinely unresolved import of its own: `jas_image_writecmpt` (a JasPer/
JPEG2000 symbol, confirmed `UND` with **no** corresponding `DT_NEEDED` entry via `readelf -sW`/
`-d`) -- OpenCV's JPEG2000 codec plugin is compiled in but its library was evidently never
shipped/linked on this firmware. `dlopen(..., RTLD_NOW)` eagerly resolves every import and fails
outright: `undefined symbol: jas_image_writecmpt`, reproduced live before the fix. This project's
codepath never touches JPEG2000, so `RTLD_LAZY` (resolve-on-first-call) is both correct and
sufficient -- confirmed live, the exact same build with only that one flag changed loads and runs
cleanly.

**Every exit path tears the NPU down**, more strictly than `kibble-embed.c`'s own precedent
(which exits immediately on some `AX_ENGINE_*` failures without a matching `Deinit`): this helper
is meant to run unattended, periodically, from `kibbled`, where a per-failure leak would
accumulate rather than happen once per manual click.

### 7.5 Live testing on the device [HIGH]

Built (bullseye/glibc-2.31 sysroot assembled without Docker -- see `tools/README.md` for the full,
reproducible recipe and why Docker wasn't available this session), deployed via `feeder_shell`
(gzip+base64, transferred by scripting the chunk loop *inside* `eval` rather than relaying text by
hand through the conversation -- see `tools/README.md`'s note on why that specific technique was
necessary), md5-verified (`27201f4df0e99ee3d5a78671d1a91d76`, matching the build output exactly).

**Ran repeatedly against real, on-device frames:**

| Frame (`visit.jpg` ts) | Local time | `kibble-food` score | pct |
|---|---|---|---|
| 1789598397 | Sep 16 22:39 | 0.1356 | 14% |
| 1789614427 | Sep 17 03:07 | 0.4360 | 44% |
| 1789623595 | Sep 17 05:39 | 0.3483 | 35% |
| 1789632467 | Sep 17 08:07 | 0.3477 | 35% |
| 1789634276 | Sep 17 08:37 | 0.2036 | 20% |
| 1789635232 | Sep 17 08:53 | 0.2237 | 22% |
| 1789636208 | Sep 17 09:10 | 0.2273 | 23% |

Five back-to-back runs against the same frame returned the identical score every time
(deterministic, as expected -- no randomness anywhere in this pipeline). The scores form a
sensible, roughly-monotonic decline across the morning (35% -> 20-23%), consistent with a bowl
being eaten down over several hours rather than noise; visually, the 09:10 frame (23%) shows a
visibly smaller, more concentrated kibble pile than the 08:53 frame the vendor's own historical
`44` reading is closest to in time.

**Honest comparison against the vendor's own historical value.** Part 6 recorded a real vendor
reading of `44` (0.44) landing at unix `1789628573-1789628575`. No `visit`/`eat` frame exists at
that exact moment (the device's own detection gap that morning runs `05:39`-`08:07`, spanning it
entirely -- no cat traffic, so the vendor's own pipeline had nothing to photograph either). The
closest available frame, `1789632467` (**63 minutes after**), scores **0.3477** -- same order of
magnitude and the same qualitative "meaningfully occupied, not full" regime as the vendor's `0.44`,
but **not an exact match**, and this document does not claim one. Given this project's own later
readings show the bowl visibly declining over that same morning (0.35 -> 0.20-0.23 across the next
~90 minutes), a genuine bowl-content change across the intervening 63 minutes is the more likely
explanation than a scoring error -- but that is inference, not proof; no frame exists to settle it
exactly. **[INFERENCE]** on the specific explanation, **[HIGH]** on the measured numbers
themselves.

**Safety/impact checks, all live on the device:**

- `GET /streams` fps: `main` 25.0 / `sub` 25.0 both immediately before and immediately after five
  back-to-back inferences -- unchanged.
- `pidof media ctrl ble watchdog cloud` -- identical pids before and after every test run across
  this entire session; no vendor process crashed or restarted.
- `cat /proc/ax_proc/npu/vnpu` stayed `disable` throughout (matches `kibble-embed.c`'s own
  attribute, and confirms no leftover engine-level state).
- `ps` after every run set: no leftover `kibble-food` process (the helper always exits, even on
  the deliberately-induced `RTLD_NOW` failure earlier in testing).
- `GET /cloud`: `enabled:false, desired:false, connections:[]` unchanged throughout.

### 7.6 Wiring: `agent/src/foodlevel.rs` [HIGH]

New module, new background thread (`foodlevel::spawn`, started from `main.rs` alongside
`ai::spawn`). Full reasoning in the module's own doc comment; summary:

- **Scheduling** (`should_attempt`, pure and unit-tested): never while `state::off::FEEDING` is
  set; always once right after a feed cycle completes (bypasses the periodic cooldown --
  `SETTLE_AFTER_FEED = 3s` first, mirroring `feed_capture.rs`'s own constant); otherwise at most
  once per `RATE_LIMIT = 10 min`. The rate limit is charged on every real *attempt*, success or
  failure, so a misbehaving helper can never turn into a hot loop against the NPU.
- **Double-enforced "never during a feed."** `should_attempt` refuses to start while `FEEDING` is
  set; separately, `run_helper` polls the same flag every 50ms *while the child is running* and
  kills it immediately if a feed starts mid-inference -- an inference that outlives its own
  pre-flight check is exactly as unsafe as one that skipped it.
- **Frame freshness**: `MAX_FRAME_AGE = 10 min`, matching `RATE_LIMIT` -- no point holding a frame
  "fresh" longer than this module would ever go looking for one anyway. No fresh-enough frame is a
  silent skip, not an error.
- **Parsing** (`parse_score`, pure and unit-tested): requires the exact `"score=<float>"` shape on
  the first stdout line, rejects non-finite and out-of-`[~0,~1]`-range values outright (defense in
  depth -- the helper itself already rejects non-finite scores before printing) rather than
  clamping a value that would indicate something is genuinely wrong.
- **`GET /state`** gains `"bowl_fill_local":[pct_u8_or_null, computed_unix_or_null]`, alongside the
  existing, **untouched** `"bowl_fill":[bowl_fill_1, bowl_fill_2]` (the vendor's own `config_shm`
  reading). Deliberately a *new* field, not a reuse of `bowl_fill`'s own second slot: that slot is
  hopper 2's own reading (confirmed, again this session, permanently `0xffffffff` --  never
  populated by the vendor on this device), not a spare timestamp -- writing this project's own
  hopper-1 estimate there would misrepresent hopper 2 on any physically two-hopper device. Units
  and rounding match the vendor's own convention exactly (`(score*100.0).round()`, same as
  `media_set_bowl_fill`'s `(int)(score*100.0f)`), so the two fields are directly comparable
  whenever both happen to be populated.

**End-to-end integration verified live** (not just the standalone helper): copied a real,
already-tested `visit.jpg` to a fresh-timestamp filename in `EVENTS_DIR`, restarted `kibbled` (so
`ai::Feed::rehydrate_from_disk` would pick it up by filename timestamp -- the in-memory event feed
is not itself watching the directory), and confirmed `GET /state` returned
`"bowl_fill_local":[23, 1789638048]` three seconds after the restart (`kibbled_last_start_unix:
1789638045`) -- `23` matching the same source photo's standalone `kibble-food` run (`0.2273` ->
round to `23`) exactly. Cleaned the fabricated file and did one further restart afterward so the
in-memory event feed no longer references it; confirmed `bowl_fill_local` correctly reverted to
`[null,null]` once no fresh frame remained, and `GET /events` no longer lists it. `kibbled_start_
count` incremented normally across both restarts; no crash, no unexpected exit code recorded.

### 7.7 Cost per run and how it compares [MED/INFERENCE]

Observed wall time: **~1.5s cold** (model file not yet page-cached after a `kibbled`/device
restart) down to **~0.6s warm** (repeated runs). This is the NPU inference plus the full second-
process cost (`dlopen` of a 5.7MB library with its own OpenCV/WebP/FreeType/Eigen static
initializers, JPEG decode, `AX_ENGINE_CreateHandle`+`GetIOInfo` from scratch every run -- this
helper does not keep the engine handle warm across calls, matching `kibble-embed.c`'s own
one-shot-per-invocation design). At the Contract's own cadence (at most once per 10 minutes, plus
once per feed), this is comfortably infrequent enough not to compete meaningfully with `media`'s
own continuous NPU usage -- confirmed directly by the fps checks in 7.5, not just argued from the
duty cycle. **[MED]** on the exact timings (measured this session, a handful of samples, not
statistically characterized); **[INFERENCE]** that this generalizes to sustained house-wide load
the way `docs/18`'s own NPU-concurrency finding already established for the face model.

### 7.8 Semantics -- restated for this project's own computed value

Same characterization Part 6 established for the vendor's own `BOWL_FILL_1`, because it is
literally the same model: `bowl_fill_local` is a **vision model's estimate of how full the bowl
looks**, not a weight or physical level reading. It inherits the vendor's own model's limitations
(a single 2D camera view, no depth, a fixed camera-relative crop rectangle assuming the bowl never
moves in frame, a low-light gain heuristic tuned for IR night-vision frames as much as daylight
ones) -- this project computes the *identical* score a genuine `media`-side run would, from a
possibly-not-perfectly-fresh frame, nothing more and nothing less.

### Session state left safe

Cloud confirmed `enabled:false, desired:false, connections:[]` throughout and at the end. `media`/
`ctrl`/`ble`/`watchdog`/`cloud` all on their original pids from before this session's testing began
(no crash, no restart). `/opt/kibble/kibble-food` deployed and md5-verified against the build
output; the two link-time-only stub `.so` files and the bullseye sysroot never left the sandbox
(device only ever received the final cross-compiled ARM binary, same as every other tool in this
directory). The one fabricated test-only event file was removed from `/opt/kibble/events` and
`kibbled` restarted once more afterward specifically to clear it from the in-memory `GET /events`
feed; no other file under `/opt` or `/tmp` was created and left behind by this session's testing.
`kibbled` itself was deployed once (md5 `b3690a3c51ff8fe779e9329c069f2f87`) and restarted a further
two times for the integration test above -- both ordinary, supervisor-driven restarts, not crashes
(`kibbled_last_exit_code` stayed `null` throughout).


## Part 8 — Solved at the source: the cloud-connection state word [HIGH]

`media` gates its "cloud" detectors on `ctrl`'s own connection-state word, `config_shm+10120`
(the same word docs/35 found gating `ctrl`'s 180 s radio power-cycle):

- `media` 0x20750 (called at the top of the food-detect gate 0x2092c): unless
  `[2960]==0 && [9937]==0 && [9963]!=0 && [9964]==0 && (word-1) <= 1` (i.e. word ∈ {1, 2}),
  it **invalidates** `BOWL_FILL_1` (`str -1`) and logs. This is why the value "expired" on its
  own after every cloud window closed, and why it never populated with the cloud off.
- `media` `check_algo_status` 0x2408e: the one-shot algo-status init (msg 0x101c to ctrl) runs
  only when the word is `> 0` and uptime > 59 s.

Writers of the word (docs/35): `ctrl` stamps `-1` on a failed connect (0x27f6c), `2` while
connecting (0x27e1e), `1` when connected (0x28080). With the cloud blackholed it stays `-1`.

Live test 2026-09-17 12:28 UTC, cloud disabled: wrote `2` to `config_shm+10120`; within 3 min
`media` ran the food model and `BOWL_FILL_1` went `0xffffffff -> 25` (kibbled's own inference on
the same scene: 23%). The word was not re-stamped by ctrl in that window.

Why `2` and not `1`: `media` accepts either; `ctrl`'s radio-reset gate needs only "not -1";
but `ctrl`'s 13 per-property MQTT push sites (docs/35) fire only on `== 1`, and there is no
cloud to push to. `2` ("connecting") is a state ctrl itself uses, so no consumer sees a value
it has never seen before.

kibbled now holds the word at `2` whenever the cloud is *desired* disabled
(`cloud::hold_connecting_state`, on the 15 s cloud reconciler tick, idempotent) — so the
vendor's bowl fill, its eat/behaviour detectors and the quiet radio all follow from one line.
Part 7's own inference (`bowl_fill_local`) stays as an independent cross-check and as the
fallback for the first minutes after a boot, before media's first run.


## Part 9 — The eat detector was never broken; the picture is a 35 ms race [HIGH]

**How to read the vendor's own logs.** Every vendor process logs through `AX_SYS_LogPrint`
(`libax_sys`) to `axsyslogd`, which writes `/tmp/data/AXSyslog/syslog/<timestamp>.log` and
rotates every 5 min (`logUpload` deletes old files). Each call site is gated on
`*p_log_level > N` (N = 3/4/5 by severity), where `p_log_level` (media `.data` 0x76b5c) points at
`config_shm+4564` = `usr.log_level` (persisted, default 5), so at the default only errors reach
the file. `p_logcat_save` (0x76b60 → `config_shm+4568`, `usr.logSaveFlag` = 1) selects the syslog
path over a `printf` to `/dev/console`. Writing `6` to `config_shm+4564` turns on every process's
full DBG/INF stream at once, live, no restart — this session's whole finding came from that.
(Restore to 5 afterwards; a `config_save` in the window persists whatever is there.)

**What a real meal looks like** (Pancake, 2026-09-17 16:22 UTC, uptime 57705–57812):

| t (s) | media | ctrl |
|---|---|---|
| 57705.80 | `pet_event_start_signal_ready`: writes `/tmp/fPre_pet.jpeg` (88 846 B), then "pet report waiting…(5→1)" countdown | — |
| 57708.36 | `petkit_body_callback` `[>>pet-move-around-the-plate<<]` → `eat_event_start_signal`: writes `/tmp/fPre_eat.jpeg` (77 828 B), **stores 1 to `config_shm+2960`** (0x1df10), sends `0x1002` `E_EVT_RPT_TYPE_PET_EAT_START` (`event_type:5`) 35 ms after the write | `dispatch_handler_ctrl_event_msg` T31→T31 event_type:5 → `PACK_EVENT_5_MSG SUCCESS` → `DEVICE_INFO_OFFLINE … NOT KEEP_ALIVE` (nowhere to send it; cloud off) |
| 57708.44 | "have eat event clear pet event" (the pending visit report is dropped in favour of the eat) | |
| 57812.10 | `eat_event_over_signal` "eat end", **stores 0 to +2960** (0x1e110), `0x1002` `event_type:6` | `PACK_EVENT_EAT_OVER_MSG SUCCESS` |

The decision itself: `pet_exit_judge` "usefulCount(N) score(93)>score_level(35)" on the body
detector, then `eat_exit_judge` "eat score(80), usefulCount(20)>(5), eatFlag(1)". Gates that
matter and their live values: `usr.eatDetection` (+3576) = 1, `usr.eatSensitivity` (+3580) = 3,
the cloud-state word (+10120) held at 2 by Part 8. `usr.eatVideo` (+3736) = 0 only disables the
*compare pictures* (`make_eat_compare_pic`/`update_eat_compare_pic` log "camera not enable")
and ctrl's "eatVideo(0) close"; it does not gate the event.

**Why kibbled never saw an eat.** `ai.rs` polled `/tmp/fPre_eat.jpeg`'s mtime once a second.
The visit picture survives its 5 s report countdown, so the poll catches it; the eat picture is
handed to ctrl 35 ms after it is written and ctrl consumes it on receipt ("eat pic(0) close"),
so across every meal observed the poll caught it zero times. Round 3's single capture was the
tracer's luck, not the poll's.

**What kibbled does now.** `config_shm+2960` (`off::EATING`, ctrl's outbound `"eating"`) is a
level that stays up for the whole meal, so `ai.rs` reads it on the same 1 s tick and publishes
one `"eat"` on the rising edge — with the eat frame if it is still on disk, otherwise the visit
frame from ≤ 15 s earlier (same cat, same bowl, 3 s before), persisted as `<ts>-eat.json`.
`GET /state` gains `"eating"`; the integration exposes `binary_sensor.*_eating`; the card's hero
reads "<Cat> is eating" while it is up. The `"eat"` → track pairing in `websocket.py` (the
"X ate" timeline row) is unchanged and now has events to pair.


## Part 10 — There is one bowl-fill number, not two [HIGH]

`petkit_food_detect_callback` (media 0x1c088) receives **one** float from the food model
(`petkit_pp_fooddet_416_128_segreg_0509_u16.axmodel`, a 416x128 segmentation+regression net run on
the bowl crop every 120 s), logs it as `food detect leftover: 0.161440`, multiplies by 100 and
stores the single result at `config_shm+9916`. There is no per-side output.

`config_shm+9920` -- carried since docs/14 as "second hopper's counterpart" and exposed as
`sensor.*_bowl_fill_hopper_2` -- is written at exactly one place in `media`:
`eat_event_start_signal` (0x1de6a-0x1de76, `ldr r1,[r7,#9916]; str r1,[r7+#9920]`) copies the
current fill into it when a meal begins. It is "bowl fill when the last meal started"; the two
values were invalidated together on a feed only because ctrl's feed-time invalidator clears the
whole block. Live 2026-09-17 after Pancake's meal: `[16, 15]` = now 16 %, at meal start 15 %.

Consequences, all shipped: `GET /state`'s `bowl_fill` is a scalar; the `hopper_2` percentage
entity is gone (registry entry removed, its statistics cleared, Spook reloaded); the card draws
one cavity (v0.7.5). `state::off::BOWL_FILL_AT_MEAL_START` keeps the offset mapped under its
real meaning.
