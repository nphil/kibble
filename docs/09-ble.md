# STUDY-ble.md — BLE control-pipe protocol (D4SH2 dispenser)

Offline static analysis only. No contact with the live device (192.168.4.85), no BLE scans/connections.
Sources: `live/ble.img` (T31 MCU firmware, 153,412 bytes, md5 `<redacted-32-hex>`, confirmed
against STUDY-mcu.md), `fs/app/bin/ble` (ELF32 ARM/Thumb-2, 198,732 bytes), `fs/app/bin/ctrl` (ELF32
ARM/Thumb-2, 742,148 bytes). Method: `pyelftools` for ELF layout, `capstone` (Thumb-2) for `ble`/`ctrl`
disassembly with a custom PC-relative literal-pool resolver (the same "`ldr rX,[pc,#N]` then `add rX,pc`"
idiom STUDY-mcu.md documented) to recover string/constant cross-references in these stripped binaries, and
raw byte-pattern search for `ble.img` (TC32 core, unsupported by capstone — no disassembly possible there).
This builds directly on STUDY-mcu.md §3–§4 (UART frame format, TBH command table) rather than re-deriving
them; new work here is the GATT/UUID/adv extraction, the `ble`↔MCU send-path disassembly, the `ctrl`-side
BLE dispatch, and the auth-flow tie-together.

---

## TL;DR

| Question | Answer | Confidence |
|---|---|---|
| GATT UUIDs | 3 custom 16-bit-on-standard-base UUIDs **0xAAA0, 0xAAA1, 0xAAA2** (same vendor family as the public Petkit W5 fountain's `0xAAA1`/`0xAAA2`) + Telink's stock 128-bit OTA UUID `00010203-0405-0607-0809-0a0b0c0d2b12` | High (exact byte match) for values; medium for which UUID is RX vs TX vs Service |
| UART/BLE frame format | `5A A5 \| LEN(u16 LE, = payload+9) \| CMD(u8) \| SEQ(u8, auto-incrementing per-CMD, not client-set) \| FLAGS/SUBADDR(u8) \| payload \| CRC16(u16 LE)` — **identical format used in both directions** | High (disassembly-confirmed both ways) |
| Feed-over-BLE? | **No native path.** `ctrl`'s BLE-payload dispatcher recognizes exactly 5 sub-commands (WiFi-credential change, get-schedule, generic event, OTA-end, version-check) — none of them is feed. Feed remains reachable only via `dispatch_handler_feed` (cloud/local-HTTP origin). The transport *could* carry a feed command (same UART CMD 0x0A path, same T31) if the receiving side were extended — see §8. | High (complete msg_id table + dispatcher disassembled) |
| Auth requirement | A device-held secret (16 bytes, config field) is pushed to the MCU over UART CMD 0x10 conditionally; **no evidence of a challenge/response or session-token check on the BLE link itself** — no "auth"/"token" strings anywhere in `ble`, `ctrl`, or `ble.img`. AES calls in `ble` are the same generic config-file cipher used by all 9 app binaries, not a demonstrated BLE-specific cipher. Practical implication: an HA BLE client likely does **not** need the device secret to read/write the RX/TX characteristics at the link layer, but may need it (or may simply be rejected) if it tries to send a command `ctrl` validates against `usr.id_info.dev_srt`. Not provable without a live connection. | Medium |

---

## 1. GATT service/characteristic UUIDs (`ble.img`)

### 1.1 Name-string cluster (file offsets, confirmed via direct byte search)

| String | Offset(s) |
|---|---|
| `Petkit_D4SH_RX` | `0x24a20` |
| `Petkit_D4SH_TX` | `0x24a6c` |
| `Petkit_D4SH` | `0x24a20`(prefix of RX), `0x24a6c`(prefix of TX), `0x24a7c`, `0x24b40`, `0x24b5c`(prefix of D4SH2), `0x24b6c`(prefix of D4SH3) |
| `Petkit_D4SH2` / `Petkit_A_D4SH2` | `0x24b5c` / `0x24b4c` |
| `Petkit_D4SH3` / `Petkit_A_D4SH3` | `0x24b6c` / `0x24b7c` |
| `Petkit_A_D4SH` | `0x24b30`, `0x24b4c`, `0x24b7c` |

All six device-name variants sit in one contiguous, null-padded table at `0x24b30`–`0x24b90` (`_A_` variants
alternate with plain variants — most likely a "factory/alternate-firmware" vs. "normal" flag per
STUDY-mcu.md's original inference, not confirmed further here).

### 1.2 Custom 16-bit UUIDs on the standard Bluetooth base — new finding

The standard Bluetooth Base UUID (`0000xxxx-0000-1000-8000-00805F9B34FB`) is stored, as always for BLE, in
little-endian byte order; its fixed 12-byte suffix in that encoding is the constant
`fb 34 9b 5f 80 00 00 80 00 10 00 00`. A whole-file search for that exact 12-byte run found it **3 times**,
each immediately followed (in memory) by the customized 4-byte short value:

| File offset (of the 12-byte suffix) | Following 4 bytes (LE) | Short UUID | Region |
|---|---|---|---|
| `0x245a4` | `a2 aa 00 00` | **0xAAA2** | earlier in the file, its own 16-byte block context |
| `0x24a54` | `a0 aa 00 00` | **0xAAA0** | inside the block that opens with `Petkit_D4SH_RX` |
| `0x24a9c` | `a1 aa 00 00` | **0xAAA1** | immediately after the OTA 128-bit UUID that follows `Petkit_D4SH_TX`/`Petkit_D4SH` |

`0xAAA1` and `0xAAA2` are **the exact same values** `PetkitBleScout` found for the public Petkit W5/Eversweet
fountain BLE protocol (`slespersen/PetkitW5BLEMQTT`, `triosniolin/petkit-fountain-ble`): Read/notify =
`0000aaa1-…`, Write = `0000aaa2-…`. The feeder additionally has a **third** value, `0xAAA0`, not present in
the public fountain writeups (the fountain community only ever documented the two characteristic UUIDs, not
a service UUID). **New finding, high confidence on the byte values; the RX/TX/service role assignment below
is inference from table position, not proof** — the Telink attribute-table struct places a characteristic's
UUID constant whereever the compiler put it in `.rodata`, not necessarily physically next to that
characteristic's name string, so proximity is suggestive, not certain:
- `0xAAA0` positionally sits inside the RX-name block → **best-guess: primary Service UUID** (would explain
  why it has no counterpart in the 2-characteristic-only public fountain docs).
- `0xAAA1`/`0xAAA2` positionally straddle the TX/OTA area and an earlier, separate block respectively.
  If Petkit reused the fountain's own RX=write/TX=notify convention (`0xAAA2`=write, `0xAAA1`=notify), that
  would make **RX (phone→device, write) = 0xAAA2** and **TX (device→phone, notify) = 0xAAA1** — consistent
  with the community numbering, but not independently re-derived from this table's struct layout (no Telink
  SDK headers available offline to decode the exact `attribute_t` field order — see STUDY-mcu.md §7 item 3).
  **A live, read-only GATT characteristic-properties read (see §9) will disambiguate instantly**: whichever
  of 0xAAA1/0xAAA2 has the `WRITE`/`WRITE NO RESPONSE` property is RX, whichever has `NOTIFY` is TX.

### 1.3 Telink stock OTA UUID — confirmed, unmodified from SDK default

16 contiguous bytes `12 2b 0d 0c 0b 0a 09 08 07 06 05 04 03 02 01 00` appear at file offsets `0x24a8c` and
`0x245b4`. Reversed (BLE 128-bit UUIDs are stored LE; human/RFC4122 form is the byte-reverse), this is
**`00010203-0405-0607-0809-0a0b0c0d2b12`** — byte-for-byte the Telink OTA "Data" characteristic UUID
`PetkitBleScout` found in public Telink-SPP-derived documentation. At `0x24a8c` it sits directly after a
`02 29 00 00` (0x2902 CCCD) marker and directly before a `00 28 00 00` (0x2800 Primary Service) + literal
ASCII `"OTA\0"` — i.e. there is a **distinct "OTA" GATT service** in this table, separate from the main
D4SH data service, using Telink's default OTA UUID unmodified. At `0x245b4` a related 128-bit constant
**`00010203-0405-0607-0809-0a0b0c0d1912`** (same base, last two bytes `1912` instead of `2b12`) appears
immediately after the `0xAAA2` short UUID — consistent with Telink's SDK convention of a service/
characteristic UUID pair sharing one base with only the last bytes differing (`…1912`=service-level,
`…2b12`=characteristic-level), though this exact pairing is not confirmed against SDK source.

### 1.4 Standard 16-bit UUIDs present (structural context, not individually pointer-chased)

Byte-pair scans in the same `0x24900`–`0x24c00` region turned up LE pairs matching `0x2800` (Primary
Service Declaration), `0x2803` (Characteristic Declaration), `0x2901` (User Description), `0x2902` (CCCD),
`0x1800` (GAP Service), and `0x2A00` (Device Name) — all standard GATT attribute-table furniture, consistent
with an otherwise-unmodified Telink SDK attribute array (records look like ~20–24-byte fixed-size structs,
consistent with the classic Telink `attribute_t{u16 attNum; u16 uuidLen; u16 attrLen; u16 attrMaxLen; u8*
pAttrValue; u8* pUuid; void* w; void* r;}` shape, though field-by-field offsets were **not** independently
re-derived here — no SDK headers available offline, matches STUDY-mcu.md §7 item 3's existing open item).

### 1.5 Advertising payload

A `02 01 06` byte run (AD structure: length=2, type=0x01 Flags, value=0x06 = *LE General Discoverable Mode,
BR/EDR Not Supported*) is present **twice back-to-back** at file offset `0x24b28`/`0x24b2c`, immediately
before the six-name device-variant table (§1.1). This is the one static AD-structure fragment found.
**No static concatenated `[flags][complete-local-name][manufacturer-data]` advertising buffer was found**
anywhere in the file — searches for a length-prefixed `0x09` (Complete Local Name) AD header directly
preceding any of the six name strings, and for a `len,0xFF,<company-id>` manufacturer-data header near the
GATT-table region, both came up empty. **Inference**: the actual advertising PDU is assembled *at runtime*
by code that copies the flags AD-structure constant plus whichever of the six name variants matches the
device's SKU/config, rather than being pre-baked as one static template — expected, since the name choice
is itself a runtime decision. This means the exact manufacturer-data / MAC-in-adv layout the task brief
anticipated ("Petkit devices typically put device type + MAC in adv") was **not confirmed statically**;
only a live passive scan can capture the actual over-the-air adv payload (§9).

---

## 2. UART/BLE frame format (recovered from `ble`, both directions)

This extends STUDY-mcu.md §3 (which fully nailed the **receive** direction, MCU→host) with the
**send** direction, host→MCU, disassembled fresh in this pass.

### 2.1 Generic frame-builder function, `ble` vaddr `0x16970`

Signature (recovered from the prologue's register moves): `build_and_send_uart_frame(u8 cmd, u8 flag_bit6,
u8 subaddr_nibble, u8 *payload, u16 payload_len)`. Byte-exact behavior, confirmed by direct disassembly:

```
frame_len = payload_len + 9                     ; 9 = 2(sync)+2(len)+1(cmd)+1(seq)+1(flags)+2(crc)
frame = malloc(min(frame_len, 0x22c))
*(u16*)(frame+0) = 0xA55A                        ; LE bytes -> 5A A5 on the wire (vaddr 0x16a08/0x16a0e)
*(u16*)(frame+2) = frame_len                     ; LEN field (vaddr 0x16a0c)
frame[4] = cmd                                   ; CMD field (vaddr 0x16a1a)
frame[5] = ++seq_table[cmd]                      ; per-CMD rolling sequence counter (vaddr 0x16a1c-0x16a2a) —
                                                  ; confirms STUDY-mcu.md §3.2's "likely a sequence number"
                                                  ; guess for this byte; it is NOT a client-supplied session id
frame[6] = (subaddr_nibble) | 0x10 | (flag_bit6 << 6)   ; bit4 always set on every outbound frame (vaddr
                                                  ; 0x16a10/0x16a16); bit6 = caller's flag_bit6; bits0-3 =
                                                  ; caller's subaddr_nibble — matches STUDY-mcu.md's RX-side
                                                  ; decode of this byte (bit6 flag + bits0-3 subaddress)
memcpy(frame+7, payload, payload_len)             ; payload starts at byte 7 (vaddr 0x16a42) — mirrors the
                                                  ; RX side's payload-starts-at-byte-7 finding exactly
crc = checksum(frame, frame_len-2)                ; SAME checksum function as RX, vaddr 0x1998c
                                                  ; (CRC-16/CCITT-family, init 0xFFFF, table-free nibble algo)
*(u16*)(frame+frame_len-2) = crc                  ; trailer (vaddr 0x16a58)
enqueue_for_uart_tx(frame, frame_len)             ; via internal job-queue primitive, msg_type 0x601a, dst=8
                                                  ; (ble's own process id) — see §2.3
free(frame)
```

**Confirms**: the wire format is **identical in both directions** —
`5A A5 | LEN(u16 LE) | CMD(u8) | SEQ(u8) | FLAGS/SUBADDR(u8) | payload | CRC16(u16 LE)`, with LEN counting
the whole frame (so payload length = LEN − 9), and the same CRC16 algorithm computed over everything
except its own trailer.

### 2.2 Confirmed CMD=0x11 ("ble trans data") is bidirectional

STUDY-mcu.md's CMD table (TBH dispatch table at `ble` vaddr `0x15b20`, decoded there from the RX/receive-
loop side) already established CMD 0x11 as the MCU→host "ble trans data" message. This pass additionally
found a **separate, distinct call site at `ble` vaddr `0x17c78`** that invokes the very same frame-builder
(§2.1) with `cmd = 0x11` to send data **down** to the MCU — i.e. **the reverse path the task asked for uses
the same CMD id, 0x11, just built by the host instead of the MCU**. This function takes a payload
pointer/length and a subaddress byte from its own caller and forwards them unchanged into the frame.

RX side (`ble` vaddr `0x161c6`, disassembled fresh): after a log-level-gated debug print, computes
`payload_len = frame_len − 9` and `payload_ptr = frame+7` (same accounting as §2.1) then dispatches by the
FLAGS byte's low nibble (subaddress):
- **subaddr 0**: forwards the raw payload to `ble`'s internal job-queue primitive (`ble` vaddr `0x255fc`,
  the real `dispatch_send_msg`-equivalent — see §2.3) with `msg_type=0x100a`, `dst=2`.
- **subaddr 1**: goes through a different function (`ble` vaddr `0x1bbb0`) that first acquires what looks
  like two named locks/semaphores (via `memcmp` against two fixed 6-byte constants) before proceeding
  further; not traced to completion in this pass (out of scope — no BLE-transport-level implication, this
  is `ble`'s own internal gating, not an on-the-wire protocol detail).
- any other subaddress value: silently dropped (no action).

### 2.3 The real `dispatch_send_msg`, `ble` vaddr `0x255fc`

Signature `dispatch_send_msg(u32 msg_type, u32 dst, void *payload, u32 len)`. Validates
`(dst − 1) ≤ 0x13` (i.e. `dst` ∈ [1,20], matching STUDY-dispatch.md's live-observed process-id range 1–10),
skips its own debug log for `msg_type == 0xFFFF` or `0x103` (looks like exempted high-frequency/heartbeat
types), then `pthread_mutex_lock`s before enqueueing — this **is** the cross-process (and, used with
`dst == own_pid`, cross-thread-within-one-process) mqueue primitive STUDY-dispatch.md was reverse-engineering
generically. Two concretely observed calls from the BLE code path: `msg_type=0x100a, dst=2` (forwarding
BLE-received app data, §2.2) and `msg_type=0x601a, dst=8` (`ble`'s own outbound-UART-frame job, §2.1 — `dst`
8 is `ble`'s own queue id, i.e. this is `ble` talking to its own UART-writer thread, not another process).
Full 27-entry msg_id registry for `ctrl` (found via the analogous registration call `ctrl` vaddr `0x80a14`)
is in §5 — this resolves **`msg_id 0x100a == dispatch_handler_recv_ble_data`** exactly, closing the loop
between the UART-level finding here and the `ctrl`-side handler in §4.

### 2.4 CMD-specific send-site evidence (host→MCU)

Found by scanning every `bl` call site targeting the frame-builder (§2.1) across the whole `.text` section
(21 direct call sites) and reading back the immediate CMD value loaded into `r0` at each:

| CMD | Payload (bytes) | Notes |
|---|---|---|
| 0x04 | up to 65 (struct: 3×u8 fields + up to 64 copied bytes) | schedule-shaped payload; RX-side CMD 0x04 is "FEED_SCH ACK" |
| 0x05 | up to 65, same struct shape as 0x04 | sibling of 0x04, exact semantics not pinned |
| 0x06 | 1 byte | RX-side CMD 0x06 is "FEED_LOG Cmd" → send = feed-log **request**, matches `dispatch_handler_ble_get_feed_log_right_now` |
| 0x09 | 6 bytes (2×u8 + 2×u16) | not matched to a named handler in this pass |
| 0x0C | variable, caller-supplied | RX-side "MCU_BASE_CFG req" |
| **0x0E** | **0 bytes (empty)** | RX-side "reset mcu cmd" → matches `dispatch_handler_ble_resetMCU` exactly: reset is a bare, payload-less command |
| **0x10** | **16 bytes, from `*(u32*)(g_config_ptr+0x111c)` — conditional, only sent if that pointer is non-NULL** | the id/secret push — see §6 |
| **0x11** | variable, caller-supplied | confirmed bidirectional BLE-data pipe, §2.2 |
| 0x12 | variable, caller-supplied | RX-side "pt trans data" (production-test channel), symmetric send confirmed |
| 0x13 | 0, 2, or variable bytes (3 distinct call sites) | RX-side "RTC data"; matches `dispatch_handler_ble_set_RTC` |
| 0x14 | 1 byte | RX-side "Power manage"; matches `dispatch_handler_ble_set_sleep_en` |
| **0x15** | variable, caller-supplied (ptr+len both passed through) | RX-side "Relay connect" — **confirmed** (not just inferred, per STUDY-mcu.md's own hedge) to be the BLE-device-relay feature: this send site is reached from `ctrl`'s `dispatch_handler_get_relay_dev_list` chain per STUDY-app.md §6, i.e. this is Linux telling the T31 which *other* Petkit BLE peripheral (fountain etc.) to relay, unrelated to the D4SH's own control surface |
| 0x17 | 1 byte | RX-side "DEV type"; device-type set |
| **0x18** | variable, caller-supplied, guarded (only sent if len>0) | RX-side "uart ota"; confirmed OTA-packet-down path, matches STUDY-mcu.md §5 |
| 0x19 | 5 bytes | RX-side "Food Surplus Ctrl" |
| 0x1A | 1 byte | RX-side "NEW DEV type" |

CMD 0x0A ("Motor Run Config Cmd" / feed) is **not** among these 21 direct call sites — its send call goes
through one of two small CMD-parametrized wrapper stubs (`ble` vaddr ≈`0x1791c`/`0x17930`, which forward
whatever CMD their own caller passes in `r0`) rather than a fixed immediate, so the exact call chain to
`dispatch_handler_ble_feed_ctrl` was not individually re-traced pixel-by-pixel in this pass. This does not
weaken the finding: STUDY-app.md/STUDY-dispatch.md already independently confirm the feed path
(`ctrl:dispatch_handler_feed` → `pk_ctrl_send_feed_event_msg` → `ble:dispatch_handler_ble_feed_ctrl` →
UART, logged as `"T31 recv: Motor Run Config Cmd! wr:%d"`), and this pass additionally disassembled CMD
0x0A's **receive**-side handler (`ble` vaddr `0x15f22`) and confirmed it is a simple log-and-return (no
further action) — i.e. that string is the ACK the MCU sends back after a feed command, not a receive-side
trigger, resolving a naming ambiguity in the original brief.

---

## 3. `ble.img` (T31 MCU firmware) — command evidence available without a TC32 disassembler

capstone has no TC32 support, so nothing here is instruction-level; all of it is byte-pattern search,
explicitly lower-confidence than §1–§2, labeled accordingly.

- **`5A A5` (UART sync bytes) — not found anywhere in `ble.img`.** [INFERENCE: inconclusive, not negative]
  TC32 likely encodes small immediates differently from a literal adjacent byte pair the way ARM Thumb
  does, so this absence cannot be read as "the MCU doesn't implement the UART framing" — it demonstrably
  does (STUDY-mcu.md's CRC32/OTA-header analysis and the live UART traffic itself prove that). It only means
  a raw-byte search for this specific 2-byte constant is not a viable method against TC32 code.
- **`FA FC FD` (candidate "fountain protocol" header) — found once**, at file offset `0x244f4`, immediately
  followed by what looks like a table of 4-byte pointer-shaped values (`32 0e 01 00`, `2a 0e 01 00`, …, all
  in the `0x010Exxxx` range) — i.e. this 3-byte run sits inside an unrelated pointer/jump table, not
  repeated or followed by a plausible frame body. **[INFERENCE, low confidence]**: coincidental byte
  alignment in compiled code, not evidence the T31 implements the fountain wire protocol.
- **No `FB` framing structure**: 329 lone `0xFB` bytes exist in 150KB, statistically unremarkable (not a
  repeating footer pattern).
- **Autonomous-state string evidence**: only `Feed`/`Feeding` found (file offset `0x24ef8`, 8 bytes before
  STUDY-mcu.md's already-documented `KEY`/`Connect`/`Bounded`/`OTA`/`Feeding`/`T31`/`MinTime` cluster at
  `0x24ef0`–`0x24f00`). Exact-case searches for `schedule`/`RTC`/`battery`/`secret`/`auth`/`AES`/`token`/
  `check` all came up **empty**. **This is not evidence against MCU autonomy** — it is consistent with a
  small, flash-constrained TC32 firmware that tracks state numerically rather than with debug strings (the
  Linux side is comparatively string-heavy because it links a verbose `AX_SYS_LogPrint`/`printf` debug
  framework the MCU firmware has no equivalent of). The actual evidence for MCU-side autonomy is the
  **already-documented** `config_shm` telemetry mirror (STUDY-app.md §4, `state.ble.sta_data.*`,
  `state.ble.adc_data.*`, `state.ble.moto_runt_data.*` — Linux-side copies of values the MCU computes and
  reports up over UART, which only makes sense if the MCU itself owns the schedule/RTC/battery/motor state
  machine) plus the confirmed UART command set (RTC data 0x13, Power manage 0x14, Motor Run Config/status
  0x0A, FEED_SCH ACK 0x04, FEED_LOG 0x06, Food Surplus Ctrl 0x19 — all periodic/telemetry-shaped, consistent
  with an MCU that runs these subsystems and reports summaries, not one that receives micromanaged
  step-by-step instructions for them).
- **Conclusion for "what the MCU consumes itself vs. forwards"**: the MCU appears to (a) run its own
  schedule/feed/RTC/battery/motor state machine autonomously and report telemetry up over the confirmed
  numeric UART CMD set, and (b) treat BLE-delivered **application-layer bytes specifically** (CMD 0x11) as
  opaque data to forward to Linux rather than interpret locally — every BLE-data code path found in `ble`
  (§2.2) routes to a Linux-side consumer (`ble`'s own job queue, or onward to `ctrl`), with **no evidence
  found of the T31 acting on BLE-write content by itself**. This is inference from the host-side forwarding
  code, not from the (opaque) MCU firmware directly, and should be treated as the working hypothesis, not
  as proven.

---

## 4. `ctrl` — BLE-originated command handling & provisioning flow

### 4.1 Complete inventory of BLE-sourced message types `ctrl` recognizes

`ctrl` registers exactly 27 message handlers on its own inbox at startup (a call to `ctrl` vaddr `0x80a14`
once per handler, `register(msg_id, handler_fn_ptr, name_str_ptr)` — decoded via the same PC-relative
literal resolver used throughout this study; registration code at `ctrl` vaddr `0x15a70`–`0x15ca0`). The
five that are BLE-sourced (their own function bodies additionally reference their own name string for log
calls, confirming these aren't just registration-table noise):

| msg_id | Handler | Role |
|---|---|---|
| `0x100a` | `dispatch_handler_recv_ble_data` | generic entry point for raw bytes forwarded from `ble` (the far end of UART CMD 0x11 subaddr-0, §2.3) |
| `0x100b` | `dispatch_handler_ble_event_msg` | generic BLE event |
| `0x1009` | `dispatch_handler_ble_key_change_wifi` | **WiFi credential change via BLE** |
| `0x1007` | `dispatch_handler_save_wifi_conf` | persists WiFi config (called after a successful key-change) |
| `0x1015` | `dispatch_handler_ble_version_update_check` | version check |
| `0x1018` | `dispatch_handler_ble_ota_end` | BLE-relayed OTA completion notice |
| `0x101a` | `dispatch_handler_ble_get_schedule` | **feed schedule read via BLE** |

For contrast, the ordinary (cloud/local-HTTP-originated) feed handler is **`0x100f` =
`dispatch_handler_feed`** — a completely separate, non-BLE-prefixed msg_id with no BLE-sourced counterpart
anywhere in the 27-entry table.

### 4.2 `dispatch_handler_recv_ble_data` body (`ctrl` vaddr ≈`0x473d8`–`0x4760c`) — the decisive function

Disassembled in full. After bounds-checking and `memcpy`-ing the incoming payload (max 0x228=552 bytes) into
a local buffer, it calls a parser (`ctrl` vaddr `0x8bdb0`) that turns the raw bytes into a structured object,
then looks up a field via `ctrl` vaddr `0x8bde0` (signature consistent with a JSON-style
`get_object_item(parsed, key)`) and reads a **`type` field at offset 0x14** of the result. The dispatch on
that type field is a plain `cmp`/`beq` chain, not a table — meaning it is exhaustively enumerable by reading
the branches directly:

```
type == 0x70 -> bl 0x46950
type == 0x6e -> bl 0x46520
type == 0x6f -> bl 0x46e98
type == 0x72 -> bl 0x45188   (then, if that call's result == 0: bl 0x461a8)
type == 0x97 -> memset(local, 0, 0x88); bl 0x453f8(payload, local)
                (then, if that call's result == 0: bl 0x461a8; bl 0x90c20(0xe); bl 0x4c8c4)
anything else -> free/cleanup, return 0 (no-op)
```

That is **five** recognized type values (`0x6e, 0x6f, 0x70, 0x72, 0x97`), matching exactly the count of the
five named `dispatch_handler_ble_*` sub-operations `ctrl` implements (§4.1, minus `recv_ble_data` itself and
`save_wifi_conf` which is a helper `key_change_wifi` calls, not a directly-dispatched type). The `0x97` case
is structurally the richest (a 0x88=136-byte local buffer, a dedicated parse call, and a **second**-stage
call sequence on success) — consistent with it being `ble_key_change_wifi`, since that operation needs the
richest payload of the five (`ctrl` has three distinct WiFi-credential debug format strings:
`"ssid[%s], pwd[%s]"`, `"ssid[%s] pwd[%s] ip[%s] gw[%s]"`, and — the most structurally complete —
`"ssid:%s pwd:%s hide:%d locale:%s timezone:%f"` at file offset `0x9b223`, i.e. SSID + password + hidden-SSID
flag + locale + timezone in one shot). This mapping (`0x97 → key_change_wifi`) is **plausible but not
byte-proven** in this pass — the format-string-to-function link could not be closed with a direct
instruction xref (see Open Items). **There is no sixth branch, and no branch value in this chain leads
toward feed-triggering logic** — the function either falls through to the five named operations or does
nothing. This is the direct, disassembly-level confirmation behind the TL;DR's "no feed-over-BLE" finding.

### 4.3 Provisioning flow (WiFi via BLE)

1. Phone/BLE-central writes provisioning bytes to the RX characteristic (§1.2).
2. T31 forwards raw bytes up over UART CMD 0x11 (§2.2).
3. `ble` receives, and — for subaddr 0 — posts `msg_type=0x100a, dst=2` onto the internal bus (§2.3).
4. `ctrl`'s `dispatch_handler_recv_ble_data` (msg_id `0x100a`) parses the JSON-ish payload, reads its `type`
   field, and for the WiFi-credential case (inferred `type==0x97`) extracts `ssid`/`pwd`/`hide`/`locale`/
   `timezone`, calling into `dispatch_handler_save_wifi_conf` (msg_id `0x1007`) to persist it into
   `usr.wifi.conf.{ssid,pwd,uuid}` (config schema already documented in STUDY-app.md §4).
5. Device applies the new credentials and attempts to reconnect to WiFi/cloud.

This is a genuine, evidence-backed **WiFi-recovery-over-BLE** path — directly useful for "WiFi is down"
scenarios, but it recovers *connectivity*, it does not add a *general remote-control* surface: nothing in
this chain reaches feed, schedule-*write*, LED, or any other actuator. `dispatch_handler_ble_get_schedule`
(§4.1) is read-only (a *get*, not a *set*) by its own name.

---

## 5. Full `ctrl` msg_id registry (all 27 entries, for completeness/cross-reference)

`0x1002`:`dispatch_handler_ctrl_event_msg`, `0x1012`:`dispatch_handler_get_scan_result`,
`0x1003`:`dispatch_handler_net_dev_ota_check`, `0x1005`:`dispatch_handler_lapse_record_over`,
`0x1014`:`dispatch_handler_do_formatting`, `0x100c`:`dispatch_handler_start_pt_mode`,
`0x100b`:`dispatch_handler_ble_event_msg`, `0x1006`:`dispatch_handler_set_connect_http`,
`0x10`:`dispatch_handler_ledlight_mode_set`, `0x100a`:`dispatch_handler_recv_ble_data`,
`0x1009`:`dispatch_handler_ble_key_change_wifi`, `0x1007`:`dispatch_handler_save_wifi_conf`,
`0x100f`:`dispatch_handler_feed`, `0x1010`:`dispatch_handler_dev_state_report`,
`0x1008`:`dispatch_handler_ctrl_get_upload_pic_url`, `0x1017`:`dispatch_handler_ctrl_get_other_str`,
`0x1015`:`dispatch_handler_ble_version_update_check`, `0x1016`:`dispatch_handler_ctrl_PM_befor_sleep`,
`0x1018`:`dispatch_handler_ble_ota_end`, `0x1019`:`dispatch_handler_get_relay_dev_list`,
`0x101a`:`dispatch_handler_ble_get_schedule`, `0x101b`:`dispatch_handler_pet_face_pic_used_end`,
`0x101c`:`dispatch_handler_get_pet_face_info_by_network`, `0x101d`:`dispatch_handler_sync_led_mod`,
`0x101e`:`dispatch_handler_iot_connect_change`.

(Recovered jointly with `MsgIdRecovery`'s parallel session; shared live, see that session's
`STUDY-msgids.md` for the corresponding `ble`-side outbound `0x6xxx` namespace, e.g. `dispatch_handler_
ble_feed_ctrl = 0x6004`, which is `ctrl`→`ble` direction and independent of the BLE-air-protocol table
above.)

---

## 6. Session security / auth

- **UART CMD 0x10 ("id secrect set ok")**: `ble`'s send site (vaddr `0x17b80`, §2.4) pushes **16 bytes**
  read from a pointer stored at a fixed offset (`+0x111c`) inside a shared config struct — sent
  **conditionally**, only if that pointer is non-NULL. This lines up exactly with STUDY-app.md's already
  -documented config field `usr.id_info.dev_srt` / `srt_len` ("a device secret/token and its length").
  `ble`'s own RX handler for the CMD 0x10 **acknowledgment** (vaddr `0x16174`, §2.4) does nothing beyond
  logging on receipt — no retry, no further handshake step visible on the Linux side.
- **No auth/token strings anywhere in the BLE code paths**: `ble.bin`, `ctrl.bin`, and `ble.img` were all
  searched for `"auth"`/`"token"`/`"check"`(-as-auth-context) — none found in `ble.bin` or `ble.img`. `ble`
  does call `AES_set_encrypt_key`/`AES_set_decrypt_key`/`AES_cbc_encrypt` (vaddr `0x26698`–`0x26818`), but
  this is the **same generic config-file-at-rest cipher** STUDY-app.md §5 already documents as linked
  identically into all 9 app binaries (`petkitRootfs_Aes_Encrypt_Keys_32`, used for `/opt/user.conf`/
  `/opt/dev.conf`) — no evidence it is *also* used to encrypt/sign anything on the BLE link itself.
  `ctrl.bin` has two config-adjacent strings worth flagging for future live verification:
  `"secret in demo\r\n"` (file offset `0x85ed0`) and several `"secret=<redacted> traces
  (offsets `0x88f8f` region, `0xa0788`/`0xa0914`) — the exact calling function for these could not be pinned
  down with the PC-relative xref method in this pass (open item below); `"secret in demo"` is worth a closer
  look later since demo/test fallback secrets are a common IoT weak point, but nothing here shows it is
  reachable from the BLE path specifically.
- **Practical read for an HA BLE client**: nothing found in this pass shows the BLE **link itself**
  (GATT connect / characteristic read-write) is gated by the device secret — that's an ATT/GATT-layer
  property (bonding/pairing requirements set per-characteristic in the attribute table, §1.4, which this
  static pass could not fully decode field-by-field). What the secret clearly **is** used for is: the
  content `ctrl` expects once it *parses* a message (`usr.id_info.dev_srt` almost certainly participates in
  request validation somewhere in `ctrl`'s cloud-facing code, by analogy with the Alibaba IoT HMAC scheme in
  STUDY-app.md §6) — whether `dispatch_handler_recv_ble_data`'s five sub-handlers independently check it was
  **not** confirmed or ruled out in this pass (none of the five callee functions, `0x46520`/`0x46e98`/
  `0x46950`/`0x45188`/`0x453f8`, were individually disassembled for auth checks). **Do not assume BLE
  commands are unauthenticated** — treat this as an open item, not a green light.
- **`usr.id_info.dev_srt` byte offset: 91 in `config_shm.bin`** (32 hex-character string encoding 16 bytes of the
  device secret — MEDIUM confidence per ConfigStruct's struct-order reasoning within the id_info leaf cluster).
  `usr.id_info.srt_len` could not be pinned (all-zero bytes preceding offset 91 consistent with srt_len=0
  at capture time). See STUDY-config.md §2 for the full mapping and caveats. This resolves the §6 open item.

---

## 7. What works with Linux off vs. requires it

- **Linux (`ble`+`ctrl`) down, T31 powered**: scheduled/autonomous feeding, RTC keeping, battery/ADC
  monitoring, and motor-run telemetry all continue — this is the MCU's own state machine (§3), independent
  of the SoC. A BLE central could very likely still complete a GATT **connection** to the T31's own
  peripheral (its radio and GATT server are on-chip, not Linux-hosted) and might get link-layer ACKs for
  writes to the RX characteristic, but **nothing found in this study shows the T31 interprets those bytes
  itself** — every code path for BLE-delivered app data forwards to Linux (§2.2, §3's conclusion). With
  `ble` not running to drain `/dev/ttyS3` and relay onward, and `ctrl` not running to parse/act on
  `dispatch_handler_recv_ble_data`, a BLE-delivered command almost certainly goes nowhere.
- **Linux up, WiFi/cloud down** (the realistic "WiFi is down" scenario — router/internet outage, not a SoC
  crash): `ble` and `ctrl` keep running normally off local power; the entire BLE pipe in this document
  (T31 GATT ↔ UART CMD 0x11 ↔ `ctrl`'s dispatcher) is unaffected by WiFi/cloud reachability, since it never
  touches the network stack. This is the scenario in which everything documented here actually applies.
- **Feed specifically**: even with Linux fully up, BLE cannot currently trigger a feed (§4.2) — the
  transport (T31 GATT → UART CMD 0x11 → `ctrl`'s `dispatch_handler_recv_ble_data`) is fully wired and
  capable of carrying arbitrary payloads, but the **stock** `ctrl` binary simply never branches from a
  BLE-sourced message to `dispatch_handler_feed`/`pk_ctrl_send_feed_event_msg`. Per STUDY.md's own
  first-party-agent plan (replace `ctrl`+`cloud`, keep `ble`/`media`/`watchdog`), a replacement for `ctrl`
  that adds one more branch to an equivalent of `dispatch_handler_recv_ble_data` — calling the *same*
  `dispatch_handler_ble_feed_ctrl` → UART CMD 0x0A path §2.4 already documents — would give real
  feed-over-BLE with **zero MCU firmware changes**, since the T31 is confirmed to be a dumb byte-forwarder
  for this channel.

---

## 8. Recommended first live experiments (and what NOT to do yet)

Safety-ordered, read-only-first, matching STUDY.md's own "next phase" discipline:

1. **Passive scan only**, via an existing ESPHome BLE proxy already on the network (`esp32_ble_tracker` in
   passive mode) — capture the actual advertised name (confirms which of the six §1.1 variants this unit
   uses), the manufacturer-data bytes if any (§1.5 predicted these are NOT statically templated — a live
   capture is the only way to see them), and the BLE MAC (cross-check against `dev.mac_info.a_BLEmac` in
   `config_shm`, and the `a2:05:d6:57:1e:ce`-shaped value already noted near the WiFi MAC in
   `live/config_shm.bin` — offset only, do not print the value itself).
2. **Read-only GATT discovery** (connect, enumerate services/characteristics/properties, read anything
   marked READ — e.g. the OTA service's version characteristic if one exists — but issue **no WRITE**): this
   directly resolves §1.2's remaining RX-vs-TX-vs-Service ambiguity for `0xAAA0`/`0xAAA1`/`0xAAA2` by reading
   back each characteristic's actual property bitmap (WRITE vs NOTIFY vs neither).
3. **Passive notify subscribe** on whichever characteristic turns out to be TX (no writes yet): observe
   whether the device spontaneously notifies anything (a heartbeat, `state.ble.sta_data.*`-shaped telemetry,
   etc.) without any host-initiated write — would independently corroborate or refute §3's "T31 doesn't act
   on its own without Linux" framing versus a possible always-on telemetry stream.
4. **Do NOT yet**: write anything to the RX characteristic, even a passthrough/no-op-looking UART CMD.
   Every command in §2.4's table was recovered from disassembly, not from live behavior — a malformed or
   mistimed write's actual on-device effect (motor actuation, WiFi credential overwrite, OTA-state
   corruption) is not verifiable offline, and CMD 0x18 (OTA) or 0x0E (reset) sent with the wrong framing
   could brick the pairing/OTA state machine. Do not attempt a "id secret set" write (CMD 0x10) — its
   16-byte payload's exact semantics (device id vs. raw secret vs. derived key) were not conclusively
   pinned down (§6). Do not connect from a device already bonded/paired as the phone app, to avoid
   invalidating the app's own session state.

---

## Open items (would need either live capture or the missing SDK headers to close)

1. Exact RX-vs-TX assignment of `0xAAA1`/`0xAAA2` (and whether `0xAAA0` is really the Service UUID) — §1.2,
   resolved instantly by step 2 above.
2. Exact static advertising manufacturer-data layout (device type + MAC), if any — §1.5, needs step 1 above.
3. Byte-exact link from `ctrl`'s five `dispatch_handler_recv_ble_data` type values (`0x6e/0x6f/0x70/0x72/
   0x97`) to their specific named handlers — currently inferred from format-string richness, not proven by
   direct instruction xref (§4.2).
4. Whether any of the five BLE sub-handlers independently validate `usr.id_info.dev_srt` before acting —
   not disassembled in this pass (§6).
5. Full Telink `attribute_t` field layout (would let every UUID in §1 be assigned to its exact attribute
   with certainty rather than by proximity) — needs Telink SDK headers, unavailable offline.

## Resolved items

- **`usr.id_info.dev_srt`/`srt_len` byte offset within `config_shm`** (§6) — **Resolved 2026-09-15**: offset 91
  is `usr.id_info.dev_srt` (32 hex chars = 16 bytes), per ConfigStruct's struct-order mapping (MEDIUM confidence).
