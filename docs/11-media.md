# STUDY-media.md — the `media` process: video/audio pipeline for Scrypted + HomeKit

Produced 2026-09-15. All device-facing work was read-only telnet (`pklive`) plus offline static
analysis of binaries pulled from `fs/{app,soc}`. No `pktool` command was ever run, nothing was
written to the device, and no process was killed/restarted. Final live check (06:33 UTC) confirms
`media`(201)/`ble`(200)/`ctrl`(214)/`watchdog`(199) all still running (media at 3h48 accumulated
CPU time, up from 3h36 at the start of this session — continuous uptime, not a restart).

Confidence tags used throughout: **[LIVE]** = directly read from a running `/proc` node or process
state this session. **[STATIC]** = directly read from binary symbols/strings this session.
**[DOC]** = corroborated from an existing STUDY-*.md written by a prior pass, re-verified here where
noted. **[INFERENCE]** = reasoned from the above, not directly observed.

## 1. Pipeline diagram

```
GC2083 sensor (active; GC2053 driver present but unmapped) --MIPI-->
  AX_VIN (dev0/pipe0) --> AX_ISP (Proton core, 3A: AX_ISP_ALG_Ae/Awb) --1920x1080 NV12-->
  AX_IVPS Grp0 (crop X=96,Y=0,W=1728,H=1080 -> 1728x1080 working frame)
        |
        +--> IVPS Chn0 (SCL+TDP, identity 1728x1080) --> VENC Chn0 "main"  (H.264, 1728x1080@25fps)
        +--> IVPS Chn1 (SCL+TDP, downscale 1152x720)  --> VENC Chn1 "sub"   (H.264, 1152x720@25fps)
        |                                              \-> VENC Chn2 "thumb" (H.264, 1152x720, throttled to 5fps)
        +--> IVPS Chn2 (SCL+TDP, 1728x1080, one-shot)  --> AX_VENC_JpegEncodeOneFrame -> /tmp/snap_main.jpeg
        +--> (sync CropResizeVpp 640x384 RGB888 / 224x224 NV12) --> libalgo.so (in-process, dlopen'd) --> NPU detection
        +--> AX_MIPI_RX / raw-frame path --> /tmp/snap_sub.jpeg (sub-res snapshot)

  VENC Chn0/1/2 --AX_NT_SetStreamSource / AX_NT_Ctrl,Stream--> media's own ring-buffer writer
        --> /dev/shm/media_buffer_frame_buf (8 MiB + 1000 B shared ring, POSIX shm)
              readers (plain POSIX shm_open+mmap+sem, NOT an Axera client lib): agora (RTC out),
              cloud (CVR/event upload), [[room for more — see §3]]

  AX_AI (mic, 16 kHz/16-bit, 2-mic array, AEC/AGC/NS) --raw PCM--> media
        --> "audio-out"-style ring/bus hand-off --> agora --Opus/RTC--> phone app  (uplink talk)
  phone app --RTC/Opus--> agora --decodes--> "audio-out" channel --> media's `audio_out_thread`
        --> AX_AO_SendFrame --> ALSA pcmC0D1p speaker                                (downlink talk)
  fs/audio/{cn,en}/*.aac --dispatch_handler_play_aac_file--> AX_ADEC_FdkInit(AAC) --> AX_AO_SendFrame
        --> ALSA pcmC0D1p speaker                                            (canned prompts, separate path)
```

`media` (pid 201, `/app/bin/media`) is the **sole** owner of the entire capture/encode/audio
pipeline — it is the only one of the 9 vendor binaries that imports any `AX_VIN_*`/`AX_ISP_*`/
`AX_IVPS_*`/`AX_VENC_*`/`AX_AI_*`/`AX_AO_*`/`AX_ADEC_*` symbol **[STATIC, confirmed this session
via `.dynsym` of `/app/bin/media`]**. `agora` and `cloud` are pure ring *readers*; neither links any
`AX_VIN/ISP/IVPS/VENC/AI/AO` symbol.

## 2. Video channels — from live `/proc/ax_proc/venc` (2026-09-15 06:19–06:22 UTC)

**[LIVE]**, cross-checked against `/proc/ax_proc/link_table` (kernel DMA-link table, independent
source) and `/proc/ax_proc/ivps`. Full raw dump of the three tables is in the appendix (§9).

| Chn | Codec | Resolution | Src FPS | Target FPS | Bitrate (target/actual CBR) | GOP | Min/Max Qp | I Min/Max Qp | Stream buf | Owning thread | Frames encoded (cumulative) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 "main"  | **H.264** | 1728×1080 | 25.00 | 25.00 | 1280 kbps / **1784.07 kbps actual** | 100 (NormalP, 4s @25fps) | 12/45 | 12/45 | 2899 KB | PID 201 TID 211 | 462,189 |
| 1 "sub"   | **H.264** | 1152×720  | 25.00 | 25.00 | 800 kbps / **364.14 kbps actual**  | 100 (NormalP, 4s @25fps) | 30/51 | 30/51 | 1279 KB | PID 201 TID 212 | 462,189 |
| 2 "thumb" | **H.264** | 1152×720  | 25.00 | **5.00** | 800 kbps cap / **6.25 kbps actual** | 10 (NormalP, 2s @5fps) | 39/51 | 39/51 | 1279 KB | PID 201 TID 213 | 92,440 (≈20% of chn1's count, matching the 5/25 fps ratio) |

All three channels show `ChnStat=STARTED`, zero `EncFail`/`BlkFail`, and steadily climbing frame
counters across repeated reads — i.e. **all three run continuously**, not just when a viewer/upload
is active. **H.264 profile was not captured** — `/proc/ax_proc/venc`'s `VENC CHN ATTR`/`RC ATTR`
tables have no Profile column, and it was not independently pinned via static analysis either; flagged
open (§8).

**Wiring, from `/proc/ax_proc/link_table` [LIVE]** (authoritative, kernel-level, independent of the
VENC proc dump):
```
(VIN  0 0) -> (IVPS 0 0)
(IVPS 0 0) -> (VENC 0 0)      # chn0 "main" sourced from IVPS chn0 (identity 1728x1080)
(IVPS 0 1) -> (VENC 0 1)      # chn1 "sub"  sourced from IVPS chn1 (downscaled 1152x720)
(IVPS 0 1) -> (VENC 0 2)      # chn2 "thumb" sourced from the SAME IVPS chn1 frames as chn1
```
Chn1 and chn2 are two independent H.264 encodes of the *same* scaled 1152×720 frame stream — chn2
is not an extra resolution, it's a lower-frame-rate/lower-bitrate derivative, most plausibly for a
lightweight live-thumbnail/event-preview consumer rather than full continuous streaming.

**Answer to "does a substream already exist": YES.** Chn1 (1152×720@25fps H.264, ~800 kbps class)
is a full-rate substream, already hardware-encoded, already running, already in the same shared ring
as the mainstream. A 4th channel is also possible (see §6) but for an RTSP bridge to Scrypted, chn1
can likely be consumed **as-is** with no new encoder channel at all.

**Sensor**: GC2083 is the currently-active sensor driver — confirmed by `/app/lib/libsns_gc2083.so`
mapped into `media`'s live address space (`/proc/201/maps`); `libsns_gc2053.so` exists on disk but is
not mapped. **[LIVE + STATIC]**

## 3. Audio — mic capture, speaker playback, and talkback

**[LIVE]**, `/proc/ax_proc/ai`, `/proc/ax_proc/ao`, `/proc/ax_proc/aenc`, `/proc/ax_proc/adec`:

| Path | Device | Sample rate | Bit depth | Channels/layout | Live status |
|---|---|---|---|---|---|
| Mic capture (`AX_AI`) | AiCard0/Dev0 (ALSA `pcmC0D0c`, from `STUDY-live.md` [DOC]) | **16000 Hz** | 16-bit | 2, `MIC_MIC` (2-mic array) | VQE on: AEC mode0, NS on (level3), AGC on (mode2, target −1, gain90), **VAD off**. Continuously capturing: `GetFrm=1,849,379` since boot — mic pipeline runs at all times, not just during an active talk session. |
| Speaker output (`AX_AO`) | AoCard0/Dev1 (ALSA `pcmC0D1p`, from `STUDY-live.md` [DOC]) | **16000 Hz** | 16-bit | 2 | Volume 0.70. Nearly idle: `SndFrm=GetFrm=Writei=156` since boot (≈1.6s of cumulative audio ever played this uptime) — confirms playback is event-driven, not continuous. |
| Local prompt decode (`AX_ADEC`) | Chn0 | n/a (decodes to PCM) | — | — | **PlType = "AAC decoder"** (FDK-AAC, `AX_ADEC_FdkInit` **[STATIC]** import confirmed). `SndStrm=159 DecOk=159`, `GetFrm=RlsFrm=156` — matches AO's 156 exactly: **every frame played back this session came from the AAC prompt path**, not a live talk session (none was triggered, per the read-only constraint). |
| Mic/RTC encode (`AX_AENC`) | — | — | — | — | No active channel at observation time (transient, opened only during an active talk/RTC session). Only `AX_AENC_SendFrame` is imported **[STATIC]** — no `AX_AENC_CreateChn`/`Init`, implying channel lifecycle is managed inside `libax_audio.so` and `media` just pushes frames into an already-open channel. |
| JPEG snapshot (`AX_VENC` JPEG mode) | — | n/a | — | — | See §5. |

**Canned prompts** (`fs/audio/{cn,en}/*.aac`, ~120 files, FDK-AAC): triggered by
`dispatch_handler_play_aac_file` **[STATIC string, confirmed in `/app/bin/media` rodata this
session]**; decode via `AX_ADEC_FdkInit`→`SendStream`→`GetFrame`, playback via `AX_AO_SendFrame`
**[STATIC + LIVE, matches ADEC/AO counters above]**. This is a completely separate code path from
talkback (different producer, same AO sink).

**Two-way talk ("pet call") — confirmed implemented, via Agora:**
- **Uplink** (mic → app): `dispatch_handler_speak_start`/`dispatch_handler_speak_stop` **[STATIC
  strings confirmed in `/app/bin/media`]** are the bus handlers that (by name) start/stop a talk
  session. `AX_AI_SetUpTalkVqeAttr` **[STATIC]** — a *talk-specific* VQE attribute setter, distinct
  from the always-on capture VQE attrs — is imported, confirming the SDK has a dedicated talk-mode
  audio tuning path. `agora`'s own exported symbol `agora_rtsa_start_real_play`/`agora_rtsa_start_audio_play`
  **[STATIC, `/app/bin/agora` `.dynsym`]** ("rtsa" = Agora's embedded RTSA/IoT RTC SDK) is the
  RTC-side entry point. Exact numeric `msg_id` for `speak_start`/`speak_stop` was **not recovered**
  (see §4 methodology note) — mechanism confidence is HIGH, exact wire value is open.
- **Downlink** (app → speaker): a dedicated thread inside `media`, literally named
  `audio_out_thread` **[STATIC, from the log string `"...recv audio-out data over 5s, exist[sic]
  audio_out_thread"` in `/app/bin/media` rodata]**, consumes a channel/slot named **`auido-out`**
  (verbatim vendor typo, confirmed byte-for-byte both in `media`'s rodata and live inside the shared
  ring's reader-registry table, §4) and calls `AX_AO_SendFrame` to play it, with a 5-second
  inactivity timeout on the thread. The format at this boundary is **raw 16 kHz/16-bit PCM** (AO's
  own live attrs, above) — NOT AAC and NOT Opus; whatever wire codec Agora uses (almost certainly
  Opus, given `libax_opus.so`/`libopus.so.0.8.0` are present in `/soc/lib` per `INVENTORY.md`
  **[DOC]**) is decoded *inside* the `agora` process before the PCM reaches `media`. This exact
  producer-to-`audio_out_thread` hand-off mechanism (ring slot vs. something else) is
  **[INFERENCE]** — strongly indicated by the shared name ("audio-out" appears both in `media`'s
  own strings and as a live registry entry inside the same ring that carries video) but not traced
  instruction-by-instruction.
- `dispatch_handler_speaker_enable` **[STATIC string confirmed]** exists as a further handler,
  plausibly a mute/enable toggle independent of starting a full talk session.

**Talkback path summary**: mic (`AX_AI`, 16 kHz PCM, on-device AEC/AGC/NS) → `media` → Agora RTSA
SDK (`agora` process, Opus-encode, RTC) ⇄ phone app, and reverse: Agora RTSA (Opus-decode) →
`"audio-out"` hand-off → `media`'s `audio_out_thread` → `AX_AO_SendFrame` → ALSA `pcmC0D1p` speaker,
16 kHz/16-bit PCM at the hardware boundary.

## 4. The `media_buffer_frame_buf` ring — size, structure, readers

**Size [LIVE]**: `ls -la /dev/shm/` → **8,389,608 bytes** exactly (= 8 MiB + 1000 B), matching
`STUDY-live.md`'s prior figure exactly (stable across sessions/reboots).

**Mapped by** (via `/proc/<pid>/maps`, matching the *same* underlying inode **5275** in every case
— i.e. genuinely one shared segment, not per-process copies) **[LIVE]**:
- `media` (201, writer) — `rw-s`, plus **both** `sem.media_buffer_reader_4` (inode 12061) and
  `sem.media_buffer_reader_5` (inode 11794) mapped (the writer holds the reader semaphores so it can
  `sem_post()` them on new data).
- `agora` (268) — `rw-s` on the ring, **and** the *same two* semaphore inodes (12061, 11794) mapped
  under randomized-looking `(deleted)` names — i.e. **agora is the process behind both
  `sem.media_buffer_reader_4` and `_5`**.
- `cloud` (269) — `rw-s` on the ring, **no** named semaphore mapped at all — cloud's reader(s) are
  synchronized some other way (almost certainly polling a sequence/pts field rather than blocking on
  a semaphore; consistent with CVR/event-upload being latency-tolerant where agora's live RTC is not).
- A third semaphore, `sem.media_buffer_reader_6`, exists in `/dev/shm` (created 04:58 UTC, ~1h15m
  before this observation) but was **not** mapped by media/agora/cloud/kibbled at observation time —
  an orphaned (unlinked-but-not-removed) POSIX semaphore, most plausibly left over from an earlier
  reader attach/detach cycle during today's concurrent study activity. Harmless, and itself evidence
  the "create a new `sem.media_buffer_reader_N` and attach" step has already happened at least once
  without disturbing `media`/`agora`/`cloud`.
- `kibbled` (26205) — the already-deployed first-party Kibble agent skeleton — is running right now
  (1 thread, 184 KB RSS) alongside all of the above with zero disruption, holding `/msg_dispatch_8`
  and its own listening socket open, but at observation time does **not** map
  `media_buffer_frame_buf` — it hasn't exercised the video-ring path yet, but its clean coexistence
  demonstrates the process-level pattern works.

**Record layout — the first ~0x2C0 bytes are a reader-registration table, not frame data.**
Two live captures taken ~1s apart (`od -A x -t x1 -N512` then `hexdump -C -N512`) were byte-diffed;
identical bytes are static/registration fields, differing bytes are live/dynamic counters
**[LIVE, high confidence for structure, lower confidence for individual field semantics — see
below]**:

```
000000  00 00 00 00 00 00 00 00 00 00 00 00 80 00 00 00   <- slot 0: no name (writer/global header)
000010  00 00 00 00 00 00 00 00 5b 23 14 00 75 17 14 00      two u32s here changed +2 between captures
000020  0b d4 7f 00 fa 83 3e 00 05 58 3e 00 63 6c 6f 75      "clou" name begins at 0x2C
000030  64 5f 72 65 61 64 65 72 31 37 00 00 00 00 07 00   <- "cloud_reader17\0"  (slot 1)
000040  00 00 00 00 00 00 00 00 00 04 00 00 78 0f 07 00
000050  01 00 00 00 00 00 00 00 63 6c 6f 75 64 5f 72 65   <- "cloud_reader216\0" begins (slot 2, @0x58)
000060  61 64 65 72 32 31 36 00 01 00 10 00 00 00 00 00
000070  00 00 00 00 00 04 00 00 70 17 07 00 01 00 00 00
000080  00 00 00 00 63 6c 6f 75 64 5f 72 65 61 64 65 72   <- "cloud_reader311\0" begins (slot 3, @0x84)
000090  33 31 31 00 02 00 0b 00 00 00 00 00 00 00 00 00
0000a0  00 04 00 00 78 1b 07 00 01 00 00 00 00 00 00 00
0000b0  65 76 65 6e 74 5f 72 65 61 64 65 72 31 30 00 00   <- "event_reader10\0" (slot 4, @0xB0)
0000c0  03 00 00 00 00 00 00 00 00 00 00 00 00 04 00 00
0000d0  e0 20 07 00 01 00 00 00 00 00 00 00 61 67 6f 72   <- "agora_read_19\0" begins (slot 5, @0xDD)
0000e0  61 5f 72 65 61 64 5f 31 39 00 00 00 04 00 09 00
0000f0  94 26 12 00 1d 66 33 00 11 0b 01 00 80 d8 c3 b3
000100  01 00 00 00 00 00 00 00 61 67 6f 72 61 5f 72 65   <- "agora_read_25\0" begins (slot 6, @0x101)
000110  61 64 5f 32 35 00 00 00 05 00 05 00 93 a5 12 00
000120  42 cb 0c 00 95 9d 01 00 f8 1a c0 b3 01 00 00 00
000130  00 00 00 00 61 75 69 64 6f 2d 6f 75 74 00 00 00   <- "auido-out\0" [sic] begins (slot 7, @0x134)
000140  00 00 00 00 06 00 02 00 65 d8 0e 00 ed 13 42 00
000150  00 04 00 00 c0 df 51 ae 01 00 00 00 01 00 00 00
000160  00 00 00 00 ...                                    <- slot 8: empty (all zero)
000170  07 00 00 00 ...                                    <- slot 9: index byte only, else empty
000190  ... 08 00 00 00 (slot 10, ~0x19C)  ... 09 00 00 00 (slot 11, ~0x1C8)  ... 0a 00 00 00 (slot 12, ~0x1F4)
```

**Structural facts (HIGH confidence, directly measured):**
- **Fixed 44-byte stride.** `cloud_reader216`'s name starts at 0x58, `cloud_reader17`'s at 0x2C
  (Δ=0x2C=44); `cloud_reader311`'s starts at 0x84 (Δ from 0x58 = 0x2C=44 again). Every record is a
  fixed 44-byte slot regardless of name length (short names are zero-padded to the boundary).
- **Slot 0 (bytes 0x00–0x2B) carries no name** — it's a global header, not a named reader; its
  fields are the ones observed changing between the two ~1s-apart captures (two u32s at 0x18/0x1C
  incrementing by exactly 2, and a larger-magnitude field at 0x20 jumping by ~14,000) — consistent
  with global write-position/sequence/tick bookkeeping owned by the writer (`media`), not a reader.
- **7 named slots currently populated** (3 cloud, 1 event, 2 agora, 1 audio), **≥5 more empty slots**
  visible in just the first 512 bytes (index markers 7/8/9/10 seen standalone with otherwise-zero
  surrounding bytes) — the table has real spare capacity.
- Each named slot's trailing bytes mix a small monotonic-looking integer (plausibly a per-slot
  index/generation, e.g. `01 00 10 00` for slot 2 vs `02 00 0b 00` for slot 3) with at least one
  field that is either static or slow-changing across the two samples — **exact field-by-field
  semantics (which byte is a read-cursor vs. a pid vs. a last-seen-pts) were not pinned down**; this
  is stated explicitly as not fully recovered.

**Per-frame video/audio payload header (codec/pts/size/keyframe), beyond the registry table —
NOT independently recovered.** An 8 KB sample (`od -N8192`) shows the registry ending and a
high-entropy region beginning around offset 0x400 (byte-nonzero-density jumps from ~25–50% in the
registry area to >99% from 0x400 onward — consistent with compressed bitstream), but scanning that
whole 8 KB window found **no H.264 Annex-B start codes** (`00 00 00 01` / `00 00 01`) — the one
apparent match at 0x159 is a false positive inside the registry's own numeric fields, not bitstream
data. This argues *against* raw unprefixed Annex-B storage and *for* a length/type-prefixed custom
record (consistent with `AX_VENC_GetStream`'s own descriptor-based C API, which hands the writer an
explicit per-NALU length+type rather than requiring it to re-derive boundaries from start codes) —
but this is **[INFERENCE]**, not a confirmed struct layout. Static recovery of the exact writer
function and its record-struct field offsets was attempted and **did not succeed**: `/app/bin/media`
is a stripped, non-PIE, Thumb-2 ET_EXEC binary with no `.symtab`, no ARM/Thumb ELF mapping symbols
(`$a`/`$t`/`$d`), and the analysis environment has no on-box `objdump`/binutils. A `capstone`
linear-sweep disassembly (with `skipdata` resync) of `.text` found 68,933 plausible instructions but
**zero** reliable cross-references — via `movw`/`movt` pairs, PC-relative literal-pool `ldr`, or raw
pointer-table scan — to the `"/media_buffer_frame_buf"` path string, the `"auido-out"` string, or any
of the six `dispatch_handler_*` name strings checked, despite those strings unquestionably being used
somewhere (they're live-observed and/or clearly present in `.rodata`). This is reported as an
explicit gap rather than guessed. **What a follow-up session could do to close it**: correlate two
`od` snapshots taken a known ~40 ms apart during active streaming against `VENC`'s own per-channel
`FrmEnc` delta to locate the write cursor empirically, or (safer, no more static-tooling dead ends)
write a tiny throwaway reader that attaches as a new named slot and logs raw byte offsets/lengths it
receives from `sem_timedwait` wake-ups.

**Third-reader assessment: A NEW reader CAN attach without disturbing `media`/`agora`/`cloud`.**
Evidence: (1) the registry has spare slots (§ above); (2) the two existing readers already use two
*independent* synchronization strategies (agora = semaphore-notified, cloud = apparently polling)
against the same segment with no coordination between them — the protocol is inherently
multi-reader-safe by construction, not a fragile 2-party pairing; (3) `agora`'s reader-side code uses
only vanilla POSIX (`shm_open`, `mmap`, `sem_open`, `sem_post`, `sem_timedwait`, confirmed
**[STATIC]** imports in `/app/bin/agora` — **zero** `AX_NT_*` imports), so it depends on no
proprietary Axera client library and is directly reproducible by a third process; (4) the write side
(`media`) posts to specific named semaphores and updates the shared header — nothing about that
requires foreknowledge of readers, so adding one is additive. Recommended shape for a Kibble/Scrypted
reader: `shm_open("/media_buffer_frame_buf", O_RDWR)` + `mmap`, claim one of the visibly-empty
registry slots with a distinct name (e.g. `kibble_reader_N`), `sem_open` a new
`sem.media_buffer_reader_N`, and `sem_timedwait` for wake-ups exactly as `agora` does.

## 5. JPEG snapshots — hardware path, confirmed

**[STATIC, confirmed this session]**: `/app/bin/media` imports `AX_VENC_JpegEncodeOneFrame`
directly — there is **no separate `AX_JENC_*` userspace symbol family** imported anywhere in
`media`, despite a dedicated `ax_jenc.ko` kernel module and a live `/proc/ax_proc/jenc` node
existing (confirmed **[LIVE]**: `ax_jenc V3.0.0`, `MaxChnNum 16`, but **zero active channels** at
observation time — JPEG channels are opened transiently, one-shot, per snapshot, not held open like
the 3 continuous VENC channels). This means: JPEG snapshots go through the same `AX_VENC` userspace
API surface as video (a JPEG-payload-type one-shot encode call), while the dedicated JENC hardware
block/kernel driver does the actual work underneath, invisible to `media`'s own import table.
Two resolutions are produced, per string literals `/tmp/snap_main.jpeg` and `/tmp/snap_sub.jpeg`
**[STATIC]**, corroborated by `STUDY-app.md` **[DOC]** — consistent with one-shot JPEG encodes taken
from the main (1728×1080-or-native) and sub (1152×720) IVPS-scaled stages. `AX_VENC_RequestIDR`
**[STATIC]** is also imported — used to force an immediate keyframe, most plausibly right before a
live-view session starts so the first frame a new viewer/uploader sees is already an I-frame.

## 6. CPU / NPU / VPU headroom

**[LIVE]**, `top -b -n2 -d1` (two 1-second-apart samples) + `/proc/201/status` +
`/proc/ax_proc/{venc,mem_cmm_info}`:

- System: 2× Cortex-A53 online (`Cpus_allowed_list: 0-1`), loadavg 7.10/7.37/7.58 (matches the
  ~7.6 figure in the assignment brief — stable, not a spike).
- `top` summary (normalized to 2-core = 100%): **usr 30.5–31.8%, sys 4.5–5.9%, idle 63.5–63.6%.**
  → roughly **1.27 of 2 cores sit idle right now**, even with `media` actively running 3 concurrent
  H.264 encodes plus the continuous ISP/AI/NPU-detection pipeline.
- `media` (201) alone: **~31.6–31.8%** of the 2-core budget in both samples — it accounts for
  essentially all of the system's "usr" time; every other userspace process (`ctrl`, `cloud`,
  `agora`, `logUpload`, `ble`, `watchdog`) shows **0.0% CPU** in both samples (fully event-driven,
  idle). `media`: 26 threads, VmRSS 24,148 KB (23.6 MB), VmPeak/HWM 32,368 KB.
- VENC hardware ceiling: `MaxChnNum 16` **[LIVE]** — only 3 of 16 channel slots in use, **13 free**.
  JENC likewise `MaxChnNum 16`, 0 held open (transient use).
- CMM (media/NPU DRAM pool): **121 MB used / 160 MB total → 38 MB (23.6%) free** **[LIVE,
  `/proc/ax_proc/mem_cmm_info`]**, including distinct, already-reserved per-channel regions
  (`venc_fifo_chn0/1/2`, `venc_ewl_chn0/1/2`) showing the 3 active channels each have their own
  hardware working buffers already carved out — a 4th channel would get its own new region from the
  38 MB free pool.
- NPU: the pet/food detection pipeline (`libalgo.so`, `dlopen`'d in-process, up to 8 concurrent
  `AX_ENGINE` handles per `STUDY-alg.md` **[DOC]**) runs on the dedicated NPU silicon, not the ARM
  cores — it does not show up in the ARM `%CPU` figures above at all; it is a separate resource
  budget from VENC/CPU headroom.

**Conclusion: a new VENC channel (substream #2, or a dedicated RTSP-bridge-optimized stream) would
be encoded entirely by the same dedicated hardware VENC IP block already driving chn0–2** — not a
software re-encode. Its ARM-CPU cost is limited to IVPS-scale-stage setup, buffer/ring bookkeeping,
and copying each already-encoded NALU into the ring — not H.264 compression math. Given ~1.27 idle
cores, 13/16 free hardware channel slots, and 38 MB/160 MB free CMM, this is inexpensive on every
axis measured. **In practice it's moot for the immediate goal**: chn1 (the substream) already exists,
already runs continuously, and is already sitting in the same ring `agora`/`cloud` read from.

## 7. Scrypted / HomeKit fit

**Codec question — answered definitively: chn0/1/2 are ALL H.264** (`Type` column in
`/proc/ax_proc/venc`, live, all three rows say `h264`), **despite the SoC/SDK supporting H.265**
(`STUDY-soc.md` **[DOC]**: "H.264/H.265 5MP@30"). Petkit's firmware simply never turns H.265 on.
**This means HomeKit's H.264-only requirement is met as-is — no transcode needed for codec.**

What the mainstream (chn0) gives Scrypted as-is: **H.264, 1728×1080, ~25 fps, CBR ~1280–1784 kbps,
4-second GOP** (H.264 profile not captured, §2/§8). What the substream (chn1) gives it: **H.264,
1152×720, ~25 fps, CBR ~800 kbps target/~364 kbps actual, 4-second GOP**. Both are continuously
hardware-encoding today regardless of viewers — attaching a ring reader costs zero additional encode
work, it's a pure "read bytes already being produced" operation.

The 4-second GOP is long for "join mid-stream, see video fast" UX, but `AX_VENC_RequestIDR`
**[STATIC, confirmed imported]** lets a bridge process force an immediate keyframe the moment a new
RTSP client connects, without needing to touch `media`'s own channel configuration (which would be
both out of scope for a passive tap and risky to the vendor pipeline).

**Recommended lowest-latency design** (matches `STUDY.md`'s existing plan, now with live
confirmation it's sound): a first-party reader process attaches to `media_buffer_frame_buf` using
the same plain-POSIX protocol `agora` already uses (§4), reads chn1 (or chn0) NALUs with **zero
re-encode**, and serves them over RTSP itself — building proper Annex-B framing / an SPS+PPS-bearing
keyframe payload for RTSP muxing from whatever framing the ring actually uses (not byte-pinned, §4,
but NALU boundaries and type are almost certainly present given the source API's descriptor-based
design). Audio: Scrypted/HomeKit want AAC or PCM in the RTSP `SETUP`; nothing in this pipeline
produces AAC in real time (`AX_ADEC` decodes AAC, it doesn't encode it — the vendor's live-audio
codec is Opus via Agora), so a from-scratch RTSP audio track would need either a light PCM→AAC
encode step in the bridge process (cheap on ARM, tiny frames) or presenting raw PCM if the downstream
client accepts it.

**Talkback into HomeKit**: push audio to the feeder's speaker via the **same `"audio-out"` /
`audio_out_thread` path `agora` already uses** (§3) — 16 kHz/16-bit PCM is what `AX_AO_SendFrame`
physically plays, so a bridge need only get PCM at that rate/depth to the right hand-off point
(exact IPC primitive for that hand-off is the one open item flagged in §3/§4). The alternative,
lower-effort but **not real two-way audio**, is the canned-prompt file-drop path
(`dispatch_handler_play_aac_file` + an AAC file under `/tmp` or `/audio`) — usable only for
pre-recorded announcements, not live conversation.

## 8. Open questions / not recovered

1. H.264 profile (Baseline/Main/High) for chn0–2 — not exposed by `/proc/ax_proc/venc`, not pinned
   statically.
2. Exact per-frame ring record header (codec tag / pts / size / keyframe flag byte offsets) beyond
   the registry table — see §4's detailed methodology note; registry structure is solid, payload
   record is not.
3. Exact numeric `msg_id` values for `speak_start`/`speak_stop`/`speaker_enable`/`play_aac_file` —
   handler *names* and mechanism are confirmed by direct string evidence; the wire integers were not
   recoverable via the same xref techniques that worked for `ctrl`'s `dispatch_handler_feed`
   (`STUDY-msgids.md` **[DOC]**) — `media`'s build apparently doesn't materialize these particular
   name-string addresses in a way a stripped-binary/no-mapping-symbols/no-objdump capstone sweep can
   trace; would need a live `strace`-equivalent or an emulator, neither available in this
   environment.
4. Exact inter-process hand-off primitive for mic-PCM-out and "audio-out"-PCM-in between `media` and
   `agora` (ring slot vs. mqueue frames vs. something else) — named-string evidence is solid, the
   specific IPC mechanism is [INFERENCE].
5. `sem.media_buffer_reader_6`'s origin — orphaned semaphore, plausibly from earlier study-session
   activity today; not chased further (low value, no device risk either way).

## 9. Recommendations

- **Substream: EXISTS, already hardware-encoded, already running.** Chn1 (1152×720@25fps H.264,
  ~800 kbps class) is directly consumable from the existing ring with the same reader protocol
  `agora` uses — no new VENC channel needs to be created for a first cut. If a differently-tuned
  stream is wanted later (different resolution/bitrate/GOP than chn0/chn1), a **new 4th channel
  would also be hardware-encoded** (13/16 VENC slots free, ~1.27 idle ARM cores, 38 MB free CMM) —
  cheap, but not necessary for the immediate goal.
- **Talkback: path = the `"audio-out"` hand-off into `media`'s `audio_out_thread` →
  `AX_AO_SendFrame` → ALSA `pcmC0D1p`; format = 16 kHz/16-bit PCM at that boundary.** This is the
  same physical path Agora's downlink audio already uses today, confirmed by matching string
  evidence (`"auido-out"` in both `media`'s rodata and the live ring registry) and live AO
  attributes. It is architecturally distinct from, and should not be confused with, the
  AAC-canned-prompt path (`dispatch_handler_play_aac_file`), which is for pre-recorded
  announcements only.

## Appendix — raw live captures referenced above

`/proc/ax_proc/venc` (2026-09-15 06:19 UTC, abridged to the three data tables used above — full
output was captured and is reproducible with the same command):
```
-------- VENC CHN ATTR 1 ------------------------
ID    Type    wSrc    hSrc    ... PixFmt  ...
0     h264    1728    1080    ... NV12    ...
1     h264    1152    720     ... NV12    ...
2     h264    1152    720     ... NV12    ...
-------- VENC RC ATTR 1 ------------------------
ID    RcMode    SrcFr   DstFr   Br(kbps)  MinQp   MaxQp   MinIQp  MaxIQp ...
0     CBR       25.00   25.00   1280      12      45      12      45   ...
1     CBR       25.00   25.00   800       30      51      30      51   ...
2     CBR       25.00   5.00    800       39      51      39      51   ...
-------- VENC GOP ATTR  ------------------------
ID    GopMode   Gop   GopVI
0     NormalP   100   0
1     NormalP   100   0
2     NormalP   10    0
-------- VENC STATUS 2 -------------------------
ID  ChnStat  FrmRecv  FrmEnc  OutFps  RealBr    PID  TID  ThNum
0   STARTED  462189   462189  25.02   1784.07   201  211  3
1   STARTED  462189   462189  25.01   364.14    201  212  3
2   STARTED  462202   92440   5.00    6.25      201  213  3
```

`/proc/ax_proc/link_table`:
```
(VIN 0 0) -> (IVPS 0 0)
(IVPS 0 1) -> (VENC 0 2)
(IVPS 0 1) -> (VENC 0 1)
(IVPS 0 0) -> (VENC 0 0)
```

`od -A x -t x1 -N512 /dev/shm/media_buffer_frame_buf` — first 0x160 bytes (registry table, see §4
for the full annotated version and field-boundary reasoning):
```
000000 00 00 00 00 00 00 00 00 00 00 00 00 80 00 00 00
000010 00 00 00 00 00 00 00 00 59 23 14 00 73 17 14 00
000020 c4 f9 7f 00 fa 83 3e 00 05 58 3e 00 63 6c 6f 75
000030 64 5f 72 65 61 64 65 72 31 37 00 00 00 00 07 00
000040 00 00 00 00 00 00 00 00 00 04 00 00 78 0f 07 00
000050 01 00 00 00 00 00 00 00 63 6c 6f 75 64 5f 72 65
000060 61 64 65 72 32 31 36 00 01 00 10 00 00 00 00 00
000070 00 00 00 00 00 04 00 00 70 17 07 00 01 00 00 00
000080 00 00 00 00 63 6c 6f 75 64 5f 72 65 61 64 65 72
000090 33 31 31 00 02 00 0b 00 00 00 00 00 00 00 00 00
0000a0 00 04 00 00 78 1b 07 00 01 00 00 00 00 00 00 00
0000b0 65 76 65 6e 74 5f 72 65 61 64 65 72 31 30 00 00
0000c0 03 00 00 00 00 00 00 00 00 00 00 00 00 04 00 00
0000d0 e0 20 07 00 01 00 00 00 00 00 00 00 61 67 6f 72
0000e0 61 5f 72 65 61 64 5f 31 39 00 00 00 04 00 09 00
0000f0 94 26 12 00 1d 66 33 00 11 0b 01 00 80 d8 c3 b3
000100 01 00 00 00 00 00 00 00 61 67 6f 72 61 5f 72 65
000110 61 64 5f 32 35 00 00 00 05 00 05 00 93 a5 12 00
000120 42 cb 0c 00 95 9d 01 00 f8 1a c0 b3 01 00 00 00
000130 00 00 00 00 61 75 69 64 6f 2d 6f 75 74 00 00 00
000140 00 00 00 00 06 00 02 00 65 d8 0e 00 ed 13 42 00
000150 00 04 00 00 c0 df 51 ae 01 00 00 00 01 00 00 00
```

`/proc/<pid>/maps` shm/sem lines (2026-09-15 06:2x UTC):
```
media(201):  rw-s /dev/shm/sem.media_buffer_reader_4 (ino 12061)
             rw-s /dev/shm/sem.media_buffer_reader_5 (ino 11794)
             rw-s /dev/shm/media_buffer_frame_buf     (ino 5275)
             rw-s /dev/shm/config_shm                 (ino 4380)
agora(268):  rw-s /dev/shm/media_buffer_frame_buf     (ino 5275)
             rw-s /dev/shm/<unlinked> (ino 12061)  <- same inode as media's sem.media_buffer_reader_4
             rw-s /dev/shm/<unlinked> (ino 11794)  <- same inode as media's sem.media_buffer_reader_5
             rw-s /dev/shm/config_shm                 (ino 4380)
cloud(269):  rw-s /dev/shm/media_buffer_frame_buf     (ino 5275)
             rw-s /dev/shm/config_shm                 (ino 4380)
             (no sem.media_buffer_reader_* mapped)
```

`media`'s linked libraries relevant to this study (from `/proc/201/maps`, all under `/soc/lib` or
`/app/lib` unless noted): `libax_venc.so`, `libax_ivps.so`, `libax_ae.so`/`libax_awb.so`/
`libax_af.so` (3A), `libax_proton.so` (ISP core), `libax_engine.so`/`libax_interpreter.so` (NPU
runtime — only `AX_ENGINE_Deinit` actually imported by `media` itself, per `STUDY-alg.md`),
`libax_mipi.so`, `libax_nt_stream.so`/`libax_nt_ctrl.so` (network-transport plumbing — linked
directly by `media`, **not** by `agora`), `libax_audio.so`/`libax_audio_3a.so`, `libax_fdk.so` +
`libfdk-aac.so.2.0.1` (AAC), `libtinyalsa.so.2.0.0`, `libsamplerate.so.0.2.2`, `libsns_gc2083.so`
(active sensor driver), plus glibc/libstdc++/libssl/libcurl.
