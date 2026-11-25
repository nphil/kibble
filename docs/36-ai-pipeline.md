# 36. Cat detection, identity and evidence: pipeline v2

Status: design of record, 2026-09-24. Supersedes the device-side gallery, pending-face queue and
device classifier described in 27-cat-id.md and librefeed/docs/05-vision.md "Cat identification".

## Why v2

Measured on the live feeder, 2026-09-24:

- `/opt` (80 MB UBIFS) filled to 100% twice. Writers: full-resolution before/after JPEGs
  (~550 KB each), event scene/face crops, the face gallery and a 200-crop pending queue.
  Nothing on the device handles ENOSPC, so once full every new event lost its evidence.
- The last 50 events had no scene, no face, and identity `not_evaluated / no_face`.
- 199 pending face crops sat on the device with no usable review flow.
- Identity used only the vendor face embedding. Pancake is black: the face detector rarely
  fires on her, so most of her visits cannot be named from a face at all.
- `last_seen_pet` came from `/identify` (latest crop, no margin gate) while the per-cat rows
  came from track naming, so the UI contradicted itself.

## Ownership

| Layer | Owns | Does not own |
|---|---|---|
| Feeder (librefeedd) | Detection, visit/eat tracking, capturing evidence, a bounded transient spool, feeding | Identity, training data, review, long-term storage |
| HA integration | Evidence archive, event journal, identity engine, training set, retention, entities | Real-time detection |
| Card | Presentation and labelling UI | Any business logic beyond grouping for display |

The feeder is a sensor. Home Assistant is the system of record.

## Data flow

```text
camera -> media (NPU): bodies[+224px body crop], faces[+224px crop, 512-D embedding], stream-5 scene
  -> librefeedd Track: open (2 frames + motion), eat (bowl overlap >= hold), close (5 s absent)
       captures up to 6 samples per track + 1 scene frame into /opt/librefeed/spool (hard cap)
  -> push/poll: GET /events rows reference spool assets
  -> HA ingest: fetch assets -> write /config/kibble/<entry_id>/media/... -> SQLite row
       -> DELETE /events/<asset> (ack) on the feeder
       -> identity engine classifies the track -> entities + WS subscribers update
  -> card: kibble/timeline, kibble/cats, kibble/review/photos, labelling via kibble/sample/label
  -> a label is saved and acknowledged at once; the debounced learning job then retrains and
     events inside retention are re-classified
```

## Device contract (feeder HTTP, port 8765)

### Spool

- Directory `/opt/librefeed/spool/`. Every transient evidence file lives here and nowhere else:
  event body crops, face crops, face embeddings, scene frames, meal and feed before/after frames.
- Hard cap `SPOOL_CAP_BYTES = 8 MiB`, plus a free-space floor: never write when `/opt` free
  space would drop below 4 MiB. On pressure evict oldest spool files first. A failed or skipped
  write leaves the row's reference `null`; it never aborts tracking or journaling.
- Before/after and scene frames come from the low-resolution stream-5 scene cache, never
  full-resolution stream-2 snapshots.
- `GET /events/<name>` serves a spool file (JPEG, or `application/octet-stream` for `.emb`).
- `DELETE /events/<name>` is HA's acknowledgement; the file is removed. 404 when absent.
- `GET /spool` -> `{"used_bytes": n, "cap_bytes": n, "files": n, "evicted_total": n, "opt_free_bytes": n}`.

Filenames: `e<event_id>-scene-<k>.jpg` (`k` = the producing sample's own serial) or
`e<event_id>-scene-eat.jpg` (eat-start fallback, no sample ever produced one -- see below);
`e<event_id>-s<k>-body.jpg`, `e<event_id>-s<k>-face.jpg`, `e<event_id>-s<k>-face.emb` (512
little-endian f32, L2-normalised), `f<feed_id>-before.jpg`, `f<feed_id>-after.jpg`,
`e<event_id>-before.jpg`, `e<event_id>-after.jpg`. `scene` is rewritten under a NEW filename
every time a better sample supersedes the last one (docs/40-vision-judge.md, Contract 4); the
superseded spool file is removed the instant that happens. Rows written before this change keep
the older fixed `e<event_id>-scene.jpg` name, overwritten in place -- still readable, never
migrated.

### `GET /events` and push `events` body

Newest first, at most 256 rows, open tracks included. Row:

```json
{
  "event_id": 1201, "seq": 6810, "ts": 1790287729, "end": 1790287760, "open": false,
  "class": "visit",
  "eat_start": null,
  "scene": "e1201-scene-3.jpg",
  "samples": [
    {"k": 1, "t": 1790287731, "box": [x1, y1, x2, y2], "score": 0.93,
     "body": "e1201-s1-body.jpg",
     "face": {"jpeg": "e1201-s1-face.jpg", "emb": "e1201-s1-face.emb", "score": 0.81}}
  ],
  "image": "e1201-s1-body.jpg",
  "image_before": null, "image_after": null
}
```

- `class` is `"visit"` or `"eat"`. `end` is null while `open`.
- Legacy journal rows written before v2 are served in this same shape: `samples` holds one entry
  built from the legacy face crop and embedding when both exist, else `[]`; `scene` is the legacy
  scene or image name. Their assets keep their legacy names.
- `samples` holds up to 10 entries spread across the whole track: the first sample, then a new
  one when a face beats every kept face, at least 4 s have passed, or the subject visibly changed;
  at the cap the least valuable interior sample is replaced. A frame is only sample-worthy when
  its selected subject covers at least 4% of the frame and clears `body_threshold`. Frames seen
  before the track is motion-verified are not dropped: the best sample-worthy one is held in
  memory and committed, with its own `t`, the moment the track verifies (2026-09-25, track 1494).
  `face` is null when no face was detected in that frame. Any asset field may be null when its
  write was refused by the spool guard or its file was already acknowledged.
- `scene` is the full frame of the best sample so far, named after the sample that produced it
  (`e<event_id>-scene-<k>.jpg`) -- it can be, and often is, rewritten more than once across a
  track's own lifetime as a better sample arrives; `/events`' `scene` field always names the
  CURRENT file. HA's ingest treats a changed name for an already-known event as a replacement:
  it fetches the new asset, updates the row, and removes its own superseded copy
  (docs/40-vision-judge.md, Contract 4). An `eat` track whose subject never produced a sample
  still gets a `scene`: the eat-start frame, named `e<event_id>-scene-eat.jpg`, patched onto the
  row at close. A `visit` without samples has none. An `eat` that closes with zero samples logs
  its last 32 frames' admission trace (area, score, moved, verified, bowl overlap, outcome) to
  syslog.
- `image` is kept for the Scrypted plugin: the best sample's body crop.
- Removed from rows: `cat`, `cat_score`, `identity`, `review`, `evidence` and every legacy field.

### Removed device surfaces

`/faces/*`, `/cats*`, `/identify*`, `/events/review`, `/retention`, the gallery, the pending
queue, `catid.rs`, `faces.rs`, vision config keys `naming`, `identify_min_score`,
`identify_min_margin`, and push bodies `cats`, `identify`, `review_face`, `pending_faces`.
`/opt/librefeed/faces/` is deleted after HA has imported it (see Migration).

### `GET /feeds`

Unchanged shape except `before`/`after` name spool assets (`f<id>-before.jpg`).

## HA storage

Root: `/config/kibble/<entry_id>/`

```text
kibble.db                     SQLite (WAL); schema below
media/<YYYY-MM-DD>/<asset>    archived evidence; deleted by retention
training/<cat_slug>/<id>.jpg  copies of labelled samples; never removed by retention
```

Retention option (options flow): 7, 14, 30 or 90 days, default 14. Runs at startup and hourly.
Deletes events, their samples and their media older than the cutoff, except samples that were
copied into training. Guard: if `media/` exceeds 2 GiB, delete the oldest days until under 90%
of the cap. Training has its own cap of 400 samples per cat, oldest auto-added samples first.

### Schema (v1)

```sql
events(uid TEXT PRIMARY KEY, device_event_id INT, kind TEXT, start INT, end INT, open INT,
       eat_start INT, scene TEXT, before TEXT, after TEXT,
       cat TEXT, identity_status TEXT, confidence REAL, reviewed INT, updated INT)
samples(uid TEXT PRIMARY KEY, event_uid TEXT, t INT, body TEXT, face TEXT,
        face_emb BLOB, body_feat BLOB, face_feat BLOB, mode TEXT,
        guess TEXT, guess_confidence REAL,
        box_x1 REAL, box_y1 REAL, box_x2 REAL, box_y2 REAL, score REAL)
training(uid TEXT PRIMARY KEY, cat TEXT, source TEXT, created INT, body TEXT, face TEXT,
         face_emb BLOB, body_feat BLOB, face_feat BLOB, mode TEXT)
feeds(uid TEXT PRIMARY KEY, device_feed_id TEXT, ts INT, portions REAL, hopper INT,
      scheduled INT, confirmed INT, before TEXT, after TEXT)
cats(name TEXT PRIMARY KEY, color INT, created INT)
meta(key TEXT PRIMARY KEY, value TEXT)
```

`uid` values are `<entry>-e<device_event_id>-<start>` style strings, stable across feeder reboots
that reset event ids.

### Schema v10 (multi-cat sessions)

Schema v9 adds `events.scene_k`/`events.subjects` and `samples.sid`/`frame_k`/`is_primary`/
`bowl`/`frame_boxes` for device evidence. Schema v10 adds `samples.review_src` to distinguish a
person's per-photo answer from a subject-wide answer; legacy `NULL` values remain protected as
photo reviews. Session parts, planning, migration, and the full WebSocket contract are in
`docs/42-multi-cat.md`.

## Identity engine (HA, `identity.py`, numpy + Pillow only)

Classes: every enrolled cat plus `not_a_cat`.

Features per sample:

1. `face_emb`: the device's 512-D embedding when a face exists.
2. `body_feat`: appearance descriptor computed in HA from the body crop.
3. `face_feat`: the same appearance descriptor computed from the face crop.

Appearance descriptor: center-weighted histograms over the crop resized to 96x96.
`mode = "ir"` when mean chroma is below a fixed threshold (night vision), else `"day"`.
Day: joint HSV histogram (8 hue x 3 sat x 4 value) plus a 16-bin luminance histogram plus an
8-bin gradient-orientation histogram. IR: 16-bin luminance plus 10-bin uniform LBP plus
gradient histogram. Every block L1-normalised, whole vector compared with Hellinger distance.
Samples only compare against training samples of the same mode.

Per modality, a distance-weighted k-NN (k = 5) gives class scores. Per track, per-sample class
log-probabilities are averaged across all samples and modalities available.
Decision: `cat` when the top class probability >= 0.75 and beats the runner-up by >= 0.2,
`not_a_cat` with the same gates, otherwise `unknown`. Thresholds live in one constants block.

Leave-one-out accuracy per cat and modality is computed after every training change and shown in
the Cats card, so "trained well enough" is a measured number, not a guess.

Human review always wins: a reviewed event keeps its label; the engine never overwrites it.

Since 0.27.0, an optional second backend (`coral_identity.py`) can replace this per-event when a
CoralHub server is configured, falling back to the histogram engine above automatically when it
is not configured or unreachable -- see `docs/41-coral-recognition.md` for the full design and
the live-data comparison that justified turning it on.

## Learning (0.29.7)

Every label used to start a full relearn on the one store thread: model rebuild, an O(n^2)
leave-one-out, a re-check of about 1,400 events and a snapshot. Labelling five visits quickly meant
92-129 s of work, `kibble/cats` waited 10-38 s behind it and saves failed. Measured on one relearn
(about 13 s): Coral leave-one-out 42%, histogram leave-one-out 20%, verdict writes 13%, Coral
network about 2%. Now a label is saved and acknowledged at once and learning runs as a separate job
(`learning.py`, `LearningManager`, `coordinator.learning`):

- **Generation counter.** The store bumps a persisted `learning.gen` in the same transaction as any
  training-gallery change, then tells the manager (`notify(gen, answers)`) on the event loop. No
  caller has to tell the manager anything, so no path can forget.
- **Debounce.** A pass starts 20 s (`QUIET_S`) after the last change, but never later than 120 s
  (`MAX_WAIT_S`) after the first one. Both are constructor arguments so tests can shrink them.
- **Single-flight, follow-up pass.** Only one pass runs at a time; changes that arrive during a pass
  schedule one more pass with a fresh quiet window.
- **A pass:** reconcile queued sessions, enforce the training caps (label writes no longer do),
  read the target generation, `engine.async_rebuild(gen=target)` (skipped when `built_gen` already
  equals it), `engine.async_reclassify_unreviewed(cutoff, progress)`,
  `store.async_learning_complete(gen, answers, result, counts)`, identity snapshot refresh.
  `events_for_reclassify` returns top-level events only: child parts of a split session are rebuilt
  from the parent (re-checking a child un-split the session and every pass re-split it).
- **Failure and retry.** An exception, or an unreachable CoralHub (`async_rebuild` returns an error
  string), sets state `retrying`, backs off 30 s doubling to 900 s and stores the text in
  `learning.last_error`; success clears it. A new answer while retrying does not wait out the whole
  back-off: a pass runs after the usual quiet window, but never later than the retry already due.
- **Restart.** At start, a pass is scheduled when `gen > done_gen` or the learning queue is not
  empty. `async_stop()` runs first on unload, cancels the worker and leaves the durable state dirty,
  so the next start resumes. Nothing is lost.
- **Reads never wait.** `kibble/learning` is answered from memory and `kibble/cats` is a single store
  call (`async_cats_overview`; its live `spool_stats()` is bounded to 0.75 s, falling back to the
  last known value). The `clear_training` service no longer rebuilds inline either.

Status payload (WebSocket result, subscription event, and the one diagnostic
`sensor.<device>_learning`, whose state is `state` and whose attributes are the other fields):

```ts
type Learning = {
  state: "idle" | "waiting" | "learning" | "rechecking" | "retrying";
  pending: number;           // answers saved but not yet learned
  done: number | null; total: number | null;   // re-check progress; null until the first tick
  starts_at: number | null;  // unix seconds, while waiting
  retry_at: number | null;   // unix seconds, while retrying
  started: number | null;    // unix seconds the running pass began
  last: null | { finished: number; duration: number; answers: number;
                 learned: Record<string, number>;   // cat -> +/- photos
                 renamed: number; rechecked: number;
                 stages: Stages };
  error: string | null;
};

type Stages = {                // milliseconds spent in each stage of the last pass
  reconcile_ms: number; caps_ms: number; state_ms: number;
  rebuild_ms: number;          // all of the model rebuild: histogram_ms plus the coral_* stages
  histogram_ms?: number;       // present once a rebuild ran
  coral_health_ms?: number; coral_embed_ms?: number; coral_load_ms?: number;
  coral_build_ms?: number; coral_loo_ms?: number;   // only with CoralHub configured
  rebuild_skipped?: true;      // the models already matched the generation, nothing rebuilt
  coral_requests?: { calls: number; crops: number; ms: number; reused: number };
  recheck_ms: number; snapshot_ms: number;
};
```

The sensor is diagnostic and disabled by default. History keeps only its state (every attribute is
unrecorded), and it rewrites itself only when the state, the number of waiting answers, the error
or a finished pass changes: progress ticks do not, the other attributes catch up on the
coordinator's next refresh. It stays available while the feeder is down.

`set_event_classification` no longer bumps `updated` for an unchanged verdict and returns True only
when the name changed.

## HA entities

Per enrolled cat (created and removed with the roster):

- `binary_sensor.<device>_<cat>_present` (existing unique id `{serial}_cat_present_{slug}`):
  on while an open event is identified as that cat. Attributes `last_seen`, `last_ate` (ISO).
- `sensor.<device>_<cat>_last_seen` (timestamp), `sensor.<device>_<cat>_last_meal` (timestamp),
  `sensor.<device>_<cat>_meals_today` (count, `total_increasing` not used; plain measurement).

Device-wide: `sensor.*_last_seen_pet` = cat of the newest identified event (same engine as the
rows), `image.*_last_detection` = its best body crop.

Removed: `select.*_label_face`, `image.*_pending_face`, `sensor.*_pending_faces`,
`sensor.*_identification_score`, services `label_face` and `unlabel_face`.

## HA WebSocket API (card contract)

All commands take `entry_id`. Assets are `{"id": "<asset>", "url": "/api/kibble/<entry_id>/media/<asset>"}`
fetched with `hass.fetchWithAuth`. Media responses are immutable (`Cache-Control: private, max-age=31536000, immutable`).

- `kibble/timeline` `{limit?: 1..100 = 30, cursor?}` ->
  `{items: TimelineEvent[], cursor: string|null, has_more: bool}` newest first. Shows only
  `visit`, `eat` and `feed` (never `import`, never a `not_a_cat` verdict). Thumbnail selection
  per event: drop any sample the classifier itself called `not_a_cat`, drop a box under 4% of
  frame area or clipped at the frame edge (skip the box gate when the device sent no box at
  all), then prefer a sample with a face, then the highest detector score. Computed fresh on
  every read, never cached, so a later reclassification is reflected immediately. A `visit`
  with no sample surviving that is not shown at all (it still exists for `kibble/review/photos` and
  ages out under retention normally); an `eat` always shows something -- its best surviving
  crop, or the scene frame if none survives.
- `kibble/timeline/subscribe` `{}` -> subscription; each message `{changed: true}`; client refetches.
- `kibble/event` `{uid}` -> `{event: TimelineEvent, samples: Sample[]}` (full detail for the sheet).
- `kibble/label` `{uids: string[], label}` where `label` is a cat name, `"not_a_cat"` or
  `"unknown"`. Marks every existing event in `uids` reviewed with that label and returns
  `{events: TimelineEvent[]}` immediately -- no file I/O on this path. For a cat or not_a_cat
  label (never `unknown`), reconciling every one of those events' samples afterward -- a
  sample with no override of its own (`review IS NULL`) adopts the new label, a `"skip"`
  override stays untrained, any other override is left exactly alone. The label is saved and
  acknowledged at once; the model rebuild and re-classification happen later in the learning job,
  followed by a `kibble/timeline/subscribe` push once that settles.
- `kibble/review/photos` `{entry_id, cursor?, limit?: 1..60 = 24}` replaces the old `kibble/review`
  (events needing review). Result `{items: [{sample, visit: {uid, start, kind, scene, in_scene}}],
  remaining, visits, cursor, has_more}`: one photo per card. Keyset-paged, so the cursor stays
  valid while photos are answered (an answered photo just disappears), and it never depends on a
  relearn. At most 3 photos per visit: different from each other and from the training gallery,
  unsure ones first, good crops only, newest first.
- `kibble/sample/label` `{entry_id, sample_uid, label}` where `label` is a cat name, `"not_a_cat"`,
  `"both"` ("Both in this photo", only with exactly two enrolled cats, never trained), `"skip"`
  ("Don't use") or `"follow"` (undo: back to no answer). Replies
  `{sample, visit: {uid, kind, cats: [{cat, ate}], needs_ate}}` at once;
  training changes are learned later by the debounced learning job (see "Learning"). Repeating the
  same answer is a successful no-op. Only the photos the person answers train in a photo-driven
  visit; a per-photo `not_a_cat` trains the `not_a_cat` class (still vetoable).
- `kibble/session/ate` `{entry_id, uid, ate: string[]}` -> `{visit}`: who ate, asked only when a
  meal's photos name two cats and the feeder's per-subject data cannot tell. `agent_rejected` for an
  empty list or a cat not in the visit, `not_found` for an unknown visit. `kibble/event` adds
  `event.needs_ate`.
- Legacy, kept and idempotent: `kibble/label` and `kibble/session/label` (visit-level answers; they
  keep the old follow-photo training). The visit's result (who visited, who ate, title) is derived
  server-side from the photo answers; nobody answers for a bundle.
- `kibble/learning` `{entry_id}` -> learning status (see "Learning"), served from memory.
  `kibble/learning/subscribe` `{entry_id}`: result first, then at once one event `{learning: status}`,
  then one per change (progress pushes throttled to about 2 a second).
- Error codes: `not_found` and `agent_rejected` are terminal; everything else is transient.
- "Can't tell" (`unknown`) at visit level and a per-photo `skip` both guarantee that none of that
  visit's / photo's photos stay in the training gallery (the `<sample_uid>-train` rows and their
  cached Coral embeddings are removed) and auto-learn never re-adds them (it skips reviewed events).
- `kibble/cats` `{}` -> `{cats: CatSummary[], storage: Storage}`.
- `kibble/cats/add` `{name}`, `kibble/cats/delete` `{name}`.
- `kibble/training` `{cat, limit?: 1..60 = 24, cursor?}` -> `{items: TrainingSample[], total, cursor, has_more}`.
- `kibble/training/remove` `{uids: string[]}` -> `{removed: n}`.

```ts
type Asset = { id: string; url: string };
type TimelineEvent = {
  uid: string; kind: "visit" | "eat" | "feed" | "import";
  start: number; end: number | null; open: boolean;
  cat: string | null;                       // null = unknown or not applicable (feed)
  identity: "auto" | "reviewed" | "unknown" | "not_a_cat" | null;
  confidence: number | null;                // 0..1, auto only
  thumb: Asset | null;                      // best body crop
  scene: Asset | null;
  before: Asset | null; after: Asset | null;
  sample_count: number;
  feed?: { portions: number; hopper: number | null; scheduled: boolean; confirmed: boolean };
};
type Sample = { uid: string; t: number; body: Asset | null; face: Asset | null;
                guess: string | null; guess_confidence: number | null;
                review: string | null; label: string | null };
type TrainingSample = { uid: string; cat: string; created: number; source: "label" | "import";
                        body: Asset | null; face: Asset | null };
type CatSummary = { name: string; color: number; avatar: Asset | null;
                    training: { total: number; face: number; body: number };
                    accuracy: number | null;           // leave-one-out, 0..1, null under 5 samples
                    last_seen: number | null; last_meal: number | null;
                    meals_today: number; present: boolean };
type Storage = { used_bytes: number; events: number; retention_days: number;
                 oldest: number | null; device_spool: { used_bytes: number; cap_bytes: number } | null };
```

Since schema v9, `TimelineEvent` includes `session` and `Sample` includes `sid`/`box`/`crop`/
`peers`/`cat`. `kibble/event` returns `EventDetail` for the whole session, including all samples,
`scene_subjects`, `cats`, and `multiple_cats`; there is no v1 `companions` field. V2 commands
`kibble/session/label` and `kibble/session/subject` save the human's session cat set and subject
labels. `kibble/sample/label` remains the per-photo answer. See `docs/42-multi-cat.md` for shapes
and precedence rules.

`not_a_cat` events and a `visit` with no usable thumb (see `kibble/timeline` above) are hidden
from `kibble/timeline` but kept until retention removes them.

## Migration (one-off, run by the operator, not shipped)

1. Back up `/opt/librefeed` (done: `/data/home/KibbleOS/backups/feeder-20260924-1807/opt.tar`).
2. Import the device gallery into HA training: `Kitty`, `Pancake` as cats, `not_a_cat` as the
   reject class. `other` (the old skip bucket) is not imported. Face crops plus `.emb` sidecars.
3. Import the 199 pending crops as events of kind `import` with one face-only sample each.
   `import` events never appear in `kibble/timeline`; they appear in `kibble/review/photos` and age
   out under retention like any other event.
4. Move the existing `.storage/kibble/evidence` archive files into `media/<date>/<name>`
   (date from the leading unix timestamp in the name), then delete `.storage/kibble/`.
   Ingest never re-fetches an asset whose file already exists, so legacy rows the feeder still
   serves resolve to these files.
5. Deploy daemon, then delete `/opt/librefeed/faces/` and `/opt/librefeed/retention.json`.
