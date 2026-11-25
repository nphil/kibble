"""The photo review queue: which few photos Nitin is asked about, and in what order.

The review screen asks about ONE photo (one crop) at a time, never a visit as a bundle. A visit (a
top-level `events` row plus the per-cat parts it may have been split into) owns a handful of
photos and most of them add nothing new, so each visit offers at most `PICKS_PER_VISIT` photos --
photos already answered count against that budget -- and `select_picks` chooses which.

`select_picks` and its helpers are pure (no database); `photo_page` is the SQL that finds the
visits and photos and pages through them. Nothing here ever writes.

Which photos of a visit. Every photo gets a score; the best is taken first, and each further pick
gets a bonus for not being a near-copy of what is already picked:

- 45% "unsure": the engine has no guess for the photo, or only a weak one -- a human answer
  teaches the most exactly there.
- 25% "novel": the photo's Coral body embedding is far from every cat in the training gallery
  (neutral when the photo or the gallery has no embedding).
- 30% "good crop": a big box that is not cut off by the frame edge, a confident detector score,
  and a face crop on top of the body crop -- a crop that shows the cat well is also easier to
  answer.
- up to +35% "different": the embedding distance to the photos already picked or, without
  embeddings, the seconds between the frames (3 s apart counts as fully different), so three
  frames of one pose are never asked about while other poses exist.

Ties go to the newest photo, then the uid, so the choice never depends on row order.

Which visits, in what order. A visit is in the queue while it is closed, recent enough, not
labelled as a whole by a person (a legacy visit-level answer; a visit whose identity was derived
from photo answers stays until `PICKS_PER_VISIT` of its photos are answered) and still has an
unanswered photo with a trustworthy crop. Visits are served from the current camera view first
(captured after the crop fix, `crop_geometry.LEGACY_CROP_BEFORE`), then by how little the engine
knows about their photos (no guess at all, then a weak guess, then only confident guesses worth a
quick confirm), newest first. A visit's picks are consecutive and a page always holds whole
visits.

The cursor is a keyset on that order, not an offset, so photos answered between two requests never
shift the next page onto photos that were already shown. One pass over every candidate visit's
photos is what makes `remaining` exact and the order stable; the costly parts (embeddings and the
pick itself) only run for the visits on the page that is returned.
"""

from __future__ import annotations

import base64
import json
import math
import sqlite3
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np

from . import coral_identity, crop_geometry, identity

PICKS_PER_VISIT = 3  # an answered photo of a visit counts against this budget
UNSURE_BELOW = 0.75  # a guess under this confidence is "unsure"

# A visit's tier: the least-known of its eligible photos decides how urgently it is asked about.
_TIER_UNKNOWN = 0
_TIER_UNSURE = 1
_TIER_CONFIDENT = 2

# Photo score weights (see the module docstring) and the bonus for being different.
_W_UNSURE = 0.45
_W_NOVELTY = 0.25
_W_QUALITY = 0.30
_W_DIVERSITY = 0.35
# Two frames this many seconds apart are fully different when no embedding can say otherwise.
_FULL_DIVERSITY_GAP_S = 3.0
# What an unknown input (no embedding, no gallery, no box, no detector score) counts as.
_NEUTRAL = 0.5

# Crop quality: how much each part counts (they add up to 1.0).
_W_SIZE = 0.55
_W_DETECTOR = 0.25
_W_CROPS = 0.20
# Inside the crops part a face crop outweighs a body crop: a face confirms it is a cat, which is
# also why `store.pick_thumb` prefers one. Both together make the full part.
_CROP_BODY = 0.4
_CROP_FACE = 0.6
# Box size runs from 0 at the store's minimum area to 1 at this share of the frame; a box that
# touches the frame edge keeps only part of its size score. `_MIN_BOX_AREA` and `_EDGE_MARGIN` are
# `store._MIN_BOX_AREA_RATIO` and `store._EDGE_MARGIN` (this module cannot import the store; a
# test keeps them equal).
_MIN_BOX_AREA = 0.04
_FULL_BOX_AREA = 0.24
_EDGE_MARGIN = 0.02
_CLIPPED_SIZE_FACTOR = 0.6

# SQLite's historical limit is 999 bound variables per statement.
_SQL_CHUNK = 500


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


# --- Gallery: the training set as one centroid per class ---------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class Gallery:
    """Per-class L2-normalised mean Coral BODY embedding of the training gallery (class ->
    vector): what "a photo that looks like nothing we have trained on" is measured against.
    Compared by identity (`eq=False`): numpy arrays have no usable `==`."""

    centroids: Mapping[str, np.ndarray]


def _unit_vector(blob: bytes) -> np.ndarray | None:
    """A stored embedding as a unit-length float32 vector, or `None` when the blob cannot be one
    (not whole floats, non-finite, or all zero): such a row must read as "no embedding" instead
    of putting NaN into a score, where it would make every comparison unreliable."""
    if len(blob) % 4:
        return None
    vector = identity.unpack(blob)
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm < 1e-9:
        return None
    return vector / norm


def load_gallery(conn: sqlite3.Connection) -> Gallery | None:
    """The gallery from every training row that has a cached Coral body embedding for the
    current model, or `None` when nothing is cached (Coral not configured, or not caught up).
    Unlike the recognizer's own gallery it does not skip rows whose source crop was untrustworthy:
    this only ranks photos within a visit, and one query keeps it cheap enough to refresh."""
    sums: dict[str, np.ndarray] = {}
    for cat, blob in conn.execute(
        "SELECT t.cat, e.embedding FROM training t JOIN coral_embeddings e "
        "ON e.row_kind='training' AND e.row_uid=t.uid AND e.crop_kind='body' AND e.model_id=?",
        (coral_identity.CORAL_BODY_MODEL_ID,),
    ):
        vector = _unit_vector(blob)
        if vector is None:
            continue
        total = sums.get(cat)
        if total is None:
            sums[cat] = vector
        elif total.shape == vector.shape:
            total += vector
    centroids: dict[str, np.ndarray] = {}
    for cat, total in sums.items():
        norm = float(np.linalg.norm(total))
        if norm >= 1e-9:
            centroids[cat] = total / norm
    return Gallery(centroids) if centroids else None


# --- Photo selection: a pure function of one visit's eligible photos ---------------------------


@dataclass(frozen=True, slots=True, eq=False)
class PhotoCandidate:
    """Exactly what `select_picks` needs from one eligible photo. `box` is the detector box as
    frame fractions (`None` when absent or not a real rectangle), `embedding` the cached Coral
    body embedding, L2-normalised, or `None` when it is not cached (yet). Compared by identity
    (`eq=False`): numpy arrays have no usable `==`."""

    uid: str
    t: int
    score: float | None
    box: tuple[float, float, float, float] | None
    has_body: bool
    has_face: bool
    guess: str | None
    guess_confidence: float | None
    embedding: np.ndarray | None


def _box_area(box: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = box
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _box_clipped(box: tuple[float, float, float, float]) -> bool:
    x1, y1, x2, y2 = box
    return (
        x1 <= _EDGE_MARGIN
        or y1 <= _EDGE_MARGIN
        or x2 >= 1.0 - _EDGE_MARGIN
        or y2 >= 1.0 - _EDGE_MARGIN
    )


def quality(c: PhotoCandidate) -> float:
    """0..1: how well the crop shows the cat. Mostly box size -- 0 at or below the store's 4%
    minimum area, 1 from 24% of the frame, and only 60% of that when the box touches the frame
    edge (the cat is cut off); no box at all is a neutral half. Then the detector's own score, and
    which crops exist (a face crop is worth more than a body crop, both together are the best)."""
    if c.box is None:
        size = _NEUTRAL
    else:
        size = _clamp01((_box_area(c.box) - _MIN_BOX_AREA) / (_FULL_BOX_AREA - _MIN_BOX_AREA))
        if _box_clipped(c.box):
            size *= _CLIPPED_SIZE_FACTOR
    detector = _NEUTRAL if c.score is None else _clamp01(c.score)
    crops = (_CROP_BODY if c.has_body else 0.0) + (_CROP_FACE if c.has_face else 0.0)
    return _clamp01(_W_SIZE * size + _W_DETECTOR * detector + _W_CROPS * crops)


def unsure(c: PhotoCandidate) -> float:
    """1.0 when the engine has no guess (or no confidence) for the photo, else `1 - confidence`:
    the less sure the engine is, the more a human answer is worth."""
    if c.guess is None or c.guess_confidence is None:
        return 1.0
    return _clamp01(1.0 - c.guess_confidence)


def _cosine(a: np.ndarray, b: np.ndarray) -> float | None:
    """Cosine similarity of two unit vectors, `None` when they cannot be compared (different
    sizes, e.g. a stale row left by another model revision)."""
    if a.shape != b.shape:
        return None
    return float(np.dot(a, b))


def novelty(c: PhotoCandidate, gallery: Gallery | None) -> float:
    """1 minus the best cosine similarity to any class centroid: 0 for a photo the gallery
    already knows, 1 for one unlike every cat in it. A neutral half when the photo has no
    embedding or there is no gallery to compare with."""
    if c.embedding is None or gallery is None:
        return _NEUTRAL
    similarities = [
        similarity
        for centroid in gallery.centroids.values()
        if (similarity := _cosine(c.embedding, centroid)) is not None
    ]
    if not similarities:
        return _NEUTRAL
    return _clamp01(1.0 - max(similarities))


def _difference(a: PhotoCandidate, b: PhotoCandidate) -> float:
    """0..1: how different two photos of one visit are. By embedding distance when both have one;
    otherwise by the seconds between them, so near-simultaneous frames count as copies."""
    if a.embedding is not None and b.embedding is not None:
        similarity = _cosine(a.embedding, b.embedding)
        if similarity is not None:
            return _clamp01(1.0 - similarity)
    return min(1.0, abs(a.t - b.t) / _FULL_DIVERSITY_GAP_S)


def _rank(
    c: PhotoCandidate, base: float, picked: Sequence[PhotoCandidate]
) -> tuple[float, int, str]:
    """Ascending sort key for the next pick: best score (plus the bonus for differing from every
    photo picked so far -- the least different one counts) first, then newest, then uid."""
    bonus = _W_DIVERSITY * min(_difference(c, p) for p in picked) if picked else 0.0
    return (-(base + bonus), -c.t, c.uid)


def select_picks(
    candidates: Sequence[PhotoCandidate], gallery: Gallery | None, k: int = PICKS_PER_VISIT
) -> list[str]:
    """Up to `k` uids from one visit's eligible photos (distinct uids), most useful first. The
    best-scoring photo goes first; every further pick is the best of the rest once near-copies
    of what is already picked lose their score to the diversity bonus, so they are skipped
    while other photos exist. Ties go to the newest photo, then the uid: the result depends on
    the photos only, never on their order."""
    base = {
        c.uid: _W_UNSURE * unsure(c) + _W_NOVELTY * novelty(c, gallery) + _W_QUALITY * quality(c)
        for c in candidates
    }
    remaining = list(candidates)
    picked: list[PhotoCandidate] = []
    while remaining and len(picked) < k:
        best = min(remaining, key=lambda c: _rank(c, base[c.uid], picked))
        picked.append(best)
        remaining.remove(best)
    return [c.uid for c in picked]


# --- The queue: a read-only page builder -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PickedPhoto:
    """One photo to ask about. `in_scene` is true when its detector box belongs to the visit's
    scene frame, so the card can mark where in the scene image this cat is; false for any other
    frame or when the stored box is not a real rectangle."""

    uid: str
    in_scene: bool


@dataclass(frozen=True, slots=True)
class VisitPicks:
    """The photos picked from one top-level visit (session). `kind` is `eat` when the visit or
    any of its per-cat parts is an eat; `scene` is the visit's scene asset id."""

    uid: str
    start: int
    kind: str
    scene: str | None
    picks: tuple[PickedPhoto, ...]  # most useful first, 1..PICKS_PER_VISIT


@dataclass(frozen=True, slots=True)
class PhotoPage:
    """One page of the queue: whole visits in queue order. `remaining` is the exact number of
    photos still waiting in the whole queue and `total_visits` the visits they belong to --
    neither depends on the cursor or the page size. `cursor` goes back into `photo_page`."""

    visits: tuple[VisitPicks, ...]
    remaining: int
    total_visits: int
    cursor: str | None
    has_more: bool


class _Row(NamedTuple):
    """One result row of `_QUEUE_SQL`: the visit's columns, then one of its photos'. The order is
    the SELECT's."""

    visit_uid: str
    start: int
    kind: str
    scene: str | None
    scene_k: int | None
    part_eats: int
    uid: str
    t: int
    body: str | None
    face: str | None
    score: float | None
    x1: float | None
    y1: float | None
    x2: float | None
    y2: float | None
    guess: str | None
    confidence: float | None
    review: str | None
    frame_k: int | None


# Every photo of every candidate visit, answered or not, in one pass. A candidate is a closed,
# top-level eat/visit in retention that no person labelled as a whole: `reviewed` on the visit or
# on any of its parts is a visit-level (legacy) label, unless the visit's identity was derived
# from photo answers (`from_photos`). `hidden` is deliberately not tested: a split session's
# hidden parent is the visit; its photos live on the parent or on its parts (`family`).
_QUEUE_SQL = """
WITH visit AS (
    SELECT e.uid, e.start, e.kind, e.scene, e.scene_k,
           EXISTS (SELECT 1 FROM events c WHERE c.parent_uid = e.uid AND c.kind = 'eat') AS part_eats
    FROM events e
    WHERE e.parent_uid IS NULL AND e.open = 0 AND e.start >= ? AND e.kind IN ('eat', 'visit')
      AND (e.from_photos = 1
           OR NOT (e.reviewed = 1
                   OR EXISTS (SELECT 1 FROM events c WHERE c.parent_uid = e.uid AND c.reviewed = 1)))
), family(uid, visit_uid) AS (
    SELECT uid, uid FROM visit
    UNION ALL
    SELECT c.uid, c.parent_uid FROM events c JOIN visit v ON c.parent_uid = v.uid
)
SELECT v.uid, v.start, v.kind, v.scene, v.scene_k, v.part_eats,
       s.uid, s.t, s.body, s.face, s.score, s.box_x1, s.box_y1, s.box_x2, s.box_y2,
       s.guess, s.guess_confidence, s.review, s.frame_k
FROM family f
JOIN visit v ON v.uid = f.visit_uid
JOIN samples s ON s.event_uid = f.uid
"""


def _tier(guess: str | None, confidence: float | None) -> int:
    if guess is None:
        return _TIER_UNKNOWN
    if confidence is None or confidence < UNSURE_BELOW:
        return _TIER_UNSURE
    return _TIER_CONFIDENT


def _queue_key(era: int, tier: int, start: int, uid: str) -> tuple[int, int, int, str]:
    """The ascending queue order: current camera view (era 0) first, then the tier, then the
    newest visit, then the uid so no two visits ever tie."""
    return (era, tier, -start, uid)


@dataclass(slots=True)
class _Visit:
    """One candidate visit while the queue is built: how many of its photos are answered, and the
    rows of those still eligible to be asked about."""

    uid: str
    start: int
    era: int
    kind: str
    scene: str | None
    scene_k: int | None
    answered: int = 0
    tier: int = _TIER_CONFIDENT
    eligible: list[_Row] = field(default_factory=list)

    @property
    def pick_count(self) -> int:
        return max(0, min(PICKS_PER_VISIT - self.answered, len(self.eligible)))

    @property
    def key(self) -> tuple[int, int, int, str]:
        return _queue_key(self.era, self.tier, self.start, self.uid)


def _crop_trusted(row: _Row) -> bool:
    """Whether the photo's crops may be shown: the legacy-crop rule on the stored box (a photo
    without a complete box is never judged by it)."""
    coords = (row.x1, row.y1, row.x2, row.y2)
    box = None if any(value is None for value in coords) else coords
    return crop_geometry.is_legacy_crop_trustworthy(box, row.t)


def _valid_box(
    x1: float | None, y1: float | None, x2: float | None, y2: float | None
) -> tuple[float, float, float, float] | None:
    """The stored box when it is a real normalised rectangle -- the rule `store._sample_dict`
    applies before it exposes one. NaN and infinite values fail the comparisons."""
    if x1 is None or y1 is None or x2 is None or y2 is None:
        return None
    if 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1:
        return (x1, y1, x2, y2)
    return None


def _queue(conn: sqlite3.Connection, cutoff: int) -> list[_Visit]:
    """Every visit with something left to ask, in queue order."""
    legacy_before = crop_geometry.LEGACY_CROP_BEFORE
    visits: dict[str, _Visit] = {}
    for row in map(_Row._make, conn.execute(_QUEUE_SQL, (cutoff,))):
        visit = visits.get(row.visit_uid)
        if visit is None:
            visit = visits[row.visit_uid] = _Visit(
                uid=row.visit_uid,
                start=row.start,
                era=0 if legacy_before is not None and row.start >= legacy_before else 1,
                kind="eat" if row.part_eats else row.kind,
                scene=row.scene,
                scene_k=row.scene_k,
            )
        if row.review is not None:
            visit.answered += 1
        elif (row.body is not None or row.face is not None) and _crop_trusted(row):
            visit.eligible.append(row)
            visit.tier = min(visit.tier, _tier(row.guess, row.confidence))
    queue = [visit for visit in visits.values() if visit.pick_count > 0]
    queue.sort(key=lambda visit: visit.key)
    return queue


def _load_embeddings(conn: sqlite3.Connection, uids: Sequence[str]) -> dict[str, np.ndarray]:
    """The cached Coral body embeddings of these samples (missing ones are simply absent), in as
    few queries as SQLite's variable limit allows."""
    found: dict[str, np.ndarray] = {}
    for offset in range(0, len(uids), _SQL_CHUNK):
        chunk = uids[offset : offset + _SQL_CHUNK]
        marks = ",".join("?" * len(chunk))
        for uid, blob in conn.execute(
            "SELECT row_uid, embedding FROM coral_embeddings "
            f"WHERE row_kind='sample' AND crop_kind='body' AND model_id=? AND row_uid IN ({marks})",
            (coral_identity.CORAL_BODY_MODEL_ID, *chunk),
        ):
            vector = _unit_vector(blob)
            if vector is not None:
                found[uid] = vector
    return found


def _visit_picks(
    visit: _Visit, embeddings: Mapping[str, np.ndarray], gallery: Gallery | None
) -> VisitPicks:
    candidates: list[PhotoCandidate] = []
    in_scene: dict[str, bool] = {}
    for row in visit.eligible:
        box = _valid_box(row.x1, row.y1, row.x2, row.y2)
        candidates.append(
            PhotoCandidate(
                uid=row.uid, t=row.t, score=row.score, box=box,
                has_body=row.body is not None, has_face=row.face is not None,
                guess=row.guess, guess_confidence=row.confidence,
                embedding=embeddings.get(row.uid),
            )
        )
        in_scene[row.uid] = (
            box is not None and row.frame_k is not None and row.frame_k == visit.scene_k
        )
    chosen = select_picks(candidates, gallery, visit.pick_count)
    return VisitPicks(
        uid=visit.uid, start=visit.start, kind=visit.kind, scene=visit.scene,
        picks=tuple(PickedPhoto(uid=uid, in_scene=in_scene[uid]) for uid in chosen),
    )


def _encode_cursor(visit: _Visit) -> str:
    raw = json.dumps([visit.era, visit.tier, visit.start, visit.uid], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, int, int, str]:
    """The queue key a cursor stands for; `ValueError` for anything `_encode_cursor` could not
    have produced."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except ValueError as err:  # not base64, not UTF-8, not JSON
        raise ValueError("invalid review cursor") from err
    if (
        not isinstance(payload, list)
        or len(payload) != 4
        or any(type(part) is not int for part in payload[:3])
        or not isinstance(payload[3], str)
    ):
        raise ValueError("invalid review cursor")
    era, tier, start, uid = payload
    return _queue_key(era, tier, start, uid)


def photo_page(
    conn: sqlite3.Connection,
    *,
    cutoff: int,
    cursor: str | None,
    limit: int,
    gallery: Gallery | None,
) -> PhotoPage:
    """One page of the photo review queue; never writes. `cutoff` is the oldest visit start
    still in retention. `limit` is the minimum number of photos: whole visits are added until at
    least that many are collected, so a page is never a visit split in two and may exceed `limit`
    by up to `PICKS_PER_VISIT - 1`. A missing (or empty) `cursor` starts at the top; a cursor
    continues after the visit it names, and photos answered between two calls do not move it.
    Raises `ValueError("invalid review cursor")` for a cursor this module did not issue."""
    after = _decode_cursor(cursor) if cursor else None
    queue = _queue(conn, cutoff)
    first = bisect_right([visit.key for visit in queue], after) if after is not None else 0
    end = first
    photos = 0
    while end < len(queue):
        photos += queue[end].pick_count
        end += 1
        if photos >= limit:
            break
    page = queue[first:end]
    embeddings = _load_embeddings(conn, [row.uid for visit in page for row in visit.eligible])
    has_more = end < len(queue)
    return PhotoPage(
        visits=tuple(_visit_picks(visit, embeddings, gallery) for visit in page),
        remaining=sum(visit.pick_count for visit in queue),
        total_visits=len(queue),
        cursor=_encode_cursor(page[-1]) if has_more else None,
        has_more=has_more,
    )
