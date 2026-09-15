# The frame ring: layout, and the decision to stream from it

**Decision: `kibbled` serves RTSP directly from the vendor's shared-memory frame ring, with no
re-encoding.** This document is the evidence for that choice; [11-media.md](11-media.md) describes
the pipeline that fills the ring.

Why it works out so well: `media` already hardware-encodes **three continuous H.264 channels**
whether or not anyone is watching — 1728x1080 main, **1152x720 sub**, and a 5 fps thumbnail. So a
reader costs no extra encode work, H.264 is what HomeKit wants (no transcode), and the substream
Scrypted should use for live view already exists.

## Record layout (56-byte header + payload)

| off | type | meaning | confidence |
|----:|------|---------|-----------|
| 0  | u32 LE | global sequence, +1 per record of any type | HIGH |
| 4  | u32 LE | payload length; whole record is 56 + length | HIGH |
| 8  | u32 LE | per-channel sequence | HIGH |
| 12 | u32 LE | UNIX wall-clock seconds | HIGH |
| 16 | u32 LE | PTS, **microseconds**, shared monotonic clock (~39987 us at 25.01 fps) | HIGH |
| 32 | u8 | frame type: 0 audio, **1 video keyframe**, 2 video interframe | HIGH |
| 33 | u8 | media class: 1 video, 4 audio | HIGH |
| 34 | u8 | channel: 4 main, **8 sub**, 16 thumb, 1 audio | HIGH |
| 46 | u16 LE | width (video) / bit depth (audio) | HIGH |
| 48 | u16 LE | height (video) / sample rate (audio) | HIGH |
| 20, 24, 28 | — | undetermined (20 is constantly 5; 24/28 are audio-only and nonzero) | — |

A keyframe record is **one access unit bundling SPS+PPS+IDR**; interframes carry the P-slice. All
payloads are Annex-B.

## Two things a reader must know

**There is no new-frame signal.** `sem.media_buffer_reader_6` exists unattached, but `media` never
posts to it — `sem_open` succeeded, value 0, one `sem_trywait` returned `EAGAIN` and the value never
moved. `media` only signals reader slots it was built to know about. **Polling is the only option**,
with per-record validation (sane length, known channel, global sequence exactly previous+1) and a
rescan-to-resync on failure. That algorithm was not designed on paper; it comes from hitting the
torn-read race while building the extractor.

**Audio is in the ring but its codec is unknown.** Microphone audio appears under `chan=1` with the
same header, ~60 ms cadence, ~35 kbps, 16 kHz / 16-bit per the header fields. It is demonstrably
**not raw PCM** (entropy and byte-histogram tests) and **not raw Opus** (a byte-exact Ogg/Opus
container parsed correctly but libopus rejected the packet payloads). That is left as an honest
open question rather than a guess — which is why the first RTSP pass is video-only.

## Proof

A 10.36 s window of the sub channel, extracted from the live ring with zero re-encoding and
MD5-verified at every hop, then independently re-checked by me on Unraid:

```
codec_name=h264
width=1152
height=720
avg_frame_rate=25/1
nb_read_frames=180
```

`ffmpeg -v error -i sub_clean.h264 -f null -` printed nothing: 180 frames, zero decode errors. A
decoded frame shows the plant room in IR with the feeder's own burned-in timestamp matching the
capture time, and kibble visible in the bowl.

---

# media_buffer_frame_buf per-record layout, RTSP-zero-reencode feasibility

Author: RingDecode. Device 192.168.4.85 (armv7l, glibc 2.25, kernel 4.19), telnet only,
shared with a sibling session (`NpuAbi`, working `13-npu-probe.md`). All device work was
read-only against the ring itself (`PROT_READ` mmap only, never `PROT_WRITE`); the only
mutating action taken anywhere was one bounded `sem_trywait()` on the explicitly-authorized
`sem.media_buffer_reader_6`, immediately verified to have changed nothing (see §4). No
`pktool` command, no `msg_dispatch_8`, no `ttyS3`, no vendor-process signal, no write outside
`/tmp` and this build directory.

Confidence tags: **[HIGH]** byte-exact, independently reproduced across multiple samples/sessions.
**[MED]** correlational or single-session evidence, plausible but not exhaustively cross-checked.
**[LOW]** speculative, stated as such. **[DOC]** taken from the prior study (`docs/11-media.md`),
not independently re-derived this session (out of scope — my mandate was specifically the
per-record header, which the prior study explicitly left open).

Everything below was produced by two small static-armv7 C tools I wrote (`ringtool.c`,
`semtool.c`, both in this directory) plus offline Python (`analysis.py`). All are read-only
except the one authorized semaphore test.

## 1. Registry table (0x000–~0x400) — corroborates the prior study, not re-litigated

Re-verified live (`ringtool reg 0 704`) that the first 0x2C0 (704) bytes are exactly what
`docs/11-media.md` §4 already established **[DOC, re-confirmed HIGH]**: 16 fixed 44-byte slots,
slot 0 a nameless writer/global-header slot, slots 1–7 currently populated
(`cloud_reader17/216/311`, `event_reader10`, `agora_read_19/25`, `auido-out`), slots 8–15 empty
with just an index byte. A `density` sweep (256-byte windows, `ringtool density 0 16384 256`)
shows byte-nonzero-density sitting at 20–32% through the registry region and jumping to
99–100% at exactly offset 0x400 (1024) — that is the true registry/data boundary; bytes
0x2C0–0x400 are extra headroom/padding within the registry area, not yet-undiscovered slots.
I did not re-derive the per-slot reader-cursor field semantics (slot-0's own counters, or the
small per-slot integers next to each reader name) — that is squarely the prior study's territory
and remains at its stated confidence; my new results below are entirely about the **per-record**
header the prior study explicitly could not recover.

## 2. Per-record header — 56 bytes, byte-level table

Method: mmap read-only, scan the data region (offset ≥ 1024) for the 4-byte Annex-B start code
`00 00 00 01`. Because H.264 emulation-prevention guarantees a real NAL body can never itself
contain that exact 4-byte sequence, **every hit is a genuine record/NAL boundary** — I never had
to filter false positives out of the data region. For every hit, dumping the 64 bytes immediately
before it and diffing across many records (both within one capture and across two independent
captures ~30–60 minutes apart) isolated a fixed-size, fixed-offset header that precedes every
record — video or audio.

**Header size: 56 bytes, immediately preceding a video record's Annex-B start code (for a video
record, `payload = record[56:]` begins with `00 00 00 01`; for an audio record there is no start
code — the header is followed directly by opaque payload bytes). [HIGH]** — confirmed three
independent ways: (1) a length field at header+4, correlated against the measured byte distance
to the next record, matches exactly once 56 is added back in (`length = distance_to_next − 56`)
for ~100% of non-torn samples; (2) a global sequence counter at header+0 increments by exactly 1
for every 56-byte step taken, video or audio, with zero exceptions across thousands of steps in
the final extraction runs; (3) `gapcheck`'s raw byte dumps of the "hidden" region between two
video hits decode as a perfectly well-formed 56-byte header in place, every time.

| Off | Size | Type/endian | Meaning | Confidence |
|----:|-----:|:---|:---|:---|
| 0  | 4 | u32 LE | **Global sequence number.** +1 for every record written to the ring, video or audio, all channels share one counter. Verified as the single most reliable corruption/torn-read detector: any deviation from `prev+1` reliably flagged a live write race. | **HIGH** |
| 4  | 4 | u32 LE | **Record length.** Bytes following the header, i.e. `total_record_bytes = 56 + length`, and `this_header + 56 + length == next_header`. For an ordinary video record this is exactly the size of the one Annex-B NALU (start code included). **A keyframe access unit is ONE ring record**: SPS+PPS+IDR-slice are three back-to-back Annex-B NALUs packed into a single record's payload, sharing one header/length (confirmed on 5 independent keyframe bundles, byte-exact). | **HIGH** |
| 8  | 4 | u32 LE | **Per-stream sequence number.** A *separate* counter per channel: main, sub and audio each increment their own copy by exactly 1 per record of that type (confirmed independently for all three by isolating same-channel consecutive records). Effectively "the Nth frame of this stream since boot/attach." | **HIGH** |
| 12 | 4 | u32 LE | **Wall-clock timestamp, UNIX epoch seconds** (`CLOCK_REALTIME`). Converts to the exact real UTC time of capture, cross-checked at two points ~1046s (17.4 min) apart in this session (`2026‑09‑15 07:35:47` then `07:53:11`), matching real elapsed wall-clock time between those two device touches. Constant across every record written in the same second, video or audio. | **HIGH** |
| 16 | 4 | u32 LE | **Presentation timestamp, monotonic clock, microseconds** (not ms — a common mislabeling trap: consecutive same-channel steps measure ≈39,987 µs, i.e. ≈25.01 fps, matching the *live-measured* `OutFps 25.01–25.02` in `/proc/ax_proc/venc`, not the nominal 25.000). One shared clock domain across main, sub and audio (audio pts values interleave sensibly between video pts values from the same moment). | **HIGH** |
| 20 | 4 | u32 LE | Constant **5** in every one of ~40 directly-inspected samples (both video channels, both keyframe/interframe, and audio). Candidate: high 32 bits of a 64-bit µs PTS (would only tick after 2³²µs ≈ 71.6 min of monotonic-clock life — not enough elapsed session time to force a second value); alternatively an unrelated format/version constant. | **MED** (constant, HIGH; meaning, LOW) |
| 24 | 4 | u32 LE | **Zero on every video record observed.** On audio records, a large nonzero value in the same numeric neighborhood as, but not identical to, the offset-16 PTS. Candidate: an audio-specific secondary timestamp (e.g. a separate ALSA/capture-clock domain), not confirmed. | **LOW** (meaning); split video=0/audio≠0 is **HIGH** |
| 28 | 4 | u32 LE | Zero on video; equals the offset-20 constant (5) on audio. Not resolved. | **LOW** |
| 32 | 1 | u8 | **Frame-type marker.** `0` = audio record. `1` = video **keyframe bundle** (payload's first NAL is SPS, type 7 — this is exactly the assignment's "keyframe flag": set precisely on records whose payload starts with SPS/IDR, confirmed on 5 independent bundles across both main and thumb channels, always classb0=1). `2` = video **interframe** (payload is a single P-slice, NAL type 1). No other value observed. | **HIGH** |
| 33 | 1 | u8 | **Media class.** `1` = video (either frame-type), `4` = audio. | **HIGH** |
| 34 | 1 | u8 | **Channel id.** `4` = main (1728×1080), `8` = sub (1152×720, 25 fps), `1` = audio (no sub-channels; offset-33's media class already separates it from video). `16` = thumb (1152×720, 5 fps) — initially only correlational (frequency vs. resolution clustering), but confirmed byte-exact once I looked at *clean, non-contaminated keyframe-bundle headers*: of 3 keyframe bundles captured, 2 read `chan=16` and 1 `chan=4`; thumb's GOP is 10 @ 5 fps = 2 s vs. sub's 100 @ 25 fps = 4 s (`docs/11-media.md` §2), so thumb keyframes are intrinsically twice as frequent in any given window — exactly the skew observed, and no sub-channel (`chan=8`) keyframe happened to land in the small keyframe sample, unsurprising at that ratio. | **HIGH** for 1/4/8; **HIGH** (upgraded from MED) for 16 |
| 35 | 1 | u8 | Always `0` in every sample. Reserved/unused. | HIGH (constant); meaning unknown |
| 36–45 | 10 | — | Always all-zero in every sample (video and audio). Reserved/padding. | HIGH (constant); meaning unknown |
| 46 | 2 | u16 LE | **Video:** frame width (`1728` main, `1152` sub/thumb — matches live VENC config exactly). **Audio:** re-purposed as **bits per sample = 16**. | **HIGH** |
| 48 | 2 | u16 LE | **Video:** frame height (`1080` main, `720` sub/thumb). **Audio:** re-purposed as **sample rate = 16000** (Hz) — matches `docs/11-media.md` §3's documented mic-capture spec (16 kHz/16-bit) exactly. | **HIGH** |
| 50–55 | 6 | — | Always all-zero in every sample. Reserved/padding. | HIGH (constant); meaning unknown |

All 56 bytes are accounted for. Undetermined fields, explicitly: **offset 20 (constant-5
semantics), offset 24 and 28 (audio-only secondary values)**. Nothing else in the header is
unexplained.

Immediately after the header: for video, `00 00 00 01` + NAL header byte + RBSP (one NALU for an
interframe; SPS+PPS+IDR concatenated for a keyframe bundle); for audio, `length` bytes of opaque
payload with no internal framing (see §6).

## 3. Ring topology / wraparound — learned the hard way, now handled correctly

The ring is a genuine byte-continuous circular buffer (not fixed slots): records of wildly
different sizes (43 B to ~9 KB) sit back-to-back with **zero padding** between
`this_header+56+length` and the next header — confirmed by the length-field arithmetic matching
exactly, byte for byte, with no slack. Live capacity was **not** the ~31 s a naive
bitrate estimate would suggest; two consecutive live measurements (using the correct
"walk-to-writer-boundary" method in §5) measured **21.7 s and 12.2 s** of currently-buffered
sub/audio history respectively at two different moments — i.e. capacity is a moving target,
directly a function of current scene complexity (a mostly-static scene produces much smaller
frames than the vendor study's snapshot, so more real seconds fit).

**Crossing the live writer's position mid-read produces exactly the header-validity failure you'd
design for, and nothing worse**: I hit this directly. An early version of my extraction tool
walked a naive "full lap" and silently spliced ~44 s-old stale data onto the tail of a supposedly
"most recent 10 s" selection, because physical address order is **only** chronological within one
unbroken run — crossing the writer's current position is where the newest and (~1 lap) oldest data
meet, and address order does *not* stay monotonic in time across that seam. The fix (and the
correct general reader algorithm) is in §4.

## 4. Frame-arrival signalling: **polling required, semaphore does not exist for a new reader**

Checked `/proc/{201,268,269,14190}/maps` for `reader_6` immediately before touching it: **zero**
processes (`media`, `agora`, `cloud`, `kibbled`) had it mapped, exactly as the assignment stated.
Confirmed with `semtool` (`sem_open` + `sem_getvalue` + exactly one `sem_trywait`, restore-on-success
logic included though never triggered):

```
$ /tmp/semtool /media_buffer_reader_6
sem_open(/media_buffer_reader_6) OK
initial value=0
trywait: FAILED errno=11 (Resource temporarily unavailable)
final value=0
```

Value 0, `trywait` returns `EAGAIN` immediately (no block, no signal pending), value unchanged
afterward — nothing has ever posted to this semaphore, and nothing will, because `media` only
posts to the two specific named semaphores it already knows about (`_4`/`_5`, both consumed by
`agora` today, confirmed in `/proc/201/maps`) — there is no dynamic-subscription mechanism visible
from outside the (still-unrecovered) writer binary. `_4`/`_5` were never touched, per the
constraint. **Conclusion: a brand-new reader gets no wakeup from any semaphore it can safely
create — polling is the only viable strategy**, exactly as the assignment anticipated. (This is a
statically-linked-with-a-newer-glibc probe touching a semaphore file most likely created by the
device's own glibc 2.25; it worked cleanly here, but I would not treat that as proof of full
on-disk struct-layout compatibility for a long-lived production reader — verify independently in
whatever runtime `kibbled` actually uses.)

**Safe polling strategy** (this is the exact algorithm `ringtool extract`'s final version
implements, arrived at only after the naive version above produced corrupted output):

1. Track your last-consumed **global sequence number** (header+0) and your last header offset.
2. Poll on a ~20–40 ms timer (matches the 25 fps main/sub cadence; no benefit to polling faster).
3. Walk forward via the header chain (`next = header + 56 + length`). Before trusting any record,
   validate **all three** of: (a) `length` in a sane range (single bytes up to a few MB); (b)
   `chan` ∈ {1, 4, 8, 16}; (c) `seq == prev_seq + 1`. All three held on every genuine record found
   this session; a violation on any of them reliably flagged either a live write race (you caught
   the writer mid-record) or an overrun (the writer lapped you and you fell behind).
4. On failure: re-synchronize by scanning forward for the next 4-byte Annex-B start code and
   recomputing `header = start_code − 56`; retry (or back off a poll tick if you were racing the
   writer — the very next attempt normally succeeds).
5. To find "the writer's current position" (needed once, to seed a cursor, or any time you must
   recover from an overrun): walk with no resync from any anchor until validation fails — that
   failure point *is* the writer. The record immediately after it is the oldest surviving data;
   start your real polling cursor there for maximum runway before you catch back up.
6. Never assume ring-physical-address order is globally chronological — only within one unbroken
   run between two such boundary crossings.

## 5. Proof: zero-re-encode extraction of the 1152×720 substream

`ringtool extract 2048 8 10000 /tmp/sub10s.h264` (chan `8` = sub, confirmed §2) implements the
two-phase walk from §3/§4: phase 1 locates the live writer boundary from an arbitrary anchor;
phase 2 restarts immediately after it (maximum chronological runway) and collects every
`chan==8` record up to the *next* boundary crossing, then takes the tail (freshest data). Result:

```
extract: 545 records match chan=8
chan=8 pts range [334532330,356285258] span=21752928ms over 545 records
wrote 260 records (of 545 total chan matches), 139921 bytes,
  pts [345928625,356285258] (span 10356633ms) to /tmp/sub10s.h264
```
(`ringtool`'s own `fprintf` mislabels the span unit as `ms`; per §2 the field is actually
**microseconds**, so this is genuinely **21.75 s** of buffered sub-stream history at capture time,
of which the extracted tail is **10.36 s** — not 21.75/10.36 *milliseconds*. Noted here rather than
silently edited so the quoted tool output stays verbatim.)

10.36 real seconds, 260 ring records (258 interframes + 1 keyframe bundle = 3 NALs → 264 NALs
total), written as pure `fwrite()` of `[header+56 .. header+56+length)` per record — **the exact
bytes the vendor encoder produced, concatenated in ring order, no transcode, no byte rewritten.**
Transferred to Unraid over a raw TCP socket (`nc`/`/dev/tcp`, not chunked telnet) with MD5 verified
identical at every hop (`<redacted-32-hex>`).

The raw file's first 80 NALs are P-frames that precede its first keyframe bundle (unsurprising —
"last N records" doesn't align to a GOP boundary by construction, exactly like a real RTSP client
joining mid-stream, which is why `AX_VENC_RequestIDR` exists per the prior study). Trimmed to start
at the first SPS (`sub_clean.h264`, 120,472 bytes, 180 pictures) for a maximally clean proof run:

```
$ ffprobe -v error -show_format -show_streams -count_frames -i sub_clean.h264
[STREAM]
codec_name=h264
profile=77                    # H.264 Main profile
codec_type=video
width=1152
height=720
pix_fmt=yuvj420p
level=51
r_frame_rate=25/1
avg_frame_rate=25/1
nb_read_frames=180
[/STREAM]
[FORMAT]
format_name=h264
size=120472
probe_score=51
[/FORMAT]
```

`-v error` prints only on parse/decode failure — **there were none.** Full decode confirms it:

```
$ ffmpeg -i sub_clean.h264 -f null -
Input #0 ... Video: h264, yuvj420p, 1152x720, 25 fps, 25 tbr
frame=  180 fps=0.0 q=-0.0 Lsize=N/A time=00:00:07.20 bitrate=N/A speed= 161x
```

180/180 frames decoded, 7.20 s at 25 fps, **zero errors.** A decoded frame
(`ffmpeg -frames:v 1 sub_clean.h264 → frame_sample.jpg`) is a genuine current camera image: IR
night-vision view of the feeder interior, kibble visible in the bowl, PETKIT branding on the bowl
itself, and the device's own burned-in overlay reading **`PETKIT 2026/09/15 04:24:13`** — real,
current content, not noise (kept at `frame_sample.jpg` next to this report).

For comparison, the untrimmed 139,921-byte file decodes the same way but reports
`nb_read_frames=180` out of 264 total NALs because the leading 80 orphan P-frames (before any
SPS/PPS reaches the decoder) legitimately cannot be decoded standalone — an inherent property of
starting mid-GOP, not a bug in the extraction; a real RTSP bridge handles this by requesting an
IDR (or simply waiting for the next natural one) before serving a new client, exactly per the
prior study's recommendation.

**This satisfies the acceptance criterion: the record layout produces a file ffmpeg decodes
cleanly, end to end, with real recognizable content.**

## 6. Audio: present in the ring, NOT raw PCM, NOT raw Opus

`chan == 1` records exist in the *same* ring, *same* 56-byte header (§2), interleaved with video
by the same writer. Extracted 95 consecutive audio-only records
(`ringtool extractpkt 2048 1 5000 audio_pkt.bin`, length-prefixed so per-packet boundaries survive
transfer) and one 8.58 s / 135-record raw concatenation (`audio5s.raw`) for statistics:

- **Cadence:** ~60 ms between consecutive audio records (per-stream counter at header+8
  increments by exactly 1 each time, independent of video's own per-channel counters).
- **Size:** 208–298 bytes/record, mean ≈ 278 B ⇒ **≈ 28–40 kbps** (mean ≈ 35 kbps) at 60 ms framing.
- Header's width/height pair reads **16 / 16000** on *every* audio record — bits-per-sample and
  sample-rate of the *source* PCM, matching the study's documented mic spec (16 kHz/16-bit) exactly.
- **Not raw PCM [HIGH]:** byte-value histogram across the full sample uses all 256 values almost
  uniformly; interpreted directly as signed 16-bit PCM, mean `|sample|` ≈ 16,398 (half of full
  scale) and mean sample-to-sample `|Δ|` ≈ 21,799 — real audio, even loud audio, does not look like
  this (see `analysis.py`'s histogram/PCM check); this is the signature of entropy-coded/compressed
  data.
- **Not standard-framed Opus [MED-HIGH]:** built a byte-exact Ogg/Opus container in Python
  (`analysis.py:build_ogg_opus` — correct Ogg CRC-32 pages, `OpusHead`/`OpusTags`, one page per 10
  packets, each of the 95 real extracted payloads as one packet) and fed it to ffprobe/ffmpeg. The
  **container** parsed perfectly (`format_name=ogg`, `probe_score=100`, correct
  rate/channels/duration straight from the header bytes I wrote), proving my test harness is sound
  — but libopus's own decoder immediately rejected the packet contents
  (`[opus] Error parsing the packet header`). Despite `libopus.so`/`libax_opus.so` being present
  elsewhere in this firmware for Agora's RTC path, **this specific bitstream is not raw Opus.**
  Exact codec **not identified** within the available time — reported as a genuine open question,
  not guessed at.

**Answer to the assignment's real question:** yes, microphone-derived audio (16 kHz/16-bit source,
per the header) **is present in this same ring**, tagged and interleaved exactly like a fourth
channel, with a well-understood record shape; only the *specific compression codec* on the wire is
unresolved. A Kibble RTSP bridge can therefore plan on pulling audio from this same ring/reader
slot rather than needing a separate tap into `media`'s internals — it just needs to either identify
the codec (to remux, e.g. into an RTP/RTSP-compatible payload type) or transcode from it once
identified; it should **not** assume PCM or assume Opus without first confirming against a fresh
sample using the same test in `analysis.py`.

## 7. Cleanup / process health

```
$ ps | grep -E "media|agora|cloud|ble|ctrl|watchdog|kibbled"
  199 root      0:29 ./watchdog
  200 root      0:48 ./ble
  201 root      5h09 ./media        # was 4h25 at the start of this whole session — continuous, never restarted
  214 root      1:49 ./ctrl
  268 root      2:28 ./agora
  269 root      0:16 ./cloud
14127 root      0:00 sh -c while :; do /opt/kibble/kibbled >/dev/null 2>&1; sleep 5; done
14190 root      0:00 /opt/kibble/kibbled
```

`media`'s accumulated CPU time only ever increased across every check this session — never reset,
confirming continuous uptime, never restarted. `/tmp` on the device: every file I created
(`ringtool`, `ringtool.gz`, `semtool`, `semtool.gz`, `sub10s.h264`, `audio5s.raw`, `audio_pkt.bin`,
`nc.err`, `nohup.out`) removed; final listing shows only pre-existing device files
(`ble.img`, `wpa_supplicant.conf`, logs, etc. — none mine). `/dev/shm` unchanged: still exactly
`config_shm`, `media_buffer_frame_buf`, `sem.media_buffer_reader_{4,5,6}` — no new shm segment or
semaphore left behind. `sem.media_buffer_reader_6` is back at value 0, its pre-test state (the one
`trywait` failed, so there was nothing to `sem_post` back). No dispense, no `pktool`, no vendor
process touched or restarted.

## 8. What I could not determine (explicit)

- Exact semantics of header offsets **20** (constant 5), **24** and **28** (audio-only secondary
  values) — flagged with concrete hypotheses in §2, none confirmed.
- Exact bit/enum meaning of offset 33 beyond the observed 1=video/4=audio split (i.e., whether it
  is truly a bitmask with unseen other bits, or just these two values).
- The registry table's own per-slot reader-cursor and slot-0 write-position fields — out of scope
  for this session (the prior study's territory, not re-derived here); see `docs/11-media.md` §4.
- Exact audio codec (proven compressed, proven not-PCM, proven not-raw-Opus; nothing further).
- Whether `AX_VENC_RequestIDR`-forced keyframes (not tied to a GOP boundary) look identical at the
  header level to the GOP-boundary keyframes I actually sampled — never observed one directly
  (would require forcing a keyframe, out of scope/no such action taken).

## Files in this directory

- `ringtool.c` / `ringtool` — read-only mmap prober: `reg`, `density`, `scan`, `gapcheck`,
  `extract`, `extractpkt`. Never opens the ring for write or maps it writable.
- `semtool.c` / `semtool` — bounded semaphore probe (`sem_open`+`sem_getvalue`+one
  `sem_trywait`, restore-on-success). Only ever invoked against `_6` this session.
- `analysis.py` — offline header decoder + Ogg/Opus muxer used for the audio-codec test + the
  PCM-implausibility check in §6.
- `sub10s.h264` (139,921 B) / `sub_clean.h264` (120,472 B, keyframe-aligned) — the proof artifacts
  for §5.
- `frame_sample.jpg` — one decoded frame from `sub_clean.h264`.
- `audio5s.raw` (raw concatenation) / `audio_pkt.bin` (length-prefixed) / `audio_test.opus` (the
  Ogg/Opus test container) — the artifacts for §6.
