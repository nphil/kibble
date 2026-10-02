# Vision judge: a second opinion from a real VLM

Status: design of record, 2026-09-25. The evidence for why this exists, measured on Nitin's own
feeder and photos that evening, is summarised under "Why" and "Bake-off".

## Why

The local recognizer (`identity.py`) classifies body/face crops with hand-built histograms --
cheap enough to run on every sample, but wrong often enough at night and on close, ambiguous
crops that it should never be the only opinion. Two problems this judge exists to catch:

- **Night false "meals".** A cold static-clutter memory (`librefeed/daemon/src/vision.rs`)
  produces real `eat` tracks with a score, a box and a scene photo -- but no cat. The local
  recognizer has no way to say "there is nothing here"; it can only guess *which* cat.
- **Day/night identity confusion.** Close-up crops dominated by the steel bowl and background
  (the feeder's own framing) regularly fool the histogram classifier -- Pancake's own crops
  guessed as Kitty at 0.94, Kitty's own crops guessed as Pancake at 0.72-0.79 (measured on the
  live gallery, 2026-09-25).

A real vision-language model, given the same photos a human would look at (plus a text
description of what each enrolled cat actually looks like), catches both. It runs after the
fact, off the event loop, and never faster or more strictly than the rules below -- it is a
second opinion for a human to see, never a second detector and never a teacher for the first
one.

## Contract

- **Endpoint.** `POST {vision_judge_url}/chat/completions`, OpenAI-compatible (llama-swap).
  Structured output via `response_format: {"type": "json_schema", "json_schema": {"schema":
  ..., "strict": true}}` -- confirmed live against the bake-off's own llama-server build
  (see "Bake-off" below). The verdict schema's `cat` property carries a per-request `enum`
  built from the live roster plus `"unknown"`/`"none"`, so the model is grammar-enforced to
  never name a cat that isn't actually enrolled -- `judge.parse_verdict` still validates
  independently regardless, since a schema is only as good as the endpoint that honours it.
- **Sampling is pinned per request**: `temperature: 0`, `presence_penalty: 0`,
  `frequency_penalty: 0`. The llama-swap server is shared with other apps, and whatever sampling
  defaults it carries for them must never change the greedy decoding the bake-off validated.
- **Verdict**, exactly: `{"cat_present": bool, "cat": string, "confidence": number,
  "multiple_cats": bool, "reason": string}`. `cat` is one enrolled name, `"unknown"` (a cat is
  present but not identifiable), or `"none"` (no cat). `confidence` is 0..1 for the whole
  verdict, not just the name.
- **Request images**, in order: the roster as text (`Enrolled cats:\n<Name>: <coat
  description>` per line), then the scene (or the `after` frame when there is no scene)
  labelled `Scene`, then up to 3 close-ups labelled `Close-up` (best sample score first), then
  the question. Sent at native resolution, no resize step (measured no accuracy gain, slightly
  higher latency, see "Bake-off"). No reference images by default
  (`JUDGE_INCLUDE_REFERENCE_IMAGES`) -- reference images measured a ~9-point presence-accuracy
  regression in the bake-off.
- **Coat descriptions** (`cats.description`, schema v7): filled once per cat, the first time it
  is blank, by asking the same model to describe the coat from the cat's avatar plus up to one
  day and one IR training crop (`VisionJudge.ensure_descriptions`, run once at startup).
- **Scene replacement.** `librefeedd` rewrites a meal's scene photo under a NEW filename as a
  better sample arrives (`e<id>-scene-<k>.jpg`, or `e<id>-scene-eat.jpg` for the eat-start
  fallback), removing the superseded file from its own spool. `ingest.py`'s `_ingest_event`
  detects a changed filename for an already-known event, fetches the new asset, updates the
  row, deletes HA's own superseded copy (only once nothing else still references that asset
  id), and clears that event's stored `judge_evidence` so the changed scene is judged exactly
  once more. The pre-existing `e<id>-scene.jpg` name (rows from before this change) is left
  alone and stays readable.

## Legacy crop trustworthiness

A separate 2026-09-25 bug (`librefeed/media/src/crop_rect.h`'s own history note): a device crop
resize helper forgot to fold the frame-edge-clamped left offset into its own column index, so
every capture before the fix silently sampled the wrong columns -- an off-centre narrow/tall box
(a standing cat, the feeder's own common shape) shows unrelated room content instead of the cat
(`crop_rect_test.c`'s regression cases: e1410-s1, e1492-s1). `crop_geometry.py` ports the device's
own `crop_rect_square` to pure Python and derives, from one sample's own stored box, how much of
what was actually sampled overlaps what should have been (`crop_overlap`); below
`CROP_OVERLAP_TRUST_THRESHOLD` (0.9) a LEGACY sample (`t < crop_geometry.LEGACY_CROP_BEFORE`) is
untrustworthy. `LEGACY_CROP_BEFORE` is set to the actual librefeed-media deploy time
(1790385640, 2026-09-25 21:20 America/New_York) -- a capture at or after it is trusted outright,
no box needed.

An untrustworthy sample's crop is never deleted, only ever left out of:

- the vision judge's own evidence (`judge.select_judge_crops`) -- it falls back to the event's
  scene/`after` frame instead, the one asset this bug never touches;
- auto-learn's own new-training-sample selection (`ingest.IdentityEngine.async_maybe_auto_learn`)
  -- an untrustworthy crop never becomes a new `training` row in the first place;
- the identity engine's own gallery build (`store.all_training_features`) -- an EXISTING
  `label`/`auto` training row copied from an untrustworthy crop is skipped on every rebuild, so
  it stops feeding the classifier without needing a retroactive cleanup pass. A row whose source
  sample has since aged out of retention is kept: purge is routine and proves nothing about crop
  quality, so an unprovable row is never punished for it. `import`/`upload` rows have no such
  source sample and are always kept;
- every card-facing view (`store._thumb_candidates`, and `event_detail`'s own `samples`, which
  is built from the same filtered list) -- never offered as a thumbnail, never shown to a human
  for labelling. The event's own `scene` and whole-event `kibble/label` are unaffected either
  way: neither is derived from `samples`.

## Eligibility (item 4)

A closed (`open=0`), unreviewed (`reviewed=0`), unhidden `eat`/`visit` event, started within the
last 48h, with at least one image (a scene/`after` frame or a scored crop), is a candidate.
Eats are **always** eligible. Visits are eligible only when they would actually show on the
timeline (`store.pick_thumb` finds a usable thumb) **and** the local recognizer is not already
confidently `auto` (`confidence < judge.VISIT_JUDGE_SKIP_CONFIDENCE`, currently 0.85) -- a visit
already confidently named is not worth the round trip.

An event already judged against its *current* evidence (the scene/`after` asset id plus the
exact sample uids of the crops that would be sent, `judge.evidence_key`) is skipped; any change
to that evidence -- a new best crop arrives, or a scene replacement invalidates it -- makes the
event eligible again, exactly once.

Scheduling (`VisionJudge.schedule_judge`) happens right after `ingest.py` durably writes an
event's evidence, for every closed `eat`/`visit`, on every poll -- a debounce
(`judge.JUDGE_DEBOUNCE_S`) means a late-arriving `after` frame or a scene replacement that lands
moments later is folded into the run that actually fires, rather than being judged against
stale evidence. A startup sweep (`VisionJudge.async_backfill_eligible`, capped at 100, newest
first) re-offers whatever the in-memory debounce/schedule map lost across a restart.

## Application rules

Thresholds are module constants in `judge.py` (`JUDGE_SUPPRESS_MAX_CONFIDENCE`,
`JUDGE_IDENTIFY_MIN_CONFIDENCE`), set from the bake-off below. The model's `confidence` behaves
as its belief that a cat is present, so a confident "no cat" is a LOW number. Every verdict is
recorded in `events.judge_*` regardless of which rule fired.

| Rule | Condition | Effect |
| --- | --- | --- |
| (a) | `cat_present=false` and confidence ≤ 0.2 (the model rates a cat at 20% or less), **unless** the event is an `eat` the feeder kept 4+ samples for (`JUDGE_PROTECTED_EAT_SAMPLES`: false meals on record kept 0-3, real meals 4-31) | `identity_status='not_a_cat'`, `cat=NULL`, `reviewed` left at 0 -- the timeline's own `identity_status IS NOT 'not_a_cat'` filter hides it; a human can still undo it later. |
| (b) | `cat_present=true`, an enrolled name, confidence ≥ 0.7, not `multiple_cats`, **and** the event's identity is `None`/`unknown`, or `auto` naming a *different* cat | `cat`/`confidence` set to the verdict's own, `identity_status='auto'`. |
| (c) | anything else | The verdict is still recorded; nothing about the event's own identity changes. |

Rule (a)/(b) never touch a `reviewed` event -- the same `WHERE reviewed=0` guard
`store.set_event_classification` already uses, checked once before the request and again,
against a freshly re-read row, right before the write (a human review or a fresh local
reclassification can land during the model's own round trip). Rule (b) is deliberately narrower
than "any mismatch": an event already `not_a_cat` is left to a human or to the local recognizer,
never silently walked back to a name by this rule alone.

**The judge never trains the recognizer.** It never calls `add_auto_training`/
`add_upload_training`, and `autolearn.is_judge_sourced` makes the existing auto-learn gate skip
any event whose current `cat` came from rule (b) (`judge_cat == cat` with `judge_at` set) --
gate 5 of `autolearn.py`'s own five-gate design. A judge-sourced identity is a second opinion for
a human to see, never a label the classifier gets to learn from and later be graded against.

After a change, the coordinator's identity snapshot is refreshed and pushed through the same
path `kibble/timeline/subscribe` already listens on (`KibbleCoordinator.
async_refresh_identity_snapshot`) -- no separate push mechanism.

## Bake-off (2026-09-25)

Chosen by measurement on 33 labelled events (88 images, day and IR, including 9 false alarms
and one both-cats meal), every candidate at Q8_0 weights + F16 mmproj on the P40 via llama-swap,
same prompt and images for all:

| Model | Presence | Identity | Confident wrong answer on the 9 false alarms | Extra VRAM |
| --- | --- | --- | --- | --- |
| gemma4-e4b (already resident) | 84.8% | 60.6% | 4 | 0 |
| **Qwen3-VL-4B-Instruct** (chosen, llama-swap `qwen3-vl-4b`) | 87.9% | 60.6% | 1 | 6767 MiB |
| MiniCPM-V-4.6 | 78.8% | 54.5% | 8 | 2190 MiB |
| InternVL3.5-2B | 78.8% | 63.6% | 9 (calls any dark shape Pancake) | 5847 MiB |

Prompt strategy on Qwen3-VL-4B: coat descriptions as text with no reference images scored 97.0%
presence and 48.5% identity, and every identity miss was a safe `"unknown"`, never a wrong name.
Adding reference images dropped presence to 87.9%: the model described the reference photo's cat
as if it were in the scene. Scene only, without crops, dropped presence to 78.8%. Production
uses descriptions, scene plus up to 3 crops, native resolution: 0 JSON failures, median 2.5 s.

Confidence, measured on that final run: all 6 correct "no cat" verdicts carried 0.0-0.1, all 26
real cat events were judged present at 0.7 or higher, and one mirror reflection was judged
present at 0.7. Rule (a)'s bar of 0.2 and rule (b)'s 0.7 come straight from that split; a "no
cat" answer with a high number is self-contradictory and changes nothing. The answer key (which
of the 33 events really had a cat) and the full bake-off, Coral and night-evidence reports live
outside the repo in `/data/home/KibbleOS/backups/kibble-eval/vision-judge-2026-09-25/`; the photos
themselves stay in HA's own Kibble archive under those event numbers.

## Disabling it

Leave `vision_judge_url` (the options flow's "Vision judge server") empty -- the default. With
it empty, `VisionJudge.enabled` is `False`: no request is ever built, no description is ever
filled, no `judge_*` column is ever written, and the startup backfill/description sweep are
no-ops. `vision_judge_model` (default `qwen3-vl-4b`) only matters once a URL is set.

## Operational notes

- Concurrency 1, 45s timeout, one retry (two attempts total) per request -- shared by both the
  verdict path and the description-filling path, since both talk to the same server. A
  persistent failure logs once per new failure streak (`_LOGGER.warning`), then stays quiet
  (`_LOGGER.debug`) until the next success resets it -- a judge server that is simply off must
  never spam the log once per event.
- Detection thresholds elsewhere in this integration (what counts as a meal at all) are
  untouched by this feature; the judge only ever narrows *identity*, never manufactures or
  hides a real detection outright beyond rule (a)'s own conservative, always-logged,
  always-reversible suppression.
- Diagnostics (`diagnostics.py`) expose whether the judge is enabled, its configured model, the
  last request error (if any), verdict counts by outcome, and every cat's current description.
