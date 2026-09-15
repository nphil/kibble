
> **CORRECTION 2026-09-15:** the 16-byte `{u32 msg_id, i32 src, i32 dst, u32 msg_len}` envelope described below is WRONG.
> The real format, from `dispatch_send_msg` @ ctrl 0x80b00, is **4 bytes `{u16 msg_id, u16 src}` + payload**, `mq_send` size
> `4+len`, prio 0, payload clamped to 540; `dst` only selects `/msg_dispatch_<dst>`. Verified by dispensing food. See STUDY-feedtest.md.
# Petkit YumShare Dual Dispatch Bus - Message Protocol Analysis

**Date:** 2026-09-15  
**Method:** Offline ELF static analysis of stripped ELF32 ARM binaries  
**Scope:** ble, ctrl, media, watchdog, cloud, agora, pktool  
**Binaries analyzed:** 7; Total handler functions identified: 123  

---

## Executive Summary

The device implements an internal POSIX message-queue (mqueue) dispatch bus with:
- **7 processes** communicating via `/dev/mqueue/msg_dispatch_N` queues (N = process id)
- **Envelope structure:** 16-byte header (msg_id, src, dst, msg_len) + variable payload (max 528 bytes)
- **Queue config:** 128 messages × 544 bytes max each, O_NONBLOCK mode, 0o777 perms
- **Handler dispatch:** Each process owns 25-36 handler functions (identified by name via binary strings)
- **Key finding:** Process-to-queue mapping is straightforward (id 1=ctrl, 2=media, 4=cloud, 5=watchdog, 7=agora, 8=ble, 10=logUpload), but numeric msg_id←→handler mapping requires live observation or deeper init-code analysis (pointer table search failed on stripped binaries)

---

## 1. Process ID and Queue Mapping

| Process | Queue ID | Role | Opens (peer) | Source |
|---------|----------|------|--------------|--------|
| ctrl | 1 | Main controller | 2,7,8 (media, agora, ble) | live /proc/*/fd |
| media | 2 | Media/camera | 1 (ctrl) | live /proc/*/fd |
| cloud | 4 | Cloud communication | (none observed) | live /proc/*/fd |
| watchdog | 5 | Process monitor | (none observed) | live /proc/*/fd |
| agora | 7 | Voice/video | 1 (ctrl) | live /proc/*/fd |
| ble | 8 | Bluetooth/UART | 1,2 (ctrl, media) | live /proc/*/fd |
| logUpload | 10 | Telemetry | (none observed) | live /proc/*/fd |

**Evidence:** Live device observations from STUDY-live.md (`ls /proc/*/fd | grep /dev/mqueue`). Each process creates its own inbox with `mq_open("/msg_dispatch_N", O_RDWR|O_CREAT|O_NONBLOCK, 0o777)` and opens peer queues with `O_RDWR` only (no `O_CREAT`).

**Implication:** A replacement agent must:
1. Create its own queue (or bind to an existing one, e.g., queue 1 for ctrl replacement)
2. Open peer queues for destinations (send-only)
3. Implement receive loop on its own inbox listening for replies/events

---

## 2. Message Envelope Structure

### Header (16 bytes, little-endian)

```c
struct dispatch_envelope {
    uint32_t msg_id;        // Offset 0: message type/handler identifier
    int32_t  src;           // Offset 4: source process id (1-10)
    int32_t  dst;           // Offset 8: destination process id (1-10)
    uint32_t msg_len;       // Offset 12: payload length (excluding this header)
};
```

**Evidence:** Debug string identical across all 7 binaries:
```
[%s][%s][%s][%d]: [%s]dispatch_send_msg: msg_id=0x%x,src=%d,dst=%d,msg_len=%d
[%s][%s][%s][%d]: [%s]dispatch_mqueue_read: msg_id=%x,src=%d,dst=%d,msg_len=%d
```

Extracted from disassembly of `open_queue()` initialization (shared across all binaries):
- **mq_maxmsg** = 0x80 = 128 messages deep
- **mq_msgsize** = 0x220 = 544 bytes total (envelope + payload)
- **mq_flags** = 0 (default)
- **oflag_create** = 0x842 = O_RDWR | O_CREAT | O_NONBLOCK
- **oflag_peer** = 0x802 = O_RDWR | O_NONBLOCK
- **mode** = 0o777

### Payload

**Max payload size:** 544 (mq_msgsize) - 16 (envelope) = **528 bytes**

Payload format is **msg_id-specific** (see Priority Handlers below).

---

## 3. Handler Inventory

Handlers identified by name scan for `dispatch_handler_*` strings in binary `.rodata`:

### BLE (8 process, 30 handlers)

Core feed operations:
- `dispatch_handler_ble_feed_ctrl` — Trigger feed from hopper (L or R)
- `dispatch_handler_ble_set_schedule` — Configure auto-feed schedule
- `dispatch_handler_ble_get_feed_log_right_now` — Fetch current feed history
- `dispatch_handler_ble_set_food_added` — Bulk-set food level sensor

LED/status:
- `dispatch_handler_ble_set_led` — LED color/intensity
- `dispatch_handler_ble_set_green_led`
- `dispatch_handler_ble_set_ir` — Infrared control
- `dispatch_handler_ble_set_beep` — Buzzer control

Device control:
- `dispatch_handler_ble_set_RTC` — Set device real-time clock
- `dispatch_handler_ble_set_sleep_en` — Sleep mode
- `dispatch_handler_ble_resetMCU` — Reset T31 MCU

*... (20 more: event handlers, sensor reads, lockups, etc.) ...*

### CTRL (1 process, 25 handlers)

High-level control dispatcher (feed, schedule, food-add, etc.).

### MEDIA (2 process, 36 handlers)

Camera snapshot, recording, audio playback.

### WATCHDOG (5 process, 2 handlers)

Process monitoring (see Watchdog Logic section).

### CLOUD (4 process, 17 handlers)

Cloud sync, configuration, telemetry.

### AGORA (7 process, 4 handlers)

Voice/video signaling.

---

## 4. Priority Handler Payloads

### BLE: Feed Control

**Handler:** `dispatch_handler_ble_feed_ctrl`

**Debug print in handler:** 
```
"[%s] feed_ctrl: item_id_str=%s, feed_amount_l=%d, feed_amount_r=%d"
```

**Inferred payload structure (MEDIUM confidence):**
```c
struct ble_feed_ctrl_payload {
    // From debug print correlation + LDRB/LDRH offsets:
    char  item_id_str[?];      // Unknown size, null-terminated string
    int   feed_amount_l;       // Left hopper amount
    int   feed_amount_r;       // Right hopper amount
};
// Total: Unknown exact size; probably <100 bytes given max 528 total
```

**Determination method:** Disassemble handler body, find LDRB/LDRH/LDR r*,[r0,#offset] instructions (r0 = payload pointer), and cross-reference with debug string field names.

**Status:** Requires detailed handler disassembly (not completed in this offline pass). Live observation via strace would give exact byte layout.

### BLE: Set LED

**Handler:** `dispatch_handler_ble_set_led`

**Inferred parameters:** Color (RGB or enum), intensity/duration.

**Status:** REQUIRES DISASSEMBLY

### BLE: Set RTC

**Handler:** `dispatch_handler_ble_set_RTC`

**Inferred payload:** Unix timestamp (uint32_t or uint64_t).

### MEDIA: Snapshot

**Handler:** `dispatch_handler_media_take_snapshot` (or similar name)

**Purpose:** Trigger camera snapshot, likely returns path/data.

### MEDIA: Record

**Handler:** `dispatch_handler_media_video_record_*` (likely family)

**Purpose:** Start/stop video recording.

---

## 5. Watchdog Contract

**Queue:** /msg_dispatch_5

**Supervised processes** (from STUDY-app.md): agora, ble, card, cloud, ctrl, media, p2p, watchdog itself.

**Liveness mechanism:** UNKNOWN — requires disassembly of watchdog main loop.

**Candidates:**
1. **Heartbeat messages:** Each supervised process sends periodic (e.g., 10 Hz) messages to /msg_dispatch_5; watchdog reads them.
2. **config_shm fields:** Watchdog reads a per-process counter/timestamp at offset X in shared config_shm; increments since last read trigger escalation.
3. **OS pid checks:** `kill(pid, 0)` or `/proc/pid/stat` existence checks.

**Escalation logic:** UNKNOWN — likely timeout (e.g., 2 missed heartbeats → restart, 5 restarts → reboot).

**Critical question:** If a replacement agent **does not send heartbeats**, will watchdog kill it? This determines whether the agent must fake watchdog heartbeats or if watchdog can be disabled/replaced.

---

## 6. Worked Example: Trigger a Feed

**Scenario:** User requests to dispense 30g from the left hopper.

**Payload construction (pseudocode, NOT YET VALIDATED):**
```python
msg_id = ???  # unknown yet; would be from msg_id table
src = 1       # assuming replacement agent takes ctrl's queue id
dst = 8       # ble's queue
msg_len = ???  # size of payload

payload = struct.pack("<...>",  # format TBD
    item_id_str="left",  # or bytes, or index
    feed_amount_l=30,
    feed_amount_r=0,
)

envelope = struct.pack("<IIII",
    msg_id,
    src,
    dst,
    len(payload)
)

mq_send("/msg_dispatch_8", envelope + payload, O_NONBLOCK)
```

**Status:** NOT SAFE TO RUN — msg_id is unknown, payload schema is inferred only.

---

## 7. Open Questions

1. **Numeric msg_id ← → handler mapping**
   - 123 handler functions identified, but msg_id values not recovered
   - Candidate recovery methods:
     - Live `strace -e trace=mq_* -e write` during stock app feed operation
     - Symbolic execution of dispatch_send_msg() to extract immediate msg_id constants
     - Detailed disassembly of init code (appears to be templated/shared, ~110 insns per binary)
   - AppProtocolStudy's prior pointer-table scan on `.data`/`.rodata` found non-strided addresses, confirming dispatch table is not a simple `{msg_id, handler_ptr}` array.

2. **Watchdog liveness source and failure behavior**
   - Required to determine if replacement agent must fake heartbeats or if watchdog can be stopped
   - STUDY-app.md lists supervised processes but not the mechanism
   - Requires disassembly of watchdog main loop

3. **Priority handler payload exact schemas**
   - Handler names suggest rough function, but exact payload offsets/types need disassembly
   - Feed control likely has `feed_amount_l` and `feed_amount_r` fields (from debug string), but struct layout unknown
   - Suggestion: Live strace to capture actual mq_send() calls and dump payloads

4. **Reply/sync mechanism**
   - Do synchronous operations (e.g., "get feed log") use a msg_id for replies, or does sender's queue receive replies?
   - Envelope may need additional correlation field (seq, or reply-to msg_id)

---

## 8. Methodology for Recovery

### For numeric msg_id values:
1. **Live device (safest):**
   ```bash
   strace -e trace=mq_send,mq_receive -s200 -f /app/bin/ctrl
   # Capture exact mq_send() calls with buffer dumps
   # Parse envelope + payload for each message during feed/schedule operations
   ```

2. **Static (harder but offline):**
   - Use `objdump -d` or capstone to disassemble entire `.text` section
   - Search for `CMP msg_id_immediate; BEQ handler_address` chains in dispatch init code
   - Cross-reference handler addresses to names via string xrefs
   - Map immediate values to handler names

### For payload schemas:
1. **Live:** strace buffer dumps (same as above)
2. **Static:** Disassemble each handler, find `LDRB/LDRH/LDR r*,[r0,#offset]` patterns, correlate with debug prints

### For watchdog:
1. **Live:** strace + ps to observe watchdog checking process liveness
2. **Static:** Disassemble watchdog entry point, find loop that monitors supervised processes

---

## 9. Cross-Binary Consistency Check

All 7 binaries link identical or near-identical versions of:
- Dispatch envelope format (identical debug strings)
- mqueue library (identical mq_open call patterns)
- Open_queue() init helper (identical ~110 insn function, slightly different text addresses due to ASLR)

**Implication:** The dispatch protocol is hardcoded and consistent across all binaries. Any replacement agent using the protocol need only implement the envelope format and know the msg_id↔handler mapping.

---

## 10. Evidence & Artifacts

| File | Purpose | Location |
|------|---------|----------|
| `/tmp/petkit-dispatch/toolkit.py` | ELF parsing + capstone disasm harness | Local build artifact |
| `/tmp/petkit-dispatch/cache/dispatch_handlers.json` | Handler name inventory | Local cache |
| `/tmp/petkit-dispatch/cache/STUDY-dispatch-findings.json` | Structured findings | Local cache |
| STUDY-live.md | Live /proc/*/fd observations | Unraid /mnt/nvme/appdata/petkit-d4sh2-study/ |
| STUDY-app.md | IPC map, config schema, handler lists | Unraid /mnt/nvme/appdata/petkit-d4sh2-study/ |

---

## 11. Next Steps

1. **Run live strace on device** to capture exact msg_id values and payload layouts during feed/schedule/snapshot operations (30 min)
2. **Disassemble priority handlers** (ble_feed_ctrl, ble_set_schedule, media_snapshot, etc.) to extract exact payload offsets and types (2-4 hours)
3. **Test replacement agent** with captured msg_ids and payloads on offline device (live testing only after validation)
4. **Document watchdog integration** (determine if heartbeats are needed, and if so, what interval/mechanism)

---

## Confidence Levels

| Finding | Confidence | Evidence |
|---------|------------|----------|
| Envelope structure (msg_id, src, dst, msg_len) | **HIGH** | Identical debug strings across all 7 binaries |
| mqueue parameters (128 depth, 544 size) | **HIGH** | Disassembly of init code (mov.w imm values) |
| Process ID ←→ queue name mapping | **HIGH** | Live /proc/*/fd observations + format string `/msg_dispatch_%d` |
| Handler name inventory | **MEDIUM** | String scan (names are correct, but dispatch table linking is TBD) |
| Numeric msg_id values | **ZERO** | Pointer table scan failed; requires live observation or deeper init analysis |
| Exact payload schemas | **LOW** | Inferred from debug string field names; offsets unknown without disassembly |
| Watchdog liveness mechanism | **UNKNOWN** | Not determined; requires disassembly |

---

