# Two-way audio: how Home Assistant does it, and what Kibble must implement

Requirement (Nitin, 2026-09-15): the feeder's camera must appear **as an entity on the Kibble
device itself** in Home Assistant, with full two-way audio, done the way Home Assistant natively
does two-way audio for cameras — not as a separate integration's device, and not only via Scrypted.

## How HA actually does it

From the [camera entity docs](https://developers.home-assistant.io/docs/core/entity/camera/):
two-way audio rides on **WebRTC**, not HLS. There are two routes.

1. **Native WebRTC** — the entity declares `CameraEntityFeature.STREAM` and implements
   `async_handle_async_webrtc_offer` + `async_on_webrtc_candidate` (and optionally
   `close_webrtc_session`). The integration then owns the whole peer connection. Note the doc's
   warning: implementing these tells the frontend the camera is WebRTC-only, with **no HLS
   fallback**.
2. **A WebRTC provider** — the entity just returns a `stream_source()` (our RTSP URL) and
   `CameraEntityFeature.STREAM`, and HA's bundled **go2rtc** provider converts RTSP to WebRTC.
   Audio flows back to the camera when the source offers a return path.

**Kibble uses route 2.** Rationale: it is far less code than owning ICE/DTLS/SRTP in the
integration, it keeps HLS and recording working, it reuses the go2rtc that ships with HA, and — the
decisive point — the return path it needs is the *same* RTSP audio backchannel that Scrypted's
ONVIF intercom needs ([scrypted-onboarding.md](scrypted-onboarding.md)). One implementation in
`kibbled` satisfies both consumers.

## What that means for `kibbled`

| piece | requirement | status |
|---|---|---|
| RTSP video | H.264 from the frame ring, no re-encode | **in progress** ([19-frame-ring.md](19-frame-ring.md)) |
| RTSP audio, camera → client | mic audio as an RTP track | **blocked: codec unknown** |
| RTSP backchannel, client → camera | a second `m=audio` section marked `a=sendonly`, negotiated when the client sends `Require: www.onvif.org/ver20/backchannel`; accept G.711 µ-law/A-law (what Scrypted's intercom offers) and/or Opus (what HomeKit sends before conversion) | not implemented |
| Play received audio | decode and hand to the speaker | mechanism identified, unproven |

### The two genuine unknowns

**Outgoing audio.** Microphone audio *is* in the frame ring (`chan=1`, ~60 ms cadence, ~35 kbps,
header says 16 kHz / 16-bit), but it is demonstrably **not raw PCM** (entropy and byte-histogram
tests) and **not raw Opus** (a byte-exact Ogg/Opus container parsed fine; libopus rejected the
payloads). Until that codec is identified, the stream is video-only — `noAudio` in Scrypted, and no
outgoing audio over WebRTC. Next step: check whether it is AAC-LC in a raw/ADTS-less form (the
vendor links FDK-AAC for playback, so an AAC *encoder* is plausibly in the same library), or a
Telink/Axera-specific ADPCM variant. Trying `libfdk_aac` against the raw payload is a five-minute
experiment once someone is at a keyboard.

**Incoming audio.** The vendor's own talkback (Agora) feeds a dedicated `audio_out_thread` inside
`media`, consuming a channel literally named `audio-out` (vendor typo, present in both the binary's
strings and the live reader registry), which calls `AX_AO_SendFrame` with **raw 16 kHz 16-bit PCM**.
That is the format to produce. What is *not* established is whether a second process can push into
that hand-off, or whether it is reachable only from inside `media` — i.e. whether Kibble must
instead replace the vendor's audio path. That is the next thing to establish, and it decides
whether talkback is a small feature or a large one.

## The entity, when it lands

On the existing Kibble device (so it appears inside the same device card, not a second device):

```python
class KibbleCamera(KibbleEntity, Camera):
    _attr_supported_features = CameraEntityFeature.STREAM

    async def stream_source(self) -> str:
        return f"rtsp://{host}:8554/sub"      # substream: 1152x720, H.264, 25 fps

    async def async_camera_image(self, width=None, height=None) -> bytes:
        ...                                   # hardware JPEG via the agent
```

Snapshots come from the device's hardware JPEG encoder (`AX_VENC_JpegEncodeOneFrame`), so a still
costs nothing on the ARM cores.

Scrypted remains useful in parallel — NVR recording, object detection, and the HomeKit accessory
alongside Nitin's other cameras — and reads the same RTSP URL. The HA camera entity is not a
duplicate of it: it is what makes the feeder self-contained if Scrypted is ever not in the path.
