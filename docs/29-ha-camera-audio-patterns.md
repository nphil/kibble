# Home Assistant Camera and Two-Way Audio Patterns
**Research into Reolink integration (platinum) and HA camera/WebRTC APIs for Kibble**

## Executive Summary

This document compares how Reolink (a platinum-quality Home Assistant integration) implements camera streaming and two-way audio against HA\'s core camera APIs, cross-checked against Nitin\'s live instance. **The highest-priority finding: two-way audio from the HA UI is not a native camera feature—it requires a separate audio transport (go2rtc ONVIF backchannel or a media_player entity with TTS), and the UI does not yet have a built-in microphone button for camera entities.** For Kibble, the practical path is either:

1. **Expose a media_player entity for TTS playback** (what Kibble does today), or
2. **Integrate with go2rtc\'s ONVIF backchannel** to add two-way audio via a custom WebRTC provider or dashboard card.

---

## 1. How Reolink Exposes Streams

### 1.1 Camera Entity Structure (Source: `reolink/camera.py`, dev branch)

Reolink creates **one Camera entity per stream profile**, not per physical camera. A single Reolink device channel can generate up to 9 separate camera entities:

**Key pattern:** Multiple `ReolinkCameraEntityDescription` subclasses let Reolink expose different quality tiers and stream types without duplicating entity class code. The `entity_registry_enabled_default=False` hides less-used streams (main, snapshots) from the UI by default, reducing clutter while keeping them available.

### 1.2 Stream Source and Snapshots

- **`stream_source()`** returns an RTSP URL (e.g., `rtsp://192.168.1.50:554/stream0`). HA\'s `stream` component (via go2rtc) converts this RTSP → HLS for the browser.
- **`async_camera_image()`** fetches a JPEG snapshot directly from the camera. Reolink does NOT use the stream for snapshots (`use_stream_for_stills=False`).

### 1.3 Feature Declaration

Reolink declares `CameraEntityFeature.STREAM`, which enables streaming. **Reolink does not declare native WebRTC support; it relies on HA\'s bundled go2rtc WebRTC provider** to convert the RTSP stream.

### 1.4 Why Reolink Does Not Use `async_handle_async_webrtc_offer`

Reolink cameras do not implement native WebRTC—they speak RTSP. HA\'s camera integration automatically:

1. Detects `CameraEntityFeature.STREAM` + RTSP URL
2. Registers go2rtc as the WebRTC provider
3. go2rtc handles the SDP offer/answer and RTSP↔WebRTC translation

---

## 2. Two-Way Audio in Home Assistant: The Complete Picture

### 2.1 **THE CRITICAL FINDING: No Native Camera Two-Way Audio**

**Fact:** HA\'s `Camera` entity has **no built-in two-way audio or microphone support**. There is no attribute, property, or method like `supports_microphone` or `async_send_audio()`. The UI does not automatically show a microphone button for camera entities.

**Evidence:**
- HA camera component (`homeassistant/components/camera/__init__.py`) supports `CameraEntityFeature.STREAM` (video streaming only) and `CameraEntityFeature.ON_OFF` (power control).
- HA camera constants (`camera/const.py`) define only `HLS` and `WEB_RTC` stream types; no audio-specific types.
- Reolink\'s live entities (checked on Nitin\'s instance) have `supported_features: 2` (STREAM only), no audio feature flags.
- Reolink\'s official integration does NOT expose any microphone, speaker, or TTS entity alongside its camera entities.

### 2.2 How Reolink Achieves Two-Way Audio (Community Solutions)

Since the official integration doesn\'t do two-way audio, community solutions exist:

#### **Option A: Baichuan Protocol (Direct, Proprietary)**

Integration: `reolink_talk` (joeblack2k/reolink_talk, maintained fork at mrtncode/reolink_talk)

- Speaks Reolink\'s proprietary **Baichuan binary protocol** directly from Home Assistant
- Exposes a **`media_player` entity** for TTS playback and voice clips
- **Pros:** No additional services (no go2rtc needed); works over HTTP; integrates with HA\'s TTS pipeline
- **Cons:** Only works if camera exposes TalkAbility with `audioType=adpcm`; not tested on cameras behind NVRs

#### **Option B: ONVIF Backchannel (Standardized, Codec-Agnostic)**

Transport: RTSP backchannel over go2rtc, standard ONVIF

- Cameras advertise audio backchannel in their SDP (Session Description Protocol)
- **go2rtc** transcodes audio (e.g., MP3 → G.711 PCMU) and streams it up the backchannel
- **Pros:** Works with any camera that supports ONVIF backchannel; codec-agnostic; industry standard
- **Cons:** Requires go2rtc service; browser requires HTTPS (microphone API requires secure context); latency 2–5s

**How it works (go2rtc):**
1. Camera\'s RTSP SDP includes `a=sendonly` media line for audio backchannel (trackID=2, TCP interleaved)
2. `go2rtc` detects this and advertises backchannel in its WebRTC SDP to the browser
3. Browser captures microphone → WebRTC → go2rtc
4. go2rtc transcodes audio to G.711 PCMU
5. go2rtc sends PCMU up the RTSP backchannel to the camera speaker

**For Kibble:** The device already exposes ONVIF backchannel on TCP interleaved 4-5 with PCMU/8000 codec. **This is a direct drop-in for go2rtc.**

#### **Option C: Custom `media_player` for TTS Only (What Kibble Does Today)**

Kibble already exposes `media_player.kibble_speaker`. This allows TTS playback via HA\'s `tts.speak` service.

**Limitations (current):**
- Audio playback is **blocked on the device side** (agent receives POST correctly, but `audio_out_thread` doesn\'t consume ring buffer—issue being closed separately)
- **No microphone capture** from the HA UI
- **One-way only** (device → browser via camera stream; browser → device requires separate mechanism)

---

## 3. The Missing UI Layer: Why the HA Frontend Doesn\'t Show a Microphone Button

**Fact:** Default HA Lovelace cards do **not support two-way audio**. There is no built-in camera card with a microphone button.

**Why:**
- HA\'s camera entity has no metadata about audio input capability
- WebRTC offer/answer negotiation is handled entirely between browser and provider (go2rtc, Frigate, etc.)
- The frontend would need to know **which camera supports what audio codecs** and **whether HTTPS is available** before showing a microphone button

**Current workarounds:**
1. **Custom `webrtc-camera` card** (AlexxIT/WebRTC integration) — handles SDP negotiation and microphone capture
2. **Browser Mod blueprint** + automation (AmexHusky/reolink-intercom) — pops up card on doorbell ring with talk button
3. **Custom dashboard resource** — implement card component that handles audio capture and WebRTC

---

## 4. Entity Model: Lessons from Reolink

### 4.1 EntityDescription Subclasses

Reolink uses custom `EntityDescription` subclasses to reduce boilerplate. This avoids repeating channel/stream logic for every entity type.

### 4.2 `entity_registry_enabled_default`

Reolink intelligently disables low-priority entities:
- Snapshots (redundant with live stream)
- Main stream (higher bandwidth; substream is preferred)
- Autotrack telephoto variants (advanced features)

**For Kibble:** The camera entity is the single high-value entity; others could use `entity_registry_enabled_default=False` if noisy or system-level.

### 4.3 Coordinator Pattern

Reolink uses a `ReolinkDeviceCoordinator` that polls API on fixed interval + separate `ReolinkFirmwareCoordinator` for 24h update checks. **For Kibble:** Separating firmware/diagnostics polling would reduce unnecessary wakeups.

### 4.4 Availability Semantics

Reolink marks entities unavailable when:
- Device itself is unreachable
- Camera channel is offline (important for NVRs)
- Privacy mode is active

**For Kibble:** Availability is already nuanced (BLE/Wi-Fi fallback); marking unavailable when feeder unreachable would help users.

---

## 5. Quality Scale: What Kibble Would Need for Silver/Gold

### 5.1 Current State

Kibble is a custom (non-core) integration. Kibble today:
- ✅ **Config flow:** User enters IP, configures stream URL (options)
- ✅ **Unique config entry:** Per-device setup
- ✅ **Runtime data:** Coordinator holds live state
- ✅ **Entity naming:** Has entity name
- ❌ **Parallel updates:** Camera doesn\'t set `PARALLEL_UPDATES = 0`
- ❌ **Diagnostics:** No diagnostic endpoint
- ❌ **Repair issues:** No issue registry
- ❌ **Discovery:** No mDNS/zeroconf

### 5.2 What Would Move Kibble toward Silver/Gold

**Silver:**
1. Parallel updates protection: `PARALLEL_UPDATES = 0`
2. Config entry unloading
3. Entity availability: Mark unavailable when offline
4. Test coverage: Unit tests for codec, fallback, API errors

**Gold:**
1. Diagnostics support
2. Repair issues
3. Discovery (Zeroconf mDNS)
4. Translation keys: Full i18n

**Hard constraint:** Custom integrations cannot satisfy core HA team ownership.

---

## 6. What NOT to Copy from Reolink to Kibble

### 6.1 Multiple Stream Profiles

Reolink exposes main/sub/telephoto because each has separate encoders. **Kibble:** Has one encoder (1728×1080 H.264) + one substream (1152×720). Creating 9 entities would be noise. Stick with one primary camera entity.

### 6.2 NVR/Hub Device Hierarchy

Reolink creates device tree (NVR → Camera → Lens). **Kibble:** Single feeder = single device. No hierarchy needed.

### 6.3 Direct Camera Control

Reolink supports PTZ, motion detection, recording control. **Kibble:** Feeder doesn\'t. Don\'t invent unsupported features.

### 6.4 Privacy Mode Availability

Reolink marks all entities unavailable in privacy mode. **Kibble:** Privacy mode N/A. Availability = connectivity only.

---

## 7. Kibble-Specific Recommendations (Prioritized)

### 7.1 **Priority 1: Enable Two-Way Audio via go2rtc ONVIF Backchannel** ⭐⭐⭐

Kibble\'s device exposes ONVIF backchannel on TCP interleaved 4-5, PCMU/8000. Scrypted rebroadcast URL is valid RTSP. go2rtc can wrap this.

**Concrete steps:**

1. **Verify backchannel in Scrypted rebroadcast:**
   ```bash
   ffprobe rtsp://scrypted-host:40081/<hash>
   # Should show: audio track, codec=pcmu, 8000 Hz
   ```

2. **Documentation:** Add to config flow:
   ```
   Scrypted Rebroadcast URL (RTSP)
   [rtsp://192.168.1.69:40081/abc123    ]
   
   This URL supports two-way audio via ONVIF backchannel when accessed
   through Home Assistant with go2rtc (bundled in WebRTC Camera add-on
   or Home Assistant OS 2024.8+).
   ```

3. **Dashboard setup:** Guide users to:
   - Enable HTTPS (required for microphone access)
   - Install `webrtc-camera` custom card or use Browser Mod
   - Set `media: video,audio,microphone` in card config

**Expected UX:**
- Camera stream shows live video
- Browser asks for microphone permission (HTTPS only)
- User clicks microphone → audio captured → transcoded to PCMU → feeder speaker
- Latency: 2–3 seconds (go2rtc backchannel overhead)

**Blockers:**
- Scrypted must preserve ONVIF backchannel (likely does)
- User must have valid HTTPS (not just HTTP on LAN IP)
- Requires custom card; no built-in HA support yet

### 7.2 **Priority 2: Expand media_player for Voice Input** ⭐⭐

Once device-side audio playback is unblocked:

1. **Create `assist_satellite` entity** (ESPHome/VoIP/Wyoming pattern):
   - Kibble\'s microphone via agent HTTP API
   - Integrates with HA Assist pipelines (STT → interpretation → TTS response)
   - User: "Hey, ask Kibble..." → feeder listens + responds

2. **Or: Simple voice clip recording service**
   - Capture audio from browser
   - Upload to agent
   - Store on device for playback

**Blockers:**
- Agent doesn\'t yet expose microphone endpoint
- Requires HTTPS + secure WebSocket

### 7.3 **Priority 3: Improve Entity Model** ⭐

Reduce 51 entities to curated set with `entity_registry_enabled_default=False` for diagnostic/advanced entities.

**Rationale:** Users see too many sensors. Hide firmware, binary sensors, advanced number settings.

**Target:** ~10 entities (camera, speaker, dispense button, bowl fill, desiccant, WiFi signal, online status).

---

## 8. Snapshot: Reolink\'s Platinum Quality (Live Instance Check)

Nitin\'s HA 2026.9.2:
```
Reolink integration (core)
├── Discovered 3 devices (DHCP auto-discovery)
├── Camera entities: 3 (one per device, state: idle, supported_features: 2 = STREAM)
├── Sensor entities: motion, temperature, etc. (disabled by default)
├── Binary sensor entities: presence, motion
├── Switch entities: privacy, recording, etc.
├── Select entities: quality, resolution, etc.
└── Light entities: LED control (some models)

NO media_player, NO microphone, NO two-way audio entities.
```

**Platinum rating from:**
- ✅ Full device support (NVR + cameras + chimes)
- ✅ Comprehensive entities
- ✅ Clean config + reauthentication
- ✅ Repair issues + diagnostics
- ✅ Full i18n
- ✅ Extensive tests

**NOT from audio features** — two-way audio is user\'s choice of WebRTC provider + custom card.

---

## 9. References

- **HA Camera Entity Docs:** https://developers.home-assistant.io/docs/core/entity/camera/
- **Reolink Integration (dev):** https://github.com/home-assistant/core/tree/dev/homeassistant/components/reolink
  - `camera.py`: Stream, snapshot, profiles
  - `entity.py`: EntityDescription, availability, device hierarchy
  - `quality_scale.yaml`: Platinum requirements
- **HA Camera WebRTC:** `homeassistant/components/camera/webrtc.py` (dev)
- **HA Stream:** `homeassistant/components/stream/` — HLS, go2rtc
- **go2rtc:** https://github.com/AlexxIT/go2rtc — RTSP↔WebRTC, ONVIF backchannel
- **Community Two-Way Audio:**
  - Baichuan: https://github.com/mrtncode/reolink_talk
  - ONVIF backchannel: https://github.com/AmexHusky/reolink-intercom-Home-Assistant
  - Browser Mod: https://github.com/thomasloven/hass-browser_mod
- **Kibble:**
  - Camera: `custom_components/kibble/camera.py`
  - Media player: `custom_components/kibble/media_player.py`
  - Backchannel: `agent/src/backchannel.rs` (ONVIF, PCMU/8000, TCP interleaved 4-5)

---

**Document version:** 2026-09-15
**HA Core version researched:** dev branch (2026.9+)
**Reolink integration quality:** Platinum (core)
**Kibble integration quality:** Custom (HACS, target: Silver)
