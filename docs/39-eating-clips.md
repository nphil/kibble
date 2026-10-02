# Eating clips: Scrypted Events Recorder -> timeline playback

Status: design of record, 2026-09-25. Nitin: no 24x7 recording on the pet camera; clips come only
from the Events Recorder plugin, triggered by real eating; the timeline plays them back cleanly.

## Findings that shaped this (verified on the live Scrypted, 2026-09-25)

- Scrypted runs on Unraid (`scrypted` container, host network). Pet camera = device 240
  (`@scrypted/onvif`); device 238 is the old RTSP device of the same camera (NVR dirs empty).
- NVR was recording device 240 24x7: 93 GB under `/mnt/endurancessd/NVR/scrypted-240*`.
- Events Recorder = `@apocaliss92/scrypted-events-recorder` 0.0.51 (no license file). Clips live
  in `/NVR/clips/<deviceId>/videoclips/<startMs>_<endMs>_<10-bit class hash>.mp4` (H.264, CRF 23,
  audio stripped, `+faststart`), thumbnails beside them, quota-based cleanup (`maxSpaceInGb`).
- Its only triggers are the device's own `ObjectDetector` and `MotionSensor` events. A detection
  counts only if its `className` maps to a known class (`cat` -> Animal), it has a `boundingBox`
  (`ignoreCameraDetections`), and it is not `movement.moving === false`. A recording is extended by
  each new trigger up to `maxLength` (default 60 s) and ends `postEventSeconds` after the last one;
  pre-roll is half the stream's rebroadcast prebuffer.
- Why today's 778 clips on 240 are short arrival clips (15 to 26 s): the Kibble mixin emits
  `className: "eat"` once per track with no box, which the recorder ignores; the clips come from
  motion/ONNX animal detections, and an eating cat is stationary, so nothing extends the clip.

## Design

```
feeder /events (open eat track)
  -> Kibble mixin (device 240): every poll while eating, an ObjectDetector event
     {className: "cat", label: <cat>, boundingBox: <feeder box>, score}, stable detectionId per track
  -> Events Recorder (device 240, triggers = Animal only): records one MP4 spanning the meal
     (pre-roll from prebuffer, extended every 5 s, ends post-roll after eating stops)
HA  -> Kibble plugin HTTP endpoint (public, read-only JSON): clips overlapping a time window, via
       device 240's VideoClips (the recorder) -> HA links clip to the eat session in its store
HA  -> authenticated HA view proxies the MP4 (Range passthrough) from the recorder's own
       `videoclip` webhook -> card <video> in the review sheet
```

- The recorder is driven through its designed interface (detections on the device); no fork.
- As shipped (2026-09-25), device 240:
  - NVR `recording:privacyMode` = true (no 24x7 recording); its 110 GiB of old recordings deleted.
  - Events Recorder mixin: `detectionClasses` [animal], `maxLength` 900, `postEventSeconds` 15,
    `minDelayBetweenClips` 1, `maxSpaceInGb` 80 (about 38 MB per minute of clip),
    `ignoreCameraDetections` true, `prolongClipOnMotion` false.
  - NVR Object Detection allowList narrowed to [package] (it rejects an empty list), so it can no
    longer raise Animal detections; the ONVIF plugin's boxless events are dropped by
    `ignoreCameraDetections`; the Kibble mixin's own ONNX second pass never returns animal classes.
  - Pre-roll is 5 s, not 10 s: Scrypted's prebuffer is a hard-coded 10 s and the recorder uses half.
- Kibble plugin: `GET http://192.168.1.69:11080/endpoint/@nphil/kibble-scrypted/public/clips?start=&end=`
  (ms) lists clips; `videoId` is `<startMs>_<endMs>_<10 bits>` with no extension. The integration
  option "Scrypted clips server" (`scrypted_clips_url`) turns the feature on.
- HA links a clip to each closed eat session (immediately, then +30 s, +2 min, +10 min; relinks
  the last day at startup) and serves it at `GET /api/kibble/<entry>/clip/<event uid>` with Range
  passthrough; the card signs that path. A missing clip degrades to the photo silently.
- Card: eat rows get a small play badge on the existing photo tile (no new tile, no autoplay in
  the list). The review sheet's scene slot becomes the player: scene photo as poster, play on
  tap, inline, native controls and full screen, released on close. Per-cat split segments start
  at their own offset in the shared clip.
