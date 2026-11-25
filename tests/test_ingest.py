"""`ingest.py`'s invariants its own module docstring calls out by name (docs/36-ai-pipeline.md):

- An evidence asset is acknowledged to the feeder (`DELETE /events/<name>`) only once it is
  durably written locally first -- an ack-before-write ordering would let a crash between the
  two lose the asset forever, since the feeder has already dropped it from its own spool.
- A human review always wins: `IdentityEngine.async_classify_event` never overwrites an already
  reviewed event's classification, no matter what the current model would say.
- The learning job's side of `IdentityEngine`: one build at a time (`_build_lock`), the quadratic
  histogram leave-one-out only when CoralHub is not configured, `built_gen` recorded only once
  every stage succeeded, and a recheck sweep that skips one failing event but gives up after
  `RECLASSIFY_MAX_CONSECUTIVE_FAILURES` of them in a row.
- Ingesting never rebuilds or rechecks anything inline: the store's generation counter is what
  wakes the learning job, so a poll stays as short as its own HTTP round trips.

`ingest.py` has zero `homeassistant` import at module scope (only under `TYPE_CHECKING`), so
this runs under either Python this project has. The reviewed-event guard is proved against a
real `_SyncStore` (sqlite3, no mock of the guard itself) so the assertion is that the actual
`WHERE reviewed=0` clause holds, not that a hand-rolled fake remembers to enforce it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from kibble import coral_identity, identity
from kibble.api import DetectionEvent, Sample
from kibble.ingest import RECLASSIFY_MAX_CONSECUTIVE_FAILURES, IdentityEngine, Ingestor, event_uid
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

    async def async_set_event_classification(self, uid: str, **kwargs) -> bool:
        return self._sync.set_event_classification(uid, **kwargs)

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

    # What the learning job's recheck sweep and a whole poll (`async_ingest`) read and write.

    async def async_all_training_features(self):
        return self._sync.all_training_features()

    async def async_events_for_reclassify(self, cutoff: int):
        return self._sync.events_for_reclassify(cutoff)

    async def async_features_by_sid_for_family(self, uid: str):
        return self._sync.features_by_sid_for_family(uid)

    async def async_close_orphaned_open(self, live_open_uids) -> int:
        return self._sync.close_orphaned_open(live_open_uids)

    async def async_insert_sample(self, **kwargs) -> None:
        self._sync.insert_sample(**kwargs)


# --- human review always wins -------------------------------------------------------------------


class _FakeModel:
    def __init__(self, verdict: identity.Verdict) -> None:
        self._verdict = verdict

    def classify(self, samples: list[identity.Features]) -> identity.Verdict:
        return self._verdict

    def class_distribution(self, samples: list[identity.Features]):
        return None  # nothing to say per subject, so the session planner gets no scores


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

    changed = await engine.async_classify_event("e1")

    row = sync.conn.execute("SELECT cat, identity_status, reviewed FROM events WHERE uid='e1'").fetchone()
    assert row["cat"] == "Kitty"
    assert row["identity_status"] == "reviewed"
    assert row["reviewed"] == 1
    assert changed is False  # nothing was written, so no name changed


async def test_an_unreviewed_events_classification_is_written_normally() -> None:
    """Sanity companion: the guard above is specific to an already-reviewed event, not a
    blanket refusal to ever write -- an ordinary unreviewed event must still get classified."""
    calls: list[tuple] = []

    class _RecordingAsyncStore:
        async def async_features_for_event(self, event_uid: str):
            return [identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day")]

        async def async_sample_uids_ordered(self, event_uid: str):
            return ["e1-s1"]

        async def async_set_event_classification(self, uid, **kwargs) -> bool:
            calls.append((uid, kwargs))
            return True

    engine = IdentityEngine(_FakeHass(), _RecordingAsyncStore())
    engine._model = _FakeModel(identity.Verdict(label="Pancake", confidence=0.95, per_sample=[("Pancake", 0.95)]))

    changed = await engine.async_classify_event("e1")

    assert changed is True  # the store reported a changed name and the engine passes that on
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

        async def async_set_event_classification(self, uid, **kwargs) -> bool:
            calls.append((uid, kwargs))
            return True

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

        async def async_set_event_classification(self, uid, **kwargs) -> bool:
            calls.append((uid, kwargs))
            return True

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

        async def async_set_event_classification(self, uid, **kwargs) -> bool:
            calls.append((uid, kwargs))
            return True

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
    def __init__(self, training: list[tuple[str, identity.Features]] | None = None) -> None:
        self._training = training or []

    async def async_all_training_features(self):
        return self._training


class _RebuildCoral:
    """What `IdentityEngine` reads of a `CoralRecognizer` while rebuilding: the rebuild itself,
    why the last one failed (`rebuild_error`, `None` = healthy), its per-stage timings, its running
    embedding totals, and -- once a model exists -- its own leave-one-out figures."""

    def __init__(self, *, rebuild_error: str | None = None, timings: dict[str, int] | None = None) -> None:
        self.rebuild_error = rebuild_error
        self.timings = timings or {}
        self.stats = coral_identity.EmbedStats()
        self.available = False
        self.loo: dict[str, float | None] = {}
        self.rebuilds = 0

    async def async_rebuild(self) -> bool:
        self.rebuilds += 1
        return self.rebuild_error is None

    def loo_accuracy(self, cat: str) -> float | None:
        return self.loo.get(cat)


class _HangingCoral(_RebuildCoral):
    """A CoralHub that accepts the request and never answers."""

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

    class _Coral(_RebuildCoral):
        async def async_rebuild(self) -> bool:
            seen.append(engine._model is not None)
            return await super().async_rebuild()

    engine = IdentityEngine(_FakeHass(), _RebuildStore(), coral=_Coral())

    await engine.async_rebuild()

    assert seen == [True]


async def test_rebuilding_without_coralhub_configured_builds_just_the_local_model() -> None:
    engine = IdentityEngine(_FakeHass(), _RebuildStore())

    assert await engine.async_rebuild() is None
    assert await engine.async_rebuild_coral() is None  # nothing configured: harmless, never raises

    assert engine._model is not None


def _kitty_gallery() -> list[tuple[str, identity.Features]]:
    """Exactly `identity.MIN_LOO_SAMPLES` Kitty photos: the fewest a leave-one-out figure exists for."""
    return [
        (
            "Kitty",
            identity.Features(
                face_emb=None, body_feat=np.array([0.9, 0.1 + i / 100], dtype=np.float32), face_feat=None, mode="day"
            ),
        )
        for i in range(identity.MIN_LOO_SAMPLES)
    ]


async def test_the_histogram_leave_one_out_is_only_worked_out_when_coralhub_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It costs time quadratic in the gallery and is only ever read while the histogram model is
    the one answering; with CoralHub configured that model is merely the fallback."""
    runs: list[str] = []
    real = identity.Model.loo_accuracy

    def _counting(model: identity.Model) -> dict[str, float | None]:
        runs.append("loo")
        return real(model)

    monkeypatch.setattr(identity.Model, "loo_accuracy", _counting)

    local_only = IdentityEngine(_FakeHass(), _RebuildStore(_kitty_gallery()))
    await local_only.async_rebuild()
    assert runs == ["loo"]
    assert local_only.loo_accuracy("Kitty") is not None

    runs.clear()
    coral = _RebuildCoral()
    with_coral = IdentityEngine(_FakeHass(), _RebuildStore(_kitty_gallery()), coral=coral)
    await with_coral.async_rebuild()
    await with_coral.async_rebuild_histogram()
    assert runs == []
    assert with_coral._model is not None  # the fallback model itself is still built, just not scored
    assert with_coral.loo_accuracy("Kitty") is None

    # Once CoralHub has a model, the accuracy figure is the one CoralHub's own model worked out.
    coral.available = True
    coral.loo = {"Kitty": 0.75}
    assert with_coral.loo_accuracy("Kitty") == 0.75


async def test_built_gen_is_recorded_only_when_every_stage_succeeded() -> None:
    """`learning.py` skips a pass whose generation matches `built_gen`, so claiming a generation
    CoralHub never finished would leave the recognizer stale until the next training change."""
    local = IdentityEngine(_FakeHass(), _RebuildStore())
    assert local.built_gen is None
    assert await local.async_rebuild(gen=3) is None
    assert local.built_gen == 3

    coral = _RebuildCoral()
    engine = IdentityEngine(_FakeHass(), _RebuildStore(), coral=coral)
    assert await engine.async_rebuild(gen=4) is None
    assert engine.built_gen == 4

    coral.rebuild_error = "CoralHub did not answer its health check"
    assert await engine.async_rebuild(gen=5) == "CoralHub did not answer its health check"
    assert engine.built_gen == 4, "the failed pass must be retried, not recorded as built"

    coral.rebuild_error = None
    assert await engine.async_rebuild(gen=5) is None
    assert engine.built_gen == 5


async def test_a_failing_histogram_build_raises_leaves_built_gen_alone_and_frees_the_build_lock() -> None:
    class _FlakyStore(_RebuildStore):
        broken = False

        async def async_all_training_features(self):
            if self.broken:
                raise RuntimeError("the store is closing")
            return await super().async_all_training_features()

    store = _FlakyStore()
    engine = IdentityEngine(_FakeHass(), store)
    await engine.async_rebuild(gen=1)

    store.broken = True
    with pytest.raises(RuntimeError, match="the store is closing"):
        await engine.async_rebuild(gen=2)
    assert engine.built_gen == 1

    store.broken = False
    async with asyncio.timeout(2):  # a lock left held by the failed build would hang right here
        assert await engine.async_rebuild(gen=2) is None
    assert engine.built_gen == 2


async def test_the_two_start_up_halves_never_claim_a_generation_was_built() -> None:
    coral = _RebuildCoral()
    engine = IdentityEngine(_FakeHass(), _RebuildStore(), coral=coral)

    await engine.async_rebuild_histogram()
    assert await engine.async_rebuild_coral() is None

    assert coral.rebuilds == 1
    assert engine.built_gen is None


async def test_a_coral_only_rebuild_reports_why_coralhub_could_not_be_reached() -> None:
    coral = _RebuildCoral(rebuild_error="CoralHub did not answer its health check")
    engine = IdentityEngine(_FakeHass(), _RebuildStore(), coral=coral)

    assert await engine.async_rebuild_coral() == "CoralHub did not answer its health check"

    coral.rebuild_error = None
    assert await engine.async_rebuild_coral() is None


async def test_concurrent_rebuilds_run_one_after_the_other() -> None:
    """The learning job, start-up and the Coral backfill can all ask for a build at the same
    moment; two interleaved builds would each read the gallery at a different instant."""
    gate = asyncio.Event()

    class _GatedStore(_RebuildStore):
        reads = 0
        active = 0
        most_active = 0

        async def async_all_training_features(self):
            self.reads += 1
            self.active += 1
            self.most_active = max(self.most_active, self.active)
            try:
                await gate.wait()
            finally:
                self.active -= 1
            return []

    store = _GatedStore()
    engine = IdentityEngine(_FakeHass(), store)

    async with asyncio.timeout(5):
        first = asyncio.create_task(engine.async_rebuild(gen=1))
        second = asyncio.create_task(engine.async_rebuild(gen=2))
        third = asyncio.create_task(engine.async_rebuild_histogram())
        for _ in range(5):
            await asyncio.sleep(0)
        assert store.reads == 1, "the others must wait for the first build to finish"

        gate.set()
        await asyncio.gather(first, second, third)

    assert store.reads == 3
    assert store.most_active == 1
    assert engine.built_gen == 2, "builds finish in the order they were asked for"


async def test_a_coral_only_rebuild_waits_for_a_full_rebuild_that_is_already_running() -> None:
    log: list[str] = []
    gate = asyncio.Event()

    class _GatedCoral(_RebuildCoral):
        async def async_rebuild(self) -> bool:
            log.append("start")
            await gate.wait()
            log.append("end")
            return True

    engine = IdentityEngine(_FakeHass(), _RebuildStore(), coral=_GatedCoral())

    async with asyncio.timeout(5):
        full = asyncio.create_task(engine.async_rebuild(gen=1))
        while not log:
            await asyncio.sleep(0)
        coral_only = asyncio.create_task(engine.async_rebuild_coral())
        for _ in range(5):
            await asyncio.sleep(0)
        assert log == ["start"], "the second build must not start while the first holds the lock"

        gate.set()
        await asyncio.gather(full, coral_only)

    assert log == ["start", "end", "start", "end"]


async def test_a_rebuild_reports_how_long_each_stage_took() -> None:
    coral = _RebuildCoral(timings={"health_ms": 12, "embed_ms": 340, "build_ms": 7})
    engine = IdentityEngine(_FakeHass(), _RebuildStore(), coral=coral)

    await engine.async_rebuild(gen=1)

    assert set(engine.timings) == {"histogram_ms", "coral_health_ms", "coral_embed_ms", "coral_build_ms"}
    assert engine.timings["coral_embed_ms"] == 340
    assert engine.timings["histogram_ms"] >= 0

    local = IdentityEngine(_FakeHass(), _RebuildStore())
    await local.async_rebuild()
    assert set(local.timings) == {"histogram_ms"}

    coral.timings = {"health_ms": 3}  # the next pass replaces the figures rather than adding to them
    await engine.async_rebuild(gen=2)
    assert set(engine.timings) == {"histogram_ms", "coral_health_ms"}


def test_embed_stats_are_the_recognizers_own_totals_or_none_without_coralhub() -> None:
    assert IdentityEngine(_FakeHass(), object()).embed_stats is None

    coral = _RebuildCoral()
    assert IdentityEngine(_FakeHass(), object(), coral=coral).embed_stats is coral.stats


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

    async def async_classify_event(self, uid: str) -> bool:
        return False

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


# --- classifying one event reports whether its NAME changed -------------------------------------


def _seed_event(sync: _SyncStore, uid: str, start: int) -> None:
    """A closed visit with one photo and no classification yet."""
    sync.upsert_event(
        uid=uid, device_event_id=1, kind="visit", start=start, end=start + 10, open_=False,
        eat_start=None, scene=None, before=None, after=None,
    )
    sync.insert_sample(
        uid=f"{uid}-s1", event_uid=uid, t=start, body="b.jpg", face=None,
        features=identity.Features(face_emb=None, body_feat=None, face_feat=None, mode="day"),
        box=None, score=0.9,
    )


def _verdict(label: str | None, confidence: float | None) -> identity.Verdict:
    return identity.Verdict(label=label, confidence=confidence, per_sample=[(label, confidence)])


async def test_classifying_an_event_reports_true_only_when_its_cat_name_changed(tmp_path: Path) -> None:
    sync = _SyncStore(tmp_path, "entry1")
    _seed_event(sync, "e1", 100)
    engine = IdentityEngine(_FakeHass(), _RealAsyncStore(sync))

    def stored() -> tuple[str | None, str, float | None]:
        row = sync.conn.execute("SELECT cat, identity_status, confidence FROM events WHERE uid='e1'").fetchone()
        return row["cat"], row["identity_status"], row["confidence"]

    engine._model = _FakeModel(_verdict("Kitty", 0.9))
    assert await engine.async_classify_event("e1") is True  # nobody -> Kitty
    assert stored() == ("Kitty", "auto", 0.9)

    # The very same answer again: nothing changes, and nothing is even written back.
    sync.conn.execute("UPDATE events SET updated=0 WHERE uid='e1'")
    sync.conn.commit()
    assert await engine.async_classify_event("e1") is False
    assert sync.conn.execute("SELECT updated FROM events WHERE uid='e1'").fetchone()["updated"] == 0

    # A firmer answer for the same cat is saved, but the NAME did not change.
    engine._model = _FakeModel(_verdict("Kitty", 0.97))
    assert await engine.async_classify_event("e1") is False
    assert stored() == ("Kitty", "auto", 0.97)

    # Another cat is a rename, and so is losing the name altogether.
    engine._model = _FakeModel(_verdict("Pancake", 0.9))
    assert await engine.async_classify_event("e1") is True
    assert stored() == ("Pancake", "auto", 0.9)
    engine._model = _FakeModel(_verdict(None, None))
    assert await engine.async_classify_event("e1") is True
    assert stored() == (None, "unknown", None)


async def test_classifying_an_event_with_nothing_to_go_on_changes_nothing(tmp_path: Path) -> None:
    sync = _SyncStore(tmp_path, "entry1")
    _seed_event(sync, "e1", 100)
    sync.upsert_event(
        uid="bare", device_event_id=2, kind="visit", start=200, end=210, open_=False,
        eat_start=None, scene=None, before=None, after=None,
    )  # no photos at all
    engine = IdentityEngine(_FakeHass(), _RealAsyncStore(sync))

    assert await engine.async_classify_event("e1") is False, "no model has been built yet"

    engine._model = _FakeModel(_verdict("Kitty", 0.9))
    assert await engine.async_classify_event("bare") is False, "no photos to look at"
    assert sync.conn.execute("SELECT cat FROM events WHERE uid='bare'").fetchone()["cat"] is None


# --- the recheck sweep the learning job runs after a rebuild ------------------------------------


class _SweepStore:
    def __init__(self, uids: list[str]) -> None:
        self.uids = uids
        self.cutoffs: list[int] = []

    async def async_events_for_reclassify(self, cutoff: int) -> list[str]:
        self.cutoffs.append(cutoff)
        return list(self.uids)


class _ScriptedEngine(IdentityEngine):
    """An `IdentityEngine` whose per-event work is scripted -- which events the model renamed,
    which the session replan moved, which raise -- so the sweep AROUND that work is tested alone."""

    def __init__(
        self,
        uids: list[str],
        *,
        renamed: tuple[str, ...] = (),
        replanned: tuple[str, ...] = (),
        failing: dict[str, Exception] | None = None,
        replan_failing: dict[str, Exception] | None = None,
    ) -> None:
        super().__init__(_FakeHass(), _SweepStore(uids))
        self.renamed = set(renamed)
        self.replanned = set(replanned)
        self.failing = failing or {}
        self.replan_failing = replan_failing or {}
        self.attempted: list[str] = []

    async def async_classify_event(self, uid: str) -> bool:
        self.attempted.append(uid)
        if uid in self.failing:
            raise self.failing[uid]
        return uid in self.renamed

    async def async_replan_session(self, uid: str) -> bool:
        if uid in self.replan_failing:
            raise self.replan_failing[uid]
        return uid in self.replanned


async def test_the_recheck_sweep_reports_progress_as_done_of_total() -> None:
    engine = _ScriptedEngine(["a", "b", "c"])
    seen: list[tuple[int, int]] = []

    result = await engine.async_reclassify_unreviewed(1234, progress=lambda done, total: seen.append((done, total)))

    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert result == {"rechecked": 3, "renamed": 0, "failed": 0}
    assert engine._store.cutoffs == [1234]


async def test_a_recheck_sweep_over_nothing_reports_zero_of_zero() -> None:
    engine = _ScriptedEngine([])
    seen: list[tuple[int, int]] = []

    result = await engine.async_reclassify_unreviewed(0, progress=lambda done, total: seen.append((done, total)))

    assert seen == [(0, 0)]
    assert result == {"rechecked": 0, "renamed": 0, "failed": 0}


async def test_the_recheck_sweep_yields_to_the_event_loop_between_events() -> None:
    """A sweep over thousands of events must not starve the rest of Home Assistant."""
    engine = _ScriptedEngine([f"e{i}" for i in range(20)])
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(_ticker())
    try:
        await engine.async_reclassify_unreviewed(0)
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)

    assert ticks >= 20, "something else got to run between every pair of events"


async def test_one_failing_event_is_logged_and_skipped_and_the_sweep_carries_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="kibble.ingest")
    engine = _ScriptedEngine(["a", "b", "c"], renamed=("a", "c"), failing={"b": RuntimeError("bad row")})
    seen: list[tuple[int, int]] = []

    result = await engine.async_reclassify_unreviewed(0, progress=lambda done, total: seen.append((done, total)))

    assert engine.attempted == ["a", "b", "c"], "the event after the failure is still rechecked"
    assert result == {"rechecked": 2, "renamed": 2, "failed": 1}
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)], "a skipped event still counts as done"
    assert "Could not recheck event b" in caplog.text


async def test_three_failures_in_a_row_stop_the_sweep_and_raise_the_last_error() -> None:
    assert RECLASSIFY_MAX_CONSECUTIVE_FAILURES == 3
    engine = _ScriptedEngine(list("abcde"), failing={uid: RuntimeError(f"boom-{uid}") for uid in "abcde"})

    with pytest.raises(RuntimeError, match="boom-c"):
        await engine.async_reclassify_unreviewed(0)

    assert engine.attempted == ["a", "b", "c"], "d and e must never be tried"


async def test_a_session_replan_that_keeps_failing_stops_the_sweep_the_same_way() -> None:
    engine = _ScriptedEngine(list("abcd"), replan_failing={uid: RuntimeError(f"plan-{uid}") for uid in "abcd"})

    with pytest.raises(RuntimeError, match="plan-c"):
        await engine.async_reclassify_unreviewed(0)

    assert engine.attempted == ["a", "b", "c"]


async def test_failures_with_a_success_between_them_never_stop_the_sweep() -> None:
    bad = RuntimeError("bad row")
    engine = _ScriptedEngine(
        list("abcdefg"), renamed=("c", "g"), failing={"a": bad, "b": bad, "d": bad, "e": bad}
    )

    result = await engine.async_reclassify_unreviewed(0)

    assert engine.attempted == list("abcdefg")
    assert result == {"rechecked": 3, "renamed": 2, "failed": 4}


async def test_the_renamed_count_counts_events_whose_name_or_session_plan_moved() -> None:
    engine = _ScriptedEngine(list("abcd"), renamed=("a", "c"), replanned=("b", "c"))

    result = await engine.async_reclassify_unreviewed(0)

    # a: new name. b: new session plan only. c: both, still ONE event. d: neither.
    assert result == {"rechecked": 4, "renamed": 3, "failed": 0}


async def test_a_recheck_sweep_over_a_real_store_touches_only_unreviewed_events_inside_retention(
    tmp_path: Path,
) -> None:
    sync = _SyncStore(tmp_path, "entry1")
    sync.add_cat("Kitty")
    for uid, start in (("fresh", 1000), ("settled", 1010), ("kept", 1020), ("old", 10)):
        _seed_event(sync, uid, start)
    sync.label_events(["kept"], "Kitty")  # a person's word: never touched
    sync.set_event_classification(
        "settled", cat="Pancake", identity_status="auto", confidence=0.95,
        per_sample_uids=["settled-s1"], per_sample=[("Pancake", 0.95)],
    )  # already carries this very verdict
    engine = IdentityEngine(_FakeHass(), _RealAsyncStore(sync))
    engine._model = _FakeModel(_verdict("Pancake", 0.95))
    seen: list[tuple[int, int]] = []

    result = await engine.async_reclassify_unreviewed(500, progress=lambda done, total: seen.append((done, total)))

    assert result == {"rechecked": 2, "renamed": 1, "failed": 0}
    assert seen == [(0, 2), (1, 2), (2, 2)]
    rows = sync.conn.execute("SELECT uid, cat, identity_status FROM events").fetchall()
    assert {row["uid"]: (row["cat"], row["identity_status"]) for row in rows} == {
        "fresh": ("Pancake", "auto"),
        "settled": ("Pancake", "auto"),
        "kept": ("Kitty", "reviewed"),
        "old": (None, None),  # never classified: outside retention, so left exactly as it was
    }
    # Nothing is left to change, so a second sweep renames nothing.
    assert await engine.async_reclassify_unreviewed(500) == {"rechecked": 2, "renamed": 0, "failed": 0}


# --- a poll never rebuilds or rechecks inline ---------------------------------------------------


class _SpyEngine(_NoopEngine):
    """Records every engine entry point a poll's ingest touches, including the heavy ones it must not."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def async_classify_event(self, uid: str) -> bool:
        self.calls.append("classify")
        return False

    async def async_replan_session(self, uid: str) -> bool:
        self.calls.append("replan")
        return False

    async def async_maybe_auto_learn(self, uid: str) -> bool:
        self.calls.append("auto_learn")
        return True  # it DID add training photos

    async def async_rebuild(self, gen: int | None = None) -> None:
        self.calls.append("rebuild")

    async def async_rebuild_histogram(self) -> None:
        self.calls.append("rebuild_histogram")

    async def async_rebuild_coral(self) -> None:
        self.calls.append("rebuild_coral")

    async def async_reclassify_unreviewed(self, retention_cutoff: int, progress=None) -> dict[str, int]:
        self.calls.append("reclassify")
        return {"rechecked": 0, "renamed": 0, "failed": 0}


def _eat_event_with_one_photo() -> DetectionEvent:
    return DetectionEvent(
        event_id=1, seq=1, ts=1790000000, end=1790000060, open=False, kind="eat", eat_start=1790000000,
        scene=None, samples=(Sample(k=0, t=1790000005, box=None, score=0.9, body="e1-s0-body.jpg", face=None),),
        image=None, image_before=None, image_after=None,
    )


def _spied_ingestor(sync: _SyncStore) -> tuple[Ingestor, _SpyEngine]:
    ingestor = _wired_ingestor(sync, _RecordingClient([]))
    engine = _SpyEngine()
    ingestor._engine = engine
    ingestor.coordinator = SimpleNamespace(auto_learn_enabled=True, bowl_fill_settle_pending=lambda: None)
    return ingestor, engine


async def test_a_poll_never_rebuilds_or_rechecks_inline_even_when_auto_learn_adds_photos(tmp_path: Path) -> None:
    """Auto-learn adding photos only moves the store's generation counter: the learning job --
    not the poll -- rebuilds the models and rechecks the unreviewed events once things go quiet.
    A rebuild or a recheck sweep inside the poll would put seconds of CPU back on the path that
    has to stay as short as its own HTTP round trips."""
    sync = _SyncStore(tmp_path, "entry1")
    ingestor, engine = _spied_ingestor(sync)
    event = _eat_event_with_one_photo()

    await ingestor.async_ingest([event], [])

    uid = event_uid("entry1", event)
    assert sync.existing_sample_uids(uid) == {f"{uid}-s0"}, "the event really was ingested"
    assert engine.calls == ["classify", "replan", "auto_learn"]


async def test_ingesting_one_event_returns_nothing(tmp_path: Path) -> None:
    """It used to say whether the gallery had changed so the poll could rebuild on the spot; the
    store's generation counter carries that now, so there is nothing to return."""
    sync = _SyncStore(tmp_path, "entry1")
    ingestor, engine = _spied_ingestor(sync)

    assert await ingestor._ingest_event(_eat_event_with_one_photo()) is None
    assert "auto_learn" in engine.calls  # auto-learn did report adding photos: still nothing returned
