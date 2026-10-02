"""`store.py`'s `_SyncStore`: the actual persistence/query logic every async facade method and
`scripts/migrate_v2.py` both delegate to, exercised directly (no Home Assistant, no event loop --
`store.py` has zero `homeassistant` import at module scope, so this file runs under either
Python this project has).

Covers, each pinned on the observable contract docs/36-ai-pipeline.md and store.py's own module
docstring describe, never on plumbing:

- Ingest idempotency: re-inserting the same sample `uid` is a no-op, not a silent overwrite.
- Retention purge deletes aged events/media but never a sample already copied into training.
- Cursor pagination is stable across equal-timestamp ties and immune to a concurrent insert
  landing between two page fetches.
- A `not_a_cat` verdict and a `visit` with no usable thumb are both hidden from
  `kibble/timeline` but remain reachable through `kibble/event`/`kibble/review`.
- `label_events` (fast: one DB write) and `reconcile_event_training` (slow: real file I/O) are
  a distinct, later step, not two names for the same work.
- A per-photo `kibble/sample/label` override survives an event relabel; a skip removes any
  training row it had; a schema migration adds `samples.review` to an existing database
  without losing rows.
- `pick_thumb`/`thumb_asset_id`'s face-only fallback: a sample with no body crop must still be
  selected as the thumb, and resolve to its face asset id.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import numpy as np

import pytest
from kibble import identity, sessions
from kibble.store import (
    SCHEMA_VERSION,
    TRAINING_CAP_PER_CAT,
    TRAINING_HARD_CAP_PER_CAT,
    ThumbCandidate,
    TrainingCandidate,
    _select_eviction_candidates,
    _SyncStore,
    _utc_date,
    pick_thumb,
    thumb_asset_id,
)


def _features() -> identity.Features:
    return identity.Features(face_emb=None, body_feat=None, face_feat=None, mode=None)


def _insert_event(store: _SyncStore, uid: str, start: int, *, kind: str = "visit", **overrides) -> None:
    kwargs: dict = dict(
        uid=uid,
        device_event_id=1,
        kind=kind,
        start=start,
        end=start + 10,
        open_=False,
        eat_start=None,
        scene=None,
        before=None,
        after=None,
    )
    kwargs.update(overrides)
    store.upsert_event(**kwargs)


def _insert_sample(store: _SyncStore, uid: str, event_uid: str, t: int, *, body: str | None = "b.jpg") -> None:
    store.insert_sample(
        uid=uid, event_uid=event_uid, t=t, body=body, face=None, features=_features(), box=None, score=0.9
    )


# --- ingest idempotency --------------------------------------------------------------------


def test_reinserting_the_same_sample_uid_is_a_noop(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100)
    _insert_sample(store, "e1-s1", "e1", 100, body="first.jpg")

    # A crash-recovery replay of the same poll would try to insert this exact sample uid again,
    # with whatever the device answers this time -- possibly different bytes/box/score.
    store.insert_sample(
        uid="e1-s1", event_uid="e1", t=999, body="second.jpg", face="f.jpg",
        features=_features(), box=(0.1, 0.1, 0.9, 0.9), score=0.1,
    )

    rows = store.conn.execute("SELECT * FROM samples WHERE uid='e1-s1'").fetchall()
    assert len(rows) == 1
    # INSERT OR IGNORE: the first insert's values stand -- a true no-op, not a silent overwrite.
    assert rows[0]["body"] == "first.jpg" and rows[0]["t"] == 100


# --- retention -------------------------------------------------------------------------------


def test_retention_purge_keeps_training_rows_and_files_while_deleting_aged_events_and_media(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    old_ts = int(time.time()) - 30 * 86400
    date = _utc_date(old_ts)
    body_asset = store.write_media(date, "e1-s1-body.jpg", b"body-bytes")
    _insert_event(store, "e1", old_ts)
    _insert_sample(store, "e1-s1", "e1", old_ts, body=body_asset)
    store.reconcile_event_training(["e1"], "Kitty")
    training_body = store.conn.execute("SELECT body FROM training WHERE uid='e1-s1-train'").fetchone()["body"]
    training_path = store.root / training_body
    assert training_path.exists()  # sanity: the copy really landed before purging

    store.purge(retention_days=14)

    assert store.conn.execute("SELECT 1 FROM events WHERE uid='e1'").fetchone() is None
    assert store.conn.execute("SELECT 1 FROM samples WHERE event_uid='e1'").fetchone() is None
    assert not (store.media_root / body_asset).exists()
    assert store.conn.execute("SELECT 1 FROM training WHERE uid='e1-s1-train'").fetchone() is not None
    assert training_path.exists()


# --- cursor pagination -------------------------------------------------------------------------


def test_cursor_pagination_is_stable_across_equal_timestamps(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    for uid in ("e1", "e2", "e3"):  # a burst of visits captured in the exact same second
        _insert_event(store, uid, 1000)
        _insert_sample(store, f"{uid}-s1", uid, 1000, body=f"{uid}.jpg")

    page1 = store.timeline_page(limit=2, cursor=None)
    assert len(page1["items"]) == 2
    assert page1["has_more"] is True

    page2 = store.timeline_page(limit=2, cursor=page1["cursor"])
    seen = [item["uid"] for item in page1["items"]] + [item["uid"] for item in page2["items"]]
    assert sorted(seen) == ["e1", "e2", "e3"]
    assert len(set(seen)) == 3  # no duplicate and no gap despite the timestamp tie


def test_cursor_pagination_is_immune_to_a_concurrent_insert_between_pages(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    for uid, ts in (("e1", 300), ("e2", 200), ("e3", 100)):
        _insert_event(store, uid, ts)
        _insert_sample(store, f"{uid}-s1", uid, ts, body=f"{uid}.jpg")

    page1 = store.timeline_page(limit=1, cursor=None)
    assert [i["uid"] for i in page1["items"]] == ["e1"]

    # A new event newer than everything already paged lands between the two fetches -- the
    # cursor already fixed the boundary at e1's own (start, uid), so it must not shift page 2.
    _insert_event(store, "e-new", 500)
    _insert_sample(store, "e-new-s1", "e-new", 500, body="new.jpg")

    page2 = store.timeline_page(limit=1, cursor=page1["cursor"])
    assert [i["uid"] for i in page2["items"]] == ["e2"]


# --- hidden from timeline, still reachable via event_detail/review ------------------------------


def test_not_a_cat_verdict_is_hidden_from_timeline_but_reachable_via_event_detail(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100)
    _insert_sample(store, "e1-s1", "e1", 100)
    store.set_event_classification(
        "e1", cat=None, identity_status="not_a_cat", confidence=0.9,
        per_sample_uids=["e1-s1"], per_sample=[("not_a_cat", 0.9)],
    )

    assert store.timeline_page(limit=10, cursor=None)["items"] == []

    detail = store.event_detail("e1")
    assert detail is not None
    assert detail["event"]["identity"] == "not_a_cat"


def test_visit_with_no_usable_thumb_is_hidden_from_timeline_but_reachable_via_event_detail_and_review(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100)  # no samples at all -- nothing could ever be a thumb
    store.set_event_classification(
        "e1", cat=None, identity_status="unknown", confidence=None, per_sample_uids=[], per_sample=[]
    )

    assert store.timeline_page(limit=10, cursor=None)["items"] == []

    detail = store.event_detail("e1")
    assert detail is not None
    assert detail["event"]["uid"] == "e1" and detail["event"]["thumb"] is None

    review = store.review_page(limit=10, cursor=None, retention_cutoff=0)
    assert [i["uid"] for i in review["items"]] == ["e1"]


# --- label_events (fast, no I/O) vs copy_samples_to_training (slow, real file copy) -------------


def test_label_events_updates_reviewed_state_immediately_with_zero_file_io(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    _insert_event(store, "e1", 100)
    body_asset = store.write_media(_utc_date(100), "e1-s1-body.jpg", b"body-bytes")
    _insert_sample(store, "e1-s1", "e1", 100, body=body_asset)

    events, training_changed = store.label_events(["e1"], "Kitty")

    assert training_changed is True
    assert events[0]["cat"] == "Kitty" and events[0]["identity"] == "reviewed"
    row = store.conn.execute(
        "SELECT cat, identity_status, confidence, reviewed FROM events WHERE uid='e1'"
    ).fetchone()
    assert (row["cat"], row["identity_status"], row["confidence"], row["reviewed"]) == ("Kitty", "reviewed", None, 1)
    # No training row and no file exists yet -- copying samples is a distinct, later step.
    assert store.conn.execute("SELECT 1 FROM training").fetchone() is None
    assert not any(store.training_root.rglob("*"))


def test_label_events_of_unknown_label_never_reports_training_changed(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100)
    _insert_sample(store, "e1-s1", "e1", 100)

    events, training_changed = store.label_events(["e1"], "unknown")

    assert training_changed is False
    assert events[0]["identity"] == "unknown" and events[0]["cat"] is None


def test_label_events_of_not_a_cat_also_reports_training_changed(tmp_path: Path) -> None:
    """`not_a_cat` is itself a trainable class (`identity.py`: "every enrolled cat plus
    not_a_cat") -- docs/36-ai-pipeline.md's `kibble/label` contract adds its chosen samples to
    training for a cat OR `not_a_cat` label, never for `unknown` only."""
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100)
    _insert_sample(store, "e1-s1", "e1", 100)

    events, training_changed = store.label_events(["e1"], "not_a_cat")

    assert training_changed is True
    assert events[0]["identity"] == "not_a_cat" and events[0]["cat"] is None


def test_reconcile_event_training_is_the_distinct_later_step_that_actually_copies_files(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    _insert_event(store, "e1", 100)
    body_asset = store.write_media(_utc_date(100), "e1-s1-body.jpg", b"body-bytes")
    _insert_sample(store, "e1-s1", "e1", 100, body=body_asset)
    store.label_events(["e1"], "Kitty")

    store.reconcile_event_training(["e1"], "Kitty")

    row = store.conn.execute("SELECT cat, body FROM training WHERE uid='e1-s1-train'").fetchone()
    assert row["cat"] == "Kitty"
    training_path = store.root / row["body"]
    assert training_path.exists()
    assert training_path.read_bytes() == b"body-bytes"


# --- kibble/sample/label: per-photo override, independent of the event's own label -------------


def test_label_sample_persists_a_per_photo_override_across_reads(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100)
    _insert_sample(store, "e1-s1", "e1", 100)
    store.add_cat("Kitty")
    store.add_cat("Pancake")
    store.label_events(["e1"], "Kitty")  # event reviewed as Kitty

    saved = store.label_sample("e1-s1", "Pancake")

    assert saved is not None
    assert (saved["review"], saved["label"]) == ("Pancake", "Pancake")
    detail = store.event_detail("e1")
    sample = detail["samples"][0]
    assert (sample["review"], sample["label"]) == ("Pancake", "Pancake")
    row = store.conn.execute("SELECT cat FROM training WHERE uid='e1-s1-train'").fetchone()
    assert row["cat"] == "Pancake"


def test_label_sample_skip_removes_an_existing_training_row_and_its_files(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100)
    body_asset = store.write_media(_utc_date(100), "e1-s1-body.jpg", b"body-bytes")
    _insert_sample(store, "e1-s1", "e1", 100, body=body_asset)
    store.add_cat("Kitty")
    store.label_events(["e1"], "Kitty")
    store.reconcile_event_training(["e1"], "Kitty")
    training_path = store.root / store.conn.execute(
        "SELECT body FROM training WHERE uid='e1-s1-train'"
    ).fetchone()["body"]
    assert training_path.exists()  # sanity: it really was trained first

    saved = store.label_sample("e1-s1", "skip")

    assert (saved["review"], saved["label"]) == ("skip", "skip")
    assert store.conn.execute("SELECT 1 FROM training WHERE uid='e1-s1-train'").fetchone() is None
    assert not training_path.exists()


def test_label_sample_rejects_an_unknown_cat_name(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100)
    _insert_sample(store, "e1-s1", "e1", 100)

    with pytest.raises(ValueError):
        store.label_sample("e1-s1", "NotEnrolled")


def test_label_sample_returns_none_for_an_unknown_sample_uid(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    assert store.label_sample("missing", "skip") is None


def test_label_sample_follow_trains_as_the_events_own_reviewed_label(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100)
    _insert_sample(store, "e1-s1", "e1", 100)
    store.add_cat("Kitty")
    store.label_events(["e1"], "Kitty")
    store.label_sample("e1-s1", "not_a_cat")  # override away from Kitty first

    saved = store.label_sample("e1-s1", "follow")

    assert (saved["review"], saved["label"]) == (None, "Kitty")
    row = store.conn.execute("SELECT cat FROM training WHERE uid='e1-s1-train'").fetchone()
    assert row["cat"] == "Kitty"


def test_follow_clears_a_photo_override_but_keeps_its_new_session_cat(tmp_path: Path) -> None:
    """Choosing a new cat for one photo adds that cat to the session; following later clears
    only the photo-specific answer and keeps the session answer the person just established."""
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100)
    _insert_sample(store, "e1-s1", "e1", 100)
    store.add_cat("Kitty")
    store.label_sample("e1-s1", "Kitty")

    saved = store.label_sample("e1-s1", "follow")

    assert (saved["review"], saved["label"]) == (None, "Kitty")
    event = store.conn.execute("SELECT cat, reviewed FROM events WHERE uid='e1'").fetchone()
    assert (event["cat"], event["reviewed"]) == ("Kitty", 1)
    row = store.conn.execute("SELECT cat FROM training WHERE uid='e1-s1-train'").fetchone()
    assert row["cat"] == "Kitty"


def test_reconcile_event_training_leaves_an_override_alone_and_removes_a_skip(tmp_path: Path) -> None:
    """Relabelling the whole event must never move a per-photo override, and a skipped photo
    must stay untrained even though the rest of the event just got a fresh cat name."""
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100)
    _insert_sample(store, "e1-s1", "e1", 100)  # will follow
    _insert_sample(store, "e1-s2", "e1", 101)  # will be overridden
    _insert_sample(store, "e1-s3", "e1", 102)  # will be skipped
    store.add_cat("Kitty")
    store.add_cat("Pancake")
    store.label_sample("e1-s2", "Pancake")
    store.label_sample("e1-s3", "skip")

    store.label_events(["e1"], "Kitty")
    store.reconcile_event_training(["e1"], "Kitty")

    cats = {
        r["uid"]: r["cat"]
        for r in store.conn.execute(
            "SELECT uid, cat FROM training WHERE uid IN ('e1-s1-train', 'e1-s2-train', 'e1-s3-train')"
        )
    }
    assert cats == {"e1-s1-train": "Kitty", "e1-s2-train": "Pancake"}  # s3 never lands in training
    detail = store.event_detail("e1")
    labels = {s["uid"]: s["label"] for s in detail["samples"]}
    assert labels == {"e1-s1": "Kitty", "e1-s2": "Pancake", "e1-s3": "skip"}


# --- pick_thumb / thumb_asset_id: face-only fallback regression --------------------------------


def test_pick_thumb_selects_a_face_only_sample_when_it_is_the_only_candidate() -> None:
    """Regression: a face-only sample (no body crop -- true of every face-only migrated import
    row, or a live sample whose body write the spool guard refused) used to be excluded from
    thumbnail selection entirely no matter what. It must now win by default, and
    `thumb_asset_id` must resolve to its face id since `.body` alone is never enough."""
    candidate = ThumbCandidate(
        uid="s1", t=100, body=None, has_face=True, score=0.9, box=None, guess=None, face="e1-s1-face.jpg"
    )

    chosen = pick_thumb([candidate])

    assert chosen is candidate
    assert thumb_asset_id(chosen) == "e1-s1-face.jpg"


def test_pick_thumb_prefers_a_body_only_candidate_over_a_worse_scored_face_only_one_absent() -> None:
    """Sanity companion to the regression above: with an actual face-bearing candidate in the
    pool, faces still win over a bare body crop (pick_thumb's own documented "prefer a face"
    rule) -- the face-only fallback is for when nothing else qualifies, not a general override."""
    body_only = ThumbCandidate(uid="s1", t=100, body="body.jpg", has_face=False, score=0.99, box=None, guess=None)
    face_only = ThumbCandidate(
        uid="s2", t=101, body=None, has_face=True, score=0.1, box=None, guess=None, face="face.jpg"
    )

    chosen = pick_thumb([body_only, face_only])

    assert chosen is face_only
    assert thumb_asset_id(chosen) == "face.jpg"


def test_review_thumb_shows_a_doubtful_crop_that_the_timeline_thumb_rejects():
    from kibble.store import ThumbCandidate, pick_thumb, review_thumb

    doubtful = ThumbCandidate(uid="s1", t=1, body=None, has_face=True, score=0.9, box=None, guess="not_a_cat", face="f1.jpg")
    assert pick_thumb([doubtful]) is None
    assert review_thumb([doubtful]) is doubtful
    assert review_thumb([]) is None


def test_legacy_all_zero_box_is_treated_as_unknown_not_as_a_tiny_box():
    from kibble.api import Sample

    parsed = Sample.from_json({"k": 1, "t": 1, "box": [0, 0, 0, 0], "score": 0.0, "body": None, "face": None})
    assert parsed.box is None


def test_an_open_visit_that_becomes_a_meal_is_stored_as_a_meal(tmp_path: Path) -> None:
    """The device first reports a track as a visit and only reclassifies it as an eat once the
    cat has stayed at the bowl long enough. Regression for 2026-09-24: Pancake's meal stayed a
    visit in HA because the upsert never updated `kind`."""
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="visit", open_=True, end=None)
    _insert_event(store, "e1", 100, kind="eat", open_=False, eat_start=130)
    row = store.conn.execute("SELECT kind, eat_start FROM events WHERE uid='e1'").fetchone()
    assert (row["kind"], row["eat_start"]) == ("eat", 130)


# --- schema migration: `feeds` gains amount1/amount2/food1/food2/single -----------------------


def _legacy_feeds_db(root: Path) -> None:
    """A pre-migration `feeds` table (schema version 1, docs/37-hopper-full.md): the five new
    columns and the meta row naming the version do not exist yet."""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "kibble.db")
    conn.execute(
        """
        CREATE TABLE feeds(
            uid TEXT PRIMARY KEY, device_feed_id TEXT, ts INTEGER NOT NULL, portions REAL,
            hopper INTEGER, scheduled INTEGER, confirmed INTEGER, before TEXT, after TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO feeds(uid, device_feed_id, ts, portions, hopper, scheduled, confirmed) "
        "VALUES ('old-1', 'f1', 100, 5.0, 1, 0, 1)"
    )
    conn.commit()
    conn.close()


def test_migrating_a_pre_existing_database_adds_the_new_feed_columns_without_losing_old_rows(
    tmp_path: Path,
) -> None:
    _legacy_feeds_db(tmp_path)

    store = _SyncStore(tmp_path, "entry1")

    row = store.conn.execute("SELECT * FROM feeds WHERE uid='old-1'").fetchone()
    assert row["portions"] == 5.0  # the pre-existing row survives untouched
    assert (row["amount1"], row["amount2"], row["food1"], row["food2"], row["single"]) == (
        None, None, None, None, None,
    )
    version = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)


def test_migration_is_idempotent_across_repeated_opens_of_the_same_database(tmp_path: Path) -> None:
    store1 = _SyncStore(tmp_path, "entry1")
    store1.upsert_feed(
        uid="f1", device_feed_id="1", ts=100, portions=3.0, scheduled=False, confirmed=True,
        before=None, after=None, amount1=1, amount2=2, food1="Kibble", food2="Fish", single=False,
    )
    store1.conn.close()

    store2 = _SyncStore(tmp_path, "entry1")  # re-opens the same file; `_migrate` reruns
    row = store2.conn.execute("SELECT * FROM feeds WHERE uid='f1'").fetchone()
    assert (row["amount1"], row["amount2"], row["food1"], row["food2"]) == (1, 2, "Kibble", "Fish")
    version = store2.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)


# --- schema migration: `samples` gains `review` ------------------------------------------------


def _legacy_samples_db(root: Path) -> None:
    """A pre-migration `samples` table (schema version 2, before `review`): the new column and
    the meta row naming the version do not exist yet."""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "kibble.db")
    conn.execute(
        """
        CREATE TABLE samples(
            uid TEXT PRIMARY KEY, event_uid TEXT NOT NULL, t INTEGER NOT NULL, body TEXT,
            face TEXT, face_emb BLOB, body_feat BLOB, face_feat BLOB, mode TEXT, guess TEXT,
            guess_confidence REAL, box_x1 REAL, box_y1 REAL, box_x2 REAL, box_y2 REAL, score REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO samples(uid, event_uid, t, body, guess) VALUES ('old-s1', 'old-e1', 100, 'b.jpg', 'Kitty')"
    )
    conn.commit()
    conn.close()


def test_migrating_a_pre_existing_database_adds_the_review_column_without_losing_old_rows(
    tmp_path: Path,
) -> None:
    _legacy_samples_db(tmp_path)

    store = _SyncStore(tmp_path, "entry1")

    row = store.conn.execute("SELECT * FROM samples WHERE uid='old-s1'").fetchone()
    assert (row["guess"], row["body"]) == ("Kitty", "b.jpg")  # the pre-existing row survives untouched
    assert row["review"] is None
    version = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)


# --- upsert_feed: amount/food/mode are recorded once, at first insert only --------------------


def test_upsert_feed_freezes_amounts_food_and_mode_at_first_insert_and_never_overwrites_them(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.upsert_feed(
        uid="f1", device_feed_id="1", ts=100, portions=3.0, scheduled=False, confirmed=False,
        before=None, after=None, amount1=1, amount2=2, food1="Kibble", food2="Freeze-Dried",
        single=False,
    )

    # A later re-upsert of the same uid -- the device confirms it, and the divider/food names
    # have since changed -- must update `confirmed`/`portions`/`before`/`after` but never touch
    # the five frozen, recorded-at-ingest facts.
    store.upsert_feed(
        uid="f1", device_feed_id="1", ts=100, portions=9.0, scheduled=False, confirmed=True,
        before="b.jpg", after="a.jpg", amount1=7, amount2=7, food1="Renamed", food2="Renamed",
        single=True,
    )

    row = store.conn.execute("SELECT * FROM feeds WHERE uid='f1'").fetchone()
    assert (row["confirmed"], row["portions"]) == (1, 9.0)
    assert (row["before"], row["after"]) == ("b.jpg", "a.jpg")
    assert (row["amount1"], row["amount2"]) == (1, 2)
    assert (row["food1"], row["food2"]) == ("Kibble", "Freeze-Dried")
    assert row["single"] == 0


def test_feed_timeline_dict_exposes_the_frozen_facts_for_websocket_py_to_render(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.upsert_feed(
        uid="f1", device_feed_id="1", ts=100, portions=3.0, scheduled=False, confirmed=True,
        before=None, after=None, amount1=1, amount2=2, food1="Kibble", food2=None, single=False,
    )
    row = store.conn.execute("SELECT * FROM feeds WHERE uid='f1'").fetchone()

    assert store._feed_timeline_dict(row)["feed"] == {
        "portions": 3.0, "scheduled": False, "confirmed": True,
        "amount1": 1, "amount2": 2, "food1": "Kibble", "food2": None, "single": False,
    }


# --- avatar: auto-pick fallback, custom override, choose-existing, clear -----------------------


def _trained_photo(store: _SyncStore, cat: str, event_uid: str, ts: int) -> None:
    """The shortest path to one real training row -- an event, a sample, then a `label`."""
    store.conn.execute(
        "INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, scene, "
        "before, after, reviewed, updated) VALUES (?, ?, 'visit', ?, ?, 0, NULL, NULL, NULL, NULL, 0, ?)",
        (event_uid, ts, ts, ts + 10, ts),
    )
    store.insert_sample(
        uid=f"{event_uid}-s1", event_uid=event_uid, t=ts, body="b.jpg", face=None,
        features=_features(), box=None, score=0.9,
    )
    store.label_events([event_uid], cat)
    store.reconcile_event_training([event_uid], cat)


def test_cat_avatar_falls_back_to_the_newest_trained_photo_when_no_custom_one_is_set(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    assert store.cat_avatar("Kitty") is None
    (tmp_path / "media" / "b.jpg").write_bytes(b"body")
    _trained_photo(store, "Kitty", "e1", 100)
    avatar = store.cat_avatar("Kitty")
    assert avatar is not None
    assert store.avatar_info("Kitty") == {"avatar": avatar, "custom": False}


def test_set_cat_avatar_overrides_the_auto_pick_and_survives_eviction_of_the_source_sample(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    saved = store.set_cat_avatar("Kitty", b"custom-photo-bytes")
    assert saved == {"id": "avatars/kitty.jpg", "url": "/api/kibble/entry1/media/avatars/kitty.jpg"}
    assert (tmp_path / "avatars" / "kitty.jpg").read_bytes() == b"custom-photo-bytes"
    info = store.avatar_info("Kitty")
    assert info["custom"] is True and info["avatar"] == saved


def test_set_cat_avatar_is_none_for_an_unknown_cat_and_never_writes_a_file(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    assert store.set_cat_avatar("Nope", b"x") is None
    assert not (tmp_path / "avatars").exists()


def test_set_cat_avatar_from_asset_copies_bytes_and_is_none_for_a_bad_asset_id(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    (tmp_path / "media" / "b.jpg").write_bytes(b"body")
    _trained_photo(store, "Kitty", "e1", 100)
    training_asset_id = store.cat_avatar("Kitty")["id"]
    chosen = store.set_cat_avatar_from_asset("Kitty", training_asset_id)
    assert chosen is not None
    assert store.avatar_info("Kitty")["custom"] is True
    assert store.set_cat_avatar_from_asset("Kitty", "media/does-not-exist.jpg") is None
    assert store.set_cat_avatar_from_asset("Kitty", "../../etc/passwd") is None


def test_clear_cat_avatar_removes_the_file_and_falls_back_to_auto_pick(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    (tmp_path / "media" / "b.jpg").write_bytes(b"body")
    _trained_photo(store, "Kitty", "e1", 100)
    store.set_cat_avatar("Kitty", b"custom")
    assert store.clear_cat_avatar("Kitty") is True
    assert not (tmp_path / "avatars" / "kitty.jpg").exists()
    info = store.avatar_info("Kitty")
    assert info["custom"] is False and info["avatar"] is not None  # auto-pick still there
    assert store.clear_cat_avatar("NeverEnrolled") is False


def test_delete_cat_removes_its_avatar_file(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    store.set_cat_avatar("Kitty", b"custom")
    store.delete_cat("Kitty")
    assert not (tmp_path / "avatars" / "kitty.jpg").exists()


# --- training-set eviction: pure _select_eviction_candidates + a real-store integration --------


def _candidate(uid: str, source: str, confidence: float | None, created: int) -> TrainingCandidate:
    return TrainingCandidate(uid=uid, source=source, confidence=confidence, created=created)


def test_eviction_absorbs_pressure_with_auto_rows_before_touching_any_protected_row() -> None:
    candidates = [_candidate(f"a{i}", "auto", 0.9, i) for i in range(5)] + [
        _candidate(f"p{i}", "label", None, i) for i in range(3)
    ]
    evicted = _select_eviction_candidates(candidates, redundancy={}, soft_cap=5, hard_cap=100)
    assert len(evicted) == 3
    assert all(uid.startswith("a") for uid in evicted)


def test_eviction_ranks_the_most_redundant_auto_row_before_a_merely_low_confidence_one() -> None:
    candidates = [_candidate("redundant", "auto", 0.99, 10), _candidate("lowconf", "auto", 0.1, 20)]
    redundancy = {"redundant": 0.01, "lowconf": 0.9}
    evicted = _select_eviction_candidates(candidates, redundancy, soft_cap=1, hard_cap=100)
    assert evicted == ["redundant"]


def test_eviction_never_touches_protected_rows_while_any_auto_row_remains() -> None:
    candidates = [_candidate("only-auto", "auto", 0.9, 1)] + [
        _candidate(f"p{i}", "upload", None, i) for i in range(10)
    ]
    evicted = _select_eviction_candidates(candidates, {}, soft_cap=1, hard_cap=100)
    assert evicted == ["only-auto"]


def test_eviction_evicts_protected_rows_oldest_first_once_over_the_hard_cap() -> None:
    candidates = [_candidate(f"p{i}", "label", None, i) for i in range(10)]  # zero auto rows
    evicted = _select_eviction_candidates(candidates, {}, soft_cap=1, hard_cap=6)
    assert evicted == ["p0", "p1", "p2", "p3"]


def test_eviction_treats_import_source_as_protected_like_label_and_upload() -> None:
    candidates = [_candidate("imp", "import", None, 1), _candidate("auto1", "auto", 0.9, 2)]
    assert _select_eviction_candidates(candidates, {}, soft_cap=1, hard_cap=100) == ["auto1"]


def test_enforce_training_cap_evicts_auto_rows_down_to_the_soft_cap_and_keeps_every_label_row(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    for i in range(TRAINING_CAP_PER_CAT + 10):
        vec = [0.0] * 120
        vec[i % 120] = 1.0  # near-orthogonal -> large pairwise distance, not mutually redundant
        store.conn.execute(
            "INSERT INTO training(uid, cat, source, created, body, face, face_emb, body_feat, "
            "face_feat, mode, confidence) VALUES (?, 'Kitty', 'auto', ?, NULL, NULL, NULL, ?, NULL, 'day', 0.9)",
            (f"auto-{i}", i, identity.pack(np.array(vec, dtype="float32"))),
        )
    for i in range(2):
        store.conn.execute(
            "INSERT INTO training(uid, cat, source, created, body, face, face_emb, body_feat, "
            "face_feat, mode, confidence) VALUES (?, 'Kitty', 'label', ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
            (f"label-{i}", 10_000 + i),
        )
    store.conn.commit()
    store._enforce_training_cap("Kitty")
    total = store.conn.execute("SELECT COUNT(*) AS n FROM training WHERE cat='Kitty'").fetchone()["n"]
    label_count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM training WHERE cat='Kitty' AND source='label'"
    ).fetchone()["n"]
    assert total == TRAINING_CAP_PER_CAT
    assert label_count == 2


def test_enforce_training_cap_is_a_no_op_under_the_soft_cap(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    store.conn.execute(
        "INSERT INTO training(uid, cat, source, created, body, face, face_emb, body_feat, "
        "face_feat, mode, confidence) VALUES ('a', 'Kitty', 'auto', 1, NULL, NULL, NULL, NULL, NULL, NULL, 0.9)"
    )
    store.conn.commit()
    store._enforce_training_cap("Kitty")
    assert store.conn.execute("SELECT COUNT(*) AS n FROM training").fetchone()["n"] == 1


# --- upload/auto-learn persistence --------------------------------------------------------------


def test_add_upload_training_writes_a_source_upload_row(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    feat = identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")
    saved = store.add_upload_training(cat="Kitty", uid="u1", data=b"jpeg-bytes", features=feat)
    assert saved is not None and saved["source"] == "upload" and saved["cat"] == "Kitty"
    assert (tmp_path / "training" / "kitty" / "u1-body.jpg").read_bytes() == b"jpeg-bytes"


def test_add_upload_training_is_none_for_an_unknown_cat(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    feat = identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")
    assert store.add_upload_training(cat="Nope", uid="u1", data=b"x", features=feat) is None


def test_add_auto_training_is_idempotent_and_never_clobbers_an_existing_row(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Pancake")
    (tmp_path / "media" / "b.jpg").write_bytes(b"body")
    store.conn.execute(
        "INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, scene, "
        "before, after, reviewed, updated) VALUES ('e2', 2, 'visit', 200, 210, 0, NULL, NULL, NULL, NULL, 0, 200)"
    )
    store.insert_sample(
        uid="e2-s1", event_uid="e2", t=200, body="b.jpg", face=None,
        features=identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day"),
        box=None, score=0.9,
    )
    assert store.add_auto_training(cat="Pancake", sample_uid="e2-s1", confidence=0.95) is True
    assert store.add_auto_training(cat="Pancake", sample_uid="e2-s1", confidence=0.95) is False
    row = store.conn.execute("SELECT source, confidence FROM training WHERE uid='e2-s1-train'").fetchone()
    assert row["source"] == "auto" and row["confidence"] == 0.95


def test_a_human_label_promotes_a_previously_auto_sourced_sample_to_label(tmp_path: Path) -> None:
    """"Human and uploaded samples always outrank auto ones": once a person reviews the event a
    sample belongs to, that sample's training row must stop being evictable-as-auto."""
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Pancake")
    (tmp_path / "media" / "b.jpg").write_bytes(b"body")
    store.conn.execute(
        "INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, scene, "
        "before, after, reviewed, updated) VALUES ('e2', 2, 'visit', 200, 210, 0, NULL, NULL, NULL, NULL, 0, 200)"
    )
    store.insert_sample(
        uid="e2-s1", event_uid="e2", t=200, body="b.jpg", face=None,
        features=identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day"),
        box=None, score=0.9,
    )
    store.add_auto_training(cat="Pancake", sample_uid="e2-s1", confidence=0.95)
    store.conn.execute("UPDATE events SET reviewed=1, identity_status='reviewed', cat='Pancake' WHERE uid='e2'")
    store.reconcile_event_training(["e2"], "Pancake")
    row = store.conn.execute("SELECT source FROM training WHERE uid='e2-s1-train'").fetchone()
    assert row["source"] == "label"


# --- review outcomes + auto-learn pause/resume, driven through label_events --------------------


def test_label_events_records_a_review_outcome_only_for_a_prior_auto_verdict(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    # An "auto" verdict the human then confirms -- one hit.
    store.conn.execute(
        "INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, scene, "
        "before, after, cat, identity_status, reviewed, updated) "
        "VALUES ('e1', 1, 'visit', 1, 2, 0, NULL, NULL, NULL, NULL, 'Kitty', 'auto', 0, 1)"
    )
    # An "unknown" verdict -- reviewing this teaches nothing about the engine's own accuracy.
    store.conn.execute(
        "INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, scene, "
        "before, after, cat, identity_status, reviewed, updated) "
        "VALUES ('e2', 2, 'visit', 2, 3, 0, NULL, NULL, NULL, NULL, NULL, 'unknown', 0, 2)"
    )
    store.conn.commit()
    store.label_events(["e1"], "Kitty")
    store.label_events(["e2"], "Kitty")
    outcomes = store.conn.execute("SELECT uid, cat, correct FROM review_outcomes").fetchall()
    assert [(r["uid"], r["cat"], r["correct"]) for r in outcomes] == [("e1", "Kitty", 1)]


def test_repeated_review_of_the_same_event_replaces_its_outcome_row(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    store.conn.execute(
        "INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, scene, "
        "before, after, cat, identity_status, reviewed, updated) "
        "VALUES ('e1', 1, 'visit', 1, 2, 0, NULL, NULL, NULL, NULL, 'Kitty', 'auto', 0, 1)"
    )
    store.conn.commit()
    store.label_events(["e1"], "not_a_cat")  # first review: engine said Kitty, human says no -- miss
    # A person changing their mind is a fresh event lookup each time in this store, so the
    # second call finds `identity_status` already 'not_a_cat' now (no longer 'auto') and does
    # not add a second outcome row -- proving `review_outcomes` never accumulates duplicates
    # for one event no matter how many times it is relabelled.
    store.label_events(["e1"], "Kitty")
    rows = store.conn.execute("SELECT COUNT(*) AS n FROM review_outcomes WHERE uid='e1'").fetchone()
    assert rows["n"] == 1


def test_auto_learn_pauses_a_cat_after_enough_wrong_reviews_and_resumes_after_recovery(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("TestCat")
    for i in range(20):
        store.conn.execute(
            "INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, scene, "
            "before, after, cat, identity_status, reviewed, updated) "
            "VALUES (?, ?, 'visit', ?, ?, 0, NULL, NULL, NULL, NULL, 'TestCat', 'auto', 0, ?)",
            (f"rev-{i}", i, i, i + 1, i),
        )
    store.conn.commit()
    assert store.auto_learn_paused("TestCat") is False
    for i in range(20):
        label = "TestCat" if i < 4 else "not_a_cat"  # 4/20 = 20% accuracy
        store.label_events([f"rev-{i}"], label)
    assert store.auto_learn_paused("TestCat") is True

    # Recovery: a fresh run of mostly-correct reviews on new events pushes the rolling window
    # (last `ROLLING_REVIEW_WINDOW`) back above the resume threshold.
    for i in range(20, 40):
        store.conn.execute(
            "INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, scene, "
            "before, after, cat, identity_status, reviewed, updated) "
            "VALUES (?, ?, 'visit', ?, ?, 0, NULL, NULL, NULL, NULL, 'TestCat', 'auto', 0, ?)",
            (f"rev-{i}", i, i, i + 1, i),
        )
    store.conn.commit()
    for i in range(20, 40):
        store.label_events([f"rev-{i}"], "TestCat")  # 20 correct in a row
    assert store.auto_learn_paused("TestCat") is False


# --- schema migration: cats gains avatar_asset/avatar_updated, training gains confidence -------


def _legacy_v3_db(root: Path) -> None:
    """A pre-migration database (schema version 3, before avatar/confidence/auto-learn
    tables): built with the exact SQL those tables had at that version, plus one real row in
    each of `cats`/`training` to prove migration never loses existing data."""
    conn = sqlite3.connect(root / "kibble.db")
    conn.executescript(
        """
        CREATE TABLE cats(name TEXT PRIMARY KEY, color INTEGER NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE training(
            uid TEXT PRIMARY KEY, cat TEXT NOT NULL, source TEXT NOT NULL, created INTEGER NOT NULL,
            body TEXT, face TEXT, face_emb BLOB, body_feat BLOB, face_feat BLOB, mode TEXT
        );
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO cats(name, color, created) VALUES ('Kitty', 0, 100);
        INSERT INTO training(uid, cat, source, created) VALUES ('t1', 'Kitty', 'label', 100);
        INSERT INTO meta(key, value) VALUES ('schema_version', '3');
        """
    )
    conn.commit()
    conn.close()


def test_migrating_a_v3_database_adds_avatar_and_confidence_columns_without_losing_rows(
    tmp_path: Path,
) -> None:
    _legacy_v3_db(tmp_path)
    store = _SyncStore(tmp_path, "entry1")
    cat_row = store.conn.execute("SELECT * FROM cats WHERE name='Kitty'").fetchone()
    assert cat_row["avatar_asset"] is None and cat_row["avatar_updated"] is None
    training_row = store.conn.execute("SELECT * FROM training WHERE uid='t1'").fetchone()
    assert training_row["confidence"] is None and training_row["source"] == "label"
    version = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)
    # The new tables exist and are queryable even though the legacy DB never had them.
    assert store.conn.execute("SELECT COUNT(*) AS n FROM review_outcomes").fetchone()["n"] == 0
    assert store.auto_learn_paused("Kitty") is False


def test_v4_migration_is_idempotent_across_repeated_opens(tmp_path: Path) -> None:
    _legacy_v3_db(tmp_path)
    store1 = _SyncStore(tmp_path, "entry1")
    store1.set_cat_avatar("Kitty", b"avatar-bytes")
    store1.close()
    store2 = _SyncStore(tmp_path, "entry1")
    assert store2.avatar_info("Kitty")["custom"] is True
    version = store2.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)
    store2.close()


# --- session splitting: one device track, more than one cat (docs/36-ai-pipeline.md) -------


def _subject_scores() -> dict[int, sessions.IdentityScores]:
    return {
        1: sessions.IdentityScores((("Kitty", 0.95), ("_other", 0.05)), 0.75, 0.2),
        2: sessions.IdentityScores((("Pancake", 0.93), ("_other", 0.07)), 0.75, 0.2),
    }


def _insert_cooccurring_session(
    store: _SyncStore,
    uid: str = "e1",
    *,
    start: int = 100,
    end: int = 140,
    open_: bool = False,
    pancake_eats: bool = False,
) -> None:
    store.add_cat("Kitty")
    store.add_cat("Pancake")
    _insert_event(
        store, uid, start, kind="eat", end=end, open_=open_, eat_start=start + 4,
        scene="scene.jpg", before="before.jpg", after="after.jpg", scene_k=start,
        subjects=[
            {"sid": 1, "first": start, "last": start + 20, "eat_start": start + 4},
            {"sid": 2, "first": start + 4, "last": start + 24,
             "eat_start": start + 12 if pancake_eats else None},
        ],
    )
    frame_boxes = [[1, 0.1, 0.2, 0.4, 0.8], [2, 0.6, 0.2, 0.9, 0.8]]
    for index, offset in enumerate((0, 4, 8)):
        t = start + offset
        store.insert_sample(
            uid=f"{uid}-s{index}", event_uid=uid, t=t, body=f"{uid}-s{index}.jpg", face=None,
            features=_features(), box=(0.1, 0.2, 0.4, 0.8), score=0.9,
            sid=1, frame_k=t, is_primary=True,
            frame_boxes=frame_boxes if index == 0 else None,
        )
        store.insert_sample(
            uid=f"{uid}-s{index}-o2", event_uid=uid, t=t, body=f"{uid}-s{index}-o2.jpg", face=None,
            features=_features(), box=(0.6, 0.2, 0.9, 0.8), score=0.85,
            sid=2, frame_k=t, is_primary=False,
            frame_boxes=frame_boxes if index == 0 else None,
        )


def test_replan_creates_cat_parts_and_event_detail_covers_the_whole_session(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_cooccurring_session(store)

    assert store.replan_session("e1", _subject_scores()) is True
    assert store.replan_session("e1", _subject_scores()) is False

    parent = store.conn.execute("SELECT * FROM events WHERE uid='e1'").fetchone()
    children = {
        row["cat"]: row for row in store.conn.execute("SELECT * FROM events WHERE parent_uid='e1'")
    }
    assert parent["hidden"] == 1
    assert set(children) == {"Kitty", "Pancake"}
    kitty, pancake = children["Kitty"], children["Pancake"]
    assert kitty["uid"] == "e1-cat-kitty" and pancake["uid"] == "e1-cat-pancake"
    assert (kitty["kind"], kitty["eat_start"], kitty["before"], kitty["after"]) == (
        "eat", 104, "before.jpg", "after.jpg"
    )
    assert (pancake["kind"], pancake["eat_start"], pancake["before"], pancake["after"]) == (
        "visit", None, None, None
    )
    assert kitty["scene"] == pancake["scene"] == "scene.jpg"
    assert {row["uid"] for row in store.conn.execute("SELECT uid FROM samples WHERE event_uid=?", (kitty["uid"],))} == {
        "e1-s0", "e1-s1", "e1-s2"
    }
    assert {row["uid"] for row in store.conn.execute("SELECT uid FROM samples WHERE event_uid=?", (pancake["uid"],))} == {
        "e1-s0-o2", "e1-s1-o2", "e1-s2-o2"
    }

    store.conn.execute("UPDATE events SET judge_multiple=1 WHERE uid='e1'")
    store.conn.commit()
    detail = store.event_detail(pancake["uid"])
    assert detail is not None
    assert detail["event"]["uid"] == "e1" and detail["event"]["cat"] is None
    assert detail["event"]["kind"] == "eat"
    assert detail["multiple_cats"] is True
    assert [sample["uid"] for sample in detail["samples"]] == [
        "e1-s0", "e1-s0-o2", "e1-s1", "e1-s1-o2", "e1-s2", "e1-s2-o2"
    ]
    assert {sample["cat"] for sample in detail["samples"] if sample["cat"] == "Kitty"} == {"Kitty"}
    assert [cat["cat"] for cat in detail["cats"]] == ["Kitty", "Pancake"]
    assert [cat["ate"] for cat in detail["cats"]] == [True, False]
    assert len(detail["scene_subjects"]) == 2
    assert all(set(subject) == {"box", "sid", "label", "reviewed"} for subject in detail["scene_subjects"])
    assert "companions" not in detail and all("cat" in sample for sample in detail["samples"])


def test_human_cat_set_is_exact_can_add_an_empty_cat_remove_one_and_collapse(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Whiskers")
    _insert_cooccurring_session(store)
    store.replan_session("e1", _subject_scores())

    detail = store.set_session_cats(
        "e1", [("Kitty", True), ("Pancake", False), ("Whiskers", True)]
    )
    assert detail is not None
    children = {row["cat"]: row for row in store.conn.execute("SELECT * FROM events WHERE parent_uid='e1'")}
    assert set(children) == {"Kitty", "Pancake", "Whiskers"}
    assert children["Whiskers"]["start"] == 100 and children["Whiskers"]["end"] == 140
    assert children["Whiskers"]["reviewed"] == 1 and children["Whiskers"]["kind"] == "eat"
    assert store.conn.execute("SELECT COUNT(*) FROM samples WHERE event_uid=?", (children["Whiskers"]["uid"],)).fetchone()[0] == 0

    store.set_session_cats("e1", [("Kitty", True), ("Pancake", False)])
    assert {row["cat"] for row in store.conn.execute("SELECT cat FROM events WHERE parent_uid='e1'")} == {
        "Kitty", "Pancake"
    }
    store.replan_session("e1", {1: sessions.IdentityScores((("Pancake", 0.99),), 0.75, 0.2)})
    preserved = {row["cat"]: row["identity_status"] for row in store.conn.execute("SELECT cat, identity_status FROM events WHERE parent_uid='e1'")}
    assert preserved == {"Kitty": "reviewed", "Pancake": "reviewed"}

    detail = store.set_session_cats("e1", [("Pancake", True)])
    assert detail is not None and detail["event"]["uid"] == "e1"
    parent = store.conn.execute("SELECT hidden, cat, reviewed FROM events WHERE uid='e1'").fetchone()
    assert (parent["hidden"], parent["cat"], parent["reviewed"]) == (0, "Pancake", 1)
    assert store.conn.execute("SELECT COUNT(*) FROM events WHERE parent_uid='e1'").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM samples WHERE event_uid='e1'").fetchone()[0] == 6


def test_a_photo_box_repeated_without_a_sid_is_not_drawn_twice(tmp_path: Path) -> None:
    """Session e2060's scene frame: the primary photo had no sid, and `frame_boxes` repeated its
    box with a null sid next to a second (sid 27) box. That showed one Kitty as both "Kitty" and
    an unnamed "Who?" box."""
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    start = int(time.time()) - 600
    _insert_event(store, "e1", start, kind="eat", end=start + 60, open_=False)
    primary = (0.028125, 0.0, 0.56328124, 0.61527777)
    other = (0.13359375, 0.104166664, 0.5882813, 0.85)
    frame_boxes = [[None, *primary], [27, *other]]
    store.insert_sample(uid="e1-s24", event_uid="e1", t=start + 10, body="b.jpg", face=None, features=_features(),
                        box=primary, score=0.9, sid=None, frame_k=24, is_primary=True, frame_boxes=frame_boxes)
    store.insert_sample(uid="e1-s24-o27", event_uid="e1", t=start + 10, body="b.jpg", face=None, features=_features(),
                        box=other, score=0.9, sid=27, frame_k=24, is_primary=False, frame_boxes=frame_boxes)
    store.conn.execute("UPDATE events SET scene_k=24, cat='Kitty', identity_status='reviewed', reviewed=1 WHERE uid='e1'")
    store.conn.commit()

    detail = store.event_detail("e1")

    assert detail is not None
    assert [(item["sid"], item["label"]) for item in detail["scene_subjects"]] == [(None, "Kitty"), (27, "Kitty")]



def test_subject_review_preserves_photo_review_and_event_detail_labels_scene_boxes(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_cooccurring_session(store)
    store.replan_session("e1", _subject_scores())
    store.set_session_cats("e1", [("Kitty", True), ("Pancake", True)])

    store.set_session_subject("e1", 1, "Pancake", _subject_scores())
    sample = store.label_sample("e1-s0", "Kitty", _subject_scores())
    assert sample is not None and sample["review"] == "Kitty"
    rows = {
        row["uid"]: row for row in store.conn.execute(
            "SELECT uid, review, review_src FROM samples WHERE uid IN ('e1-s0', 'e1-s1')"
        )
    }
    assert (rows["e1-s0"]["review"], rows["e1-s0"]["review_src"]) == ("Kitty", "photo")
    assert (rows["e1-s1"]["review"], rows["e1-s1"]["review_src"]) == ("Pancake", "subject")

    detail = store.event_detail("e1")
    assert detail is not None
    scene = {item["sid"]: item for item in detail["scene_subjects"]}
    assert scene[1]["label"] == "Kitty" and scene[1]["reviewed"] is True
    assert scene[2]["label"] == "Pancake"
    assert all(set(item) == {"box", "sid", "label", "reviewed"} for item in scene.values())

    store.set_session_subject("e1", 1, "follow", _subject_scores())
    rows = {
        row["uid"]: row for row in store.conn.execute(
            "SELECT uid, review, review_src FROM samples WHERE uid IN ('e1-s0', 'e1-s1')"
        )
    }
    assert (rows["e1-s0"]["review"], rows["e1-s0"]["review_src"]) == ("Kitty", "photo")
    assert (rows["e1-s1"]["review"], rows["e1-s1"]["review_src"]) == (None, None)


def test_deleting_a_split_parent_cascades_to_its_cat_parts_and_samples(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_cooccurring_session(store)
    store.replan_session("e1", _subject_scores())

    store._delete_event("e1")

    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0


def test_migrating_a_pre_split_database_adds_parent_and_hidden_columns_without_losing_rows(tmp_path: Path) -> None:
    store1 = _SyncStore(tmp_path, "entry1")
    _insert_event(store1, "old-1", 100)
    store1.close()
    columns = {r[1] for r in sqlite3.connect(tmp_path / "kibble.db").execute("PRAGMA table_info(events)")}
    assert {"parent_uid", "hidden"} <= columns

    store2 = _SyncStore(tmp_path, "entry1")  # re-opens the same file; `_migrate` reruns
    row = store2.conn.execute("SELECT * FROM events WHERE uid='old-1'").fetchone()
    assert row["hidden"] == 0 and row["parent_uid"] is None
    version = store2.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)


# --- schema migration: `events` gains `clip_id`/`clip_start_ms`/`clip_end_ms` -------------------


def _legacy_v5_events_db(root: Path) -> None:
    """A pre-migration database (schema version 5, before `clip_id`/`clip_start_ms`/
    `clip_end_ms`): the exact `events` shape that version had, plus one real row to prove
    migration never loses existing data."""
    conn = sqlite3.connect(root / "kibble.db")
    conn.executescript(
        """
        CREATE TABLE events(
            uid TEXT PRIMARY KEY, device_event_id INTEGER, kind TEXT NOT NULL, start INTEGER NOT NULL,
            end INTEGER, open INTEGER NOT NULL, eat_start INTEGER, scene TEXT, before TEXT, after TEXT,
            cat TEXT, identity_status TEXT, confidence REAL, reviewed INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL, parent_uid TEXT, hidden INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, scene, before, after,
                            cat, identity_status, confidence, reviewed, updated, parent_uid, hidden)
        VALUES ('e1', 1, 'eat', 100, 160, 0, 104, NULL, NULL, NULL, 'Kitty', 'auto', 0.9, 0, 200, NULL, 0);
        INSERT INTO meta(key, value) VALUES ('schema_version', '5');
        """
    )
    conn.commit()
    conn.close()


def test_migrating_a_pre_clip_database_adds_clip_columns_without_losing_rows(tmp_path: Path) -> None:
    _legacy_v5_events_db(tmp_path)
    store = _SyncStore(tmp_path, "entry1")
    row = store.conn.execute("SELECT * FROM events WHERE uid='e1'").fetchone()
    assert (row["clip_id"], row["clip_start_ms"], row["clip_end_ms"]) == (None, None, None)
    assert row["cat"] == "Kitty"  # the pre-existing row survives untouched
    version = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)


def test_clip_column_migration_is_idempotent_across_repeated_opens(tmp_path: Path) -> None:
    _legacy_v5_events_db(tmp_path)
    store1 = _SyncStore(tmp_path, "entry1")
    store1.set_event_clip("e1", clip_id="100000_160000_0001000000.mp4", clip_start_ms=90000, clip_end_ms=175000)
    store1.close()
    store2 = _SyncStore(tmp_path, "entry1")  # re-opens the same file; `_migrate` reruns
    row = store2.conn.execute("SELECT clip_id FROM events WHERE uid='e1'").fetchone()
    assert row["clip_id"] == "100000_160000_0001000000.mp4"
    version = store2.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)
    store2.close()


# --- eating clips: linking, resolution, child-through-parent, timeline payload ------------------


def test_set_event_clip_then_resolve_returns_the_linked_clip(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat", end=160, open_=False)
    assert store.resolve_event_clip("e1") is None  # nothing linked yet
    store.set_event_clip("e1", clip_id="100000_160000_0001000000.mp4", clip_start_ms=90000, clip_end_ms=175000)
    clip = store.resolve_event_clip("e1")
    assert clip == {"owner_uid": "e1", "clip_id": "100000_160000_0001000000.mp4", "start_ms": 90000, "end_ms": 175000}


def test_resolve_event_clip_is_none_for_an_unknown_event(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    assert store.resolve_event_clip("nope") is None


def test_clear_event_clip_removes_the_link(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat", end=160, open_=False)
    store.set_event_clip("e1", clip_id="c.mp4", clip_start_ms=1, clip_end_ms=2)
    store.clear_event_clip("e1")
    assert store.resolve_event_clip("e1") is None


def test_a_split_child_resolves_its_clip_through_its_parent(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_cooccurring_session(store)
    store.replan_session("e1", _subject_scores())
    children = [row["uid"] for row in store.conn.execute("SELECT uid FROM events WHERE parent_uid='e1'")]
    assert len(children) == 2
    store.set_event_clip("e1", clip_id="c.mp4", clip_start_ms=1000, clip_end_ms=9000)
    for child_uid in children:
        clip = store.resolve_event_clip(child_uid)
        assert clip == {"owner_uid": "e1", "clip_id": "c.mp4", "start_ms": 1000, "end_ms": 9000}


def test_clearing_via_the_resolved_owner_uid_clears_it_for_every_child_too(tmp_path: Path) -> None:
    """Clearing a family clip via its resolved owner removes it for every cat part."""
    store = _SyncStore(tmp_path, "entry1")
    _insert_cooccurring_session(store)
    store.replan_session("e1", _subject_scores())
    child_uid = store.conn.execute("SELECT uid FROM events WHERE parent_uid='e1' LIMIT 1").fetchone()["uid"]
    store.set_event_clip("e1", clip_id="c.mp4", clip_start_ms=1000, clip_end_ms=9000)
    resolved = store.resolve_event_clip(child_uid)
    store.clear_event_clip(resolved["owner_uid"])
    assert store.resolve_event_clip(child_uid) is None
    assert store.resolve_event_clip("e1") is None


def test_events_needing_clip_link_finds_only_closed_unlinked_top_level_eat_sessions(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "open-eat", 100, kind="eat", end=None, open_=True)  # still open
    _insert_event(store, "visit", 200, kind="visit", end=260, open_=False)  # not an eat
    _insert_event(store, "already-linked", 300, kind="eat", end=360, open_=False)
    store.set_event_clip("already-linked", clip_id="c.mp4", clip_start_ms=1, clip_end_ms=2)
    _insert_event(store, "needs-link", 400, kind="eat", end=460, open_=False)
    _insert_cooccurring_session(store, uid="split-parent")
    store.replan_session("split-parent", _subject_scores())

    found = {uid for uid, _start, _end in store.events_needing_clip_link(cutoff=0)}
    assert found == {"needs-link", "split-parent"}


def test_events_needing_clip_link_respects_the_cutoff(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "old", 100, kind="eat", end=160, open_=False)
    _insert_event(store, "recent", 1000, kind="eat", end=1060, open_=False)
    found = {uid for uid, _start, _end in store.events_needing_clip_link(cutoff=500)}
    assert found == {"recent"}


def test_timeline_and_event_detail_carry_the_clip_field_only_when_linked(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat", end=160, open_=False)
    _insert_sample(store, "e1-s1", "e1", 105)
    page = store.timeline_page(limit=10, cursor=None)
    item = next(i for i in page["items"] if i["uid"] == "e1")
    assert item["clip"] is None
    store.set_event_clip("e1", clip_id="c.mp4", clip_start_ms=90000, clip_end_ms=175000)
    page = store.timeline_page(limit=10, cursor=None)
    item = next(i for i in page["items"] if i["uid"] == "e1")
    assert item["clip"] == {"start_ms": 90000, "end_ms": 175000}
    detail = store.event_detail("e1")
    assert detail is not None
    assert detail["event"]["clip"] == {"start_ms": 90000, "end_ms": 175000}


def test_each_cat_parts_timeline_payload_carries_the_parents_clip(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_cooccurring_session(store, pancake_eats=True)
    store.replan_session("e1", _subject_scores())
    store.set_event_clip("e1", clip_id="c.mp4", clip_start_ms=1000, clip_end_ms=9000)
    page = store.timeline_page(limit=10, cursor=None)
    child_items = [i for i in page["items"] if i["uid"].startswith("e1-cat-")]
    assert len(child_items) == 2
    for item in child_items:
        assert item["clip"] == {"start_ms": 1000, "end_ms": 9000}


# --- schema migration: `events` gains `judge_*`, `cats` gains `description` ---------------------


def _legacy_v6_db(root: Path) -> None:
    """A pre-migration database (schema version 6, before the vision judge): the exact `events`/
    `cats` shapes that version had, plus one real row of each to prove migration never loses
    existing data."""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "kibble.db")
    conn.executescript(
        """
        CREATE TABLE events(
            uid TEXT PRIMARY KEY, device_event_id INTEGER, kind TEXT NOT NULL, start INTEGER NOT NULL,
            end INTEGER, open INTEGER NOT NULL, eat_start INTEGER, scene TEXT, before TEXT, after TEXT,
            cat TEXT, identity_status TEXT, confidence REAL, reviewed INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL, parent_uid TEXT, hidden INTEGER NOT NULL DEFAULT 0,
            clip_id TEXT, clip_start_ms INTEGER, clip_end_ms INTEGER
        );
        CREATE TABLE cats(
            name TEXT PRIMARY KEY, color INTEGER NOT NULL, created INTEGER NOT NULL,
            avatar_asset TEXT, avatar_updated INTEGER
        );
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, scene, before, after,
                            cat, identity_status, confidence, reviewed, updated, parent_uid, hidden,
                            clip_id, clip_start_ms, clip_end_ms)
        VALUES ('e1', 1, 'eat', 100, 160, 0, 104, NULL, NULL, NULL, 'Kitty', 'auto', 0.9, 0, 200, NULL, 0,
                NULL, NULL, NULL);
        INSERT INTO cats(name, color, created) VALUES ('Kitty', 0, 100);
        INSERT INTO meta(key, value) VALUES ('schema_version', '6');
        """
    )
    conn.commit()
    conn.close()


def test_migrating_a_pre_judge_database_adds_judge_and_description_columns_without_losing_rows(
    tmp_path: Path,
) -> None:
    _legacy_v6_db(tmp_path)

    store = _SyncStore(tmp_path, "entry1")

    row = store.conn.execute("SELECT * FROM events WHERE uid='e1'").fetchone()
    assert row["cat"] == "Kitty" and row["identity_status"] == "auto"  # the pre-existing row survives
    for column in (
        "judge_present", "judge_cat", "judge_confidence", "judge_multiple",
        "judge_reason", "judge_model", "judge_at", "judge_evidence",
    ):
        assert row[column] is None
    cat_row = store.conn.execute("SELECT * FROM cats WHERE name='Kitty'").fetchone()
    assert cat_row["description"] is None
    version = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)


def test_judge_column_migration_is_idempotent_across_repeated_opens(tmp_path: Path) -> None:
    _legacy_v6_db(tmp_path)
    store1 = _SyncStore(tmp_path, "entry1")
    store1.set_cat_description("Kitty", "brown mackerel tabby")
    store1.close()

    store2 = _SyncStore(tmp_path, "entry1")  # re-opens the same file; `_migrate` reruns
    row = store2.conn.execute("SELECT description FROM cats WHERE name='Kitty'").fetchone()
    assert row["description"] == "brown mackerel tabby"
    version = store2.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)


# --- upsert_event: `scene` is replaced, `before`/`after` never are ------------------------------


def test_upsert_event_replaces_scene_but_never_before_or_after(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat", scene="d/scene-1.jpg", before="d/before.jpg", after=None)
    row1 = store.conn.execute("SELECT scene, before, after FROM events WHERE uid='e1'").fetchone()
    assert (row1["scene"], row1["before"]) == ("d/scene-1.jpg", "d/before.jpg")

    # ingest.py always resolves the correct final value itself (kept, or freshly re-fetched);
    # upsert_event just takes whatever it is given for scene, but keeps the first before/after.
    _insert_event(
        store, "e1", 100, kind="eat", scene="d/scene-2.jpg",
        before="d/SHOULD-NOT-OVERWRITE.jpg", after="d/after.jpg",
    )
    row2 = store.conn.execute("SELECT scene, before, after FROM events WHERE uid='e1'").fetchone()
    assert row2["scene"] == "d/scene-2.jpg", "scene must be replaced"
    assert row2["before"] == "d/before.jpg", "before must never be replaced once set"
    assert row2["after"] == "d/after.jpg"  # after was never set before -- this is a first fetch, not a replace


# --- vision judge SQL surface (docs/40-vision-judge.md) ------------------------------------------


def test_events_needing_judge_filters_kind_open_reviewed_hidden_and_cutoff(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "eat1", 1000, kind="eat", end=1010, open_=False)
    _insert_event(store, "visit1", 1005, kind="visit", end=1006, open_=False)
    _insert_event(store, "open1", 1006, kind="eat", end=None, open_=True)
    _insert_event(store, "import1", 1007, kind="import", end=1008, open_=False)
    _insert_event(store, "too_old", 10, kind="eat", end=20, open_=False)
    store.conn.execute("UPDATE events SET reviewed=1 WHERE uid='visit1'")
    store.conn.execute("UPDATE events SET hidden=1 WHERE uid='eat1'")
    store.conn.commit()
    assert store.events_needing_judge(cutoff=500, limit=100) == []

    store.conn.execute("UPDATE events SET hidden=0 WHERE uid='eat1'")
    store.conn.commit()
    assert store.events_needing_judge(cutoff=500, limit=100) == ["eat1"]


def test_events_needing_judge_is_newest_first_and_respects_the_limit(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "eat1", 1000, kind="eat", end=1010, open_=False)
    _insert_event(store, "eat2", 2000, kind="eat", end=2010, open_=False)
    _insert_event(store, "eat3", 1500, kind="eat", end=1510, open_=False)
    assert store.events_needing_judge(cutoff=0, limit=2) == ["eat2", "eat3"]


def test_judge_event_context_returns_the_fields_the_judge_needs_or_none(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat", scene="d/e1-scene.jpg")
    ctx = store.judge_event_context("e1")
    assert ctx == {
        "kind": "eat", "open": False, "reviewed": False, "hidden": False,
        "scene": "d/e1-scene.jpg", "before": None, "after": None, "cat": None,
        "identity_status": None, "confidence": None, "judge_evidence": None, "sample_count": 0,
    }
    assert store.judge_event_context("does-not-exist") is None


def test_event_sample_candidates_matches_thumb_candidates(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")
    _insert_sample(store, "e1-s1", "e1", 100, body="d/e1-s1.jpg")
    candidates = store.event_sample_candidates("e1")
    assert len(candidates) == 1
    assert candidates[0].uid == "e1-s1" and candidates[0].body == "d/e1-s1.jpg"


def test_apply_judge_verdict_record_only_never_touches_identity_fields(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")
    store.conn.execute("UPDATE events SET cat='Kitty', identity_status='auto', confidence=0.6 WHERE uid='e1'")
    store.conn.commit()

    changed = store.apply_judge_verdict(
        "e1", model="qwen3-vl-4b", evidence="d/e1-scene.jpg|", present=True, cat="Kitty",
        confidence=0.5, multiple=False, reason="ok", apply_identity=False,
    )
    assert changed is True
    row = store.conn.execute("SELECT * FROM events WHERE uid='e1'").fetchone()
    assert (row["judge_present"], row["judge_cat"], row["judge_confidence"], row["judge_model"]) == (
        1, "Kitty", 0.5, "qwen3-vl-4b",
    )
    assert row["judge_evidence"] == "d/e1-scene.jpg|"
    assert (row["cat"], row["identity_status"], row["confidence"]) == ("Kitty", "auto", 0.6)  # untouched


def test_apply_judge_verdict_with_apply_identity_overwrites_cat_identity_status_and_confidence(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")
    store.conn.execute("UPDATE events SET cat='Kitty', identity_status='auto', confidence=0.6 WHERE uid='e1'")
    store.conn.commit()

    store.apply_judge_verdict(
        "e1", model="qwen3-vl-4b", evidence="d/e1-scene.jpg|s1", present=False, cat="none",
        confidence=0.9, multiple=False, reason="empty bowl", apply_identity=True,
        new_cat=None, new_identity_status=identity.NOT_A_CAT, new_confidence=None,
    )
    row = store.conn.execute("SELECT * FROM events WHERE uid='e1'").fetchone()
    assert (row["cat"], row["identity_status"], row["confidence"]) == (None, "not_a_cat", None)
    assert row["judge_evidence"] == "d/e1-scene.jpg|s1"


def test_apply_judge_verdict_is_a_true_no_op_against_a_reviewed_event(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")
    store.conn.execute(
        "UPDATE events SET cat='Pancake', identity_status='auto', confidence=0.7, reviewed=1 WHERE uid='e1'"
    )
    store.conn.commit()

    changed = store.apply_judge_verdict(
        "e1", model="m", evidence="x", present=False, cat="none", confidence=0.99,
        multiple=False, reason="looks empty", apply_identity=True,
        new_cat=None, new_identity_status=identity.NOT_A_CAT, new_confidence=None,
    )
    assert changed is False
    row = store.conn.execute("SELECT * FROM events WHERE uid='e1'").fetchone()
    assert (row["cat"], row["identity_status"]) == ("Pancake", "auto")  # completely untouched
    assert row["judge_present"] is None, "not even the record-only judge_* columns are written"


def test_apply_judge_verdict_against_an_unknown_uid_returns_false(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    assert store.apply_judge_verdict(
        "nope", model="m", evidence="e", present=True, cat="none", confidence=0.5,
        multiple=False, reason="r", apply_identity=False,
    ) is False


def test_invalidate_scene_asset_deletes_an_unreferenced_file_and_clears_judge_evidence(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")
    media_dir = store.media_root / "2026-09-25"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "old-scene.jpg").write_bytes(b"old-scene-bytes")
    store.conn.execute("UPDATE events SET judge_evidence=? WHERE uid='e1'", ("2026-09-25/old-scene.jpg|",))
    store.conn.commit()

    store.invalidate_scene_asset("e1", "2026-09-25/old-scene.jpg")

    assert not (media_dir / "old-scene.jpg").exists()
    row = store.conn.execute("SELECT judge_evidence FROM events WHERE uid='e1'").fetchone()
    assert row["judge_evidence"] is None


def test_invalidate_scene_asset_never_deletes_a_file_still_referenced_elsewhere(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat", scene="2026-09-25/shared.jpg")
    _insert_event(store, "e2", 200, kind="eat")
    media_dir = store.media_root / "2026-09-25"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "shared.jpg").write_bytes(b"shared-bytes")

    # e1's own event row still references it -- e2's (unrelated) replacement must not delete it.
    store.invalidate_scene_asset("e2", "2026-09-25/shared.jpg")
    assert (media_dir / "shared.jpg").exists()

    # a sample's body crop referencing it is equally protective.
    store.conn.execute("UPDATE events SET scene=NULL WHERE uid='e1'")
    store.conn.commit()
    _insert_sample(store, "e1-s1", "e1", 100, body="2026-09-25/shared.jpg")
    store.invalidate_scene_asset("e2", "2026-09-25/shared.jpg")
    assert (media_dir / "shared.jpg").exists()

    # once truly unreferenced, it is deleted.
    store.conn.execute("DELETE FROM samples WHERE uid='e1-s1'")
    store.conn.commit()
    store.invalidate_scene_asset("e2", "2026-09-25/shared.jpg")
    assert not (media_dir / "shared.jpg").exists()


def test_cat_description_methods(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    store.add_cat("Pancake")
    assert set(store.cats_needing_description()) == {"Kitty", "Pancake"}

    store.set_cat_description("Kitty", "brown mackerel tabby")
    assert store.cats_needing_description() == ["Pancake"]
    assert store.cat_descriptions() == {"Kitty": "brown mackerel tabby", "Pancake": None}

    # an empty string still counts as "needing" one, same as NULL.
    store.set_cat_description("Pancake", "")
    assert store.cats_needing_description() == ["Pancake"]


def test_training_crop_for_mode_picks_the_newest_per_mode(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    assert store.training_crop_for_mode("Kitty", "day") is None
    store.conn.executescript(
        """
        INSERT INTO training(uid, cat, source, created, body, mode)
        VALUES ('t1', 'Kitty', 'auto', 100, 'training/kitty/t1-body.jpg', 'day');
        INSERT INTO training(uid, cat, source, created, body, mode)
        VALUES ('t2', 'Kitty', 'auto', 200, 'training/kitty/t2-body.jpg', 'day');
        """
    )
    store.conn.commit()
    assert store.training_crop_for_mode("Kitty", "day") == {"uid": "t2", "body": "training/kitty/t2-body.jpg"}
    assert store.training_crop_for_mode("Kitty", "ir") is None


def test_judge_diagnostics_counts_verdicts_by_outcome(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")
    _insert_event(store, "e2", 200, kind="eat")
    assert store.judge_diagnostics() == {
        "total_judged": 0, "cat_present_true": 0, "cat_present_false": 0, "identity_fixed": 0,
    }
    store.apply_judge_verdict(
        "e1", model="m", evidence="e", present=True, cat="Kitty", confidence=0.9, multiple=False,
        reason="r", apply_identity=True, new_cat="Kitty", new_identity_status="auto", new_confidence=0.9,
    )
    store.apply_judge_verdict(
        "e2", model="m", evidence="e", present=False, cat="none", confidence=0.9, multiple=False,
        reason="r", apply_identity=True, new_cat=None, new_identity_status=identity.NOT_A_CAT, new_confidence=None,
    )
    assert store.judge_diagnostics() == {
        "total_judged": 2, "cat_present_true": 1, "cat_present_false": 1, "identity_fixed": 1,
    }


def test_event_training_context_includes_judge_cat_and_judge_at(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")
    store.conn.execute("UPDATE events SET cat='Kitty', identity_status='auto' WHERE uid='e1'")
    store.conn.commit()
    ctx = store.event_training_context("e1")
    assert ctx == {
        "open": False, "reviewed": False, "identity_status": "auto", "cat": "Kitty",
        "judge_cat": None, "judge_at": None,
    }

    store.apply_judge_verdict(
        "e1", model="qwen3-vl-4b", evidence="x", present=True, cat="Kitty", confidence=0.9,
        multiple=False, reason="r", apply_identity=True,
        new_cat="Kitty", new_identity_status="auto", new_confidence=0.9,
    )
    ctx2 = store.event_training_context("e1")
    assert ctx2["judge_cat"] == "Kitty" and ctx2["judge_at"] is not None


# --- crop-geometry gallery build: skip label/auto training rows from untrustworthy crops --------


def test_all_training_features_skips_a_label_or_auto_row_copied_from_an_untrustworthy_crop(
    tmp_path: Path,
) -> None:
    """(a): the identity engine's own gallery build must never train on a `label`/`auto` row
    whose source device sample had a crop the 2026-09-25 shift bug corrupted -- skipped only,
    the row and its files are left exactly as they were."""
    store = _SyncStore(tmp_path, "entry1")
    bad_box = (0.478, 0.208, 0.593, 0.622)  # e1492-s1 -- overlap 0.0, always untrustworthy
    _insert_event(store, "e1", 100, kind="eat")
    _insert_sample(store, "e1-s1", "e1", 100, body="d/e1-s1-body.jpg")
    store.conn.execute(
        "UPDATE samples SET box_x1=?, box_y1=?, box_x2=?, box_y2=? WHERE uid='e1-s1'", bad_box
    )
    store.conn.commit()
    store.add_cat("Kitty")
    assert store.add_auto_training(cat="Kitty", sample_uid="e1-s1", confidence=0.95) is True

    gallery = store.all_training_features()
    assert gallery == [], "a training row copied from an untrustworthy crop must never enter the gallery"
    # never deleted -- the row (and its file) are still there for a later, more informed pass
    assert store.conn.execute("SELECT 1 FROM training WHERE uid='e1-s1-train'").fetchone() is not None


def test_all_training_features_always_keeps_import_rows_regardless_of_crop_trustworthiness(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    store.add_import_training(cat="Kitty", uid="import-1", body=b"x", face=None, features=_features())
    gallery = store.all_training_features()
    assert [cat for cat, _feat in gallery] == ["Kitty"]


def test_all_training_features_keeps_a_row_whose_source_sample_has_since_been_purged(
    tmp_path: Path,
) -> None:
    """A `label`/`auto` row survives long past its own source sample's retention window --
    purge is routine and proves nothing about crop quality, so an unprovable row must never be
    excluded from the gallery just because the sample it was copied from is gone."""
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    store.conn.execute(
        "INSERT INTO training(uid, cat, source, created, body, mode) "
        "VALUES ('gone-s1-train', 'Kitty', 'auto', 100, 'x.jpg', 'day')"
    )
    store.conn.commit()
    gallery = store.all_training_features()
    assert [cat for cat, _feat in gallery] == ["Kitty"]


# --- crop-geometry card display: untrustworthy legacy samples are never shown -------------------


def test_event_detail_keeps_every_photo_but_marks_untrustworthy_crops_unavailable(
    tmp_path: Path,
) -> None:
    """Session details retain every photo, while invalid crops remain ineligible as thumbnails
    and are surfaced with no usable `crop` asset."""
    store = _SyncStore(tmp_path, "entry1")
    bad_box = (0.478, 0.208, 0.593, 0.622)
    _insert_event(store, "e1", 100, kind="eat", scene="d/e1-scene.jpg")
    _insert_sample(store, "e1-s1", "e1", 100, body="d/e1-s1-body.jpg")
    store.conn.execute(
        "UPDATE samples SET box_x1=?, box_y1=?, box_x2=?, box_y2=? WHERE uid='e1-s1'", bad_box
    )
    store.conn.commit()

    assert store._thumb_candidates("e1") == []
    detail = store.event_detail("e1")
    assert len(detail["samples"]) == 1
    assert detail["samples"][0]["uid"] == "e1-s1"
    assert detail["samples"][0]["crop"] is None
    assert detail["event"]["sample_count"] == 1
    assert detail["event"]["scene"] == {"id": "d/e1-scene.jpg", "url": "/api/kibble/entry1/media/d/e1-scene.jpg"}
    assert detail["event"]["thumb"] == detail["event"]["scene"]
    store.add_cat("Kitty")
    events, _training_changed = store.label_events(["e1"], "Kitty")
    assert len(events) == 1 and events[0]["uid"] == "e1" and events[0]["identity"] == "reviewed"
    row = store.conn.execute("SELECT cat, reviewed FROM events WHERE uid='e1'").fetchone()
    assert row["cat"] == "Kitty" and row["reviewed"] == 1


def test_event_detail_preserves_crop_status_for_every_photo(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    bad_box = (0.478, 0.208, 0.593, 0.622)
    good_box = (0.0, 0.4, 0.0625, 0.5)  # left-edge box -- rx0 clamps to 0, overlap 1.0
    _insert_event(store, "e1", 100, kind="eat")
    _insert_sample(store, "e1-s1", "e1", 100, body="d/e1-s1-body.jpg")
    _insert_sample(store, "e1-s2", "e1", 110, body="d/e1-s2-body.jpg")
    store.conn.execute("UPDATE samples SET box_x1=?, box_y1=?, box_x2=?, box_y2=? WHERE uid='e1-s1'", bad_box)
    store.conn.execute("UPDATE samples SET box_x1=?, box_y1=?, box_x2=?, box_y2=? WHERE uid='e1-s2'", good_box)
    store.conn.commit()

    candidates = store._thumb_candidates("e1")
    assert [candidate.uid for candidate in candidates] == ["e1-s2"]
    detail = store.event_detail("e1")
    assert [sample["uid"] for sample in detail["samples"]] == ["e1-s1", "e1-s2"]
    assert detail["samples"][0]["crop"] is None
    assert detail["samples"][1]["crop"] is not None
    assert len(detail["samples"][1]["crop"]) == 4


# --- schema migration: `coral_embeddings` table (docs/41-coral-recognition.md) -----------------


def _legacy_v7_db(root: Path) -> None:
    """A pre-Coral database (schema version 7): the full v7 shape (already carries every
    judge_*/description column from the previous migration), plus one real `training` row, to
    prove the coral_embeddings migration never loses existing rows."""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "kibble.db")
    conn.executescript(
        """
        CREATE TABLE events(
            uid TEXT PRIMARY KEY, device_event_id INTEGER, kind TEXT NOT NULL, start INTEGER NOT NULL,
            end INTEGER, open INTEGER NOT NULL, eat_start INTEGER, scene TEXT, before TEXT, after TEXT,
            cat TEXT, identity_status TEXT, confidence REAL, reviewed INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL, parent_uid TEXT, hidden INTEGER NOT NULL DEFAULT 0,
            clip_id TEXT, clip_start_ms INTEGER, clip_end_ms INTEGER,
            judge_present INTEGER, judge_cat TEXT, judge_confidence REAL, judge_multiple INTEGER,
            judge_reason TEXT, judge_model TEXT, judge_at INTEGER, judge_evidence TEXT
        );
        CREATE TABLE cats(
            name TEXT PRIMARY KEY, color INTEGER NOT NULL, created INTEGER NOT NULL,
            avatar_asset TEXT, avatar_updated INTEGER, description TEXT
        );
        CREATE TABLE training(
            uid TEXT PRIMARY KEY, cat TEXT NOT NULL, source TEXT NOT NULL, created INTEGER NOT NULL,
            body TEXT, face TEXT, face_emb BLOB, body_feat BLOB, face_feat BLOB, mode TEXT, confidence REAL
        );
        CREATE TABLE samples(
            uid TEXT PRIMARY KEY, event_uid TEXT NOT NULL, t INTEGER NOT NULL, body TEXT, face TEXT,
            face_emb BLOB, body_feat BLOB, face_feat BLOB, mode TEXT, guess TEXT, guess_confidence REAL,
            box_x1 REAL, box_y1 REAL, box_x2 REAL, box_y2 REAL, score REAL, review TEXT
        );
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO cats(name, color, created) VALUES ('Kitty', 0, 100);
        INSERT INTO training(uid, cat, source, created, body)
            VALUES ('t1', 'Kitty', 'label', 100, 'training/kitty/t1-body.jpg');
        INSERT INTO meta(key, value) VALUES ('schema_version', '7');
        """
    )
    conn.commit()
    conn.close()


def test_migrating_a_pre_coral_database_adds_the_embeddings_table_without_losing_rows(
    tmp_path: Path,
) -> None:
    _legacy_v7_db(tmp_path)

    store = _SyncStore(tmp_path, "entry1")

    row = store.conn.execute("SELECT cat FROM training WHERE uid='t1'").fetchone()
    assert row["cat"] == "Kitty"  # the pre-existing row survives
    assert store.conn.execute("SELECT COUNT(*) AS n FROM coral_embeddings").fetchone()["n"] == 0
    version = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)


def test_coral_embeddings_migration_is_idempotent_across_repeated_opens(tmp_path: Path) -> None:
    _legacy_v7_db(tmp_path)
    store1 = _SyncStore(tmp_path, "entry1")
    store1.set_coral_embedding("training", "t1", "body", "model-a", b"\x00\x01\x02\x03")
    store1.close()

    store2 = _SyncStore(tmp_path, "entry1")  # re-opens the same file; `_migrate` reruns
    assert store2.coral_embedding("training", "t1", "body", "model-a") == b"\x00\x01\x02\x03"
    version = store2.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)


# --- coral embedding cache: write-once, independently keyed ------------------------------------


def test_set_coral_embedding_a_second_write_for_the_same_key_is_dropped_not_overwritten(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.set_coral_embedding("training", "t1", "body", "model-a", b"first")

    store.set_coral_embedding("training", "t1", "body", "model-a", b"second")

    assert store.coral_embedding("training", "t1", "body", "model-a") == b"first"


def test_coral_embedding_is_keyed_independently_by_row_kind_crop_kind_and_model(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.set_coral_embedding("training", "t1", "body", "model-a", b"body-a")
    store.set_coral_embedding("training", "t1", "face", "model-a", b"face-a")
    store.set_coral_embedding("training", "t1", "body", "model-b", b"body-b")
    store.set_coral_embedding("sample", "t1", "body", "model-a", b"sample-body-a")

    assert store.coral_embedding("training", "t1", "body", "model-a") == b"body-a"
    assert store.coral_embedding("training", "t1", "face", "model-a") == b"face-a"
    assert store.coral_embedding("training", "t1", "body", "model-b") == b"body-b"
    assert store.coral_embedding("sample", "t1", "body", "model-a") == b"sample-body-a"
    assert store.coral_embedding("training", "t1", "body", "model-c") is None


# --- training_rows_needing_coral: what the backfill/rebuild "ensure embedded" pass reads --------


def test_training_rows_needing_coral_returns_only_rows_missing_that_crops_embedding(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    feat = identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")
    store.add_upload_training(cat="Kitty", uid="t1", data=b"already-embedded", features=feat)
    store.add_upload_training(cat="Kitty", uid="t2", data=b"still-needs-it", features=feat)
    store.set_coral_embedding("training", "t1", "body", "model-a", b"cached")

    rows = store.training_rows_needing_coral("model-a", "model-b", limit=10)

    assert [r["uid"] for r in rows] == ["t2"]
    assert rows[0]["body_bytes"] == b"still-needs-it"
    assert rows[0]["face_bytes"] is None  # upload rows never have a face crop


def test_training_rows_needing_coral_is_newest_first_and_respects_the_limit(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    feat = identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")
    for uid, created in (("t1", 100), ("t2", 200), ("t3", 300)):
        store.add_upload_training(cat="Kitty", uid=uid, data=uid.encode(), features=feat)
        store.conn.execute("UPDATE training SET created=? WHERE uid=?", (created, uid))
    store.conn.commit()

    rows = store.training_rows_needing_coral("model-a", "model-b", limit=2)

    assert [r["uid"] for r in rows] == ["t3", "t2"]


def test_training_rows_needing_coral_skips_a_label_row_with_an_untrustworthy_source_crop(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    bad_box = (0.478, 0.208, 0.593, 0.622)  # same fixture `all_training_features`'s own test uses
    _insert_event(store, "e1", 100, kind="eat")
    body_asset = store.write_media(_utc_date(100), "e1-s1-body.jpg", b"body-bytes")
    _insert_sample(store, "e1-s1", "e1", 100, body=body_asset)
    store.conn.execute(
        "UPDATE samples SET box_x1=?, box_y1=?, box_x2=?, box_y2=? WHERE uid='e1-s1'", bad_box
    )
    store.conn.commit()
    store.add_cat("Kitty")
    assert store.add_auto_training(cat="Kitty", sample_uid="e1-s1", confidence=0.95) is True

    rows = store.training_rows_needing_coral("model-a", "model-b", limit=10)

    assert rows == [], "a training row copied from an untrustworthy crop must never be embedded"


# --- samples_needing_coral / samples_needing_coral_for_event ------------------------------------


def test_samples_needing_coral_only_considers_samples_of_an_unreviewed_event(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")  # reviewed=0 by default
    _insert_event(store, "e2", 200, kind="eat")
    store.conn.execute("UPDATE events SET reviewed=1 WHERE uid='e2'")
    body1 = store.write_media(_utc_date(100), "e1-s1-body.jpg", b"needs-embedding")
    body2 = store.write_media(_utc_date(200), "e2-s1-body.jpg", b"frozen-reviewed-event")
    _insert_sample(store, "e1-s1", "e1", 100, body=body1)
    _insert_sample(store, "e2-s1", "e2", 200, body=body2)
    store.conn.commit()

    rows = store.samples_needing_coral("model-a", "model-b", limit=10)

    assert [r["uid"] for r in rows] == ["e1-s1"]
    assert rows[0]["body_bytes"] == b"needs-embedding"


def test_samples_needing_coral_for_event_ignores_review_status_its_own_uid_already_scopes_it(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")
    store.conn.execute("UPDATE events SET reviewed=1 WHERE uid='e1'")
    body = store.write_media(_utc_date(100), "e1-s1-body.jpg", b"classify-time-catchup")
    _insert_sample(store, "e1-s1", "e1", 100, body=body)
    store.conn.commit()

    rows = store.samples_needing_coral_for_event("e1", "model-a", "model-b")

    assert [r["uid"] for r in rows] == ["e1-s1"]
    assert rows[0]["body_bytes"] == b"classify-time-catchup"


def test_samples_needing_coral_for_event_excludes_a_crop_already_cached(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")
    body = store.write_media(_utc_date(100), "e1-s1-body.jpg", b"data")
    _insert_sample(store, "e1-s1", "e1", 100, body=body)
    store.conn.commit()
    store.set_coral_embedding("sample", "e1-s1", "body", "model-a", b"already-cached")

    rows = store.samples_needing_coral_for_event("e1", "model-a", "model-b")

    assert rows == []


# --- all_training_coral_features / coral_features_for_event: cache -> CoralFeatures -------------


def test_all_training_coral_features_reads_cached_embeddings_into_features(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    feat = identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")
    store.add_upload_training(cat="Kitty", uid="t1", data=b"photo", features=feat)
    vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    store.set_coral_embedding("training", "t1", "body", "model-a", identity.pack(vec))

    pairs = store.all_training_coral_features("model-a", "model-b")

    assert len(pairs) == 1
    cat, coral_feat = pairs[0]
    assert cat == "Kitty"
    np.testing.assert_allclose(coral_feat.body_emb, vec)
    assert coral_feat.face_emb is None


def test_all_training_coral_features_omits_a_row_with_neither_crop_embedded_yet(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    feat = identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")
    store.add_upload_training(cat="Kitty", uid="t1", data=b"photo", features=feat)

    pairs = store.all_training_coral_features("model-a", "model-b")

    assert pairs == []


def test_coral_features_for_event_reads_cached_embeddings_per_sample_in_capture_order(
    tmp_path: Path,
) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")
    body1 = store.write_media(_utc_date(100), "e1-s1-body.jpg", b"s1")
    body2 = store.write_media(_utc_date(100), "e1-s2-body.jpg", b"s2")
    _insert_sample(store, "e1-s1", "e1", 100, body=body1)
    _insert_sample(store, "e1-s2", "e1", 110, body=body2)
    store.conn.commit()
    vec1 = np.array([1.0, 0.0], dtype=np.float32)
    store.set_coral_embedding("sample", "e1-s1", "body", "model-a", identity.pack(vec1))
    # e1-s2 is left un-embedded on purpose -- proves a missing embedding is `None`, not skipped.

    feats = store.coral_features_for_event("e1", "model-a", "model-b")

    assert len(feats) == 2
    np.testing.assert_allclose(feats[0].body_emb, vec1)
    assert feats[1].body_emb is None


# --- coral embedding cache cleanup: deleted training/sample rows never leave orphans ------------


def test_training_remove_also_deletes_its_cached_coral_embeddings(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    feat = identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")
    store.add_upload_training(cat="Kitty", uid="t1", data=b"photo", features=feat)
    store.set_coral_embedding("training", "t1", "body", "model-a", b"cached")

    assert store.training_remove(["t1"]) == 1

    assert store.coral_embedding("training", "t1", "body", "model-a") is None


def test_clear_training_also_deletes_cached_coral_embeddings(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    feat = identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")
    store.add_upload_training(cat="Kitty", uid="t1", data=b"photo", features=feat)
    store.set_coral_embedding("training", "t1", "body", "model-a", b"cached")

    store.clear_training("Kitty", keep_uploads=False)

    assert store.coral_embedding("training", "t1", "body", "model-a") is None


def test_relabelling_a_training_row_to_a_different_cat_keeps_its_cached_embedding(
    tmp_path: Path,
) -> None:
    """A re-label MOVES the row's files to the new cat's directory (`_upsert_training_row`) but
    never deletes the row -- the cached embedding, a property of the crop's own pixels, must
    survive the move untouched."""
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    store.add_cat("Pancake")
    _insert_event(store, "e1", 100, kind="eat")
    body = store.write_media(_utc_date(100), "e1-s1-body.jpg", b"photo")
    _insert_sample(store, "e1-s1", "e1", 100, body=body)
    store.conn.commit()
    store.reconcile_event_training(["e1"], "Kitty")
    store.set_coral_embedding("training", "e1-s1-train", "body", "model-a", b"cached")

    store.reconcile_event_training(["e1"], "Pancake")

    assert store.coral_embedding("training", "e1-s1-train", "body", "model-a") == b"cached"
    row = store.conn.execute("SELECT cat FROM training WHERE uid='e1-s1-train'").fetchone()
    assert row["cat"] == "Pancake"


def test_deleting_an_event_also_deletes_its_samples_cached_coral_embeddings(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat")
    body = store.write_media(_utc_date(100), "e1-s1-body.jpg", b"photo")
    _insert_sample(store, "e1-s1", "e1", 100, body=body)
    store.conn.commit()
    store.set_coral_embedding("sample", "e1-s1", "body", "model-a", b"cached")

    store._delete_event("e1")

    assert store.coral_embedding("sample", "e1-s1", "body", "model-a") is None


# --- session parts and v9 to v10 migration ----------------------------------------------------


def test_legacy_sidless_session_keeps_the_chronological_split_fallback(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    _insert_event(store, "e1", 100, kind="eat", end=140, open_=False)
    for index, (t, cat) in enumerate(((100, "Kitty"), (104, "Kitty"), (130, "Pancake"), (134, "Pancake"))):
        uid = f"e1-s{index}"
        _insert_sample(store, uid, "e1", t)
        store.conn.execute(
            "UPDATE samples SET guess=?, guess_confidence=0.95 WHERE uid=?", (cat, uid)
        )
    store.conn.commit()

    assert store.replan_session("e1", _subject_scores()) is True
    children = store.conn.execute("SELECT uid, cat FROM events WHERE parent_uid='e1' ORDER BY uid").fetchall()
    assert [row["uid"] for row in children] == ["e1-seg0", "e1-seg1"]
    assert [row["cat"] for row in children] == ["Kitty", "Pancake"]
    detail = store.event_detail("e1")
    assert detail is not None and len(detail["samples"]) == 4
    assert {sample["uid"] for sample in detail["samples"]} == {f"e1-s{i}" for i in range(4)}


def test_identity_summary_counts_cat_parts_not_the_hidden_session_parent(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    start = int(time.time()) - 3600
    _insert_cooccurring_session(store, start=start, end=start + 50)
    assert store.replan_session("e1", _subject_scores()) is True
    store.conn.execute("UPDATE events SET cat='Kitty', kind='eat' WHERE uid='e1'")
    store.conn.commit()

    summary = store.identity_summary()

    assert summary.cats["Kitty"].last_meal == start
    assert summary.cats["Kitty"].recent_meals == (start,)
    assert summary.cats["Pancake"].last_meal is None


def test_v10_migration_recovers_all_photos_and_keeps_reviewed_subject_and_photo_labels(tmp_path: Path) -> None:
    store = _SyncStore(tmp_path, "entry1")
    store.add_cat("Kitty")
    store.add_cat("Pancake")
    start = int(time.time()) - 3600
    _insert_event(store, "e2060", start, kind="eat", end=start + 60, eat_start=start + 4, open_=False)
    # The real v1 rows: the reviewed Kitty lane was stored as a VISIT although the session was a
    # 3.5 minute meal (per-subject bowl timing on a fragmented track); v2 must restore the meal.
    legacy_parts = (
        ("e2060-sub1", "visit", "Kitty", "reviewed", 1),
        ("e2060-sub2", "visit", None, "not_a_cat", 1),
        ("e2060-sub3", "visit", None, "unknown", 1),
    )
    for uid, kind, cat, identity_status, reviewed in legacy_parts:
        store.conn.execute(
            "INSERT INTO events(uid, device_event_id, kind, start, end, open, eat_start, cat, "
            "identity_status, reviewed, updated, parent_uid, hidden) "
            "VALUES (?, 2060, ?, ?, ?, 0, NULL, ?, ?, ?, ?, 'e2060', 0)",
            (uid, kind, start, start + 60, cat, identity_status, reviewed, start),
        )
    _insert_sample(store, "e2060-s1", "e2060-sub2", start + 1)
    _insert_sample(store, "e2060-s1-o1", "e2060-sub1", start + 2)
    store.conn.execute("UPDATE samples SET review='Kitty' WHERE uid='e2060-s1-o1'")
    _insert_sample(store, "e2060-s1-unknown", "e2060-sub3", start + 3)
    for index in range(3, 42):
        _insert_sample(store, f"e2060-s{index}", "e2060", start + index)
    store.conn.commit()
    store.close()

    legacy = sqlite3.connect(tmp_path / "kibble.db")
    legacy.execute("ALTER TABLE samples DROP COLUMN review_src")
    legacy.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
    legacy.commit()
    legacy.close()

    migrated = _SyncStore(tmp_path, "entry1")

    parent = migrated.conn.execute("SELECT * FROM events WHERE uid='e2060'").fetchone()
    assert (parent["hidden"], parent["cat"], parent["kind"], parent["reviewed"]) == (
        0, "Kitty", "eat", 1
    )
    assert migrated.conn.execute("SELECT COUNT(*) FROM samples WHERE event_uid='e2060'").fetchone()[0] == 42
    assert migrated.conn.execute("SELECT COUNT(*) FROM events WHERE parent_uid='e2060'").fetchone()[0] == 0
    reviews = {
        row["uid"]: (row["review"], row["review_src"])
        for row in migrated.conn.execute(
            "SELECT uid, review, review_src FROM samples WHERE uid IN "
            "('e2060-s1', 'e2060-s1-o1', 'e2060-s1-unknown')"
        )
    }
    assert reviews == {
        "e2060-s1": ("not_a_cat", "photo"),
        "e2060-s1-o1": ("Kitty", None),
        "e2060-s1-unknown": ("unknown", "photo"),
    }
    detail = migrated.event_detail("e2060")
    assert detail is not None and detail["event"]["uid"] == "e2060"
    assert len(detail["samples"]) == 42
    assert migrated.identity_summary().cats["Kitty"].last_meal == start
    version = migrated.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert version["value"] == str(SCHEMA_VERSION)
