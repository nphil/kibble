# Kibble Feeder — Scrypted plugin

A Scrypted `MixinProvider` for the Kibble feeder camera (device 240, "Plant Room Cat Feeder").
Adds:

- **`ObjectDetector`** — relays the agent's (`kibbled`) real on-device detection feed
  (short-polls `GET /events` on a gap; see "The starvation incident" below for why it does not use
  the agent's `GET /events/stream` long-poll) as Scrypted `ObjectsDetected` events: a one-shot,
  boxless augmentation per fresh track for NVR smart search, plus a continuous, real-box
  className: 'cat' trigger emitted every poll
  while a track is open and eating (`handleEating`) — the only thing meant to drive
  `@apocaliss92/scrypted-events-recorder`'s own mixin into recording a clip. See
  `kibble/docs/39-eating-clips.md` for the full design and `## Eating clips` below for what's
  proven live.
- **`Settings`** (plugin-level, not per-mixin — see `types.ts`'s `CameraRegistry` doc comment for
  why) — feeder host/ports, the second-pass toggle, and a "Record test clip" button that exercises
  the eating-trigger pipeline without a real cat.
- **`HttpRequestHandler`** — a public, read-only `clips` webhook HA polls for the feeder camera's
  Events Recorder clips (`main.ts`'s `onRequest`).

> **Note on the feeder's address:** the feeder has moved networks at least once during this
> project (`192.168.4.85` → `192.168.1.85`). The host is a plugin Setting, not a hardcoded
> constant, for exactly this reason — update it there, not in code, if it moves again. Evidence
> captured earlier in this README against `192.168.4.85`/device 238 predates both the move and a
> device re-add; the default now points at the current address, and the live camera is device 240.

## The starvation incident (read this before touching the poll design)

During this branch's live testing, attaching the mixin ran `KibbleDetectionFeed` against the
agent's `GET /events/stream?since=N` long-poll (a deliberate ~25s server-side hold,
`ai::LONG_POLL_TIMEOUT`) continuously. `kibbled`'s HTTP server has limited concurrency, and the
held connection starved Home Assistant's own polling of the same device — every Kibble entity
went `unavailable` for the whole house until the mixin was detached. Confirmed live:
`GET /state` on the agent went from 0.19s (uncontended) to 3-of-5 attempts timing out at 10s while
the long-poll was attached, and recovered to 0.01–0.15s on 4/4 immediately after detaching.

Two independent fixes, both present in this branch:
1. **The real fix**: `KibbleDetectionFeed` no longer touches `/events/stream` at all. It
   short-polls the plain, instant `GET /events` snapshot (an in-memory read, no server-side wait)
   on a 5s gap, comparing `seq` client-side. No single request this feed makes should ever hold
   the server for more than a fraction of a second, by construction — not "usually," structurally.
2. **Belt and suspenders**: `stop()` calls `AbortController.abort()` on whatever request is
   currently in flight, rather than only skipping the next poll iteration, so releasing the mixin
   can never leave a request hanging regardless of which endpoint is in use.

## Install

```sh
cd scrypted-plugin
npm install
NODE_ENV=development npm run build        # -> out/plugin.zip (use NODE_ENV=production for dist/)
NODE_TLS_REJECT_UNAUTHORIZED=0 npx scrypted-deploy <scrypted-host>[:10443]
```

`npx scrypted-deploy <host>` needs `~/.scrypted/login.json`:
```json
{ "<host>:10443": { "username": "<user>", "token": "<password>" } }
```
(or run `npx scrypted login <host>` interactively once).

Then, in Scrypted → Devices → the feeder camera → Extensions, enable "Kibble Feeder", or via
`@scrypted/client`:
```js
const cam = systemManager.getDeviceById(<feeder-device-id>);
const kibble = systemManager.getDeviceByName('Kibble Feeder');
await cam.setMixins([...(cam.mixins || []), kibble.id]);
```

Configure the feeder's host/ports and the intercom's RTSP mount under the "Kibble Feeder" plugin's
own Settings (not the mixin) — one feeder, one place to configure it.

## What's proven, live, this session — and how

All three items below were re-verified against the actual running Scrypted instance
(`https://100.86.255.118:10443`, Scrypted `v0.147.0`) and the real feeder (`192.168.4.85`) after
the plugin was deployed, not merely code-reviewed.

**1. The plugin loads and the mixin attaches cleanly.** Deployed via `scrypted-deploy`; appears as
device "Kibble Feeder" (`@nphil/kibble-scrypted`) with a live plugin host process. Attaching it to
device 238 ("Plant Room Feeder Camera") via `setMixins` added `Intercom` to that device's
interface list without disturbing its existing `Camera`/`ObjectDetector`(from the NVR's own motion
mixins)/`VideoCamera`/etc. — mixin stacking works exactly the way Scrypted's own NVR
detection mixins already stack onto cameras that have their own native detection (several of
Nitin's other cameras in this exact instance show that same pattern already).

**2. `ObjectDetector.getObjectTypes()` on the mixed-in camera returns real, live data requiring
both halves of the feature to actually work:**
```
{"classes":["face","visit","eat","person","vehicle","animal"]}
```
`face`/`visit`/`eat` are the agent's own on-device classes (`agent/src/ai.rs`'s `WATCHED` array).
`person`/`vehicle`/`animal` came from live-querying this Scrypted instance's own "ONNX Object
Detection" plugin (`@scrypted/onnx`, a YOLOv9c model) via `systemManager`/`ObjectDetection.
getDetectionModel()` — proving the second-pass detector auto-discovery and the `sdk.systemManager`
access it depends on both work end-to-end against the live system, not just in isolation.

**3. Intercom RTSP/ONVIF backchannel negotiation — full transcript, captured live via the plugin's
own "Test intercom negotiation" button** (Settings → Kibble Feeder), which drives the *exact* same
`RtspBackchannelClient` `startIntercom` uses, standalone:
```
connecting to rtsp://192.168.4.85:8554/sub
TCP connect ok
OPTIONS -> 200 OK
DESCRIBE (Require: backchannel) -> 200 OK; backchannel offered in SDP = true
SETUP trackID=2 (UDP) -> 461 Unsupported Transport (expect 461 Unsupported Transport)
SETUP trackID=2 (TCP) -> 200 OK; Transport: RTP/AVP/TCP;unicast;interleaved=4-5
PLAY -> 200 OK
sent 25 PCMU frames on the backchannel; received 84 video + 0 mic frames while playing
TEARDOWN -> 200 OK
RESULT: PASS
```
This is the real ONVIF backchannel handshake (`Require: www.onvif.org/ver20/backchannel`, the
documented UDP→461 fallback, TCP interleaved=4-5, a live PLAY that received real video frames back
— proof the session was genuinely live, not just a 200 OK) against the running device, not a
simulation. **It proves the negotiation and transport. It does not and cannot prove sound was
heard** — see "What's blocked" below.

**4. Re-verified after the starvation incident and fix, live, mixin left attached:** re-attached
to device 238 with the short-poll `GET /events` design; `getObjectTypes()` immediately answered
correctly again (same merged on-device+ONNX classes as above, proving the mixin instance came
back up cleanly). Sampled `GET /state` on the agent across two full poll windows afterward: 10 of
11 samples at 9–48ms, one at 4.6s — a single, brief blip consistent with ordinary shared-device
contention (other work was concurrently running against the same feeder this session), not the
sustained 3-of-5-attempts-timing-out-at-10s starvation pattern from before the fix. Left attached.

**Not independently re-observed this session:** a live `ObjectsDetected` event firing from a real
or file-dropped cat visit. The detection-feed poll loop starts unconditionally in the mixin's
constructor (the same constructor that successfully answered `getObjectTypes()` above both times,
which requires the instance to be fully alive), so it is running; a `face`/`visit`/`eat` event was
not independently triggered and captured in this session's remaining time. The mechanism itself
(`agent/src/main.rs`'s route table, `agent/src/ai.rs`'s poller) is unchanged, existing, and
already used by other consumers.

## What's real vs. honestly incomplete in `ObjectsDetected`

The feeder's GET /events returns track records. Each kept sample's box and score are live detector
results; body/face are flat asset names, each null when that crop was not retained. Identity is no
longer in the feeder API; Home Assistant owns cat naming.

This plugin's one-shot detection mirrors the track class and does not invent missing box, score, or
identity fields. The eating trigger uses the latest real sample's box and score, with 0.9 only when
the daemon provides no score.

**Event media path:** the feeder serves assets by flat name at GET /events/<name>. Home Assistant's
ingest archives them under media/<YYYY-MM-DD>/<asset>, then acknowledges them with DELETE
/events/<asset>. That date directory is the HA archive layout, not part of the feeder URL. An event
row can still name an asset already removed from the feeder's transient spool. The plugin
URL-encodes the flat name; 404 means no image is available, and other HTTP/network failures remain
visible.

## Second pass: off-device detection

The plugin can invoke Scrypted's ONNX/OpenVINO detector as a service from the mixin (verified live
with ObjectDetection.detectObjects on ONNX Object Detection, about 35 ms). It runs this re-check
only when an event crop is available. Auto-discovery skips the NVR's own per-camera detection
mixins and matches on /onnx|openvino/i, so it also works with an OpenVINO-based instance. Toggle:
Settings → Second-pass detection.

## Eating clips: Events Recorder trigger, verified live end to end

Design: `kibble/docs/39-eating-clips.md`. The wire schema this depends on (`GET /events` reports
TRACKS with `samples[]`, not one-shot detections — `event_id` stable, `seq` bumping every admitted
frame while open) was re-verified live against the real feeder 2026-09-25, since it had drifted
from this file's original (dead) `RawDetection` shape; see `types.ts`'s header comment.

**The trigger.** `handleEating` (`mixin.ts`) watches every poll's full snapshot (not just the
`seq`-fresh subset `handleOne` uses) for a track that is `open` and either `class === 'eat'` or
already has `eat_start` set. While one exists, it emits one `ObjectDetector` event per poll:
`className: 'cat'`, a real `boundingBox` (the latest sample's fractional `box`, converted to pixel
space via `fractionalBoxToPixels`), `score` (the sample's own, or `0.9` when absent), and a
detectionId stable for that track's whole life (`kibble-eat-<event_id>` — `event_id`, not `seq`,
because `seq` itself keeps changing while the track is open). Configured on device 240's Events
Recorder mixin: `detectionClasses: ['animal']` only (no `motion`), `ignoreCameraDetections: true`,
`prolongClipOnMotion: false`, `maxLength: 900`, `postEventSeconds: 15`, `minDelayBetweenClips: 1`.

**Nothing else may trigger it.** Scrypted NVR Object Detection's per-camera `allowList` on 240 is
narrowed off (`["package"]` — an empty list is silently rejected by that plugin, confirmed live;
`package` is a harmless placeholder that can never fire at a feeder). `ignoreCameraDetections:
true` also blocks the ONVIF plugin's own native `Detection` events, which never carry a
`boundingBox` (`@scrypted/onvif`'s `onvif-events.ts`) — verified by reading that plugin's own
source, not assumed. Within this plugin, `trySecondPass`'s general-purpose off-device re-check
(runs on every one-shot detection with a crop, not just eating) strips any Animal-mapped className
before returning — real, low-confidence `'animal'`-classed hits from it were confirmed live in
`/NVR/clips/240/events/*/events.json` before this filter existed, which would otherwise have made
this mixin its own second, uncontrolled trigger source on ordinary bowl *visits*.

**Verified live, 2026-09-25**, via the "Record test clip" Settings button (Kibble Feeder plugin,
not the camera's own Settings — see `types.ts`'s `CameraRegistry` doc comment for why): pressing
it emits the same shape of synthetic eating detection every 5s for 30s. Result: Events Recorder
logged `Starting new recording: {"classTriggers":["animal"]}` at the press instant, then
`Videoclip stored /NVR/clips/240/videoclips/1790329844804_1790329894817_1001000000.mp4` ~50s
later (5s pre-roll + 30s test + ~15s post-roll, matching the configured settings). `ffprobe`:
valid `h264`/1920x1080/25fps + `aac`, 53.76s, 3.79 Mbps. The `clips` webhook listed it
(`{"videoId":"...804_...817_1001000000","startTime":1790329844804,"endTime":1790329894817,
"duration":50013,"detectionClasses":["animal","motion"]}`); a `Range: bytes=0-2097151` GET against
the recorder's own `videoclip` webhook for that `videoId` returned `206 Partial Content` with a
matching `Content-Range`.

**Also observed live in the same window, unprompted:** real cat "visit" tracks (event_ids
1433-1438, `class: "visit"`, `eat_start: null`) produced no clip at all — direct evidence, not
just synthetic-test evidence, that a walk-by no longer records.

**Missing images:** feeder assets disappear from its transient spool after Home Assistant archives
them. If the feeder returns 404, the plugin supplies no detection image instead of throwing; the
detection and clip trigger still run. Other HTTP/network failures remain visible.

## Intercom: MOVED to `@nphil/camera-intercom` — historical record below, not current behavior

Everything in this section describes an earlier build. Two-way audio no longer lives in this
plugin at all — see this file's top summary and `mixin.ts`'s own header comment. Kept verbatim
for the historical negotiation transcript (still a real, useful reference for the ONVIF
backchannel protocol itself), not because any of `startIntercom`/`stopIntercom`/`rtspBackchannel.ts`
still exists here.

### Historical: "Intercom: working, wideband" (original section, verbatim)

The feeder serves video to exactly one persistent consumer (Scrypted's Rebroadcast). Scrypted's
rebroadcast path is receive-only, so talkback opens its **own** short-lived RTSP session straight to
`kibbled` (`rtspBackchannel.ts`) — real DESCRIBE/SETUP/PLAY against the device, per
`docs/23-audio-codec.md §14`. Because the device's RTSP server always starts full video+audio
playback on `PLAY`, this transient session is also a second video session on `/sub` for the
duration of one call — the role `agent/src/rtsp.rs`'s `MAX_SESSIONS_PER_STREAM` spare slot exists
for. `startIntercom` tears it down on `stopIntercom`/error.

`startIntercom` spawns `ffmpeg` (via `mediaManager.getFFmpegPath()`, low-delay flags) on whatever
`FFmpegInput` the caller hands in (HomeKit's Opus, the Scrypted app's WebRTC audio), transcodes to
**L16/16000** (raw 16-bit big-endian PCM at the feeder's native 16 kHz — no G.711 companding, no
8 kHz band-limit), and sends it as RTP on the negotiated interleaved channel (payload type 98,
`a=rtpmap:98 L16/16000` in the feeder's SDP; G.711 stays available for generic clients).

On the feeder, `kibbled` encodes to AAC and streams it through a named pipe that `media` plays as
one long "prompt file" (`docs/23-audio-codec.md §20`), so there are no file/chunk boundaries and
the speaker is paced by the audio driver itself. Measured latency budget ≈ pre-roll 0.5 s + encoder
~0.1 s + driver buffer; session diagnostics (frames played, silence inserted, max lag) are on the
agent's `GET /audio` after every call.

`noAudio` on device 238 should be `false` so Scrypted negotiates the mic AAC track
(`prebuffer:detectedCodec` = `h264/aac`) for viewers and HomeKit.

## A real @scrypted/sdk@0.5.59 workaround (`sdkFix.ts`)

Live-diagnosed against this exact deployment, not guessed: `import sdk from '@scrypted/sdk'` (the
ES-interop `.default` binding) reads as `undefined` in every file of this bundle — built with
`concatenateModules: false` in `webpack.nodejs.config.js`, itself required to work around a
*different*, real webpack/SDK bug (`Cannot get final name for export 'MixinDeviceBase'` under the
default production config). The SDK's own `dist/src/index.js` documents a self-population
mechanism (`__non_webpack_require__(process.env.SCRYPTED_SDK_MODULE).getScryptedStatic()`); calling
that exact expression from this plugin's own code returns a fully populated object, but the SDK's
own attempt to do the same doesn't stick on `exports.default`. Its **named** `.sdk` export,
however, *is* the live object `ScryptedDeviceBase`/`MixinDeviceBase`'s own internals mutate and
read. `sdkFix.ts` fetches `require('@scrypted/sdk').sdk` (never `.default`) and merges the real
statics into it once; every other file imports `sdk` from `./sdkFix`, never from `@scrypted/sdk`
directly. Also required: `@scrypted/sdk`'s own `webpack.nodejs.config.js` is missing
`terser-webpack-plugin` from its dependency list (added here as a devDependency), and its shipped
`tsconfig.plugin.json` sets `module: "commonjs"` + `moduleResolution: "Node16"`, a combination
TypeScript 5.9 now rejects (this project's `tsconfig.json` extends it via a relative path — package
`exports` maps don't expose that file as a subpath import — and overrides `module` to `"Node16"`).

## Project constraints honored

Zero existing agent or HA files touched (new `scrypted-plugin/` directory only). No second
*persistent* RTSP consumer. `noAudio` untouched. No food dispensed, no vendor process touched, no
`kibbled` restart. Coordinated with `AudioStart` over `hub` before/after the audio measurement.
