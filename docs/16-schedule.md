# STUDY-schedule.md — Feed Schedule Protocol, End to End

**Date:** 2026-09-15
**Method:** Offline static ELF analysis (pyelftools + capstone Thumb-2 disassembly, custom PC-relative
literal-pool resolver, ARM long-PLT-stub decoder for libc symbol resolution, GOT-indirection resolver
for the two binaries' internal `register(msg_id, handler_fn, name_str)` dispatch tables, and TBH
jump-table decode for the T31 UART command dispatcher). No contact with the live device — this
supersedes/deepens the schedule-adjacent findings already in `STUDY-ble.md` §2.4/§4.1,
`STUDY-msgids.md`'s ADDENDUM, and `DESIGN-entities.md` §4, all of which are cited below where relevant.
Cross-checked live with `SettingsWrite`'s parallel `STUDY-settings-write.md` session (msg_id family
enumeration, ctrl's dispatch-table-is-a-dead-letter-box finding) — see “Corrections to prior studies”.

**Binaries:** `ble` (ELF32 ARM/Thumb-2, `.text` 0x12490/off 0x2490, `.rodata` 0x2f770) and `ctrl`
(ELF32 ARM/Thumb-2, `.text` 0x15948/off 0x5948, `.rodata` 0x923ec), both `file_off = vaddr - 0x10000`.
GOT bases resolved by disassembly: `ctrl` r4 = `0xd4000` (start of `.got`), `ble` r4 = `0x50000` (start
of `.got`). All function-pointer values recovered via this GOT indirection have the Thumb bit (+1) set
in the raw word, as expected for `BX`/`BLX`-callable addresses — every address quoted below is
**already masked to the even (real) instruction address**.

---

## TL;DR

| Question | Answer |
|---|---|
| Can the schedule be read back from the MCU? | **No.** Three independent, mutually-reinforcing dead ends (see §4). |
| Outbound (ctrl→ble) schedule-set msg_id | **`0x6005`** = `dispatch_handler_ble_set_schedule` |
| UART CMD to the T31 MCU | **`0x04`** (`STUDY-mcu.md`'s "FEED_SCH ACK" — the name is the RX-side ack name for this same CMD number) |
| Per-entry struct | 22 bytes: `id[16]` (string) + `amount_l`(u8) + `amount_r`(u8) + `time`(s32) — **names taken verbatim from the binary's own embedded log format string**, not inferred |
| Entry capacity | **No explicit cap anywhere in the code.** Implicit ceiling ≈24 entries from the bus's hard 540-byte payload truncation (a silent-corruption hazard, not a clean rejection) |
| Time encoding | A required signed 32-bit field read from JSON key `"t"` — see §3.4, this is the one item flagged **UNVERIFIED/surprising** in this whole study |
| Write semantics | **Replaces the whole table.** One message carries the full current entry list; there is no single-entry add/delete on the wire |
| kibbled design | **Own a persisted cache**, do not attempt to query the MCU — see §5 |

---

## 1. The two `0x101a`s are unrelated — first, a namespace clarification

`STUDY-msgids.md`'s ADDENDUM and `STUDY-ble.md` §4.1/§5 both list `0x101a` =
`dispatch_handler_ble_get_schedule` as one of **`ctrl`'s own 27-ish registered top-level dispatch
handlers** (registered on `ctrl`'s *own* inbox, i.e. `dst=1` — not a `ble`-side handler at all, despite
the "ble_" name prefix, which is just this codebase's convention for "handles something BLE-related").
I independently re-derived `ctrl`'s full registration table by disassembly (below) and it matches
exactly. Separately, `ble` sends a *different, unrelated* message that happens to reuse the same numeric
value `0x101a` to **`dst=2` (media)**, with an empty payload (§4.3) — msg_ids are **not globally unique**,
they're scoped per destination queue, and this is a concrete example of that (worth remembering for
future msg_id recovery work — don't assume a numeric collision means the same handler).

### 1.1 `ctrl`'s registration table, disassembly-verified with handler addresses (new — prior studies had names+ids but not handler addresses)

Recovered by disassembling `ctrl`'s registration loop at vaddr `0x15a70`–`0x15c86` (25 back-to-back
`bl 0x80a14(r0=msg_id, r1=handler_ptr_via_GOT, r2=name_str_ptr)` calls; GOT-base r4=`0xd4000` resolved
from the `ldr r4,[pc,#0x530]`/`add r4,pc` pair at ctrl vaddr `0x15986`/`0x15996`). Full 25-entry table
cross-checked byte-for-byte against `STUDY-ble.md` §5's independently-obtained name/id list — exact
match; validated further against the **known-good** `0x100f`→`dispatch_handler_feed`@`0x44f98` entry
from `STUDY-feedtest.md`.

The entry we care about:

| msg_id | Handler name | Handler addr (masked) | Registration call site |
|---|---|---|---|
| `0x101a` | `dispatch_handler_ble_get_schedule` | **ctrl `0x47858`** | `bl 0x80a14` @ ctrl `0x15c32` |

### 1.2 `dispatch_handler_ble_get_schedule` (ctrl `0x47858`) is a dead stub — full disassembly

Function boundary confirmed precisely: its own `ldr rX,[pc,#N]` literal-pool references all resolve to
addresses inside `[0x478d8, 0x47904)` (verified by computing every literal-pool target the function's
own code touches — they cap out exactly at `0x47900`), and a brand-new function prologue
(`push.w {r4,r5,r6,r7,r8,sb,sl,fp,lr}`) starts at exactly `0x47904` — i.e. this function is **126 bytes
of code + 44 bytes of literal pool, nothing more.**

Body, in full (ctrl `0x47858`–`0x478d6`):
```
push {r0,r1,r2,r3,r4,lr}
<load a log-verbosity global via GOT>
cmp <verbosity>-1, #1 ; bhi 0x4787a
bl 0x23054              ; a generic, no-argument diagnostic/state-dump helper (14KB stack buffer,
                         ;   shared utility — NOT schedule-specific; unconditionally reached from here
                         ;   only when verbosity is 1 or 2)
movs r0, #0              ; <-- the ONLY place r0 (return value) is ever set
add sp, #0x10
pop {r4, pc}             ; RETURN 0
; every other branch in this function (0x4787a, 0x478bc, 0x47956) is an alternate
; conditionally-gated AX_SYS_LogPrint()/printf() call, and every one of them falls
; through or branches back to the same "movs r0,#0; pop{r4,pc}" tail above.
```
**Every path returns 0. There is no call to `dispatch_send_msg` (`0x80b00`) anywhere in this function's
body, no read of any config/shared-memory pointer, no reference to `ble`'s queue.** This is not "a read
that we failed to trace" — it is a function that, byte for byte, does nothing but optionally log and
return a constant. **Confidence: HIGH** (full function disassembled, boundary independently confirmed
via literal-pool extent, zero ambiguity in the control flow — every branch converges on the same `mov
r0,#0` return).

---

## 2. `ble`'s own dispatch table has no schedule-*get* counterpart at all

### 2.1 Recovery method (two independent confirmations)

1. **Dynamic symbol table.** `ble`'s `.dynsym` unexpectedly exports every `dispatch_handler_*` function
   as `STT_FUNC` (166 total dynsym entries, 30 of them `dispatch_handler_*`) — this alone gives
   name→address for free, no disassembly needed. Full list checked; **no `dispatch_handler_ble_get_schedule`
   or any other "get schedule" name exists in `ble`'s dynsym at all.**
2. **Registration-loop disassembly**, exactly analogous to §1.1: found by tallying every `bl` target in
   `ble.text` — target `0x25510` is called **exactly 30 times**, matching the dynsym count precisely.
   GOT-base r4=`0x50000` resolved from `ldr r4,[pc,#0x4b0]`/`add r4,pc` at ble vaddr `0x12496`/`0x124a6`.
   Decoded all 30 `(msg_id, handler_addr, name)` triples; **every single one matches the dynsym address
   for that name exactly** (e.g. `0x6004`→`dispatch_handler_ble_feed_ctrl`@`0x16ecc`, matching
   `STUDY-feedtest.md`'s independently live-tested `0x6004` finding byte for byte — strong validation of
   the whole method).

### 2.2 Full 30-entry table (new — not previously enumerated with msg_ids)

`0x6001`:`ble_set_adv`, `0x6002`:`ble_set_led`, `0x6003`:`ble_send_data`, `0x6004`:`ble_feed_ctrl`,
**`0x6005`:`ble_set_schedule`**, `0x6006`:`ble_set_IdSecrect`, `0x6007`:`ble_set_RTC`,
`0x6008`:`ble_send_pt_data`, `0x6009`:`ble_set_beep`, `0x600a`:`ret_interval_data`,
`0x600b`:`ble_set_ir`, `0x600c`:`ble_set_enable`, `0x600d`:`ble_set_food_added`,
`0x600e`:`ble_set_sleep_en`, `0x600f`:`ble_res_feed_log`, `0x6010`:`ble_set_green_led`,
`0x6011`:`ble_set_BLE_relay`, `0x6012`:`ble_get_rtc_right_now`, `0x6013`:`ble_get_feed_log_right_now`,
`0x6014`:`ble_resetMCU`, `0x6015`:`ble_uart_ota_start`, `0x6016`:`ble_uart_ota_stop`,
`0x6017`:`ble_dev_list_ctrl`, `0x6018`:`WAN_ctrl_ble_relay`, `0x6019`:`ble_relay_response_rpt_cb`,
`0x601a`:`ble_uart_send`, `0x601b`:`subchip_req_data`, `0x601c`:`update_ble_dev_type`,
`0x601d`:`ble_disable_RTC`, `0x601e`:`discon_ble_relay`.

**This is the complete, exhaustive set of messages `ble`'s own inbox understands (dst=8).** Note it
*does* have `ble_get_rtc_right_now` (`0x6012`) and `ble_get_feed_log_right_now` (`0x6013`) — i.e. the
firmware authors clearly *do* have the concept of a "get X back from the device" message for other
subsystems, and simply never built one for schedule. **Confidence: HIGH** (dual-sourced: dynsym export
table + independent registration-loop disassembly, exact agreement on all 30 entries).

---

## 3. Write path: `ctrl → ble` msg `0x6005` → UART CMD `0x04`

### 3.1 `ble`-side receiver: pure, unconditional pass-through (no repacking)

`dispatch_handler_ble_set_schedule` (ble `0x170fc`) is a 20-byte wrapper:
```
push {r3, lr}
cbz r2, 0x1710c        ; r2 = payload ptr; if null, return -1
mov r1, r3               ; r3 = len
mov r0, r2
bl 0x17944                ; delegate(payload_ptr, len)
movs r0, #0
pop {r3, pc}
```
`0x17944` (the delegate) is **also** a pure pass-through:
```
push {r0, r1, r2, lr}
movs r2, #0
str r1, [sp]              ; len -> stack (frame-builder's 5th arg)
mov r3, r0                 ; payload ptr (unchanged)
mov r1, r2                  ; flag_bit6 = 0
movs r0, #4                  ; CMD = 4
bl 0x16970                    ; build_and_send_uart_frame(cmd=4, flag=0, subaddr=0, payload, len)
```
**The bytes ctrl sends over the bus become the UART CMD `0x04` payload byte-for-byte, unmodified.**
There is no ble-side struct at all for this message — the struct recovered in §3.3 below (built by
`ctrl`) *is* the UART wire payload directly. **Confidence: HIGH** (both functions fully disassembled,
CMD immediate `#4` directly visible at the call site to the confirmed frame-builder `0x16970`).

### 3.2 Full call chain from the cloud property down to the send

```
cloud "property/set" MQTT message, top-level JSON key "feed" (LOCALKIT-HARVEST.md's documented key)
  -> ctrl: parse_recv_property_set_feed_param  (source file server_cmd_parse.c, string ctrl 0xa60f6,
       function body ctrl ~0x3c3c0-0x3c42e)
     - clears a global byte at struct-offset 0x26e9 (also touched by pk_schmg_parse_schedule itself,
       and independently by ble around its own CMD-0x04 ACK/0x101a-to-media path — looks like a
       shared "schedule sync in flight" flag; not otherwise characterized in this pass)
     - reads the "feed" cJSON item's own valuestring (offset 0x10 of the cJSON node — i.e. "feed"'s
       *value* is itself a JSON-encoded STRING, not a nested object: a double-encoded property, a
       common IoT/MQTT pattern)
     - strlen()s it, calls: bl 0x4799c(text_ptr, len)     <- ctrl 0x3c42e
  -> ctrl: pk_schmg_parse_schedule  (source file schedule_ctrl.c, strings ctrl 0xac2cc/0xac324 —
       **this is the function's own name, read directly out of its embedded debug-log call, not
       inferred**; entry point ctrl 0x4799c)
     - sanity-checks len > 0x27 (39) and re-strlen()s the text as a second check (ctrl 0x47a34-0x47a40)
     - bl 0x8bda8(text)   -- cJSON_Parse-shaped call (parses the SECOND, inner JSON document)
     - get_object_item(root, "result") -- ctrl 0x47b12; if present, XORs the 0x26e9 flag byte
     - get_object_item(root, "latest") -- ctrl 0x47bf4; REQUIRED, else return -1
     - array_size("latest") -- ctrl 0x47cb6 (bl 0x8bdc4) -> entry count N, stashed at [sp+0x2c]
     - malloc(22*N + 2)  -- ctrl 0x47dfa-0x47e04 (bl 0x1563c = malloc, confirmed via PLT resolve);
       this buffer IS the exact byte sequence later sent as msg 0x6005's payload
     - per-entry loop (§3.3) builds each 22-byte record
     - bl 0x80b00(msg_id=0x6007, dst=8, payload=<4-byte value from bl 0x80312>, len=4)   <- ctrl 0x48a08
       (0x6007 = ble_set_RTC per §2.2; 0x80312's return value is reused elsewhere in this same codebase
       as a log-tag/PID-ish value, but here it's written directly into the outgoing 4-byte RTC payload,
       so it is most plausibly a `time(NULL)`-shaped "current Unix time" helper — MEDIUM confidence,
       not fully independently disassembled this pass, tangential to schedule)
     - bl 0x80b00(msg_id=0x6005, dst=8, payload=r7 (the malloc'd buffer), len=uxth([sp+0x28]+2))
       <- ctrl 0x48a18   **the schedule-set send**
```
So: **every schedule write also refreshes the MCU's RTC** (msg `0x6007` sent immediately before msg
`0x6005`, same function, same trigger). **Confidence: HIGH** for the call chain and both msg_id/dst
pairs (both `bl 0x80b00` sites and their preceding `movw r0,#imm`/`movs r1,#imm` directly disassembled);
MEDIUM for `0x80312`'s exact identity (RTC-refresh side effect is a reasonable but not fully confirmed
inference).

### 3.3 The 22-byte per-entry struct — field names taken verbatim from the binary's own log string

Smoking-gun evidence: `pk_schmg_parse_schedule` logs each entry it builds with format string (ctrl
`0xac64f`, resolved by the same PC-relative technique used throughout this study):
```
"[%s][%s][%s][%d]: latest_item[%d]:id = %16s,amount_l=%d,amount_r=%d,time=%d\n"
```
This directly names every field in order. Cross-checked field-by-field against the actual `strb`/`str`
instructions that fill the entry buffer (`r6`, a reusable 22-byte scratch region, later `memcpy`'d
22-bytes-at-a-time into the final `malloc`'d buffer at `ctrl 0x4831a`-`0x48358`):

```c
struct schedule_entry {              /* 22 bytes; sent `count` times back-to-back */
    char     id[16];                  /* +0x00  cJSON key "id" (string). memset(0,16) then memcpy up
                                        *        to strlen(id), REQUIRED, max 16 chars (strlen>0x10 is
                                        *        an error) — ctrl 0x47ffc(get "id")-0x4802e(memcpy) */
    uint8_t  amount_l;                 /* +0x10  cJSON key "a" OR "a1" (fallback pair — "a" tried
                                         *        first at ctrl 0x47fd4, falls through to "a1" at
                                         *        ctrl 0x48096 only if "a" is absent). REQUIRED
                                         *        (neither present -> return -1). Read via cJSON
                                         *        valueint (node offset +0x14), optionally divided
                                         *        by a per-field config value (__aeabi_idiv, gated by
                                         *        a flag at config-struct offset 0xeb0) before the
                                         *        strb at ctrl 0x47ff2 / 0x480b6 */
    uint8_t  amount_r;                  /* +0x11  cJSON key "a2", REQUIRED, same shape (config-struct
                                          *        offset 0xeb4 for its own divisor flag) —
                                          *        ctrl 0x480ba(get "a2")-0x480d8(strb) */
    int32_t  time;                       /* +0x12  cJSON key "t", REQUIRED (absent -> return -1 via
                                           *        ctrl 0x48046-0x4812c). Read via valueint
                                           *        (+0x14). See §3.4 for the odd sign-dependent
                                           *        write — ctrl 0x4803a(get "t")-0x482a8..0x482b6 */
};
struct schedule_msg {                  /* sent as ctrl->ble msg_id 0x6005 AND verbatim as UART CMD 0x04 */
    uint8_t  count;                      /* +0    incremented once per entry actually packed —
                                           *       ctrl 0x48358-0x4835c */
    uint8_t  reserved;                    /* +1    zeroed once at alloc time (memset via strh),
                                            *       never written again — ctrl 0x47ea0-0x47ea2 */
    struct schedule_entry entries[count]; /* +2    packed back-to-back, 22 bytes each */
};                                          /* total wire length = 2 + 22*count, computed at
                                             * ctrl 0x48a0c (uxth of [sp+0x28]+2) and passed as the
                                             * `len` arg of dispatch_send_msg(0x6005, 8, buf, len) */
```
**Confidence: HIGH.** Every offset above was read directly off a `strb`/`ldrb` instruction with an
explicit immediate offset into the entry buffer; the field *names* come from the binary's own debug
string (not guessed), and the overall 22-byte size independently cross-checks against the
`movs r6,#0x16` (22) `mul` used for the `malloc` size and the per-entry copy stride — three
independent arithmetic confirmations of the same number.

### 3.4 The one UNVERIFIED item: `time` collapses to zero for the (presumably normal) non-negative case

Disassembly of the `time`-field write (ctrl `0x482a8`-`0x482b6`, raw bytes re-verified byte-for-byte,
not just via capstone's text rendering):
```
ldr   r3, [r0, #0x14]      ; r3 = time_item->valueint
cmp   r3, #0
itett ge
movge r3, #0                ; if time>=0: r3 := 0
strlt.w r3, [r6, #0x12]       ; if time<0 (BEFORE the movge above executed): store raw r3 as a WORD
strhge r3, [r6, #0x12]         ; if time>=0: store r3(=0) as halfword
strhge r3, [r6, #0x14]          ; if time>=0: store r3(=0) as halfword    (together: zero all 4 bytes)
```
Manually re-derived from raw bytes (`43 69 00 2b a9 bf 00 23 c6 f8 12 30 73 82 b3 82`) independently of
capstone's mnemonic text and confirmed identical. **The literal, disassembly-proven behavior is: a
non-negative `time` value is discarded and the 4-byte field becomes `0`; only a negative value is
preserved (as a raw signed 32-bit word).** This directly contradicts a plain "minute-of-day passed
through" encoding for the common case. I cannot fully explain this from static analysis alone — it may
mean: (a) legitimate schedule times in this build are conveyed some other way this pass didn't find
(the `id` string is the only other variable-content field, and is generously sized — 16 bytes is more
than a `"HH:MM"` needs — so it is plausible, but **not proven**, that `id` itself carries the real
time/identity payload and `t` is mostly a spare/negative-sentinel slot); (b) a real firmware quirk/bug;
or (c) `t`'s negative range is reserved for a delete/cancel semantic analogous to `feed_ctrl`'s `cancel`
byte, and ordinary adds are expected to arrive with `t<0` in practice, contradicting the field's own
name. **Do not implement kibbled's schedule-write path by assuming this field carries a plain
minute-of-day integer without a live capture confirming it** — this is the one place in this whole
recovery where I'd explicitly recommend a single passive/read-only verification step (a `strace`-style
capture of one real app-driven schedule edit) before shipping. Everything else in §3.3 is safe to build
against as-is.

### 3.5 CMD `0x04` cross-reference to `STUDY-mcu.md`

`STUDY-mcu.md` §4's disassembly-verified 28-entry T31 CMD table names CMD `0x04` **"FEED_SCH ACK"
(`res=%d`)**. Matches exactly: the RX-side handler for CMD `0x04` (decoded fresh this pass from the TBH
table at ble `0x15b20`, table index 4 → handler `0x15caa`) reads exactly one byte, `payload[0]` (frame
offset +7), as a result code and, if it's `1` or `2`, signals a local completion event (§4.2). "ACK" in
the RX-side name is simply this codebase's convention for "the reply that comes back for a command with
this same CMD number" — it does not imply a different CMD carries the request.

### 3.6 A `0x05` sibling exists but is **not** part of the schedule path — closing `STUDY-ble.md`'s open item

`STUDY-ble.md` §2.4 flagged CMD `0x05` as "sibling of 0x04, exact semantics not pinned". Traced this
pass: its only caller in the whole binary is ble `0x16ffc`, which reads its three arguments from
`[r4+0]`, `[r4+0x41]`, `[r4+0x42]` — **exactly** `feed_ctrl`'s own `cancel`/`amount1`/`amount2` offsets
(`STUDY-feedtest.md`'s struct), and `0x16ffc` sits inside `dispatch_handler_ble_feed_ctrl`'s own address
range (`0x6004`, ble `0x16ecc`-`0x170fc`). **CMD `0x05` is sent from within the feed handler, using the
feed struct's own fields, conditionally** (gated by a `beq` I did not fully trace) — most plausibly a
"this dispense corresponds to a schedule slot" bookkeeping notification to the MCU, sent alongside the
real CMD `0x0A` dispense, not a second, independent schedule-write path. **Confidence: MEDIUM** (call
site and argument correspondence are solid; the exact trigger condition and purpose are not fully
traced — tangential to this assignment, flagged for a future feed-focused pass rather than pursued
further here).

---

## 4. Read path: definitively absent, three independent dead ends

1. **`ctrl`'s own `0x101a` handler is a stub that always returns 0** and never calls `dispatch_send_msg`
   (§1.2, HIGH confidence, full function disassembled).
2. **`ble` has no "get schedule" message in its own 30-entry dispatch table** — only `set_schedule`
   (`0x6005`) exists; contrast with `ble_get_rtc_right_now`/`ble_get_feed_log_right_now`, which prove
   the firmware *does* have a "read X back" pattern for other subsystems and simply never built one for
   schedule (§2.2, HIGH confidence, dual-sourced via dynsym + registration-loop disassembly).
3. **The UART CMD `0x04` ACK itself never reaches `ctrl`.** Fully disassembled ble `0x15caa`-`0x15e30`
   (the complete RX handler for CMD `0x04`, both its `flags==0` and `flags!=0` branches): the normal
   case calls a local function (`0x15890`) that does nothing but compare the ack's CMD+SUBID against
   the last CMD+SUBID *`ble` itself sent* and, on match, releases an internal wait — a pure
   synchronous-write-confirmation primitive, entirely local to `ble`, that never touches the message
   bus. The only `dispatch_send_msg` call anywhere in this handler (`ble 0x15e28`, reached only when the
   frame's subaddress nibble is `1`) sends `msg_id=0x101a` to **`dst=2` (media)** with a **NULL,
   zero-length payload** — an empty notification to an unrelated process, not schedule content, and not
   even addressed to `ctrl`. (This is the same numeric `0x101a` from §1 — confirmed here to be a
   completely different, unrelated message; see §1's namespace note.)

**There is no code path anywhere in `ctrl` or `ble`, in either direction, that moves schedule content
from the MCU back to `ctrl`, the cloud, or an HA-facing surface.** This isn't "unproven" or "an open
item" — it is three separately-disassembled, complete functions all independently confirming absence.
This directly reconfirms and hardens `DESIGN-entities.md` §4.3's existing (softer, string-search-based)
conclusion with full instruction-level proof.

---

## 5. What kibbled should do

**Own the schedule; do not attempt to read it back — there is nothing to read.**

- Maintain a persisted (not just in-memory) authoritative cache of the entry list, since §4 proves no
  MCU query can ever repopulate it after a restart. This is a hard requirement, not a nice-to-have:
  `DESIGN-entities.md` §4.3/§9 already flagged the cold-start gap as a caveat on top of an assumed-good
  design; this study shows the design has no alternative to fall back to, so the persistence needs to be
  treated as core, not optional.
- Every `kibble.schedule_add`/`_remove`/`_set_enabled` operation must **rebuild and resend the complete
  entry list** (msg `0x6005`, dst 8, header `{count, 0}` + 22-byte records per §3.3) — the wire protocol
  has no single-entry add/remove primitive; "remove" is "resend everything except that entry."
- Self-enforce a **≤24-entry cap client-side** before sending (`(540 - 2) / 22 = 24.4`, floored). The
  device enforces nothing here — `dispatch_send_msg`'s own hard 540-byte truncation
  (`STUDY-feedtest.md` §"Corrected wire format") would silently truncate a 25th+ entry into a
  corrupted, partial 23-byte tail rather than reject the write. `DESIGN-entities.md` §8.2 already
  flagged "no MCU-side storage-capacity constant was found" as to-verify; this resolves it precisely —
  the real ceiling is the bus, not the MCU, and it's a truncation hazard, not a clean limit.
- Refresh the MCU's RTC (msg `0x6007`, 4-byte payload, `dst=8`) alongside every schedule write, matching
  the observed `ctrl` behavior exactly (§3.2) — cheap to replicate and keeps parity with stock behavior.
- **Before wiring up the `time` field**, get one live, read-only confirmation of what a real app-driven
  schedule edit actually puts in `t` (§3.4) — this is the single genuinely open question left in the
  whole write path, and it's cheap to resolve with a passive capture (no new write risk) once live
  verification is back in scope.
- Do not build any "get schedule" client code against `0x101a` — it is dead in this firmware build,
  confirmed at both ends.

---

## 6. Corrections to prior studies / cross-references

- `STUDY-ble.md` §"Open items" question 3 and its `0x97→key_change_wifi` type-mapping discussion are
  unrelated to this study and untouched.
- `SettingsWrite`'s parallel `STUDY-settings-write.md` (2026-09-15) independently found and named
  `parse_recv_property_set_feed_param` (ctrl `~0x3c3c0`-`0x3c42e`, `server_cmd_parse.c`) — that
  function is the **caller** documented in §3.2 above; it extracts the cloud "feed" key's (double-JSON-
  encoded) string value and hands it to `pk_schmg_parse_schedule` (this study's `ctrl 0x4799c`). Their
  write-up scoped that function as "a feed-record id reference" without following the callee — this
  document supersedes that specific characterization; the rest of their session (full 25-entry ctrl
  inbound table, proof that ctrl's single `mq_receive` site linearly drops any unregistered msg_id, and
  the `0x600C`/`0x600D`/`0x6007` msg_id recoveries) is independent, complementary, and cited above
  where it overlaps (§3.2's RTC reuse, the `0x6007` cross-check).
- `DESIGN-entities.md` §4.3's "no bulk read-back UART command was recovered" conclusion is upgraded here
  from "checked the RX command table, found nothing named GET" to "traced all three plausible forwarding
  points end to end, all three are dead" — same conclusion, substantially stronger evidence.

---

## Confidence summary

| Finding | Confidence | Evidence type |
|---|---|---|
| `0x101a` registered on `ctrl`'s own queue, handler = stub | HIGH | Full disassembly, function boundary independently confirmed |
| `ble` has no get-schedule handler (30/30 enumerated) | HIGH | Dual-sourced: dynsym export table + registration-loop disassembly |
| CMD 0x04 ACK never reaches ctrl / carries no data | HIGH | Full RX-handler disassembly, both branches |
| msg_id 0x6005 = ble_set_schedule, dst=8 | HIGH | Registration table + direct `bl 0x80b00` call-site disassembly |
| ble-side set_schedule is a verbatim pass-through to UART CMD 0x04 | HIGH | Full disassembly of both wrapper and delegate |
| 22-byte entry struct, offsets 0x00/0x10/0x11/0x12 | HIGH | Field names from the binary's own log format string + direct strb/str offset disassembly, cross-checked 3 ways against the 22-byte stride |
| `id`/`amount_l`(`a`/`a1`)/`amount_r`(`a2`) semantics | HIGH | Direct cJSON key-string resolution at each call site |
| `time` field's sign-dependent zero-out behavior | HIGH (as literal fact) / UNVERIFIED (semantic meaning) | Raw-byte-level IT-block re-verification; semantic "why" not resolved statically |
| No explicit entry-count cap | HIGH | Exhaustive scan of every `cmp`-immediate in the function; nothing above 0x27 (a string-length check, not a count) |
| Implicit ~24-entry ceiling | HIGH (arithmetic) | Derived from `STUDY-feedtest.md`'s independently-proven 0x21c bus truncation constant |
| RTC (0x6007) resent alongside every schedule write | HIGH | Direct disassembly of both adjacent `bl 0x80b00` sites |
| 0x80312 = a time-source function | MEDIUM | Inferred from its return value being written into the RTC payload; not independently disassembled |
| CMD 0x05 = feed-schedule-bookkeeping companion, not a schedule-write path | MEDIUM | Caller/argument correspondence solid; exact trigger/purpose not fully traced |
| Entries are daily, not per-weekday | MEDIUM (absence-based) | No weekday/bitmask field found anywhere in the fully-enumerated 22-byte struct |
| `enable` is not a wire field; disabled entries are simply omitted from `"latest"` | MEDIUM (inference) | Struct is fully accounted for at 22 bytes with no room left; the cloud→internal transform that would filter disabled entries was not itself traced (out of scope, happens upstream of `pk_schmg_parse_schedule`) |
