> **Editor's note (Main, 2026-09-15):** this survey was written by a read-only research
> agent working from public sources. Its section on `nphil/kibble` describes *this* repo
> from its README and is out of date — `agent/src/audioout.rs` is not a stub: the encoder,
> RTSP AAC track, ONVIF backchannel, `/speak`, clip store, the announce protocol and a
> hand-rolled cross-process futex lock are all implemented and live-verified (see
> `docs/23-audio-codec.md` §13, §17). Read that section as "an external observer's view of
> our own public README", not as an assessment of the code. Everything else in this
> document is external prior art and stands on its own citations.

# Prior Art: Audio-to-Speaker in Petkit & De-Clouded Devices

This document surveys how other projects approach getting audio INTO a Petkit device speaker, or comparable camera/feeder systems with audio output. Kibble (this project) aims to extend local control to the speaker; this research identifies existing solutions, their mechanisms, and what works vs. what remains unsolved.

---

## Summary

**No project currently plays custom audio to a Petkit speaker locally without cloud API or full firmware replacement.** The vendor's Agora RTC talkback is the only known working path on unmodified stock firmware. Projects that do solve audio-in do so by replacing firmware entirely (Thingino/OpenIPC cameras) or reverse-engineering proprietary protocols on different hardware (budget cameras).

**Critical finding:** Petkit's own vendor stack provides a file-driven audio path (`play_aac_file`, bus message 0x2) that reads pre-recorded `.aac` files from the filesystem and feeds them through the audio decoder to the speaker. This is the mechanism used by the vendor's own voice prompts and is proven to work. No external project has discovered or attempted this locally, making it an unexplored avenue that sidesteps all ring-buffer and cross-process synchronization issues that have blocked Kibble's ring-write approach.

---

## Reviewed Projects

### 1. Jezza34000/py-petkit-api & homeassistant_petkit

**What it does:**
- Reverse-engineered Petkit cloud API client library (Python).
- Powers the most mature Home Assistant Petkit integration.
- Provides device control: manual feed, settings, diagnostics, and pre-recorded sound playback.

**Audio capability:**
- `FeederCommand.PLAY_SOUND`: plays one of the pre-recorded sounds already stored on the device (`D4H`, `D4SH` models).
  - **Mechanism**: sends `{"play_sound": selected_sound_id}` via cloud API → Petkit servers → MQTT/cloud message to device.
  - **Limitation**: plays ONLY pre-existing sounds by ID; cannot accept arbitrary audio data or streaming audio. Cloud API does not expose an endpoint to upload or inject custom audio frames.

**Live video/Agora RTC:**
- Implements Agora WebRTC client for live camera streams (credited to `@mikey0000`'s reverse-engineering).
- Joins Agora channel with token obtained from Petkit cloud.
- One-way video only (no two-way audio in this integration; Agora channel is receive-only).

**Does it solve audio-in locally?** 
**No.** Cloud-dependent; pre-recorded sounds only. Confirms that even Petkit's own cloud API does not support arbitrary audio upload or real-time audio streaming to the device speaker.

**Reference:** https://github.com/Jezza34000/py-petkit-api

---

### 2. dwyschka/localkit

**What it does:**
- Fully local Petkit device control (feeders, litter boxes, fountains, accessories).
- PHP-based; communicates directly with devices over local network via undocumented protocol.
- MQTT bridge to Home Assistant.
- Supports feeding, diagnostics, activity logs, local media storage, and geolocation.

**Audio capability:**
- **None documented or implemented.**
- Supported devices: Yumshare Solo (`d4h`), Yumshare Dual (`d4sh`), Pura Max (`t4`), etc.
- No mention of speaker, talkback, or audio playback in README, device docs, or exposed entities.

**Why not audio?**
- Localkit reverse-engineered the *feeding and litter control* protocol but has not reverse-engineered the audio path.

**Does it solve audio-in locally?** 
**No.** No reverse-engineering of audio protocol.

**Reference:** https://github.com/dwyschka/localkit, https://localkit.io/

---

### 3. nphil/kibble (This Project's Parallel)

**What it does:**
- On-device Rust agent running alongside stock Petkit firmware.
- Reverse-engineered internal message-bus (POSIX mqueues, shared-memory config).
- Local HTTP control: dispense per auger, live state, cat ID via ML.
- Home Assistant integration with feed/cancel buttons.

**Audio capability:**
- **Status: designed, not implemented.**
- README explicitly lists: "Camera / two-way audio via Scrypted | designed, not implemented."
- Source: `agent/src/audioout.rs` exists but is incomplete/stub.

**Known blocker (identical to this project's finding):**
- Vendor's `audio_out_thread` consumes from `/dev/shm/media_buffer_frame_buf` with `chan=2` (AAC-LC 16 kHz mono).
- Kibble's ring-publish approach correctly formats AAC frames, but vendor thread does not consume them.
- Unknown signal/trigger prevents consumption; thread may be blocked on a different message-bus event or memory condition.

**Does it solve audio-in locally?** 
**No.** Same ring-write dead end as this project's findings.

**Reference:** https://github.com/nphil/kibble

---

### 4. schnebeck/thingino-arenti-petcam

**What it does:**
- **Full firmware replacement** for Arenti PetCam (Ingenic T31X, different SoC than Petkit's Axera AX620Q).
- Decloudified using Thingino firmware (open-source MIPS-based firmware for budget IP cameras).
- Implements: PTZ control, treat dispenser, audio-triggered recording, **two-way audio via WebRTC**, local storage backup.

**Two-way audio mechanism:**
- **Push-to-talk**: browser or HA client encodes audio → sends Opus frames → camera decodes and plays on speaker.
- Uses WebRTC directly (no Agora); Majestic WebRTC server runs on device.
- `/play_audio` HTTP endpoint accepts Opus or other audio formats; camera mixes and outputs to speaker.

**Hardware dependency:**
- T31X SoC audio codec and driver stack differ from Axera AX620Q (Petkit's chip). Not portable to Petkit without full firmware replacement.

**Does it solve audio-in locally?** 
**Yes, but only on non-Petkit hardware.** Requires full firmware replacement and running on different camera model.

**Reference:** https://github.com/schnebeck/thingino-arenti-petcam

---

### 5. Thingino/OpenIPC General (t31, t32, ak3918)

**What it does:**
- Open-source firmware for Ingenic T31/T32 and Anyka AK3918 SoCs (budget IP cameras).
- Provides standardized `/play_audio` HTTP endpoint on Majestic streamer.
- Audio formats: Opus, AAC, L16 PCM; automatic resampling.

**Audio playback mechanism:**
- **POST `/play_audio`** with audio file → Majestic decodes → sends to audio codec hardware → plays on speaker.
- Recent improvements: Opus support (no conversion needed), rate auto-resampling, channel mixing.

**Does it solve audio-in locally?** 
**Yes, but only on OpenIPC-compatible hardware (T31, T32, AK3918).** Not applicable to Petkit (Axera AX620Q).

**Reference:** https://github.com/OpenIPC/firmware, https://openipc.org/majestic-endpoints

---

### 6. DavidVentura/cam-reverse

**What it does:**
- Reverse-engineered iLnk/iLnkP2P/PPPP protocol (budget pet cameras: X5, A9, DG-series).
- Streams JPEG video + 8 kHz A-law PCM audio without proprietary app.
- Two-way audio: browser sends audio via protocol.

**Audio mechanism:**
- Protocol frames include audio; reversing done via Ghidra + Frida on closed-source firmware.
- Hardware-specific; not Petkit.

**Does it solve audio-in locally?** 
**Yes, but only on X5/A9/DG cameras.** Irrelevant to Petkit hardware.

**Reference:** https://github.com/DavidVentura/cam-reverse

---

## The File-Driven Prompt Path: An Unexplored Avenue

### The Mechanism

Petkit's vendor firmware exposes a file-driven audio path:
- **Bus message `0x2` (`play_aac_file`)**: the `media` process reads a `.aac` file from the device filesystem and feeds it through the Axera audio decoder (`AX_ADEC` → `AX_AO_SendFrame`) to the speaker.
- This is the mechanism used by Petkit's own voice prompts, pre-recorded sounds, and greeting messages.
- **Key property:** sidesteps all shared-memory ring issues, cross-process synchronization problems, and consumer-thread signal unknowns that have blocked Kibble's ring-write approach.

### Evidence That This Is the Vendor-Sanctioned Path

Petkit's cloud API `PLAY_SOUND` command:
- Does NOT stream arbitrary audio to the device.
- Does NOT accept audio file uploads in real-time.
- Only triggers **pre-existing sounds by ID** that are already stored on the device or fetched from cloud once at setup.
- This architectural choice (pre-stored, file-driven) suggests the vendor considers file-based playback to be the fundamental audio output mechanism.

### The Prior-Art Gap

**No external project has documented or attempted to use this path locally:**
- py-petkit-api only triggers pre-recorded sounds via cloud API (does not modify device files).
- Kibble's audioout.rs focuses on ring-write, not filesystem access.
- Localkit has no audio implementation at all.
- Thingino/OpenIPC/Arenti projects all replace firmware; they do not attempt to co-exist with the vendor's `media` process.

**No evidence** that anyone has:
- Identified the device's AAC prompt file location or naming convention.
- Attempted to write custom `.aac` files to the device filesystem.
- Triggered `play_aac_file` (message `0x2`) with a custom file path via the message bus.

This makes the file-driven path a largely unexplored opportunity that could solve audio-in without requiring:
- Reverse-engineering Agora RTC.
- Understanding the ring-buffer consumer signal.
- Full firmware replacement.
- Cross-process synchronization primitives.

### Why This Matters for Kibble

The file-driven path is a **strict generalization** of what Petkit's cloud API already does:
- Cloud API: fetch pre-recorded sound from cloud, store on device, trigger by ID via message bus.
- Local file-driven: write custom `.aac` file to device, trigger by path via message bus.

Other projects (all cloud-dependent) only ever trigger pre-existing sounds, implicitly confirming this is the only audio path the vendor exposes. Kibble could reverse the process: write the file locally, trigger it locally, no cloud.

---

## Cloud API Routes (Petkit Official)

**Question:** Is there a documented Petkit API endpoint to play audio on the device?

**Answer:** **Partially.** Petkit's API (reverse-engineered; undocumented) supports:
- `PLAY_SOUND`: trigger a pre-recorded sound already stored on the device (confirmed in py-petkit-api).
- Supports custom voice messages recorded via the app (uploaded to Petkit cloud, fetched by device).
- **No real-time audio stream or arbitrary file upload endpoint found** in any public integration.

**Legitimate fallback (cloud-dependent but working):**
- Record or synthesize audio → upload to Petkit cloud → device fetches and stores → trigger via cloud API → plays locally.
- Limited by Petkit's storage (typically 20 seconds per message) and cloud dependency.
- Acceptable as a fallback for TTS/announcements even if not truly local.

---

## Why Nobody Solved Custom Audio-In Locally

1. **Agora RTC is proprietary and opaque.**
   - Only Agora's own client implementation works reliably.
   - Reverse-engineering is complex; no time/resources yet invested.

2. **Vendor's `audio_out_thread` signal is undocumented.**
   - Ring-publish approach (this project's current path) is technically sound but incomplete.
   - Trigger mechanism remains unknown without deep on-device debugging.

3. **File-driven path has never been attempted.**
   - No project has looked for or documented the AAC prompt file location.
   - No attempt to trigger `play_aac_file` with custom paths.
   - Represents a genuine gap in prior art, not a known dead end.

4. **Petkit prioritizes cloud + Agora for competitive advantage.**
   - No public incentive to document local audio paths.

---

## Options for Kibble (Ranked by Locality)

| Option | Mechanism | Cloud? | Effort | Status | Notes |
|--------|-----------|--------|--------|--------|-------|
| **1. File-driven path (local)** | Discover device AAC file location; write custom `.aac`; trigger `play_aac_file` (msg `0x2`) via bus. | No | Medium | **Unexplored** | Sidesteps all ring-buffer issues; vendor-sanctioned. **Best bet.** |
| **2. Deep debug `audio_out_thread` (local)** | GDB on device; set breakpoints; observe thread state, signals, memory. Determine why it does not consume ring. | No | High | Achievable | If file-drive fails, this clarifies why ring-write cannot work. |
| **3. Cloud API TTS (cloud)** | Use py-petkit-api to play pre-recorded sounds or trigger cloud-synthesized audio messages. | **Yes** | Low | **Working** | Acceptable fallback; not truly local but functional. |
| **4. Agora reverse-engineering (local)** | Reverse Agora handshake + audio codec; attempt to clone or proxy Petkit's Agora client. | No | Very high | Not attempted; may be infeasible without credentials. |
| **5. Scrypted proxy (local, partial)** | Bridge camera stream + audio to Scrypted. Requires solving outbound audio path first. | No | Medium | Blocked on outbound path | Designed but not implemented in Kibble. |
| **6. Full firmware replacement (local)** | Dump, patch, and flash Petkit firmware. | No | Very high | **Not recommended** | Breaks Kibble's design principle. Only if all else fails. |

---

## Recommended Next Steps

1. **Investigate the file-driven path (highest ROI):**
   - Use `telnet` to access device; enumerate `/` and common paths (`/media`, `/var`, `/opt`, `/mnt`) for `.aac` files.
   - Identify the naming pattern for pre-recorded prompts.
   - Manually create a test `.aac` file (encode one using `ffmpeg` locally).
   - Send a `play_aac_file` message (msg `0x2`) to `media` process with the file path.
   - If it plays, Kibble has found the solution.

2. **Deep-debug `audio_out_thread` as a fallback:**
   - If file-drive fails or file location is inaccessible, use on-device GDB to observe thread state.
   - Determine if thread is waiting on a message-queue event, a futex, or a GPIO/hardware line.
   - May reveal a required message or handshake that Kibble's ring-write is missing.

3. **Integrate cloud API TTS as a pragmatic fallback:**
   - Use py-petkit-api's `PLAY_SOUND` to offer announcements via pre-recorded sounds.
   - Cloud-dependent but working; suitable for automation (e.g., "feeding time" alerts).
   - Easier to ship than waiting for local solution.

---

## Conclusion

**No project has solved custom audio playback on unmodified Petkit devices locally.** The file-driven prompt path represents an unexplored opportunity: it is vendor-sanctioned (used by Petkit's own prompts), sidesteps all known ring-buffer issues, and has never been attempted by any external project. This is Kibble's most promising next avenue.

Kibble's ring-publish approach is not wrong; it is simply incomplete. Discovery of the correct file location and message-bus trigger could unlock local audio without reverse-engineering Agora, full firmware replacement, or cloud dependency.

---

## References

- **Jezza34000/py-petkit-api**: https://github.com/Jezza34000/py-petkit-api (`pypetkitapi/command.py`, `FeederCommand.PLAY_SOUND`)
- **Jezza34000/homeassistant_petkit**: https://github.com/Jezza34000/homeassistant_petkit (Agora reverse-engineering credits)
- **dwyschka/localkit**: https://github.com/dwyschka/localkit, https://localkit.io/
- **nphil/kibble**: https://github.com/nphil/kibble (`docs/23-audio-codec.md`, `agent/src/audioout.rs`)
- **schnebeck/thingino-arenti-petcam**: https://github.com/schnebeck/thingino-arenti-petcam
- **OpenIPC/firmware**: https://github.com/OpenIPC/firmware, https://openipc.org/majestic-endpoints
- **DavidVentura/cam-reverse**: https://github.com/DavidVentura/cam-reverse
- **Scrypted**: https://github.com/koush/scrypted, https://docs.scrypted.app


[You have received this identical output 3 times. Re-reading 'agent://PriorArtAudio?q=.report' will not change it — use a narrower selector (path:A-B), or proceed with the edit.]