# Two-way audio: shipped architecture

Requirement (Nitin, 2026-09-15, refined 2026-09-16): the feeder's camera and its two-way audio
must be usable from Home Assistant **and** from HomeKit, with **Scrypted as the single source of
truth** for all audio/video — one consumer of the feeder's streams, not one per client.

## The path, as built

```
phone / HA dashboard / HomeKit  --Opus/WebRTC-->  Scrypted (Unraid, GPU box)
                                                   │  WebRTC plugin: sendrecv audio
                                                   │  Rebroadcast: the one persistent RTSP pull
                                                   ▼
                                        Kibble Scrypted mixin (Intercom)
                                                   │  ffmpeg → L16/16000 RTP
                                                   ▼
                          kibbled RTSP backchannel (trackID=2, interleaved 4-5, PT 98)
                                                   │  aacenc → ADTS AAC-LC/16 kHz
                                                   ▼
                     /tmp/kibble-talk.aac (FIFO) → one play_aac_file → media → speaker
```

Outgoing (mic) audio rides the same Scrypted stream: `kibbled` puts the ring's `chan=1` records
on the RTSP session as an AAC track with zero re-encode
([23-audio-codec.md](23-audio-codec.md) §14), Scrypted's prebuffer reports `h264/aac`, and every
consumer — HA, HomeKit, the Scrypted app, NVR recording — reads Scrypted's rebroadcast.

**Why not HA's own go2rtc provider (the original plan):** it would open a second RTSP session on
the feeder for video and a third for the backchannel, on a device whose own server only keeps a
spare slot for one transient session. Routing everything through Scrypted keeps the feeder at one
persistent consumer, reuses the intercom HomeKit already drives, and puts the heavy work on the
server that has the GPU. HA is a client of Scrypted, not a second camera stack.

## The pieces

| piece | where | status |
|---|---|---|
| RTSP video + mic AAC, zero re-encode | `agent/src/rtsp.rs` | done ([19-frame-ring.md](19-frame-ring.md), 23 §14) |
| RTSP backchannel, client → feeder | `agent/src/rtsp.rs`, `backchannel.rs` | done — offers `L16/16000` (PT 98) first, G.711 for generic clients |
| Speaker playback | `agent/src/audioout.rs` | done — one `play_aac_file` on a FIFO (23 §20.6) |
| Scrypted intercom | `scrypted-plugin/src/{mixin,rtspBackchannel}.ts` | done — ffmpeg → L16/16000, low-delay flags |
| HA camera + talk UI | `kibble-card` v0.3.0 (`scrypted_id` config) | done — WebRTC through the Scrypted HA integration's proxy, hold-to-talk |
| HomeKit two-way | Scrypted HomeKit mixin | works once the Kibble mixin is ordered before it (below) |

## Setup notes that are easy to get wrong

- **Mixin order in Scrypted matters.** The Kibble mixin provides `Intercom`; the WebRTC and
  HomeKit mixins only offer two-way audio if they can *see* it, i.e. if Kibble sits **before**
  them in the camera's mixin list. With Kibble last, WebRTC answers with the audio m-line
  rejected (`m=audio 0`) and there is no return path at all — which is exactly how this looked
  broken for a day. Current order: Rebroadcast, **Kibble Feeder**, WebRTC, Snapshot, Adaptive
  Streaming, HomeKit, NVR, NVR Object Detection, Accelerated Motion, Events recorder.
- **`noAudio` must be `false`** on the RTSP Camera device so Scrypted negotiates the mic track
  (`prebuffer:detectedCodec` = `h264/aac`).
- **HA's Kibble integration** keeps `stream_url` pointed at Scrypted's rebroadcast URL, so its
  camera entity (snapshots, HLS, automations) never touches the feeder directly either.
- **`/opt/kibble/audio_enabled`** gates every speaker path on the feeder. It is on; nothing but an
  explicit `/speak`, `/clips/<name>/play` or backchannel `PLAY` can reach `media`'s audio path
  (23 §19.4, §20.7).

## The camera entity

`custom_components/kibble/camera.py`, on the existing Kibble device (not a second one):

```python
class KibbleCamera(KibbleEntity, Camera):
    _attr_supported_features = CameraEntityFeature.STREAM

    async def stream_source(self) -> str:
        return self.coordinator.rtsp_url          # Scrypted's rebroadcast when configured

    async def async_camera_image(self, width=None, height=None) -> bytes:
        ...                                       # hardware JPEG via the agent
```

Snapshots come from the device's hardware JPEG encoder (`AX_VENC_JpegEncodeOneFrame`), so a still
costs nothing on the ARM cores. Live view and talkback in the dashboard come from the card's
Scrypted WebRTC session, not from this entity — HLS through `stream_source` remains the fallback
for anything that wants a plain camera entity (automations, notifications, Assist).
