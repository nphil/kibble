"""What a person's answer does inside the store: the Kibble 0.29.7 backend contract for
labelling, one-photo review and the learning counter, exercised on a real `_SyncStore` in
`tmp_path` (no Home Assistant, no mocked store). Only the listener tests use `KibbleStore`, the
async facade, and it only needs a stand-in `hass` with an event loop and a config path.

Each test pins one promise a person or the learning job relies on:

- "Can't tell" and "Don't use" mean "do not learn from this": an `unknown` verdict (on a visit
  or on a split session), and a per-photo `skip`, remove the photos' training rows, their files
  and their cached embeddings, and nothing re-adds them -- not a later answer, not auto-learn.
- Saying the same thing twice is a success that changes nothing (no write, no new training row,
  no generation bump).
- `learning.gen` rises with every change to the training gallery; `answers` counts only the
  answers that changed it; a finished pass never moves `done_gen` or `answers_done` backwards;
  `KibbleStore` tells its listener on the event loop after a job that changed the gallery and
  after no other.
- One photo answers the visit (`label_sample`): the visit's cats are derived from its answered
  photos, only answered photos train, undoing an answer hands the visit back, and a meal two
  named cats shared asks "who ate?" exactly when nothing else can say.
- `review_photos_page`: unanswered photos only, a cursor that survives answers between two page
  requests, whole-queue totals.
- `cats_overview` is `kibble/cats` in one call; the recheck sweep never touches a split
  session's child parts; an unchanged verdict costs no write; a Coral embedding batch is
  idempotent.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from kibble import identity, sessions
from kibble.const import DOMAIN
from kibble.ingest import IdentityEngine
from kibble.store import TRAINING_CAP_PER_CAT, KibbleStore, _SyncStore

_MODEL = "test-model"


# --- helpers -----------------------------------------------------------------------------------


def _features(mode: str | None = None) -> identity.Features:
    return identity.Features(face_emb=None, body_feat=None, face_feat=None, mode=mode)


def _insert_event(store: _SyncStore, uid: str, start: int, *, kind: str = "visit", **overrides: Any) -> None:
    fields: dict[str, Any] = dict(
        uid=uid, device_event_id=1, kind=kind, start=start, end=start + 10, open_=False,
        eat_start=None, scene=None, before=None, after=None,
    )
    fields.update(overrides)
    store.upsert_event(**fields)


def _seed_visit(
    store: _SyncStore,
    uid: str,
    start: int,
    *,
    photos: int = 3,
    kind: str = "visit",
    sids: list[int] | None = None,
    box: tuple[float, float, float, float] | None = None,
    **event_overrides: Any,
) -> list[str]:
    """A closed visit with `photos` photos `{uid}-s0`, `{uid}-s1`, ... one second apart, each with
    a real crop file (a label copies it into the training gallery). An eat visit has an
    `eat_start`. `sids` gives each photo its feeder subject."""
    event_overrides.setdefault("eat_start", start + 4 if kind == "eat" else None)
    _insert_event(store, uid, start, kind=kind, **event_overrides)
    sample_uids = []
    for index in range(photos):
        sample_uid = f"{uid}-s{index}"
        asset = f"{sample_uid}.jpg"
        (store.media_root / asset).write_bytes(b"crop " + sample_uid.encode())
        extra: dict[str, Any] = {"frame_k": start + index}
        if sids is not None:
            extra["sid"] = sids[index]
        store.insert_sample(
            uid=sample_uid, event_uid=uid, t=start + index, body=asset, face=None,
            features=_features(), box=box, score=0.9, **extra,
        )
        sample_uids.append(sample_uid)
    return sample_uids


def _subject_scores() -> dict[int, sessions.IdentityScores]:
    return {
        1: sessions.IdentityScores((("Kitty", 0.95), ("_other", 0.05)), 0.75, 0.2),
        2: sessions.IdentityScores((("Pancake", 0.93), ("_other", 0.07)), 0.75, 0.2),
    }


def _seed_two_cat_session(store: _SyncStore, uid: str = "e1", start: int = 100) -> None:
    """One closed meal the feeder tracked two animals through: subject 1 ate, subject 2 only
    visited. Every frame has a photo of each, `{uid}-s{n}` for subject 1 and `{uid}-s{n}-o2`
    for subject 2, each with a real crop file."""
    _insert_event(
        store, uid, start, kind="eat", end=start + 40, eat_start=start + 4,
        scene="scene.jpg", before="before.jpg", after="after.jpg", scene_k=start,
        subjects=[
            {"sid": 1, "first": start, "last": start + 20, "eat_start": start + 4},
            {"sid": 2, "first": start + 4, "last": start + 24, "eat_start": None},
        ],
    )
    frame_boxes = [[1, 0.1, 0.2, 0.4, 0.8], [2, 0.6, 0.2, 0.9, 0.8]]
    for index, offset in enumerate((0, 4, 8)):
        t = start + offset
        for suffix, sid, box in (("", 1, (0.1, 0.2, 0.4, 0.8)), ("-o2", 2, (0.6, 0.2, 0.9, 0.8))):
            sample_uid = f"{uid}-s{index}{suffix}"
            (store.media_root / f"{sample_uid}.jpg").write_bytes(b"crop " + sample_uid.encode())
            store.insert_sample(
                uid=sample_uid, event_uid=uid, t=t, body=f"{sample_uid}.jpg", face=None,
                features=_features(), box=box, score=0.9, sid=sid, frame_k=t,
                is_primary=(sid == 1), frame_boxes=frame_boxes if index == 0 else None,
            )


def _training(store: _SyncStore) -> dict[str, str]:
    return {row["uid"]: row["cat"] for row in store.conn.execute("SELECT uid, cat FROM training")}


def _training_files(store: _SyncStore) -> list[str]:
    return sorted(path.name for path in store.training_root.rglob("*") if path.is_file())


def _cache_embeddings(store: _SyncStore) -> None:
    """One cached Coral embedding for every training row and for every sample."""
    store.set_coral_embeddings(
        "training", [(row["uid"], "body", _MODEL, b"\x01") for row in store.conn.execute("SELECT uid FROM training")]
    )
    store.set_coral_embeddings(
        "sample", [(row["uid"], "body", _MODEL, b"\x02") for row in store.conn.execute("SELECT uid FROM samples")]
    )


def _cached(store: _SyncStore, row_kind: str = "training") -> set[str]:
    return {
        row["row_uid"]
        for row in store.conn.execute("SELECT row_uid FROM coral_embeddings WHERE row_kind=?", (row_kind,))
    }


def _generation(store: _SyncStore) -> int:
    return store.learning_state()["gen"]


def _event(store: _SyncStore, uid: str) -> dict[str, Any]:
    return dict(store.conn.execute("SELECT * FROM events WHERE uid=?", (uid,)).fetchone())


def _verdict(store: _SyncStore, uid: str) -> tuple[Any, ...]:
    """`(cat, identity_status, reviewed, from_photos)`."""
    row = _event(store, uid)
    return row["cat"], row["identity_status"], row["reviewed"], row["from_photos"]


def _children(store: _SyncStore, uid: str) -> list[str]:
    return [row["uid"] for row in store.conn.execute("SELECT uid FROM events WHERE parent_uid=? ORDER BY rowid", (uid,))]


def _cats_by_ate(visit: dict[str, Any]) -> dict[str, bool]:
    return {entry["cat"]: entry["ate"] for entry in visit["cats"]}


def _state(store: _SyncStore) -> dict[str, Any]:
    """Everything a no-op has to leave exactly as it found it."""
    return {
        "learning": store.learning_state(),
        "training": _training(store),
        "events": [dict(row) for row in store.conn.execute("SELECT * FROM events ORDER BY uid")],
        "samples": [dict(row) for row in store.conn.execute("SELECT uid, review FROM samples ORDER BY uid")],
    }


@pytest.fixture
def store(tmp_path: Path) -> Iterator[_SyncStore]:
    sync = _SyncStore(tmp_path, "entry1")
    sync.add_cat("Kitty")
    sync.add_cat("Pancake")
    yield sync
    sync.close()


# --- "Can't tell" and "Don't use": nothing is learned from it ----------------------------------


def test_cant_tell_on_a_visit_removes_each_training_photo_with_its_file_and_cached_embedding(
    store: _SyncStore,
) -> None:
    _seed_visit(store, "e1", 1000)
    _seed_visit(store, "e2", 2000, photos=2)
    store.label_events(["e1"], "Kitty")
    store.label_events(["e2"], "Pancake")
    _cache_embeddings(store)
    assert len(_training(store)) == len(_cached(store)) == len(_training_files(store)) == 5
    generation = _generation(store)

    store.label_events(["e1"], "unknown")

    assert _training(store) == {"e2-s0-train": "Pancake", "e2-s1-train": "Pancake"}
    assert _cached(store) == {"e2-s0-train", "e2-s1-train"}
    assert _training_files(store) == ["e2-s0-train-body.jpg", "e2-s1-train-body.jpg"]
    assert _verdict(store, "e1")[:3] == (None, "unknown", 1)
    assert _generation(store) > generation, "the learning job has to hear that the gallery shrank"
    # an embedding belongs to the crop, which is still there: only the training copies go
    assert {"e1-s0", "e1-s1", "e1-s2"} <= _cached(store, "sample")


@pytest.mark.parametrize("target", ["e1", "e1-cat-pancake"], ids=["the session", "one of its cat parts"])
def test_cant_tell_on_a_split_session_unsplits_it_and_removes_every_photos_training_row(
    store: _SyncStore, target: str
) -> None:
    _seed_two_cat_session(store)
    store.label_sample("e1-s0", "Kitty", _subject_scores())
    store.label_sample("e1-s1-o2", "Pancake", _subject_scores())
    assert _children(store, "e1") == ["e1-cat-kitty", "e1-cat-pancake"]
    assert _training(store) == {"e1-s0-train": "Kitty", "e1-s1-o2-train": "Pancake"}
    _cache_embeddings(store)

    store.label_events([target], "unknown")

    assert _children(store, "e1") == []
    assert _event(store, "e1")["hidden"] == 0
    assert _verdict(store, "e1")[:3] == (None, "unknown", 1)
    assert _training(store) == {}
    assert _cached(store) == set()
    assert _training_files(store) == []


def test_cant_tell_wins_over_the_answers_of_the_visits_individual_photos(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_sample("e1-s0", "Kitty")
    store.label_sample("e1-s1", "Kitty")
    assert set(_training(store)) == {"e1-s0-train", "e1-s1-train"}

    store.label_events(["e1"], "unknown")

    assert _training(store) == {}
    assert store.reconcile_session_training("e1") is False, "nothing is left to bring in line"
    assert _training(store) == {}


@pytest.mark.parametrize("review", ["skip", "both", "unknown", "not_a_cat"])
def test_auto_learn_never_adds_a_photo_whose_own_answer_says_not_to_learn_from_it(
    store: _SyncStore, review: str
) -> None:
    _seed_visit(store, "e1", 1000, photos=2)
    store.conn.execute("UPDATE samples SET review=? WHERE uid='e1-s0'", (review,))
    store.conn.commit()

    assert store.add_auto_training(cat="Kitty", sample_uid="e1-s0", confidence=0.99) is False
    assert _training(store) == {}
    # the neighbour nobody answered is still welcome, so it was the answer that blocked the first
    assert store.add_auto_training(cat="Kitty", sample_uid="e1-s1", confidence=0.99) is True
    assert set(_training(store)) == {"e1-s1-train"}


def test_auto_learn_never_adds_a_photo_of_a_visit_that_was_marked_cant_tell(store: _SyncStore) -> None:
    sample_uids = _seed_visit(store, "e1", 1000)
    store.label_events(["e1"], "unknown")

    for sample_uid in sample_uids:
        assert store.add_auto_training(cat="Kitty", sample_uid=sample_uid, confidence=0.99) is False
    assert _training(store) == {}
    assert _training_files(store) == []


def test_a_photo_marked_dont_use_loses_its_training_row_file_and_embedding_and_only_those(
    store: _SyncStore,
) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_events(["e1"], "Kitty")
    _cache_embeddings(store)
    generation = _generation(store)

    result = store.label_sample("e1-s1", "skip")

    assert result is not None and result["sample"]["review"] == "skip" and result["sample"]["label"] == "skip"
    assert set(_training(store)) == {"e1-s0-train", "e1-s2-train"}, "the visit's other photos still teach"
    assert _cached(store) == {"e1-s0-train", "e1-s2-train"}
    assert _training_files(store) == ["e1-s0-train-body.jpg", "e1-s2-train-body.jpg"]
    assert _generation(store) > generation
    assert store.add_auto_training(cat="Kitty", sample_uid="e1-s1", confidence=0.99) is False
    assert set(_training(store)) == {"e1-s0-train", "e1-s2-train"}


def test_dont_use_on_the_only_photo_that_taught_anything_leaves_the_gallery_empty(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_sample("e1-s0", "Kitty")
    assert _training(store) == {"e1-s0-train": "Kitty"}
    _cache_embeddings(store)

    store.label_sample("e1-s0", "skip")

    assert _training(store) == {}
    assert _cached(store) == set()
    assert _training_files(store) == []
    assert store.add_auto_training(cat="Kitty", sample_uid="e1-s0", confidence=0.99) is False


# The same promises one level up: the engine's own auto-learn decision, over a real store.


class _EngineHass:
    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


class _EngineStore:
    """The store calls `IdentityEngine.async_maybe_auto_learn` makes, forwarded to a real
    `_SyncStore` (the async facade only adds a thread hop)."""

    def __init__(self, sync: _SyncStore) -> None:
        self._sync = sync

    async def async_event_training_context(self, uid: str) -> Any:
        return self._sync.event_training_context(uid)

    async def async_auto_learn_paused(self, cat: str) -> bool:
        return self._sync.auto_learn_paused(cat)

    async def async_sample_guesses(self, uid: str) -> Any:
        return self._sync.sample_guesses(uid)

    async def async_training_feats_for_cat(self, cat: str, mode: str | None) -> Any:
        return self._sync.training_feats_for_cat(cat, mode)

    async def async_add_auto_training(self, **kwargs: Any) -> bool:
        return self._sync.add_auto_training(**kwargs)


def _seed_confident_visit(store: _SyncStore, uid: str = "e1", start: int = 1000, photos: int = 4) -> list[str]:
    """A closed meal the engine named Kitty: every photo guessed "Kitty" at 95% and each looking
    different enough from the others for auto-learn's diversity filter to keep all of them."""
    _insert_event(store, uid, start, kind="eat", eat_start=start + 4)
    sample_uids = []
    for index in range(photos):
        angle = index * math.pi / 6
        feature = np.array([math.cos(angle), math.sin(angle)], dtype=np.float32)
        sample_uid = f"{uid}-s{index}"
        (store.media_root / f"{sample_uid}.jpg").write_bytes(b"crop")
        store.insert_sample(
            uid=sample_uid, event_uid=uid, t=start + index, body=f"{sample_uid}.jpg", face=None,
            features=identity.Features(face_emb=None, body_feat=feature, face_feat=None, mode="day"),
            box=None, score=0.9,
        )
        sample_uids.append(sample_uid)
    store.conn.execute("UPDATE samples SET guess='Kitty', guess_confidence=0.95 WHERE event_uid=?", (uid,))
    store.conn.execute(
        "UPDATE events SET cat='Kitty', identity_status='auto', confidence=0.95 WHERE uid=?", (uid,)
    )
    store.conn.commit()
    return sample_uids


async def test_the_engine_auto_learns_a_confident_visit_nobody_has_answered(store: _SyncStore) -> None:
    _seed_confident_visit(store)
    engine = IdentityEngine(_EngineHass(), _EngineStore(store))

    assert await engine.async_maybe_auto_learn("e1") is True

    assert set(_training(store)) == {f"e1-s{index}-train" for index in range(4)}, (
        "the control: without an answer standing in the way, every photo is learned"
    )


async def test_the_engine_never_auto_learns_a_visit_marked_cant_tell(store: _SyncStore) -> None:
    sample_uids = _seed_confident_visit(store)
    store.label_events(["e1"], "unknown")
    engine = IdentityEngine(_EngineHass(), _EngineStore(store))

    assert await engine.async_maybe_auto_learn("e1") is False

    assert _training(store) == {}
    for sample_uid in sample_uids:
        assert store.add_auto_training(cat="Kitty", sample_uid=sample_uid, confidence=0.99) is False


@pytest.mark.parametrize("review", ["skip", "both", "unknown", "not_a_cat"])
async def test_the_engine_skips_just_the_photo_whose_answer_says_not_to_learn_it(
    store: _SyncStore, review: str
) -> None:
    _seed_confident_visit(store)
    store.conn.execute("UPDATE samples SET review=? WHERE uid='e1-s0'", (review,))
    store.conn.commit()
    engine = IdentityEngine(_EngineHass(), _EngineStore(store))

    assert await engine.async_maybe_auto_learn("e1") is True

    assert set(_training(store)) == {"e1-s1-train", "e1-s2-train", "e1-s3-train"}
    assert store.add_auto_training(cat="Kitty", sample_uid="e1-s0", confidence=0.99) is False


async def test_the_engine_does_not_auto_learn_a_visit_a_person_answered_a_photo_of(store: _SyncStore) -> None:
    _seed_confident_visit(store)
    store.label_sample("e1-s0", "skip")
    engine = IdentityEngine(_EngineHass(), _EngineStore(store))

    assert await engine.async_maybe_auto_learn("e1") is False

    assert _training(store) == {}


# --- the same answer twice is a no-op ----------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "teaches"), [("Kitty", True), ("not_a_cat", True), ("unknown", False)]
)
def test_the_same_label_on_a_visit_twice_changes_nothing_the_second_time(
    store: _SyncStore, label: str, teaches: bool
) -> None:
    _seed_visit(store, "e1", 1000)
    first = store.label_events(["e1"], label)
    # the first answer owes the learning job something only if it changed the training gallery
    assert (store.take_learning_mark() is not None) is teaches
    store.conn.execute("UPDATE events SET updated=1")  # an untouched row keeps this
    store.conn.commit()
    after_first = _state(store)

    second = store.label_events(["e1"], label)

    assert _state(store) == after_first
    assert [event["uid"] for event in second] == [event["uid"] for event in first] == ["e1"]
    assert store.take_learning_mark() is None, "the retry owes the learning job nothing"


@pytest.mark.parametrize("target", ["e1", "e1-cat-kitty"], ids=["the session", "one of its cat parts"])
def test_the_same_label_on_a_split_session_twice_changes_nothing_the_second_time(
    store: _SyncStore, target: str
) -> None:
    _seed_two_cat_session(store)
    assert store.replan_session("e1", _subject_scores()) is True
    store.label_events([target], "Kitty")
    assert _children(store, "e1") == [], "a whole-session label leaves one visit, not two cats"
    store.conn.execute("UPDATE events SET updated=1")
    store.conn.commit()
    after_first = _state(store)

    store.label_events([target if target == "e1" else "e1"], "Kitty")

    assert _state(store) == after_first


def test_the_same_answer_on_a_photo_twice_changes_nothing_the_second_time(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    first = store.label_sample("e1-s0", "Kitty")
    store.conn.execute("UPDATE events SET updated=1")
    store.conn.commit()
    after_first = _state(store)

    second = store.label_sample("e1-s0", "Kitty")

    assert second == first
    assert _state(store) == after_first
    assert _training(store) == {"e1-s0-train": "Kitty"}, "still one row, not two"


def test_the_same_dont_use_on_a_photo_twice_changes_nothing_the_second_time(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_sample("e1-s0", "Kitty")
    store.label_sample("e1-s0", "skip")
    store.conn.execute("UPDATE events SET updated=1")
    store.conn.commit()
    after_first = _state(store)

    store.label_sample("e1-s0", "skip")

    assert _state(store) == after_first


def test_a_label_for_a_cat_that_is_not_enrolled_is_refused_and_changes_nothing(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    before = _state(store)

    with pytest.raises(ValueError, match="unknown cat: Nobody"):
        store.label_events(["e1"], "Nobody")
    with pytest.raises(ValueError, match="unknown cat: Nobody"):
        store.label_sample("e1-s0", "Nobody")

    assert _state(store) == before


def test_labelling_a_visit_that_does_not_exist_is_skipped(store: _SyncStore) -> None:
    assert store.label_events(["nope"], "Kitty") == []
    assert store.label_sample("nope-s0", "Kitty") is None
    assert store.learning_state()["gen"] == 0


# --- the generation counter and the learning-state API -----------------------------------------


def test_a_fresh_store_has_nothing_owed_and_nothing_to_report(store: _SyncStore) -> None:
    assert store.learning_state() == {
        "gen": 0, "done_gen": 0, "answers": 0, "answers_done": 0, "queued": 0,
        "last_result": None, "counts_done": None, "error": None,
    }
    assert store.take_learning_mark() is None
    assert store.training_counts_by_cat() == {}


def test_every_kind_of_change_to_the_training_gallery_advances_the_generation(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    _seed_visit(store, "e2", 2000)
    for index in range(TRAINING_CAP_PER_CAT + 5):
        vector = [0.0] * 120
        vector[index % 120] = 1.0  # far apart, so none of them is redundant with another
        store.conn.execute(
            "INSERT INTO training(uid, cat, source, created, body, face, face_emb, body_feat, "
            "face_feat, mode, confidence) VALUES (?, 'Pancake', 'auto', ?, NULL, NULL, NULL, ?, NULL, 'day', 0.9)",
            (f"auto-{index}", index, identity.pack(np.array(vector, dtype="float32"))),
        )
    store.conn.commit()
    assert _generation(store) == 0, "rows written around the store's own paths are not its business"

    def advances(name: str, action: Any) -> None:
        before = _generation(store)
        action()
        assert _generation(store) > before, f"{name} did not advance the generation"

    advances("labelling a visit", lambda: store.label_events(["e1"], "Kitty"))
    advances("answering a photo", lambda: store.label_sample("e2-s0", "Kitty"))
    advances("uploading a photo", lambda: store.add_upload_training(cat="Kitty", uid="u1", data=b"x", features=_features()))
    advances("auto-learning a photo", lambda: store.add_auto_training(cat="Kitty", sample_uid="e2-s1", confidence=0.95))
    advances("removing a training photo", lambda: store.training_remove(["u1"]))
    advances("a cap evicting old photos", store.enforce_training_caps)
    advances("clearing a cat's training", lambda: store.clear_training("Kitty", keep_uploads=False))
    advances("new Coral embeddings arriving", store.touch_training_generation)


def test_changes_that_leave_the_gallery_alone_do_not_advance_the_generation(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_events(["e1"], "Kitty")
    store.take_learning_mark()
    generation = _generation(store)

    store.enforce_training_caps()  # nothing over any cap
    store.clear_training("Pancake", keep_uploads=False)  # a cat with nothing to clear
    assert store.training_remove(["never-existed"]) == 0
    store.label_events(["e1"], "Kitty")  # a retry
    store.set_coral_embeddings("training", [("e1-s0-train", "body", _MODEL, b"\x01")])

    assert _generation(store) == generation
    assert store.take_learning_mark() is None


def test_the_learning_mark_is_handed_out_once_per_change(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    assert store.take_learning_mark() is None

    store.label_events(["e1"], "Kitty")
    state = store.learning_state()

    assert store.take_learning_mark() == (state["gen"], state["answers"])
    assert store.take_learning_mark() is None

    store.label_events(["e1"], "unknown")
    assert store.take_learning_mark() == (store.learning_state()["gen"], 2)
    assert store.take_learning_mark() is None


def test_only_answers_that_changed_the_gallery_are_counted(store: _SyncStore) -> None:
    _seed_visit(store, "bare", 500, photos=0)
    _seed_visit(store, "e1", 1000)
    _seed_visit(store, "e2", 2000)
    _seed_visit(store, "e3", 3000)
    _seed_visit(store, "e4", 4000, photos=2)

    store.label_events(["bare"], "Kitty")
    assert _verdict(store, "bare")[:3] == ("Kitty", "reviewed", 1), "the answer is saved..."
    assert store.learning_state()["answers"] == 0, "...but a visit with no photos teaches nothing"

    store.label_events(["e1"], "Kitty")
    assert store.learning_state()["answers"] == 1

    store.label_events(["e1"], "Kitty")
    assert store.learning_state()["answers"] == 1, "a retry is not a second answer"

    store.label_events(["e2", "e3"], "Pancake")
    assert store.learning_state()["answers"] == 3, "two visits, two answers"

    store.label_sample("e4-s0", "skip")
    assert store.learning_state()["answers"] == 3, "skipping a photo that taught nothing changes nothing"

    store.label_sample("e4-s1", "Kitty")
    assert store.learning_state()["answers"] == 4


def test_learning_state_counts_what_waits_in_the_queue(store: _SyncStore) -> None:
    store.conn.execute("INSERT INTO learning_queue(session_uid) VALUES ('e1'), ('e2')")
    store.conn.commit()
    assert store.learning_state()["queued"] == 2


def test_a_finished_pass_records_what_it_covered_and_what_it_learned(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    _seed_visit(store, "e2", 2000)
    store.label_events(["e1"], "Kitty")
    store.label_events(["e2"], "Pancake")
    state = store.learning_state()
    result = {"learned": {"Kitty": 3, "Pancake": 3}, "renamed": 1}

    store.learning_complete(gen=state["gen"], answers=state["answers"], result=result, counts={"Kitty": 3, "Pancake": 3})

    done = store.learning_state()
    assert (done["done_gen"], done["answers_done"]) == (state["gen"], state["answers"])
    assert done["last_result"] == result
    assert done["counts_done"] == {"Kitty": 3, "Pancake": 3}
    assert done["error"] is None
    assert done["gen"] == state["gen"] and done["answers"] == state["answers"], "reporting is not changing"


def test_a_pass_finishing_late_never_moves_done_gen_or_answers_done_backwards(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    _seed_visit(store, "e2", 2000)
    store.label_events(["e1"], "Kitty")
    store.label_events(["e2"], "Pancake")
    state = store.learning_state()
    store.learning_complete(gen=state["gen"], answers=2, result={"pass": "new"}, counts={"Kitty": 3})

    store.learning_complete(gen=state["gen"] - 4, answers=1, result={"pass": "late"}, counts={"Kitty": 1})

    done = store.learning_state()
    assert (done["done_gen"], done["answers_done"]) == (state["gen"], 2)
    assert done["last_result"] == {"pass": "late"} and done["counts_done"] == {"Kitty": 1}, (
        "what is reported is whatever pass finished last"
    )


def test_a_pass_cannot_claim_more_answers_than_were_ever_saved(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_events(["e1"], "Kitty")

    store.learning_complete(gen=99, answers=50, result={}, counts={})

    done = store.learning_state()
    assert done["answers_done"] == done["answers"] == 1
    assert done["done_gen"] == 99, "the generation it covered is taken as reported"


def test_a_failed_pass_leaves_its_error_until_the_next_pass_finishes(store: _SyncStore) -> None:
    store.learning_failed("CoralHub did not answer")
    assert store.learning_state()["error"] == "CoralHub did not answer"
    assert store.learning_state()["done_gen"] == 0, "a failure covers nothing"

    store.learning_complete(gen=0, answers=0, result={}, counts={})

    assert store.learning_state()["error"] is None


def test_the_per_cat_counts_are_the_baseline_the_next_pass_measures_from(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_events(["e1"], "Kitty")
    assert store.training_counts_by_cat() == {"Kitty": 3}

    store.learning_set_counts(store.training_counts_by_cat())

    assert store.learning_state()["counts_done"] == {"Kitty": 3}


# --- KibbleStore tells its listener after a job that changed the gallery -----------------------


@pytest.fixture
async def facade(tmp_path: Path) -> AsyncIterator[KibbleStore]:
    hass = SimpleNamespace(
        loop=asyncio.get_running_loop(),
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
    )
    seed = _SyncStore(Path(hass.config.path(DOMAIN, "entry1")), "entry1")
    seed.add_cat("Kitty")
    seed.add_cat("Pancake")
    _seed_visit(seed, "e1", 1000)
    _seed_visit(seed, "e2", 2000, photos=2)
    seed.close()
    opened = KibbleStore(hass, "entry1")
    await opened.async_setup()
    yield opened
    await opened.async_close()


async def _settle() -> None:
    await asyncio.sleep(0)  # the store thread queues the hand-off on the loop


async def test_the_listener_hears_about_every_job_that_changed_the_gallery_and_about_no_other(
    facade: KibbleStore,
) -> None:
    heard: list[tuple[int, int, int]] = []
    facade.set_learning_listener(lambda gen, answers: heard.append((gen, answers, threading.get_ident())))

    await facade.async_learning_state()
    await facade.async_review_photos_page(limit=3, cursor=None, retention_cutoff=0)
    await facade.async_cats_overview(14)
    await facade.async_event_detail("e1")
    await _settle()
    assert heard == [], "reading is not changing"

    writes = [
        ("labelling a visit", lambda: facade.async_label_events(["e1"], "Kitty")),
        ("answering a photo", lambda: facade.async_label_sample("e2-s0", "Pancake")),
        ("uploading a photo", lambda: facade.async_add_upload_training(
            cat="Kitty", uid="u1", data=b"jpeg", features=_features())),
        ("auto-learning a photo", lambda: facade.async_add_auto_training(
            cat="Pancake", sample_uid="e2-s1", confidence=0.95)),
        ("removing a training photo", lambda: facade.async_training_remove(["u1"])),
        ("clearing a cat's training", lambda: facade.async_clear_training("Kitty", keep_uploads=False)),
    ]
    for name, write in writes:
        before = len(heard)
        await write()
        await _settle()
        state = await facade.async_learning_state()
        assert len(heard) == before + 1, f"{name}: the listener should hear exactly once"
        assert heard[-1][:2] == (state["gen"], state["answers"]), name
        assert heard[-1][2] == threading.get_ident(), f"{name}: the listener runs on the event loop"

    settled = len(heard)
    await facade.async_label_events(["e1"], "Kitty")  # a retry: the visit already says Kitty
    await facade.async_label_sample("e2-s0", "Pancake")
    await _settle()
    assert len(heard) == settled, "a retry changes nothing, so nobody is told"


async def test_a_refused_label_tells_nobody(facade: KibbleStore) -> None:
    heard: list[tuple[int, int]] = []
    facade.set_learning_listener(lambda gen, answers: heard.append((gen, answers)))

    with pytest.raises(ValueError, match="unknown cat"):
        await facade.async_label_events(["e1"], "Nobody")
    await _settle()

    assert heard == []


async def test_a_store_with_no_listener_carries_on_and_a_removed_listener_goes_quiet(facade: KibbleStore) -> None:
    heard: list[tuple[int, int]] = []
    facade.set_learning_listener(lambda gen, answers: heard.append((gen, answers)))
    await facade.async_label_events(["e1"], "Kitty")
    await _settle()
    assert len(heard) == 1

    facade.set_learning_listener(None)
    await facade.async_label_events(["e2"], "Pancake")
    await _settle()

    assert len(heard) == 1
    assert (await facade.async_learning_state())["answers"] == 2, "the answer was still counted"


async def test_kibble_cats_costs_the_store_thread_one_job(facade: KibbleStore) -> None:
    jobs: list[str] = []
    run_job = facade._job

    def counting_job(func: Any, *args: Any) -> Any:
        jobs.append(getattr(func, "__name__", repr(func)))
        return run_job(func, *args)

    facade._job = counting_job  # type: ignore[method-assign]

    overview = await facade.async_cats_overview(14)

    assert jobs == ["cats_overview"]
    assert [cat["name"] for cat in overview["cats"]] == ["Kitty", "Pancake"]


# --- one photo answers the visit (label_sample) -------------------------------------------------


def test_answering_one_photo_names_the_visit_and_teaches_only_that_photo(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)

    result = store.label_sample("e1-s0", "Kitty")

    assert result is not None
    assert result["visit"] == {"uid": "e1", "kind": "visit", "cats": [{"cat": "Kitty", "ate": False}], "needs_ate": False}
    assert result["sample"]["review"] == result["sample"]["label"] == "Kitty"
    assert _verdict(store, "e1") == ("Kitty", "reviewed", 1, 1)
    assert _training(store) == {"e1-s0-train": "Kitty"}, "he vouched for one photo, not for its neighbours"


def test_a_meal_with_one_named_cat_says_that_cat_ate(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000, kind="eat")

    result = store.label_sample("e1-s0", "Kitty")

    assert result is not None
    assert result["visit"] == {"uid": "e1", "kind": "eat", "cats": [{"cat": "Kitty", "ate": True}], "needs_ate": False}


def test_a_visit_whose_photos_name_two_cats_becomes_two_cat_parts(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)

    store.label_sample("e1-s0", "Kitty")
    result = store.label_sample("e1-s1", "Pancake")

    assert result is not None
    assert _cats_by_ate(result["visit"]) == {"Kitty": False, "Pancake": False}
    assert _children(store, "e1") == ["e1-cat-kitty", "e1-cat-pancake"]
    assert _event(store, "e1")["hidden"] == 1
    for child, cat in (("e1-cat-kitty", "Kitty"), ("e1-cat-pancake", "Pancake")):
        assert _verdict(store, child)[:3] == (cat, "reviewed", 1)
    assert _training(store) == {"e1-s0-train": "Kitty", "e1-s1-train": "Pancake"}


def test_two_cats_in_one_feeder_tracked_meal_keep_each_photo_with_its_own_animal(store: _SyncStore) -> None:
    _seed_two_cat_session(store)

    store.label_sample("e1-s0", "Kitty", _subject_scores())
    result = store.label_sample("e1-s1-o2", "Pancake", _subject_scores())

    assert result is not None
    assert _cats_by_ate(result["visit"]) == {"Kitty": True, "Pancake": False}, "the feeder saw who ate"
    assert result["visit"]["needs_ate"] is False
    kitty_photos = {r["uid"] for r in store.conn.execute("SELECT uid FROM samples WHERE event_uid='e1-cat-kitty'")}
    assert kitty_photos == {"e1-s0", "e1-s1", "e1-s2"}
    assert _training(store) == {"e1-s0-train": "Kitty", "e1-s1-o2-train": "Pancake"}


def test_a_visit_whose_every_photo_is_not_a_cat_is_not_a_cat_and_every_photo_teaches_that(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000, photos=2)

    store.label_sample("e1-s0", "not_a_cat")
    result = store.label_sample("e1-s1", "not_a_cat")

    assert result is not None and result["visit"]["cats"] == []
    assert _verdict(store, "e1") == (None, "not_a_cat", 1, 1)
    assert _training(store) == {"e1-s0-train": "not_a_cat", "e1-s1-train": "not_a_cat"}


def test_a_visit_whose_every_photo_is_dont_use_has_no_cat_and_teaches_nothing(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000, photos=2)

    store.label_sample("e1-s0", "skip")
    result = store.label_sample("e1-s1", "skip")

    assert result is not None and result["visit"]["cats"] == []
    assert _verdict(store, "e1") == (None, "unknown", 1, 1)
    assert _training(store) == {}


def test_a_not_a_cat_photo_in_a_cat_visit_teaches_not_a_cat_without_changing_the_visits_cat(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)

    store.label_sample("e1-s0", "Pancake")
    result = store.label_sample("e1-s1", "not_a_cat")

    assert result is not None and result["sample"]["label"] == "not_a_cat"
    assert _verdict(store, "e1") == ("Pancake", "reviewed", 1, 1)
    assert _training(store) == {"e1-s0-train": "Pancake", "e1-s1-train": "not_a_cat"}


def test_a_dont_use_photo_does_not_stop_its_neighbour_naming_the_visit(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000, photos=2)

    store.label_sample("e1-s0", "skip")
    store.label_sample("e1-s1", "Kitty")

    assert _verdict(store, "e1") == ("Kitty", "reviewed", 1, 1)
    assert _training(store) == {"e1-s1-train": "Kitty"}


# both: the photo shows both of the household's two cats


def test_a_both_photo_adds_both_cats_to_the_visit_and_teaches_nothing(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)

    result = store.label_sample("e1-s0", "both")

    assert result is not None
    assert result["sample"]["label"] == result["sample"]["review"] == "both"
    assert _cats_by_ate(result["visit"]) == {"Kitty": False, "Pancake": False}
    assert _children(store, "e1") == ["e1-cat-kitty", "e1-cat-pancake"]
    assert _training(store) == {}, "a photo of two cats cannot teach either one what it looks like"
    assert store.add_auto_training(cat="Kitty", sample_uid="e1-s0", confidence=0.99) is False


@pytest.mark.parametrize("cats", [["Kitty"], ["Kitty", "Pancake", "Waffle"]], ids=["one cat", "three cats"])
def test_both_needs_exactly_two_cats_enrolled(tmp_path: Path, cats: list[str]) -> None:
    store = _SyncStore(tmp_path, "entry1")
    for cat in cats:
        store.add_cat(cat)
    _seed_visit(store, "e1", 1000)
    before = _state(store)

    with pytest.raises(ValueError, match="'both' needs exactly two cats enrolled"):
        store.label_sample("e1-s0", "both")

    assert _state(store) == before
    store.close()


# undo: answering back, or answering again


def test_answering_back_a_photo_hands_the_visit_back_to_the_engine(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000, kind="eat")
    store.label_sample("e1-s0", "Kitty")
    store.set_session_ate("e1", ["Kitty"])
    generation = _generation(store)

    result = store.label_sample("e1-s0", "follow")

    assert result is not None and result["visit"]["cats"] == []
    row = _event(store, "e1")
    assert (row["cat"], row["identity_status"], row["reviewed"], row["from_photos"], row["ate_confirmed"]) == (
        None, "unknown", 0, 0, 0
    )
    assert _training(store) == {}
    assert _generation(store) > generation


def test_a_dont_use_photo_that_is_answered_back_leaves_no_trace(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_sample("e1-s0", "skip")
    assert _verdict(store, "e1") == (None, "unknown", 1, 1)

    store.label_sample("e1-s0", "follow")

    assert _verdict(store, "e1") == (None, "unknown", 0, 0)
    assert store.conn.execute("SELECT review FROM samples WHERE uid='e1-s0'").fetchone()["review"] is None


def test_answering_a_photo_again_replaces_the_first_answer(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_sample("e1-s0", "Kitty")

    result = store.label_sample("e1-s0", "Pancake")

    assert result is not None and result["visit"]["cats"] == [{"cat": "Pancake", "ate": False}]
    assert _verdict(store, "e1") == ("Pancake", "reviewed", 1, 1)
    assert _training(store) == {"e1-s0-train": "Pancake"}


def test_answering_back_one_of_two_cats_collapses_the_visit_to_the_cat_still_named(store: _SyncStore) -> None:
    _seed_two_cat_session(store)
    store.label_sample("e1-s0", "Kitty", _subject_scores())
    store.label_sample("e1-s1-o2", "Pancake", _subject_scores())
    assert len(_children(store, "e1")) == 2

    result = store.label_sample("e1-s1-o2", "follow", _subject_scores())

    assert result is not None and result["visit"]["cats"] == [{"cat": "Kitty", "ate": True}]
    assert _children(store, "e1") == []
    assert _event(store, "e1")["hidden"] == 0
    assert _training(store) == {"e1-s0-train": "Kitty"}


def test_answering_back_a_not_a_cat_photo_leaves_the_cat_it_was_next_to(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_sample("e1-s0", "Kitty")
    store.label_sample("e1-s1", "not_a_cat")

    store.label_sample("e1-s1", "follow")

    assert _verdict(store, "e1") == ("Kitty", "reviewed", 1, 1)
    assert _training(store) == {"e1-s0-train": "Kitty"}


# legacy visit-level labels versus photo-driven visits


def test_a_visit_labelled_as_a_whole_keeps_teaching_every_photo_that_follows_it(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)

    store.label_events(["e1"], "Kitty")

    assert _verdict(store, "e1") == ("Kitty", "reviewed", 1, 0)
    assert set(_training(store)) == {"e1-s0-train", "e1-s1-train", "e1-s2-train"}


def test_answering_a_photo_of_a_cant_tell_visit_turns_it_into_a_photo_driven_one(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_events(["e1"], "unknown")
    assert _verdict(store, "e1") == (None, "unknown", 1, 0)

    result = store.label_sample("e1-s0", "Kitty")

    assert result is not None and result["visit"]["cats"] == [{"cat": "Kitty", "ate": False}]
    assert _verdict(store, "e1") == ("Kitty", "reviewed", 1, 1)
    assert _training(store) == {"e1-s0-train": "Kitty"}, "the other photos are not vouched for"


def test_a_photo_answer_on_a_visit_labelled_as_a_whole_adds_a_cat_to_it(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_events(["e1"], "Kitty")

    result = store.label_sample("e1-s1", "Pancake")

    assert result is not None
    assert _cats_by_ate(result["visit"]) == {"Kitty": False, "Pancake": False}
    assert _children(store, "e1") == ["e1-cat-kitty", "e1-cat-pancake"]
    assert result["sample"]["label"] == "Pancake"


def test_label_sample_refuses_an_unknown_cat_and_an_unknown_photo(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)

    with pytest.raises(ValueError, match="unknown cat: Nope"):
        store.label_sample("e1-s0", "Nope")

    assert store.label_sample("nope-s0", "Kitty") is None


# who ate


def _answer_a_meal_two_cats_shared(store: _SyncStore, uid: str = "e1", **overrides: Any) -> dict[str, Any]:
    _seed_visit(store, uid, 1000, kind="eat", **overrides)
    store.label_sample(f"{uid}-s0", "Kitty")
    result = store.label_sample(f"{uid}-s1", "Pancake")
    assert result is not None
    return result


def test_a_meal_two_named_cats_shared_asks_who_ate_when_the_feeder_cannot_say(store: _SyncStore) -> None:
    result = _answer_a_meal_two_cats_shared(store)

    assert result["visit"]["needs_ate"] is True
    assert _cats_by_ate(result["visit"]) == {"Kitty": True, "Pancake": True}, "until someone says, both are credited"
    detail = store.event_detail("e1")
    assert detail is not None and detail["event"]["needs_ate"] is True


def test_the_question_is_only_asked_once_two_cats_are_named(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000, kind="eat")

    first = store.label_sample("e1-s0", "Kitty")

    assert first is not None and first["visit"]["needs_ate"] is False


def test_a_meal_does_not_ask_who_ate_when_the_feeders_own_data_says(store: _SyncStore) -> None:
    subjects = [
        {"sid": 1, "first": 3000, "last": 3020, "eat_start": 3004},
        {"sid": 2, "first": 3004, "last": 3024, "eat_start": None},
    ]
    _seed_visit(store, "e1", 3000, kind="eat", sids=[1, 2, 1], subjects=subjects)

    store.label_sample("e1-s0", "Kitty")
    result = store.label_sample("e1-s1", "Pancake")

    assert result is not None
    assert _cats_by_ate(result["visit"]) == {"Kitty": True, "Pancake": False}
    assert result["visit"]["needs_ate"] is False
    detail = store.event_detail("e1")
    assert detail is not None and detail["event"]["needs_ate"] is False


def test_a_meal_asks_who_ate_when_the_feeders_data_says_neither_did(store: _SyncStore) -> None:
    subjects = [
        {"sid": 1, "first": 3000, "last": 3020, "eat_start": None},
        {"sid": 2, "first": 3004, "last": 3024, "eat_start": None},
    ]
    _seed_visit(store, "e1", 3000, kind="eat", sids=[1, 2, 1], subjects=subjects)

    store.label_sample("e1-s0", "Kitty")
    result = store.label_sample("e1-s1", "Pancake")

    assert result is not None and result["visit"]["needs_ate"] is True


def test_a_meal_does_not_ask_who_ate_when_the_feeder_says_both_did(store: _SyncStore) -> None:
    subjects = [
        {"sid": 1, "first": 3000, "last": 3020, "eat_start": 3004},
        {"sid": 2, "first": 3004, "last": 3024, "eat_start": 3010},
    ]
    _seed_visit(store, "e1", 3000, kind="eat", sids=[1, 2, 1], subjects=subjects)

    store.label_sample("e1-s0", "Kitty")
    result = store.label_sample("e1-s1", "Pancake")

    assert result is not None
    assert _cats_by_ate(result["visit"]) == {"Kitty": True, "Pancake": True}
    assert result["visit"]["needs_ate"] is False


def test_nothing_to_ask_about_a_visit_with_no_meal(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000, kind="visit")

    store.label_sample("e1-s0", "Kitty")
    result = store.label_sample("e1-s1", "Pancake")

    assert result is not None
    assert result["visit"]["needs_ate"] is False
    assert _cats_by_ate(result["visit"]) == {"Kitty": False, "Pancake": False}


def test_nothing_to_ask_about_a_visit_a_person_labelled_as_a_whole(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000, kind="eat")
    store.label_events(["e1"], "Kitty")

    result = store.label_sample("e1-s1", "Pancake")

    assert result is not None and result["visit"]["needs_ate"] is False
    assert [event["needs_ate"] for event in store.label_events(["e1"], "Kitty")] == [False]


def test_set_session_ate_stores_the_answer_and_stops_the_question(store: _SyncStore) -> None:
    _answer_a_meal_two_cats_shared(store)
    training = _training(store)
    generation = _generation(store)

    result = store.set_session_ate("e1", ["Kitty"])

    assert result is not None
    assert _cats_by_ate(result["visit"]) == {"Kitty": True, "Pancake": False}
    assert result["visit"]["needs_ate"] is False
    detail = store.event_detail("e1")
    assert detail is not None and detail["event"]["needs_ate"] is False
    assert _event(store, "e1")["ate_confirmed"] == 1
    assert _event(store, "e1-cat-pancake")["kind"] == "visit", "the cat that only visited has a visit, not a meal"
    assert _training(store) == training and _generation(store) == generation, "who ate teaches nothing"


def test_set_session_ate_is_idempotent(store: _SyncStore) -> None:
    _answer_a_meal_two_cats_shared(store)
    first = store.set_session_ate("e1", ["Kitty"])
    store.conn.execute("UPDATE events SET updated=1")
    store.conn.commit()
    after_first = _state(store)

    second = store.set_session_ate("e1", ["Kitty"])

    assert second == first
    assert _state(store) == after_first


def test_the_who_ate_answer_survives_a_later_photo_answer(store: _SyncStore) -> None:
    _answer_a_meal_two_cats_shared(store)
    store.set_session_ate("e1", ["Pancake"])

    result = store.label_sample("e1-s2", "Kitty")

    assert result is not None
    assert _cats_by_ate(result["visit"]) == {"Kitty": False, "Pancake": True}
    assert result["visit"]["needs_ate"] is False


def test_who_ate_can_name_both_cats(store: _SyncStore) -> None:
    _answer_a_meal_two_cats_shared(store)

    result = store.set_session_ate("e1", ["Kitty", "Pancake"])

    assert result is not None
    assert _cats_by_ate(result["visit"]) == {"Kitty": True, "Pancake": True}
    assert result["visit"]["needs_ate"] is False


def test_set_session_ate_refuses_what_makes_no_sense_and_changes_nothing(store: _SyncStore) -> None:
    _answer_a_meal_two_cats_shared(store)
    before = _state(store)

    with pytest.raises(ValueError, match="at least one cat must have eaten"):
        store.set_session_ate("e1", [])
    with pytest.raises(ValueError, match="duplicate cat"):
        store.set_session_ate("e1", ["Kitty", "Kitty"])
    with pytest.raises(ValueError, match="not in this visit: Waffle"):
        store.set_session_ate("e1", ["Waffle"])

    assert _state(store) == before
    assert store.set_session_ate("nope", ["Kitty"]) is None


def test_set_session_ate_on_a_one_cat_meal_confirms_the_meal(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000, kind="eat")
    store.label_sample("e1-s0", "Kitty")

    result = store.set_session_ate("e1", ["Kitty"])

    assert result is not None and result["visit"]["cats"] == [{"cat": "Kitty", "ate": True}]
    row = _event(store, "e1")
    assert (row["kind"], row["ate_confirmed"], row["from_photos"]) == ("eat", 1, 1)


def test_undoing_every_answer_also_withdraws_the_who_ate_answer(store: _SyncStore) -> None:
    _answer_a_meal_two_cats_shared(store)
    store.set_session_ate("e1", ["Kitty"])

    for sample_uid in ("e1-s0", "e1-s1"):
        store.label_sample(sample_uid, "follow")

    row = _event(store, "e1")
    assert (row["cat"], row["identity_status"], row["reviewed"], row["from_photos"], row["ate_confirmed"]) == (
        None, "unknown", 0, 0, 0
    )
    assert _children(store, "e1") == []


# --- kibble/review/photos ----------------------------------------------------------------------


def _page(store: _SyncStore, *, limit: int = 3, cursor: str | None = None) -> dict[str, Any]:
    return store.review_photos_page(limit=limit, cursor=cursor, retention_cutoff=0)


def _seed_five_visits(store: _SyncStore) -> None:
    for index in range(1, 6):
        _seed_visit(store, f"v{index}", index * 1000)


def _page_visits(page: dict[str, Any]) -> list[str]:
    visits: list[str] = []
    for item in page["items"]:
        if item["visit"]["uid"] not in visits:
            visits.append(item["visit"]["uid"])
    return visits


def test_a_store_with_no_photos_has_an_empty_queue(store: _SyncStore) -> None:
    assert _page(store) == {"items": [], "remaining": 0, "visits": 0, "cursor": None, "has_more": False}


def test_a_page_carries_each_photo_its_visit_and_the_queues_totals(store: _SyncStore) -> None:
    box = (0.0, 0.2, 0.3, 0.8)  # a box on the left edge: its legacy-era crop is trusted, so it is queued
    _seed_visit(store, "e1", 1000, kind="eat", box=box, scene="scene.jpg", scene_k=1000)

    page = _page(store, limit=24)

    assert set(page) == {"items", "remaining", "visits", "cursor", "has_more"}
    assert (page["remaining"], page["visits"], page["cursor"], page["has_more"]) == (3, 1, None, False)
    assert {item["sample"]["uid"] for item in page["items"]} == {"e1-s0", "e1-s1", "e1-s2"}
    for item in page["items"]:
        assert set(item) == {"sample", "visit"}
        assert set(item["sample"]) == {
            "uid", "cat", "t", "body", "face", "guess", "guess_confidence", "review", "sid", "box",
            "crop", "peers", "label",
        }
        assert set(item["visit"]) == {"uid", "start", "kind", "scene", "in_scene"}
        assert (item["visit"]["uid"], item["visit"]["start"], item["visit"]["kind"]) == ("e1", 1000, "eat")
        assert item["visit"]["scene"]["id"] == "scene.jpg"
        assert item["sample"]["box"] == box
        assert item["sample"]["review"] is None and item["sample"]["label"] is None
    # only the first photo was taken on the very frame the scene picture shows
    assert {item["sample"]["uid"]: item["visit"]["in_scene"] for item in page["items"]} == {
        "e1-s0": True, "e1-s1": False, "e1-s2": False,
    }


def test_the_newest_visit_comes_first_and_a_page_never_cuts_a_visit_in_two(store: _SyncStore) -> None:
    _seed_five_visits(store)

    page = _page(store, limit=1)

    assert _page_visits(page) == ["v5"]
    assert len(page["items"]) == 3, "a page asks for at least `limit` photos, rounded up to whole visits"
    assert (page["remaining"], page["visits"], page["has_more"]) == (15, 5, True)
    assert page["cursor"] is not None


def test_the_totals_cover_the_whole_queue_whatever_page_they_arrive_on(store: _SyncStore) -> None:
    _seed_five_visits(store)

    first = _page(store)
    second = _page(store, cursor=first["cursor"])

    assert (first["remaining"], first["visits"]) == (second["remaining"], second["visits"]) == (15, 5)
    assert _page_visits(first) == ["v5"] and _page_visits(second) == ["v4"]


def test_an_empty_cursor_starts_at_the_top(store: _SyncStore) -> None:
    _seed_five_visits(store)

    assert _page(store, cursor="") == _page(store, cursor=None)


def test_a_visit_older_than_the_retention_cutoff_is_not_queued(store: _SyncStore) -> None:
    _seed_five_visits(store)

    page = store.review_photos_page(limit=24, cursor=None, retention_cutoff=3000)

    assert _page_visits(page) == ["v5", "v4", "v3"], "a visit that starts at the cutoff is inside retention"
    assert (page["remaining"], page["visits"]) == (9, 3)


def test_every_answered_photo_leaves_the_queue_and_the_remaining_count_shrinks(store: _SyncStore) -> None:
    _seed_visit(store, "v1", 1000)
    _seed_visit(store, "v2", 2000)
    assert _page(store, limit=24)["remaining"] == 6

    expected = 6
    for sample_uid, label in (("v2-s0", "Kitty"), ("v2-s1", "skip"), ("v1-s0", "not_a_cat")):
        store.label_sample(sample_uid, label)
        expected -= 1
        page = _page(store, limit=24)
        assert page["remaining"] == expected
        assert sample_uid not in {item["sample"]["uid"] for item in page["items"]}

    store.label_sample("v2-s1", "follow")  # answering back puts the photo in the queue again
    page = _page(store, limit=24)
    assert page["remaining"] == expected + 1
    assert "v2-s1" in {item["sample"]["uid"] for item in page["items"]}


def test_a_cursor_stays_valid_while_photos_are_answered_between_two_page_requests(store: _SyncStore) -> None:
    _seed_five_visits(store)
    first = _page(store)
    assert _page_visits(first) == ["v5"] and first["has_more"] is True

    # while the card shows page one, all of its photos get answered -- and one photo further down
    answered = {item["sample"]["uid"] for item in first["items"]} | {"v3-s0"}
    for sample_uid in sorted(answered):
        store.label_sample(sample_uid, "Kitty" if sample_uid != "v3-s0" else "Pancake")

    pages = []
    cursor = first["cursor"]
    while cursor is not None:
        page = _page(store, cursor=cursor)
        pages.append(page)
        cursor = page["cursor"]

    seen = [item["sample"]["uid"] for page in pages for item in page["items"]]
    assert len(seen) == len(set(seen)) == 11, "no photo twice, none skipped: 15 less the 4 answered"
    assert not answered & set(seen), "answered photos are gone"
    assert [uid for page in pages for uid in _page_visits(page)] == ["v4", "v3", "v2", "v1"]
    assert {(page["remaining"], page["visits"]) for page in pages} == {(11, 4)}
    assert pages[-1]["has_more"] is False and pages[-1]["cursor"] is None


def test_a_visit_with_nothing_left_to_answer_drops_out_of_the_queue(store: _SyncStore) -> None:
    _seed_five_visits(store)
    for index in range(3):
        store.label_sample(f"v5-s{index}", "skip")

    page = _page(store, limit=24)

    assert page["visits"] == 4 and page["remaining"] == 12
    assert "v5" not in _page_visits(page)


def test_a_visit_labelled_as_a_whole_leaves_the_photo_queue(store: _SyncStore) -> None:
    _seed_five_visits(store)

    store.label_events(["v4"], "Kitty")
    store.label_events(["v2"], "unknown")

    page = _page(store, limit=24)
    assert _page_visits(page) == ["v5", "v3", "v1"]
    assert (page["remaining"], page["visits"]) == (9, 3)


def _forged_cursor(payload: object) -> str:
    """Well-formed base64 around `payload`: something the queue never issued."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "not a cursor",
        _forged_cursor([]),
        _forged_cursor([1, 2, 3]),
        _forged_cursor({"era": 1}),
        _forged_cursor(["1", 0, 1000, "v1"]),
        _forged_cursor([1, 0, True, "v1"]),
        _forged_cursor([1, 0, 1000, 7]),
    ],
    ids=[
        "not base64 json", "empty list", "three items", "an object", "text for a number",
        "a bool for a number", "a number for the visit",
    ],
)
def test_an_invalid_cursor_is_refused(store: _SyncStore, cursor: str) -> None:
    _seed_five_visits(store)

    with pytest.raises(ValueError, match="invalid review cursor"):
        _page(store, cursor=cursor)


# --- kibble/cats -------------------------------------------------------------------------------


def test_cats_overview_is_what_the_per_cat_helpers_say_for_every_cat_in_one_call(store: _SyncStore) -> None:
    store.add_cat("Waffle")
    _seed_visit(store, "e1", 1000)
    store.label_events(["e1"], "Kitty")
    assert store.set_cat_avatar("Pancake", b"jpeg") is not None
    store.conn.execute("INSERT INTO auto_learn_state(cat, paused, updated) VALUES ('Waffle', 1, 1)")
    store.conn.commit()

    overview = store.cats_overview(14)

    expected_cats = []
    for cat in store.cats():
        info = store.avatar_info(cat["name"])
        expected_cats.append({
            "name": cat["name"],
            "color": cat["color"],
            "avatar": info["avatar"],
            "avatar_custom": info["custom"],
            "learning_paused": store.auto_learn_paused(cat["name"]),
            "training": store.training_counts(cat["name"]),
        })
    assert overview == {"cats": expected_cats, "storage": store.storage_summary(14)}
    assert set(overview) == {"cats", "storage"}
    by_name = {cat["name"]: cat for cat in overview["cats"]}
    assert list(by_name) == ["Kitty", "Pancake", "Waffle"]
    assert by_name["Kitty"]["training"] == {"total": 3, "face": 0, "body": 3}
    assert by_name["Kitty"]["avatar_custom"] is False and by_name["Kitty"]["avatar"] is not None
    assert by_name["Pancake"]["avatar_custom"] is True and by_name["Pancake"]["avatar"] is not None
    assert by_name["Waffle"]["learning_paused"] is True and by_name["Waffle"]["avatar"] is None
    assert by_name["Kitty"]["learning_paused"] is False
    assert overview["storage"]["retention_days"] == 14


def test_cats_overview_of_a_household_with_no_cats_still_reports_storage(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")

    overview = store.cats_overview(30)

    assert overview["cats"] == []
    assert overview["storage"] == store.storage_summary(30)
    store.close()


# --- the recheck sweep -------------------------------------------------------------------------


def test_the_recheck_sweep_lists_unreviewed_top_level_events_inside_retention(store: _SyncStore) -> None:
    _seed_visit(store, "old", 50)
    _seed_visit(store, "plain", 200)
    _seed_visit(store, "answered", 300)
    store.label_events(["answered"], "Kitty")
    _seed_two_cat_session(store, "split", start=400)
    assert store.replan_session("split", _subject_scores()) is True
    assert _children(store, "split") == ["split-cat-kitty", "split-cat-pancake"]

    assert store.events_for_reclassify(0) == ["old", "plain", "split"]
    assert store.events_for_reclassify(100) == ["plain", "split"]
    assert store.events_for_reclassify(200) == ["plain", "split"], "a visit that starts at the cutoff is inside"
    assert store.events_for_reclassify(1000) == []


def test_a_recheck_of_a_split_session_from_its_parent_leaves_the_split_alone(store: _SyncStore) -> None:
    _seed_two_cat_session(store)
    assert store.replan_session("e1", _subject_scores()) is True
    family = [(row["uid"], row["rowid"], row["hidden"]) for row in store.conn.execute("SELECT uid, rowid, hidden FROM events ORDER BY rowid")]

    for _ in range(2):  # what the sweep does, event by event, on every pass
        for uid in store.events_for_reclassify(0):
            assert store.replan_session(uid, _subject_scores()) is False

    assert [(row["uid"], row["rowid"], row["hidden"]) for row in store.conn.execute("SELECT uid, rowid, hidden FROM events ORDER BY rowid")] == family
    assert _children(store, "e1") == ["e1-cat-kitty", "e1-cat-pancake"]


def test_set_event_classification_reports_only_a_changed_name(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    guesses = dict(per_sample_uids=["e1-s0", "e1-s1", "e1-s2"], per_sample=[("Kitty", 0.9)] * 3)

    assert store.set_event_classification("e1", cat="Kitty", identity_status="auto", confidence=0.9, **guesses) is True
    assert store.set_event_classification("e1", cat="Kitty", identity_status="auto", confidence=0.95, **guesses) is False
    assert store.set_event_classification("e1", cat="Pancake", identity_status="auto", confidence=0.95, **guesses) is True
    assert store.set_event_classification("e1", cat=None, identity_status="unknown", confidence=None, **guesses) is True
    assert store.set_event_classification("nope", cat="Kitty", identity_status="auto", confidence=0.9, **guesses) is False


def test_an_unchanged_verdict_costs_no_write(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    guesses = dict(per_sample_uids=["e1-s0", "e1-s1", "e1-s2"], per_sample=[("Kitty", 0.9)] * 3)
    store.set_event_classification("e1", cat="Kitty", identity_status="auto", confidence=0.9, **guesses)
    store.conn.execute("UPDATE events SET updated=1")
    store.conn.commit()
    before = _state(store)
    commits = 0
    real_conn = store.conn

    class CountingConnection:
        def commit(self) -> None:
            nonlocal commits
            commits += 1
            real_conn.commit()

        def __getattr__(self, name: str) -> Any:
            return getattr(real_conn, name)

    store.conn = CountingConnection()  # type: ignore[assignment]
    try:
        assert store.set_event_classification("e1", cat="Kitty", identity_status="auto", confidence=0.9, **guesses) is False
    finally:
        store.conn = real_conn

    assert commits == 0
    assert _state(store) == before


def test_a_reviewed_event_keeps_its_answer_whatever_the_engine_now_thinks(store: _SyncStore) -> None:
    _seed_visit(store, "e1", 1000)
    store.label_events(["e1"], "Kitty")
    store.conn.execute("UPDATE events SET updated=1")
    store.conn.commit()

    renamed = store.set_event_classification(
        "e1", cat="Pancake", identity_status="auto", confidence=0.99,
        per_sample_uids=["e1-s0"], per_sample=[("Pancake", 0.99)],
    )

    assert renamed is False
    assert _verdict(store, "e1")[:3] == ("Kitty", "reviewed", 1)
    assert _event(store, "e1")["updated"] == 1
    guess = store.conn.execute("SELECT guess, guess_confidence FROM samples WHERE uid='e1-s0'").fetchone()
    assert (guess["guess"], guess["guess_confidence"]) == ("Pancake", 0.99), "what the engine thinks is still recorded"


# --- Coral embeddings --------------------------------------------------------------------------


def test_a_second_write_of_an_embedding_is_dropped_not_overwritten(store: _SyncStore) -> None:
    store.set_coral_embeddings("training", [("t1", "body", _MODEL, b"\x01"), ("t1", "face", _MODEL, b"\x02")])

    store.set_coral_embeddings("training", [("t1", "body", _MODEL, b"\xff"), ("t2", "body", _MODEL, b"\x03")])

    assert store.coral_embedding("training", "t1", "body", _MODEL) == b"\x01", "the first vector stays"
    assert store.coral_embedding("training", "t1", "face", _MODEL) == b"\x02"
    assert store.coral_embedding("training", "t2", "body", _MODEL) == b"\x03", "new keys in the same batch still land"
    assert store.coral_embedding("sample", "t1", "body", _MODEL) is None, "a training row and a sample row are different keys"
    assert store.coral_embedding("training", "t1", "body", "other-model") is None


def test_a_batch_of_embeddings_is_one_transaction(store: _SyncStore) -> None:
    commits = 0
    real_conn = store.conn

    class CountingConnection:
        def commit(self) -> None:
            nonlocal commits
            commits += 1
            real_conn.commit()

        def __getattr__(self, name: str) -> Any:
            return getattr(real_conn, name)

    store.conn = CountingConnection()  # type: ignore[assignment]
    try:
        store.set_coral_embeddings("sample", [(f"s{index}", "body", _MODEL, b"\x01") for index in range(50)])
    finally:
        store.conn = real_conn

    assert commits == 1
    assert len(_cached(store, "sample")) == 50
