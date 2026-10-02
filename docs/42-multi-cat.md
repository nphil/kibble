# 42. Multi-cat sessions: who was here (v2)

Status: design of record, 2026-09-26. This replaces the lane/focus behavior shipped in 0.28.0.
The feeder still owns detection and tracking, Home Assistant owns labels and meal statistics, and
the card owns presentation. The complete design and evidence live in `local://multicat-v2.md`.

## Why this changed

Kitty and Pancake sometimes visit or eat together. The v1 review sheet split one feeder session
into per-cat tabs, but Nitin could not add Pancake when the detector missed her. A close-up of
Kitty also became several detector subjects, creating false extra cats. When a `sid` was missing,
photos remained on the hidden parent and per-cat rows could stay open after the feeder session
closed. V2 treats the person as the authority, treats boxes as hints, and always reviews one
whole session at a time.

## Feeder contract

The v2 feeder changes same-frame duplicate merging and subject re-identification. The existing
wire shape is unchanged: `subjects`, each sample's `sid`/`frame_k`/`bowl`, `others`, and
`frame_boxes` continue to carry device evidence. Subject ids can be absent or fragmented; HA
must not treat a box as proof that another cat exists. See `docs/36-ai-pipeline.md` for the
persisted v9 evidence columns and feeder fields.

## Session planning (`sessions.py`)

The pure planner, `plan_parts`, groups samples by subject id; sid-less photos remain singletons.
It uses each photo's own human label first, then the subject's human label, then a confident
engine decision. Engine identity requires at least two cropped photos for the subject and the
engine's normal confidence and margin gates. A human label of `not_a_cat` or `skip` never removes
the photo from the session.

Without a human cat set, automatic parts are created only for two or more different confident
cats whose qualifying subjects co-occur in a frame. Two subjects that resolve to the same cat
merge into one part, even if they are detector fragments. Unknowns, junk boxes, sid-less photos,
or non-co-occurring subjects cannot create an automatic multi-cat split. If this plan does not
split, the existing chronological `plan_split` fallback remains for legacy sessions.

When the person supplies a cat set, it is exact: one part per listed cat, with the supplied `ate`
value. A named cat may have no photos. A set of one cat keeps the session unsplit. Photos bearing
a listed cat go to that cat; photos of omitted or uncatted animals go to the main part (most
photos, ties to the first listed cat). Subject identity remains visible in scene labels even if
the human cat set is smaller.

## Storage and lifecycle (`store.py`)

Automatic or human parts use stable child uids `<session>-cat-<slug>`; the parent is hidden only
while multiple parts exist. A human one-cat answer labels the session row directly. Replanning
is idempotent and runs on every ingest pass touching a session, including close-only updates, and
after every human write. Every part mirrors parent open/closed state. Part start/end are clamped
to the session bounds and derive from its photos and subject span; an empty human part spans the
whole session. Session media and clip are shared; before/after media stay on eating parts only.

When no split remains, photos return to the session row and children are removed. Hidden parents
never count in per-cat statistics; each visible eating part counts once, while food consumption
remains counted once for the session.

Schema v10 adds `samples.review_src` (`photo`, `subject`, or `NULL`). `NULL` means a legacy photo
review and is protected as human-authored. A per-photo review is never replaced by a later subject
label. Subject `follow` clears only subject-sourced labels.

## WebSocket contract

`kibble/event {entry_id, uid}` accepts a session, part, or hidden v1 uid and returns the entire
session. The `event` is the session row; `kind` is `eat` if the session or any part ate. Its cat
is set only when the session has exactly one cat. `samples` contains every photo in time order
with its part `cat`. `scene_subjects` contains `{box, sid, label, reviewed}` for animals in the
selected scene. `cats` lists session cats with eaters first. `multiple_cats` is a boolean on the
EventDetail. V1 `companions` and scene subject `sample_uid`/`event_uid` fields are removed.

`kibble/session/label {entry_id, uid, cats}` saves the person's exact cat set, where each row is
`{cat, ate}`. The alternate form `{entry_id, uid, verdict}` accepts only `not_a_cat` or `unknown`;
exactly one of `cats` or `verdict` is required. The handler replies with the updated EventDetail,
then reconciles training and refreshes identity in the background.

`kibble/session/subject {entry_id, uid, sid, label}` accepts a cat name, `not_a_cat`, or `follow`.
The session uid may be `event.uid` from the preceding response. A cat label not already in the
session is added, with `ate` set when the session is an eating session. It replies with updated
EventDetail and follows the same training path.

`kibble/sample/label {entry_id, sample_uid, label}` still replies `{sample}` and marks that
photo review as photo-sourced. A newly named cat is added to the session. `kibble/label` continues
to support timeline quick-pick and bulk review; on a session or part it means the session has
exactly that one cat, or the supplied `not_a_cat`/`unknown` verdict.

`kibble/vision/last` is unchanged. Existing scene boxes and live names remain available to the
card, but boxes never create a human cat choice.

## Migration from v1

At startup, schema v10 migrates old `-sub<sid>` children into the new session model. Human-reviewed
cat children become the session's cat set; reviewed `not_a_cat`/`unknown` children label their
photos only where no photo review already exists. All child photos return to the session before
the family is replanned. The e2060 regression must recover one reviewed Kitty eating session,
all 42 photos, preserve `s1` as `not_a_cat` and `s1-o1` as `Kitty`, and count Kitty's meal.

## Invariants

1. One review sheet represents one whole session; no per-cat tabs or focus state.
2. The human cat set is authoritative; detector misses cannot prevent adding a cat.
3. Automatic splitting needs two different confident, cropped, co-occurring cats.
4. Same-cat detector fragments merge, and no photo is stranded on a hidden parent.
5. Every part closes with the session; human photo, subject, and session answers are never overwritten.
6. Legacy sessions without subject ids retain their chronological split behavior.
7. Hidden parents do not affect per-cat statistics; food use is counted once.
8. No new HA entities or entity-id changes.

## Verification scope

`tests/test_sessions.py` covers grouping, conservative automatic split gates, human sets, review
precedence, and sid-less photos. `tests/test_store.py` covers part lifecycle, whole-session event
detail, statistics, photo/subject review precedence, and v10 migration. `tests/test_ingest.py`
covers close-only replanning. `tests/test_websocket_api.py` covers the session label, subject, and
sample label replies. Full-suite validation is run by Main.
