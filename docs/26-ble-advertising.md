# BLE advertising: the lever, its wire bytes, and one negative-inconclusive live test

Static analysis: disassembly of `pktool` (280,776 B, md5 `0cce31adf4c23cae370d8e1a075f73fa`),
`ble` (198,732 B, md5 `133ee0b50aecf9419ac64d0c150c8de5`) and `ctrl` (742,148 B) pulled live off
192.168.4.85 on 2026-09-15 (identical sizes/role to the copies `docs/08-mcu.md`/`09-ble.md`
already analyzed). Method: `pyelftools` for ELF layout, `capstone` (Thumb-2) with a resilient
linear sweep (resync +2 bytes past any undecodable run — necessary because both binaries embed
literal pools inside `.text`) plus the same PC-relative `ldr rX,[pc,#N]` (+ optional `add
rX,pc`) literal resolver `09-ble.md` used, extended to also resolve **32-bit** `ldr.w`
encodings (the earlier pass only matched plain 16-bit `ldr`, which silently missed every
`pktool` string reference — worth a note for whoever picks this method up next). One live test:
a single on/off cycle of the lever below, approved and directed step-by-step by `Main`; see §4.

---

## TL;DR

| Question | Answer |
|---|---|
| Lever | Bus message **`msg_id 0x6001`**, `dst=8` (`ble`'s queue), 4-byte payload `{enable: u8, 0, 0, 0}`. Byte-identical whether sent by `pktool`'s `bleadv 0\|1`, by `ctrl`'s own pairing flow, or by a bare `mq_send` — three independent senders converge on the exact same wire bytes. |
| `ble`-side handler | `dispatch_handler_ble_set_adv`, `ble` vaddr `0x16b76` (dynsym-confirmed). 24 bytes: reads payload byte 0, forwards to a shared "small setting" wrapper with selector `2`. |
| UART command to the MCU | **CMD `0x09`**, subaddr `2` (frame byte 6 = `0x12`), 6-byte payload `{enable,0,0,0,0,0}`. Resolves `08-mcu.md`'s open item — CMD 0x09 was previously unmatched to any handler; it's a shared command whose subaddr selects *which* simple setting (0=LED, 1=beep, 2=**BLE advertising enable**, 3=IR, 4=food-added, 5=green LED). |
| Reverse (stop advertising) | Same msg_id, same CMD, payload byte 0 = `0`. No separate "stop" command exists or is needed. |
| Lifetime / auto-off | **No timeout on the wire path itself.** `ctrl`'s own pairing flow (`bind_ctrl.c`) layers a disassembly-proven **300-second (`0x12c`)** timeout *on top*, but only for sessions *it* opened (two of its own config flags) — a bare send is invisible to it and will not be auto-expired by any vendor code found. No MCU-internal timeout evidence either way (T31 firmware is TC32, opaque to static tools). |
| Live test | **Negative-inconclusive** (§4). One on/off cycle produced no `Petkit`/`D4SH` name, no `0xAAA0-2` UUID, in Home Assistant's Bluetooth debug log via the plant-room proxy — while that same proxy is proven live (saw an unrelated device mid-window). Does not rule out a bare/unnamed advert, which the test wasn't instrumented to distinguish from ambient noise. |
| `ctrl`-does-more-than-us diff | **No.** Every one of the four places `ctrl` sends `msg_id 0x6001` sends *only* that one message to `ble`; nothing else goes out on the bus around it (§5). If something extra is required, it either lives inside the opaque MCU firmware or in a boot-time-only path already resolved long before any live agent runs (§5.3). |

---

## 1. `pktool`'s `bleadv` subcommand

`pktool` is a single giant `main()`-shaped dispatcher: a long chain of `strcmp(argv[1], "<name>")`
blocks, each ending in a shared cleanup jump (`b #0x128ca`), not a lookup table — confirmed by
disassembly, not inferred (this matches the shape `09-ble.md` already found for `ctrl`'s BLE-type
dispatch). The `"bleadv\0"` string sits at rodata vaddr `0x40f00`; two `strcmp` sites reference it
(`0x12862` — an earlier candidate check unrelated to the actual handler — and `0x1304c`, the real
one). Full handler, `0x1304c`-`0x131cc`:

```
0x1304c  ldr.w r1,[pc,#imm]; mov r0,r7; add r1,pc; blx strcmp      ; strcmp(argv[1], "bleadv")
0x1305a  cmp r0,#0 ; bne.w 0x131cc                                  ; no match -> next command
0x13058  mov r4,r0                                                  ; r4 = 0 (strcmp result on match)
0x13060  cmp.w r8,#2 ; bne 0x130f0                                  ; r8 = argc; argc!=2 -> parse the 0|1 arg
  [argc==2: prints "%s bleadv 0|1\n" usage, b 0x128ca]
0x130f0  ldr.w r1,[pc,#imm]; ...; blx strcmp                        ; strcmp(argv[2], "0")
0x13108  cbnz r0,0x1313e                                             ; argv[2] != "0" -> try "1"
  0x1310a..0x13128  (log "Ble adv to off.\n" if log level enabled)
  0x1312c  movs r3,#4 ; mov r2,r8(&payload) ; movs r1,#8 ; movw r0,#0x6001
  0x13136  str r4,[r7,#-0x10]     ; payload[0] = r4 = 0   (verified: r4 last written at 0x13058,
                                  ; never touched again before this store)
  0x1313a  bl #0x2514c                                               ; dispatch_send_msg(0x6001, 8, &payload, 4)
0x1313e  ldr.w r1,[pc,#imm]; ...; blx strcmp                        ; strcmp(argv[2], "1")
0x1314c  bne.w 0x128ca                                               ; neither "0" nor "1" -> no-op, exit
  0x1315a..0x13190  (log "Ble adv to on.\n" if log level enabled)
  0x13194  movs r3,#1 ; str r3,[r7,#-0x10]                           ; payload[0] = 1
  0x1319c  movs r1,#8 ; movs r3,#4 ; movw r0,#0x6001
  0x131a4  bl #0x2514c                                               ; dispatch_send_msg(0x6001, 8, &payload, 4)
```

`0x2514c` is `pktool`'s own compiled copy of the shared `dispatch_send_msg(msg_id, dst, payload,
len)` primitive `14-feed-test.md` already fully characterized in `ctrl` (`@0x80b00`) — confirmed
structurally identical here too: same `msg_id == 0xFFFF / 0x103` log-skip special case, same
`(dst-1) <= 0x13` range validation (`0x251ce`-`0x251d2`), same per-`dst` cached-`mqd_t` array
indexed by `dst`. `pktool bleadv 0` and `pktool bleadv 1` are confirmed, byte-for-byte, to be
nothing more than `mq_send("/msg_dispatch_8", {0x01,0x60, <src>,<src>, enable,0,0,0}, 8, 0)`.

---

## 2. `ble`'s handler, and the UART command it emits

### 2.1 Finding `msg_id 0x6001` in `ble`'s handler table

`ble` registers its ~30 handlers in one straight-line block, `ble` vaddr `0x12490`-`0x127c0`
(mirrors `09-ble.md §5`'s description of `ctrl`'s own `0x15a70`-`0x15ca0` registration block).
Each entry is `movw r0,#<msg_id>; ldr r2,[pc,#imm]; add r2,pc  ; <name string>; bl
<register_fn>@0x25510`. The **first** entry, `msg_id = 0x6001` (`movw r0,#0x6001` @ `0x12564`),
names itself via the string load at `0x12568`: **`dispatch_handler_ble_set_adv`**. Cross-check:
the fourth entry in the same block is `msg_id=0x6004` → `dispatch_handler_ble_feed_ctrl` — the
already-proven feed msg_id (`14-feed-test.md`) — landing exactly where this same recovery method
says it should, which is the strongest evidence the method (and therefore `0x6001`) is correct.

`register_fn`@`0x25510` stores `{u16 msg_id; u16 pad; void*; char* name}` (12-byte stride,
verified: `movs r0,#0xc` @`0x255a0`) into a table; the third field's value at registration time is
read through an *unresolved* `.got`-relative load (file value `0`, confirmed by direct byte read
at the computed slot) — because the handler functions are themselves `.dynsym`-exported (`STT_FUNC,
STB_GLOBAL`, confirmed for every `dispatch_handler_*` name), filled in by the dynamic linker at
process start, not statically at rest in the file. The ELF's own dynamic symbol table is therefore
the authoritative, no-runtime-needed source for every handler's real address — used throughout
this document instead of chasing the GOT indirection further:

```
dispatch_handler_ble_set_adv   0x16b77   (thumb; code at 0x16b76)
dispatch_handler_ble_set_led   0x16b8f
dispatch_handler_ble_feed_ctrl 0x16ecd
```

### 2.2 `dispatch_handler_ble_set_adv`, `ble` vaddr `0x16b76`-`0x16b8d` (24 bytes, complete)

```
0x16b76  push {r0, r1, r4, lr}
0x16b78  movs r4, #0
0x16b7a  ldrb r1, [r2]          ; r1 = payload[0]  (r2 = this handler's 3rd arg = the msg payload)
0x16b7c  mov r3, r4             ; r3 = 0
0x16b7e  mov r2, r4             ; r2 = 0
0x16b80  movs r0, #2            ; r0 = 2  (selector -- see §2.3)
0x16b82  str r4, [sp]           ; 5th (stack) arg = 0
0x16b84  bl #0x17a90
0x16b88  mov r0, r4 ; add sp,#8 ; pop {r4, pc}     ; returns 0 unconditionally
```

No condition, no state check, no reference to any config flag — a pure one-byte relay. This is
the complete handler body; there is nothing else it does.

### 2.3 The shared wrapper, `ble` vaddr `0x17a90`, and CMD 0x09's real shape

`0x17a90` is called from **seven** sites, not just `set_adv`:

| Caller | its `r0` into the wrapper (= UART frame's subaddr nibble) |
|---|---|
| `dispatch_handler_ble_set_led` (`0x16b8e`) | `0` |
| `dispatch_handler_ble_set_beep` (`0x16bc1`) | `1` |
| **`dispatch_handler_ble_set_adv` (`0x16b76`)** | **`2`** |
| `dispatch_handler_ble_set_ir` (`0x16c8d`) | `3` |
| `dispatch_handler_ble_set_food_added` (`0x16d79`) | `4` |
| `dispatch_handler_ble_set_green_led` (`0x16ba7`) | `5` |
| one more caller at `0x1359e`, inside a boot-time init routine — see §3 | `2` (same as adv) |

So `0x09` is not "the advertising command" — it's a shared **"simple small setting"** UART
command whose *subaddress* nibble picks the setting, and BLE-advertising-enable happens to be
setting `2`. Disassembly of the wrapper (each caller passes its selector in `r0`, on/off byte in
`r1`):

```
0x17a90  push {r0,r1,r2,r3,r4,lr}
0x17a92  strh.w r3,[sp,#0xa]        ; buf+4:5 (u16) = caller's r3 (0)
0x17a9a  strb.w r1,[sp,#8]          ; buf+0 = caller's r1 = the on/off byte
0x17a9e  movs r1,#0                 ; flag_bit6 = 0  (frame_builder's 2nd arg)
0x17aa0  strb.w r2,[sp,#9]          ; buf+1 = caller's r2 (0)
0x17aa4  uxtb r2,r0                 ; subaddr_nibble = caller's r0 (2 for adv)
0x17aa6  strh.w r3,[sp,#0xc]        ; buf+4 already written above; redundant 2nd store, same value
0x17aaa  movs r3,#6 ; str r3,[sp]   ; len = 6 (frame_builder's 5th/stack arg)
0x17aae  movs r0,#9                 ; **CMD = 9**
0x17ab0  add r3,sp,#8               ; payload = &buf
0x17ab2  bl #0x16970                ; build_and_send_uart_frame(cmd=9, flag_bit6=0, subaddr=2, &buf, 6)
```

`0x16970` is the exact `build_and_send_uart_frame` `09-ble.md §2.1` already fully disassembled
(`frame[6] = subaddr | 0x10 | (flag_bit6<<6)`). With `subaddr=2, flag_bit6=0`: `frame[6] =
0x12`. Final wire frame (payload `enable` = the byte from the bus message):

```
5A A5  0F 00  09  <seq>  12  <enable> 00 00 00 00 00  <crc16 lo> <crc16 hi>
```

This resolves `08-mcu.md §4`'s open item directly: CMD `0x09` was listed there as "not matched to
a named handler in this pass" — it now has one, and turns out to be a small family of six.

---

## 3. Is there an "always advertise" mode, and what turns it off? (lifetime semantics)

**No MCU-side or `ble`-side timer exists on this specific command.** `dispatch_handler_ble_set_adv`
(§2.2) is stateless and unconditional — it does not arm anything, and nothing in `ble` polls a
"how long has adv been on" clock for this msg_id. The *only* timeout found anywhere in the vendor
software belongs to `ctrl`'s own pairing/binding state machine, and it does not automatically cover
a message that didn't come from that state machine.

### 3.1 `ctrl`'s pairing flow (`bind_ctrl.c`) — the reference implementation

Four `ctrl` call sites send `msg_id 0x6001` (`bl dispatch_send_msg@0x80b00`, all `dst=8`):

| Site | Payload | Function | Trigger |
|---|---|---|---|
| `0x404b4` | `1` (on) | `parse_recv_from_topic` (`server_cmd_parse.c`), right after logging `"Ble OTA adv to on."` | a cloud MQTT command with `type=="ota"` — a BLE-relay-for-OTA path, unrelated to WiFi-down fallback (needs the cloud, which is exactly what's down in our scenario) |
| `0x4c852` | `1` (on) | `bind_event_start` (`bind_ctrl.c`), after `"BLE---------start broadcast"` | entering pairing mode — called from `parse_recv_from_topic` (a `user_cmd`) and re-armed from `dispatch_handler_ble_key_change_wifi` mid-BLE-provisioning |
| `0x4ccda` | `0` (**off**) | `check_close_ble_broadcast` (`bind_ctrl.c`), after `"===============close BLE BROADcast"` | the bind window's timeout elapsed |
| `0x4cd96` | `1` (on) | `check_close_ble_broadcast`, after `"===============open BLE BROADcast"` | keep-alive: window still open but the MCU-mirrored on/off flag reads off |

`bind_event_start` (`ctrl` vaddr `0x4c800`-`0x4c8c4`, complete): logs, sends `0x6001`=`1`, sets
three of its own config bytes (`+0x26e4`=`ble_open_by_bind`=1, `+0x26dd`=0, `+0x26e7`=0), then
calls `0x4c7d4`:

```
0x4c7d4  push {r4,lr}
0x4c7d8  bl #0x80300                     ; get_current_time() -> r0  (unix seconds)
0x4c7e4  add.w r0, r0, #0x12c            ; **r0 = now + 300**
0x4c7ee  str r0, [r3, r2=#0x26d8]        ; field = expiry timestamp
0x4c7f4  strb r1, [r3, r2=#0x26dc]       ; field = 0  ("timeout handled" flag, reset)
```

**`0x12c` = 300 decimal = 5 minutes**, a compiled-in immediate, not a config value.
`check_close_ble_broadcast` (`ctrl` vaddr `0x4cb68`-`0x4ce70`, complete, called periodically —
it's `.dynsym`-exported like the `ble` handlers, i.e. invoked through the same generic
timer/callback framework `ble`'s `__dispatch_run_timer`-style mechanism uses, not by a plain `bl`
anywhere in `.text`):

1. Early-outs entirely if a master gate byte (`+0x26c9`) is set, or if **neither**
   `ble_open_by_bind` (`+0x26e4`) **nor** `+0x26dd` is set — i.e. it only acts on a session *it*
   itself is tracking.
2. If tracking a session: compares `get_current_time()` against the stored expiry (`+0x26d8`).
   Before expiry: falls through to a keep-alive check (below). At/after expiry: clears its own
   flags, and if the MCU-mirrored "currently advertising" byte (`+0x27f9`) is nonzero, sends
   `0x6001`=`0`.
3. Keep-alive (runs on *every* tick while the window is open, elapsed or not, and also on the
   very first tick after a session starts): if `+0x27f9` reads **off** while the bind flags say
   it should be **on**, re-sends `0x6001`=`1`. This is a self-healing re-assert, not a second
   independent trigger.

**Consequence for a direct/bare send** (`pktool`, or Kibble's own agent, calling `mq_send`
without going through `bind_event_start`): none of `ble_open_by_bind`/`+0x26dd`/`+0x26d8` get
set. `check_close_ble_broadcast`'s early gate (`+0x26e4==0 && +0x26dd==0`) routes to a fallback
path (`0x4cd18`→`0x4cda4`) that, absent two unrelated device-state conditions (`+0x2630==5` or
`+0xb2c!=0` — not chased further; neither is set in the common case), does **nothing** — it
never sends the off command for a session it didn't open. **A bare send is invisible to this
timeout and will advertise until explicitly told off**, which is exactly the risk `Main` flagged
and §6 below designs against.

### 3.2 `+0x27f9`: candidate for `state.ble.sta_data.ble_adv`, unconfirmed

`check_close_ble_broadcast` gates both its off-send and its keep-alive-on-send on this one byte
reading the MCU's actual current advertising state — almost certainly the `state.ble.sta_data.
ble_adv` field `03-app.md` already lists ("Whether BLE advertising is currently on"). Byte offset
**not independently confirmed** — `appendix-config-layout.json` has no entry for it, and the live
test (§4) could not confirm it either. Worth a byte-exact pin next time someone has a live window
(see §6).

### 3.3 A boot-time-only advertising trigger inside `ble` itself (found, not a live-test factor)

`ble` vaddr `0x134f0` is registered as a periodic (1000 ms) timer callback (`ble`'s own
`__dispatch_run_timer`-equivalent, `bl #0x2657c`, timer slot `2`, registration at `0x127ec`).
Guarded by a one-time latch (`if already_run: return` at its very top). On its first successful
tick, it: waits (up to ~20s, via nested retry/poll loops at `0x19584`/`0x193f0`, sleeping 20 ms
and 1000 ms respectively) for a status query to return `2` (almost certainly "has the T31 MCU
finished its own boot"), then configures a batch of LED-family settings (subaddrs `5,7,8,2,3,4`
via the same `0x193f0` "trigger + poll" helper — note subaddr `2` appears here too, i.e. this
same one-time block also flips advertising) and, at the very end, calls the exact same wrapper as
§2.3 with `subaddr=2, value=1` (`bl #0x17a90` @ `0x1359e`).

This explains a plausible **brief advertising blip at MCU power-up** — not a prerequisite for
later toggles. It is a one-shot boot latch; on a device that has been running for any real length
of time (ours has), it resolved (success or permanent failure) long before any live test, and
re-running our own `0x6001` send does not re-arm or depend on it. Flagged for completeness, not
used to explain the negative live-test result.

---

## 4. Live test, 2026-09-15 — negative-inconclusive

Approved by `Main` for exactly one on/off cycle after static analysis above. Sender: `tools/
kibble-msg.c` cross-compiled static armv7 (`arm-linux-gnueabihf-gcc -static -Os -march=armv7-a
+fp -mfpu=neon-vfpv4`), pushed to `/tmp/km` over the LAN (nothing written to device flash).

| Step | Wire bytes | Result |
|---|---|---|
| Baseline | — | `config_shm[10233]` = `0x00` (candidate flag, unconfirmed — see §3.2) |
| ON | `/tmp/km 8 6001 1 hex:01000000` → `01 60 01 00 \| 01 00 00 00` | `mq_send` returned `ok` |
| HA check (~3 min window) | — | `homeassistant.components.bluetooth.manager` debug log via the plant-room proxy (`54:32:04:3E:F3:72`): **zero** hits for `Petkit`/`D4SH` names, **zero** for `0000aaa0/1/2` UUIDs, zero for the `94:BA:06` WiFi-OUI prefix (weak check — BLE typically has its own BD_ADDR). Proxy proven live/forwarding: an unrelated device (a Govee sensor, −64 dBm) logged in the same window. |
| OFF | `/tmp/km 8 6001 1 hex:00000000` → `01 60 01 00 \| 00 00 00 00` | `mq_send` returned `ok` |
| Post-check | — | `config_shm[10233]` still `0x00`; wider dump `[10200,10263]` unchanged across on→off except two unrelated free-running telemetry counters (`10218-10223`) — nothing there tracks our command |

**What this proves:** the wire-level lever (§1-§2) is real — three independent senders,
disassembly-traced end to end, agree byte-for-byte, and `mq_send` accepted every message. The
message unambiguously reached `ble`'s inbox.

**What this does not prove:** whether the T31 MCU actually began RF advertising. The negative
result cannot distinguish "the MCU never started advertising" from "it advertised bare/unnamed,
with no service UUIDs" — HA's log was only greppable for the specific strings above, and no
before/after baseline of *unnamed* adverts was captured, so a new anonymous MAC appearing during
the window (if any) is indistinguishable from ambient noise. `config_shm[10233]` is an unconfirmed
guess (§3.2) and its non-movement is not strong evidence either way.

Per the no-blind-retries agreement, no second attempt was made. See §6 for what a decisive next
attempt needs.

---

## 5. The `ctrl`-diff: does the vendor do anything else around `0x6001`?

Directly answers the assignment's "diff what `ctrl` does around its `0x6001` send that a bare
send omits."

**No additional bus message of any kind.** All four `0x6001` call sites in `ctrl` (§3.1) send
*exactly one* bus message each — nothing to `ble`, `cloud`, or anywhere else accompanies it. The
only things `bind_event_start`/`check_close_ble_broadcast` do beyond the send are writes to
`ctrl`'s **own** config bytes (`+0x26c9,+0x26d8,+0x26dc,+0x26dd,+0x26e4,+0x26e5,+0x26e7`) — all of
that is `ctrl`'s private bookkeeping for its own 300 s timeout (§3.1); none of it is read by `ble`
or forwarded anywhere, so it cannot be a prerequisite `ble`/the MCU needs.

The only *other* candidate prerequisite found anywhere in either binary is the one-shot boot-time
init in `ble` itself (§3.3) — and that one is unconditional on `ble`'s own boot success, not
per-toggle, and (see §3.3) not a plausible explanation for our specific negative result on an
already-running device.

**Working conclusion:** if a real prerequisite exists, it is not visible from the Linux side at
all — it would have to live inside the T31's own TC32 firmware (opaque to every static tool
available here; `08-mcu.md §7` already flags this as the project's hard limit) — for example a
distinct "(re)build advertising payload / set adv interval" step the MCU performs autonomously
that our specific CMD `0x09`/subaddr `2` toggle doesn't itself trigger, or a "never actually
provisioned this exact unit over BLE, so the adv name/UUID table was never populated" gap (`dev.
mac_info.a_BLEmac`, `usr.bind.step`, `state.dev_pro.first_linked` would be worth checking against
this specific unit's config for that hypothesis, not yet done). Neither is resolvable without a
proper live baseline (§6) or, ideally, someone at Petkit's own answer.

---

## 6. What a decisive next live test needs (for whoever runs it)

Per `Main`'s call: no blind retry. Next attempt should go in instrumented from the start:

1. **Baseline every unnamed advert** the plant-room proxy reports for 60 s *before* the send, and
   again for 60 s *after* — diff for a newly-appearing (or disappearing) anonymous MAC. This is
   the only way to catch a bare/unnamed advertisement, which §4's test could not distinguish from
   ambient noise.
2. **Bracket `config_shm[10200:10264]`** (or wider) immediately before and immediately after —
   byte-diff instead of guessing one offset. This also has a chance of finally pinning
   `state.ble.sta_data.ble_adv`'s real offset (§3.2).
3. **A phone BLE scanner (nRF Connect or similar) held next to the feeder** — decisive, not
   subject to ESPHome-proxy scan-interval or filtering limitations, and Nitin is a one-message ask
   away. Worth doing before spending another automated cycle.
4. If that still comes back negative: chase the TC32-opaque and never-BLE-provisioned hypotheses
   in §5 with whatever live `config_shm` fields are accessible (`dev.mac_info.a_BLEmac`,
   `usr.bind.step`, `state.dev_pro.first_linked`/`.ble_open_by_bind`/`.ble_open_by_key`), and
   consider whether Petkit's own public documentation or firmware changelog says anything about a
   required first-time BLE provisioning step.
