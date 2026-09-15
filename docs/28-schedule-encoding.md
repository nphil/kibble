# STUDY-schedule-encoding.md — Per-Entry Schedule Time Encoding, Resolved

**Date:** 2026-09-15
**Method:** Live, read-only capture against the running device (192.168.4.85) — full `/dev/shm/config_shm`
dump, full heap dump of the running `ctrl` process (PID 212), and a fresh pull + independent Thumb-2
disassembly (capstone 5.0.7, `pyelftools`) of the currently-deployed `/app/bin/ctrl`. No process was
killed, restarted, or sent any write; no `pktool set_*` was run; the device's schedule was never
touched. All device access was over telnet (short-lived one-shot sessions via `pklive.py`); zero HTTP
requests were made to the device's own HTTP server. Cross-checked against this repo's own
`16-schedule.md` (`STUDY-schedule.md`), `07-config.md`, `08-mcu.md`, and `agent/src/schedule.rs`.

**Trigger:** Nitin created a real feeding plan in the Petkit app minutes before this study — one entry,
17:25 local (`America/New_York`, EDT = UTC−4) every day, 2 portions split 1/1 across the two hoppers.
This gave a live, known-good specimen to search for instead of guessing.

**Provenance (for reproducibility):**

| Artifact | Size | MD5 |
|---|---|---|
| `/app/bin/ctrl` (pulled fresh this session) | 742,148 bytes | `c645c0665da2cf73db93ffa8d9d0ea68` |
| `/dev/shm/config_shm` (full dump) | 11,952 bytes | `dd54dbe975df12166b338d17e62f7db9` |
| `ctrl` heap, PID 212, `[heap]` VMA `0x000d5000`–`0x00260000` (full dump via `/proc/212/mem`) | 1,617,920 bytes | `2e14cd5a080c3995b2f9e640694c1204` |

---

## TL;DR

| Question | Answer |
|---|---|
| Weekday bitmask (`0x7F`) on the wire? | **No.** [HIGH] Recurrence is a cloud-side JSON string (`"re":"1,2,3,4,5,6,7"`), never part of the 22-byte wire struct. Directly refutes the pre-search hypothesis. |
| Amount encoding | **Raw byte, no scaling on this device.** [HIGH] `amount_l=1`, `amount_r=1` on the wire, byte-for-byte equal to the app's own "1 portion per hopper" — confirmed by both live capture and by reading the actual divisor-flag bytes in `config_shm` (both `0`, i.e. disabled). |
| `id[16]` content | **A generated string encoding date + local time-of-day**, e.g. `"s_20260916_62700"` (16 chars exactly, no in-field NUL). [HIGH] |
| Time-of-day units/zone | **Local seconds-since-midnight** (62700 = 17:25:00 EDT). Confirmed present; the UTC equivalent (77100) was searched for and never found. [HIGH] |
| The `time` **wire** field for this entry | **Zero.** [HIGH, one inferential link — see §6] The real JSON's `t` for the entry that ships to `ble`/MCU is a *positive* seconds-until-next-occurrence value (86341); `ctrl`'s own encoder (independently re-disassembled, byte-identical to `16-schedule.md` §3.4) zeroes any non-negative `t` before it reaches the wire. |
| Is the earlier failed live test now explained? | **Yes**, with a different mechanism than originally suspected — see §7. No "feed list enable" flag was found anywhere (exhaustive search, §5); the more likely explanation is that the wire `time` field never carries a usable countdown in real operation at all. |
| `enable` per-entry flag | **Confirmed absent** from the real JSON too (not just struct-capacity inference). [HIGH] |
| Does `config_shm` cache the schedule? | **No, at all.** [HIGH, exhaustive] Every encoding candidate and a full ASCII/binary scan of the complete 11,952-byte struct found nothing schedule-shaped. |

---

## 1. Where the specimen was found

`16-schedule.md` §3.2 traces the write path as `ctrl`'s `pk_schmg_parse_schedule` (`schedule_ctrl.c`,
entry `ctrl 0x4799c`) parsing a `"latest"` JSON array pulled from the cloud's `property/set` "feed" key,
then building a `malloc(22*N+2)`-sized buffer sent as bus msg `0x6005`. Two possible ground-truth
locations for a live specimen were considered:

1. **`config_shm`** — checked exhaustively (§5). Nothing found.
2. **`ctrl`'s own heap** — the malloc'd wire buffer is freed right after sending, but `free()` does not
   zero memory, and more usefully, **the JSON text `ctrl` parsed to build that buffer is a separate,
   independent allocation that survives much longer** (nothing overwrites short-lived string buffers
   as eagerly as the tight, frequently-reused small `struct schedule_entry` allocations). Searching the
   heap for literal JSON key text (`"latest"`, `"id"`, `"a1"`, `"a2"`, `"t":`) instead of guessing binary
   time encodings is what actually worked.

Full heap dump: `dd if=/proc/212/mem bs=4096 skip=213 count=395 | base64` (213×4096 = `0xd5000`, the
heap's own start address in `/proc/212/maps`; 395×4096 = `0x18b000`, its exact length — both page-aligned,
matching the VMA exactly). Decoded locally; `grep`-for-ASCII found four hits, one populated, three
showing the empty/idle state:

**The populated capture** (heap offset 664448, i.e. `ctrl` heap vaddr `0xd5000+0xa22c0` = `0x177380`;
the leading `{"result":{"schedu` fell in a part of the buffer a later allocation had since overwritten —
only the tail from `le":[...` onward survived intact; reconstructed against the three *intact* empty
captures below, which independently prove the missing prefix is `{"result":{"schedule":`):

```
le":[{"it":[{"a1":1,"a2":1,"id":"n_62700","t":62700}],"re":"1,2,3,4,5,6,7"}],"nextTick":86341,"latest":[{"a1":1,"a2":1,"id":"s_20260916_62700","t":86341}]}
```

**One of three fully-intact empty-state captures** (heap offset 728309), proving the true key name and
full envelope shape byte-for-byte:

```
{"result":{"schedule":[{"re":"1,2,3,4,5,6,7","it":[],"itemJsonString":"[]"}],"nextTick":86340,"latest":[]}}
```

This text immediately follows an HTTP response header ending `Transport-Security: max-age=31536000;
includeSubDomains\r\n\r\n` in the same heap region — i.e. this is the body of an HTTPS response `ctrl`
received (via its linked `libcurl`/`libssl`, confirmed present in `/proc/212/maps`), not an MQTT push.
`ctrl`'s own `.rodata` independently confirms it owns this exact flow: it contains the literal strings
`net_dev_get_feed_schedule`, `feed_over_get_feed_schedule` (fires right after a feed completes —
consistent with re-syncing "when's the next one" after every dispense), and an entire debug-log cluster
at file offset `0x9c2cc`–`0x9cb87` (`pk_schmg_parse_schedule`, `schedule_ctrl.c`, `"get schedule latest
start %d"`, `"get schedule latest item_num <= 0"`, `"get schedule latest item[%d] is NULL"`, `"get
schedule schdl start"` for the `schedule`/`it` fallback shape) that matches this JSON's own key names
exactly. **Confidence: HIGH** — four independent captures, one intact end-to-end, cross-validated
against `ctrl`'s own embedded strings.

---

## 2. Field-by-field confirmation against the known-good specimen

Nitin's stated plan: 17:25 local, daily, 1 portion from each hopper.

| Field | Real captured value | Matches Nitin's plan? | Confidence |
|---|---|---|---|
| `a1` / `a2` | `1`, `1` | **Yes, exactly.** | HIGH |
| per-entry `id` (in `"latest"`) | `"s_20260916_62700"` | `62700` = `17*3600+25*60` = 17:25:00 **local** seconds-of-day; `20260916` = the calendar date of the *next* occurrence (tomorrow, Sept 16 — see §4 for why) | HIGH |
| per-entry `id` (in `"schedule"."it"`, the stable recurring template) | `"n_62700"` | Same `62700` local-seconds-of-day, this time as a **non-decaying** value (see §3) | HIGH |
| `"re"` (recurrence) | `"1,2,3,4,5,6,7"` | Comma-separated weekday-number list, all seven present = "every day" | HIGH |
| `"t"` (in `"schedule"."it"`) | `62700` | The stable local time-of-day itself | HIGH |
| `"t"` (in `"latest"`) | `86341` | A **decaying countdown**, not a clock time — see §3 | HIGH |
| `enable` | **absent** | No such key anywhere in either object | HIGH |

**UTC vs. local, settled directly:** 17:25 EDT = 21:25 UTC = `21*3600+25*60` = `77100` seconds-of-day.
That value was searched for across the full heap and config dump and **never appears anywhere**; `62700`
(the local value) appears three times. **The device's own cloud-facing schedule representation is in
local time, not UTC.** [HIGH]

**Why not a 15-minute grid:** `62700` is not a multiple of any round sub-hour boundary in a way that
would suggest quantization — it is exactly `17*3600 + 25*60 + 0`, i.e. plain HH:MM:00 converted to
seconds. No evidence of any coarser grid.

---

## 3. `"latest"` vs. `"schedule"."it"` — two representations, only one goes on the wire

Disassembling `ctrl 0x47bec`–`0x47bf4` (`ldr.w r1,[pc,#0xa28]; ldr r0,[sp,#0x30]; add r1,pc; bl
0x8bde0`) and resolving the PIC literal by hand: the literal pool at `ctrl 0x48618` holds delta
`0x648a5`; `0x648a5 + (0x47bf2+4) = 0xac49b`, which is exactly `ctrl`'s own string `"latest"` at file
offset `0x9c49b` (`vaddr = file_off + 0x10000`, this repo's standing convention). **Independently
re-confirms** `16-schedule.md`'s claim that `ctrl 0x47bf4` is `get_object_item(root, "latest")` — this
is not merely "the doc's own account", it is now hand-verified against a fresh binary pull.

Immediately after (`0x47bfc: cmp r0,#0; bne #0x47cb6`): **if `"latest"` is present, `ctrl` jumps
straight to using it and never touches `"schedule"`/`"it"`/`"re"` for this call.** The `"schdl"`-tagged
debug strings (§1) belong to the **fallback** path taken only when `"latest"` is *absent* — i.e.
`"schedule"."it"` is the stable, human-authored recurring template, while `"latest"` is a derived,
single-occurrence convenience value `ctrl` actually consumes when present (which it was, in our
capture). **This directly explains why `"latest"`'s `t` (86341) does not equal `"it"`'s `t` (62700):**
`86341 = 86400 − 62759 + 62700`, i.e. `"latest"` was computed at `ctrl` fetch-time `62759`s-of-day
(17:25:59 — 59 seconds after today's 17:25 had already passed), so "next occurrence" rolled to
*tomorrow*, `62700` seconds after midnight — matching the `20260916` (tomorrow's date) embedded in the
`"latest"` entry's own `id`. This is a fully self-consistent reconstruction, not a coincidence.
**Confidence: HIGH** (arithmetic identity, cross-validated against the `id` string's own embedded date).

**Consequence:** the wire-facing code path (whichever object it draws from) always operates on a `t`
that is a plain **non-negative** integer in ordinary operation — a stable local time-of-day in the
`"schedule"."it"` fallback shape, or a decaying countdown in the `"latest"` shape actually used here.
Both are positive. See §6 for what that means once it reaches the zeroing logic.

---

## 4. Amount encoding — confirmed byte-exact, divisor confirmed disabled

`16-schedule.md` §3.3 found `amount_l`/`amount_r` are read from cJSON `"a"` (or `"a1"` fallback) /
`"a2"`, "optionally divided by a per-field config value... gated by a flag at config-struct offset
`0xeb0`/`0xeb4`". Re-disassembled this myself (`ctrl 0x47fc0`–`0x480d8`, capstone, fresh pull):

```
0x47fe6: ldr.w r1, [r3, #0xeb0]     ; r1 = g_config->…(offset 0xeb0), amount_l's divisor
0x47fea: cmp   r1, #0
0x47fec: ble   #0x47ff2             ; divisor <= 0 -> skip the divide entirely
0x47fee: blx   #0x155c8             ; __aeabi_idiv(r0, r1) -- only reached if divisor > 0
0x47ff2: strb  r0, [r6, #0x10]      ; amount_l byte, wire offset +0x10
...
0x480aa: ldr.w r1, [r3, #0xeb0]     ; identical gate, "a1" fallback path
...
0x480cc: ldr.w r1, [r3, #0xeb4]     ; amount_r's own divisor, offset 0xeb4
0x480d8: strb  r0, [r6, #0x11]      ; amount_r byte, wire offset +0x11
```

Both `"a"`/`"a1"` and the `"a2"` path gate on the **same two config-struct offsets** regardless of which
JSON key supplied the value. Reading those exact bytes out of the live `config_shm` dump:

```
config_shm[0xeb0..0xeb4] = 00 00 00 00   (i32 = 0)
config_shm[0xeb4..0xeb8] = 00 00 00 00   (i32 = 0)
```

**Both divisors are `0` (`<= 0`) on this real device → the divide is skipped both times.** The wire
byte is the raw JSON integer, unmodified. Our captured JSON has `"a1":1,"a2":1` → **wire
`amount_l = 0x01`, `amount_r = 0x01`, byte-for-byte identical to the app's own "1 portion per hopper".**
**Confidence: HIGH** — disassembly of the gate *and* a live read of the actual gate value, on the actual
device, agree.

(Whether "1" means "1 gram" or "1 portion-unit" at the MCU is a separate, undetermined question — the
existing `feed_ctrl`/CMD `0x0A` path uses literal grams, but this divisor's very existence suggests the
schedule path may use a different, coarser unit on some SKUs/regions where the flag is enabled. Not
resolved here; irrelevant to the wire *byte value*, which is unambiguously `1`.)

---

## 5. `id[16]` — exact shape, and a real edge case Kibble's own code already handles correctly

`"s_20260916_62700"` is **exactly 16 ASCII characters** — ctrl's own field is `id[16]`
(`memset` 16 bytes, then `memcpy` up to `strlen`, max 16, `strlen > 0x10` is a hard error). At exactly
16, the field is **completely filled with no in-field NUL terminator**:

```
73 5f 32 30 32 36 30 39 31 36 5f 36 32 37 30 30    "s_20260916_62700" (16 bytes, no terminator)
```

`agent/src/schedule.rs`'s `WireEntry::decode()` already handles this correctly —
`b[0..16].iter().position(|&c| c == 0).unwrap_or(16)` falls back to treating the full 16 bytes as the
string when no NUL is found. **This is now a proven, not just defensively-coded, real case.**

The `s_`/`n_` prefixes are presumably a "single next occurrence" vs. "named/normal item" distinction on
the vendor's side; not otherwise confirmed and not load-bearing for Kibble's own `id` scheme (Kibble is
free to use its own convention, e.g. the `entry_id` random-hex scheme already sketched in
`design-entities.md` §4.2 — nothing about the wire format requires mirroring the vendor's string
convention).

---

## 6. The `time` field: independently re-confirmed, and now resolved with a live specimen

Re-disassembled `16-schedule.md` §3.4's cited snippet myself, from a **freshly pulled** binary (not
trusting the prior study's own copy):

```
ctrl 0x382a8 (file offset) bytes: 43 69 00 2b a9 bf 00 23 c6 f8 12 30 73 82 b3 82   <- byte-for-byte match
```

capstone (Thumb mode) decode of those exact bytes:

```
0x482a8: ldr   r3, [r0, #0x14]     ; r3 = t_node->valueint  (r0 is the raw get_object_item(entry,"t")
                                    ;   result, handed straight in from ctrl 0x4803e's `bne 0x482a8` --
                                    ;   no transform of any kind between "find the t key" and this read)
0x482aa: cmp   r3, #0
0x482ac: itett ge
0x482ae: movge r3, #0              ; t >= 0  ->  r3 := 0
0x482b0: strlt.w r3, [r6, #0x12]   ; t <  0  ->  store raw r3 as a 32-bit word
0x482b4: strhge r3, [r6, #0x12]    ; t >= 0  ->  store r3(=0) as the low halfword
0x482b6: strhge r3, [r6, #0x14]    ; t >= 0  ->  store r3(=0) as the high halfword (together, zero all 4 bytes)
```

**Identical mnemonics, identical interpretation, to `16-schedule.md` §3.4 — independently reproduced
from scratch against a fresh pull.** This confirms the earlier study was not misreading its own
evidence: **a non-negative `t` is unconditionally discarded (wire `time = 0`); only a negative `t`
survives, stored verbatim.**

**The one inferential link this document adds, and flags plainly:** I did not literally capture the
raw bytes of a `msg_id 0x6005` payload in flight on the bus at the moment of a write — doing so would
need active bus tracing at the instant of a schedule edit, which did not happen during this read-only
session (Nitin's edit had already landed before I started looking). What I *do* have is: (a) `ctrl`'s
own confirmed encoder logic (above), and (b) a real, live JSON object — reached via a confirmed
`get_object_item(root,"latest")` call site — whose `"t"` is `86341`, a plain positive decimal integer
with no minus sign anywhere in the source text (cJSON's own parser has no way to introduce a sign that
isn't in the text). Chaining (a) and (b): **for this real entry, the wire `time` field is `0x00000000`.**
This chain has no known gap, but it is a chain, not a single direct observation — hence "HIGH, one
inferential link" rather than "proven by wire capture" in the TL;DR.

**Full predicted wire payload for `msg_id 0x6005`, this entry** (count=1, one 22-byte entry):

```
01 00                                              count=1, reserved=0
73 5f 32 30 32 36 30 39 31 36 5f 36 32 37 30 30    id[16] = "s_20260916_62700"
01                                                  amount_l = 1
01                                                  amount_r = 1
00 00 00 00                                        time = 0 (t=86341 was non-negative -> zeroed)
```
24 bytes total. (If the fallback `"schedule"."it"` shape were used instead — `t=62700`, also
non-negative — the outcome is identical: `time = 0`. **The conclusion does not depend on which of the
two JSON shapes actually feeds the encoder**, since both carry a positive `t` in any realistic
schedule.)

---

## 7. Does this explain the earlier failed live test? Yes — a different mechanism than suspected

The suspected "feed list enable" flag was **not found**: exhaustively searched the complete
`config_shm` dump (every candidate integer/string encoding, plus a raw ASCII/binary scan for
`"enable"`-adjacent fields near the schedule region) and it is **confirmed absent** from the real
per-entry JSON as well (§2). That hypothesis is not supported by any evidence gathered this session or
in `07-config.md`'s own 228-entry field dictionary (no `feed`/`schedule`-adjacent `enable` name exists
in it at all).

The evidence *does* point at a working explanation: **the wire `time` field never carries a usable
"fire in N seconds" value in real operation.** Any hand-computed positive countdown sent directly to the
wire (bypassing `ctrl`'s JSON layer, which is exactly what a manual/agent-driven test of `msg_id 0x6005`
would do) is not something the vendor's own software ever actually sends — the real pipeline's own
positive countdown gets zeroed **before** it would ever reach this exact byte position. If the earlier
test wrote a non-zero, positive time value expecting the MCU to arm a countdown from it, that is a wire
pattern **the real app+cloud+`ctrl` pipeline itself never produces**, and there is no evidence the MCU
firmware (opaque TC32, `08-mcu.md`) does anything with a non-zero value here beyond what a real ACK
already proves it does with zero — nothing distinguishing.

**Architectural implication (§8) matters more here than the specific flag search did.**

---

## 8. Architectural implication: the MCU is very unlikely to be the thing that actually fires this on schedule

This is flagged clearly as **[MEDIUM/INFERENCE]** — real, suggestive evidence, not a live-observed
dispense.

- `ctrl`'s own `.rodata` names a function `feed_over_get_feed_schedule`, logged as
  `"---- delay(%dmin) get feed schedule ----"` — i.e. `ctrl` re-fetches "what's the schedule status"
  **right after every feed completes**. That is exactly the behavior of a host-side scheduler
  re-arming itself, not of a device that just delegated timing to an MCU and can forget about it.
- The `"latest"` value that (per §6) is the thing that actually reaches the wire is a **decaying
  countdown**, recomputed on every poll (`nextTick` changes between captures: `86340` vs `86341`,
  one second apart in two different snapshots — consistent with repeated fetches, not a single stored
  constant). A decaying value is a poor choice for something meant to arm non-volatile MCU storage once;
  it is a natural choice for "the answer to 'how long until the next tick', useful for a *local Linux
  timer* to sleep on."
  - `nextTick` (86341) at the exact moment of this capture perfectly reconstructs `86400 − 62759 +
    62700` (§3) — i.e. it behaves exactly like "seconds until my sleep should end," a host-timer
    concept, not an MCU register value.
- The MCU's own CMD `0x04` ACK (`08-mcu.md` §4/§6) carries only a 1-byte result code — no schedule
  content flows back, so even if the MCU *did* store something useful here, nothing on the Linux side
  could ever verify or rely on it after a restart. A design that put real scheduling authority in a
  component it cannot read back from, for a safety-relevant action like dispensing food, would be an
  unusual choice; a design where the MCU write is a **secondary/cosmetic record** (bookkeeping, or a
  crude fallback with much coarser granularity we haven't found) while the Linux side (`ctrl`, backed
  by cloud connectivity) drives real timing end-to-end is the more parsimonious read of all the
  evidence gathered across this and the referenced studies.
- The RTC sync (`msg 0x6007`, sent immediately before every schedule write, `16-schedule.md` §3.2) is
  fully consistent with either theory (it's needed regardless, e.g. for `FEED_LOG` timestamps), so it
  is not independent evidence either way.

**What this means for Kibble, concretely:** attempting to encode "fire at HH:MM daily" into the wire
`time` field is very unlikely to work, matches no observed real-world byte pattern, and is not what the
vendor's own software does. §9 proposes the corresponding `agent/src/schedule.rs` change.

---

## 9. `config_shm` — exhaustive negative result

Searched the complete, live 11,952-byte dump for every plausible encoding of the known-good specimen
(minutes/seconds since midnight, local and UTC, signed and negated, big- and little-endian, BCD,
military-decimal, raw epoch seconds, and literal ASCII `"17:25"`/`"1725"`/`"62700"`/`"20260916"`), and
separately for the *structural* fingerprint `count=1,reserved=0,…,amount_l=amount_r` at every byte
offset. **Zero matches of any kind.** This is consistent with, and strengthens, the existing
`07-config.md` Table A finding that no `feed`/`schedule`-named field exists anywhere in the vendor's own
228-entry `pktool` debug dictionary. **`config_shm` does not cache the schedule table in any form.**
Persistence, to the extent it exists client-side at all, is not in shared memory.

(The `ctrl`-heap structural fingerprint search, by contrast, produced only certificate/TLS-shaped false
positives — expected, since `ctrl` links `libssl`/`libcrypto` and its heap is dominated by X.509 DER
structures. The literal-JSON-text search in §1 is what actually worked, and is the technique worth
reusing for any future schedule-adjacent capture.)

---

## 10. Confidence summary

| Finding | Confidence | Evidence |
|---|---|---|
| 22-byte entry struct, header shape | HIGH | `16-schedule.md` §3.3, unchanged by this study |
| `amount_l`/`amount_r` = raw byte, divisor disabled on this device | HIGH | Fresh disassembly of the gate (`ctrl 0x47fe6`/`0x480aa`/`0x480cc`) + live read of `config_shm[0xeb0]`/`[0xeb4]` = 0 |
| `id[16]` real-world shape: `"s_YYYYMMDD_SSSSS"` / `"n_SSSSS"`, exactly-16-char case has no in-field NUL | HIGH | Live heap capture, cross-checked against 3 intact empty-state captures |
| Time-of-day unit: local seconds-since-midnight | HIGH | `62700` present 2×; UTC equivalent `77100` searched for, never found |
| Non-negative `t` zeroed before the wire; negative `t` preserved | HIGH | Byte-identical re-pull + independent capstone disassembly, matches `16-schedule.md` §3.4 exactly |
| Wire `time` for this real entry = 0 | HIGH, one inferential link (see §6) | Confirmed encoder logic + confirmed-positive real `t`; no literal wire-payload capture at write time |
| No weekday bitmask on the wire; recurrence is cloud-side JSON only | HIGH | Struct fully accounted for (prior study) + live capture shows recurrence as `"re":"1,2,3,4,5,6,7"`, a JSON sibling never reaching the entry-building code for the `"latest"`-present branch |
| `enable` is not a wire-adjacent field | HIGH (upgraded from prior MEDIUM) | Absent from real captured JSON, not just inferred from struct capacity |
| `"feed list enable"` flag exists somewhere | **Not found** | Exhaustive `config_shm` search (§9) + absent from real JSON |
| MCU does not autonomously fire this on schedule; Linux/cloud side drives real timing | MEDIUM/INFERENCE | `feed_over_get_feed_schedule` re-fetch-after-feed behavior, decaying `"latest".t`, MCU's proven lack of read-back — no live-observed dispense to confirm directly |
| Earlier failed live test is explained | Yes, by §6+§8, not by a missing enable flag | See §7 |

---

## 11. Proposed `agent/src/schedule.rs` change — NOT applied, for Main's review

**No write to the MCU is proposed or should happen without Main's explicit approval.** This is a design
recommendation plus a safe validation-test plan.

**Change:** stop trying to compute a meaningful wire `time`. `wire_time_seconds_until()` (current doc
comment already flags it as "pending confirmation") should be **retired** — it computes exactly the
positive-countdown shape that is now confirmed to be zeroed by the real device before it ever reaches
the wire. Replace it with: always encode `time = 0` for every wire entry (matching confirmed real
vendor behavior byte-for-byte), and implement actual recurrence **entirely in `kibbled`**: a local
timer/reconciler that, at the configured local HH:MM each day an entry is enabled, calls the
already-proven, already-working manual feed path (`dispatch_handler_feed` / `feed_ctrl`, `0x6004`/CMD
`0x0A`) directly — the same mechanism `POST /feed` already uses. The MCU-facing `msg_id 0x6005` write
becomes a "keep the vendor's own bookkeeping in sync" side effect (harmless, matches real behavior,
useful if the app is ever used to *read* schedule status), not the actual trigger.

**Test plan (safest first step, per the assignment's own instruction):** with Nitin's real plan still
active on the device, have `kibbled` construct and send the **byte-identical** table this document
derived (`id="s_20260916_62700"` — or `ctrl`'s freshly-recomputed equivalent for whatever day it is by
the time this runs — `amount_l=1, amount_r=1, time=0`) and confirm the device's behavior is unchanged
(still fires once at 17:25, no double-feed, no missed feed, `GET /state`/hopper sensors unaffected).
This validates the *encoder* without introducing any novel byte pattern the MCU has never seen. A
genuinely novel entry (different time/amount) should not be attempted until Main approves a specific
follow-up test, and only after the Linux-side-timer design in this section (or an equivalent) is in
place, since a wire-only write with `time=0` will not itself cause anything to fire on the new schedule.

---

## 12. What is still open

1. **No literal wire-payload capture of `msg_id 0x6005` at the moment of a real write.** Settling this
   completely would need either a bus-level tap active *during* a live schedule edit (out of scope this
   session — read-only, and no edit occurred while watching), or Main's approval to have `kibbled` itself
   send a byte-identical table (§11) and observe the MCU's ACK/behavior.
2. **Whether the MCU does anything at all with a *negative* `t`.** Never observed in real traffic this
   session; the existing "delete/cancel sentinel" hypothesis (`16-schedule.md` §3.4) remains unconfirmed
   either way.
3. **Portions vs. grams for the schedule path's `amount_l`/`amount_r`**, i.e. what the MCU itself does
   with the value `1` once it has it (irrelevant to the wire *byte*, which is settled, but relevant to
   any future Kibble UI that wants to display it in the app's own units).
4. **Direct proof (vs. strong circumstantial evidence) that the MCU never autonomously fires a schedule
   entry.** Would need either observing a real feed event's live call stack, or further disassembly of
   `net_dev_get_feed_schedule`'s and `feed_over_get_feed_schedule`'s own callers/timers — both are named
   and located in `ctrl`'s `.rodata` (§1) but not yet fully traced.

**Single next experiment that would settle the biggest remaining question (#1 above), if/when Main
wants it pursued:** re-run this exact heap-forensics technique (search for literal JSON text, not binary
patterns) at the moment `ctrl` is *about* to send `msg_id 0x6005` — e.g. capture the heap again
immediately after intentionally triggering `net_dev_get_feed_schedule` (a passive re-fetch, not a
schedule edit) in quick succession, to catch a second `"latest"` sample and confirm the countdown truly
decays between polls exactly as §3's arithmetic predicts. This requires no write and no device risk.
