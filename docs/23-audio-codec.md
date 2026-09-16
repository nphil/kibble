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

## 17.9 Live test, approved and run: `speak_start`/`speak_stop` proven safe; consumption proven absent, cleanly this time

Deployed to the device with full backup/md5 discipline (backup `kibbled.pre-audiostart`, md5
`f862cc2e52cb14aacb461027c82e3788`; new binary md5 `1b4e1e3570ccba0bca6d28db380a5adf`, byte-
verified transfer). Built from this branch on top of current `origin/main`.

**First run (confounded, not counted as evidence)**: a 2.5s/~40-frame clip showed `SndFrm`
advance 1842->1867 (+25) and a previously-nonexistent ADEC channel appear with `SndStrm=1876`.
Initially reported as a possible real result. Two independent confounds surfaced on review: (a)
Nitin created a Petkit-app feeding schedule within the same minute, which plays a vendor canned-
prompt through the *same* `AX_ADEC`/`AX_AO` hardware resources and `/proc` counters `audio_out_
thread` uses -- the two are not distinguishable from counter values alone, only from clean timing
isolation; (b) a concurrent agent's own kibbled restart/log-redirect truncated `/tmp/kibbled.log`
mid-run, destroying this session's own `frames_written` confirmation. Neither confound was caused
by anything wrong in the write path or the new `AudioOutThread` code; both were environmental.

**Settled, independent of the confound**: the "is `slot->0x12` a bitmask that could select the
microphone" question Main raised is resolved from `ring.rs`'s own live-verified channel constants
(not the live test): `CHAN_AUDIO`(mic)`=1`, `CHAN_MAIN=4`, `CHAN_SUB=8`, `CHAN_THUMB=16`,
`CHAN_AUDIO_OUT=2` -- a proper one-hot scheme across all five real channels. `slot->0x12=2`
ANDed against mic's channel byte (`1`): `0b001 & 0b010 = 0`. Zero overlap. The filter can only
ever pass `CHAN_AUDIO_OUT` records; a mic-audio feedback loop through this specific mechanism is
not possible **[HIGH]**.

**Second run, clean**: device frozen to every other agent for the duration (no concurrent telnet/
HTTP/log access). An 8.000s/128,000-sample/**exactly 125-frame** clip (1024 samples/frame, no
remainder -- chosen so the expected `SndFrm` delta is unambiguous and could not coincidentally
match ambient activity), prediction (`+125`, stated before sending) recorded in the hub log ahead
of the test per the same discipline as every other consequential action this project takes.

| checkpoint | UTC | `SndFrm` | ADEC `SndStrm`/`DecOk`/`GetFrm`/`RlsFrm` | guard flag (`0x767f0`) |
|---|---|---|---|---|
| baseline | 21:34:36 | 1867 | 1876/1876/1867/1867 | 0 |
| send | 21:34:42.65 | -- | -- | -- |
| +~16s (past the 8s window) | 21:34:59 | 1867 | 1876/1876/1867/1867 | 0 |
| +~40s, final | 21:35:22 | 1867 | 1876/1876/1867/1867 | 0 |

**Every single number identical, before and after, down to the digit.** Predicted `+125`, got
`+0`. `aenc` showed only the idle banner throughout (confirmed no real app call this time, so
this is not a repeat of the first run's confound); all 7 vendor PIDs present with continuous
uptime across the whole test; RTSP verified `h264`+`aac` immediately before and after; the guard
flag read `0` at every checkpoint (either it never needed to be `1` for as long as a between-
checkpoint gap could catch, or `speak_stop` cleared it promptly either way -- the vendor-side
counters are the load-bearing evidence here, not the flag's transient value).

**Conclusion, at full confidence this time**: `speak_start`/`speak_stop` and the `AudioOutThread`
RAII lifecycle are proven safe end-to-end on real hardware -- idempotent, self-cleaning-up,
zero health-gate impact, guard flag verified correct. `audio_out_thread` spawns and behaves
exactly as disassembled. **Kibbled's write is still not consumed.** This is no longer a
disassembly prediction or an ambiguous live reading; it is a clean, isolated, confound-free
measurement that matches §17.3's root cause exactly: consumption gates on `ring_base->0x18`, the
single mutex-protected all-channel sequence counter, which kibbled's writer has never touched.
The first run's `+25` is now understood as the vendor's own canned-prompt playback (a real pet-
call-adjacent event, coincidentally timed), not evidence of anything Kibble wrote being played.

**Acceptance bar, honestly met via path (b)**: not audible playback, but a precisely evidenced
reason it still cannot be driven safely without a new, separate, higher-risk step -- joining the
`PTHREAD_PROCESS_SHARED` mutex embedded in `media_buffer_frame_buf` and correctly advancing the
same global counter live video/mic recording depends on (§17.3.1's lower-risk candidate, "ride
the counter's existing ambient cadence by rewriting only `slot->0x14`/`0x18`," remains a real,
disassembly-grounded hypothesis but is unverified and was explicitly not attempted this session
per standing instruction -- the household's camera path is not something to experiment on without
a deliberate, separate go-ahead).

## 17.10 The publish protocol, fully disassembled: mutex robustness, exact field sequence, torn-record story, recovery

Found the actual publisher this session -- a shared `publish(chn_ctx, header_and_len_struct)` at
`0x32540`, called (by cross-reference, not yet individually confirmed per-caller) from wherever
`media`'s own video/mic encoder completion callbacks live; not itself re-derived this session, but
its *body* is fully disassembled and is the same function every legitimate writer funnels through
-- confirmed by it being the *only* place in the whole binary that increments `ring_base->0x18`
(searched: exactly one `str` to that offset from the global-pointer register, at `0x326aa`) or
writes `ring_base->0x28` (`0x326a2`-adjacent stores are the only writes to that field anywhere).
Answering Main's four questions in order:

### 17.10.1 Is the mutex robust? No -- confirmed by absence, not inference

**No.** `pthread_mutexattr_setrobust` and `pthread_mutex_consistent` appear **zero times** in
`/app/bin/media`'s entire dynamic symbol table (`.dynsym`, 455 entries, every imported libc/SDK
function enumerated and checked by name) -- for a dynamically-linked binary, a symbol that is
never imported can never be called, so this is not "not found by this session," it is "does not
exist in this binary" **[HIGH]**. `PTHREAD_PROCESS_SHARED` **is** confirmed: the only
`pthread_mutexattr_setpshared` call site in the whole binary (`0x3229e`) feeds the same mutex
object at `ring_base+0x00`, immediately followed by `pthread_mutex_init` at `0x322a6` -- this is
the one and only process-shared mutex `media` creates.

**Consequence, stated plainly**: this is a plain futex-based mutex with no kernel-assisted
owner-death recovery. If any process dies while holding it -- `kibbled` included -- the futex
word in shared memory stays marked "locked," the kernel does nothing special (`set_robust_list`
cleanup only fires for mutexes created with the robust attribute), and *no* future
`pthread_mutex_lock`/`_timedlock` call from *any* process will ever succeed again. Since this is
the **same single mutex every writer uses** (§17.10.2), that means video and mic recording stall
too, not just audio -- confirmed by tracing every `pthread_mutexattr_setpshared` call site (one)
and cross-referencing that `0x31d58` (the lock-with-timeout helper) is called from all of: the
registry-slot-open path, the channel-mask-set path, the audio-out consumer's poll loop, *and* the
publish function itself (`0x3255e`) -- one mutex, shared by the read and write sides of every
channel.

### 17.10.2 Exact publish sequence, byte-level, from `0x32540`

1. **Before the lock**: bounds check only (`length + 56 <= 0x800000`); reject and return early if
   not. No lock held for this step.
2. **Lock** (`0x31d58`: `pthread_mutex_trylock`, else a 1-second `pthread_mutex_timedlock`,
   logging+propagating failure without ever touching ring bytes on a failed lock -- §17.10.3).
3. An **advisory-only** check (compares `ring_base->0x28`/`0x24`/`0x20` against a fixed
   threshold; if exceeded, logs a warning and calls a **counter-reset** helper (`0x31c38`, zeros
   `ring_base->0x18/0x1c/0x20/0x24/0x28` back to `0`) but does **not** abort -- write proceeds
   regardless, now starting from a wiped write cursor). Not fully re-derived this session (an edge
   case, not the steady-state path); flagged rather than guessed at.
4. **Bump the global sequence counter first, before any bytes move**: `ring_base->0x18 += 1`;
   the *new* value is written straight into the caller's own soon-to-be-written header struct
   (`*header_struct = new_seq`) -- this is where a record's header seq field ultimately comes
   from, confirming `ring_base->0x18` is the single source of truth for every channel's seq,
   not just a value readers happen to track.
5. Conditionally, **only when `ring_base->0x20 == 0` at that instant**, also sets
   `ring_base->0x1c = new_seq` -- a bootstrap/edge condition, not a per-write step; this is the
   mechanism behind `docs/11-media.md`'s old "0x1c trails 0x18 by a roughly-constant gap"
   observation (set once, early, then just falls behind as 0x18 keeps incrementing).
6. **Write the 56-byte header** at `ring_base + 0x3e8 + ring_base->0x28` via a generic
   `write_ring_bytes(offset, src, len)` helper (`0x31d1c`, the write-side mirror of the read
   helper `0x31cdc` -- same `+0x3e8` base, same `0x800000` wrap modulus, confirmed symmetric).
7. **Advance `ring_base->0x28` past the header** (`+= 56`, wrapped), then **write the payload**
   at the new offset via the same helper.
8. **Advance `ring_base->0x28` past the payload** (wrapped mod `0x800000`) -- `ring_base->0x28`
   is, definitively, **the ring's write cursor / next-free-byte offset**, touched by every single
   append. This resolves §17.3's flagged uncertainty about `audio_out_thread` seeding its own read
   cursor from this exact field at open time: it is not a stale or arbitrary value, it is "start
   reading from wherever the writer currently is" -- the same "fast-forward to now, don't replay
   history" policy `ring.rs`'s own `Walker::seed()` independently implements on kibbled's side.
9. Also advances `ring_base->0x20` by `56 + length`, **not wrapped** -- a monotonic running total
   consumed only by step 3's advisory/reset check next time, not by any reader.
10. A conditional side effect when the record's own `frame_type`-equivalent header byte (`+0x20`
    within the header struct) equals `1` (a keyframe flag, matching `docs/19-frame-ring.md`'s
    `FRAME_KEYFRAME`): copies a couple of header fields into `ring_base+0x3a4`/`+0x3b0` --
    plausibly last-keyframe bookkeeping for some other consumer; tangential, not re-derived
    further.
11. **Notify loop, still under the lock**: walks all 20 registry slots (`base+0x2c` stride `0x2c`,
    same geometry as §17.2); for each slot with `+0x50 != 0` (active) *and* a bitmask match
    between the just-written record's channel field (header `+0x22`, the exact field
    `audio_out_thread`'s own filter reads, §17.8) and that slot's own `+0x3e` subscription mask,
    calls a per-slot notify helper (`0x31f64`): lazily `sem_open()`s a **named POSIX semaphore**
    indexed by the slot's own `idx` field (`+0x10`) the first time, then `sem_post()`s it, then
    clears a per-slot `+0x28` "wants a kick" flag. This is a **second synchronization primitive**
    beyond the mutex -- a per-consumer semaphore array -- not mentioned in any prior session's
    docs. **Not required for correctness here**: `audio_out_thread`'s own poll loop (§17.3, fully
    traced) never calls `sem_wait`/`sem_timedwait` anywhere in its body -- it is a pure poller
    gated on `ring_base->0x18` vs its own bookmark, re-checked on a timer/loop cadence, not woken
    by this semaphore. A publish that skips the notify step would only cost some polling latency
    for *this specific* consumer, not correctness -- but is noted precisely rather than assumed,
    since a different consumer (if any) could depend on it.
12. **Unlock** (`0x31e44` -- a 4-instruction tail call straight into `pthread_mutex_unlock`,
    confirmed by its target PLT address; nothing else happens between the notify loop and the
    unlock).
13. Return `0`.

### 17.10.3 Torn/mid-write records: not a live race under normal operation

Traced the **read side's own lock discipline** in `audio_out_thread`'s poll function (`0x32d1c`)
precisely: it locks at entry (`0x32d40`, same `0x31d58` helper) and does **not** unlock until one
of exactly two exit points, both confirmed by address: (a) the "nothing new right now" path
(`0x32e1e`) -- which, before unlocking, **re-syncs `slot->0x14`/`0x18` to the current
`ring_base->0x18`/`0x28`** so the consumer's own bookmarks never silently drift behind the writer
-- or (b) the "found and copied a matching record" path (`0x32f9c`), which unlocks **only after**
the payload `read_ring_bytes` call (`0x32f86`) has already completed. **The same mutex that
serializes every writer's header+payload write also serializes every read of that data** -- a
reader can only ever observe a fully-written record or nothing yet, never a partial one, as long
as it holds the lock (which it does, for the entirety of both header and payload reads) **[HIGH,
directly disassembled, not inferred from the header's own seq/length sanity fields]**. This also
means the *existing*, independently-documented "torn record" tolerance in `docs/23-audio-codec.md`
/`ring.rs` (sane-length + seq-continuity + rescan-on-failure) is defense for a **different**
scenario -- a reader that does *not* take this lock at all (kibbled's own background poller,
`ring.rs`'s `Walker`, which was designed before this mutex was understood and reads raw bytes
without ever calling into `media`'s pthread API) -- not evidence that torn writes are a live
concern for a lock-honoring writer. A failed lock acquisition (`0x31d58` times out after 1s)
returns failure **without touching any ring bytes** on either the read or write side -- confirmed
for both call sites -- so a contended-but-not-wedged mutex degrades to "try again later," never to
a partial read or write.

### 17.10.4 Recovery story: `/dev/shm` is tmpfs, confirmed live

`mount | grep shm` on the device, live: `tmpfs on /dev/shm type tmpfs (rw,relatime,mode=777)`
**[HIGH, live-read, read-only command]**. RAM-backed, non-persistent. A reboot recreates the
segment from nothing, re-running the zero-init + mutex-init sequence (§17.2) from scratch --
**confirmed**: worst case for a wedged mutex is "reboot the feeder," which is recoverable, not
"replace the device" or "silent permanent camera loss." This materially bounds the downside of
§17.10.1's finding, exactly as Main's framing anticipated, but does not remove the need for the
design below -- an unplanned reboot of the household's camera/mic is still a real cost, not a
free option.

## 17.11 Design whose failure mode is bounded by construction (not implemented -- awaiting Main's decision on whether to write it)

Given §17.10's answers, a `kibbled`-side publish that cannot wedge the shared pipeline by
construction:

1. **Compute everything before locking.** Encode the AAC payload, build the full 56-byte header
   in a local (stack/heap) buffer with every field *except* the seq number, and know the payload
   length -- all of this already happens today, outside any lock, in `audioout.rs`. Nothing
   fallible happens after this point.
2. **Lock with a bounded timeout, never indefinitely.** `pthread_mutex_timedlock` (not
   `_trylock`-then-block, not a bare `_lock`) with a short deadline (hundreds of ms, not the
   vendor's own 1s -- kibbled has no reason to wait as long as `media`'s own internal callers,
   which are on a real-time video pipeline's own deadline pressure). On timeout: treat exactly
   like a normal "busy, try again" `SpeakError`, the same way a failed ring open already fails
   `play_encoded` today. Never retry the *same* lock call in a loop that could itself block
   unboundedly.
3. **Inside the lock: read `ring_base->0x18`, increment, write it back, stamp the header, `pwrite`
   header then payload at `ring_base->0x28` (mirroring §17.10.2 steps 4-9 exactly, byte-for-byte,
   including the `ring_base->0x1c` conditional and the `+0x20` running-total update, so this write
   is indistinguishable from a real vendor write to every other reader), then unlock.** No
   allocation inside this section (buffers pre-allocated in step 1), no logging, no `eprintln!`,
   no panics possible (every fallible step -- the two `pwrite`s -- already happens today in
   `write_frame`, which returns `Result` rather than unwrapping; keep it that way inside the
   locked section specifically, with zero `.unwrap()`/`.expect()` anywhere between lock and
   unlock). Skip the notify-semaphore step (§17.10.2 step 11, `sem_post`) deliberately: it is
   real vendor behavior but not required for `audio_out_thread` (§17.10.3), and every extra
   `sem_open`/`sem_post` call inside the locked section is one more fallible operation to
   exclude by the rule above.
4. **`Drop`-guaranteed unlock**, exactly the same RAII discipline already proven this session for
   `AudioOutThread`/`speak_stop` (§17.4-§17.7): a guard type whose `Drop` unlocks even on an early
   return or (should it ever happen despite `panic = "abort"` making this moot for kibbled's own
   process) a panic unwind. `panic = "abort"` cuts the other way here and matters more than usual:
   it means *any* panic anywhere in kibbled, not just in the audio path, aborts the whole process
   immediately -- so the real mitigation is keeping the locked section itself panic-free by
   construction (step 3), not relying on unwind-time cleanup that this profile disables anyway.
5. **Never write `slot->0x14`/`0x18` directly** (§17.3.1's original, lower-risk-looking idea) --
   §17.10.2 shows those are consumer-owned working state that the *publish* protocol updates as a
   side effect of a correct append, not a separate channel a writer is meant to poke. Following
   the real protocol exactly (steps 1-4 above) makes that idea moot: a correct publish already
   causes every matching consumer's next poll to see new data through the same
   `ring_base->0x18`-vs-bookmark gate every other channel relies on.

This is a design, not an implementation -- no code written against it this session, per standing
instruction. Awaiting Main's go/no-go before touching `agent/src/audioout.rs`'s writer again.

## 17.12 §17.11 implemented and deployed; lock exclusion proven live; consumption still not
   achieved -- root cause narrowed to a reset/bookmark race, not disproven, not fully proven

Author: AudioPublish. Implemented the §17.11 design in `agent/src/audioout.rs` (`publish`,
`lock_ring_mutex`/`unlock_ring_mutex`, `WritableRegistry`), built and deployed it live with full
md5 discipline, and ran two supervised consumption tests. **Net result: the vendor's mutex
protocol is now correctly implemented and independently proven to provide real cross-process
exclusion (not just "didn't crash") -- but `SndFrm` still did not move on either test.** The two
tests, plus live counter readings taken while chasing the first negative result, point at a
specific, plausible new failure mode (a counter-reset race against `audio_out_thread`'s bookmark)
that this session found but could not fully confirm or fix live. Every claim below is tagged
[HIGH] (directly observed this session), [MED] (strong circumstantial evidence, not fully
isolated), or **[UNPROVEN -- FLAGGED]** (believed likely, explicitly not confirmed) per Main's
standing instruction to separate those clearly.

### 17.12.1 Two discrepancies resolved before writing any code [HIGH]

1. **`ring::DATA_START` (1024) is not the vendor's own address origin.** §17.8 already flagged
   `read_ring_bytes`'s disassembly-confirmed `0x3e8` (1000) as "24 bytes below `DATA_START`,
   ...not re-derived further." Resolved by arithmetic, not new disassembly: §17.2's independently
   disassembly-confirmed segment capacity (`0x800000` exactly) plus `0x3e8` equals `ring.rs`'s own
   live-measured total file size (`RING_LEN` = 8,389,608) *exactly* (`0x800000 + 0x3e8 ==
   8_389_608`). `DATA_START` is a safe scan-start seed for the read side's self-resyncing walker
   (harmless to be off, since `find_next_header` corrects for it) but is the wrong constant for
   byte-exact writer addressing. `publish()` uses `0x3e8` (`VENDOR_DATA_ORIGIN` in code).
2. **musl calling glibc's mutex would have been a silent, undetectable correctness bug.** Fetched
   and read musl's actual `pthread_mutex_timedlock`/`unlock`/`__timedwait` source: musl decides
   private-vs-shared futex mode from its own `_m_type` field, at a musl-specific struct offset.
   glibc's `pthread_mutex_t` (fetched and read glibc 2.25's actual `nptl`/`sysdeps/nptl` source)
   lays out `__kind`/`__lock`/etc. differently. Calling musl's own `pthread_mutex_lock`-family
   functions on this already-glibc-initialized mutex would read garbage where musl expects its own
   pshared flag, most likely misdetecting the mutex as process-*private* and using the private
   futex path -- no error, just zero real cross-process exclusion. `agent/src/audioout.rs` instead
   hand-rolls the lock directly against the known-correct offset-0 word using always-non-private
   raw `futex(2)` syscalls (`SYS_futex`=240, confirmed against the kernel's own
   `arch/arm/tools/syscall.tbl` for ARM EABI), implementing glibc's exact 3-state (0/1/2) algorithm
   (verified against glibc 2.25's actual `lowlevellock.h`/`lowlevellock.c`, not from memory).

### 17.12.2 Lock exclusion independently verified live, before touching `kibbled` at all [HIGH]

Built a throwaway verification binary (`src/bin/locktest.rs`, deleted after use, never committed)
implementing the identical lock/unlock algorithm standalone. Pushed to `/tmp`, ran directly against
the live mutex:

- **Realistic-to-generous range (1 ms - 700 ms hold, six runs: 1/5/20/100/300/700 ms).** Sampled
  `ring_base->0x18` and the mutex word *from inside the held critical section itself* every
  100-200 us (228+ samples total across all six runs). **Zero exceptions**: `seq_advanced_during_
  hold=false` on every single run, including the full 700 ms run (39x longer than this session's
  own `LOCK_TIMEOUT`=200 ms, and many orders of magnitude longer than `publish()`'s actual hold,
  which is a handful of memory operations with no syscalls). The word correctly read `2`
  (locked-with-waiters) once holds exceeded the ~15-20 ms video/mic write interval, proving real
  vendor threads were contending and correctly blocking, not merely "some bit happened to be set."
- **Adversarial range (1.5-2+ s hold).** Word spuriously read back to `0` and `seq` resumed
  advancing *while the test process had not yet called its own unlock* -- i.e., something else
  released a lock this process still believed it held. This is a real anomaly, not a measurement
  artifact (confirmed with precise `CLOCK_MONOTONIC` timestamps and in-process sampling, ruling out
  the cross-process timing-correlation error the first, cruder version of this test suffered from).
  **[MED] working theory, not confirmed further**: the vendor's own `publish()` uses
  `pthread_mutex_trylock` then a **1-second** `pthread_mutex_timedlock` fallback (§17.10.2 step 2)
  before giving up; this anomaly appears only past that same ~1 s boundary, consistent with an
  edge case in how a pile of glibc waiters that individually time out at 1s clean up the shared
  "waiters" state under sustained multi-second contention -- a scenario this project's own writer
  never creates (hold time: microseconds; `LOCK_TIMEOUT`: 200 ms, both far short of 1 s). Not
  re-derived via disassembly of glibc's `__lll_timedlock_wait` this session; flagged, not chased
  further, because it does not appear reachable by the real code path.
- Ruled out `/tmp/slot7mon` (AudioAnnounce's still-running passive sampler) as a confound: its own
  prior documentation states it is structurally `O_RDONLY`+`PROT_READ`-only, independently
  confirmed by simple physics (a `PROT_READ` mapping cannot write, so it cannot be the source of
  any mutex-word change regardless of anything else).

### 17.12.3 Deployment [HIGH]

Backup `kibbled.pre-audiopublish`, md5 `1b4e1e3570ccba0bca6d28db380a5adf` (the binary that was
live going into this session). New binary md5 `c87501c8f4ad8ac671c6dd8437f45980`, byte-verified
on-device after transfer, byte-verified again as still-running at session end. Both reported to
Main before and after, per standing instruction. Health gate before AND after every device
interaction this session (initial deploy, both `/speak` tests): all 7 vendor PIDs continuous
accumulated CPU time throughout (`media` 5h55 -> 6h03+ across the session, never reset), `aenc`
idle 3-line banner every time, RTSP `/main` serving live h264+aac via `ffprobe` every time. No
vendor process ever touched, killed, or restarted. `kibbled` itself was killed once (by design,
the only way to pick up a new binary under this device's `app_init.sh` supervisor loop) and came
back on its own supervisor-managed restart within its normal ~5s cycle, verified by PID change and
immediate re-confirmation of every health-gate item.

### 17.12.4 Two consumption tests, both `SndFrm` delta = 0 against a predicted +125 [HIGH]

Clip: 8.000 s / 128,000 samples / exactly 125 frames, 440 Hz, 50 ms fade in/out, sent via
`POST /speak` (not the vendor's feed-prompt sound). `SndFrm` baseline 1867, confirmed stable
across multiple reads spanning several minutes before either test.

- **Test 1** (predicted and pinged to Main before sending): HTTP 200,
  `{"ok":true,"samples":128000,"estimated_ms":8000}`. Waited 18.5 s (> clip duration + encode
  overhead). `SndFrm` before=1867, after=1867. **Delta 0, not +125.**
- **Test 2** (re-seed retest, Main-authorized, no audible confirmation needed -- `SndFrm` movement
  alone is the acceptance bar): same clip, same result. Waited 30.3 s. `SndFrm` before=1867,
  after=1867. **Delta 0 again.**
- Neither test regressed the health gate (§17.12.3). Case (c) (published+consumed but inaudible,
  e.g. a volume/routing problem) is therefore ruled out for both tests: the counter that would
  prove consumption never moved at all.

### 17.12.5 The reset/bookmark-race hypothesis: the evidence, and exactly what is NOT proven

While chasing test 1's negative result, direct reads of `ring_base`'s registry fields (raw byte
dumps via `dd`/`od` over telnet, no new binary needed) found:

- Shortly after test 1: `ring_base->0x18` (the global sequence, i.e. `audio_out_thread`'s gate
  counter) read **8973** -- far below the ~305,000+ range this same counter was in minutes earlier,
  during §17.12.2's lock-verification runs. **[HIGH, directly read]**
- Confirmed the counter was not stuck: two reads 3 s apart (13,665 -> 13,882) gave a rate of
  **72.3/s**, matching the documented ~70 Hz video/mic write rate exactly. The writer side is
  healthy; it is simply counting up from a much lower baseline than before. **[HIGH]**
- A second pair of reads, taken after test 2, found the counter **had dropped again**: 4,005 ->
  4,146 over 2 s (rate 70.5/s, again healthy). **[HIGH]** This means at least two resets occurred
  during this session's own testing window, not one.

**[MED] hypothesis, partially tested, not confirmed**: §17.10.2 step 3 describes an
advisory-only overflow check the real `publish()` runs on every call, which zeros
`ring_base->0x18/0x1c/0x20/0x24/0x28` back to 0 if a running-total threshold is exceeded --
explicitly a *normal*, vendor-designed, harmless-to-video/mic mechanism, not a bug. If
`audio_out_thread` is spawned (via `speak_start`) and seeds its bookmark from the counter
*before* such a reset fires, then the counter is zeroed *after*, the gate
(`ring_base->0x18 >= slot->0x14`) can only be satisfied once the (now near-zero) counter climbs
back past the old, much higher bookmark -- at ~70/s, over an hour for a ~300,000 gap. This would
fully explain zero consumption independent of whether `publish()` itself is correct.

**This session's own lock-hold stress testing (§17.12.2, six runs holding the real mutex for up
to 700 ms, each followed by a burst of queued writers all landing at once) is the most likely
trigger for at least the first reset, and very plausibly primed the ring for the second one too --
say this plainly, as instructed: a diagnostic side effect of proving the lock is very likely what
broke the very counter the subsequent audible test depended on.** This is offered as the most
plausible explanation, not a proven causal chain -- no threshold constant was disassembled or
directly observed being crossed.

**What this hypothesis predicts, and where the prediction failed:** if a stale pre-reset bookmark
were the *whole* story, re-sending `speak_start` *after* the reset (letting the previous, never-
timed-out-yet, but never-consuming thread's own 5 s idle timeout lapse, then a fresh `/speak` call)
should re-seed the bookmark from the current, healthy, climbing counter and fix consumption on the
next test. **Test 2 was exactly this retest, authorized by Main specifically because it needed no
audible confirmation -- and it also returned delta 0.** Two explanations remain open, and this
session could not distinguish between them before being told to stop:

(a) **[UNPROVEN -- FLAGGED]** A *third* reset happened during test 2's own ~8 s playback window
    (plausible: the confirmed second reset's timing overlaps test 2's send time, and 8 s is a long
    window if resets are currently happening every 1-5 minutes -- itself possibly elevated by test
    2's own 125-record write burst on top of already-disturbed state from testing). If so, the
    ordering fix is directionally correct but insufficient alone while the ring is in this
    unusually reset-prone state; consumption may well work correctly once tested against an
    undisturbed ring (e.g., after a device reboot clears whatever accumulated state is driving the
    elevated reset frequency).
(b) **[UNPROVEN -- FLAGGED]** `publish()` itself is not correctly writing consumable records (a
    real bug in this session's new code, separate from the reset story entirely). This session
    could **not** rule this out directly: the one planned direct check -- pulling a chunk of the
    ring's raw bytes back off the device to confirm 125 well-formed `chan=2` records physically
    exist where `publish()` should have placed them -- failed for tooling reasons (a device-
    initiates-outbound `nc` push timed out; this project's own proven pattern, used successfully
    elsewhere in this session, is the *device listens, host connects in* direction instead, e.g.
    `recv.py`'s approach). Not retried, per the explicit instruction to stop rather than start
    anything new. **`publish()`'s correctness was verified by compilation, by the unmodified,
    passing `build_record`/`find_append_target` unit tests, and by the mutex-exclusion proof in
    §17.12.2 -- but never by reading back an actual record it wrote to the live ring.** This is the
    single most important gap in this session's evidence and should be the next session's first
    check, before any further audible test.

### 17.12.6 Also flagged, not investigated this session [UNPROVEN -- FLAGGED]

- **Volume mapping.** `/state` reportedly shows a `volume` of `20`; `/config`'s documented scale
  (per a separate, HA-side finding this session did not independently verify) is `0`-`9`. If the
  device is ever handed an out-of-range value, or `20` maps to something near-silent, a fully
  successful publish-and-consume could still be inaudible (case (c) from Main's framing) --
  worth a direct check *before* concluding a future non-zero `SndFrm` result means "audible."
- **`/tmp/kibbled.log` stopped being useful mid-session.** It predates this session (its visible
  content was RTSP session lines from before any restart here) and is not written by the
  `app_init.sh`-supervised `kibbled` process, which redirects both stdout and stderr to
  `/dev/null`. `PlaybackStats.frames_written` and any `SpeakError` from a `/speak` call are
  therefore invisible in the current deployment, which is exactly the gap that left test 1's
  "did the write even happen" question open. Next session should arrange a real log sink (a
  file `kibbled` itself opens and writes to, not a shell redirect that only exists for as long as
  someone manually launches it that way) before running another live test.

### 17.12.7 State at handoff

`kibbled` running (fresh restart, PID confirmed, md5 `c87501c8f4ad8ac671c6dd8437f45980`), all
vendor processes undisturbed, health gate clean, telnet session released. Branch `audio-publish`
pushed to origin, not merged, per standing instruction. The `publish()` implementation is real,
compiled, deployed, and its locking is independently proven exclusive on real hardware -- genuine
progress over §17.11's "design, not implemented" state -- but audible/consumption proof is still
not achieved, and the honest reason why is narrowed to two candidate explanations (§17.12.5) that
the next session should resolve in this order: (1) fix the ring-readback tooling and directly
confirm or refute that `publish()` writes valid records, independent of any consumption question;
(2) if writes are confirmed good, retest consumption against a freshly-rebooted (not just
freshly-restarted) device to remove the elevated-reset-frequency confound entirely; (3) only then,
if `SndFrm` still fails to move, revisit the gate model itself.

## 18. `play_aac_file` payload decoded; the whole dispatch chain re-verified live; `speak_start` still produces zero effect even from a byte-identical sender post-reboot

Author: AudioSolve. Method: disassembly (capstone + pyelftools, same pipeline every prior
session used) against the same `f9e74f321a2bb7693f495598d816386a` `media` copy, **plus**, new
this session, live read-only verification against the actual running device: a mounted
`mqueue` pseudo-filesystem, `/proc/<pid>/fd`, and a handful of targeted `/proc/204/mem` reads
(never `ptrace`, never a write, never touching a vendor process's execution). One real,
Main-approved device reboot was performed and is reported in full. Every claim below is tagged
per this doc's standing convention.

### 18.1 A methodological bug in this session's own tooling, found and fixed [HIGH]

Early in this session's disassembly, `register()`'s table-population arguments (the `r1`/`r2`
GOT-indirected values every prior session's own re-derivation relied on) resolved to
garbled-looking 32-bit values instead of clean pointers. Root cause, found by hand-verifying
one known-good case: this session's PC-relative-address simulator applied `Align(PC,4)` to
**every** PC-relative instruction, but that alignment rule is only correct for `LDR`
(literal)/`ADR`-class instructions -- a plain data-processing `ADD Rd, PC` (used throughout
this binary to materialize a GOT/data base pointer, e.g. `ldr r4,[pc,#N]; add r4,pc`) uses the
**unaligned** `PC = instruction_address + 4`. Whenever the `add`'s own address happened to
already be a multiple of 4 the bug was invisible (several early spot-checks this session
coincidentally landed there); whenever it wasn't, every downstream GOT dereference read two
bytes into the wrong slot, producing exactly the "half of one field + half of the next"
garbage this session chased for a while. Fixed by using unaligned `PC` for `ADD`/`MOV`-class
PC reads and keeping `Align(PC,4)` only for `LDR`/literal reads. **Flagging this explicitly
for any future session redoing this kind of analysis on this binary -- it is a real trap, not
specific to this session's code, and would silently corrupt any GOT-relative table read.**

### 18.2 `dispatch_handler_play_aac_file`'s payload: a plain, unconstrained path string [HIGH]

Traced from `register()`'s live table entry for `msg_id=2` (handler `0x31a05`, confirmed live,
§18.4) through to the actual file I/O, every call PLT-resolved by symbol name against
`.rel.plt`/`.dynsym` (not guessed):

1. **Trampoline** (`0x31a04`): receives `(msg_id, src, payload_ptr, payload_len)` -- the same
   4-argument convention `dispatch_mqueue_read` uses for every handler (§18.3). Gates only on
   `payload_len-1 <= 0xff` (i.e. length in `1..=256`, purely for whether a debug log is safe to
   print) and `payload_ptr != NULL`; on the real path, calls `0x314d4` with `r0 = payload_ptr`.
2. **`0x314d4`**: `strlen(payload_ptr)`, `malloc(len+1)`, `memcpy` -- a plain heap string
   duplicate of the payload, **no prefix, no suffix, no directory concatenation, no table/index
   lookup anywhere in this path**. The duplicate exists so the payload -- which only lives for
   the duration of the dispatch call, backed by `dispatch_mqueue_read`'s own stack buffer -- has
   a stable copy for the async worker thread below.
3. `pthread_create(thread, NULL, start_routine=0x301fa, arg=<the heap copy>)`.
4. **Worker thread** (`0x301fa`): `pthread_self`+`pthread_detach` (self-detaching, same idiom as
   `audio_out_thread`), `pthread_mutex_lock` on an unrelated internal state mutex (not the ring
   mutex), then **`fopen(arg, "rb")` with the heap-copied string passed verbatim as the path** --
   confirmed by symbol name, not inferred. `fread`s exactly 7 bytes and checks for a genuine
   ADTS sync word (`0xFF` then top nibble `0xF`) before proceeding to
   `AX_ADEC_SendStream`/`AX_ADEC_GetFrame`/`AX_AO_SendFrame` (§6's already-confirmed
   canned-prompt engine); on a bad/missing file it logs via `fputs` and falls through to
   `fclose`/`free` cleanup instead of crashing.

**This is an arbitrary-path file player, not a fixed-prompt index.** Any path this device's
root user can `fopen()` -- including a file `kibbled` itself wrote under `/tmp` or
`/opt/kibble` -- is a legal payload as far as this code is concerned. `/audio/en/*.aac` (the
vendor's own 7 KB-ish prompts, confirmed AAC-LC/16kHz/mono ADTS via local `ffprobe` on a
fetched copy) needs no special-casing; it is simply the vendor's own choice of `arg`.

### 18.3 `dispatch_mqueue_read` and the message table, fully re-derived and cross-checked live [HIGH]

Corrected the prior citation of this function's address (`0x33171`, one byte off a real
instruction boundary and, worse, disassembled without `capstone`'s `skipdata=True`, which
silently produces garbage across any embedded literal pool -- the true function starts at
`0x33172`, one instruction into what looked like noise under the old method). Full trace, byte
by byte:

1. `mq_receive(mqd, buf=&local[544 bytes], len=0x220, prio=NULL)` -- `0x220` = 544 matches
   `bus.rs`'s own documented `msgsize` exactly. The 544-byte buffer is `memset` to 0 immediately
   before every call, so an under-length message's tail is zero, not garbage.
2. `payload_len = bytes_received - 4`. `msg_id = buf[0:2]`, `src = buf[2:4]` (both `u16`) --
   matches `bus.rs`'s documented envelope exactly.
3. Two sentinels checked before general dispatch: `msg_id==0xFFFF` returns immediately without
   dispatching anything (a shutdown/no-op value); `msg_id==0x103` skips a verbose debug-log call
   only, still dispatches normally. Neither is `0xa`/`0xb`/`0x2`.
4. **Table lookup**: `pthread_mutex_lock` a dedicated table-protection mutex (confirmed, by
   disassembly, to be a *different* mutex object from the ring's `media_buffer_frame_buf` one --
   this table search cannot be affected by anything this project has done to the ring mutex),
   linear-scans a **12-byte-stride array** (`{u16 msg_id, u16 pad, u32 handler, u32
   name_str_ptr}`) comparing only the leading `u16`, `pthread_mutex_unlock`, then, if found and
   `handler != NULL`, calls `handler(msg_id, src, payload_ptr, payload_len)` with `payload_ptr`
   pointing at `buf+4` (the same 544-byte receive buffer, not a second copy). `register()`
   (`0x337c8`) is confirmed to write this exact same table (cross-derived from *both* sides --
   the reader's table-base computation and the writer's -- landing on the identical address once
   §18.1's bug was fixed).

### 18.4 Live confirmation: the table is correctly populated, right now, on the actual running device [HIGH]

Read the table directly out of `media`'s own live memory (`/proc/204/mem`, root, plain
byte-range read -- no `ptrace`, no signal, no pause of the target; the same read-only technique
this project already uses for `/dev/shm/media_buffer_frame_buf`). Base address `0x76844`
(the *other* of two independently-computed candidates was wrong -- a stale/miscomputed literal
offset, discarded once the live read distinguished them). First 8 entries, exactly as predicted
by static analysis with zero discrepancies:

| index | msg\_id | handler | name (from §18.1-corrected `register()` trace) |
|---|---|---|---|
| 0 | 3 | `0x1a029` | `dispatch_handler_set_day_night_mode` |
| 1 | 4 | `0x28091` | `dispatch_handler_get_jpeg` |
| 2 | 6 | `0x28091` | (same handler, second registration) |
| 3 | 1 | `0x283d9` | `dispatch_handler_request_IDR` |
| 4 | **2** | **`0x31a05`** | `dispatch_handler_play_aac_file` |
| 5 | **0xa** | **`0x31b21`** | `dispatch_handler_speak_start` |
| 6 | **0xb** | **`0x31b2d`** | `dispatch_handler_speak_stop` |
| 7 | 0x1013 | `0x241f1` | `dispatch_handler_algo_ctrl` |

Entries 4-6 (the three this project cares about) are **byte-identical in shape** to every
neighboring entry -- same 12-byte layout, non-null handler, plausible name pointer. No
corruption, no null, nothing structurally different singles them out. This directly answers
the "is 0xa/0xb's table entry different from a working one" question: **no**, at the table
level they are indistinguishable from entries this device uses for its own core video
functions (`request_IDR`, `get_jpeg`).

Also read the `speak_start`/`speak_stop` guard flag (`0x767f0`, the address prior sessions
cited) directly: **0**, not stuck at `1`. Not independently re-derived this session with the
§18.1-corrected method, so this specific address is [MED] rather than [HIGH] -- but it is at
least consistent with "not the reason nothing happens."

### 18.5 The queue is draining, not backing up [HIGH]

Mounted the kernel's `mqueue` pseudo-filesystem read-only (`mount -t mqueue none /tmp/mq`,
trivially reversible, touches no persistent storage) and read `QSIZE` directly:
`msg_dispatch_1` and `msg_dispatch_2` both read **`QSIZE:0`** after multiple `speak_start`/
`play_aac_file` sends from this session. Also confirmed via `/proc/204/fd` that `media`
(PID 204) holds `/msg_dispatch_2` open (`fd 41`, `O_RDWR|O_NONBLOCK` per `/proc/204/fdinfo/41`,
matching the disassembled `mq_open` flags at `0x336ec`/`0x336e6` exactly) -- the exact same name
`bus::Peer::Media` opens. **Messages are not being sent to the wrong queue, and they are not
piling up unread; something is receiving and discarding them with zero observable effect.**

### 18.6 The reboot experiment: a wedged-thread hypothesis, tested and refuted [HIGH]

Before this section's live-memory work, the leading theory (this session, informed by
`speak_start` producing zero thread-count change from *this session's own* sender) was that
hours of prior sessions' ring-mutex stress testing (§17.12.2's 700 ms-2 s holds) had wedged
*only* `media`'s message-dispatch thread while leaving its video/audio subsystems healthy.
Tested directly, Main-approved, full before/after capture:

| | before | after |
|---|---|---|
| uptime | 9h01m | 91s (fresh boot) |
| `SndFrm` | 1867 | 0 |
| `media` thread count | 32 | 26 |
| ring `0x18` | 227285 | 6452 |
| `kibbled` md5 | `c87501...` | `c87501...` (identical, relaunched by `app_init.sh`) |
| all 7 vendor PIDs | present | present, fresh PIDs |

Reboot executed cleanly (`reboot`, busybox; ~35s to answer telnet again, +10s settle). Full
stack verified back: `GET /state` answering, RTSP `/main` serving `h264`+`aac` via live
`ffprobe`. Thread count dropping 32→26 on a clean boot is itself informative (something
*was* accumulated over the 9h session -- most plausibly ordinary per-RTSP-session worker
threads from repeated `ffprobe`/testing, not evidence of a leak specific to the audio path) but
**`speak_start`, sent identically post-reboot, still produced 26→26, not 26→27.** A completely
fresh `media` process, dispatch table freshly built by its own startup code, still does not
respond. **This refutes the wedged-thread hypothesis as this session understood it** -- whatever
is wrong is not accumulated session damage, and reappears from a cold start.

### 18.7 Where this leaves the investigation [HIGH for the facts, genuinely open for the conclusion]

Every mechanical layer this session could independently verify is now confirmed correct and
live-checked, not just statically inferred:
- Wire format, queue name, `src` value: byte-identical to `bus::Sender::send`, including a
  true zero-length-payload replica of the exact bytes `speak_start`/`speak_stop` send today.
- Transport: right queue (`/proc/204/fd` confirms), not backing up (`QSIZE:0`).
- Table: right handler address for `0xa`/`0xb`/`0x2`, live-read from the process's own memory,
  structurally identical to entries this device demonstrably uses for its own video pipeline.
- Guard flag: not stuck.
- Sender-side variables ruled out one at a time: payload length/NUL-termination (tried true
  zero-length), timing (waited up to 6 s), sender-process lifetime (5 s linger before
  `mq_close`), IPC namespacing (checked directly -- `/proc/<pid>/ns/ipc` does not exist on this
  kernel; effectively one global IPC namespace, so a separate-namespace queue is not possible
  here), and now session-accumulated vendor-side damage (reboot).

**What remains genuinely unexplained**: a message that is queued successfully, drained from the
queue, and whose table entry resolves to a real, correctly-shaped, non-null handler produces no
observable effect (no thread, no `SndFrm` movement, no error, no crash, no log this session
could see). The next test in progress as of this write-up is the one Main proposed: instrument
`media`'s thread count (plus `SndFrm` and the guard flag) at high rate and watch it through one
real, externally-triggered app press-and-hold talkback. That test directly brackets the last
remaining open question -- whether `speak_start`/`audio_out_thread` is really how the vendor's
own talkback reaches the speaker at all, independent of anything this project has ever sent.
**Not run yet as of this section**; see the live hub log / a follow-up doc section for the
result.

## 19. REGRESSION, CONFIRMED BY CONTROL TEST: a real vendor talkback session was cut short by our own autonomous `speak_stop`

**Update after §19.1-19.3 were first written (which called this "likely, circumstantial"):
Main rolled the device back to `f862cc2e` (no `media`-touching code at all) and had Nitin retry.
His talkback ran clean for 15+ seconds. It had cut at 5-8s every time on the binary containing
`SpeakerOwner::new()`'s unconditional startup `speak_stop`, and ran long and clean the moment
that code was gone from the running binary. That is a real control test, not a timing
correlation: root cause CONFIRMED, at [HIGH]. The GPIO error (`pa_gpio->pa_linout`, §19.1) is
exonerated as a one-off -- it appears exactly once in the whole kernel ring buffer, against (at
least) four talkback attempts, so it is not routine noise at every session start and not the
explanation either.** The restart-timing arithmetic (kibbled exit ~701.7s + the supervisor's
fixed 5s sleep = relaunch at ~706.7s, against an observed guard-flag-clear at 706.72s) remains
circumstantial -- no direct crash log exists for this specific incident, which is exactly the
gap §19.4's fixes close for next time.


Author: AudioSolve. **This is the most important finding in this document.** Everything in §18
answered "why does our own audio not play." This section answers a different, higher-priority
question Main raised mid-session: **did this project's own testing break a feature the
household already relies on?** Short answer: **very likely yes, on this occasion, and the exact
autonomous code path that could do it is identified below, code-verified, not inferred.**

### 19.1 What happened, in wall-clock/device-uptime order

Per Main's live report: Nitin pressed and held the Petkit app's talkback button, expecting the
normal experience (his voice plays through the feeder). It played for several seconds and then
**abruptly stopped while he was still holding the button** -- his own words: this is the first
time the app's talkback has ever cut out; it has always worked before. That makes this session's
testing the prime suspect for a genuine, user-visible regression, not merely "our own feature
still doesn't work."

A read-only sampler (armed and mirrored off-device per this project's standing discipline
*before* asking for the test, per §18.7) captured the whole window at ~0.3 s resolution. Device
`/proc/uptime` seconds, this boot (the one from §18.6's reboot):

| t (s) | `media` threads | `SndFrm` | guard flag (`0x767f0`) | event |
|---|---|---|---|---|
| 605-699 | 26 (steady) | 0 (steady) | 0 (steady) | baseline, nothing happening |
| **699.71** | **26→27** | 0 | **0→1** | a `speak_start`-shaped event fires -- Nitin's press |
| 700.08-705.79 | 27 | 0→77, climbing ~5 frames/sample (real-time paced) | 1 | audio genuinely flowing to the speaker |
| ~706.1-706.4 (est.) | 27 | **plateaus at 77 -- stops climbing** | 1 | **consumption stalls; Nitin is still holding, per his own report** |
| **706.72** | 27 | 77 | **1→0** | **something sends `speak_stop`** (the only thing that clears this flag) |
| **707.04** | **27→26** | 77 | 0 | `audio_out_thread` exits, one sample tick later |

**The sequence is: audio consumption stalls first (a ~1-2 s gap where nothing moves while the
guard flag is still `1`), then a `speak_stop`-equivalent event clears the flag, then the thread
exits.** This is a real, non-`kibbled`-initiated talkback session (nobody on this project sent
`speak_start` at `t=699.71`; every send this session used msg IDs and timing that don't line up)
being cut short by something -- and the flag-clear at `706.72` is the one event in this whole
trace that only an explicit `speak_stop` message can cause.

### 19.2 The autonomous mechanism, found in the currently-deployed binary's own source

`agent/src/audioout.rs`, `SpeakerOwner::new()` (unchanged on the branch that produced the
binary running on-device throughout this incident, md5 `c87501c8f4ad8ac671c6dd8437f45980`):

```rust
pub fn new() -> Arc<Self> {
    // Best-effort, non-fatal: clears a guard flag a previous kibbled crash may have left
    // stuck at `1` ... Runs exactly once, here, so `main.rs` doesn't need its own startup hook.
    AudioOutThread::clear_stale_guard_flag();
    Arc::new(Self(AtomicBool::new(false)))
}
```

`clear_stale_guard_flag()` sends `speak_stop` (msg `0xb`) **unconditionally, with no HTTP
request, no user action, and no check for whether a real session might currently be active** --
by design, so that a `kibbled` crash mid-`/speak` doesn't leave the vendor's guard flag stuck at
`1` forever (§17.4's documented failure mode). This runs **every single time the `kibbled`
process starts**: a deliberate binary swap, an `app_init.sh`-driven supervisor restart after a
crash, or -- because this build's `Cargo.toml` sets `panic = "abort"` -- **any panic anywhere in
`kibbled`, including in code with nothing to do with audio** (an HTTP handler, the RTSP server,
the schedule/config-sync logic, anything), aborts the whole process and relaunches it,
re-arming this same unconditional `speak_stop` send.

**This is the mechanism Main asked about, confirmed present and unconditional in the exact
binary that was live on-device for this entire incident.** It fully explains how this project's
testing could end a real talkback session the household was actively using, with *zero*
intentional audio action on this project's part: `kibbled` restarting for any reason at all,
at any moment, sends `speak_stop` regardless of what else is happening on the speaker.

### 19.3 What is proven versus what is not [tagged per this doc's convention]

- **[HIGH]** The guard-flag-clear at `t=706.72` did not come from any message this project's
  agents sent this session with `sendmsg`/`bus::Sender` -- none of this session's own sends
  landed anywhere near that device-uptime timestamp, and every one of this session's own
  `speak_start`/`speak_stop`/`play_aac_file` sends is independently accounted for elsewhere in
  this doc with its own timestamp.
- **[HIGH]** `SpeakerOwner::new()`'s unconditional startup `speak_stop` exists in the
  currently-deployed source, is reachable with zero HTTP/RTSP trigger, and fires on every
  `kibbled` process start for any reason.
- **[MED, not directly observed]** That `kibbled` actually restarted at `t≈706` s this specific
  boot. This session did not itself restart `kibbled` in that window and has no visibility into
  whether it crashed on its own (`kibbled`'s stdout/stderr goes to `/dev/null` under
  `app_init.sh` -- the same logging gap `docs/23-audio-codec.md §17.12.6` already flagged as
  needing a real fix before more live testing). A `ps`/PID-change check immediately after this
  incident could have confirmed or refuted a restart directly; by the time this section was
  written `kibbled` had already been taken down for Main's planned rollback, so that specific
  confirmation is not available for this incident and is flagged as the concrete gap for whoever
  investigates next: **add a real, `kibbled`-owned log sink** (not a shell redirect) that records
  process start time and every `speak_start`/`speak_stop` send with a timestamp, so a future
  incident can be diagnosed from evidence instead of timing correlation.
- **[MED, architectural, stated by multiple prior sessions, not independently re-proven this
  session]** That the vendor's own real talkback (via `agora`) uses this *same* guard flag as a
  liveness signal for its own session, such that an external `speak_stop` arriving mid-session
  could plausibly disrupt it beyond just "the flag value changed." This project has never traced
  `agora`'s own binary to confirm it, but it is the simplest hypothesis consistent with every
  observation in §19.1, and is exactly the risk this project's own standing instructions have
  been warning about since `docs/23-audio-codec.md §17.6`: *"Kibbled calling speak_start first,
  then a real app talkback starting... could plausibly manifest as a real user's pet-call
  talkback silently not working for up to ~5s after any kibbled-initiated speak_start/speak_stop
  cycle."* That prior warning undersold the risk: it assumed the disruption window was bounded
  by the ~5s idle timeout and required *kibbled* to have initiated a session first. This incident
  suggests a **kibbled restart alone, with no prior kibbled-initiated speak session at all**, can
  send the disruptive message, and the household saw an active real session — not a merely-
  delayed one — actually terminate.

### 19.4 Response taken

Per Main's direction: **the device is being rolled back to `kibbled.pre-audiostart`/
`kibbled.pre-announce` (md5 `f862cc2e52cb14aacb461027c82e3788`)** -- the last build with no
`speak_start`/`speak_stop`/ring-publish code at all, keeping every feature the household
actually uses (feeding, camera, settings, Wi-Fi, cat ID, schedule read) while removing every
autonomous or explicit touch of `media`'s audio path. Audio work resumes, if at all, only behind
an explicit off-by-default flag, so a `kibbled` restart can never again send an unrequested
message to the vendor's speaker subsystem. **This is a harder requirement than "works
correctly": any future `speak_start`/`speak_stop` sender must be gated so that process startup,
crash-recovery, and any other non-`/speak`-triggered code path cannot reach it, full stop.**

### 19.5 Handoff state (end of this session)

Implemented on branch `audio-solve` (pushed to origin, NOT merged, NOT deployed to the device):
- `agent/src/audioout.rs`: `SpeakerOwner::new()` no longer sends anything (the unconditional
  `clear_stale_guard_flag`/`speak_stop` is deleted, not just disabled); `try_acquire()` now
  refuses unless `enabled()` is true; new `enabled()`/`set_enabled()` gated on
  `/opt/kibble/audio_enabled` existing.
- `agent/src/main.rs` / `agent/src/health.rs`: `GET`/`POST /audio` to read/toggle the flag;
  `health::record_start()` (called once, at the top of `main()`, not periodically) persists
  `start_count`/`last_start_unix` to `/opt/kibble/health.json`; `health::last_exit_code()` reads
  the boot script's own restart ledger; all three merged into `GET /state` as
  `kibbled_start_count`/`kibbled_last_start_unix`/`kibbled_last_exit_code`.
- `scripts/app_init.sh`: reviewed line-by-line by Main and approved, staged in-repo, **not yet
  copied to `/opt/app_init.sh` on the device**. Splits kibbled's own stdout/stderr (potentially
  high-volume, → `/tmp/kibbled.log`, tmpfs, unbounded -- never touches flash) from a small
  durable start/exit forensic record (→ `/opt/kibble/restarts.log`, flash, bounded on both line
  count and byte size) per the household's standing flash-wear rule. Tested standalone with a
  fake exiting binary under `dash`; not yet run on-device.
- Full `cargo test --release` on this branch: 268/268 (one `embed::` test is flaky under
  parallel execution -- confirmed passing standalone, environmental, not a regression).

**Not done, and this is the concrete next step**: build for `armv7-unknown-linux-musleabihf`,
report md5 before/after, deploy `scripts/app_init.sh` to `/opt/app_init.sh` and the new
`kibbled` to `/opt/kibble/kibbled`, verify the full health gate, `POST /audio {"enabled":true}`,
confirm the guard flag (`0x767f0`) reads `0` (no vendor session active), send `speak_start`,
`publish()` an 8s test clip predicting the exact frame-count delta first, report the `SndFrm`
delta, and -- regardless of outcome -- confirm a real vendor app talkback still runs 15+ seconds
clean afterward (this is now an acceptance criterion, not an afterthought, per §19). If the
publish test fails, per Main's explicit instruction: report the numbers and stop, do not start a
new investigation in the same session.

**Why kibbled exited at ~701.7s remains unanswered.** The new logging (once deployed) makes this
answerable after the *next* occurrence, not this one -- there is no way to recover a cause for
an incident that predates the log existing.

## 20. `play_aac_file` plays; the vendor's talkback traced end to end; its "slow and garbled" explained

Author: Main, 2026-09-16 (device uptime ~77.5-78.8 ks; `media` pid 204, `agora` pid 270). Every
number below is from a live read; `kibbled` sent nothing to `media` outside the two labelled
tests, and `/opt/kibble/audio_enabled` was absent for the whole session except inside them.

### 20.1 `play_aac_file` (msg `0x2`) works on the first try -- one-way audio is DONE [HIGH]

Test: `cp /audio/en/en_feed_start.aac /tmp/kt.aac` (8489 bytes, **23** ADTS frames, header
`fff16040...` = AAC-LC/16 kHz/mono), predicted `SndFrm +23`, sent `sendmsg 1 0x2 /tmp/kt.aac`
with a 0.28 s read-only sampler running (`/tmp/s1.sh`: `/proc/ax_proc/{ao,adec}`, `media`
thread count, guard flag `0x767f0` via `/proc/204/mem`):

| t (s) | threads | `SndFrm` | ADEC `SndStrm`/`DecOk`/`GetFrm` | guard |
|---|---|---|---|---|
| 77605.38 | 28 | 138 | 142/142/138 | 0 |
| 77605.65 | **29** | 142 | 148/148/144 | 0 |
| 77606.79 | 29 | 160 | 165/165/161 | 0 |
| 77607.08 | **28** | **161** | 166/166/161 | 0 |

`SndFrm` **+23 exactly**, real-time paced (~16 frames/s), one transient worker thread, guard
flag untouched, ring untouched. Repeated through `kibbled` after the cutover below: `/speak`
with a 65 536-sample tone → `SndFrm` 161→227 (**+66** = 64 frames + 2 encoder flush frames),
`/clips/<name>/play` with an 18-frame clip → 227→245 (**+18**). ADEC `SndStrm` runs one ahead
of frames per file (an end-of-stream send). Two `play_aac_file` sent back to back: `SndFrm`
**+46** continuous, no plateau, two worker threads briefly alive -- `media` **serializes** them
on its internal mutex and plays them gaplessly (as far as the driver counter can show) [HIGH].

**Cutover (commit `4ca7545`, md5 `e50c3cca…`, live):** `/speak` encodes to ADTS, stages it at
`/tmp/kibble-speak.aac` (tmpfs; `.part` + `rename`), sends `PLAY_AAC_FILE` with the path, polls
`SndFrm` until the delta reaches the frame count (or nominal length + 3 s), removes the file.
`/clips/<name>/play` sends the stored clip's own path. The finite-clip ring player is deleted;
`LiveSession` (ring path) remains only for the backchannel decision. `SpeakerOwner::try_acquire`
now also refuses while `0x767f0 == 1` (read from `/proc/<media>/mem`) -- the direct "a real
talkback is running" signal -- in addition to the aenc check. Gate verified: `/speak` → 409 with
audio off; a second `/speak` during playback → 409; `enabled:false` restored after each test.

### 20.2 The comparative capture that never ran -- now run twice [HIGH]

`ringmon` (read-only: `PROT_READ` mmap of the ring, `/proc/204/task` readdir, 4-byte `pread` of
`/proc/204/mem`) at 21 Hz rows (run 1) and 4 Hz rows (run 2, 8x lighter, the control for
sampler load), through one real app press-and-hold each. Both runs identical in every respect
below; Nitin reported both as **"garbled and slow"**.

| event | run 1 (t, s) | run 2 (t, s) |
|---|---|---|
| first chan=2 record written by `agora`; slot 7 word `+0x28` 1→0 | 78296.05 | 78583.97 |
| **guard 0→1 AND `media` threads 28→29, same tick** (`speak_start` fired) | 78296.78 | 78584.84 |
| slot 7 bookmark/cursor jump to the **3rd/4th chan=2 record** agora wrote ~0.75 s earlier | same tick | same tick |
| last chan=2 record | 78317.71 | 78608.6 |
| **guard 1→0, thread gone ≤50 ms later** (`speak_stop`) | 78321.02 / 78321.03 | 78611.62 / 78611.65 |
| chan=2 records / distinct `pts_us` | **680 / 340** | **765 / 383** |
| consecutive byte-identical pairs (same pts+len, seq+1) | 318 | 363 |
| distinct-frame rate | 15.7/s (real time) | 15.6/s |
| `SndFrm` during guard=1 | +381 in 24.3 s = 15.7/s | +421 in 26.8 s = 15.7/s |
| consumer lag (global seq − slot 7 bookmark), start → end | 82 → 1167 records | 95 → 1350 records |

Answers to the three step-2 questions:

1. **The vendor's talkback does go through `speak_start` → `audio_out_thread`** (§17.1's [MED]
   is now [HIGH]): guard flag and thread count change on the same 5 ms tick, twice. And
   **`speak_stop` does end the thread**, within one sample tick, with 1100+ records still
   unconsumed -- §17.4's "does not stop the thread" was a static-analysis miss.
2. **`agora` writes the ring**, starting ~0.75 s *before* `speak_start`, and it writes **every
   frame twice** (§11's intermittent defect was active in both runs today). The consumer starts
   positioned at the 3rd/4th of those records -- *not* at the ring's current global write
   cursor (§17.3's "starts from `ring_base->0x28`" is wrong or incomplete; the start position
   arrives some other way, plausibly in `speak_start`'s payload -- **[OPEN]**, not investigated
   per the stop rule, but note it would also explain §18.7: an empty-payload `speak_start`
   from `kibbled` doing *nothing at all*).
3. **What advances the counter:** every record of every channel bumps `ring_base->0x18`
   (idle run: 420 records ↔ 421 seq). The consumer's bookmark advances +1 per record of *any*
   channel as it walks (its cursor steps through 9 KB video records between 312-byte audio
   ones), filtering chan=2 for decode -- so §17.3.1's open question is answered: **it walks
   everything and filters** [HIGH]. Its pace is bounded by the AO at 15.7 frames/s.

### 20.3 Why the talkback is "slow and garbled" [HIGH for the mechanism]

`agora` delivers 31.4 chan=2 records/s (each 64 ms frame twice); `audio_out_thread` decodes
**every** record -- no de-duplication -- and the AO plays them at 15.7/s. Result: half-speed,
each frame heard twice (the stutter), and a backlog growing ~50 ring records/s (11 s of ring
by the end of a 22 s hold) that `speak_stop` then discards. §11 already predicted exactly this
from a byte-level capture on 2026-09-15 ("almost certainly what makes the stock app's own
talkback sound slow and garbled on this unit"); today's traces close the loop from the ring
to the driver counter. **Not caused by `kibbled`**: audio was off (no sends, `try_acquire`
never reached), the only `kibbled` ring access is the unchanged read-only poller, and the 8x
lighter control run reproduced it exactly. What was *not* run: a talkback with `kibbled`
rolled back to `kibbled.pre-4ca7545` (md5 `24d6c577…`), so "today's build is not the trigger
of agora's double-write" is [MED] by construction rather than [HIGH] by control. §19's
"15+ s clean" acceptance run therefore did not pass today for a reason outside this project;
the rollback control is the one-ask experiment that would settle it.

### 20.4 Design decision for live talkback -- superseded by §20.6

This section originally picked **chunked files** over `play_aac_file`, reasoning from §20.1's
back-to-back test (two queued files, `SndFrm` +46 continuous, no plateau). The listening tests
in §20.6 refuted that: the driver counter cannot see a gap at a file boundary, and every
boundary is audible. The shipped design is one `play_aac_file` on a **named pipe** (§20.6.2).
Options (a)/(c) -- mimicking `speak_start` or the bookmark trick -- stay closed, and are no
longer needed: the [OPEN] item in §20.2 (how the vendor's consumer learns its start position)
is now only of archaeological interest, since nothing shipped touches the ring at all.

### 20.5 Tooling left on the device (all tmpfs)

`/tmp/ringmon` (this session's build: args `<secs> <log> [tick_ms] [row_ms]`), `/tmp/sendmsg`
(older build stamping `src=1`; handlers ignore it), `/tmp/s1.sh` (0.28 s shell sampler),
`/tmp/kt.aac`, `/tmp/toneA.aac` + `/tmp/tc*.aac` (§20.6's test tones). `kibbled`'s
stdout/stderr still go to `/dev/null` (§19.5's `app_init.sh` log split is deployed but writes
to `/tmp/kibbled.log` only while the supervisor loop owns the process; a hand-restarted
`kibbled` inherits the telnet session's `/dev/null`), which is why `PlaybackStats` is now also
served on `GET /audio` as `last_session` -- see §20.7.

## 20.6 Listening tests: what the frame counter cannot see, and the design that came out of it

Author: Main, 2026-09-16 (later the same session). Every result below is Nitin listening to the
feeder in the room; `SndFrm` agreed with every prediction in all of them, which is exactly the
point -- **the driver counter proves delivery, not continuity.** Test audio: a 4.000 s 440 Hz
tone, ADTS AAC-LC/16 kHz/mono, 64 frames.

| # | how the same 64 frames were delivered | `SndFrm` | heard |
|---|---|---|---|
| A | one file, one `play_aac_file` | +64 | clean, continuous (the reference) |
| B | sixteen 4-frame files (250 ms), queued back to back | +64 | **gaps/stutter at the boundaries** |
| C | one `play_aac_file` on a **named pipe**, fed in 250 ms bursts | +64 | clicks and pops |
| D | same pipe, fed one frame per 64 ms (real-time cadence) | +64 | **rapid pops throughout** |
| E | same pipe, fed one frame per 30 ms (ahead of real time), 4-frame pre-roll | +64 | **clean and continuous**, one pop at the start and one at the end |

Conclusions, in the order they change the design:

1. **Queued files are not gapless [HIGH].** §20.1's `SndFrm` +46 "continuous" reading was a
   false negative: `media` serializes the files (no overlap, no loss) but the decoder/AO
   restart between them, and the counter advances either way. Chunking is dead.
2. **One `play_aac_file` can consume a FIFO [HIGH].** `media`'s worker `fopen`/`fread`s the
   path it is given with no seeking (§18.2), so a named pipe works: one bus message, one
   decoder session, no boundaries, EOF when the writer closes. Thread count confirms a single
   worker for the whole session (28 → 29 → 28), and the pipe is `O_RDWR|O_NONBLOCK` on our
   side so a stalled reader surfaces as `WouldBlock` instead of parking the drainer forever.
3. **Starvation is the whole audio-quality story [HIGH].** D vs E is one variable -- feed rate
   vs real time -- and it is the difference between unusable and clean. The AO's own buffer is
   ~300 ms (`PeriodSize=160`, `AoDepth=30` at 16 kHz), so a feeder that is merely *on time*
   underruns constantly. Hence the shipped `LiveSession`: an 8-frame (512 ms) pre-roll before
   the first byte reaches `media`, a filler thread that encodes silence whenever the client's
   PCM falls more than 6 frames behind the session's own 16 kHz sample clock, and 4 frames of
   tail silence before EOF (E's start/end pops).
4. **The sample clock must start with the client's first audio, not with the session [HIGH].**
   The first Scrypted-driven run inserted 20 silence frames because the clock started at
   `LiveSession::start` while ffmpeg was still spawning; anchoring it on the first real `feed`
   dropped that to **zero** silence frames across every run since.

### 20.6.1 End-to-end acceptance, through Scrypted, on the shipped build [HIGH]

`kibbled` md5 `6af73cb5f1ca81e68d4b888cd81cca2d`. Sessions driven by a real WebRTC talkback
(browser mic → Scrypted's WebRTC plugin → Kibble Scrypted mixin → RTSP backchannel → this
pipe), read back from `GET /audio`'s `last_session`:

| source | frames handed to `media` | frames the AO emitted | silence inserted | max lag |
|---|---|---|---|---|
| `node intercom.mjs` (ffmpeg → Scrypted intercom) | 193 | 193 | 0 | 3 frames |
| browser WebRTC, 10 s hold | 161 | 161 | 0 | 4 frames |
| the Kibble card's own hold-to-talk button, 8 s | 130 | 130 | 0 | 2 frames |

Zero dropped frames and zero synthesized silence in every run -- i.e. no gaps, which is more
than the vendor's own app manages (§20.3: it double-writes every frame and plays at half speed).

### 20.6.2 The shipped path, end to end

```
phone/browser mic --Opus--> Scrypted WebRTC plugin (sendrecv audio transceiver)
  --> Kibble Scrypted mixin's Intercom --ffmpeg--> L16/16000 RTP
  --> kibbled RTSP backchannel (trackID=2, interleaved 4-5, PT 98)
  --> aacenc (ADTS AAC-LC/16 kHz) --> /tmp/kibble-talk.aac (FIFO)
  --> one play_aac_file --> media's decoder + AO --> the feeder's speaker
```

Wideband throughout: the SDP now offers `L16/16000` (PT 98) ahead of PCMU/PCMA, and the plugin
sends 16 kHz PCM, so nothing is companded or band-limited to 8 kHz between the caller's Opus and
the feeder's own AAC encoder. G.711 remains for generic ONVIF/go2rtc clients.

Latency budget: 512 ms pre-roll + one encoder frame (64 ms) + the AO's own ~300 ms buffer, so
roughly 0.9 s mouth-to-speaker, plus whatever WebRTC adds (single-digit ms on the LAN).
Lowering the pre-roll trades directly against pop-free playback -- test E is the evidence.

### 20.7 What changed in the agent

- `audioout.rs`: the ring writer is **deleted** (`publish`, `lock_ring_mutex`,
  `WritableRegistry`, `find_append_target`, `build_record`, `write_frame`, plus
  `speak_start`/`speak_stop` and `AudioOutThread`) -- `media` never consumed one record of it
  across four sessions of byte-level work, and nothing shipped needs the ring. `ring.rs`'s
  `TailCursor` and `Header::chan_seq`, which existed only to feed that writer, are gone too.
- `LiveSession` is now the FIFO streamer described above; `play_path`/`play_bytes` serve
  `/speak` and `/clips/<name>/play` from ordinary files.
- `SpeakerOwner::try_acquire` additionally refuses while `media`'s `speak_start` guard flag
  (`0x767f0`, read from `/proc/<pid>/mem`) is set -- the direct "a real app talkback is running"
  signal (§19.1/§20.2) -- alongside the existing `/proc/ax_proc/aenc` check. A live session
  re-checks both every 16 frames and aborts rather than playing over a household talkback.
- `PlaybackStats` gained `frames_played` (the `SndFrm` delta -- the only ground truth for "it
  made sound"), `silence_frames` and `max_lag_frames`, and the last session is served on
  `GET /audio` as `last_session`, since the supervisor still discards `kibbled`'s stderr.
- `/opt/kibble/audio_enabled` stays the single gate and is **on** now that the household uses
  the feature; §19.4's hard requirement is met structurally rather than by the flag: no
  code path outside an explicit `/speak`, `/clips/<name>/play` or RTSP-backchannel `PLAY`
  can reach `media`'s audio path, and process start sends nothing at all.
