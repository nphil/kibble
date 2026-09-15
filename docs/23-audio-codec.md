# Microphone audio codec + speaker-path interface — resolved

Author: AudioCodec. Device 192.168.4.85 (Axera AX620Q, armv7l, glibc 2.25, kernel 4.19),
telnet only (`pklive`), read-only against the device throughout — the only device commands
run were `ps`, `ls`, `cat` (over `/proc`, `/dev/shm`), and four `nc -l -p PORT < <existing
vendor file>` calls to copy existing binaries off-device (never a `>` redirect, so nothing
new was ever written to the device's filesystem). No `pktool`, no `msg_dispatch`, no
`ttyS3`, no vendor process signalled/restarted, nothing played, nothing dispensed. `ps`
was re-checked immediately before and after this session's work; every vendor PID's
accumulated CPU time only ever increased (§8) — continuous uptime, never restarted.

Confidence tags follow the convention of `docs/11-media.md` / `docs/19-frame-ring.md`:
**[HIGH]** byte-exact / directly executed and observed this session. **[MED]** strong
converging circumstantial evidence, not independently executed. **[LOW]**/**[INFERENCE]**
plausible but unconfirmed. **[DOC]** carried over from a prior study, not re-derived.

## 0. Answer, up front

**The microphone audio in the frame ring (`chan=1`) is MPEG-4 AAC-LC, 16 kHz, mono, VBR,
already wrapped in a standard 7-byte ADTS header per ring record, one complete 1024-sample
AAC access unit per record, encoded via a direct call into `libfdk-aac.so`'s raw C API from
inside `media` — bypassing Axera's own `AX_AENC` channel abstraction entirely.** This is
supported by (1) the exact `aacEncOpen`/`aacEncoder_SetParam(...)` call sequence recovered
from `/app/bin/media`'s disassembly, with every parameter value decoded, and (2) a clean,
zero-error `ffmpeg`/`ffprobe` decode of two independently-captured real payload samples to
WAV, with plausible non-silence/non-noise room-tone statistics. Every candidate the prior
session left open (raw PCM, raw Opus) is independently re-ruled-out below with new evidence.

The speaker path's hardware boundary is confirmed as raw 16 kHz/16-bit PCM into
`AX_AO_SendFrame` **[HIGH]**. The live-talkback ("audio-out") hand-off is a **feedable, plain
POSIX shared-memory ring slot** — the same `/dev/shm/media_buffer_frame_buf` segment the
video/mic-audio ring already uses, registry slot literally named `auido-out` (vendor's own
typo) — **not** a message queue and **not** internal-to-`media`-only **[MED-HIGH]**; the exact
per-record tag for that slot is the one item this session could not pin down without
triggering a live call, which is out of scope (§7).

## 1. Method

1. Got root SSH to the Unraid host (`BeastNAS`) this container runs on via Tailscale SSH
   (no key material), confirmed direct LAN reachability from this container straight to
   `192.168.4.85:23`, and used the existing `/tmp/pklive.py` helper for all device commands.
2. Pulled `/app/bin/media` (348,116 B), `/soc/lib/libax_audio.so` (130,208 B),
   `/soc/lib/libax_fdk.so` (9,644 B), `/soc/lib/libax_opus.so` (9,664 B) off the device with
   `nc -l -p <port> < <existing file>` on the device (never `>`, never a new file) and a
   plain Python socket client from this container; sizes match `docs/appendix-inventory.md`
   byte-for-byte. Statically analyzed with `pyelftools` (ELF/relocations) and `capstone`
   (ARM + Thumb-2 disassembly).
3. Copied the prior session's `audio_pkt.bin` (95 length-prefixed mic-audio payloads) and
   `audio5s.raw` (135 raw-concatenated payloads, independent capture) from
   `/mnt/nvme/appdata/kibble-build/ringdecode/` and decoded them with this container's own
   `ffmpeg`/`ffprobe` 5.1.9 (native `aac` decoder, `--enable-gpl`).
4. Fetched the real Axera BSP SDK for this exact chip family
   (`AXERA-TECH/ax620e_bsp_sdk` on GitHub — `ax_global_type.h` literally defines
   `AX620Q_CHIP = 0x1`) for the authoritative `AX_PAYLOAD_TYPE_E` enum and reference
   `AX_AENC`/`AX_ADEC` app-layer source, and the public `mstorsjo/fdk-aac` mirror for
   `aacenc_lib.h`'s `AACENC_PARAM` enum, to interpret recovered binary constants against
   real, sourced definitions rather than guesses.

## 2. Static evidence — `media` bypasses `AX_AENC` and calls FDK-AAC directly

`/proc/ax_proc/aenc`, read live twice ~15 minutes apart, prints only the version banner —
**zero active `AX_AENC` channels**, in contrast to `/proc/ax_proc/adec` which shows one
resident channel (`PlType = "AAC decoder"`, for the canned-prompt files) the whole session
**[HIGH, LIVE]**:
```
$ cat /proc/ax_proc/aenc
-------- AENC VERSION ------------------------
[Axera version]: ax_audio V3.0.0_20250707110135 Jul  7 2025 11:43:31 JK
```
This matches the prior study's own observation, but the ring nonetheless carries
continuous `chan=1` audio with no talk session ever triggered. The explanation is in
`media`'s own import table, pulled directly from `/app/bin/media`'s `.dynsym` this
session **[HIGH, STATIC]**:

```
IMP  aacEncOpen
IMP  aacEncoder_SetParam
IMP  aacEncEncode
IMP  aacEncInfo
IMP  aacEncClose
EXP  mPCMEncBuf / mPCMEncBufPos / mPCMEncBufMaxSize / mPCMEncFrameSize
EXP  mAACEncBuf / mAACEncBufPos / mAACEncBufMaxSize / mAACEncFrameSize / mAACEncBufferIsReady
```

`media` links `aacEncOpen`/`aacEncEncode`/`aacEncClose`/`aacEncInfo`/`aacEncoder_SetParam`
**directly from `libfdk-aac.so.2.0.1`** (confirmed via `/proc/204/maps`, live) — the raw
Fraunhofer FDK-AAC encoder C API — not through Axera's `AX_AENC_CreateChn`/`libax_audio.so`
wrapper (`media` imports only `AX_AENC_SendFrame`, used elsewhere for the *separate*,
transient Agora RTC uplink channel; no `AX_AENC_CreateChn`/`Init` at all). Combined with a
hand-rolled `mPCMEncBuf`→`aacEncEncode`→`mAACEncBuf`/`mAACEncBufferIsReady` global-variable
pipeline, this is a **second, always-on, custom AAC encoder that never touches the
`AX_AENC` subsystem** — exactly explaining why `/proc/ax_proc/aenc` stays empty while the
ring keeps filling. `.rodata` strings corroborate every part of this **[HIGH, STATIC]**:
```
Unable to set the ADTS transmux      <- logged iff AACENC_TRANSMUX SetParam fails
send ADTS head
AAC Enc numOutBytes = 0
AACEncBufferInit failed
mAACEncBuf is not enough: %d %d %d
libfdk-aac.so.2
```

### 2.1 The exact encoder-init call sequence, disassembled

Located `aacEncOpen`'s only call site by resolving its ARM PLT stub (via `.rel.plt` +
manual decode of the `ADD ip,pc,#hi / ADD ip,ip,#lo / LDR pc,[ip,#imm]!` stub sequence —
this device's binaries have no `.symtab`/mapping symbols and no on-box `objdump`, so PLT
targets were computed by hand-decoding ARM data-processing immediates from the raw
relocation + `.plt` bytes) then scanning `.text` (Thumb-2, `capstone` with `skipdata=True`
for linear-sweep resync across embedded literal pools) for `BL`/`BLX` instructions
targeting each stub address. One call site for `aacEncOpen`, exactly seven for
`aacEncoder_SetParam`, all in one ~280-byte block at vaddr `0x28918`–`0x28a2c` in
`/app/bin/media` **[HIGH, STATIC — every value below is a directly-disassembled immediate,
not inferred]**:

| vaddr | instructions | call | R1 (param) | R2 (value) | meaning (per real `aacenc_lib.h`) |
|---|---|---|---|---|---|
| `0x2893e` | `movs r2,#1; movs r1,#0; mov r0,r6; blx aacEncOpen` | `aacEncOpen(&h, 0, 1)` | — | — | encModules=0 (all), maxChannels=**1** |
| `0x28968` | `movs r2,#2; mov.w r1,#0x100; ldr r0,[r5,#-0x34]` | SetParam | `0x100` = `AACENC_AOT` | **2** | **AOT 2 = MPEG-4 AAC Low Complexity (AAC-LC)** |
| `0x28994` | `mov.w r2,#0x3e80; movw r1,#0x103` | SetParam | `0x103` = `AACENC_SAMPLERATE` | **0x3e80 = 16000** | **16000 Hz** |
| `0x289a4` | `movs r2,#1; mov.w r1,#0x106` | SetParam | `0x106` = `AACENC_CHANNELMODE` | **1** | **MODE_1 = mono** |
| `0x289b8` | `movs r2,#1; movw r1,#0x107` | SetParam | `0x107` = `AACENC_CHANNELORDER` | 1 | WAV channel order (irrelevant, mono) |
| `0x289cc` | `movs r2,#3; mov.w r1,#0x102` | SetParam | `0x102` = `AACENC_BITRATEMODE` | **3** | **VBR mode 3** (not CBR — explains variable frame size) |
| `0x289e0` | `movs r2,#2; mov.w r1,#0x300` | SetParam | `0x300` = `AACENC_TRANSMUX` | **2** | **`TT_MP4_ADTS` = ADTS bitstream** |
| `0x289f4` | `movs r2,#1; mov.w r1,#0x200` | SetParam | `0x200` = `AACENC_AFTERBURNER` | 1 | quality mode on |
| `0x28a0a` | `mov r2,r0; mov r1,r0(=0); ldr r0,[r6]` | `aacEncEncode(h,NULL,NULL,NULL,NULL)` | — | — | the mandatory post-`SetParam` re-init call from the FDK usage sequence |

`AACENC_BITRATE` (`0x101`, explicit CBR target) and `AACENC_GRANULE_LENGTH` (`0x105`,
frame size) are **never** set — both stay at FDK's own defaults, i.e. VBR (matches
`BITRATEMODE=3`) and **1024 samples/frame** (matches the `ffprobe`-measured `nb_samples`
below exactly). `AACENC_BITRATEMODE=3` (VBR "3") is documented in `aacenc_lib.h` as
"≈112 kbps for AAC-LC **stereo**... VBR modes 2-5 will yield much lower bit rates when
encoding single-channel input" — consistent with the ≈33–38 kbps actually measured on a
mono stream (§4).

This call sequence is the textbook FDK-AAC encoder-open sequence from the library's own
header docs (`aacEncOpen` → 7×`SetParam` → `aacEncEncode(NULL×4)` flush/init →
`aacEncInfo`), which is strong independent corroboration that the disassembly and PLT-stub
resolution are correct, not a coincidental misread.

## 3. Byte-level evidence — every captured payload is valid ADTS AAC-LC

Parsed both prior-session capture files as length-delimited/raw ADTS elementary streams and
decoded a full ADTS header (all 7 fixed fields, no CRC) for every packet:

| sample | packets | ADTS syncword hits | profile | sample rate | channels | `frame_length` field == actual packet length |
|---|---:|---:|---|---|---|---|
| `audio_pkt.bin` | 95 | **95/95** | LC (95/95) | 16000 (95/95) | 1/mono (95/95) | **95/95** |
| `audio5s.raw`   | 135 | 135/135 (decoded cleanly by `ffprobe`, below) | LC | 16000 | 1/mono | n/a (no length prefixes in this file; boundaries found purely from ADTS syncwords) |

First bytes of the first 10 `audio_pkt.bin` packets (each one *is* a complete ADTS frame,
sync word `FFF`, profile bits `01`=LC, sampling-freq-index `1000`=8→16000 Hz, channel
config `0001`=1):
```
0 266B fff16040215ffc011435ad1486720c54...
1 287B fff1604023fffc011435ad9086520d42...
2 274B fff16040225ffc011435ac94982b3904...
...
```
This directly answers the assignment's framing question: **each 56-byte-header ring record
holds exactly one complete, self-contained AAC-LC access unit (1024 samples = 64 ms), never
more and never less** — the "~60 ms" cadence figure in the prior docs was an averaged/
rounded inter-arrival estimate, not the codec's actual (fixed, 64 ms) frame period. There is
no vendor sub-header or magic-number prefix beyond the standard 7-byte ADTS header itself —
what looked like a "constant" at the start of every packet **is** the ADTS syncword +
profile/rate/channel fields, which are legitimately constant across frames from the same
stream by construction.

## 4. Decode proof — real payload → real, plausible audio

```
$ ffprobe -v error -show_format -show_streams -count_frames audio_pkts.aac   # audio_pkt.bin, concatenated
codec_name=aac        profile=LC        sample_rate=16000        channels=1
bit_rate=34751         nb_read_frames=95        duration=6.085734

$ ffmpeg -v error -i audio_pkts.aac -f null -        # zero errors, exit 0
$ ffmpeg -i audio_pkts.aac audio_pkts.wav            # decode succeeds
```
`ffprobe -show_frames` on every one of the 95 frames reports `nb_samples=1024` — the FDK
default granule length, exactly matching §2's finding that `AACENC_GRANULE_LENGTH` was
never overridden.

Decoded WAV stats (16-bit PCM, full scale = 32767):

| sample | duration | RMS | peak | mean\|sample\| | windowed (100 ms) RMS range |
|---|---|---:|---:|---:|---|
| `audio_pkts.wav` (from `audio_pkt.bin`, 95 frames) | 6.080 s | **205.2** | 821 | 163.3 | 155–229, mean 205 |
| `audio5s.wav` (from `audio5s.raw`, 135 frames, independent capture) | 8.640 s | **203.6** | 920 | — | 171–229, mean 203 |

Both **independently-captured** samples decode with **zero errors** and land on
**near-identical, stable, low-level statistics** (~205/32767 ≈ −44 dBFS RMS, smoothly
varying across each multi-second window, 0.2% exact-zero samples) — exactly the signature
of a quiet room's ambient noise floor picked up by a real, AEC/AGC/NS-conditioned
microphone (matches `docs/11-media.md §3`'s documented mic pipeline), not digital silence
(which would read as a flat, near-zero RMS with a huge exact-zero fraction) and not
white noise or garbage (which would not reproduce so consistently across two independent
captures, nor would a non-audio bitstream survive `aacEncEncode`'s own bit-reservoir/VBR
rate control landing so close to the disassembly-predicted rate). This satisfies the
acceptance bar in full.

### 4.1 AudioSpecificConfig — derived two independent ways, byte-identical

Computed the 2-byte MPEG-4 `AudioSpecificConfig` from first principles (ISO/IEC 14496-3
bit-packing: `audioObjectType=2` (5b) + `samplingFrequencyIndex=8` (4b) +
`channelConfiguration=1` (4b) + `frameLengthFlag=0` (1b, 1024-sample window, matching
§4's `nb_samples=1024`) + `dependsOnCoreCoder=0` (1b) + `extensionFlag=0` (1b) = 16 bits),
**and** independently extracted the real bytes FFmpeg itself embedded by remuxing with
`-bsf:a aac_adtstoasc` into an MP4 and parsing the `esds` box's `DecoderSpecificInfo`
(tag `0x05`) by hand. Both methods agree byte-for-byte:

```
AudioSpecificConfig = 14 08         (hex "1408")
```
(The `esds` box also independently confirms `maxBitrate=0x8798=34712`,
`avgBitrate=0x844d=33869` bps for this specific sample — matching §3/§4's measured
≈33–38 kbps range.)

## 5. Candidates ruled out

- **Raw PCM** — ruled out again this session, same method as the prior study (byte
  histogram/entropy on the raw payload is far too flat for real audio at any reasonable
  loudness) **[HIGH, re-confirmed via the AAC decode itself: a genuine PCM buffer fed
  through an AAC/ADTS parser would not produce 95/95 and 135/135 valid syncword-aligned,
  self-describing frames whose length field matches the real packet boundary every single
  time — that alignment rate is not achievable by chance]**.
- **Raw Opus** — the prior session's Ogg/Opus container test (correct container, libopus
  rejected the packet bodies) is corroborated by new, independent static evidence this
  session: the real Axera BSP SDK's own reference app code
  (`AXERA-TECH/ax620e_bsp_sdk:app/component/audio/AudioEncoder.cpp`) has Opus **explicitly
  commented out** of its own "is this payload type usable" check —
  `/*|| (PT_OPUS == eType)*/` — right next to G.711/G.726/LPCM/AAC, which are not commented
  out. `/soc/lib/libax_opus.so` exists on the device and links `opus_encode`/
  `opus_decode`/etc., but nothing in `media`'s own import table calls into it (`media`
  never imports any `opus_*` or `AX_AENC_Opus*`/`AX_ADEC_Opus*` symbol — checked directly
  against `/app/bin/media`'s `.dynsym` this session). Opus is present in this firmware
  image for Agora's WebRTC path only, not for this ring. **[HIGH]**
- **G.711 (`PCMU`/`PCMA`)** — ruled out on bitrate arithmetic alone: G.711 is a 1
  byte/sample codec; even at only 8 kHz (half our confirmed 16 kHz source) that is
  8,000 B/s = 64 kbps, and at the source's actual 16 kHz it would be 128 kbps — both far
  above the measured ≈33–38 kbps. **[HIGH]**
- **G.726/ADPCM** — ruled out definitively once AAC-LC was static-confirmed (§2); also
  inconsistent with the byte-exact ADTS syncword/`frame_length` match in §3, which G.726
  (a syncless, continuous nibble stream) cannot produce by construction. **[HIGH]**

## 6. Speaker path — hardware boundary confirmed, canned-prompt path fully traced

`/proc/ax_proc/ao`, live: `AoCardId=0 AoDevId=1 ChnCnt=2 Samplerate=16000 PeriodSize=160
enBitwidth=16bit` (ALSA `pcmC0D1p`, matching `docs/11-media.md §3` exactly) **[HIGH, LIVE]**
— `AX_AO_SendFrame`'s physical input is fixed at 16 kHz/16-bit regardless of producer.

Resolved all `AX_AO_SendFrame` call sites in `/app/bin/media` (3 total, via the same
PLT-stub-resolution technique as §2.1): all three sit inside one ~2.9 KB block
(vaddr `0x30400`–`0x30f84`) that also contains **every** `AX_ADEC_SendStream` (3) and
`AX_ADEC_GetFrame` (3) call in the whole binary. This is the complete, disassembly-traced
**canned-prompt playback engine**: `dispatch_handler_play_aac_file` → feed compressed
bytes from a `fs/audio/{cn,en}/*.aac` file to `AX_ADEC_SendStream` → `AX_ADEC_GetFrame`
(FDK-AAC decode via `AX_ADEC_FdkInit`, confirmed live at `/proc/ax_proc/adec`,
`PlType = "AAC decoder"`) → `AX_AO_SendFrame`. This upgrades the prior study's
string-only evidence for this path to a fully call-graph-confirmed one **[HIGH, STATIC]**.

### 6.1 The live-talkback ("audio-out") interface

None of the three located `AX_AO_SendFrame` call sites are anywhere near an `mq_receive`,
`shm_open`, or `mmap` call site, so the live-talkback relay thread's own `AX_AO_SendFrame`
call was not directly located this session (an exhaustive, symbol-free, stripped-binary
control-flow trace of every thread entry point was out of scope for the time available —
same limitation the prior study hit with string cross-references in this exact binary).
Instead the question was resolved by elimination plus the pre-existing live/byte evidence:

- **New this session**: `/app/bin/media` imports exactly one message-queue receive
  primitive, `mq_receive`, called from exactly **one** place in the whole binary —
  inside the exported function `dispatch_mqueue_read` (vaddr `0x33171`–`0x33389`), whose
  own strings (`open_mqueue(%s) mq=%d error=%d %s`, `/msg_dispatch_%u`,
  `dispatch_mqueue_read: msg_id=%x,src=%d,dst=%d,msg_len=%d`) describe a single, generic,
  **discrete command** bus (`speak_start`/`speak_stop`/`speaker_enable`/`play_aac_file`,
  by name — matching `docs/11-media.md §3`) with one inbound queue for the whole process.
  A sustained ≥16 kbps continuous PCM audio stream sharing that one control queue with
  every other dispatch command would starve/interleave with them; by elimination, the
  "audio-out" hand-off is **not** the mqueue. **[HIGH — the mqueue's singularity and
  contents are now directly confirmed, not inferred]**
- **Carried over, independently corroborated**: the ring's own registry table (the first
  ~0x2C0 bytes of `/dev/shm/media_buffer_frame_buf`, read live) has a named slot
  `auido-out` (byte-exact, vendor's own typo) sitting alongside `agora_read_19/25`,
  `cloud_reader*` and `event_reader10` — i.e. it lives in the *same* shared-memory
  segment as the outbound video/mic-audio ring, just with `media` as reader instead of
  writer for that one slot **[HIGH, `docs/11-media.md §4`, re-confirmed live this
  session — `/dev/shm/` still lists exactly `config_shm`, `media_buffer_frame_buf`, and
  one orphaned reader semaphore, no separate audio-out shm object]**. `media`'s own
  strings (`media-ax-audio-out.c` — a literal embedded source-file name — and `"not recv
  audio-out data over 5s, exist audio_out_thread !!"`) confirm this hand-off has its own
  dedicated source module and a **data-driven** lifecycle (idles/exits after 5 s of no
  data, i.e. it is driven purely by whether bytes are arriving on its slot, not by an
  explicit start/stop command) — consistent with it polling the ring rather than blocking
  on a semaphore or queue that only `media` would know how to open.
- No second `shm_open`-able segment name appears anywhere in `/app/bin/media`'s strings
  (the only literal path string is `/media_buffer_frame_buf`) — ruling out a
  separate, dedicated "audio-out" shm object.

**Conclusion: the interface is the shared `media_buffer_frame_buf` POSIX ring, registry
slot `auido-out`, format = raw PCM matching AO's fixed 16 kHz/16-bit attrs [HIGH on the
PCM/rate; MED on it being ring-based specifically, by the elimination argument above] — and
it is reachable by a second process.** `agora`'s own existing use of this exact ring is
plain POSIX (`shm_open`+`mmap`+`sem_open`, zero proprietary `AX_NT_*` calls — confirmed
`docs/11-media.md §4`), the registry has spare slots, and the two existing readers already
use independent, uncoordinated synchronization strategies against the same segment — i.e.
the protocol is multi-participant-safe by construction, not a fragile two-party pairing.
**It is not internal-to-`media`-only.**

What this session could **not** pin down (explicitly, not guessed): the exact per-record
`chan` tag / header shape `audio_out_thread` filters for on that slot — no live capture of
an actual talk session exists (triggering one was out of scope and would have made
audible sound on a real device in someone's home). **Safe next step**: passively poll the
ring's registry + walk it (exactly the read-only method already used for `chan=1`) during
a real, user-initiated Petkit-app "pet call", which will naturally exercise this slot
without Kibble triggering anything itself.

**Practical caveat for the design**: `agora` is the *current* writer of `auido-out` during
an app-driven call. A second writer (Kibble) feeding the same named slot concurrently with
an active app call would interleave/corrupt both streams — the interface is feedable, but
a real implementation needs its own serialization policy (e.g. only write when no app call
is active, detectable via the transient `AX_AENC` uplink channel's live state).

## 7. RTP/RTSP framing for `kibbled`

### 7.1 Outgoing (mic) track — zero re-encode, same philosophy as the video path

Each ring `chan=1` record's payload is already one complete ADTS AAC-LC frame (§3). Putting
it on an RTP track per RFC 3640 ("MPEG4-GENERIC", AAC-hbr, 1 access unit/packet) is a
strip-and-prepend operation, no transcode:

1. Strip the 7-byte ADTS header (`protection_absent=1` on every observed frame, confirmed
   §3) — the raw AAC payload is `record_payload[7:]`.
2. Prepend the RFC 3640 §3.2.1 AU-header section: 2 bytes `AU-headers-length` (= `0x0010`,
   16 bits = one AU-header) + 2 bytes `AU-header` (13-bit `AU-size` = the raw payload
   length, 3-bit `AU-Index` = 0).
3. RTP payload = `[AU-headers-length][AU-header][raw AAC bytes]`, marker bit **set** on
   every packet (one full AU per packet), RTP timestamp incrementing by **1024** per packet
   at a **16000 Hz** RTP clock (RFC 3640: clock rate = sample rate for MPEG-4 audio).

SDP:
```
m=audio 0 RTP/AVP 97
a=rtpmap:97 MPEG4-GENERIC/16000/1
a=fmtp:97 streamtype=5; profile-level-id=1; mode=AAC-hbr; sizelength=13; indexlength=3; indexdeltalength=3; config=1408
```
(`config=1408` is the §4.1 `AudioSpecificConfig`, confirmed two independent ways.
`profile-level-id` is informative for audio — most real-world receivers, including
`go2rtc`/ffmpeg/VLC, do not enforce it — the load-bearing field is `config`.)

### 7.2 Talkback / backchannel (client → speaker)

Per `docs/20-two-way-audio.md`, the RTSP backchannel must accept G.711 µ-law from
Scrypted's ONVIF intercom (negotiated as a second `m=audio ... a=sendonly` section,
`Require: www.onvif.org/ver20/backchannel`). Path to the confirmed 16 kHz/16-bit PCM
`AX_AO_SendFrame` boundary (§6):

```
RTP PCMU/8000 packets
  → G.711 µ-law → 16-bit linear PCM  (256-entry ITU-T G.711 decode LUT, trivial cost)
  → upsample 8 kHz → 16 kHz          (2x; quality-insensitive for talkback-grade speech)
  → chunk (recommend mirroring AO's own ALSA period, PeriodSize=160 samples/10 ms — the
    live `/proc/ax_proc/ao` value on this exact device — though the exact chunking `agora`
    itself uses for this hand-off is unconfirmed, §6.1)
  → write as ring records into `auido-out` (56-byte header matching the rest of the ring's
    protocol: frame_type=0, media_class=4, width/bitdepth=16, height/samplerate=16000,
    `chan` = unconfirmed, §6.1)
```
SDP for the backchannel section:
```
m=audio 0 RTP/AVP 0
a=rtpmap:0 PCMU/8000
a=sendonly
```
(`PCMU` is payload type 0, a static/well-known type — no `fmtp` required. Optionally also
offer `PCMA`/8 for A-law and/or Opus for HomeKit-via-go2rtc compatibility, per
`docs/20-two-way-audio.md`; G.711 µ-law is the mandatory baseline both docs point at.)

## 8. Process hygiene

`ps`, read live at the very end of this session (compare `media`'s accumulated CPU time,
1h36, against 1h21 at the first check earlier this same session — only ever increased,
confirming continuous uptime and zero restarts throughout):
```
  196 root      0:00 [loop2]
  200 root      0:08 [loop3]
  202 root      0:08 ./watchdog
  203 root      0:13 ./ble
  204 root      1h36 ./media
  212 root      0:36 ./ctrl
  270 root      0:20 ./agora
  271 root      0:04 ./cloud
  272 root      0:02 ./logUpload
 8069 root      1:38 /opt/kibble/kibbled
```
No vendor process was signalled, killed, or restarted. `/tmp` on the device is byte-for-byte
the same pre-existing file set as at session start (`attire/`, `auth.log`, `config.lock`,
`cron/`, `cron.log`, `daemon.log`, `data/`, `fw_printenv.lock`, `io.agora.rtsa_sdk/`,
`resolv.conf`, `security.log`, `syslog.log`, `user.log`, `wpa_supplicant.conf`) — this
session never wrote a single new file to the device (every binary pull used
`nc -l -p PORT < <existing file>`, reading an existing vendor file out, never redirecting
into a new one). `/dev/shm/` is unchanged (`config_shm`, `media_buffer_frame_buf`, one
pre-existing orphaned reader semaphore) — no new shm segment or semaphore created. No
`pktool`, no `AX_VENC_RequestIDR`, no dispense, nothing played.

## 9. What this session could not determine (explicit)

- The exact `chan`/header tag `audio_out_thread` filters for on the `auido-out` ring slot
  (§6.1) — needs a passive capture during a real, externally-triggered talk session.
- The exact chunk size `agora` currently uses when writing to `auido-out` (recommended
  10 ms/160-sample chunks by analogy with AO's own ALSA period, not independently confirmed
  for this specific hand-off).
- Everything the prior sessions already flagged as open (frame-ring header offsets 20/24/28,
  the registry table's per-slot cursor semantics, exact `msg_id` wire values) remains open;
  none of it was in this session's scope and none of it blocks the two answers above.

---

# Speaker write path: encoder built and verified, ring delivery blocked on one unrecovered protocol detail

Author: AudioFinish (successor to AudioBuild, which built the codec modules and the live chan=2
capture below but hit a budget wall before writing anything to the ring). This section covers:
resolving the PCM-vs-AAC fork, the vendor's own duplicate-write defect (confirmed, not to be
replicated), the static-musl/fdk-aac helper, the full RTSP audio track + backchannel + `/speak`
`/clips` implementation (all built and independently verified), and — the one thing this session
could **not** finish — durable, audible delivery through the ring, plus the precise new evidence
narrowing exactly what's still unknown.

## 10. The PCM-vs-AAC fork, resolved: AAC, not raw PCM

§0/§7.2 above left the speaker hand-off's wire format as "raw PCM, confirmed at the
`AX_AO_SendFrame` hardware boundary, unconfirmed at the ring". AudioBuild's session (this project)
closed that gap with a live capture during a real, externally-triggered talk session: **548/548**
records on ring `chan=2` decoded as valid ADTS AAC-LC/16kHz/mono, every `frame_length` field
matching the ring record's own length exactly (`confirmed_via_research.audio_out_record_format`
in that session's yield). This is independently corroborated by §6's own disassembly: all three
`AX_AO_SendFrame` call sites in `media` sit inside one shared ~2.9KB block that also holds every
`AX_ADEC_SendStream`/`GetFrame` call in the binary — the simplest explanation consistent with both
facts is one generic "AAC-decode-then-play" routine servicing two byte sources (the canned-prompt
`.aac` files, and the `chan=2` ring hand-off), not a second, undiscovered bare-PCM call site. No
second `shm_open`-able segment or PCM entry point exists anywhere in `media`'s strings (§6.1) — the
ring **is** the only feedable path, and it wants AAC.

**Conclusion: path (a), not (b).** Kibble's speaker writer must encode PCM to AAC-LC/16kHz/mono
ADTS before writing the ring, exactly matching the mic's own format. This forces the fdk-aac
helper (§12) — there is no simpler raw-PCM shortcut available.

## 11. The vendor's own duplicate-write defect — confirmed, not replicated

AudioBuild's same capture caught Petkit's own talkback in the act of a real bug: in one of two
captured bursts, **540 of 548 (98.5%) of `agora`'s own `chan=2` records were exact byte-for-byte
duplicates of the immediately preceding record** (same internal PTS, identical payload), always in
pairs, never triples. Collapsing duplicates gives ≈16.35 unique frames/sec — genuine real-time
16kHz encoding, correctly paced — so the defect is a double-*write*, not a wrong sample rate or a
bad encode. A separate, earlier capture showed zero duplication: intermittent, not constant. This
is almost certainly what makes the stock app's own talkback sound "slow and garbled" on this unit.
It lives in the closed-source agora/cloud relay, not in anything Kibble built (the capture tool is
provably read-only — `PROT_READ`-only mmap, `O_RDONLY` open, zero semaphore/`AX_AO`/IPC calls
anywhere in its source). **Kibble's own writer (§13) paces one frame per real 64ms and never
duplicates by construction** — each `write_frame` call encodes and places exactly one access unit,
with no retry-on-suspected-failure path that could double-write (see §13's acceptance numbers).

## 12. Static musl vs. `libfdk-aac.so`: the helper subprocess

Confirmed empirically (not assumed): a fully-static musl binary (`kibbled`'s own build, `+crt-static`)
cannot `dlopen` anything — a minimal test binary built with this exact pipeline printed "Dynamic
loading not supported" at runtime. This rules out loading the device's own
`/soc/lib/libfdk-aac.so.2.0.1` (confirmed present, confirmed already used by `media` per
`/proc/204/maps`) directly from `kibbled`. Dynamically linking a *new* glibc binary against it was
also ruled out: the available cross-toolchain's glibc is 2.41, the device runs glibc 2.25 — a
16-release gap that would fail at runtime with an unresolved `GLIBC_2.3x` symbol version.

**Solution, built and verified this session**: `tools/aacenc/` cross-compiles fdk-aac
(`github.com/mstorsjo/fdk-aac`, tag `v2.0.3`) from its own open source as a small, statically-linked
ARMv7 helper binary (`arm-linux-gnueabihf-gcc`/`g++` `-static -Os -march=armv7-a+fp
-mfpu=neon-vfpv4`, the same flags `tools/kibble-msg.c` already uses and that are already proven to
run on this device) — `kibbled` spawns it as a subprocess, PCM in on stdin, ADTS AAC out on stdout.
The exact encoder parameter sequence matches §2.1's disassembly byte-for-byte (`aacEncOpen(&h,0,1)`,
`AOT=2`, `SAMPLERATE=16000`, `MODE_1`, `CHANNELORDER=1`, `BITRATEMODE=3` VBR, `TRANSMUX=TT_MP4_ADTS`,
`AFTERBURNER=1`); the helper hard-errors if `aacEncInfo` ever reports `frameLength != 1024` or a
`confBuf` other than `14 08`, so a future FDK version drifting from these pinned values fails loudly
at startup instead of silently producing a mismatched stream.

Verified (native x86_64 build of the identical source, ffprobe/ffmpeg): 2s @ 440Hz →
`codec_name=aac profile=LC sample_rate=16000 channels=1`, every frame `nb_samples=1024`, clean
`ffmpeg -f null -` decode (0 errors). The shipped ARM binary (`tools/aacenc/build-arm/aacenc`,
835,784 bytes) is `file`-confirmed statically linked with no `PT_INTERP` segment, and was
separately confirmed to run correctly **on the device itself** this session
(`frameLength=1024 confBuf=14 08`, exit 0, on real ARM hardware, not just cross-compiled).

## 13. Ring writer: byte-level correctness confirmed; durable delivery still blocked

`agent/src/audioout.rs` implements the writer design AudioBuild's yield laid out: locate the
current tail via `ring::TailCursor` (a shared snapshot the existing read-only poller keeps fresh)
plus a short, fresh catch-up walk immediately before every write (re-reading live bytes, not
trusting the snapshot alone — see that module's doc comment for the full reasoning), then
`pwrite()` a fully-assembled 56-byte-header-plus-payload record at that offset. Header fields
(`frame_type=0`, `media_class=4`, `chan=2`, offsets 20/28) replicate AudioBuild's live-observed
values byte-for-byte rather than guessing.

**What is confirmed correct, with live evidence from this device** (a temporary, since-removed
diagnostic build that read back every write immediately and again 80ms later):

- Every write lands at the intended offset with the intended header (`immediate_readback_matches`
  true on every one of 18/18 frames in the sample run below).
- **17 of 18 frames still read back byte-identical 80ms later** (`chan=2`, correct seq, correct
  payload) — only the very first (a near-silent, 13-byte encoder-lookahead priming frame) was
  overwritten in that window, by what the delayed readback shows as a genuine, tiny `chan=4`
  (video main) P-frame landing at the exact same offset with the exact same seq value `kibbled`
  also computed — i.e. a real, rare torn-tail collision of the kind the writer's design doc
  anticipates as tolerable, not a systematic failure.
- Sample run (`aacenc`-encoded, 1s @ 440Hz, quiet): 18 ADTS frames written
  (offsets `2089246..2092296+220`, seqs `23750..23767` contiguous, payload lengths `13..313` bytes
  matching real VBR AAC-LC output), zero write errors, zero duplicate seq/offset pairs.

**What does not yet work: the vendor's `audio_out_thread` never consumes any of it.**
`/proc/ax_proc/ao`'s `SndFrm` counter was sampled every ~1s across multiple `/speak` calls
(1s and 5s clips, quiet, real hardware) and **never moved from its pre-test baseline (1331) even
once**, despite dozens of byte-correct `chan=2` records being present in the ring at write time.
`dumpchanring` (rt5/ringtool2's channel-filtered walker) run promptly after a write also found
**zero** matching `chan=2` records once enough real time had passed for ordinary ring turnover
(video alone writes ~50-70 records/sec) to cycle past that region — consistent with "nothing ever
consumed it, so it just aged out like any other stale data," not "the writer is broken."

### 13.1 New evidence narrowing the real cause: the `auido-out` registry slot is never touched

`docs/11-media.md §4` already identified registry slot 7 (offset `0x134`, 44 bytes, named
`"auido-out"`) but explicitly could not pin down its trailing fields' semantics. This session
re-read that exact slot live, twice, 2 seconds apart, **immediately after** a `/speak` call that
wrote 18 correct `chan=2` records elsewhere in the ring:

```
000134 61 75 69 64 6f 2d 6f 75 74 00 00 00 00 00 00 00   "auido-out\0" + pad
000144 06 00 02 00 7d a9 0b 00 e1 f7 01 00 00 04 00 00
000154 b0 a2 c4 ad 01 00 00 00 00 00 00 00
```
**Byte-for-byte identical before and after** — this slot did not change at all in response to
kibbled's own writes landing (correctly, per §13's readback evidence) elsewhere in the ring. The
first trailing field (`06 00 02 00`) matches `docs/11-media.md`'s own older snapshot of this same
slot exactly (a stable index/generation pair); the following fields differ from that older
snapshot's values (this device has rebooted/re-run since), consistent with them being **frozen at
whatever they held after the last real talk session ended**, not actively driven by ring content.

Separately, this session precisely pinned down slot 0's own offset 24 (`0x18`) by correlating 64
live snapshots against this project's own read-only poller's observed `(seq, next_pos)`: **it is
the ring's global sequence counter**, matching the poller's own tracked value essentially exactly
on every sample (off by at most 1, the normal read-timing race) — upgrading `docs/11-media.md`'s
"[MED] constant, meaning unclear" to **[HIGH]** for this one field. Offset 28 (`0x1C`) tracks a
second, related count at a similar rate but a roughly-constant ~2,870-2,890 offset below it —
plausibly a different writer's or a different channel-group's own tally; not pinned down.

**Working hypothesis, not yet acted on**: `audio_out_thread`'s own "polling" (§2's static evidence
says it is data-driven, not command-driven — no `speak_start`-style trigger needed) most plausibly
means it polls *this specific registry slot* for a change, the same way this project's own poller
watches `TailCursor`, rather than re-scanning the raw byte stream for spontaneous `chan=2` headers.
`agora` -- the one producer whose writes are independently confirmed to reach playback (`SndFrm`
visibly advances during a real talk session, per the original assignment's proven facts) -- almost
certainly updates this slot as part of announcing new data; `kibbled` currently does not touch it
at all, which would fully explain byte-correct, briefly-surviving writes that are simply never
noticed.

**This session deliberately did not attempt to write to that slot.** Its trailing fields are not
understood with enough confidence to update safely — this exact 44-byte region is read by `media`
(confirmed) and plausibly other processes, and a wrong guess risks corrupting shared coordination
state in a way that could visibly affect the live video/mic path the household depends on, not just
silently fail like the current gap does. Per this project's own standing instruction to ask before
anything that could disturb the vendor audio path, and given the added risk here is to a *write*
into not-fully-understood shared state (materially different from a read-only probe), that
judgment call was made conservatively: stop, document precisely, hand off.

**Concrete next step for a follow-up session**: correlate registry-slot-7's own field deltas
against a real, externally-triggered talk session the way `docs/11-media.md §4` originally
suggested for slot 0 (a live before/during/after capture while `agora` is actively writing) to
determine which trailing field(s) change and by how much per record/byte, then replicate exactly
that update as part of `write_frame`. This is a bounded, well-scoped continuation, not a restart —
the physical record format, encoder, pacing, and append-target logic are already done and correct.

## 14. RTSP audio track, backchannel negotiation, `/speak` + `/clips` — built and verified

All of the following were implemented this session and are independent of §13's open item (they
either only *read* the ring, which already worked, or only depend on the *encoder*, which is
independently verified in §12):

- **Outgoing mic AAC track** (`rtsp.rs`): a third `m=audio` SDP block (`MPEG4-GENERIC/16000/1`,
  `config=1408`, `trackID=1`, interleaved `2-3`), always offered on both `/main` and `/sub`. RFC
  3640 AU-header framing per access unit (`rfc3640.rs`, already existed), ADTS header stripped
  before the RTP payload (`adts.rs`). **Verified live**: `ffprobe -rtsp_transport tcp` against the
  running device reports `codec_name=aac profile=LC sample_rate=16000 channels=1` for this track,
  and `ffmpeg -rtsp_transport tcp ... -f null -` decodes 3 real seconds of it with exit 0 and zero
  stderr output -- a genuine, zero-re-encode, end-to-end AAC RTP stream from the live mic.
- **Backchannel negotiation** (`rtsp.rs`, ONVIF-style): DESCRIBE conditionally appends a fourth
  block (`PCMU/8000`, `a=sendonly`, `trackID=2`) only when the client sent
  `Require: www.onvif.org/ver20/backchannel`. SETUP on `trackID=2` accepts TCP-interleaved
  (`4-5`) and rejects a UDP ask with `461 Unsupported Transport` so Scrypted's own documented
  UDP-then-TCP fallback fires. **Verified live** against the running device: all three
  `SETUP`s (trackID 0/1/2) return the correct interleaved ranges; a UDP-transport SETUP on
  trackID=2 returns exactly `461`.
- **Backchannel audio path** (`backchannel.rs` + `audioout::LiveSession`): strips the RTP header
  (CSRC-aware), decodes G.711 µ-law/A-law (`g711.rs`, pre-existing), upsamples 8kHz→16kHz, and
  feeds the result into a live encoder session that paces output onto the ring exactly like
  `/speak` -- inherits §13's open item (encoded correctly, not yet audibly delivered).
- **`POST /speak`** (raw signed-16-bit-LE/mono/16kHz PCM body, no container) and **`/clips/*`**
  (`GET /clips` list, `PUT`/`GET`/`DELETE /clips/<name>`, `POST /clips/<name>/play`): implemented
  in `main.rs`/`clips.rs`/`audioout.rs`. Clips are stored pre-encoded (concatenated ADTS, not raw
  PCM) so playback never re-spends the encode cost. Both paths normalize to `TARGET_RMS=1532`
  (measured off-device from `/audio/en/en_feed_start.aac`, one of the vendor's own canned prompts,
  via `ffmpeg`) before encoding, matching a Kibble announcement's loudness to the vendor's own
  prompts. **Verified live**: `POST /speak` accepts a real PCM body, returns `200` with a sample
  count, and the encode+write pipeline runs to completion (§13's byte-level evidence *is* this
  pipeline running for real, not a simulation).

## 15. Two-producer safety, implemented

Answering the assignment's question directly: yes, a live app talkback and a kibbled-initiated
write are a genuine two-producer hazard on one ring slot. Two independent mechanisms, both in
`audioout.rs`:

1. **kibbled-internal arbitration** (`SpeakerOwner`): a single `AtomicBool`-backed exclusive lock
   every writer (`/speak`, `/clips/.../play`, the RTSP backchannel) must hold for its whole
   session; a second caller gets a `409 Conflict` (`http.rs`) immediately, no partial write.
2. **Vendor-call detection** (`call_active`): reads `/proc/ax_proc/aenc` and requires the *exact*
   3-line idle banner (confirmed live: `wc -c` = 120 bytes, `od -c` shows a genuine blank final
   line -- byte-verified, not assumed); anything else, including a read error, is treated as "a
   call might be active" and refused. Checked once before a session starts (folded into
   `SpeakerOwner::try_acquire`) and again before every single frame during playback (`pace_one`),
   so a call that starts mid-clip aborts the clip (`PlaybackStats::aborted_call_active`) instead of
   interleaving with it. Fails closed by design: a false positive only delays an announcement, a
   false negative would corrupt two streams at once.

## 16. Acceptance numbers, honestly reported

Per-run numbers from the sample capture in §13 (1s @ 440Hz quiet clip, real device, real telnet
read-back, not simulated):

| metric | result |
|---|---|
| Frames written per clip | 18 (16000 samples / 1024-per-frame ≈ 15.6 nominal + encoder lookahead, matching `tools/aacenc/`'s own 2s-clip validation ratio) |
| Frame pacing | exactly one `write_frame` call per 64ms scheduled slot (`pace_one`'s deadline arithmetic; not independently re-measured with a wall-clock trace this session, but the mechanism is the same one `docs/23-audio-codec.md`'s original design specified and unit-tests already cover the arithmetic) |
| Duplicate frames | **zero**, by construction and by the byte-level readback (18 distinct, monotonically increasing seq values, 18 distinct offsets) |
| ADTS length agreement | **100%** -- every ring record's length field is set directly from the encoded payload's own length (`build_record`), so they cannot disagree |
| Immediate write survival | 18/18 |
| Survival at +80ms | 17/18 (see §13 for the one collision) |
| `SndFrm` delta over the clip's duration | **0 -- did not advance.** This specific acceptance bar is not met; root cause is §13.1's unrecovered registry-slot protocol, not the writer's byte-level correctness. |

**Bottom line**: the codec, encoder, ring-record format, pacing, and RTSP/HTTP surface are done and
independently verified end-to-end (four different verification methods: native ffprobe validation
of the encoder, on-device execution of the encoder, live RTSP ffprobe/ffmpeg decode of the mic
track, and live telnet read-back of writer output). The one gap -- real audible playback through
the vendor's speaker -- is narrowed to a single, precisely-located, unrecovered detail: how a
writer announces new data via registry slot 7, which this session chose not to guess at live.

---

# `audio_out_thread` fully disassembled: root cause of "`SndFrm` never moves" identified

Author: AudioStart (successor to AudioAnnounce). Method: static disassembly only, same
capstone+pyelftools pipeline as prior sessions, against `/app/bin/media` pulled by AudioCodec's
session (`/tmp/kibble_audio/media`, 348,116 B) -- re-verified byte-identical to the live device's
copy this session (`md5sum` over telnet, one short read-only command, matches exactly:
`f9e74f321a2bb7693f495598d816386a`). Zero writes to the device this session until the very end
(§17.8, gated on Main's approval). Every claim below is a directly-disassembled, address-cited
fact unless tagged `[INFERENCE]`.

**Correction to the prior session's PLT-resolution method**: the ARM PLT in this binary does
*not* use a uniform 12-byte stub stride (the assumption both this session initially made and the
prior AudioAnnounce/AudioCodec sessions implicitly relied on for early-table symbols only) --
stub sizes vary starting around index 21. Re-derived correctly this session by disassembling the
*entire* `.plt` section and symbolically simulating each stub's `ADD/ADD/LDR` register chain
per-stub (not by fixed offset math), then pairing the resulting 257 stub addresses 1:1 with
`.rel.plt`'s 257 relocations **in file order** (guaranteed by the ARM ABI, and independently
confirmed three ways: `aacEncOpen`'s call site still resolves to the documented `0x15740`;
`mq_receive` now has **exactly one** call site, at `0x3319c`, inside the documented
`dispatch_mqueue_read` range `0x33171`-`0x33389`; `AX_AO_SendFrame` now has **exactly three** call
sites, at `0x305f0`/`0x30e00`/`0x30f84`, matching §6's independently-documented count and range
precisely) **[HIGH]**. The mis-resolution this fixed had briefly (mid-session, caught before being
relied on) mislabeled `AX_SYS_LogPrint` as `AX_ISP_Create` for one stub -- flagging in case any
earlier session's notes used raw PLT addresses instead of symbol names from a similar table.

## 17.1 `0x30764` is `audio_out_thread` -- confirmed, not inferred

Independently re-derived media's full 27-entry `msg_id -> handler` table from scratch (same
method `docs/24-onboard-ai.md` proved: walk the registrar's `bl register()` call sites, backward-
resolve each one's `r0`=msg_id immediate and `r1`=handler address via its GOT-indirect literal
load) rather than trusting the prior session's remembered addresses. Result matches on every
count (27/27) but lands on **different, more precise handler addresses** than the prior summary
(e.g. `0xa`'s real handler is `0x31750`, not directly `0x30765` -- the prior summary's own
"10-byte trampoline" description was correct, just imprecise on the intermediate address).

- `msg_id 0xa` (`speak_start`) registers a 10-byte trampoline at `0x31b20`
  (`push {r3,lr}; bl 0x31750; movs r0,#0; pop {r3,pc}`) **[HIGH]**.
- `0x31750` (the real handler): loads a guard flag from a fixed global, **byte-resolved this
  session as `0x767f0`**; if already `1`, returns `-1` immediately via one of two paths (one with
  an `AX_SYS_LogPrint` call gated on a *different* global, `0x76b5c`) without touching the thread
  or the flag -- **idempotent, confirmed by full disassembly of both return paths, not assumed**
  **[HIGH]**. If not already started: sets the flag to `1`, then calls
  `pthread_create(thread=&0x76ca8, attr=NULL, start=0x30765, arg=NULL)` at `0x317de` -- every one
  of these four argument values independently re-derived from raw instruction bytes (GOT-indirect
  literal resolution, not pattern-matching), and every one matches the prior session's memory
  exactly **[HIGH]**.
- `0x30765`/`0x30764` (thumb-bit-adjusted): confirmed this is the *only* `pthread_create` start
  routine reachable from `speak_start`, a single ~2KB function (`push.w {r4-fp,lr}` at `0x30764`
  through its return paths in the `0x30940`-`0x31254` range) that:
  - Calls `pthread_self` + `pthread_detach` immediately (self-detaching, matches a fire-and-forget
    worker thread) **[HIGH]**.
  - Opens the ring's registry slot **by name** -- literally passes the C string `"auido-out"`
    (byte-read from `.rodata` at `0x4ddd3`, exact match to the vendor's own typo already
    documented in §6.1/§13.1) to a generic `open_or_create_named_slot(name, seed_from_global)`
    helper at `0x32894` **[HIGH]**. This is new and matters: **the slot is found by string match
    at runtime, not addressed by a hardcoded index anywhere in this thread's code.** "Slot 7" /
    absolute offset `0x134` is this *specific device's current* array position for that name
    (array position 7 because `7 * 44 = 0x134` exactly, confirmed against the registry geometry
    below), stable only because `media`'s own startup registers things in the same fixed order
    every boot -- not a protocol constant.
  - Calls the same helper for a second, differently-parameterized open, then polls in a loop
    (§17.3) that ultimately calls `AX_ADEC_SendStream` (`0x309b4`, `0x30bb0`) ->
    `AX_ADEC_GetFrame` (`0x30d98`, `0x30f26`) -> **`AX_AO_SendFrame`** (`0x30e00`, `0x30f84`),
    the decoder's own output buffer passed straight through to the speaker call unmodified in
    both cases (`r2`/`sl` identical across the `GetFrame`/`SendFrame` pair) -- i.e. this thread
    really does decode AAC and play PCM, using the exact same hardware primitives §6 already
    call-graph-confirmed for the canned-prompt path, but as its *own*, separate function (not
    shared code with `dispatch_handler_play_aac_file`, which sits just before it at `0x30400`-
    `0x30764` and has its own, separate `SendStream`/`GetFrame`/`SendFrame` call sites) **[HIGH]**.

## 17.2 Registry geometry, fully resolved

The mmap+init code for `media_buffer_frame_buf` (`0x321f6`/`0x32318`, retry-on-failure pair;
success path continues at `0x32274`) disassembles cleanly and resolves every open question
`docs/11-media.md §4` and `docs/19-frame-ring.md §1` flagged **[HIGH, every value below is a
directly-disassembled immediate or literal, not inferred]**:

- The segment is **exactly `0x800000` (8 MiB)**, confirmed independently by both the `ftruncate`
  size at segment-creation and a hard `cmp.w r4,#0x800000` bounds check in the ring-byte-read
  helper (§17.3).
- **Byte 0 of the segment is a real `pthread_mutex_t`** (24 bytes, offset `0x00`-`0x17`),
  initialized at creation with `pthread_mutexattr_setpshared(&attr, PTHREAD_PROCESS_SHARED)`
  before `pthread_mutex_init` -- a genuine **cross-process** shared mutex, not a private
  in-process one, not a semaphore. This is what `audio_out_thread` takes (`pthread_mutex_trylock`
  first, falling back to a 1-second `pthread_mutex_timedlock`, both in a small helper at
  `0x31d58`) before reading *any* registry or sequence state.
- Immediately following the mutex, still within this same 44-byte "slot 0": `+0x18` = the ring's
  global sequence counter (upgrades `docs/11-media.md`'s prior **[HIGH]** live-correlation finding
  to a from-source confirmation: zeroed at creation, read/compared throughout `audio_out_thread`).
  `+0x1c` = a second counter, also zeroed at creation, also read as a gate value (§17.3) --
  `docs/23-audio-codec.md §13.1`'s "second, related count" is confirmed real, just not yet named.
  `+0x24`/`+0x28` = a *third* pair, also zeroed at creation; `+0x28`'s role is pinned down below.
- **Slots 1-19 follow at a uniform 44-byte stride from the same base** (confirmed two independent
  ways: the `open_or_create_named_slot` linear scan loop at `0x328c0`-`0x3290e`, bounds
  `ring_base+0x2c` through `ring_base+0x39c`; and a *separate* function's all-slots scan loop at
  `0x32700`-`0x32728`, `mla r2,r6(=0x2c),r3,r0` against the identical base, bound `0x14`=20). So
  "slot 7" = `ring_base + 7*44 = ring_base + 0x134` **exactly**, matching `docs/11-media.md`'s
  live-observed absolute offset -- self-consistently, from an entirely different derivation.

### 17.2.1 Every trailing field of the live-captured "auido-out" slot now matches source, byte-for-byte

`docs/23-audio-codec.md §13.1` captured slot 7 live as (slot-relative offsets):
`+0x00` name, `+0x10` = `06 00 02 00`, `+0x14` = `0x000ba97d`, `+0x18` = `0x0001f7e1`,
`+0x1c` = `0x00000400`, `+0x20` = `0xadc4a2b0`, `+0x24` = `0x00000001`, `+0x28` = `0x00000000`.
`open_or_create_named_slot`'s claim-a-slot path (`0x328c6`-`0x32908`) writes, in order:
`strncpy(slot, name, 15)`; `slot->0x24 = 1` (refcount/in-use); `slot->0x1c = 0x400` (**a fixed
1024-byte constant**, not a counter); `slot->0x20 = malloc(0x400)` (a scratch decode buffer,
explaining the plausible-heap-pointer-looking value); `slot->0x12 = 0`; `slot->0x14 = 0` and
`slot->0x18 = 0`, **then conditionally overwritten** (the second, `seed_from_global=1` open
kibbled's target thread makes) with `slot->0x14 = ring_base->0x18` (current global seq) and
`slot->0x18 = ring_base->0x28` (the third counter). Four of these six fields are exact,
non-coincidental byte matches to the live capture (`0x1c`=1024, `0x24`=1, `0x28`=0, and `0x10`
structurally = idx/gen written by the same claim path); the other two (`0x14`, `0x20`) are
point-in-time values that necessarily differ between the source-derivation and the live capture
(a monotonically-changing seq snapshot and a heap address respectively) but match in *kind*
exactly as predicted. This is about as strong a static/live cross-check as this project's method
can produce, and it retroactively explains why AudioAnnounce's own announce-write test saw
`slot7+0x14` track their writes' seq values 292/292 -- **that field is simply whatever the last
writer (real or ours) put there; nothing downstream ever reads it back** (§17.3).

## 17.3 Root cause: consumption is gated on the *global* sequence counter, which `kibbled` never touches

This is the section the assignment asked for directly. `audio_out_thread`'s poll body (`0x32d1c`,
called in a loop from the outer function) does, in order, every step directly disassembled:

1. Take the shared mutex (`0x31d58`, above).
2. Compare `ring_base->0x1c` against the slot's own `+0x14` (a secondary, less-strict gate; only
   emits a "falling behind" log if failed, does not block).
3. **The real gate**: `ldr r1,[slot,#0x14]; ldr r0,[ring_base,#0x18]; cmp r0,r1; bge consume` --
   i.e. *consume iff `ring_base->0x18 (global seq) >= slot->0x14 (this consumer's bookmark)`*.
4. On consume: read one 56-byte record header via a byte-exact-confirmed `read_ring_bytes(dest,
   offset, len)` helper at `0x31cdc` (`src = ring_base + 0x3e8 + offset`, wraps modulo `0x800000`,
   tail-calls `memcpy` -- **`offset` is a real, byte-proven ring offset here**, resolving the
   assignment's open question about the `0x18` field's meaning: it *is* used as a ring byte
   offset, but that use is **`slot->0x18` acting as `audio_out_thread`'s own self-advancing read
   cursor** (`new_offset = (old_offset + 56 + header.payload_len) mod 0x800000`, computed at
   `0x32eb2`-`0x32ebc` from the just-read record's own `+4` length field -- byte-exact match to
   `audioout.rs`'s own `build_record`, which also places payload length at header offset 4), **not
   a value a writer announces**. It starts from whatever `ring_base->0x28` held at thread-open
   time and then walks forward under the consumer's own control; nothing about it invites a
   writer to "point" the thread at a specific record.
5. After each record, `slot->0x14 += 1` and the loop re-checks step 3 against the (possibly
   advanced) global counter, repeating until caught up.

**`ring_base->0x18` is a single, mutex-protected, all-channel counter** -- confirmed by
`docs/11-media.md`'s prior live correlation against kibbled's *own* read-only poller's tracked
`(seq, next_pos)`, which necessarily counts every channel (video + mic + audio-out) to match. A
full-binary scan for every reference to the `ring_base` global this session (19 total references)
found all of them clustered inside this one audio-out-consumer subsystem (`0x31c00`-`0x330fe`) --
the actual multi-channel record-append code that increments `0x18` lives elsewhere (video/mic
encoder callbacks), out of this session's scope, and is the same "no known, safely
reverse-engineerable atomic claim primitive for this ring's write side" gap `audioout.rs`'s own
module doc already flags as unrecovered from an earlier session.

**This fully explains AudioAnnounce's negative result.** `agent/src/audioout.rs`'s `write_frame`
places correct record bytes and (per the prior session) correct-looking `slot->0x14`/`0x18`
values, but never touches `ring_base->0x18`. Since kibbled's writer is the only thing that would
need to advance that counter for the thread to notice new data, and it never does, step 3 above
never newly turns true on kibbled's account -- the byte-correct record simply ages out of the
ring untouched, exactly as observed (`SndFrm` delta 0, zero `chan=2` records found by
`dumpchanring` after ordinary turnover).

### 17.3.1 A lower-risk path forward, identified but **not attempted this session**

Touching `ring_base->0x18` directly would mean taking a `PTHREAD_PROCESS_SHARED` mutex this
project has never touched and mutating a counter the live video/mic path also depends on --
exactly the class of risk this project's standing instructions single out. A narrower option
**[INFERENCE, untested]**: since `ring_base->0x18` already increments continuously from ordinary
video/mic traffic (~50-70 records/sec per §8), kibbled's writer could instead set *only*
`slot->0x14` backward (to a value at or below the current `ring_base->0x18`, which requires no
mutex -- a plain racy write, the same risk class as the ring byte writes already proven safe) and
`slot->0x18` forward to the exact byte offset of its own freshly-written record. The very next
ordinary video/mic frame would then satisfy step 3 "for free" and the thread would walk forward
from kibbled's chosen offset. This depends on one thing this session could not confirm: whether
the per-record consume path filters by channel before calling `AX_ADEC_SendStream` (a candidate
bitmask check, record header `+0x22` against a *different*, seemingly-unrelated function's
per-slot `+0x3e` mask, was seen once at `0x326e2`-`0x32718` but not confirmed to be on
`audio_out_thread`'s own call path) -- if it does not filter, pointing the thread at a
kibbled-written record this way would be safe (chan-mismatched frames already can't reach here
because the walk starts exactly at the offset kibbled supplies); if it does not exist at all, an
unfiltered walk driven by ordinary video traffic could feed non-audio bytes into
`AX_ADEC_SendStream` before ever reaching kibbled's record, which the decoder would most likely
reject harmlessly (wrong format) but was not verified. **Flagged as the concrete next step, not
guessed at further or implemented.**

## 17.4 `speak_stop` (`0xb`): clears the guard flag; does **not** stop the thread

`0xb`'s handler (`0x31b2c`) is a small trampoline (`time()`, stash it, `bl 0x318ec`) exactly as
the prior session described. `0x318ec` disassembles cleanly: if the guard flag (`0x767f0`,
resolved this session via the identical GOT-indirect literal chain as §17.1 and confirmed
**byte-identical** to the address `speak_start` sets) is already `0`, it logs (gated on the same
`0x76b5c` state global) and returns `-1` -- calling stop when nothing is running is harmless. If
the flag is `1`: **it is set to `0`** (`0x31964`) and the function returns `0`. That's the entire
effect on shared state.

**`audio_out_thread`'s own ~2KB body never reads or writes `0x767f0`** -- confirmed by two
independent full scans this session (every `ldr`-literal resolving to that address, and every
literal-computed address the thread's body touches at all, 154 total references, none of them
this flag). **The thread does not learn that `speak_stop` was called and does not exit because of
it.** It exits **only** via its own idle timeout (§17.5). This means:

- `speak_stop` is not required for the thread to stop -- it will stop on its own regardless.
- `speak_stop` **is** required to keep the guard flag truthful. If kibbled ever calls
  `speak_start` and the thread later self-times-out (which, per §17.3, is the *expected* outcome
  of every kibbled-initiated call under the current writer) without a matching `speak_stop`, the
  flag is left stuck at `1` forever (nothing else clears it) -- every subsequent `speak_start`,
  from kibbled **or from a real Petkit app talkback if it shares this same handler** (architecture
  consistent with §6.1's "`agora` is the current writer of `auido-out` during an app-driven call",
  not independently re-traced into `agora`'s own binary this session -- **[MED]**, not proven),
  would then return `-1` with no thread actually running. This makes an RAII/`Drop`-guaranteed
  `speak_stop` call (mirroring the existing `OwnerGuard` pattern) a hard requirement for any
  `kibbled`-side caller of `speak_start`, on every exit path including errors/panics -- not an
  optional cleanup nicety.

## 17.5 The idle-exit timeout: milliseconds, not microseconds; two gates, not one

A dedicated helper (`0x33054`) reads `CLOCK_MONOTONIC` and returns a 64-bit **millisecond** count
(`tv_sec*1000 + tv_nsec/1000000`, confirmed via the exact magic-multiply-constant compiler idiom
for that arithmetic, not inferred from behavior) -- correcting the prior session's "microsecond
delta" note. The thread samples this once at entry and again on each poll iteration, computes
`elapsed = now - start`, and checks it against **two** thresholds, both directly disassembled:
`elapsed >= 1000` (ms) gates a warmup/housekeeping branch (self-notifies via `dispatch_send_msg`,
msg `0xd`, `dst=self`); `elapsed >= 4999` (ms, `movw r2,#0x1387`, a 64-bit unsigned compare)
branches to the thread's cleanup/exit path -- matching `media`'s own
`"not recv audio-out data over 5s, exist audio_out_thread"` string almost exactly (4999 ms vs. a
nominal 5000 ms, the 1 ms gap consistent with a `>=` vs `>` fencepost, not a different unit).

## 17.6 Two-owner safety, evidenced

- **Double-spawn**: impossible by construction -- `speak_start` is idempotent (§17.1), verified by
  disassembling both its "already started" return paths, not assumed from the guard-flag's mere
  existence.
- **Kibbled calling `speak_start` while a real app talkback is already active**: harmless --
  returns `-1` immediately, same idempotent path, does not disturb the live thread or its data.
- **Kibbled calling `speak_start` first, then a real app talkback starting**: **[MED, architecture
  inference]** if the app's own talkback path also arrives via this same `msg_id 0xa`, it too
  would see the flag already `1` and return `-1` **without spawning its own thread** -- meaning
  the *existing* thread (kibbled's) would need to still be alive and would need to be the one
  servicing the real call, which it cannot do on its own (it only reads whatever is in the ring at
  the offset it's walking). This is not a corruption risk (nothing double-writes), but could
  plausibly manifest as **a real user's pet-call talkback silently not working** for up to ~5s
  after any kibbled-initiated `speak_start`/`speak_stop` cycle finishes, until the guard flag is
  next in a state that allows the real path through. Combined with §17.4's "stuck flag" risk, this
  is the strongest argument in this whole trace for keeping any kibbled-initiated `speak_start`
  window as short as strictly necessary and always paired with `speak_stop`.
- **Ring-byte collision during concurrent writing** (the risk `call_active()`/`SpeakerOwner`
  already defend against): unchanged by anything in this session's trace -- still the right,
  already-implemented mitigation, orthogonal to the thread-lifecycle question above.

## 17.7 What sending `speak_start`/`speak_stop` would look like

Exact wire shape, per `bus.rs`'s already-proven envelope (`u16 msg_id | u16 src | payload[]`,
`dst` selects the queue, never travels on the wire):
`Sender::open(Peer::Media, src)?.send(0xa, &[])` / `.send(0xb, &[])` -- **empty payload**; neither
handler reads any bytes past the 4-byte envelope (confirmed: `0xa`'s trampoline never touches the
handler-convention payload register before calling `0x31750`, and `0x31750` itself never
dereferences it). `dst = /msg_dispatch_2` (media's queue, `Peer::Media`).

Risk analysis (see §17.8 for the live approval request sent to Main): spawning the thread this way
is, per every path disassembled above, idempotent, self-cleaning-up on its own within ~5s even if
kibbled never sends `speak_stop`, and touches no vendor state beyond the one guard flag and (once
running) the same mutex-protected registry reads every other consumer already performs
concurrently. **It will not, by itself, produce audible output against the current writer** --
that gap is §17.3's, not this one's -- so the live test in §17.8/§17.9 is scoped to proving the
thread-lifecycle mechanics and gathering real `SndFrm` numbers, not to claiming sound.

## 17.8 Correction, prompted by Main's review: §17.3/§17.7's "will not produce sound" claim was overreaching

Main caught a real gap before any message was sent: video/mic writers bump `ring_base->0x18`
50-70 times/sec, so if `audio_out_thread` were alive during AudioAnnounce's 26/26 write test, the
gate (`ring_base->0x18 >= slot->0x14`) should have passed within ~20ms of every single write --
`SndFrm` staying at 0 across 26 writes is not what §17.3's gate model alone predicts. Re-checked
against every prior session's own yield: **`speak_start` has never been sent, by anyone, in any
prior test** (AudioAnnounce's own yield: "no bus message sent to media... none was sent";
AudioFinish/AudioBuild never mention it either). **`audio_out_thread` was simply never running
during any of the writer tests.** That alone fully explains every negative result to date, with no
need to invoke the gate at all -- §17.3's gate mechanics are real (directly disassembled, not
retracted) but were not the operative cause of anything observed so far, and §17.3/§17.7's framing
overstated how confidently "will not produce sound" could be predicted for a test that, for the
first time, actually spawns the thread.

Re-examined the channel-selection step (`0x32ea2`-`0x32eaa`) that was left an open question in
§17.3.1, since it bears directly on whether the currently-implemented writer has any chance of
being picked up. Resolved **[HIGH]**: `audio_out_thread` makes a *second* setup call right after
opening its slot (`0x3078e bl 0x32930`, second argument `2`, i.e. the same "chan=2" convention
`docs/23-audio-codec.md` uses throughout) which does exactly one thing --
`slot->0x12 = 2` (`0x3294a`). The per-record filter is a bitwise bitmask test:
`tst record_header[0x22:0x24], slot->0x12` -- nonzero means "process", zero means "skip, just
advance the bookmark past it". `record_header+0x22` is the exact halfword `audioout.rs`'s
`build_record` writes `rec[34] = ring::CHAN_AUDIO_OUT` into (low byte only; the high byte, offset
35, is left `0` by the zero-initialized buffer) -- if `CHAN_AUDIO_OUT == 2` (matching every other
chan=2 reference in this project), kibbled's existing, unmodified record format already satisfies
this bitmask **with zero code changes needed on the filter side**.

Traced the "process" branch (`0x32ed0`-`0x32fa4`) to its end: resizes the slot's scratch buffer
(`+0x1c`/`+0x20`) if needed, does a bookmark/gap check (`slot->0x14 + 1 == record.seq`, logging if
not), then **copies the payload (header skipped, `+0x38`) into the scratch buffer via the same
`read_ring_bytes` helper, writes the buffer pointer into the caller's output parameter, advances
`slot->0x18` past the whole record, and returns success** -- the outer function then builds the
`AX_ADEC_SendStream` struct from exactly this pointer (§17.1). This is a complete, coherent,
plausible-to-work path from "kibbled writes a `chan=2` record" through to `AX_AO_SendFrame`,
**given the thread is alive and its walk actually reaches that record**.

**What remains genuinely unresolved, and cannot be resolved further by static analysis**: both
`slot->0x14` and `slot->0x18` are seeded once, at thread-open, from `ring_base->0x18`/`0x28`
respectively (§17.1). This session found no code anywhere that writes `ring_base->0x28` after its
one-time zero-init at segment creation (a 19-reference full-binary scan of every `ring_base` use
turned up nothing) -- if nothing else updates it either, every thread-open seeds the byte cursor
to `0` (the very start of the ring's data region), which, on a ring that has been wrapping
continuously for a long uptime, is stale/arbitrary relative to current content, not "the oldest
unread record" in any meaningful sense. Whether the walk from there behaves sanely (skips through
real, well-formed records until it catches up) or reads a few torn/garbled records first (which
the ring's readers are documented elsewhere to tolerate and resync from, but not verified for
*this specific* consumer's gap-handling path) is exactly the kind of question a live, health-gated
test answers and static analysis cannot. **Going into the live test genuinely uncertain, not
expecting zero, per Main's instruction.**

**Also noted, not yet reconciled**: `read_ring_bytes`'s constant (`0x3e8` = 1000, disassembly-
confirmed byte-exact) is 24 bytes below `ring.rs`'s own `DATA_START` (1024, established
separately by live byte-density analysis of the real file). Not load-bearing for the approved
test (which does not write `slot->0x14`/`0x18`), but matters for any future attempt at §17.3.1:
`slot->0x18`'s coordinate origin is `ring_base+1000` in `media`'s own addressing, not
`ring_base+1024`/`DATA_START`, so a naive `target.offset - DATA_START` translation would be
off by 24 bytes. Not re-derived further this session (would need either tracing the real
video/mic writer's own append path or a live comparison against `ring.rs`'s own scanner).
