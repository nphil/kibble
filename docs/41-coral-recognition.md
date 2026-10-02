# 41. Coral-backed cat recognition

Status: design of record, 2026-09-26. Supersedes nothing -- `identity.py`'s histogram recognizer
stays the default and the permanent fallback; this adds a second, optional backend.

## Why

The 2026-09-25 Coral-vs-histogram benchmark (`/data/home/KibbleOS/backups/kibble-eval/vision-judge-2026-09-25/coral-bench.md`)
found a Google Coral Edge TPU's MobileNet image embedding, paired with a plain nearest-class-
centroid classifier, beat the deployed histogram recognizer (`identity.py`) on its own clean
evaluation set: body crops 0.959-0.973 accuracy vs 0.797, face crops 0.984 vs 0.957. Nitin's own
CoralHub server (for example `http://192.168.1.69:8720`; CoralHub is a separate project, not part of this repo)
already runs the two MobileNet models that benchmark recommended, as a generic inference API any
LAN app can call. This wires Kibble up to it as a second, optional recognizer.

Empty means off (`const.CONF_CORALHUB_URL`): with no URL configured, nothing here runs at all --
zero requests, zero backfill, zero behaviour change from 0.26.2. Filling in the URL (and a
bearer token, if CoralHub has one configured) turns it on; an unreachable CoralHub or an
unrecognized response falls back to the histogram recognizer automatically, per-event.

## Client (`coral_client.py`)

`CoralHubClient`: one instance per config entry, constructed only when `coralhub_url` is set.
Every request carries `X-Client: kibble` plus `Authorization: Bearer <token>` (blank when no
token is configured) -- CoralHub's own named-token feature labels Kibble's calls "Kibble" on its
Live activity dashboard rather than an anonymous container IP.

- `embed(model, images) -> list[list[float]] | None`: batches transparently at CoralHub's own
  32-image cap, runs batches concurrently (bounded), returns one L2-normalised vector per image
  in input order, or `None` for the whole call the moment any one batch fails -- never a partial
  list a caller could misalign.
- `health() -> HealthStatus`: `GET /api/v1/health`, read by `CoralRecognizer.async_rebuild` to
  decide whether Coral is worth attempting this rebuild, and by `diagnostics.py`.
- Failure handling mirrors `judge.py::VisionJudge._call_model` exactly: one retry, `None` on
  total failure, a warning logged once per NEW failure streak and silence for every repeat while
  it continues, reset the moment a request next succeeds.

## Store: embedding cache (schema v8)

New table, `coral_embeddings(row_kind, row_uid, crop_kind, model_id, embedding, created)`,
`PRIMARY KEY(row_kind, row_uid, crop_kind, model_id)`. `row_kind` is `'training'` or `'sample'`;
`row_uid` is that table's own `uid`. An embedding is a pure function of one crop's own bytes and
the model that produced it, so it is written at most once per key (`set_coral_embedding`'s own
`ON CONFLICT ... DO NOTHING`) -- "each image is embedded exactly once". A brand-new table needs
no `ALTER TABLE` dance, unlike every earlier schema bump; `CREATE TABLE IF NOT EXISTS` alone is
idempotent for both a fresh database and an upgraded one.

Deletion cleanup rides along wherever a training row or an event's samples are already deleted
(`_delete_training_row`, `_delete_event`) -- an embedding survives a training row's cat being
changed by a re-label (`_upsert_training_row` moves the FILE, never the row's `uid`), since the
cache key is a property of the crop's own pixels, not of which cat it is currently labelled as.

Crops the gallery filter already excludes are never embedded into the gallery:
`training_rows_needing_coral`/`all_training_coral_features` reuse the exact same
`_training_row_crop_trustworthy` check `all_training_features` already applies for the histogram
recognizer (`docs/36-ai-pipeline.md`'s legacy-crop-shift note) -- a `label`/`auto` row whose
source sample has an untrustworthy crop is skipped from the Coral gallery too, `import`/`upload`
rows are always kept, same as today.

### Backfill

`CoralRecognizer.async_backfill`, started once as an unawaited background task at entry setup
(never blocks setup, same pattern as `vision_judge.ensure_descriptions`): catches up every
training row's and every still-reclassifiable (unreviewed-event) sample's cached embedding,
newest first, in 32-row batches, pacing itself faster while healthy and slower after any failed
batch. Never gives up outright -- an unreachable CoralHub just means slower polling, forever,
until it catches up or the entry unloads. Calls back into `IdentityEngine.async_rebuild` after
each training batch that actually embeds something, so freshly caught-up centroids reach
production without waiting for an unrelated training mutation.

`CoralRecognizer.async_rebuild`'s own bounded (64-row) "ensure embedded" catch-up runs inline on
every ordinary rebuild too (a label, an upload, auto-learn, or the periodic reclassify pass) --
cheap, since a rebuild follows one training action so at most a handful of rows are ever newly
missing an embedding at that moment.

## Recognition (`coral_identity.py`)

`CoralModel`: nearest-class-centroid classifier, mirroring `identity.Model`'s own architecture:
one independent pool per crop kind (body, face) **and per day/ir mode** -- `identity.
to_log_probs`/`identity.fuse_log_probs` are the SAME functions `identity.Model` calls, not a
re-implementation, so both recognizers fuse modalities/samples identically once each modality's
own per-class scores exist. `not_a_cat` gets no special case anywhere: it is just another class
name that may or may not have training data, exactly like `identity.Model` treats it today.

Mode-splitting the centroid pools was not obvious from the original benchmark's own smaller
dataset (which pooled day+ir for Coral and still won) -- it turned out to matter once tested
against the full live DB (see "Calibration" below): pooling day and ir body embeddings together
gave a highly fold-unstable, occasionally worse-than-baseline body accuracy; splitting them,
mirroring `identity.Model`'s own `_body_pools`/`_face_pools` exactly, fixed the instability
outright and pushed body accuracy well past the histogram recognizer in every condition tested.
However lighting-invariant Coral's own embedding may be in isolation, a cat's pooled day+night
centroid is still a worse stand-in for either lighting condition than two separate centroids are.

Body crops use `mobilenet_v1_1.0_224_l2norm_quant_edgetpu` (CoralHub's own imprinting-base
extractor), face crops use `mobilenet_v1_1.0_224_quant_embedding_extractor_edgetpu` -- the
2026-09-25 benchmark's recommended pairing, and the two models CoralHub itself installs
automatically on first start for exactly this reason.

Decision gate: `top_p >= DECISION_TOP_THRESHOLD and (top_p - second_p) >= DECISION_MARGIN`,
identical shape to `identity.py`'s own gate, calibrated independently (see below).

## `IdentityEngine` dispatch (`ingest.py`)

`IdentityEngine.async_classify_event` tries Coral first (when configured) via `CoralRecognizer.
async_classify_event`. That method's own return contract is the whole fallback mechanism:

- Bare `None` means Coral could not answer AT ALL right now -- not configured, no model built
  yet, or CoralHub's last request is known to have failed. `IdentityEngine` falls back to the
  histogram model for that one event.
- A real `identity.Verdict` -- even an inconclusive `label=None` one -- is Coral's own answer
  and is used as-is, never second-guessed against the histogram model. "Falls back... when not
  configured or unreachable" means exactly that: unreachability, not low confidence.

`IdentityEngine.backend` (`"coral"`/`"histogram"`) and `.loo_accuracy(cat)` both key off
`CoralRecognizer.available` (a model exists) rather than live reachability -- deliberately, the
same "two signals can legitimately disagree" shape `coordinator.py`'s own `feeder_reachable` vs.
`last_update_success` already uses, so a momentary blip does not flap a device-wide status back
and forth. The existing recognition-percentage sensors (`autolearn.recognition_score`, driven by
`review_outcomes` -- human agree/disagree history, not which model produced the guess) need no
code changes at all to keep working under either backend: they already read only the `cat`/
`confidence` values `async_classify_event` writes, regardless of which recognizer wrote them.

`async_classify_one` (the upload pipeline's own "does this look like a cat at all" gate,
`views.py`) stays on the histogram model unconditionally: it only ever checks `verdict.label ==
identity.NOT_A_CAT`, and the benchmark found essentially no body-crop not-a-cat training data for
EITHER recognizer (one photo in the whole corpus) -- switching this one gate's backend would not
change its practical behaviour.

## Calibration

`tools/coral_verify.py` reruns this whenever training data has grown meaningfully; its own
docstring has the full usage. Ground truth mirrors the 2026-09-25 benchmark's own definition:
`training` rows with `source in (label, import)`, plus `samples` rows whose own `review` is a
real label or whose event is `reviewed=1` with `identity_status` not `unknown`; crop
trustworthiness uses the DEPLOYED `crop_geometry.is_legacy_crop_trustworthy` exactly (a box-less
sample is trusted, not excluded -- see that function's own docstring). Folds are event-grouped
(one event, or one standalone `import` row, per group), 5-fold, stratified by majority label via
a small greedy assignment (no scikit-learn dependency in this repo).

Run against the live DB, 2026-09-26 (602 ground-truth rows, 241 event-groups; Kitty 386 /
Pancake 157 / not_a_cat 59), evaluating the FUSED decision (whichever modalities each row
actually has, exactly how production classifies a real track) through the real, unmodified
`identity.Model` and `CoralModel` classes against real CoralHub embeddings:

| | baseline (histogram) | Coral (mode-split centroid) |
|---|---:|---:|
| Kitty-vs-Pancake accuracy, all | 0.853 | **0.915** |
| ...day | 0.943 | **0.992** |
| ...ir (night) | 0.779 | **0.852** |
| not_a_cat precision / recall | 0.744 / 0.542 | **0.792** / **0.644** |
| 95%-precision gate | unreachable at any threshold (best: 0.930 precision @ 0.664 coverage) | **reachable**: 0.952 precision @ 0.824 coverage |

Coral beat the histogram recognizer overall, on day, AND on night (IR) accuracy, and reaches the
95%-precision target the deployed model does not currently reach at any threshold on this same
data. Per crop kind alone (not fused -- reported for transparency, not the production decision):
body 0.872 vs 0.806 (day 1.000 vs 0.906, ir 0.819 vs 0.765); face 0.965 vs 0.887 (day 0.989 vs
0.935, ir 0.903 vs 0.764).

`DECISION_TOP_THRESHOLD = 0.52`, `DECISION_MARGIN = 0.15`: the coverage-maximising (threshold,
margin) pair clearing 95% precision on the FUSED run above (a joint sweep over both parameters,
since production's gate checks both -- the benchmark's own `threshold_for_precision` only swept
confidence alone). `LOG_PROB_FLOOR = 0.03` kept at the histogram recognizer's own starting value;
the live-data run found no wrong-rate benefit from moving it.

## Other Coral models tried (2026-09-26)

Nitin asked for the best model the Coral can run for each job. Same live data and tool as above
(`tools/coral_verify.py --body-model/--face-model`), every candidate through the real CoralHub.

**Identity (Kitty vs Pancake), fused accuracy.** The deployed MobileNet v1 pair stays; nothing beat it.

| Body / face embedder | All | Day | IR | Coverage at 95% precision |
| --- | --- | --- | --- | --- |
| **MobileNet v1 l2norm / MobileNet v1 extractor (deployed)** | **0.915** | **0.992** | **0.852** | **0.824** |
| EfficientNet-EdgeTPU M / M | 0.893 | 0.959 | 0.839 | 0.648 |
| EfficientNet-EdgeTPU L / MobileNet v1 extractor | 0.886 | 0.984 | 0.805 | 0.767 |
| EfficientNet-EdgeTPU L / L | 0.856 | 0.935 | 0.792 | 0.522 |
| EfficientNet-EdgeTPU S / S | 0.849 | 0.935 | 0.779 | 0.635 |
| Histogram recognizer (no Coral) | 0.853 | 0.943 | 0.779 | never reaches 95% |

**Presence (is there a cat at all).** 57 human-labelled events (49 real, 8 false alarms: IR bowl
reflections, mirror reflections, empty frames); event score = the best cat score over the scene frame
and up to 3 trustworthy body crops. Tested SSD MobileNet v2, SSDLite MobileDet, EfficientDet-Lite 0-3
(COCO `cat`) and MobileNet v2, EfficientNet-EdgeTPU L, Inception v3 (summed ImageNet cat classes).
Ranking quality (AUC) was 0.74-0.86, but **every model scored 0.0 on several real cat events** (a
tail passing, a partial body at the frame edge, dark IR close-ups), so no threshold rejects even one
false alarm without also hiding a real cat. Not shipped: presence stays with the feeder's own
detector, its persisted clutter memory, and the VLM judge (docs/40-vision-judge.md), which scored 97%
presence on the same answer key. YOLOv9-S (Frigate's Coral build) could not be tested: CoralHub
lists it but cannot decode its output yet.

Script and per-event scores: `/data/home/KibbleOS/backups/kibble-eval/vision-judge-2026-09-25/coral-models-2026-09-26/`.

## Diagnostics

`diagnostics.py`'s `"coral"` section: `backend` (which recognizer is currently preferred),
`configured` (a URL is set for this entry), `available` (a Coral model currently exists),
`last_error` (the client's own last request failure, if any). `None`/`False` throughout when
`coralhub_url` is unset, same "off means off" shape `vision_judge` already has there.
