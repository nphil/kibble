"""`ingest.py`'s two invariants its own module docstring calls out by name (docs/36-ai-pipeline.md):

- An evidence asset is acknowledged to the feeder (`DELETE /events/<name>`) only once it is
  durably written locally first -- an ack-before-write ordering would let a crash between the
  two lose the asset forever, since the feeder has already dropped it from its own spool.
- A human review always wins: `IdentityEngine.async_classify_event` never overwrites an already
  reviewed event's classification, no matter what the current model would say.

`ingest.py` has zero `homeassistant` import at module scope (only under `TYPE_CHECKING`), so
this runs under either Python this project has. The reviewed-event guard is proved against a
real `_SyncStore` (sqlite3, no mock of the guard itself) so the assertion is that the actual
`WHERE reviewed=0` clause holds, not that a hand-rolled fake remembers to enforce it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from kibble import identity
from kibble.api import DetectionEvent
from kibble.ingest import IdentityEngine, Ingestor, event_uid
from kibble.store import _SyncStore, _utc_date, resolve_asset_path


class _FakeHass:
    """Runs an "executor job" inline: `identity.features_from`/`Model.classify` are pure CPU
    functions, so there is no real thread pool worth faking."""

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class _RecordingClient:
    """Records call order into a shared list so ack-after-write can be proven, not assumed."""

    def __init__(self, calls: list[str], body: bytes = b"jpeg-bytes") -> None:
        self._calls = calls
        self._body = body

    async def asset_bytes(self, name: str) -> bytes:
        self._calls.append(f"fetch:{name}")
        return self._body

    async def delete_asset(self, name: str) -> None:
        self._calls.append(f"ack:{name}")


class _RecordingStore:
    """Just enough of `KibbleStore`'s async surface for `_fetch_event_asset`, recording into the
    same shared call-order list `_RecordingClient` uses."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self._written: dict[str, bytes] = {}

    async def async_media_exists(self, date: str, filename: str) -> bool:
        return f"{date}/{filename}" in self._written

    async def async_write_media(self, date: str, filename: str, data: bytes) -> str:
        self._calls.append(f"write:{filename}")
        self._written[f"{date}/{filename}"] = data
        return f"{date}/{filename}"

    def asset_path(self, asset_id: str):
        data = self._written[asset_id]
        return SimpleNamespace(read_bytes=lambda: data)


def _bare_ingestor(client, store) -> Ingestor:
    ingestor = Ingestor.__new__(Ingestor)
    ingestor._hass = _FakeHass()
    ingestor._client = client
    ingestor._store = store
    return ingestor


# --- durable-write-then-ack ordering ------------------------------------------------------------


async def test_a_freshly_fetched_asset_is_written_before_it_is_acknowledged() -> None:
    calls: list[str] = []
    ingestor = _bare_ingestor(_RecordingClient(calls), _RecordingStore(calls))

    asset_id, data = await ingestor._fetch_event_asset("e1-s1-body.jpg", "2026-09-24")

    assert asset_id == "2026-09-24/e1-s1-body.jpg"
    assert data == b"jpeg-bytes"
    assert calls == ["fetch:e1-s1-body.jpg", "write:e1-s1-body.jpg", "ack:e1-s1-body.jpg"]


async def test_an_already_durable_asset_is_read_locally_never_re_fetched_but_still_acknowledged() -> None:
    """A file already on disk (this poll re-processing the same event, or a crash between an
    earlier write and its ack) must never be re-fetched -- but the ack itself always happens
    regardless, since the feeder's own spool copy may still be un-acknowledged."""
    calls: list[str] = []
    store = _RecordingStore(calls)
    store._written["2026-09-24/e1-s1-body.jpg"] = b"already-there"
    ingestor = _bare_ingestor(_RecordingClient(calls), store)

    asset_id, data = await ingestor._fetch_event_asset("e1-s1-body.jpg", "2026-09-24")

    assert asset_id == "2026-09-24/e1-s1-body.jpg"
    assert data == b"already-there"
    assert calls == ["ack:e1-s1-body.jpg"]  # no fetch, no re-write, ack still happens


async def test_a_null_asset_name_is_never_fetched_or_acknowledged() -> None:
    """A sample field the device never populated (`box=None`-shaped fields, a refused spool
    write) must be a clean no-op, not a fetch of a literal `None`/empty name."""
    calls: list[str] = []
    ingestor = _bare_ingestor(_RecordingClient(calls), _RecordingStore(calls))

    asset_id, data = await ingestor._fetch_event_asset(None, "2026-09-24")

    assert (asset_id, data) == (None, None)
    assert calls == []


class _RealAsyncStore:
    """Delegates every call straight to a real `_SyncStore` (sqlite3 + pathlib, no HA needed) --
    exercises the actual persistence/guard logic `ingest.py` depends on, not a hand-rolled mock
    of it. Async in name only: every method just forwards to the synchronous store inline."""

    def __init__(self, sync: _SyncStore) -> None:
        self._sync = sync

    async def async_features_for_event(self, event_uid: str):
        return self._sync.features_for_event(event_uid)

    async def async_sample_uids_ordered(self, event_uid: str):
        return self._sync.sample_uids_ordered(event_uid)

    async def async_set_event_classification(self, uid: str, **kwargs):
        self._sync.set_event_classification(uid, **kwargs)

    # Remaining store calls used by ingest.

    async def async_event_asset_fields(self, uid: str):
        return self._sync.event_asset_fields(uid)

    async def async_replan_session(self, uid: str, scores):
        return self._sync.replan_session(uid, scores)

    async def async_media_exists(self, date: str, filename: str) -> bool:
        return self._sync.media_exists(date, filename)

    async def async_write_media(self, date: str, filename: str, data: bytes) -> str:
        return self._sync.write_media(date, filename, data)

    def asset_path(self, asset_id: str):
        return resolve_asset_path(self._sync.root, asset_id)

    async def async_upsert_event(self, **kwargs) -> None:
        self._sync.upsert_event(**kwargs)

    async def async_invalidate_scene_asset(self, uid: str, old_asset_id: str | None) -> None:
        self._sync.invalidate_scene_asset(uid, old_asset_id)

    async def async_existing_sample_uids(self, uid: str):
        return self._sync.existing_sample_uids(uid)

    async def async_event_training_context(self, uid: str):
        return self._sync.event_training_context(uid)

    async def async_auto_learn_paused(self, cat: str) -> bool:
        return self._sync.auto_learn_paused(cat)

    async def async_sample_guesses(self, event_uid: str):
        return self._sync.sample_guesses(event_uid)

    async def async_training_feats_for_cat(self, cat: str, mode):
        return self._sync.training_feats_for_cat(cat, mode)

    async def async_add_auto_training(self, **kwargs) -> bool:
        return self._sync.add_auto_training(**kwargs)


# --- human review always wins -------------------------------------------------------------------


class _FakeModel:
    def __init__(self, verdict: identity.Verdict) -> None:
        self._verdict = verdict

    def classify(self, samples: list[identity.Features]) -> identity.Verdict:
        return self._verdict


async def test_a_reviewed_events_classification_is_never_overwritten_by_reclassification(
    tmp_path: Path,
) -> None:
    sync = _SyncStore(tmp_path, "entry1")
    sync.add_cat("Kitty")
    sync.upsert_event(
        uid="e1", device_event_id=1, kind="visit", start=100, end=110, open_=False,
        eat_start=None, scene=None, before=None, after=None,
    )
    features = identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")
    sync.insert_sample(
        uid="e1-s1", event_uid="e1", t=100, body="b.jpg", face=None, features=features, box=None, score=0.9
    )
    # A human already reviewed this event as Kitty -- `label_events` is the real path that sets
    # `reviewed=1`, exactly what a live `kibble/label` call does.
    sync.label_events(["e1"], "Kitty")

    store = _RealAsyncStore(sync)
    engine = IdentityEngine(_FakeHass(), store)
    # The "current" model would confidently say Pancake if it ever got the chance to write.
    engine._model = _FakeModel(identity.Verdict(label="Pancake", confidence=0.95, per_sample=[("Pancake", 0.95)]))

    await engine.async_classify_event("e1")

    row = sync.conn.execute("SELECT cat, identity_status, reviewed FROM events WHERE uid='e1'").fetchone()
    assert row["cat"] == "Kitty"
    assert row["identity_status"] == "reviewed"
    assert row["reviewed"] == 1


async def test_an_unreviewed_events_classification_is_written_normally() -> None:
    """Sanity companion: the guard above is specific to an already-reviewed event, not a
    blanket refusal to ever write -- an ordinary unreviewed event must still get classified."""
    calls: list[tuple] = []

    class _RecordingAsyncStore:
        async def async_features_for_event(self, event_uid: str):
            return [identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")]

        async def async_sample_uids_ordered(self, event_uid: str):
            return ["e1-s1"]

        async def async_set_event_classification(self, uid, **kwargs):
            calls.append((uid, kwargs))

    engine = IdentityEngine(_FakeHass(), _RecordingAsyncStore())
    engine._model = _FakeModel(identity.Verdict(label="Pancake", confidence=0.95, per_sample=[("Pancake", 0.95)]))

    await engine.async_classify_event("e1")

    assert len(calls) == 1
    uid, kwargs = calls[0]
    assert uid == "e1"
    assert kwargs["cat"] == "Pancake" and kwargs["identity_status"] == "auto"


# --- Coral-first, histogram-fallback dispatch (docs/41-coral-recognition.md) --------------------


class _FakeCoralRecognizer:
    def __init__(self, verdict: identity.Verdict | None) -> None:
        self._verdict = verdict
        self.asked: list[str] = []

    async def async_classify_event(self, uid: str) -> identity.Verdict | None:
        self.asked.append(uid)
        return self._verdict


async def test_a_real_coral_verdict_is_used_even_when_inconclusive_never_falls_back() -> None:
    calls: list[tuple] = []

    class _RecordingAsyncStore:
        async def async_sample_uids_ordered(self, event_uid: str):
            return ["e1-s1"]

        async def async_set_event_classification(self, uid, **kwargs):
            calls.append((uid, kwargs))

    coral = _FakeCoralRecognizer(identity.Verdict(label=None, confidence=None, per_sample=[(None, None)]))
    engine = IdentityEngine(_FakeHass(), _RecordingAsyncStore(), coral=coral)
    # The histogram model would confidently say Kitty if it ever got asked -- it must not be.
    engine._model = _FakeModel(identity.Verdict(label="Kitty", confidence=0.95, per_sample=[("Kitty", 0.95)]))

    await engine.async_classify_event("e1")

    assert coral.asked == ["e1"]
    assert len(calls) == 1
    uid, kwargs = calls[0]
    assert kwargs["cat"] is None and kwargs["identity_status"] == "unknown", (
        "Coral's own inconclusive answer must be recorded as-is, never overridden by the histogram model"
    )


async def test_falls_back_to_the_histogram_model_when_coral_returns_bare_none() -> None:
    calls: list[tuple] = []

    class _RecordingAsyncStore:
        async def async_features_for_event(self, event_uid: str):
            return [identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")]

        async def async_sample_uids_ordered(self, event_uid: str):
            return ["e1-s1"]

        async def async_set_event_classification(self, uid, **kwargs):
            calls.append((uid, kwargs))

    coral = _FakeCoralRecognizer(None)  # not configured, unreachable, or no model yet
    engine = IdentityEngine(_FakeHass(), _RecordingAsyncStore(), coral=coral)
    engine._model = _FakeModel(identity.Verdict(label="Pancake", confidence=0.9, per_sample=[("Pancake", 0.9)]))

    await engine.async_classify_event("e1")

    assert coral.asked == ["e1"]
    assert len(calls) == 1
    uid, kwargs = calls[0]
    assert kwargs["cat"] == "Pancake" and kwargs["identity_status"] == "auto"


async def test_with_no_coral_recognizer_configured_the_histogram_model_answers_directly() -> None:
    """The default shape every existing (pre-0.27) install and every other test in this file
    already relies on: `coral=None` -- confirms the new parameter's default keeps every caller
    that never mentions Coral working exactly as before."""
    calls: list[tuple] = []

    class _RecordingAsyncStore:
        async def async_features_for_event(self, event_uid: str):
            return [identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")]

        async def async_sample_uids_ordered(self, event_uid: str):
            return ["e1-s1"]

        async def async_set_event_classification(self, uid, **kwargs):
            calls.append((uid, kwargs))

    engine = IdentityEngine(_FakeHass(), _RecordingAsyncStore())
    engine._model = _FakeModel(identity.Verdict(label="Kitty", confidence=0.9, per_sample=[("Kitty", 0.9)]))

    await engine.async_classify_event("e1")

    assert len(calls) == 1 and calls[0][1]["cat"] == "Kitty"


def test_backend_property_reflects_coral_availability() -> None:
    engine_off = IdentityEngine(_FakeHass(), object())
    assert engine_off.backend == "histogram"

    class _Recognizer:
        available = True

    engine_on = IdentityEngine(_FakeHass(), object(), coral=_Recognizer())
    assert engine_on.backend == "coral"


# --- the two halves of a rebuild: the local one must never wait for CoralHub --------------------


class _RebuildStore:
    async def async_all_training_features(self):
        return []


class _HangingCoral:
    """A CoralHub that accepts the request and never answers."""

    def __init__(self) -> None:
        self.rebuilds = 0

    async def async_rebuild(self) -> bool:
        self.rebuilds += 1
        await asyncio.Event().wait()
        return True


async def test_rebuilding_the_local_model_never_waits_for_coralhub() -> None:
    """Start-up builds the histogram model (it must exist before the first poll can ingest) and
    CoralHub's (a network round trip that can take tens of seconds) separately, so a CoralHub
    that never answers cannot hold the first one up."""
    coral = _HangingCoral()
    engine = IdentityEngine(_FakeHass(), _RebuildStore(), coral=coral)

    async with asyncio.timeout(2):  # a regression fails this test instead of hanging the suite
        await engine.async_rebuild_histogram()

    assert engine._model is not None
    assert coral.rebuilds == 0


async def test_a_full_rebuild_builds_the_local_model_before_asking_coralhub() -> None:
    """What every training change still runs: both recognizers, the local one first, so the
    fallback is ready before CoralHub is even asked."""
    seen: list[bool] = []

    class _Coral:
        async def async_rebuild(self) -> bool:
            seen.append(engine._model is not None)
            return True

    engine = IdentityEngine(_FakeHass(), _RebuildStore(), coral=_Coral())

    await engine.async_rebuild()

    assert seen == [True]


async def test_rebuilding_without_coralhub_configured_builds_just_the_local_model() -> None:
    engine = IdentityEngine(_FakeHass(), _RebuildStore())

    await engine.async_rebuild()
    await engine.async_rebuild_coral()  # nothing configured: harmless, never raises

    assert engine._model is not None




# --- feed photos --------------------------------------------------------------------------------


async def test_a_feed_photo_is_archived_through_the_spool_route_and_acknowledged() -> None:
    """LibreFeed spools feed before/after photos like event evidence; the old `/feeds/<name>`
    route no longer exists there. Regression for 2026-09-24: a manual feed's photos sat in the
    feeder spool while HA recorded the feed with no photos at all."""
    calls: list[str] = []
    ingestor = _bare_ingestor(_RecordingClient(calls), _RecordingStore(calls))

    asset_id = await ingestor._archive_feed_asset("flibrefeed-1-before.jpg", "2026-09-25")

    assert asset_id == "2026-09-25/flibrefeed-1-before.jpg"
    assert calls == ["fetch:flibrefeed-1-before.jpg", "write:flibrefeed-1-before.jpg", "ack:flibrefeed-1-before.jpg"]


# --- Contract 4: scene replacement, and scheduling the vision judge -----------------------------


class _RecordingVisionJudge:
    enabled = True

    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule_judge(self, uid: str) -> None:
        self.scheduled.append(uid)


class _RecordingClipLinker:
    enabled = True

    def __init__(self) -> None:
        self.scheduled: list[tuple[str, int, int]] = []

    def schedule_link(self, uid: str, start_ts: int, end_ts: int) -> None:
        self.scheduled.append((uid, start_ts, end_ts))


class _NoopEngine:
    def __init__(self) -> None:
        self.replanned: list[str] = []

    async def async_classify_event(self, uid: str) -> None:
        pass

    async def async_replan_session(self, uid: str) -> bool:
        await self.async_subject_scores(uid)
        return False

    async def async_subject_scores(self, uid: str):
        self.replanned.append(uid)
        return {}
    async def async_maybe_auto_learn(self, uid: str) -> bool:
        return False


def _wired_ingestor(sync: _SyncStore, client, *, vision_judge=None, clip_linker=None) -> Ingestor:
    """A fully-wired `Ingestor` against a real `_SyncStore`, for exercising `_ingest_event`
    end to end -- unlike `_bare_ingestor` (only `_fetch_event_asset`'s own three attributes),
    this needs every attribute `_ingest_event` itself touches."""
    ingestor = Ingestor.__new__(Ingestor)
    ingestor._hass = _FakeHass()
    ingestor._entry_id = "entry1"
    ingestor._client = client
    ingestor._store = _RealAsyncStore(sync)
    ingestor._engine = _NoopEngine()
    ingestor._clip_linker = clip_linker
    ingestor.vision_judge = vision_judge
    ingestor.coordinator = SimpleNamespace(auto_learn_enabled=False)
    return ingestor


async def test_close_only_poll_replans_session_even_without_new_samples(tmp_path: Path) -> None:
    sync = _SyncStore(tmp_path, "entry1")
    ingestor = _wired_ingestor(sync, _RecordingClient([]))
    opened = DetectionEvent(
        event_id=7, seq=1, ts=1790000000, end=None, open=True, kind="visit", eat_start=None,
        scene=None, samples=(), image=None, image_before=None, image_after=None,
    )
    closed = DetectionEvent(
        event_id=7, seq=2, ts=1790000000, end=1790000060, open=False, kind="visit", eat_start=None,
        scene=None, samples=(), image=None, image_before=None, image_after=None,
    )

    await ingestor._ingest_event(opened)
    await ingestor._ingest_event(closed)

    uid = event_uid("entry1", closed)
    assert ingestor._engine.replanned == [uid, uid]


async def test_a_replaced_scene_name_is_refetched_the_old_copy_is_removed_and_the_judge_is_scheduled(
    tmp_path: Path,
) -> None:
    """FeederNight's own scene-naming contract: `librefeedd` rewrites `scene` under a NEW
    filename as a better sample supersedes the last one. HA must refetch on a changed name (not
    just a first-ever one), replace the stored value, remove the superseded file once the new
    one is durably written, and make the closed event eligible for the vision judge again."""
    sync = _SyncStore(tmp_path, "entry1")
    client = _RecordingClient(calls := [], body=b"scene-bytes")
    vision_judge = _RecordingVisionJudge()
    clip_linker = _RecordingClipLinker()
    ingestor = _wired_ingestor(sync, client, vision_judge=vision_judge, clip_linker=clip_linker)

    # First poll: still open, first scene arrives.
    event1 = DetectionEvent(
        event_id=1, seq=1, ts=1790000000, end=None, open=True, kind="eat", eat_start=1790000000,
        scene="e1-scene-1.jpg", samples=(), image=None, image_before=None, image_after=None,
    )
    await ingestor._ingest_event(event1)
    uid = event_uid("entry1", event1)
    row1 = sync.conn.execute("SELECT scene FROM events WHERE uid=?", (uid,)).fetchone()
    assert row1["scene"].endswith("e1-scene-1.jpg")
    assert vision_judge.scheduled == [], "an OPEN event must never be scheduled for judging"

    # Second poll: the event closes and the device now reports a REPLACED scene name.
    event2 = DetectionEvent(
        event_id=1, seq=2, ts=1790000000, end=1790000060, open=False, kind="eat", eat_start=1790000000,
        scene="e1-scene-eat.jpg", samples=(), image=None, image_before=None, image_after=None,
    )
    await ingestor._ingest_event(event2)
    assert "fetch:e1-scene-1.jpg" in calls and "fetch:e1-scene-eat.jpg" in calls  # both were fetched
    row2 = sync.conn.execute("SELECT scene FROM events WHERE uid=?", (uid,)).fetchone()
    assert row2["scene"].endswith("e1-scene-eat.jpg"), "the stored scene must be replaced"
    old_path = resolve_asset_path(sync.root, f"{_utc_date(event1.ts)}/e1-scene-1.jpg")
    assert not old_path.exists(), "the superseded scene file must be removed"
    assert vision_judge.scheduled == [uid], "a newly-closed eat must be scheduled for judging"
    assert clip_linker.scheduled == [(uid, event2.ts, event2.end)]

    # Third poll: the SAME scene name again -- must never be re-fetched, and old-style
    # `e<id>-scene.jpg` rows (nothing to replace) are equally left alone by the same guard.
    fetched_before = list(calls)
    event3 = DetectionEvent(
        event_id=1, seq=3, ts=1790000000, end=1790000060, open=False, kind="eat", eat_start=1790000000,
        scene="e1-scene-eat.jpg", samples=(), image=None, image_before=None, image_after=None,
    )
    await ingestor._ingest_event(event3)
    assert calls == fetched_before, "an unchanged scene name must never be re-fetched"


async def test_a_failed_scene_refetch_keeps_the_old_scene_and_does_not_invalidate_it(
    tmp_path: Path,
) -> None:
    from kibble.api import KibbleError

    class _FailingClient:
        def __init__(self) -> None:
            self.attempted: list[str] = []

        async def asset_bytes(self, name: str) -> bytes:
            self.attempted.append(name)
            raise KibbleError("spool miss")

        async def delete_asset(self, name: str) -> None:
            pass

    sync = _SyncStore(tmp_path, "entry1")
    good_client = _RecordingClient([], body=b"scene-bytes")
    ingestor = _wired_ingestor(sync, good_client)
    event1 = DetectionEvent(
        event_id=1, seq=1, ts=1790000000, end=None, open=True, kind="eat", eat_start=1790000000,
        scene="e1-scene-1.jpg", samples=(), image=None, image_before=None, image_after=None,
    )
    await ingestor._ingest_event(event1)
    uid = event_uid("entry1", event1)

    failing_client = _FailingClient()
    ingestor._client = failing_client
    event2 = DetectionEvent(
        event_id=1, seq=2, ts=1790000000, end=1790000060, open=False, kind="eat", eat_start=1790000000,
        scene="e1-scene-eat.jpg", samples=(), image=None, image_before=None, image_after=None,
    )
    await ingestor._ingest_event(event2)

    assert failing_client.attempted == ["e1-scene-eat.jpg"]
    row = sync.conn.execute("SELECT scene FROM events WHERE uid=?", (uid,)).fetchone()
    assert row["scene"].endswith("e1-scene-1.jpg"), "a failed refetch must keep the old scene"
    old_path = resolve_asset_path(sync.root, f"{_utc_date(event1.ts)}/e1-scene-1.jpg")
    assert old_path.exists(), "nothing was actually superseded -- the old file must survive"


# --- auto-learn exclusions: a judge-sourced identity, and an untrustworthy legacy crop ----------


def _consistent_kitty_samples(sync: _SyncStore, uid: str, *, bad_uid: str | None = None) -> None:
    import numpy as np

    feats = [np.array([0.1, 0.9], dtype=np.float32), np.array([0.5, 0.5], dtype=np.float32),
             np.array([0.9, 0.1], dtype=np.float32)]
    for i, feat in enumerate(feats):
        s_uid = f"{uid}-s{i}"
        sync.insert_sample(
            uid=s_uid, event_uid=uid, t=100 + i, body=f"{s_uid}.jpg", face=None,
            features=identity.Features(face_emb=None, body_feat=feat, face_feat=None, mode="day"),
            box=(0.478, 0.208, 0.593, 0.622) if s_uid == bad_uid else None, score=0.9,
        )
        sync.conn.execute(
            "UPDATE samples SET guess='Kitty', guess_confidence=0.95 WHERE uid=?", (s_uid,)
        )
    sync.conn.commit()


async def test_a_judge_sourced_identity_is_never_fed_back_into_auto_learn(tmp_path: Path) -> None:
    sync = _SyncStore(tmp_path, "entry1")
    sync.upsert_event(
        uid="e1", device_event_id=1, kind="eat", start=100, end=110, open_=False,
        eat_start=None, scene=None, before=None, after=None,
    )
    _consistent_kitty_samples(sync, "e1")
    sync.conn.execute("UPDATE events SET cat='Kitty', identity_status='auto' WHERE uid='e1'")
    sync.conn.commit()
    # The judge is the one that set this identity -- gate 5 (autolearn.is_judge_sourced).
    sync.apply_judge_verdict(
        "e1", model="qwen3-vl-4b", evidence="x", present=True, cat="Kitty", confidence=0.9,
        multiple=False, reason="r", apply_identity=True,
        new_cat="Kitty", new_identity_status="auto", new_confidence=0.9,
    )

    engine = IdentityEngine(_FakeHass(), _RealAsyncStore(sync))
    added = await engine.async_maybe_auto_learn("e1")

    assert added is False
    assert sync.conn.execute("SELECT COUNT(*) AS n FROM training").fetchone()["n"] == 0


async def test_an_untrustworthy_legacy_crop_is_excluded_from_auto_learn_but_its_siblings_still_train(
    tmp_path: Path,
) -> None:
    sync = _SyncStore(tmp_path, "entry1")
    sync.upsert_event(
        uid="e1", device_event_id=1, kind="eat", start=100, end=110, open_=False,
        eat_start=None, scene=None, before=None, after=None,
    )
    _consistent_kitty_samples(sync, "e1", bad_uid="e1-s0")
    sync.conn.execute("UPDATE events SET cat='Kitty', identity_status='auto' WHERE uid='e1'")
    sync.add_cat("Kitty")
    sync.conn.commit()

    engine = IdentityEngine(_FakeHass(), _RealAsyncStore(sync))
    added = await engine.async_maybe_auto_learn("e1")

    assert added is True, "the two trustworthy siblings must still qualify"
    trained_uids = {
        r["uid"] for r in sync.conn.execute("SELECT uid FROM training").fetchall()
    }
    assert trained_uids == {"e1-s1-train", "e1-s2-train"}, (
        "the untrustworthy legacy crop (e1-s0) must never become a training row"
    )
