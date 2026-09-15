# Cat identification: NPU embeddings + a self-trained classifier

Status: implemented, unit-tested, cross-compiled clean; embedding extraction verified live on
the real device. Read this document for exactly what is measured vs. inferred before trusting
any specific number out of it.

## Design (why this shape)

Retraining or replacing the vendor's on-device face model was rejected outright: that needs
Ingenic's proprietary Magik toolchain and risks bricking a device with no easy recovery path.
Instead:

1. **Frozen feature extractor.** The vendor's own `petkit_face_rec_mtl_s2_v5_sim.axmodel` (a
   512-float re-ID embedding model, `docs/12-ai.md` §3.1) is run unmodified, via the proven
   second-process NPU path (`docs/18-npu-confirmed.md`): a second, independent process can
   call `AX_ENGINE_*` while `media` keeps its own eight engine handles open, with zero crash,
   zero leak, live-verified. `kibbled` itself is a static musl binary that cannot link the
   device's glibc `libax_engine.so`/`libax_sys.so` directly, so that second process is a small,
   separate, dynamically-linked helper: `tools/kibble-embed.c` (build/deploy: `tools/README.md`).
   It decodes a JPEG crop (vendored `stb_image.h`, JPEG-only), resizes to the model's confirmed
   `224x224x3` UINT8 NHWC input, runs it, and writes the raw `512xf32` embedding (+ the model's
   own `prob` scalar) to stdout as flat little-endian bytes. `agent/src/embed.rs` shells out to
   it once per crop.
2. **Nearest-centroid classifier, pure Rust, zero crates** (`agent/src/catid.rs`). Each enrolled
   cat is one running centroid (sum of its L2-normalised sample embeddings); a query is ranked
   by cosine similarity against every centroid; the top score wins if it clears an accept
   threshold, else the crop is `unknown`. O(1) per label, O(cats) per query — microseconds, no
   dependency, matching the project's own "zero new crates" constraint.
3. **Threshold measured from real data, not guessed** (`catid::derive_threshold`).
   Leave-one-out cross-validation: every cat with ≥2 samples contributes a "genuine" score
   (itself vs. a centroid of its *other* samples); every sample also scores against every
   *other* cat's full centroid ("impostor" scores). The accept threshold is the midpoint of the
   tightest observed margin, `(min(genuine) + max(impostor)) / 2`. This needs ≥2 cats and ≥1 cat
   with ≥2 samples; `agent/src/faces.rs::apply_threshold` recomputes it after every label/unlabel
   and logs plainly when it can't run yet, rather than inventing a number.
4. **Embeddings cached, not recomputed.** Each crop gets a `.emb` sidecar (same stem, raw
   `512xf32`) alongside its `.jpg`, computed once (eagerly at capture time by `ai.rs`, or lazily
   the first time anything needs one that predates the feature) and moved alongside the crop by
   `label`/`unlabel`.
5. **Two reserved buckets**, `other`/`not_a_cat` (`faces::SKIP_BUCKET`/`NOT_A_CAT_BUCKET` --
   names this project's own `faces.rs` doc comment already anticipated before any of this
   existed): a crop labelled into either still leaves the review queue and keeps its embedding
   on record, but is excluded from `GET /cats` and never trains the classifier.

## What is measured vs. inferred

**Measured, live, on the real device (192.168.4.85) this session:**

- `kibble-embed` cross-compiles cleanly against the device's own pulled `libax_engine.so`/
  `libax_sys.so` (link-time symbols only), with a maximum referenced `GLIBC_2.4` symbol version
  -- comfortably under the device's real glibc 2.25 (an earlier build accidentally pulled in
  `GLIBC_2.29` via `stb_image.h`'s unused HDR/`pow()` code path; fixed by also defining
  `STBI_NO_LINEAR`, see `kibble-embed.c`'s comment -- this is exactly the "too-new toolchain"
  failure class `docs/18-npu-confirmed.md` §5 already documented, caught here at the
  symbol-version level before it could ever fail on-device).
- **Embedding determinism, on a real photo from the feeder's own camera.** No cat visited the
  feeder during this session (`/opt/kibble/faces/pending/` was empty throughout, and
  `/tmp/saveFace.jpg` did not exist -- checked directly, not assumed), so there was no real
  vendor-flagged face crop to test against; a genuine face crop and a full camera frame are
  processed identically by this pipeline regardless (both are just a JPEG resized to
  `224x224x3`), so a real, live frame was pulled from `kibbled`'s own already-safe, read-only
  RTSP `:8554/sub` endpoint (the same interface Scrypted already consumes) with `ffmpeg`, giving
  a genuine 144,138-byte JPEG of the actual feeder scene. `kibble-embed` was deployed to
  `/opt/kibble/kibble-embed` and run against that exact file **twice**, in two independent
  process invocations: both produced a 2,052-byte output (2048 bytes of `feat` + 4 bytes of
  `prob`, the exact expected shape), and the two outputs are **byte-for-byte, MD5-identical**
  (`5a33444f75c9d024b8b159aa17bf1f8e` both times; `cmp` also confirms). Vendor processes
  (`media`/`ctrl`/`watchdog`/`agora`/`cloud`) were confirmed still running throughout, undisturbed.
- The deployed cat-id `kibbled` build starts cleanly, correctly initialises `faces::Gallery` at
  startup (log line confirms it detected the one pre-existing test artifact -- a 27-byte
  placeholder file from an earlier session's manual endpoint test, not a real photo -- failed to
  decode as JPEG, logged the failure plainly, and continued rather than crashing), correctly
  reports `NotEnoughCats` for the threshold (accurate: zero real samples exist), and binds both
  its HTTP and RTSP listeners.
- `cargo test` (host target): **231 passed, 0 failed**, including 15 new `catid` tests (centroid
  update, cosine ranking, unknown/threshold behaviour, re-label correction -- all on synthetic
  embeddings, no real numbers claimed), 6 new `embed` tests (subprocess plumbing: spawn error,
  nonzero exit, bad-length output, argv wiring, well-formed decode -- against fake `/bin/sh`
  stand-ins, not the real NPU), and updated/new `faces`/`ai` tests for the sidecar move,
  `unlabel`, and the two review/identify target orderings.
- A clean `armv7-unknown-linux-musleabihf` release cross-compile links with zero errors (same
  pre-existing warning set as before this change -- nothing new).
- The HA integration: `python3 -m py_compile` succeeds under Python 3.13 (this repo's own
  `type` alias in `coordinator.py` needs 3.12+); the full existing pytest suite plus 16 new
  tests for this feature (the label-select's option-to-bucket mapping and its guard against
  acting with nothing pending, the pending-face image's cache-busting URL construction, and the
  presence-window boundary) -- **57 passed, 0 failed** in total.

**Attempted but inconclusive this session:** a live HTTP round-trip of the new endpoints
(`/cats`, `/identify`, `/faces/current`, ...) against the deployed test build. The binary was
confirmed running (`ps`, accumulating CPU time) after a clean startup, but a session collision
with a concurrent peer on the device's single-PTY telnet, compounded by this device's
previously-documented telnet flakiness under its own memory/CPU pressure
(`docs/18-npu-confirmed.md` §8 item 3 hit the same thing), left every subsequent connection
attempt timing out with no output at all -- including the command meant to restore the original
`kibbled` binary. **This was flagged to `Main` directly and immediately** (with the exact
recovery command and the original binary's md5, `3f318ad5310870fa246f1e4d7861ffc8`, backed up at
`/opt/kibble/kibbled.pre-catid-test`) rather than left undisclosed. Whoever gets the next clean
session should confirm the restore landed before relying on the device.

**Real classification accuracy: cannot be measured yet, and no number is invented here.** The
only "labelled" crop on the device is the one artifact above, which is not a real photo. Once
Nitin has labelled a real, mixed set (see the cold-start note below for the very first phase),
the honest measurement to run is exactly `catid::derive_threshold`'s own leave-one-out
methodology, exposed for inspection rather than just consumed internally: call it directly (or
add a debug endpoint that returns its `ThresholdReport`) over the real labelled set and read
`min_genuine`/`max_impostor`/`separable` -- if `separable` is `true` and the margin is wide, the
threshold is trustworthy; if not, more labelled samples per cat are needed before treating
identifications as confident.

## Cold start: one cat, three photos

With exactly one enrolled cat, `derive_threshold` returns `Err(NotEnoughCats)` on every call
(there is no "someone else" yet to measure a false-match rate against), so `Classifier::t_accept`
stays at `catid::DEFAULT_T_ACCEPT` (`0.5`) -- a generic, **uncalibrated** cosine-similarity
midpoint, not a number derived from this face model or this device's embeddings in any way. In
that regime `/identify` will report `Known` for that cat whenever a query's cosine similarity to
its centroid happens to clear `0.5`, which for a well-separated frozen embedding is often true
for genuine visits but is *not a validated guarantee* -- there is no impostor class yet to
confirm the model doesn't also clear `0.5` for an unrelated face. With three photos of one cat,
the centroid is the mean of three samples; `GET /cats` will show `samples: 3`. Practically:
**treat every identification during this phase as a guess worth spot-checking, not a fact** --
exactly what `sensor.plant_room_cat_feeder_last_seen_pet`'s own doc comment says. The moment a
second cat is enrolled with ≥2 samples, `derive_threshold` starts running for real and
`t_accept` reflects an actual measured margin from then on.

## Live detection events: still gated on replacing `ctrl`

`GET /events`'s `score`/`pet_id`/`box` fields remain honestly `null` for the same reason
`docs/24-onboard-ai.md`/`ai.rs`'s own module doc already established: that data exists only in a
message delivered to `ctrl`'s private mqueue inbox, and this project's constraints forbid a
second reader stealing it. **This work does not change that.** What it adds is a **new, real**
field: `Detection.cat`, Kibble's own classifier's opinion (not the vendor's), populated whenever
a `"face"`-class crop is captured, embedded, and confidently matched -- `null` whenever the
classifier isn't confident, including "no cats enrolled yet". `GET /identify` is the more useful
surface for "who was just here" since it always reflects the current classifier state (including
a just-applied relabel), while `/events`'s `cat` is fixed at capture time.

## Agent surface

| Endpoint | What it does |
|---|---|
| `GET /cats` | `[{"name","samples","last_seen"}]`, sorted by name; reserved buckets excluded |
| `POST /cats {"name"}` | Pre-register a cat with zero samples (for the label select's options) |
| `GET /identify` | `{"cat","score","second_best","crop","source","ts"}` -- newest pending crop's classifier guess, or ground truth from the most recently labelled one once the queue is empty (`source` distinguishes the two) |
| `GET /faces/current` | Raw JPEG: oldest pending crop, else most recently labelled (review queue, FIFO) |
| `GET /faces/current/info` | `{"status","name","cat"}` metadata for the image above |
| `POST /faces/label {"name","cat"}` | Existing endpoint, now also updates the live centroid and re-measures the threshold |
| `POST /faces/unlabel {"name","cat"}` | Exact inverse: moves a crop back to pending and corrects the centroid; a full re-label is this followed by another `label` |

## HA surface

- `image.plant_room_cat_feeder_pending_face` -- the review-queue crop, cache-busted on
  status+name so a new crop actually refreshes.
- `select.plant_room_cat_feeder_label_face` -- cat names + "Skip"/"Not a cat"; acts on the
  crop `image.*_pending_face` is currently showing.
- `sensor.plant_room_cat_feeder_last_seen_pet` -- enabled, PRIMARY (matches the name
  `docs/design-entities.md` already anticipated).
- `sensor.plant_room_cat_feeder_identification_score`, `sensor.plant_room_cat_feeder_pending_faces`
  -- both DIAGNOSTIC, `entity_registry_enabled_default=False` (house rule 4).
- `binary_sensor.plant_room_cat_feeder_<cat>_present` -- one per enrolled cat, created
  dynamically from `GET /cats` as new cats appear; PRIMARY, enabled. "Present" is a 15-minute
  latch after the last confident identification -- an implementation choice (there is no
  continuous live-detection feed to derive real dwell time from, per the section above), not a
  device-measured value.
- Services: `kibble.label_face {device_id, crop_id, cat}`, `kibble.add_cat {device_id, name}`,
  `kibble.identify {device_id}` (returns the identify result as response data and refreshes).
- Every entity/service name is sentence-case, capability-only (house rule 9 -- never repeats
  "Cat Feeder" or an area), has a `translation_key` and an `icons.json` entry, and
  `strings.json`/`translations/en.json` are kept byte-identical per this repo's existing
  convention.

Nothing was deployed to the HA VM and HA was not restarted -- per the assignment, `Main` owns
that single restart once every branch is merged.
