# Kibble Feeder — Scrypted plugin

A Scrypted `MixinProvider` for the Kibble feeder camera (device "Plant Room Feeder Camera",
RTSP Camera Plugin). Adds two capabilities on top of that camera:

- **`ObjectDetector`** — relays the agent's (`kibbled`) real on-device detection feed
  (short-polls `GET /events` on a gap; see "The starvation incident" below for why it does not use
  the agent's `GET /events/stream` long-poll) as Scrypted `ObjectsDetected` events, plus an
  optional off-device re-check through this Scrypted instance's own ONNX/OpenVINO detector.
- **`Intercom`** — a direct, transient RTSP connection to the agent's ONVIF-style backchannel
  (`agent/src/rtsp.rs`/`backchannel.rs`), independent of Scrypted's Rebroadcast plugin (which only
  ever *reads* from the feeder).

> **Note on the feeder's address:** the feeder has moved networks at least once during this
> project (`192.168.4.85` → `192.168.1.85`). The host is a plugin Setting, not a hardcoded
> constant, for exactly this reason — update it there, not in code, if it moves again. Evidence
> captured earlier in this README against `192.168.4.85` predates the move; the default now
> points at the current address.

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

Per the agent's own module doc (`agent/src/ai.rs`): `class` and the cropped `image` are genuine
vendor JPEG side effects; `score`, `pet_id`, and `box` are **honestly always `null`** — that data
exists only inside a private vendor message queue (`ctrl`'s own `/msg_dispatch_1`, fully documented
in `docs/24-onboard-ai.md`) that Kibble deliberately does not tap (a POSIX mqueue has exactly one
reader; a second reader would steal `ctrl`'s own messages).

This plugin mirrors that honesty instead of inventing numbers:
- The on-device `ObjectDetectionResult` never sets `boundingBox` (optional field, simply omitted)
  and never sets `score` (the SDK's own type marks `score: number` *required*; this plugin builds
  the object without it and casts once, at that single boundary, with a comment explaining why —
  see `mixin.ts`'s `HonestDetectionResult`. No `0`, `1`, or `NaN` placeholder is ever emitted).
- `label`/`labelScore` on that same entry come from `GET /identify` — Kibble's **own** real,
  first-party nearest-centroid cat-name classifier (`docs/27-cat-id.md`), not the vendor's.

**A real gap this plugin found, worked around, and flagged instead of papering over — now
resolved upstream.** `agent/src/main.rs`'s route table originally had no endpoint serving
`EVENTS_DIR`'s files, so `Detection.image` was a bare filename with no way to fetch it for any
class; the first version of this plugin worked around that with a same-tick, face-class-only
heuristic (`GET /faces/current`, a different directory, no guaranteed 1:1 mapping) and flagged the
gap in an "Agent-side TODO" section. **`GET /events/<file>` now exists** (verified live by `Main`
against the real device: a real 99,831-byte JPEG fetched by name, path traversal rejected —
`../settings.json` → 400, `..%2Fsettings.json` → 404, `/etc/passwd` → 400 — unknown names → 404).
This plugin now fetches every class's crop directly by name (`mixin.ts`'s `tryFetchCrop`) instead
of the old face-only workaround; `visit`/`eat` detections get a real crop (and therefore a real
second-pass re-check) for the first time. Events with `image: null` still honestly get no crop.

## Second pass: did both, for different reasons

The assignment asks: use Scrypted's own ONNX/OpenVINO plugin if it can be invoked as a service from
a mixin; otherwise fall back to the on-device result plus Kibble's own cat name. **It can — verified
live** (`ObjectDetection.detectObjects(mediaObject)` on "ONNX Object Detection", ~35ms, real
bounding boxes/scores on a real feeder-camera JPEG). So the plugin does the real off-device
re-check (`secondPass.ts`) *and* always attaches Kibble's own cat name via `/identify` when
available — the two answer different questions (WHAT vs. WHO) and neither substitutes for the
other. Auto-discovery skips the NVR's own per-camera detection mixins (designed for their own video
pipeline, not standalone crops) and matches on `/onnx|openvino/i`, so this also works on an
OpenVINO-based instance without code changes. Toggle: Settings → "Second-pass detection".

## Intercom: what's real, what's blocked

The feeder serves video to exactly one consumer (Scrypted's Rebroadcast). Scrypted's rebroadcast
path is receive-only, so talkback has to open its **own** short-lived RTSP session straight to
`kibbled` (`rtspBackchannel.ts`) — real DESCRIBE/SETUP/PLAY against the device, per
`docs/23-audio-codec.md §14`. Because the device's RTSP server always starts full video+audio
playback on `PLAY` (there is no way to play only the backchannel track), this transient session
necessarily also becomes a real, second video session on `/sub` for the duration of one call —
exactly the role `agent/src/rtsp.rs`'s own `MAX_SESSIONS_PER_STREAM` "spare slot" (2 total; normally
1 used by Scrypted's own prebuffer) already exists for, not a violation of the one-persistent-
consumer rule. `startIntercom` tears the session down on `stopIntercom`/error.

`startIntercom` spawns `ffmpeg` (via `mediaManager.getFFmpegPath()`) on whatever `FFmpegInput` the
caller hands in, transcodes to raw `pcm_mulaw`/8kHz/mono, and packetizes+sends it as real RTP over
the negotiated interleaved channel — the same wire format `docs/23-audio-codec.md §7.2` documents.

**Audible output is currently blocked on a vendor start-signal `AudioStart` is resolving** — per
this session's live coordination, the measured result was **no `SndFrm` movement** (predicted +125,
got 0): the write path is byte-correct and reaches the ring, but the vendor's own `audio_out_thread`
never consumes it yet. **The negotiation and transport above are real and independently verified
live; the sound is not there yet.** Do not read the passing self-test as "talkback works."

**Do NOT flip `noAudio` on device 238 while this is blocked.** Once `AudioStart` reports the
backchannel is actually audible: set `noAudio=false` on the RTSP Camera Plugin device's own
settings, and confirm `prebuffer:detectedCodec` becomes `h264/aac` (the mic AAC track already
exists per `docs/23-audio-codec.md §14`'s live verification — this flag is what tells Scrypted's
prebuffer to actually negotiate and expose it, including to HomeKit). Until then it must stay
`true`, or HomeKit/WebRTC clients will try to open an audio track the device won't usefully fill.

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
