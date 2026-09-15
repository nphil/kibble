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
