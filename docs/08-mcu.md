# STUDY-mcu.md — T31 Dispenser MCU Firmware & UART Protocol

First-look analysis of `study/live/ble.img` (the T31 dispenser-MCU OTA firmware image, 153,412
bytes, md5 `<redacted-32-hex>`) and the host-side UART protocol implemented in
`fs/app/bin/ble` (`app/bin/ble`, ELF32 ARM LE, EM_ARM, Thumb-2 code, entry `0x12ae1`). All work
was static analysis on local copies; no contact with the live device (192.168.4.85).

Method: `ble.img` was decoded byte-by-byte (header fields, CRC trailer, entropy, string
extraction). `ble` was parsed with `pyelftools` (sections/imports/PLT) and disassembled with
`capstone` (Thumb-2), including reconstructing the ARM position-independent "load a PC-relative
delta into a register, then `add reg, pc`" idiom this binary uses for local `.rodata` references,
to recover real cross-references from code to the debug/format strings named in the assignment.

---

## 1. `ble.img` 64-byte header decode

Hex (bytes 0–63):
```
<redacted-hex>
```

| Offset | Bytes (LE) | Value | Meaning |
|---|---|---|---|
| 0 | `56 80 00 00` | 0x00008056 (32854) | unidentified 32-bit field (not size/CRC of anything tried) |
| 4 | `00 00 5d 02` | 0x025d0000 | unidentified |
| 8 | `4b 4e 4c 54` | ASCII `"KNLT"` | **magic** — see §2 |
| 12 | `9d 06 88 00` | (0x069d, 0x0088 as two u16) | unidentified |
| 16 | `c6 80 00 00` | 0x000080c6 (32966) | unidentified |
| 20 | `00 00 00 00` | 0 | reserved/zero |
| 24 | `44 57 02 00` | **0x00025744 = 153412** | **total file size** — exact match to the 153,412-byte image, confirmed |
| 28 | `00 00 00 00` | 0 | reserved/zero |
| 32 | `0c 64 81 a2` | (0x640c, 0xa281 as two u16) | unidentified |
| 36 | `21 0b 1a 40` | (0x0b21, 0x401a as two u16) | unidentified |
| 40–63 | `c0 06` × 12 | constant repeat | fill/padding pattern, not per-field data (confirmed by per-1KB entropy: this run and the tail of the file are the only clearly low-entropy blocks — see §2) |

I could not identify a checksum/CRC/size relationship for the offset-0/4/12/16/32/36 fields
against zlib CRC32, Adler32, CRC16 (CCITT/Modbus, several inits), or plain byte-sum, tried over
the payload, the full file, and the header itself. **Offset 24 = total file size is the one
header field I can assert with certainty.**

**Trailer (last 4 bytes of the file, offset 153408–153411): `ff ff ff ff` then... ** — actually the
true last 4 bytes are `9e 6d 67 18`; the 4 bytes immediately before them are `ff ff ff ff`. Those
last 4 bytes ARE a checksum: see §2.

---

## 2. MCU identification: **Telink TLSR82xx-family BLE SoC — HIGH confidence**

**Evidence chain (all independently corroborating):**

1. **`KNLT` magic at offset 8** is a documented Telink OTA-image marker. A public, independently
   reverse-engineered Telink OTA flasher (`crackheadakira/8BitDoRetroKeyboardFlasher`, MIT-adjacent
   GPLv3 project for a Telink-TLSR82xx-based keyboard) documents this exact format: *"A `KNLT` magic
   header at offset 8 (Telink OTA marker)... File size stored little-endian at offset 24... A
   CRC32 checksum in the final 4 bytes... The CRC32 uses standard reflected polynomial
   `0xEDB88320`, init `0xFFFFFFFF`, and no final XOR"* (differs from zlib's `crc32`, which does
   apply the final XOR).
2. **The trailer CRC matches exactly.** Computing that non-zlib CRC32 variant (poly `0xEDB88320`
   reflected, init `0xFFFFFFFF`, **no final complement**) over `ble.img[0:153408]` (the whole file
   minus its last 4 bytes) yields `0x18676d9e`. The file's actual last 4 bytes, read little-endian,
   are `9e 6d 67 18` = `0x18676d9e`. **Exact match**, byte for byte, algorithm and all — this is
   not a coincidence; it confirms both the file-size field (offset 24) and the CRC-trailer
   convention Telink's own OTA tooling uses for TLSR82xx targets.
3. **A second, smaller `KNLT`-tagged structure exists at file offset 152108** (`4b4e4c54` again),
   1304 bytes before EOF, followed by fields consistent with a compact "OTA info" record (a `1`,
   an `0xffffffff`, some small packed values, and the byte sequence `aa bb cc dd ee ff` — looks
   like a placeholder/default BLE MAC). This is consistent with Telink SDK practice of embedding a
   secondary OTA-validation/info block near the end of the image; not independently confirmed
   beyond the byte layout.
4. **Embedded BLE GATT attribute database with Petkit-branded strings.** Around file offset
   150,000–150,400 there is a byte-for-byte recognisable Telink-SDK-style GATT attribute table
   (16-bit UUID declarations `0x2803` "characteristic declaration", `0x2902` CCCD, `0x2901` user
   description, standard `0x12`/property-byte patterns) naming BLE peripherals: `Petkit_D4SH_RX`,
   `Petkit_D4SH_TX` (a custom UART-over-BLE RX/TX characteristic pair, i.e. a "Nordic-UART-service"-
   style transparent pipe), and device/service names `Petkit_D4SH`, `Petkit_A_D4SH`, `Petkit_D4SH2`,
   `Petkit_A_D4SH2`, `Petkit_D4SH3`, `Petkit_A_D4SH3` (likely SKU variants: 1/2/3-hopper models,
   "_A" perhaps a factory/alternate-firmware variant). **This firmware image runs its own BLE
   stack** with GATT services distinct from the main Axera SoC's Realtek RTL8733BU combo radio —
   this refines/corrects STUDY-app.md's inference that "the T31 MCU itself is reached only via
   UART, not BLE": the T31/dispenser MCU **is** itself a Telink BLE SoC and appears to run its own
   BLE peripheral role (plausibly for factory pairing/test or a fallback BLE-UART bridge), separate
   from the main-SoC BLE handled by `ble`'s other roles. Not independently confirmed by a live BLE
   scan (out of scope — no live contact permitted).
5. **The literal string `"T31"` is embedded in the firmware itself** at file offset 151,296,
   inside a small cluster of short state-name strings: `KEY`, `Connect`, `Bounded`, `OTA`,
   `Feeding`, `T31`, `MinTime`. This resolves the "T31" naming question from STUDY.md/STUDY-app.md:
   it is not a reference to the (MIPS-based, unrelated) Ingenic T31 camera SoC — it is a label the
   vendor's own MCU firmware uses for itself, most plausibly a short product/board code for the
   dispenser mainboard, carried through into the host-side debug strings ("T31 recv: ...").
6. **No standard-ARM Cortex-M vector table.** Bytes at payload offset 0 (file offset 64) are the
   `c0 06` filler pattern repeated, not a plausible `(initial_SP, reset_vector)` pair — consistent
   with Telink TLSR82xx's actual boot layout, where the reset/interrupt vector table is not simply
   `SP,PC` at flash offset 0 the way it is on Cortex-M, and with the core being **TC32** (Telink's
   proprietary 32-bit RISC/Thumb-like ISA), not ARM: `capstone`'s ARM/Thumb decoders have no TC32
   mode, so no disassembly sanity-check of the reset handler was possible or attempted — this is
   an inherent limitation of the tooling, not new evidence, but it is consistent with (does not
   contradict) the Telink identification.
7. **Per-1KB Shannon entropy** across the image averages 6.17 bits/byte (code-like, not random/
   encrypted — consistent with the "no encryption" note in the same public writeup) with two clear
   low-entropy regions: file offset ~24576–31744 (drops toward 0 — likely a padding/reserved gap
   before the GATT table) and the last ~2KB (offset ~147456–153412, entropy 2.7–5.9 — the GATT
   table, string cluster, and header/trailer metadata, all mixed ASCII+small-integer data, exactly
   where lower entropy is expected).

**Model-number specificity:** I cannot pin the exact Telink part (e.g. TLSR8253 vs TLSR8258 vs
TLSR8269) from static image analysis alone — no `TLSR`, `8258`, `8253`, etc. string was found in
either `ble.img` or `ble`'s strings. **Confidence: high on vendor/architecture (Telink TC32 BLE
SoC, OTA-format match is exact and independently documented elsewhere), open on exact part number.**

---

## 3. UART frame format (`/dev/ttyS3`, `ble` ⇄ T31 MCU)

Recovered by disassembling the frame-receive/dispatch function in `ble` starting at file offset
`0x58b8` (vaddr `0x158b8`), and the baud-rate setup function around file offset `0x9ab8` (vaddr
`0x19ab8`, `cfsetspeed` call at file offset `0x9b18`).

### 3.1 Baud rate
- `ble` imports libc `cfsetspeed`. Its call site (file offset `0x9b18`) is reached after a
  **data-driven lookup**: a 9-entry table of raw baud numbers `{115200, 57600, 38400, 19200, 9600,
  4800, 2400, 1200, 300}` (file offset `0x23bf4`, i.e. `.rodata`) is linearly compared against a
  value derived from the process's config (the value being matched, `r7`, is loaded earlier from
  config state, not a literal), and on match the parallel table of POSIX termios `Bxxx` symbolic
  constants `{0x1002=B115200, 0x1001=B57600, 0xf=B38400, 0xe=B19200, 0xd=B9600, 0xc=B4800,
  0xb=B2400, 0x9=B1200, 0x7=B300}` (file offset `0x23bd0`) supplies the `speed_t` passed to
  `cfsetspeed`. **This confirms STUDY-app.md's existing note that baud is a runtime/config value,
  not a compiled-in literal** — the code is generic (candidate list covers every common baud from
  300–115200) and the *actual* configured value lives in `g_config`/`config_shm`, not in this
  binary. 115200 is the highest (and typically default) candidate in the list and the most likely
  operating baud for a modern embedded UART link, but this is inference, not proof.

### 3.2 Frame layout (from the receive-loop function, file offset `0x58b8`+)

Confirmed byte-for-byte from disassembly (not inferred from strings):

| Bytes | Field | Evidence |
|---|---|---|
| 0 | `0x5A` | **sync byte 1** — code reads one byte via a UART-read helper (`bl` at file offset `0x9c28`→target `0x1a0ec`) and compares `cmp r0,#0x5a`; loops/resyncs on mismatch (file offset `0x966c`→`0x59f8`... instr at vaddr `0x1592c`) |
| 1 | `0xA5` | **sync byte 2** — same pattern immediately after, `cmp r0,#0xa5` (vaddr `0x15936`) |
| 2–3 | `LEN` (u16, LE) | two more single-byte reads combined `lo \| (hi<<8)` (vaddr `0x1593a`–`0x1594c`), then sanity-checked `LEN ≤ 0x1FC` (508) against the fixed receive-buffer size (vaddr `0x1594e`) |
| 4 | `CMD` (u8) | command id, 0–27 (`0x00`–`0x1b`); dispatch uses a genuine Thumb-2 **`TBH` (table-branch-halfword) jump table** at vaddr `0x15b20` (file offset `0x5b20`), gated by `cmp CMD,#0x1b; bhi <default>` (vaddr `0x15b18`) — see §4 for the full recovered table |
| 5 | `SUBID` (u8) | secondary byte, read alongside `CMD` (`ldrb r1,[r5,#5]`); passed into a per-command "last-seen" bookkeeping table (timestamps + a small state record) at file offset `0x78b8` — likely a sequence number (matches the `"Fill seq [%d], checksum:0x%x"` string's presence in this codebase, though I could not pin its exact call site — see §6) |
| 6 | flags/sub-address | bit 6 = a 1-bit flag (`ubfx r8,byte6,#6,#1`) and bits 0–3 = a 4-bit sub-address (`byte6 & 0xF`) — plausibly the app/dev/pt sub-addressing STUDY-app.md already inferred from the `"app -> t31"`/`"dev -> t31"`/`"pt app -> t31"` log tags |
| 7…N−2 | payload | command-specific (e.g. for `Motor Run Config Cmd`/feed, this is where `feed_amount_l`/`feed_amount_r` would live) |
| N−2..N−1 | **CRC16 trailer** (u16, LE) | last two bytes of the frame, read as `(byte[N-1]<<8) \| byte[N-2]` LE (vaddr `0x15aec`–`0x15afa`) and compared against a value computed by a checksum function (vaddr `0x1998c`, file offset `0x998c`) called over `frame[0 .. N-2]` |

The checksum function at file offset `0x998c` is **not** a simple additive/XOR sum: disassembly
shows the classic table-free CRC-16/CCITT nibble-update algorithm (`movw r3,#0xffff` init,
byte-XOR, nibble extraction, shift-5/mask-`0x1FE0`/XOR reduction per byte) — i.e. **CRC-16/CCITT
(poly 0x1021 family), initial value `0xFFFF`**, computed over the whole frame except its own
2-byte trailer. This is a different, lighter-weight check than the CRC32 described next (§3.3),
used for ordinary command/response frames.

### 3.3 Separate CRC32 path (OTA packets)

The `"uart ota"` command (`CMD=0x18`, see §4) branches into a dedicated sub-dispatcher at file
offset `0x4e34` (3-way switch on an OTA sub-state: `1`→`0xa6b0`, `2`→`0xaadc`, `3`→`0xa810`,
presumably start/data/end). This is almost certainly where the `"Check CRC32 error ! head_crc =
%x,data_crc=%x"` / `"Fill seq [%d], checksum:0x%x"` / `"recieve over, wridx:%d, checksum: 0x%x"`
strings are used (their text and the two-CRC — header vs. data — framing matches an OTA-packet
integrity scheme distinct from the lighter per-command CRC16 in §3.2), but **I was not able to
pin exact instruction-level cross-references to these four specific strings** — the addressing
idiom that worked for the 19 `"T31 recv:"` strings and the `IsCommDataCheckSumErr` string (a
local `ldr rX,[pc,#imm]` delta immediately or shortly followed by `add rX,pc` in the same
register) did not resolve for these four, even with a widened 400-instruction lookahead window;
they are evidently addressed through a different code-generation pattern (likely GOT-relative
indirection through a per-function base register established much earlier in the function, which
a full data-flow-aware disassembler would need to track). This is flagged as an open item (§7).

---

## 4. Recovered `CMD` (offset-4) command-id table

Recovered with certainty by decoding the `TBH` jump table at file offset `0x5b20` (28 entries,
one per `CMD` value `0x00`–`0x1b`) and matching each entry's branch target against the address of
the `"T31 recv: <name>"` printf call reached from that case body (both independently computed via
disassembly, not inferred from string order):

| CMD (hex) | CMD (dec) | Name (from `"T31 recv: X"` string) | Notes |
|---|---|---|---|
| 0x00–0x03 | 0–3 | *(no debug string — tiny 8–40 byte bodies)* | trivial/ack-only handlers, no log |
| 0x04 | 4 | `FEED_SCH ACK` (`res=%d`) | feed-schedule ack from MCU |
| 0x05 | 5 | *(shared default handler)* | |
| 0x06 | 6 | `FEED_LOG Cmd` | |
| 0x07 | 7 | *(shared default handler)* | |
| 0x08 | 8 | `KEY_EVENT Cmd` | physical button press |
| 0x09 | 9 | *(shared default handler)* | |
| **0x0A** | **10** | **`Motor Run Config Cmd`** (`wr:%d`) | **the feed-dispense command** — matches STUDY-app.md's known feed path (`ctrl`→`dispatch_handler_feed`→`ble: dispatch_handler_ble_feed_ctrl`→"T31 recv: Motor Run Config Cmd! wr:%d") |
| 0x0B | 11 | `MOT_RUNSTA` | motor run status |
| 0x0C | 12 | `MCU_BASE_CFG req` | |
| 0x0D | 13 | `get ver mac cmd` | version/MAC query response |
| 0x0E | 14 | `reset mcu cmd` | |
| 0x0F | 15 | *(shared default handler)* | |
| 0x10 | 16 | `id secrect set ok` [sic] | |
| 0x11 | 17 | `ble trans data` | |
| 0x12 | 18 | `pt trans data` | production-test channel |
| 0x13 | 19 | `RTC data` | |
| 0x14 | 20 | `Power manage` (`node:%d`) | |
| 0x15 | 21 | `Relay connect` (`node:%d`) | |
| 0x16 | 22 | *(shared default handler)* | |
| 0x17 | 23 | `DEV type` | |
| **0x18** | **24** | **`uart ota`** | dispatches into the OTA sub-state machine, §3.3 |
| 0x19 | 25 | `Food Surplus Ctrl` | |
| 0x1A | 26 | `NEW DEV type` | |
| 0x1B | 27 | `FEED_INFO_RECOED` [sic] | |

Every case with `CMD > 0x1b` (27) falls through to the default handler (`bhi` at vaddr `0x15b18`).
This table is a **complete, disassembly-verified enumeration** of the T31→host command space
(28 possible values, 19 named), directly answering the STUDY-app.md open item that these numeric
`msg_id`/command values were "not recoverable from strings alone."

---

## 5. OTA-over-UART packet format (partial)

- `uart ota` (`CMD=0x18`) routes into a 3-state sub-dispatcher (file offset `0x4e34`): state `1`
  (start), `2`, `3` map to three distinct handler addresses (`0xa6b0`, `0xaadc`, `0xa810` — not
  further disassembled in this pass).
- The higher-level flow is already documented from strings in STUDY-app.md §7: `ble` fetches the
  OTA package over HTTP(S) (`"uart ota start get file url:%s,file md5:%s,file size:%d"`), then
  streams it to the MCU in indexed, retry-capable packets (`"uart ota running pack data, now
  index:%d!"`), with a final version-check gate (`"ota_success_wait_ver success!"`).
- Each UART packet in this stream is presumed to use the same base frame format as §3.2
  (sync `5A A5`, `LEN`, `CMD=0x18`, `SUBID`, flags/subaddr, CRC16 trailer), carrying a chunk of
  the 153,412-byte `ble.img` blob as payload; the additional `"Check CRC32 error ! head_crc =
  %x,data_crc=%x"` check is most plausibly validating each OTA packet's own header+data integrity
  as an extra layer on top of (not instead of) the outer CRC16 frame check, given the string
  clearly reports two separate CRC values. **Not proven by disassembly this pass** (see §3.3
  and §7).
- The firmware image's own trailer format (whole-image CRC32, §2) is a *separate*, end-to-end
  integrity check applied to the reassembled image before "ota_success_wait_ver", distinct from
  the per-packet UART CRC(s) used while streaming it.

---

## 6. Cross-checks between `ble.img` and `ble`

| Item | In `ble` (host) | In `ble.img` (MCU) | Match |
|---|---|---|---|
| `"T31"` label | Used throughout as `"T31 recv: ..."` log prefix | Literal string at file offset 151296, alongside `KEY`/`Connect`/`Bounded`/`OTA`/`Feeding`/`MinTime` | **Confirmed** — resolves the "T31" naming question |
| Command names (`DEV type`, `FEED_LOG`, `KEY_EVENT`, `RTC`, `Power`, `MOT_*`, etc.) | 19 full debug strings (§4) | None found as literal ASCII (MCU firmware is compiled TC32 machine code; only BLE-facing/user strings and short state names like `KEY`/`OTA`/`Feeding` survive) | Partial — `KEY`/`OTA`/`Feeding` short forms present, full names not (expected for stripped machine code) |
| Firmware version `159` (`dev.version_info.ota_param.firmware_ble`) | not applicable | Not found as an ASCII string anywhere in `ble.img` | Not found — version is presumably stored as a binary field (possibly one of the unidentified header words in §1, or embedded in the code as an integer compare) rather than as text |
| BLE device/service names | Not found as strings in `ble`/`ctrl` (STUDY-app.md open item #4) | `Petkit_D4SH`, `Petkit_D4SH2`, `Petkit_D4SH3`, `Petkit_A_D4SH*`, `Petkit_D4SH_RX/TX` GATT names, file offset ~150,048–150,396 | **New finding** — answers STUDY-app.md's open BLE-naming question, but for the MCU's own BLE stack, not necessarily the main-SoC BLE `ble` process also runs |

---

## 7. Open questions / what would need a live (or extracted-flash) capture

1. **Exact numeric value of the configured baud rate.** The candidate table (§3.1) is generic;
   the actual selected entry depends on a `g_config` field not analyzed in this pass (see
   `STUDY-config.md` from the parallel config-struct study). A passive tap of `/dev/ttyS3` (a
   logic analyzer on the RX/TX lines, or briefly `cat`-ing the device — **both excluded from this
   offline-only study**) would confirm it directly from the line's bit timing.
2. **Exact call sites / packet layout for the four OTA-CRC strings** (`Fill seq`, `Check CRC32
   error`, `recieve over`, `UART Init success`) — located to the general `uart ota` sub-dispatcher
   region (file offset `0x4e34`+) but not pinned instruction-by-instruction; needs either a
   smarter (register-data-flow-tracking) disassembler pass or manual tracing of the three OTA
   sub-state handlers (`0xa6b0`/`0xaadc`/`0xa810`).
3. **Meaning of the six still-unidentified 64-byte-header fields** (offsets 0, 4, 12, 16, 32, 36).
   None matched CRC16/CRC32/Adler32/sum-checksum candidates tried against the payload, file, or
   header itself. Possibly a build timestamp, a Telink SDK version stamp, or flash/load-address
   fields specific to the TLSR82xx OTA format that would need the actual Telink SDK headers (not
   available offline) to interpret with certainty.
4. **Exact Telink part number** (TLSR8253/8258/8269/etc.) — no model string found in either image;
   would need either a physical board inspection (out of scope) or comparison against a larger
   corpus of known Telink OTA images/SDKs.
5. **Second `KNLT` block at file offset 152108** — its exact purpose (OTA validation record vs.
   something else) is inferred from byte layout only, not confirmed against Telink SDK source.
6. **Whether the T31 MCU's embedded BLE GATT service (`Petkit_D4SH*`, §2 item 4, §6) is actually
   active/advertised in the shipped configuration**, or is dead code inherited from a Telink SDK
   reference design — cannot be determined without a live BLE scan (explicitly out of scope for
   this study; STUDY-app.md §12 already flags a passive BLE scan as a separate, still-open
   verification step).

---

## Summary for quick reference

- **MCU**: Telink TLSR82xx-family BLE SoC (TC32 core), high confidence — `KNLT` OTA magic +
  exact non-zlib CRC32 trailer algorithm match to an independently documented Telink OTA format;
  embedded Telink-SDK-style BLE GATT table; literal `"T31"` self-label found in the image.
- **Baud**: not hardcoded; `cfsetspeed`-based runtime lookup from a 9-entry table covering
  300–115200 baud, actual value comes from `g_config`.
- **Frame**: `5A A5 | LEN(u16 LE) | CMD(u8) | SUBID(u8) | FLAGS/SUBADDR(u8) | payload... |
  CRC16-CCITT(u16 LE)`, CRC16 init `0xFFFF`, table-free nibble algorithm, computed over
  everything except its own 2-byte trailer.
- **Feed command**: `CMD = 0x0A` (`Motor Run Config Cmd`), matching the already-known
  `ctrl`→`ble` feed-dispatch path.
- **OTA**: `CMD = 0x18` (`uart ota`), with a separate, unresolved-in-detail CRC32-based
  header+data integrity scheme for the packet stream, layered on top of the whole-image CRC32
  trailer format decoded in §2.
