"""`photo_queue.py`: which photos Nitin is asked about, one at a time, and in what order.

Two halves, both pinned on observable behaviour rather than plumbing:

- The pure selection (`quality`, `unsure`, `novelty`, `select_picks`): an unsure photo, a good
  crop and a photo unlike the training gallery are asked about first, and a near-copy of a photo
  that is already picked is skipped while anything different is left.
- The read-only page builder `photo_page`, run against a real `_SyncStore` database: at most three
  photos per visit (answered ones use the budget up); which visits and photos are eligible; queue
  order (current camera view, then how little is known, then newest); exact `remaining` and
  `total_visits`; whole-visit pages; a cursor that survives answers arriving between two
  requests; and that a page costs two statements and never writes.
"""

from __future__ import annotations

import base64
import json
import math
import random
import re
import sqlite3
import time
from collections.abc import Iterator

import numpy as np
import pytest
from kibble import coral_identity, crop_geometry, identity, photo_queue
from kibble import store as store_module
from kibble.photo_queue import (
    PICKS_PER_VISIT,
    UNSURE_BELOW,
    Gallery,
    PhotoCandidate,
    PhotoPage,
    load_gallery,
    novelty,
    photo_page,
    quality,
    select_picks,
    unsure,
)
from kibble.store import _SyncStore

LEGACY = 1_000_000  # stands in for `crop_geometry.LEGACY_CROP_BEFORE`, the device crop fix
NEW = 2_000_000  # a visit after the fix: the current camera view
OLD = 500_000  # a visit before it

BIG = (0.30, 0.20, 0.70, 0.80)  # 24% of the frame, clear of every edge: the best size
MID = (0.30, 0.30, 0.65, 0.70)  # 14%: halfway between the store's minimum and the best size
TINY = (0.45, 0.45, 0.55, 0.55)  # 1%: under the store's 4% minimum
CLIPPED = (0.00, 0.20, 0.50, 0.80)  # 30% of the frame, but cut off by the left edge
# Pre-fix boxes: the device cut the first one's crop from the wrong place, the second one's from
# where the box says.
BAD_LEGACY_BOX = (563 / 1280, 16 / 720, 759 / 1280, 434 / 720)
GOOD_LEGACY_BOX = (0.0, 300 / 720, 80 / 1280, 460 / 720)

_INSERT_EVENT = (
    "INSERT INTO events(uid, device_event_id, kind, start, end, open, scene, scene_k, reviewed, "
    "updated, parent_uid, hidden, from_photos) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_PHOTO = (
    "INSERT INTO samples(uid, event_uid, t, body, face, guess, guess_confidence, box_x1, box_y1, "
    "box_x2, box_y2, score, review, review_src, frame_k) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@pytest.fixture(autouse=True)
def _crop_fix_deployed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crop_geometry, "LEGACY_CROP_BEFORE", LEGACY)


@pytest.fixture
def db(tmp_path) -> Iterator[sqlite3.Connection]:
    store = _SyncStore(tmp_path, "entry1")
    yield store.conn
    store.close()


@pytest.fixture
def ten(db: sqlite3.Connection) -> None:
    """Ten new visits of three photos each, nothing known about any: `v9` (newest) is first."""
    for i in range(10):
        _visit(db, f"v{i}", NEW + i * 100, 3)


def _event(
    db: sqlite3.Connection,
    uid: str,
    start: int,
    *,
    kind: str = "visit",
    open_: int = 0,
    parent: str | None = None,
    hidden: int = 0,
    reviewed: int = 0,
    from_photos: int = 0,
    scene: str | None = None,
    scene_k: int | None = None,
) -> None:
    db.execute(
        _INSERT_EVENT,
        (uid, kind, start, start + 30, open_, scene, scene_k, reviewed, start, parent, hidden, from_photos),
    )


def _photo(
    db: sqlite3.Connection,
    uid: str,
    event: str,
    t: int,
    *,
    body: str | None = "b.jpg",
    face: str | None = None,
    guess: str | None = None,
    confidence: float | None = None,
    box: tuple[float | None, ...] | None = None,
    score: float | None = 0.9,
    review: str | None = None,
    review_src: str | None = None,
    frame_k: int | None = None,
) -> str:
    x1, y1, x2, y2 = box or (None, None, None, None)
    db.execute(
        _INSERT_PHOTO,
        (uid, event, t, body, face, guess, confidence, x1, y1, x2, y2, score, review, review_src, frame_k),
    )
    return uid


def _photos(db: sqlite3.Connection, event: str, start: int, n: int, **fields) -> list[str]:
    """`n` photos `<event>-1` .. `<event>-n` of an event, one second apart from `start`."""
    return [_photo(db, f"{event}-{i}", event, start + i, **fields) for i in range(1, n + 1)]


def _visit(db: sqlite3.Connection, uid: str, start: int, n: int = 3, *, photo=None, **event) -> list[str]:
    """A top-level event with `n` photos (all given the `photo` fields); returns their uids."""
    _event(db, uid, start, **event)
    return _photos(db, uid, start, n, **(photo or {}))


def _answer(db: sqlite3.Connection, *uids: str, review: str = "Kitty") -> None:
    db.executemany("UPDATE samples SET review=? WHERE uid=?", [(review, uid) for uid in uids])


def _page(db: sqlite3.Connection, *, cutoff=0, cursor=None, limit=1000, gallery=None) -> PhotoPage:
    return photo_page(db, cutoff=cutoff, cursor=cursor, limit=limit, gallery=gallery)


def _order(page: PhotoPage) -> list[str]:
    """The visit uids of a page, in page order."""
    return [visit.uid for visit in page.visits]


def _photo_uids(page: PhotoPage) -> list[str]:
    """Every picked photo uid of a page, in page order."""
    return [pick.uid for visit in page.visits for pick in visit.picks]


def _unit(*xs: float) -> np.ndarray:
    vector = np.array(xs, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def _embed(
    db: sqlite3.Connection,
    row_uid: str,
    vector: np.ndarray | bytes,
    *,
    row_kind: str = "sample",
    crop_kind: str = "body",
    model_id: str = coral_identity.CORAL_BODY_MODEL_ID,
) -> None:
    """Cache an embedding for a sample or training row; raw bytes go in exactly as given."""
    blob = vector if isinstance(vector, bytes) else identity.pack(vector)
    db.execute(
        "INSERT INTO coral_embeddings(row_kind, row_uid, crop_kind, model_id, embedding, created) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (row_kind, row_uid, crop_kind, model_id, blob),
    )


def _train(
    db: sqlite3.Connection, uid: str, cat: str, vector: np.ndarray | bytes | None = None, **key
) -> None:
    db.execute("INSERT INTO training(uid, cat, source, created) VALUES (?, ?, 'review', 0)", (uid, cat))
    if vector is not None:
        _embed(db, uid, vector, row_kind="training", **key)


def _cand(
    uid: str = "p",
    t: int = 1000,
    *,
    score: float | None = 0.9,
    box: tuple[float, float, float, float] | None = BIG,
    has_body: bool = True,
    has_face: bool = False,
    guess: str | None = None,
    confidence: float | None = None,
    embedding: np.ndarray | None = None,
) -> PhotoCandidate:
    return PhotoCandidate(
        uid=uid, t=t, score=score, box=box, has_body=has_body, has_face=has_face,
        guess=guess, guess_confidence=confidence, embedding=embedding,
    )


def _b64(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _cursor(payload: object) -> str:
    return _b64(json.dumps(payload).encode())


# --- the pieces of a photo's score ----------------------------------------------------------


def test_the_example_legacy_boxes_are_what_these_tests_say():
    assert crop_geometry.is_legacy_crop_trustworthy(BAD_LEGACY_BOX, NEW)
    assert not crop_geometry.is_legacy_crop_trustworthy(BAD_LEGACY_BOX, OLD)
    assert crop_geometry.is_legacy_crop_trustworthy(GOOD_LEGACY_BOX, OLD)


def test_quality_spans_zero_to_one():
    assert quality(_cand(box=BIG, score=1.0, has_body=True, has_face=True)) == pytest.approx(1.0)
    assert quality(_cand(box=TINY, score=0.0, has_body=False, has_face=False)) == pytest.approx(0.0)


def test_quality_clamps_an_out_of_range_detector_score():
    assert quality(_cand(score=7.0)) == pytest.approx(quality(_cand(score=1.0)))
    assert quality(_cand(score=-3.0)) == pytest.approx(quality(_cand(score=0.0)))


def test_a_bigger_box_scores_higher():
    assert quality(_cand(box=BIG)) > quality(_cand(box=MID)) > quality(_cand(box=TINY))


@pytest.mark.parametrize(
    "box",
    [
        (0.00, 0.20, 0.50, 0.80),
        (0.20, 0.00, 0.70, 0.60),
        (0.50, 0.20, 1.00, 0.80),
        (0.20, 0.40, 0.70, 1.00),
    ],
    ids=["left", "top", "right", "bottom"],
)
def test_a_box_touching_the_frame_edge_scores_lower(box):
    clear_of_the_edges = (0.20, 0.20, 0.70, 0.80)  # same area
    assert quality(_cand(box=box)) < quality(_cand(box=clear_of_the_edges))


@pytest.mark.parametrize(("x1", "cut_off"), [(0.02, True), (0.03, False)])
def test_the_edge_margin_is_the_stores(x1, cut_off):
    box = (x1, 0.20, x1 + 0.50, 0.80)
    clear_of_the_edges = (0.30, 0.20, 0.80, 0.80)  # same area
    assert (quality(_cand(box=box)) < quality(_cand(box=clear_of_the_edges))) is cut_off


def test_without_a_box_the_size_counts_as_neutral():
    assert quality(_cand(box=None)) == pytest.approx(quality(_cand(box=MID)))


def test_a_missing_detector_score_counts_as_neutral():
    assert quality(_cand(score=None)) == pytest.approx(quality(_cand(score=0.5)))


def test_a_face_crop_counts_for_more_than_a_body_crop():
    both = quality(_cand(has_body=True, has_face=True))
    face_only = quality(_cand(has_body=False, has_face=True))
    body_only = quality(_cand(has_body=True, has_face=False))
    neither = quality(_cand(has_body=False, has_face=False))
    assert both > face_only > body_only > neither


@pytest.mark.parametrize(
    ("guess", "confidence", "expected"),
    [
        (None, None, 1.0),
        (None, 0.9, 1.0),
        ("Kitty", None, 1.0),
        ("Kitty", 0.9, 0.1),
        ("Kitty", 1.0, 0.0),
        ("Kitty", 1.4, 0.0),
        ("Kitty", -0.2, 1.0),
    ],
)
def test_unsure_is_one_minus_the_confidence(guess, confidence, expected):
    assert unsure(_cand(guess=guess, confidence=confidence)) == pytest.approx(expected)


GALLERY = Gallery({"Kitty": _unit(1, 0, 0), "Mochi": _unit(0, 1, 0)})


def test_novelty_is_the_distance_from_the_nearest_class():
    assert novelty(_cand(embedding=_unit(1, 0, 0)), GALLERY) == pytest.approx(0.0, abs=1e-6)
    assert novelty(_cand(embedding=_unit(0, 1, 0)), GALLERY) == pytest.approx(0.0, abs=1e-6)
    assert novelty(_cand(embedding=_unit(0, 0, 1)), GALLERY) == pytest.approx(1.0)
    assert novelty(_cand(embedding=_unit(1, 1, 0)), GALLERY) == pytest.approx(1 - 2**-0.5)


def test_novelty_never_leaves_zero_to_one():
    opposite = _cand(embedding=_unit(-1, 0, 0))
    assert novelty(opposite, Gallery({"Kitty": _unit(1, 0, 0)})) == pytest.approx(1.0)


def test_novelty_is_neutral_when_there_is_nothing_to_compare():
    embedded = _cand(embedding=_unit(1, 0, 0))
    assert novelty(_cand(embedding=None), GALLERY) == 0.5
    assert novelty(embedded, None) == 0.5
    assert novelty(embedded, Gallery({})) == 0.5
    assert novelty(embedded, Gallery({"Kitty": _unit(1, 0, 0, 0)})) == 0.5  # another model's size


def test_novelty_skips_the_classes_it_cannot_compare_with():
    mixed = Gallery({"stale": _unit(1, 0, 0, 0), "Kitty": _unit(0, 1, 0)})
    assert novelty(_cand(embedding=_unit(1, 1, 0)), mixed) == pytest.approx(1 - 2**-0.5)


def test_the_score_weights_are_normalised():
    assert photo_queue._W_UNSURE + photo_queue._W_NOVELTY + photo_queue._W_QUALITY == pytest.approx(1.0)
    assert photo_queue._W_SIZE + photo_queue._W_DETECTOR + photo_queue._W_CROPS == pytest.approx(1.0)
    assert photo_queue._CROP_BODY + photo_queue._CROP_FACE == pytest.approx(1.0)


def test_the_box_rules_match_the_stores():
    assert photo_queue._MIN_BOX_AREA == store_module._MIN_BOX_AREA_RATIO
    assert photo_queue._EDGE_MARGIN == store_module._EDGE_MARGIN


# --- choosing photos within one visit -------------------------------------------------------


def test_select_picks_returns_the_best_first_and_at_most_k():
    # Five photos 10 s apart, from nothing known to very sure: only the unsure score differs.
    photos = [
        _cand("p1", 1010, guess=None),
        _cand("p2", 1020, guess="Kitty", confidence=0.1),
        _cand("p3", 1030, guess="Kitty", confidence=0.4),
        _cand("p4", 1040, guess="Kitty", confidence=0.7),
        _cand("p5", 1050, guess="Kitty", confidence=0.95),
    ]
    assert select_picks(photos, None) == ["p1", "p2", "p3"]
    assert select_picks(photos, None, 1) == ["p1"]
    assert select_picks(photos, None, 0) == []
    assert select_picks(photos, None, 9) == ["p1", "p2", "p3", "p4", "p5"]


def test_select_picks_of_no_photos_is_empty():
    assert select_picks([], None) == []
    assert select_picks([], GALLERY, 5) == []


def test_an_unsure_photo_is_picked_before_a_confident_one_with_a_better_crop():
    confident = _cand("sure", guess="Kitty", confidence=0.95, box=BIG, has_face=True)
    unknown = _cand("unsure", box=TINY)
    assert select_picks([confident, unknown], None, 1) == ["unsure"]


def test_a_better_crop_is_picked_first_when_all_else_is_equal():
    photos = [
        _cand("tiny", 1000, box=TINY),
        _cand("big-face", 1010, box=BIG, has_face=True),
        _cand("big", 1020, box=BIG),
    ]
    assert select_picks(photos, None) == ["big-face", "big", "tiny"]


def test_a_novel_photo_is_picked_before_one_the_gallery_knows():
    known = _cand("known", 1020, embedding=_unit(1, 0, 0))
    novel = _cand("novel", 1010, embedding=_unit(0, 0, 1))
    assert select_picks([known, novel], GALLERY, 1) == ["novel"]
    assert select_picks([known, novel], None, 1) == ["known"]  # nothing to compare: newest wins


def test_a_near_copy_of_a_picked_photo_is_skipped_by_embedding():
    a = _cand("a", 1000, embedding=_unit(1, 0, 0, 0))
    a_copy = _cand("a-copy", 1000, score=0.80, embedding=_unit(1, 0.01, 0, 0))
    other = _cand("other", 1000, box=TINY, embedding=_unit(0, 1, 0, 0))  # a worse crop of another pose
    assert select_picks([a, a_copy, other], None, 2) == ["a", "other"]
    assert select_picks([a, a_copy, other], None, 3) == ["a", "other", "a-copy"]


def test_a_photo_is_as_new_as_its_nearest_picked_photo():
    # c is another pose than a but a near copy of b; d is a middling change from both.
    a = _cand("a", 1000, score=0.90, embedding=_unit(1, 0, 0))
    b = _cand("b", 1000, score=0.85, embedding=_unit(0, 1, 0))
    c = _cand("c", 1000, score=0.80, embedding=_unit(0.05, 1, 0))
    d = _cand("d", 1000, score=0.80, embedding=_unit(1, 1, 0))
    assert select_picks([a, b, c, d], None, 3) == ["a", "b", "d"]


def test_near_simultaneous_frames_are_not_both_picked_without_embeddings():
    a = _cand("a", 1000)
    a_copy = _cand("a-copy", 1000, score=0.80)
    a_next_frame = _cand("a-next", 1001, score=0.80)
    other = _cand("other", 1010, box=TINY)
    assert select_picks([a, a_copy, a_next_frame, other], None, 2) == ["a", "other"]


def test_a_photo_taken_before_a_picked_one_is_just_as_different():
    a = _cand("a", 1010)
    earlier = _cand("earlier", 1000, score=0.80)  # ten seconds before a
    a_next_frame = _cand("a-next", 1011, score=0.80)
    assert select_picks([a, earlier, a_next_frame], None, 2) == ["a", "earlier"]


def test_frames_of_one_moment_are_all_offered_when_nothing_else_exists():
    frames = [_cand(f"f{i}", 1000, score=0.9 - i * 0.01) for i in range(4)]
    assert select_picks(frames, None, 3) == ["f0", "f1", "f2"]


def test_select_picks_breaks_ties_by_newest_then_uid():
    by_age = [_cand("x", 100), _cand("y", 300), _cand("z", 200)]
    assert select_picks(by_age, None) == ["y", "z", "x"]
    same_second = [_cand("b", 500), _cand("a", 500), _cand("c", 500)]
    assert select_picks(same_second, None) == ["a", "b", "c"]


def test_select_picks_does_not_depend_on_the_order_of_the_photos():
    rng = random.Random(11)
    photos = [
        _cand(
            f"p{i:02d}",
            1000 + rng.randrange(40),
            score=rng.random(),
            box=rng.choice([BIG, MID, TINY, CLIPPED, None]),
            has_face=rng.random() < 0.4,
            guess=rng.choice([None, "Kitty", "Mochi"]),
            confidence=rng.random(),
            embedding=_unit(*[rng.gauss(0, 1) for _ in range(6)]) if rng.random() < 0.7 else None,
        )
        for i in range(12)
    ]
    gallery = Gallery({"Kitty": _unit(1, 0, 0, 0, 0, 0), "Mochi": _unit(0, 1, 0, 0, 0, 0)})
    expected = select_picks(photos, gallery, 4)
    assert len(set(expected)) == len(expected) == 4
    for _ in range(20):
        rng.shuffle(photos)
        assert select_picks(photos, gallery, 4) == expected


# --- how many photos a visit offers ---------------------------------------------------------


def test_an_empty_queue_is_an_empty_page(db):
    assert _page(db) == PhotoPage((), 0, 0, None, False)


def test_a_visit_offers_at_most_three_photos(db):
    _visit(db, "v", NEW, 9)
    page = _page(db)
    assert PICKS_PER_VISIT == 3
    assert [len(visit.picks) for visit in page.visits] == [3]
    assert len(set(_photo_uids(page))) == 3
    assert (page.remaining, page.total_visits) == (3, 1)


def test_a_visit_with_fewer_photos_than_that_offers_them_all(db):
    photos = _visit(db, "v", NEW, 2)
    page = _page(db)
    assert sorted(_photo_uids(page)) == sorted(photos)
    assert page.remaining == 2


@pytest.mark.parametrize(("answered", "picks"), [(0, 3), (1, 2), (2, 1)])
def test_answered_photos_use_up_the_budget(db, answered, picks):
    photos = _visit(db, "v", NEW, 6)
    _answer(db, *photos[:answered])
    page = _page(db)
    assert [len(visit.picks) for visit in page.visits] == [picks]
    assert page.remaining == picks
    assert not set(_photo_uids(page)) & set(photos[:answered])


@pytest.mark.parametrize("answered", [3, 4, 6])
def test_a_visit_with_three_answers_is_done(db, answered):
    photos = _visit(db, "v", NEW, 6)
    _answer(db, *photos[:answered])
    assert _page(db) == PhotoPage((), 0, 0, None, False)


@pytest.mark.parametrize("review_src", [None, "photo", "subject"])
@pytest.mark.parametrize("review", ["Kitty", "both", "unknown", "not_a_cat", "skip"])
def test_any_answer_takes_a_photo_out_whoever_gave_it(db, review, review_src):
    _visit(db, "v", NEW, 1, photo={"review": review, "review_src": review_src})
    assert _page(db) == PhotoPage((), 0, 0, None, False)


# --- what is stored for a photo steers the pick ---------------------------------------------


@pytest.mark.parametrize(
    ("better", "worse"),
    [
        ({"guess": "Kitty", "confidence": 0.20}, {"guess": "Kitty", "confidence": 0.95}),
        ({}, {"guess": "Kitty", "confidence": 0.95}),
        ({"score": 0.95}, {"score": 0.30}),
        ({"box": BIG}, {"box": TINY}),
        ({"face": "f.jpg"}, {}),
        ({"face": "f.jpg"}, {"body": None, "face": "f.jpg"}),
    ],
    ids=["engine-confidence", "engine-guess", "detector-score", "box-size", "face-crop", "body-crop"],
)
def test_what_is_stored_for_a_photo_decides_which_one_is_picked(db, better, worse):
    _event(db, "v", NEW)
    _photos(db, "v", NEW, 2, review="Kitty")  # two answers leave one pick
    _photo(db, "better", "v", NEW + 10, **better)  # the older: only what it stores can win it the pick
    _photo(db, "worse", "v", NEW + 11, **worse)
    assert _photo_uids(_page(db)) == ["better"]


def test_a_visits_picks_come_most_useful_first(db):
    _event(db, "v", NEW)
    for i, score in enumerate([0.5, 0.9, 0.7]):  # a minute apart: only the detector tells them apart
        _photo(db, f"v-{i}", "v", NEW + i * 60, score=score)
    assert _photo_uids(_page(db)) == ["v-1", "v-2", "v-0"]


# --- which visits and photos are asked about ------------------------------------------------


def test_visits_a_person_labelled_as_a_whole_are_left_out(db):
    _visit(db, "labelled", NEW + 1, 2, reviewed=1)
    _visit(db, "part-labelled", NEW + 2, 2)
    _event(db, "part-labelled-a", NEW + 2, parent="part-labelled", reviewed=1)
    _visit(db, "from-photos", NEW + 3, 2, reviewed=1, from_photos=1)  # its label came from photo answers
    _visit(db, "from-photos-part", NEW + 4, 2, from_photos=1)
    _event(db, "from-photos-part-a", NEW + 4, parent="from-photos-part", reviewed=1)
    _visit(db, "never-reviewed", NEW + 5, 2)
    assert _order(_page(db)) == ["never-reviewed", "from-photos-part", "from-photos"]


def test_open_and_expired_visits_are_left_out(db):
    _visit(db, "open", NEW + 100, 2, open_=1)
    _visit(db, "expired", NEW - 1, 2)
    _visit(db, "at-the-cutoff", NEW, 2)
    _visit(db, "recent", NEW + 1, 2)
    assert _order(_page(db, cutoff=NEW)) == ["recent", "at-the-cutoff"]


def test_only_eats_and_visits_are_asked_about(db):
    for kind in ("eat", "visit", "feed", "import"):
        _visit(db, kind, NEW, 1, kind=kind)
    assert _order(_page(db)) == ["eat", "visit"]


def test_a_split_visit_is_one_visit_holding_all_its_parts_photos(db):
    _visit(db, "s", NEW, 1, hidden=1)  # the hidden parent: its parts replace it on the timeline
    _event(db, "s-cat-a", NEW, kind="eat", parent="s")
    _event(db, "s-cat-b", NEW, kind="visit", parent="s")
    from_a = _photos(db, "s-cat-a", NEW + 10, 2)
    from_b = _photos(db, "s-cat-b", NEW + 20, 2)
    _answer(db, from_b[0])
    page = _page(db)
    (visit,) = page.visits
    assert (visit.uid, visit.kind) == ("s", "eat")
    assert (page.total_visits, page.remaining) == (1, 2)  # one of the five photos is answered
    assert len(visit.picks) == 2
    assert {pick.uid for pick in visit.picks} <= {"s-1", *from_a, from_b[1]}


def test_a_visit_whose_photos_all_belong_to_its_parts_is_still_asked_about(db):
    _event(db, "s", NEW, hidden=1)
    _event(db, "s-part", NEW, kind="eat", parent="s")
    (photo,) = _photos(db, "s-part", NEW + 1, 1)
    page = _page(db)
    assert _order(page) == ["s"]
    assert _photo_uids(page) == [photo]


@pytest.mark.parametrize(
    ("kind", "part_kinds", "expected"),
    [
        ("visit", ("eat", "visit"), "eat"),
        ("visit", ("visit", "visit"), "visit"),
        ("visit", (), "visit"),
        ("eat", ("visit",), "eat"),
    ],
)
def test_a_visit_is_an_eat_when_it_or_one_of_its_parts_is(db, kind, part_kinds, expected):
    _visit(db, "v", NEW, 1, kind=kind)
    for i, part_kind in enumerate(part_kinds):
        _event(db, f"v-part-{i}", NEW, kind=part_kind, parent="v")
    assert [visit.kind for visit in _page(db).visits] == [expected]


def test_a_visit_reports_its_start_kind_and_scene(db):
    _visit(db, "v", NEW + 5, 1, kind="eat", scene="scene-1.jpg", scene_k=4)
    (visit,) = _page(db).visits
    assert (visit.uid, visit.start, visit.kind, visit.scene) == ("v", NEW + 5, "eat", "scene-1.jpg")


@pytest.mark.parametrize(
    ("start", "box", "asked"),
    [
        (OLD, BAD_LEGACY_BOX, False),
        (NEW, BAD_LEGACY_BOX, True),  # after the device fix the same box is trusted outright
        (OLD, GOOD_LEGACY_BOX, True),
        (OLD, None, True),
        (OLD, (0.3, None, 0.7, 0.8), True),  # an incomplete box is no box
    ],
    ids=["wrong-crop", "same-box-after-the-fix", "right-crop", "no-box", "incomplete-box"],
)
def test_legacy_crops_cut_from_the_wrong_place_are_never_asked_about(db, start, box, asked):
    _visit(db, "v", start, 1, photo={"box": box})
    assert bool(_page(db).visits) is asked


def test_untrustworthy_photos_are_dropped_one_by_one(db):
    _event(db, "v", OLD)
    _photo(db, "v-wrong-crop", "v", OLD + 1, box=BAD_LEGACY_BOX)
    _photo(db, "v-right-crop", "v", OLD + 2, box=GOOD_LEGACY_BOX)
    assert _photo_uids(_page(db)) == ["v-right-crop"]


def test_a_photo_is_judged_by_its_own_capture_time(db):
    _event(db, "v", LEGACY - 5)  # the visit began before the fix ...
    _photo(db, "v-before", "v", LEGACY - 1, box=BAD_LEGACY_BOX)
    _photo(db, "v-after", "v", LEGACY + 1, box=BAD_LEGACY_BOX)  # ... and this photo is from after it
    assert _photo_uids(_page(db)) == ["v-after"]


def test_without_a_crop_fix_every_capture_is_legacy(db, monkeypatch):
    monkeypatch.setattr(crop_geometry, "LEGACY_CROP_BEFORE", None)
    _visit(db, "new-wrong-crop", NEW + 100, 1, photo={"box": BAD_LEGACY_BOX})
    _visit(db, "new-unsure", NEW + 50, 1, photo={"guess": "Kitty", "confidence": 0.5})
    _visit(db, "old", OLD, 1)
    # No capture is trusted by its time, and no visit is ahead of another for being recent.
    assert _order(_page(db)) == ["old", "new-unsure"]


@pytest.mark.parametrize(
    ("body", "face", "asked"),
    [("b.jpg", None, True), (None, "f.jpg", True), ("b.jpg", "f.jpg", True), (None, None, False)],
    ids=["body", "face", "both", "no-crop"],
)
def test_a_photo_needs_a_crop_to_be_asked_about(db, body, face, asked):
    _visit(db, "v", NEW, 1, photo={"body": body, "face": face})
    assert bool(_page(db).visits) is asked


# --- the order of the queue -----------------------------------------------------------------


def test_visits_are_ordered_by_era_then_how_little_is_known_then_newest(db):
    unsure_guess = {"guess": "Kitty", "confidence": 0.5}
    confident_guess = {"guess": "Kitty", "confidence": 0.9}
    _visit(db, "new-confident", NEW + 700, 1, photo=confident_guess)
    _visit(db, "new-unsure", NEW + 500, 1, photo=unsure_guess)
    _visit(db, "new-unknown-older", NEW + 100, 1)
    _visit(db, "new-unknown-newer", NEW + 300, 1)
    _visit(db, "old-confident", OLD + 200, 1, photo=confident_guess)
    _visit(db, "old-unknown", OLD + 100, 1)
    assert _order(_page(db)) == [
        "new-unknown-newer",
        "new-unknown-older",
        "new-unsure",
        "new-confident",
        "old-unknown",
        "old-confident",
    ]


def test_a_visit_starting_exactly_when_the_crop_fix_deployed_is_current(db):
    _visit(db, "at-the-fix", LEGACY, 1, photo={"guess": "Kitty", "confidence": 0.5})
    _visit(db, "just-before", LEGACY - 1, 1)  # knows less, but belongs to the old camera view
    assert _order(_page(db)) == ["at-the-fix", "just-before"]


def test_a_guess_is_unsure_below_the_threshold_or_without_a_confidence(db):
    assert UNSURE_BELOW == 0.75
    _visit(db, "at-the-threshold", NEW + 3, 1, photo={"guess": "Kitty", "confidence": UNSURE_BELOW})
    _visit(db, "just-below", NEW + 2, 1, photo={"guess": "Kitty", "confidence": UNSURE_BELOW - 0.01})
    _visit(db, "no-confidence", NEW + 1, 1, photo={"guess": "Kitty"})
    assert _order(_page(db)) == ["just-below", "no-confidence", "at-the-threshold"]


def test_a_visits_tier_comes_from_its_least_known_photo(db):
    _event(db, "mixed", NEW)
    _photo(db, "mixed-1", "mixed", NEW + 1, guess="Kitty", confidence=0.9)
    _photo(db, "mixed-2", "mixed", NEW + 2)
    _visit(db, "newer-but-unsure", NEW + 50, 1, photo={"guess": "Kitty", "confidence": 0.5})
    assert _order(_page(db)) == ["mixed", "newer-but-unsure"]


def test_photos_that_are_not_offered_do_not_set_the_tier(db):
    _event(db, "v", OLD + 100)
    _photo(db, "v-answered", "v", OLD + 101, review="Kitty")  # nothing known, but already answered
    _photo(db, "v-no-crop", "v", OLD + 102, body=None)
    _photo(db, "v-wrong-crop", "v", OLD + 103, box=BAD_LEGACY_BOX)
    _photo(db, "v-asked", "v", OLD + 104, guess="Kitty", confidence=0.9)
    _visit(db, "rival", OLD, 1, photo={"guess": "Kitty", "confidence": 0.5})
    assert _order(_page(db)) == ["rival", "v"]


@pytest.mark.parametrize(
    ("scene_k", "frame_k", "box", "in_scene"),
    [
        (7, 7, BIG, True),
        (7, 7, (0.00, 0.20, 0.50, 0.80), True),  # touching the frame edge is still a real box
        (7, 7, (0.30, 0.20, 1.00, 0.80), True),
        (7, 7, None, False),
        (7, 7, (0.60, 0.20, 0.30, 0.80), False),  # not a rectangle
        (7, 7, (0.50, 0.20, 0.50, 0.80), False),  # no width
        (7, 7, (0.30, 0.20, 1.40, 0.80), False),  # outside the frame
        (7, 7, (-0.10, 0.20, 0.50, 0.80), False),
        (7, 7, (0.30, 0.20, float("inf"), 0.80), False),
        (7, 7, (0.30, None, 0.70, 0.80), False),  # incomplete
        (7, 3, BIG, False),  # a box from another frame of the visit
        (7, None, BIG, False),
        (None, None, BIG, False),  # no scene frame at all: None must not equal None
        (None, 7, BIG, False),
    ],
)
def test_in_scene_marks_a_box_that_belongs_to_the_scene_frame(db, scene_k, frame_k, box, in_scene):
    _visit(db, "v", NEW, 1, scene="scene.jpg", scene_k=scene_k, photo={"box": box, "frame_k": frame_k})
    ((pick,),) = [visit.picks for visit in _page(db).visits]
    assert pick.in_scene is in_scene


# --- pages and the cursor -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "visits"),
    [(1, 1), (3, 1), (4, 2), (6, 2), (7, 3), (29, 10), (30, 10), (500, 10), (0, 1), (-5, 1)],
)
def test_a_page_holds_whole_visits_until_the_limit_is_reached(db, ten, limit, visits):
    page = _page(db, limit=limit)
    assert _order(page) == [f"v{9 - i}" for i in range(visits)]
    assert len(_photo_uids(page)) == 3 * visits
    assert page.has_more is (visits < 10)
    assert (page.cursor is None) is (not page.has_more)


def test_walking_the_cursor_visits_every_photo_once(db, ten):
    walked, photos, cursor = [], [], None
    for _ in range(20):
        page = _page(db, cursor=cursor, limit=7)
        assert (page.remaining, page.total_visits) == (30, 10)  # the whole queue, on every page
        walked += _order(page)
        photos += _photo_uids(page)
        if not page.has_more:
            assert page.cursor is None
            break
        cursor = page.cursor
    else:
        pytest.fail("the cursor never ran out")
    assert walked == [f"v{i}" for i in range(9, -1, -1)]
    assert len(photos) == len(set(photos)) == 30


def test_an_empty_cursor_means_the_first_page(db, ten):
    assert _page(db, cursor="", limit=4) == _page(db, cursor=None, limit=4)


def test_a_cursor_is_a_url_safe_string_that_can_be_reused(db, ten):
    first = _page(db, limit=4)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first.cursor)
    assert _page(db, cursor=first.cursor, limit=4) == _page(db, cursor=first.cursor, limit=4)


@pytest.mark.parametrize("uid", ["v", "vv", "vvv"])  # cursors of 18, 19, 20 bytes: 0, 2, 1 padding
def test_a_cursor_works_whatever_base64_padding_it_would_need(db, uid):
    _visit(db, f"{uid}1", NEW + 100, 3)
    _visit(db, f"{uid}0", NEW, 3)
    first = _page(db, limit=1)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first.cursor)
    assert _order(_page(db, cursor=first.cursor)) == [f"{uid}0"]


@pytest.mark.parametrize(
    "cursor",
    [
        "!!!",
        "not a cursor",
        "é",
        _b64(b"hello"),
        _b64(b"\xff\xfe\x00"),
        _b64(b"[" * 100_000),
        _cursor([0, 0, 5]),
        _cursor([0, 0, 5, "u", 1]),
        _cursor({"era": 0}),
        _cursor("abcd"),
        _cursor(5),
        _cursor(None),
        _cursor([0, 0, 5, 9]),
        _cursor([0, 0, "5", "u"]),
        _cursor([True, 0, 5, "u"]),
        _cursor([0, 0, 5.5, "u"]),
        _cursor([0, None, 5, "u"]),
    ],
)
def test_a_cursor_this_module_did_not_issue_is_refused(db, cursor):
    with pytest.raises(ValueError, match="invalid review cursor"):
        _page(db, cursor=cursor)


def test_answering_a_page_does_not_shift_the_next_one(db):
    for i in range(8):
        _visit(db, f"v{i}", NEW + i * 100, 3)
    first = _page(db, limit=6)
    assert _order(first) == ["v7", "v6"]
    _answer(db, *_photo_uids(first))  # the user works through the page
    second = _page(db, cursor=first.cursor, limit=6)
    assert _order(second) == ["v5", "v4"]
    assert (second.remaining, second.total_visits) == (18, 6)  # the answered visits are gone


def test_a_partly_answered_visit_stays_behind_the_cursor(db):
    for i in range(4):
        _visit(db, f"v{i}", NEW + i * 100, 5)
    first = _page(db, limit=3)
    assert _order(first) == ["v3"]
    _answer(db, first.visits[0].picks[0].uid)
    assert _order(_page(db, cursor=first.cursor, limit=3)) == ["v2"]
    top = _page(db)  # a fresh walk offers what is left of it
    assert _order(top)[0] == "v3"
    assert len(top.visits[0].picks) == 2


def test_new_visits_do_not_shift_a_page_in_progress(db, ten):
    first = _page(db, limit=6)
    _visit(db, "fresh", NEW + 5000, 3)  # newer than everything, so ahead of the cursor
    second = _page(db, cursor=first.cursor, limit=6)
    assert _order(second) == ["v7", "v6"]
    assert second.total_visits == 11
    assert _order(_page(db, limit=3)) == ["fresh"]


def test_a_cursor_at_the_end_of_the_queue_gives_an_empty_page(db):
    _visit(db, "v0", NEW, 3)
    _visit(db, "v1", NEW + 100, 3)
    first = _page(db, limit=3)
    assert (_order(first), first.has_more) == (["v1"], True)
    _answer(db, "v0-1", "v0-2", "v0-3")
    assert _page(db, cursor=first.cursor) == PhotoPage((), 3, 1, None, False)


def test_a_walk_that_answers_as_it_goes_never_shows_an_answered_photo_again(db):
    rng = random.Random(7)
    for i in range(30):
        uid, start = f"v{i:02d}", rng.choice([OLD, NEW]) + i * 10
        _event(db, uid, start)
        for j in range(rng.randrange(1, 8)):
            guess = rng.choice([None, "Kitty"])
            _photo(db, f"{uid}-{j}", uid, start + j, guess=guess, confidence=rng.choice([None, 0.4, 0.9]))
    answered: set[str] = set()
    cursor = None
    for _ in range(200):
        page = _page(db, cursor=cursor, limit=5)
        assert not answered & set(_photo_uids(page))
        firsts = [visit.picks[0].uid for visit in page.visits]  # answer one photo of every visit
        _answer(db, *firsts)
        answered.update(firsts)
        if not page.has_more:
            break
        cursor = page.cursor
    else:
        pytest.fail("the walk never finished")
    assert answered


# --- reading only, and cheaply --------------------------------------------------------------


def test_building_a_page_never_writes(db, ten):
    db.commit()
    path = db.execute("PRAGMA database_list").fetchone()[2]
    reader = sqlite3.connect(path)  # a plain connection: tuple rows, no row factory
    try:
        reader.execute("PRAGMA query_only=ON")  # any write now fails
        page = photo_page(reader, cutoff=0, cursor=None, limit=7, gallery=load_gallery(reader))
    finally:
        reader.close()
    assert page == _page(db, limit=7)
    changes = db.total_changes
    _page(db)
    assert db.total_changes == changes


def test_a_page_costs_two_statements_however_big_the_queue_is(db, ten):
    db.commit()
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        _page(db, limit=7)
    finally:
        db.set_trace_callback(None)
    assert len(statements) == 2  # every visit and photo in one query, then the page's embeddings


def test_an_empty_queue_costs_one_statement(db):
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        _page(db)
    finally:
        db.set_trace_callback(None)
    assert len(statements) == 1


def test_a_page_of_a_big_queue_is_quick(db):
    rng = random.Random(3)
    events, photos = [], []
    for v in range(400):
        start = NEW + v * 600
        kind = "eat" if v % 3 == 0 else "visit"
        events.append((f"v{v}", kind, start, start + 30, 0, None, None, 0, start, None, 0, 0))
        for p in range(8):
            guess = rng.choice([None, "Kitty"])
            box = (0.3, 0.2, 0.3 + 0.1 + rng.random() * 0.3, 0.2 + 0.1 + rng.random() * 0.4)
            photos.append(
                (f"v{v}-{p}", f"v{v}", start + p, "b.jpg", "f.jpg" if p % 3 == 0 else None, guess,
                 rng.random(), *box, rng.random(), None, None, p % 4)
            )
    db.executemany(_INSERT_EVENT, events)
    db.executemany(_INSERT_PHOTO, photos)
    db.commit()
    started = time.perf_counter()
    page = _page(db, limit=30)
    elapsed = time.perf_counter() - started
    assert (page.total_visits, page.remaining, page.has_more) == (400, 1200, True)
    assert elapsed < 1.0


# --- the training gallery and cached embeddings ---------------------------------------------


def test_the_gallery_is_one_normalised_mean_per_class(db):
    # Lengths differ on purpose: every training photo counts the same, however long its vector.
    _train(db, "k1", "Kitty", np.array([3, 0, 0], dtype=np.float32))
    _train(db, "k2", "Kitty", np.array([0, 5, 0], dtype=np.float32))
    _train(db, "m1", "Mochi", np.array([0, 0, 2], dtype=np.float32))
    _train(db, "n1", "not_a_cat", np.array([0, 0, -4], dtype=np.float32))
    gallery = load_gallery(db)
    assert set(gallery.centroids) == {"Kitty", "Mochi", "not_a_cat"}
    np.testing.assert_allclose(gallery.centroids["Kitty"], _unit(1, 1, 0), atol=1e-6)
    np.testing.assert_allclose(gallery.centroids["Mochi"], _unit(0, 0, 1), atol=1e-6)
    np.testing.assert_allclose(gallery.centroids["not_a_cat"], _unit(0, 0, -1), atol=1e-6)


def test_the_gallery_skips_what_is_not_a_usable_body_embedding(db):
    _train(db, "ok", "Kitty", _unit(1, 0, 0))
    _train(db, "face-crop", "Mochi", _unit(0, 1, 0), crop_kind="face")
    _train(db, "other-model", "Mochi", _unit(0, 1, 0), model_id="another-model")
    _train(db, "not-cached-yet", "Mochi")
    _train(db, "all-zero", "Mochi", np.zeros(3, dtype=np.float32))
    _train(db, "not-a-number", "Mochi", np.array([np.nan, 0, 1], dtype=np.float32))
    _train(db, "infinite", "Mochi", np.array([np.inf, 0, 1], dtype=np.float32))
    _train(db, "torn", "Mochi", b"\x01\x02\x03")
    _embed(db, "not-cached-yet", _unit(0, 1, 0), row_kind="sample")  # a sample sharing the uid
    assert set(load_gallery(db).centroids) == {"Kitty"}


def test_the_gallery_survives_a_class_with_embeddings_of_two_sizes(db):
    _train(db, "k1", "Kitty", _unit(1, 0, 0))
    _train(db, "k2", "Kitty", _unit(0, 1, 0, 0))
    assert load_gallery(db).centroids["Kitty"].shape in {(3,), (4,)}


def test_a_class_whose_embeddings_cancel_out_has_no_centroid(db):
    _train(db, "k1", "Kitty", _unit(1, 0, 0))
    _train(db, "k2", "Kitty", _unit(-1, 0, 0))
    _train(db, "m1", "Mochi", _unit(0, 1, 0))
    assert set(load_gallery(db).centroids) == {"Mochi"}


def test_there_is_no_gallery_until_something_is_cached(db):
    assert load_gallery(db) is None
    _train(db, "t1", "Kitty")
    assert load_gallery(db) is None


def test_the_gallery_steers_which_photo_is_picked(db):
    _train(db, "t1", "Kitty", _unit(1, 0, 0))
    photos = _visit(db, "v", NEW, 5)
    _answer(db, photos[3], photos[4])  # one pick left among the first three
    _embed(db, photos[0], _unit(1, 0, 0))  # looks like Kitty: nothing new
    _embed(db, photos[1], _unit(0, 1, 0))  # unlike the whole gallery
    # photos[2] has no embedding yet: neutral
    assert _photo_uids(_page(db, gallery=load_gallery(db))) == [photos[1]]
    assert _photo_uids(_page(db, gallery=None)) == [photos[2]]  # nothing to compare: newest wins


def test_every_visit_of_a_page_gets_its_embeddings(db):
    _train(db, "t1", "Kitty", _unit(1, 0, 0))
    for i in range(3):
        photos = _visit(db, f"v{i}", NEW + i * 100, 4)
        _answer(db, photos[2], photos[3])  # one pick left
        _embed(db, photos[0], _unit(0, 1, 0))  # the older photo is unlike Kitty,
        _embed(db, photos[1], _unit(1, 0, 0))  # the newer one looks just like it
    page = _page(db, gallery=load_gallery(db))
    assert _photo_uids(page) == ["v2-1", "v1-1", "v0-1"]


@pytest.mark.parametrize(
    "wrong_key",
    [{"crop_kind": "face"}, {"model_id": "another-model"}, {"row_kind": "training"}],
    ids=["face-crop", "other-model", "not-a-sample"],
)
def test_only_cached_body_embeddings_of_this_model_count_for_a_photo(db, wrong_key):
    photos = _visit(db, "v", NEW, 4)
    _answer(db, photos[2], photos[3])  # one pick left: the newer of the first two
    _embed(db, photos[1], _unit(1, 0, 0), **wrong_key)  # would read as "known" if it were used
    gallery = Gallery({"Kitty": _unit(1, 0, 0)})
    assert _photo_uids(_page(db, gallery=gallery)) == [photos[1]]


@pytest.mark.parametrize(
    "blob",
    [
        identity.pack(np.zeros(3, dtype=np.float32)),
        identity.pack(np.array([np.nan, 0, 1], dtype=np.float32)),
        identity.pack(np.array([np.inf, 0, 1], dtype=np.float32)),
        b"\x01\x02\x03",
    ],
    ids=["all-zero", "not-a-number", "infinite", "torn"],
)
def test_a_broken_cached_embedding_reads_as_no_embedding(db, blob):
    photos = _visit(db, "v", NEW, 4)
    _answer(db, photos[2], photos[3])  # one pick left: the newer of the first two
    _embed(db, photos[1], blob)  # used as it is, it would read as "known" (or crash)
    gallery = Gallery({"Kitty": _unit(1, 0, 0)})
    assert _photo_uids(_page(db, gallery=gallery)) == [photos[1]]


@pytest.mark.parametrize("chunk", [500, 2, 1])
def test_embeddings_are_found_whatever_the_query_chunk_size(db, monkeypatch, chunk):
    monkeypatch.setattr(photo_queue, "_SQL_CHUNK", chunk)
    _train(db, "t1", "Kitty", _unit(1, 0, 0))
    photos = _visit(db, "v", NEW, 7)
    _answer(db, photos[5], photos[6])  # one pick left among the first five
    # Every photo looks like Kitty, so one whose embedding went unread (neutral) would outrank
    # them all: the most novel photo is picked only when every chunk was read.
    for photo, cosine in zip(photos, [0.9, 0.8, 0.55, 0.7, 0.95]):
        _embed(db, photo, _unit(cosine, math.sqrt(1 - cosine * cosine), 0))
    assert _photo_uids(_page(db, gallery=load_gallery(db))) == [photos[2]]
