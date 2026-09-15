# STUDY-settings-write.md — Petkit D4SH2 `ctrl` Settings Write-Path Recovery

**Date:** 2026-09-15
**Scope:** `ctrl` binary only (`fs/app/bin/ctrl`, ET_EXEC Thumb-2 ARM, `.text` vaddr 0x15948/off 0x5948,
`.rodata` vaddr 0x923ec/off 0x823ec — constant delta `file_off = vaddr - 0x10000` for every section).
`ble` (`fs/app/bin/ble`, same delta convention) was also pulled locally and searched (read-only, string-table
only) specifically to settle the desiccant question in §6. No live device contact was made; **100% static
analysis** of the binaries already present under `fs/app/bin/` on this host, using `pyelftools` (ELF/section/
relocation parsing) + `capstone` (Thumb-2 disassembly) in a local Python venv. Method for resolving PC-relative
string loads is the two-instruction idiom given in the assignment brief (`ldr rX,[pc,#imm]` ... `add rX,pc`,
not necessarily adjacent — see §0.1), extended here to handle the compiler's instruction-scheduled form where
1–4 unrelated instructions separate the `ldr` from its `add` (register-clobber-tracked, described below).

## 0. Method notes (read before the tables — explains every "confidence" rating below)

### 0.1 PC-relative resolver

The straightforward "adjacent-pair" version of the `ldr rX,[pc,#imm]; add rX,pc` idiom (as used successfully
in `STUDY-config.md` §6.2 for one call site) **only recovered 0/6 of the target strings this task needed**
(`feed_realtime`, `"===== set lightMode..."`, etc. — all zero hits). Disassembling the actual call sites showed
why: the compiler frequently schedules 1–4 unrelated instructions (stack spills, immediate loads for *other*
arguments) between the `ldr` and its matching `add`, e.g.:

    0x44018: bl #0x80312
    0x4401c: ldr r3, [r6, #0x14]
    0x4401e: mov r1, r0
    0x44020: ldr.w r0, [pc, #0x7e8]      <- ldr for THIS string, register r0
    0x44024: str r3, [sp, #4]             <- unrelated (building another arg)
    0x44026: mov.w r3, #0xcc0             <- unrelated (line-number literal)
    0x4402a: str r3, [sp]
    0x4402c: add r0, pc                    <- the matching add, 3 instructions later

A corrected resolver was built: scan forward from every `ldr rX,[pc,#imm]` and keep it "pending" per-register
until either (a) the matching `add rX,pc` is seen (resolve: `target = *((ldr_addr+4)&~3)+imm) + add_addr+4`),
or (b) another instruction overwrites `rX` first (drop it — false pairing). `bl`/`blx` clobber r0-r3/r12 per
AAPCS; `pop`/`ldm` clobber their whole register list. This raised the hit rate from 0 to 4726 unique resolved
targets (14,452 total call sites) out of ~6237 strings in `.rodata`, and correctly resolved every string this
task needed. A second bug (mnemonic `ldr.w` — the 4-byte Thumb-2 encoding — not matching a filter that only
checked for bare `ldr`) cost roughly 9,600 instructions of coverage before being caught; both are noted here
because a future pass reusing this technique will hit the identical two pitfalls.

### 0.2 PLT resolution

`ctrl` links 252 dynamic symbols via a "long-form" ARM PLT (`add ip,pc,#imm1; add ip,ip,#imm2; ldr pc,[ip,#imm3]!`
— GOT is >0xbf000 bytes from `.plt`, past the short-form PLT's reach). All 252 stubs were resolved to symbol
names by decoding the two `add` immediates + the final `ldr` displacement with `capstone.detail=True` and
matching the computed GOT address against `.rel.plt`'s `r_offset` column (100% resolved, 252/252). This is
what let every `blx #0x1XXXX` in the disassembly dumps below be identified by name (`flock`, `MD5_Init`,
`AES_cbc_encrypt`, `strcmp`, `cJSON`-family calls at `0x8bde0`/`0x8bdc4`/etc. are **not** in this table — those
are statically-linked `libcjson` calls, resolved by behavior/operand pattern, not by symbol name).

### 0.3 Confidence scale (matches DESIGN-entities.md)

**HIGH** = disassembly shows the exact instruction (`str`/`str.w`/`strb.w` to `[base_reg, #imm]`, or a `bl`/`blx`
with the immediate operands read directly) — no inference. **MEDIUM** = the mechanism/offset is shown but one
link in the chain (e.g. an array's exact element stride beyond the first pair, or a called sub-function's own
body) wasn't independently re-traced. **LOW** = structural/pattern match only. **UNVERIFIED** = observed but the
receiving side (a different process's own handler table) would need that binary independently disassembled to
confirm — noted explicitly per row.

---

## 1. The rodata neighbourhood 0xaa400–0xab000, and its sibling block 0xa9200–0xaa400

The assignment's anchor pair — `feed_realtime` (0xaa606) / `feed_realtime_cancel` (0xaa614) — sit inside a
**command-name block** that runs from roughly 0xaa33a (`power`) to 0xaaff6. Every printable string in
0xaa400–0xab000 is reproduced in Appendix A verbatim. The important structural finding, not visible from the
0xaa400–0xab000 window alone, is that **this command block is immediately preceded by a second, distinct
block (0xa9200–0xaa280) that is the debug-string trail of the *settings write* handler itself** — every
`"===== set <key> (%d) ====="` string for the property_set path lives there, in the exact order the handler
processes fields. This second block is what made the whole rest of this study possible (§2) and is dumped in
full in Appendix B since the assignment's §1 instruction to "enumerate all sibling command-name strings"
undersells how directly useful the *neighbouring* block turned out to be.

### 1.1 Command names in 0xaa33a–0xaaff6, each resolved to its `.text` strcmp/dispatch site

All of these are compared against an incoming JSON `"action"`-style command string via a **strcmp chain**
(`blx strcmp` @ PLT 0x1576c) inside one function beginning at **0x43cb2** (first `strcmp` call site 0x43cb8,
resolves to `"power"`) and running to at least **0x449de** (`"privacy"`). This is `ctrl`'s single top-level
**cloud-action dispatcher** — every `property/set`-adjacent "do something now" command funnels through here,
one `strcmp` per candidate, first match wins, falls through to `0x43cde` (shared "not handled, log+return")
if nothing matches. Each entry below cites the `strcmp` call site (**HIGH** — the string is the literal
second operand of that specific compare) and, where traced further, the code it guards.

| Command string | rodata addr | strcmp call site | What happens on match | Confidence |
|---|---|---|---|---|
| `power` | 0xaa33a | 0x43cb8 | Guards a `power_action` sub-dispatch (nested `"start"`/`"stop"`/`"end"` string compares at 0x43d72/0x43ea2/0x43f2c, resolved via raw-byte read after the PC-relative resolver initially came up empty — see §0.1). Not traced past the nested compares; looks like camera/lapse-recording start/stop/end, not a power-off. | MEDIUM (dispatch site HIGH, payload semantics not traced) |
| `reset_pet` / `reset_pet_action` | 0xaa52e/0xaa538 | 0x43fb6 | Not traced past the compare. | LOW (site only) |
| `lapse` / `lapse_action` | 0xaa5a2/0xaa5a8 | 0x44040 | Not traced past the compare — almost certainly timelapse recording control (`usr.app_conf.lapseTime`/`lapseVideo`/`lapseEndTime` are handled a few hundred bytes later in the *settings* function, §2). | LOW |
| **`feed_realtime`** | 0xaa606 | 0x440ca | Confirmed path from `STUDY-feedtest.md` — builds the 67-byte `feed_ctrl` struct, `dispatch_send_msg(0x6004, dst=8/ble, …)`. Re-confirmed independently in this pass. | **HIGH — live-tested** |
| **`feed_realtime_cancel`** | 0xaa614 | 0x44190 | Same struct with `cancel=1` (`strb r3,[r4]` @ 0x441aa, r3=1), same `0x6004`→ble send. | HIGH |
| **`added`** | 0xaa629 | 0x441fe (resolved via `LDR2TARGET[0x441f6]`, not by the naive backward-strcmp-operand scan — see §6.2) | **This is the "food replenished" signal** — see §6.2, it's the answer to that part of the assignment. | HIGH |
| `play_sound` / `soundId` | 0xaa62f/0xaa63a | 0x4422c | Not traced (plays a notification sound by id — not a *setting*, an action). | LOW (site only) |
| `start_live` / `start_rtm` / `rtcToken` / `channelId` / … | 0xaa6a9 onward | 0x442d0 / 0x442de (+ a second, nested pair at 0x443fc/0x44482) | Agora video/voice call session bring-up (rtmToken/appRtmUserId/devRtmUserId are Agora RTM identifiers). Out of scope for settings. | LOW (site only) |
| `open_camera` | 0xaa7d3 | 0x44504 | Not traced — temporarily opens the camera stream (distinct from the persistent `camera` **setting**, §3). | LOW |
| `ble_relay_update` / `update_action` / `connect_action` | 0xaa870/0xaa881/0xaa926 | 0x445c4 (+ nested `"connect"` compare at 0x44658, raw-byte resolved) | BLE-relay (second-hopper-unit?) pairing/update flow. Out of scope. | LOW |
| `discern_get` | 0xaa9ce | ~0x4494a region (nested `"discern"` compare, raw-byte resolved) | Pet-recognition on-demand query — read, not write. | LOW |
| `privacy` / `privacy_action` | 0xaaa59/0xaaacc | 0x449de | Privacy-mode (presumably camera/mic mute) toggle — **not** the same as the `camera`/`microphone` *settings* in §3; a session-scoped action. Not traced further. | LOW |
| `device_sw` (debug label, not a JSON key — see below) | 0xaab3a | n/a | **Not a top-level command.** Both xrefs to the `"----------- device_sw set (%d) -----------"` string (0x3c184, 0x3c29a) are *inside* the function at **0x3c118** (`set_device_sw_mode`, confirmed by this being its own debug name), which is the shared move/pet/eat-detection enable/disable cascade — see §3's `camera` row and §3.x. It is reached from the `camera`-setting write path (0x3e8fa) and from one more internal call site (0x43d3c), **not** from a distinct cloud command string. | HIGH that this is internal, not a command |

Full string block reproduced verbatim in **Appendix A**.

---

## 2. The settings-write function itself

`ctrl` parses `property/set` through (function names below are literal strings the binary logs as its own
`__FUNCTION__`-equivalent, resolved via the same PC-relative technique — **HIGH** confidence these are the
real names, since they are the exact 3rd `%s` argument of each field's own `AX_SYS_LogPrint` call):

- `iot_property_set_recv_parse` / `web_property_set_recv_parse` — the two JSON entry points (MQTT-cloud vs.
  a **local web-based** `property_set` receiver — `on_web_recv_property_set` exists too; see §5.2, this is a
  real, if unconfirmed-scope, local-network surface that this study did not chase further).
- `parse_recv_property_set_feed_param` (source file `server_cmd_parse.c`) — handles a `"feed"` string key
  (≤39 chars, length-validated at **0x4799c**) ahead of `factor1`/`factor2`/etc. in code order. **This is a
  feed-record-id field, not the schedule array** — flagged to `ScheduleWrite` via hub (§7).
- `parse_recv_property_set_algo_param` and `parse_recv_property_set_normal` — not separately entered from what
  was traced; every field in §3's table below is walked **inside one large function**, `0x3c3b2`–`~0x40100`+,
  that processes each `usr.app_conf.*` (and a few `state.*`/`usr.*` top-level) key in turn. Every field follows
  the *identical* compiled shape, confirmed field-by-field by disassembly, not inferred once and assumed:

```
r0 = cJSON_GetObjectItem(root, "<key>")     ; bl 0x8bde0
cmp r0, #0 ; beq <next-field>                ; key absent -> skip entirely, no write
r3 = r0->valueint   [offset 0x14]             ; (or ->valuestring @0x10 for string-typed fields)
r2 = *g_config                                 ; g_config is a config_t* global; dereferenced once per field
r1 = r2[OFFSET]                                ; current stored value
cmp r1, r3 ; beq <next-field>                  ; unchanged -> skip write (and skip any notify/log below)
str[.w]/strb[.w] r3, r2[OFFSET]                ; <<< THE WRITE >>>
if (log_level > 5) AX_SYS_LogPrint(...)        ; "===== set <key> (%d) =====" debug line
[[ optional: dispatch_send_msg(...) directly, OR bl 0x90c20(type_code) ]]
```

`g_config` (the pointer variable) is loaded once per field via a per-function "anchor" register + a small
GOT-like offset table — never a bare absolute address (matches `STUDY-config.md` §6.1's finding that this
binary never uses bare absolute literals for globals). `cJSON_GetObjectItem` was identified by behavior
(`bl 0x8bde0`, takes `(cJSON*, const char*)`, returns an item pointer whose `+0x14`/`+0x10` fields are read
next — matches the standard `cJSON` struct layout `{next,prev,child,type,valuestring@0x10,valueint@0x14,
valuedouble@0x18,string@0x20}`) rather than by a resolved symbol name (statically linked, no `.dynsym` entry).

This mechanical shape is what let every offset in §3 be pulled out by direct disassembly rather than guessed.

### 2.1 The critical finding: most of the "notify" calls are dead code

Two distinct notification idioms appear after a field's `str`:

**(a) `bl 0x90c20`** — a shared helper taking one argument (a small "category" integer, almost always **0x22**,
occasionally 0x23/0x2a/0x2c — 51 call sites total, spanning essentially every `usr.app_conf.*` boolean/int
field). Fully disassembled (0x90c20–0x90c7c): it `memset`s a stack buffer, conditionally builds a formatted
string via a second helper (**0x90460**, gated by two config_shm flag bytes at offsets 9928/9929 — `0x26c8`/
`0x26c9` — and skipped entirely if the category code is exactly 0x10), then unconditionally:

    mov r2, r4        ; buffer
    uxth r3, r0        ; len = strlen(buffer)
    movs r1, #1          ; dst = 1   <-- ctrl's OWN queue
    movs r0, #2           ; msg_id = 2
    bl dispatch_send_msg

**(b) A handful of fields call `dispatch_send_msg` directly**, bypassing 0x90c20: `microphone`→msg 0xD,
`night`(irlight)→msg 0xF, `timeDisplay`→msg 0xC, `volume`→msg 0x14 — **every one of these ALSO targets
`dst=1` (ctrl's own queue)**.

`dispatch_send_msg` (0x80b00) was fully disassembled to check for a `dst==self` fast path: **there isn't
one** — `dst` is range-checked (1–20 valid) with no special case for `dst==own_id`, and falls through to the
same `pthread_mutex_lock` + queue-handle-table lookup used for every other destination. So a `dst=1` send is
a completely ordinary `mq_send("/msg_dispatch_1", ...)` — **ctrl sends a message to its own inbox.**

**The payoff:** `ctrl`'s own receive loop was then fully disassembled (single `mq_receive` call site in the
whole binary, at **0x80418**; dispatch logic 0x804aa–0x80594). It does a **linear search of the *exact same*
25-entry table** built by the `bl 0x80a14` registrations enumerated in `STUDY-msgids.md`'s ADDENDUM (msg_ids
0x1002–0x101e plus 0x10) — confirmed by disassembling the search loop itself:

    ldrh r5, [r4]                    ; msg_id from the received envelope
    ...
    mul r1, r7(=0xC), r3(=i)          ; r7 = 12-byte stride, r3 = loop index
    ldrh.w ip, [r1, r2]                ; table[i].msg_id  (r2 = table base)
    cmp ip, r5 ; bne <i++, loop>        ; no match -> keep scanning
    ldr r5, [r2+r1, #4]                  ; MATCH -> r5 = handler function pointer
    ...
    cbz r5, <return>                      ; NULL handler -> just return
    blx r5                                  ; CALL handler(msg_id, src, payload, len)

If the loop exhausts the table with **no match**, `r5` is left `0` and the mutex is released with **no call
at all** (`cbz r5, <return>` at 0x80588 — verified this is reached on loop-exhaustion via the `bgt`/fallthrough
at 0x80534/0x80538).

**None of msg_id 2, 0xC, 0xD, 0xF, or 0x14 appear in the 25-entry registered-handler table.** So:

> **Every `usr.app_conf.*` field's "notify" step (the 0x90c20 call, and the direct-dispatch calls for
> microphone/night/timeDisplay/volume) sends a message to `ctrl`'s own inbox with a `msg_id` that has no
> registered handler in this firmware build. The message is silently dropped after an uncontested mutex
> lock/unlock. It is dead code in this build — not a bug Kibble needs to route around, but also not a
> mechanism Kibble should bother reimplementing.** (HIGH confidence — both the sender immediates and the
> complete receiver-side table walk were independently disassembled and cross-checked against each other.)

This *substantially* upgrades `DESIGN-entities.md`'s "poll-based propagation assumed... unconfirmed" language
for most CONFIG-category rows in its §1.3 to **confirmed**: since the only other mechanism live media/ble
would have to learn about a changed `usr.app_conf.*` value is *reading `config_shm` on their own cadence*,
and the explicit push path provably goes nowhere, media/ble **must** be polling — there is no other way they
could ever learn of the change. The **exceptions** — settings whose write path reaches a message with a
msg_id that genuinely **is** registered somewhere (just not in ctrl's own table, since the destination is a
*different* process) — are called out individually in §3: `light`(ledlight, →`media`, msg 0x10), `added`/
`foodWarnRange`/`feed`/timezone (→`ble`, 0x6004/0x600c/0x600d/0x6007), and `capacity` (→`cloud`, 0x2009).
These cross-process sends were **not** re-verified from the receiving binary's own table (out of this task's
binary scope except `ble`'s rodata, checked only for §6) — flagged **UNVERIFIED (receiver side)** below,
except 0x6004 which is separately live-proven by `STUDY-feedtest.md`.

---

## 3. Master settings table

Offsets are **byte offsets into `config_shm`** (the `config_t` struct `STUDY-config.md` maps), read via
`g_config` (a `config_t*` global) exactly as that study describes. All of the offsets below are **new** —
`STUDY-config.md`'s own Table A only gives field *names/types* (pktool's separate, unreachable debug table);
none of its 55 `config_layout.json` entries overlaps this cluster. Every offset here was read directly off a
`str`/`str.w`/`strb.w` instruction, not inferred from adjacency (adjacency is *reported* as a cross-check
where it lines up, which it does almost everywhere — pktool's Table A print-order matches memory order
exactly inside this specific cluster, unlike the `dev`/`usr`/`state` section-level reordering `STUDY-config.md`
found elsewhere).

| Setting (Localkit key) | cJSON key(s) | config_shm offset | Write mechanism | Disasm address | Confidence |
|---|---|---|---|---|---|
| Status LED enable | `light` **(not `lightMode`!)** | 3072 = 0xc00 (u32) | `str.w` then **`dispatch_send_msg(msg_id=0x10, dst=2/media, payload=1)`** — the one confirmed cross-process push in this whole study besides feed/ble-family | write 0x3e920, dispatch 0x3e986 | **HIGH** (send confirmed; media's own receipt of 0x10 not independently re-verified — UNVERIFIED receiver) |
| Status LED *schedule* enable | `lightMode` | 3780 = 0xec4 (u32) | `str.w`, notify via dead `0x90c20`(0x22)→msg2/self (§2.1) | 0x3e2ee | HIGH (write), notify confirmed dead |
| Status LED active hours | `lightMultiRange` | 3784/3788 = 0xec8/0xecc (first `[start,end]` pair; array, `cJSON_GetArraySize`/`GetArrayItem`-style helpers at 0x8bdc4/0x8bdb0/0x8c0f0 iterate further entries, stride not independently confirmed beyond entry 0) | array store loop | 0x3e34e (key), 0x3e39c on. (first store) | MEDIUM (base offset HIGH, stride/count MEDIUM) |
| Camera enable | `camera` | 3080 = 0xc08 (u32) | `str.w`, then **local call `apply_camera_cascade()` @ 0x3c118** (`set_device_sw_mode` debug name) — when new value is `0`, clears `moveDetection`(3464)/`petDetection`(3520)/`eatDetection`(3576) to 0, sets a `state`-ish sentinel byte at config offset 9936 (0x26d0)=1, and calls two local stop-algo functions (0x4b94c, 0x45a04); the enable (`!=0`) branch clears the same sentinel and calls two local start-algo functions (0x24394, 0x25fd8). No `dispatch_send_msg` inside 0x3c118 itself — if media/ble need to react, they poll. Also: when turning ON, checks `access()`/creates a directory via `mkdir` PLT calls (0x15394/0x15130) for picture/video storage. | write 0x3e87e, cascade call 0x3e8fa | **HIGH** |
| Microphone enable | `microphone` | 3076 = 0xc04 (u32) | `str.w`, then **direct `dispatch_send_msg(msg_id=0xD, dst=1/self)`** — dead (§2.1) | write 0x3e9b2, dispatch 0x3ea18 | HIGH (write); notify confirmed dead |
| Night vision (IR) enable | `night` | 3064 = 0xbf8 (u32) | `str.w`, then **direct `dispatch_send_msg(msg_id=0xF, dst=1/self)`** — dead (§2.1) | write 0x3f6fc, dispatch 0x3f762 | HIGH (write); notify confirmed dead |
| Video timestamp overlay | `timeDisplay` | 3068 = 0xbfc (u32) | `str.w`, then **direct `dispatch_send_msg(msg_id=0xC, dst=1/self)`** — dead | write 0x3f78e, dispatch 0x3f7f4 | HIGH (write); notify confirmed dead |
| Speaker volume | `volume` | **3752 = 0xea8 (u32)** — NOT present anywhere in pktool's 228-entry Table A/`config_layout.json`; fills exactly the gap `DESIGN-entities.md` flagged ("not individually named in Table A") | `str.w`, then **direct `dispatch_send_msg(msg_id=0x14, dst=1/self, len=4)`** — dead | write 0x3ed56, dispatch 0x3edae | **HIGH — new field location, resolves a prior open question** |
| Selected notification sound | `selectedSound` | 3756 = 0xeac (u32) | `str.w`, dead-notify via 0x90c20 | ~0x3edb8 area (bulk-scanned) | HIGH |
| Feed voice prompt | `soundEnable` | 3740 = 0xe9c (u32) | `str.w`, dead-notify via 0x90c20 | 0x3ebc4 area | HIGH |
| System guidance voice | `systemSoundEnable` | 3744 = 0xea0 (u32) | `str.w`, dead-notify via 0x90c20 | 0x3ec40 area | HIGH |
| Sound on feed complete | `feedSound` | 3748 = 0xea4 (u32) | `str.w`, dead-notify via 0x90c20 | 0x3ecbc area | HIGH |
| Child lock | `manualLock` | 3872 = 0xf20 (u32) | `str.w`, notify via 0x90c20 with **category 0x23** (the only field observed using a category other than 0x22/0x2a/0x2c — still routes to the same dead msg2/dst1) | write 0x3e6ae, notify-call 0x3e71a | HIGH |
| Do-not-disturb (mute) | `toneMode` | 3824 = 0xef0 (u32) | `str.w`, then **`bl 0x8e068`** (a local "recompute DND-active-now" helper, not independently traced — plausibly what updates `state.dev_pro.toneTimeAllow`, `DESIGN-entities.md` §1.3) — then dead-notify via 0x90c20 | write 0x3e41e, helper call 0x3e478 | HIGH (write + helper call exist); helper's own body MEDIUM/untraced |
| DND hours | `toneMultiRange` | 3828/3832 = 0xef4/0xef8 (first pair) | array store loop, same shape as `lightMultiRange` | 0x3e482 (key) | MEDIUM |
| Feed photo capture | `feedPicture` | 3732 = 0xe94 (u32) | `str.w`, dead-notify via 0x90c20 | 0x3eab4 area | HIGH |
| Eat-detection video clip | `eatVideo` | 3736 = 0xe98 (u32) | `str.w`, then `blx access`(0x15394)/`blx mkdir`(0x15130) directory-prep pattern (same idiom as `camera`) — no dispatch call observed in the traced window | 0x3eb2e area | HIGH |
| Motion detection enable | `moveDetection` | 3464 = 0xd88 (u8, `strb.w`) | `strb.w` (boolean coerced: `adds r3,#0; it ne; movne r3,#1`), dead-notify | 0x3da40 | HIGH |
| *(adjacent, not independently keyed)* `move_det.trackEnable` | — (no distinct JSON key found) | 3465 = 0xd89 (u8) | Same shape, immediately follows `moveDetection` in code — likely always driven together, not separately cloud-settable | 0x3daea | MEDIUM |
| Motion sensitivity (1–9) | `moveSensitivity` | 3468 = 0xd8c (u32) | `str.w`, then **`bl 0x8db8c`** — confirms `DESIGN-entities.md`'s `petkit_modify_algo_threshold` hypothesis structurally: this is a **per-sensitivity-value switch** (`cmp r0,#3`/`cmp r0,#2`/… chain) that writes a **fixed 3-value threshold triple** to config_shm offsets **2928/2932/2936 (0xb70/0xb74/0xb78)** per level (only the `sensitivity==3` and `==2` branches were read in full — enough to prove the mechanism, not all 9 levels). No `dispatch_send_msg` anywhere in this function — pure config_shm write, confirming poll-based consumption. | sensitivity write 0x3db66, mapper 0x8db8c | **HIGH mechanism; MEDIUM only 2/9 levels' exact constants read** |
| Pet detection enable | `petDetection` | 3520 = 0xdc0 (u8) | `strb.w`, dead-notify | 0x3dae4 (approx, bulk-scanned) | HIGH |
| Pet sensitivity (1–9) | `petSensitivity` | 3524 = 0xdc4 (u32) | `str.w`, then `bl 0x8de98` — own analogous mapper function (same architecture as moveSensitivity's 0x8db8c, not independently disassembled) | 0x3dd5c area | MEDIUM (offset+call HIGH, mapper body not re-traced) |
| Eat detection enable | `eatDetection` | 3576 = 0xdf8 (u8) | `strb.w`, dead-notify | 0x3dd54 area | HIGH |
| Eat sensitivity (1–9) | `eatSensitivity` | 3580 = 0xdfc (u32) | `str.w` | 0x3de2c area | HIGH (offset); mapper fn not located/traced |
| Vomit detection enable | `vomitDetection` | 3632 = 0xe30 (u8, `strb.w`) | `strb.w`, dead-notify via 0x90c20 | 0x3f652 area | HIGH — **no separate `vomitSensitivity` field exists** (absent from pktool Table A and from this function; enable-only, cross-checked two independent ways) |
| Detection interval (global) | `detectInterval` | 3688 = 0xe68 (u32) | `str.w` | 0x3de96 area | HIGH |
| Detection active hours | `detectMultiRange` | 3692/3696 = 0xe6c/0xe70 (first pair) | array store loop | 0x3df00 (key) | MEDIUM |
| Low-food warning | `foodWarn` | 3768 = 0xeb8 (u32) | `str.w` | 0x3c686 | HIGH |
| Low-food warning hours | `foodWarnRange` | base not independently pinned (array-store uses register+register addressing `str r3,[r2,r1]`, not caught by the immediate-offset scan used for the rest of this table) | array loop, **then a direct `dispatch_send_msg(msg_id=0x600C, dst=8/ble, ...)`** — confirmed for the empty-array (`[]`) case (`len=0` payload); the non-empty-array branch (0x3c6d8) was not traced | dispatch 0x3c6cc | HIGH (dispatch exists, dst/msgid); LOW (exact stored offset) |
| Hopper 1 calibration factor | `factor1` **or bare `factor` (alias)** | **3760 = 0xeb0** (u32) | `str.w` — resolves an earlier apparent anomaly: both the `"factor"` and `"factor1"` JSON keys funnel into the *same* write site (0x3c5f2), i.e. `"factor"` is a shorthand alias specifically for hopper 1 | write 0x3c5f2 | **HIGH** (fully resolved — see §3.1 below) |
| Hopper 2 calibration factor | `factor2` | 3764 = 0xeb4 (u32) | `str.w` | write 0x3c478 | HIGH |
| Leftover-food state | `surplusControl` | 3880 = 0xf28 (u32) | `str.w`, dead-notify | 0x3f4c6 area | HIGH |
| Leftover-food threshold | `surplusStandard` | 3884 = 0xf2c (u32) | `str.w`, dead-notify | 0x3f548 area | HIGH |
| Pet auto-tracking | `smartFrame` | 3888 = 0xf30 (u8, `strb.w`) | `strb.w`, dead-notify | 0x3f5ca area | HIGH |
| Schedule last-modified | `CTime` | 3876 = 0xf24 (u32) | `str.w`, notify via 0x90c20 category **0x23** (same non-default category as `manualLock`) | 0x3e71e area | HIGH |
| Cloud storage capacity | `capacity` | not found as a scalar write (it's an array-of-objects *report* field, `fullVideo`/`eventImage`/… — matches `DESIGN-entities.md`'s own "deliberately not exposed, cloud-only" call) | n/a for write; **but** `dispatch_send_msg(msg_id=0x2009, dst=4/cloud)` fires from this same key's handling — a read/report path, not a settings write | 0x400da | N/A for Kibble (cloud-only artifact, confirmed) |
| Device local timezone (bonus, not in the 47-key list but found in the same function) | (float field, `%.2f` format) | not resolved to an offset in this pass (float compare/store not scanned by the int/string-only heuristic used for the bulk table) | `dispatch_send_msg(msg_id=0x6007, dst=8/ble)` immediately followed by a **direct call to `config_save()`** — see §4 | dispatch+save 0x406d2–0x406de | HIGH (dispatch); offset not pinned |

### 3.1 `factor` / `factor1` / `factor2` — fully resolved

Disassembly of 0x3c432–0x3c65a: the handler checks JSON key `"factor"` first; if present, treats it as
`factor1`'s value (branches to a shared write site at **0x3c5e0** that stores to **offset 0xeb0**). If absent,
it checks `"factor1"` explicitly — same shared write site, same offset. Either way, execution then falls
through unconditionally to check `"factor2"` independently, writing **offset 0xeb4** if present. No
`dispatch_send_msg` for either factor — pure config_shm writes, matching `DESIGN-entities.md`'s guess that
calibration is presumed-poll-based, now confirmed.

---

## 4. Config persistence — the answer to "does Kibble need to keep `ctrl` alive to write settings"

**Function:** `config_save(void *section_ptr)` at **`ctrl` 0x870d0** (0x87ca8 end, one `push.w`/`pop.w` pair,
~3KB of code). Internally reaches (and was fully disassembled through) a stage the binary itself labels
`usr_config_save` (debug banner `"-------- usr_config_save --------"`, rodata 0xbd861, hit at text 0x87a28).

**Callers:** 20 distinct call sites across `ctrl` (0x200ce, 0x204d4, 0x220fc, 0x23ec0, 0x30b5a, 0x406de,
0x40bfa, 0x40c92, 0x40d88, 0x425dc, 0x44c7a, 0x44ea8, 0x452d6, 0x45908, 0x45f3c, 0x48980, 0x49254, 0x4d5ea,
0x516fe, 0x88be6) — called with `r0 = &g_config_struct[+8]` at the one traced site (0x406da/0x406de), i.e. a
**pointer into the live `config_shm` struct itself**, not a boolean/enum selector; consistent with `config_save`
being reused for both the `usr.*` and `dev.*` sections by passing a pointer to whichever section's data.
**Every one of these 20 call sites is a direct, synchronous function call inside `ctrl`'s own property-set
processing code — none of them is reached via a bus message.** (Cross-checked against §2.1's finding: nothing
in the settings-write path sends a message whose destination is registered to trigger a save; persistence is
wired as an ordinary function call, full stop.)

**Disassembly-confirmed save sequence** (0x87a36–0x87bec, i.e. the `usr_config_save` stage):

1. `open("/tmp/config.lock", O_CREAT|O_RDWR (0x42), 0644)` — **0x87a42** (`blx open` @ PLT 0x15304)
2. `flock(lock_fd, LOCK_EX)` — **0x87a4a** (`blx flock` @ PLT 0x15078, `r1=2`=`LOCK_EX`)
3. Content is prepared by a helper (**0x83320**, called from 0x879e4) that computes an **MD5 digest**
   (`MD5_Init`@0x15358 → `MD5_Update`@0x156ec → `MD5_Final`@0x151a8) of the payload and **hex-encodes it to a
   32-ASCII-character string** (nibble-split loop @ 0x83370–0x83396) — this is the 32-byte header.
4. `fopen("/opt/user.conf", "wb")` — **0x87ab2** (path string confirmed at rodata 0x9f296, mode string `"wb"`
   at 0xae796)
5. `fwrite(md5_hex_header, size=32, nmemb=1, fp)` — **0x87ac6**
6. `fwrite(content_buf, size=content_len, nmemb=1, fp)` — **0x87b88** (the actual, presumably AES-encrypted —
   per `STUDY-config.md` §3's independently-confirmed `AES_set_encrypt_key`/`AES_cbc_encrypt` imports in this
   same binary, at 0x81e98/0x81f28/0x82018, not re-verified as being *this specific* call chain in this pass —
   MEDIUM on "AES-encrypted" specifically, HIGH on everything else in this sequence) config content
7. finalize/flush call — **0x87bd8** (`bl 0x82148`, not traced)
8. `fclose(fp)` — **0x87bde**
9. `flock(lock_fd, LOCK_UN)` — **0x87be6** (`r1=8`=`LOCK_UN`)
10. `close(lock_fd)` — **0x87bec**

**A near-identical inline "open lockfile, `movs r1,#0x42` open-flags, `flock(fd, LOCK_EX)`" sequence recurs at
0x833bc/0x833d0/0x833de** (a different, apparently generic locked-write helper reached from the same
MD5-computation function's caller chain) — the `/tmp/config.lock` + `LOCK_EX`/`LOCK_UN` convention is used
**consistently, not just this once**, reinforcing that this is `ctrl`'s standard config-write discipline
project-wide, not a one-off.

### 4.1 What this means for Kibble

- **Confirmed: writing a setting and persisting it to flash are two separate, both-synchronous steps inside
  `ctrl`'s own property-set code path — neither is reachable by sending `ctrl` a bus message.** There is no
  "please save now" message `ctrl` listens for (§2.1 already proved the low message-id space is a dead
  letter box in this build).
- **If Kibble wants settings to survive a reboot without keeping stock `ctrl` alive as the config owner**, it
  must either (a) reimplement the exact `/tmp/config.lock`(LOCK_EX) → MD5-hex-header → (AES-encrypted?) content
  → `/opt/user.conf` `fwrite` → `LOCK_UN` sequence itself — which requires recovering the AES key/IV (not
  attempted in this pass; `STUDY-config.md` §3 already flags this as unresolved), or (b) **keep `ctrl` (and
  only `ctrl`) alive** and feed it a synthesized `property_set` JSON payload through one of its two confirmed
  local entry points (§5.2) so that `ctrl`'s own, already-working save code runs unmodified.
- Simply `mmap`-writing `/dev/shm/config_shm` directly (bypassing `ctrl` entirely, as `STUDY-config.md` §5
  describes for read-only polling) **would work for the in-memory value immediately** (every consumer reads
  live `config_shm`, confirmed by the dead-notify finding in §2.1 — polling is the *only* propagation path
  that exists) **but would never be persisted to `/opt/user.conf`**, meaning it reverts on the next reboot or
  on `ctrl`'s own next unrelated `config_save()` call (which would overwrite it back from its still-stale
  in-memory copy the moment it next saves for an unrelated reason) — this option is a dead end unless Kibble
  also reimplements persistence per the paragraph above.
- **Update 2026-09-15, live-confirmed (`21-config-encryption.md`):** option (a) above is now known
  to be blocked, not just AES-key-shaped speculation — a live pull of `/opt/user.conf` measures its
  content at 7.915/8.0 bits/byte Shannon entropy with zero byte-level correspondence to `config_shm`
  at any offset (checked down to 4-byte windows for values already confirmed correct, e.g. `volume`'s
  live value of 6 matching this document's own §3 cited ground truth). Option (b)'s local entry point
  remains unconfirmed-reachable (§5.3) and was not pursued either, given the risk of malformed input
  reaching a live vendor process. **Kibble's actual answer is neither (a) nor (b):** write
  `config_shm` directly (this paragraph's own finding that the value takes effect immediately still
  holds) plus keep Kibble's own plaintext desired-state record (`agent/src/desired.rs`,
  `/opt/kibble/settings.json`) and a reconciler that continuously re-applies it, so a value that
  reverts is corrected rather than lost — see `21-config-encryption.md` §5 and `agent/src/persist.rs`.

---

## 5. Other findings directly requested

### 5.1 Desiccant reset

**Confirmed absent — a stronger result than `DESIGN-entities.md`'s earlier "NOT LOCATED".** That document's
own search covered `ctrl`'s and `pktool`'s string tables. This pass additionally searched **all of `ble`'s
`.rodata`** (2117 strings extracted, same method as `ctrl`) — `ble` is the process that owns the T31 MCU UART
link and would be the natural place for a desiccant/humidity sensor reading if one existed anywhere in this
firmware build. Case-insensitive search for `desic`/`dry`/`moistur`/`干燥`/`gan` across **both** `ctrl`'s full
6237-string table and `ble`'s full 2117-string table: **zero matches, in either binary.** Nothing named
`resetDesiccant`/`dryer`/`dry_box` exists as a symbol, debug string, or JSON-key-shaped literal anywhere this
study could reach. This is now a well-evidenced negative result, not a gap — needed next step (per
`DESIGN-entities.md` §7.1, unchanged by this pass): a live diff, or accept the capability doesn't exist on
this specific hardware/firmware combination despite existing on other Petkit feeder models.

### 5.2 Food-replenished

**Found — cloud command `"added"`** (rodata 0xaa629, sibling string immediately after `feed_realtime_cancel`
in the command block, §1.1). Disassembly (0x441f6–0x44222):

    strcmp(incoming_action, "added")            ; @0x44200 — string resolved via LDR2TARGET[0x441f6]
    if (match) {
        item = cJSON_GetObjectItem(root, "added")     ; @0x4420a — re-fetches its OWN key's value
        if (item) {
            payload[0] = item->valueint            ; 1-byte payload
            dispatch_send_msg(msg_id=0x600D, dst=8/ble, payload, len=1)   ; @0x4421e/0x4414c(shared tail)
        }
    }

`0x600D` sits in the same immediate family as the live-proven `0x6004` (feed) — both go to `dst=8`/ble. **This
is the mechanism to fire when a user (or Kibble's own hopper-fill sensor logic) confirms food was manually
added to a hopper** — HIGH confidence on the dispatch (msg_id/dst/payload-shape all read directly from
immediates), UNVERIFIED on `ble`'s own receipt (would need `ble`'s registration table disassembled the same
way §2.1 did for `ctrl`'s, not attempted here — out of this task's binary-copy scope beyond the string search
in §5.1).

### 5.3 Local (non-cloud) property_set entry point — flagged, not chased

Two distinct entry symbols exist for `property_set`: `iot_property_set_recv_parse` (the cloud/MQTT path) and
**`web_property_set_recv_parse`** / **`on_web_recv_property_set`** (rodata 0x97f4d/0x97f98/0xa621f — the debug
banner `"============ web save property_set conf ============="` at 0xaad8d is this path's own "I'm about to
persist" line, distinct from `"============ iot save property_set conf ============="` at 0xa7130 for the
cloud path). **This directly contradicts `DESIGN-entities.md`'s "there is no local HTTP/mDNS control surface
in the shipped firmware at all" claim** (§8.1 of that document) — at minimum the *code path* for a local,
non-cloud settings-write entry point exists and is reachable code (its own debug logging fires). Whether it's
actually wired to a listening HTTP server, and on what port/only during initial WiFi-setup mode, was **not**
investigated in this pass (out of scope for a settings-write-mechanism study, but worth flagging loudly: if
real and always-listening, this could be `config_save()`'s "reachable without keeping cloud-`ctrl` alive"
answer for §4.1, since it's the same `ctrl` process either way).

---

## 6. Schedule — handed off to `ScheduleWrite` via hub

Not this task's assignment, but surfaced along the way and forwarded live: `parse_recv_property_set_feed_param`
(§2) handles a `"feed"` JSON key that is a ≤39-char **string** (length-validated at 0x4799c against both the
literal string length and a caller-supplied length argument, `cmp r7,#0x27` — 0x27=39) — this looks like a
feed/schedule-entry **id reference**, not the schedule array payload itself, and is a different thing from
`DESIGN-entities.md`'s Tier-1 queue item #1 (`dispatch_handler_ble_set_schedule`'s outbound wire format).
Also noted for them: the source file is literally named **`server_cmd_parse.c`** (rodata 0xa7005), and msg_id
`0x101a` (`dispatch_handler_ble_get_schedule`) is registered **inbound to `ctrl`** in the same 25-entry table
this study fully enumerated (§2.1) — i.e. it's `ctrl` *receiving* a schedule-get, not sending a schedule-set,
consistent with what `DESIGN-entities.md` already had. The msg-dispatch-table-walk technique in §2.1 (how to
tell a *real, live* registered handler from a dead self-message) is directly reusable for finding the
schedule-set outbound msg_id if it's a `ctrl→ble` send analogous to `0x6004`/`0x600C`/`0x600D`.

---

## 7. Still unknown / next steps

1. **`lightMultiRange`/`toneMultiRange`/`detectMultiRange`/`cameraMultiRange` exact array stride and max
   count beyond the first `[start,end]` pair.** Base offset of entry 0 is HIGH confidence for all four; the
   iteration helpers (`bl 0x8bdc4`, `bl 0x8bdb0`, `bl 0x8c0f0`) were identified by call-site behavior
   (cJSON-array-shaped signatures) but not individually disassembled to confirm the per-entry stride (very
   likely 8 bytes, `[start,end]` as two `int`s, matching entry-0's 4-byte gap) or the maximum entry count the
   struct allocates for. **Next step:** disassemble 0x8bdc4/0x8bdb0/0x8c0f0 directly (all three are short,
   local, statically-linked cJSON-array helpers — same technique as everything else in this study).
2. **`foodWarnRange`'s exact base offset** — the array-store instructions use register+register addressing
   (`str r3, [r2, r1]`) rather than the register+immediate form the offset-scan in this study was built
   around; the *dispatch* (msg 0x600C→ble) is solid, the *storage offset* is not pinned. **Next step:** a
   small, targeted re-read of 0x3c6d8 onward (the non-empty-array branch, not traced in this pass) with the
   register-offset form added to the scanner.
3. **`petSensitivity`/`eatSensitivity`'s threshold-mapping functions** (analogous to `moveSensitivity`'s
   fully-traced `0x8db8c`) exist at `0x8de98`(pet) and were not independently located for `eat`. **Next
   step:** same technique as §3's `moveSensitivity` row, applied to these two addresses.
4. **`toneMode`'s `bl 0x8e068` helper body** (presumably recomputes `state.dev_pro.toneTimeAllow`) — call
   site confirmed, body not disassembled.
5. **AES key/IV for `/opt/user.conf`/`/opt/dev.conf`** — needed only if Kibble chooses option (a) in §4.1
   (reimplement persistence itself rather than keeping `ctrl` alive). `STUDY-config.md` §3 already flags this
   as open; this pass did not attempt it (would need to trace `AES_set_encrypt_key`'s key-material argument
   back to its source, at minimum 0x81e98 and whatever calls it). **Update 2026-09-15:** Kibble did not end
   up needing this — `21-config-encryption.md` confirms the content really is encrypted (closing the "MEDIUM
   confidence" hedge this document's own §4 step 6 left open) and Kibble's settings feature ships against
   its own `/opt/kibble/settings.json` desired-state store instead of the vendor's encrypted file. This item
   stays open only for a future feature that genuinely needs to write through the vendor's own persistence.
6. **`web_property_set_recv_parse`'s actual reachability** (§5.3) — is there a live-listening local HTTP
   server outside of initial-WiFi-setup mode? This would change the answer to "must Kibble keep cloud
   connectivity, or just `ctrl` itself, alive for settings writes" and deserves a follow-up pass in `ctrl`'s
   network/socket-bringup code, independent of this settings-write study.
7. **`power`/`start_action`/`stop_action`/`end_action`/`reset_pet_action`/`lapse_action`/`discern_get`/
   `privacy_action`/`ble_relay_update` command semantics** (§1.1) — dispatch sites are all HIGH confidence,
   payload semantics were deliberately not chased (none of these are *settings* in the 47-key sense the
   assignment scoped to; camera/agora/pairing session actions).

---

## Appendix A — verbatim rodata dump, 0xaa400–0xab000 (as literally requested by the assignment)

```
0xaa406 [0mstart_action = %d
0xaa41c [%s][%s][%s][%d]: key is %d, now adjusting
0xaa449 [0mD[%ld][%s][%s:%d]
0xaa45e [0mkey is %d, now adjusting
0xaa47b stop_action
0xaa487 [%s][%s][%s][%d]: stop_action = %d
0xaa4ac [0mD[%ld][%s][%s:%d]
0xaa4c1 [0mstop_action = %d
0xaa4d6 end_action
0xaa4e1 [%s][%s][%s][%d]: end_action = %d
0xaa505 [0mD[%ld][%s][%s:%d]
0xaa51a [0mend_action = %d
0xaa52e reset_pet
0xaa538 reset_pet_action
0xaa549 [%s][%s][%s][%d]: reset_pet_action = %d
0xaa573 [0mD[%ld][%s][%s:%d]
0xaa588 [0mreset_pet_action = %d
0xaa5a2 lapse
0xaa5a8 lapse_action
0xaa5b5 [%s][%s][%s][%d]: lapse_action = %d
0xaa5db [0mD[%ld][%s][%s:%d]
0xaa5f0 [0mlapse_action = %d
0xaa606 feed_realtime
0xaa614 feed_realtime_cancel
0xaa629 added
0xaa62f play_sound
0xaa63a soundId
0xaa642 [%s][%s][%s][%d]: ------------id=%d-----------
0xaa673 [0mD[%ld][%s][%s:%d]
0xaa688 [0m------------id=%d-----------
0xaa6a9 start_live
0xaa6b4 start_rtm
0xaa6be rtcToken
0xaa6c7 channelId
0xaa6d1 definition
0xaa6dc rtmToken
0xaa6e5 appRtmUserId
0xaa6f2 devRtmUserId
0xaa6ff [%s][%s][%s][%d]: --- start_live start agora ---
0xaa732 [0mD[%ld][%s][%s:%d]
0xaa747 [0m--- start_live start agora ---
0xaa76a [%s][%s][%s][%d]: --- start_rtm start agora ---
0xaa79c [0mD[%ld][%s][%s:%d]
0xaa7b1 [0m--- start_rtm start agora ---
0xaa7d3 open_camera
0xaa7df [%s][%s][%s][%d]: ------------open_camera duration=%d(s)-----------
0xaa825 [0mD[%ld][%s][%s:%d]
0xaa83a [0m------------open_camera duration=%d(s)-----------
0xaa870 ble_relay_update
0xaa881 update_action
0xaa88f [%s][%s][%s][%d]: ------------ble_relay_update action=%d(s)-----------
0xaa8d8 [0mD[%ld][%s][%s:%d]
0xaa8ed [0m------------ble_relay_update action=%d(s)-----------
0xaa926 connect_action
0xaa935 [%s][%s][%s][%d]: ------------ble relay connect action=%d(s)-----------
0xaa97f [0mD[%ld][%s][%s:%d]
0xaa994 [0m------------ble relay connect action=%d(s)-----------
0xaa9ce discern_get
0xaa9da [%s][%s][%s][%d]: ------------discern_get=%d(s)-----------
0xaaa17 [0mD[%ld][%s][%s:%d]
0xaaa2c [0m------------discern_get=%d(s)-----------
0xaaa59 privacy
0xaaa61 [%s][%s][%s][%d]: -------get privacy str--------
0xaaa94 [0mD[%ld][%s][%s:%d]
0xaaaa9 [0m-------get privacy str--------
0xaaacc privacy_action
0xaaadb [%s][%s][%s][%d]: get privacy_action: (%d)
0xaab08 [0mD[%ld][%s][%s:%d]
0xaab1d [0mget privacy_action: (%d)
0xaab3a [%s][%s][%s][%d]: ----------- device_sw set (%d) -----------
0xaab79 [0mD[%ld][%s][%s:%d]
0xaab8e [0m----------- device_sw set (%d) -----------
0xaabbd [%s][%s][%s][%d]: close algo mv:%d, pet:%d, eat:%d
0xaabf2 [0mD[%ld][%s][%s:%d]
0xaac07 [0mclose algo mv:%d, pet:%d, eat:%d
0xaac2c [%s][%s][%s][%d]: open algo mv:%d, pet:%d, eat:%d
0xaac60 [0mD[%ld][%s][%s:%d]
0xaac75 [0mopen algo mv:%d, pet:%d, eat:%d
0xaac99 /tmp/attire/osdAttire.rgba
0xaacb4 /app/etc/defAttire.tar.gz
0xaacce [%s][%s][%s][%d]: init_extract_attire_file err
0xaacfe [31mE[%ld][%s][%s:%d]
0xaad14 [0minit_extract_attire_file err
0xaad34 /opt/osdAttire.tar.gz
0xaad4a [%s][%s][%s][%d]: payload:%s
0xaad69 [0mD[%ld][%s][%s:%d]
0xaad7e [0mpayload:%s
0xaad8d [%s][%s][%s][%d]: ============ web save property_set conf =============
0xaadd7 [0mD[%ld][%s][%s:%d]
0xaadec [0m============ web save property_set conf =============
0xaae26 ctrl_listen_run
0xaae36 [%s][%s][%s][%d]: gpio_listener thread create error
0xaae6b gpio_ctrl.c
0xaae78 [31mE[%ld][%s][%s:%d]
0xaae8e [0mgpio_listener thread create error
0xaaebb ble_parse_language
0xaaece ble_parse_change_wifi
0xaaee4 ble_pack_got_wifi_ask
0xaaefa ble_pack_devinfo
0xaaf0b ble_pack_WIFIinfo
0xaaf1d ble_pack_net_stat
0xaaf2f pk_ble_ctrl_open_green_led
0xaaf4a pk_ble_ctrl_close_green_led
0xaaf66 pk_led_white_manage
0xaaf7a pk_hmi_sta_rpt_mg_judge_need_report
0xaaf9e send_got_wifi_stat_to_ble
0xaafb8 send_sn_mac_to_ble
0xaafcb send_net_stat_to_ble
0xaafe0 send_wifi_info_to_ble
0xaaff6 dispatch_handler_ble_key_change_wifi
```

(0xaa33a–0xaa400 — the `power`/`power_action` head of this same block — and every string in the *preceding*
settings-write block 0xa9200–0xaa33a are the ~29 `"===== set <key> (%d) ====="` lines already itemised in §3;
not re-listed here to avoid duplicating the master table.)

## Appendix B — tooling

Local scratch: `/tmp/kibble-settings/` (binaries copied via `tailscale ssh root@beastnas 'cat <path>'`,
analysis done with `pyelftools`==0.33 + `capstone`==5.0.7 already present in this workspace's Python — no
new packages installed, nothing written back to the device, no live contact). Not persisted anywhere outside
this deliverable and the session's own working directory.
