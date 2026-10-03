"""Turns one poll/push cycle's `events`/`feeds` into the HA store's durable record: fetches
missing evidence, extracts features, classifies tracks, and acknowledges the feeder only after
each asset is durably written -- docs/36-ai-pipeline.md's ingest contract.

Runs off the event loop's critical path: `coordinator.py` schedules `Ingestor.async_ingest` as
a background task, never more than one in flight at a time for the same entry (mirrors the
single-in-flight-task guard the old evidence archiver used) -- see `KibbleCoordinator.
_schedule_ingest`.

Idempotent by construction, not by a separate dirty-check: an event's non-identity columns are
always safely re-upserted, a sample is only ever fetched/archived/inserted once (`store.py`'s
`existing_sample_uids` gate), and an already-durable `before`/`after` bracket photo is never
re-fetched -- so re-processing the same poll twice, or a crash mid-pass, is always safe to just
retry from the top. `scene` is the one deliberate exception: `librefeedd` keeps rewriting it
under a new filename as a better sample supersedes the last one, so a changed filename is
refetched, the superseded copy is cleaned up and that event's vision-judge evidence is
invalidated (`docs/40-vision-judge.md`, Contract 4) -- still idempotent, since an unchanged
filename is still never re-fetched.

After a closed `eat`/`visit` event's evidence lands, `vision_judge.schedule_judge` (when
configured) gets the chance to send it for a second opinion -- see `judge.py`'s own module
docstring for that background arc.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from . import autolearn, coral_identity, crop_geometry, identity, sessions
from .api import DetectionEvent, FeedRecord, KibbleClient, KibbleError, OtherSubjectSample, Sample
from .store import KibbleStore, _utc_date

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import KibbleCoordinator
    from .eating_clips import ClipLinker as EatingClipLinker
    from .judge import VisionJudge

_LOGGER = logging.getLogger(__name__)


def event_uid(entry_id: str, event: DetectionEvent) -> str:
    return f"{entry_id}-e{event.event_id}-{event.ts}"


def feed_uid(entry_id: str, feed: FeedRecord) -> str:
    return f"{entry_id}-f{feed.id}-{feed.ts}"


def sample_uid(evt_uid: str, sample: Sample) -> str:
    return f"{evt_uid}-s{sample.k}"


class IdentityEngine:
    """Owns the current classifier -- the histogram `identity.Model`, always built (the
    fallback every install has from day one), and, when CoralHub is configured
    (`const.CONF_CORALHUB_URL`), a `coral_identity.CoralRecognizer` layered in front of it.
    `async_classify_event` prefers Coral whenever it can actually answer (configured, has a
    model, and CoralHub is not currently known to be down) and falls back to the histogram
    model automatically otherwise -- see `CoralRecognizer.async_classify_event`'s own docstring
    for the exact `None`-vs-`Verdict` contract that decision rests on.

    `async_classify_one` (the upload pipeline's own "does this look like a cat at all" gate,
    `views.py`) stays on the histogram model unconditionally, by design: it only ever checks
    `verdict.label == identity.NOT_A_CAT`, and the 2026-09-25 benchmark found essentially no
    body-crop not-a-cat training data for EITHER recognizer (one photo in the whole corpus) --
    switching this one gate's backend would not change its practical behaviour, so it is left
    alone rather than adding a second Coral entry point for no measurable benefit.

    Both the histogram model and its leave-one-out accuracy are read far more often (every
    `kibble/cats` call) than training actually changes, so both are cached alongside the model
    rather than recomputed per read; the Coral side follows the same pattern
    (`CoralRecognizer.available`/`loo_accuracy`)."""

    def __init__(
        self, hass: HomeAssistant, store: KibbleStore, coral: coral_identity.CoralRecognizer | None = None
    ) -> None:
        self._hass = hass
        self._store = store
        self._coral = coral
        self._model: identity.Model | None = None
        self._loo: dict[str, float | None] = {}

    @property
    def backend(self) -> str:
        """Which recognizer is CURRENTLY preferred -- `"coral"` once a Coral model exists
        (`CoralRecognizer.available`), `"histogram"` otherwise (including "Coral is not
        configured at all"). This is an INTENT-level signal, not a live one: like `coordinator.
        feeder_reachable` vs. `last_update_success` (see that module's own docstring), a
        transient CoralHub outage between rebuilds can leave this reporting `"coral"` while one
        specific `async_classify_event` call actually falls back for that one event -- the two
        are allowed to disagree the same way, for the same reason (a momentary blip should not
        flap a device-wide status back and forth)."""
        return "coral" if (self._coral is not None and self._coral.available) else "histogram"

    @property
    def coral_status(self) -> coral_identity.CoralStatus | None:
        """`None` when Coral is not configured for this entry at all; otherwise
        `CoralRecognizer.status` -- read by `diagnostics.py`."""
        return None if self._coral is None else self._coral.status

    async def async_rebuild(self) -> None:
        """Both recognizers, histogram first, then (if configured) CoralHub's -- what every
        training change runs. Start-up calls the two halves separately instead
        (`__init__.py`'s `_async_finish_setup`), because they cost very different things."""
        await self.async_rebuild_histogram()
        await self.async_rebuild_coral()

    async def async_rebuild_histogram(self) -> None:
        """The local recognizer only: reads the training set and builds `identity.Model` plus
        its leave-one-out accuracy in the executor. No network, but not cheap either --
        `loo_accuracy` builds one fresh model per training row, so it grows with the SQUARE of
        the training set (measured here with synthetic features: 0.3 s at 160 samples, 3 s at
        500, close to a minute at 1000) -- which is why start-up runs it in the entry's
        background task rather than inside `async_setup_entry`."""
        training = await self._store.async_all_training_features()

        def _build() -> tuple[identity.Model, dict[str, float | None]]:
            model = identity.Model(training)
            return model, model.loo_accuracy()

        self._model, self._loo = await self._hass.async_add_executor_job(_build)

    async def async_rebuild_coral(self) -> None:
        """CoralHub's recognizer only (`CoralRecognizer.async_rebuild`), a no-op when Coral is
        not configured. A network round trip: a health check of up to two 10 s attempts, then
        a bounded batch of embedding requests, then its own leave-one-out build -- so it can
        take tens of seconds when CoralHub is slow or off, and must never sit on the setup path.
        """
        if self._coral is not None:
            await self._coral.async_rebuild()

    def loo_accuracy(self, cat: str) -> float | None:
        """`None` both when neither model has ever been built and when `cat` has fewer than
        five training samples -- see `identity.Model.loo_accuracy`'s own "not enough data yet"
        note, shared verbatim by `coral_identity.CoralModel.loo_accuracy`. Reads whichever
        backend `self.backend` currently prefers, so the UI's own per-cat accuracy figure
        always reflects the recognizer actually answering classification calls."""
        if self.backend == "coral":
            assert self._coral is not None
            return self._coral.loo_accuracy(cat)
        return self._loo.get(cat)

    async def _classify(self, samples: list[identity.Features]) -> identity.Verdict | None:
        model = self._model
        if model is None:
            return None
        return await self._hass.async_add_executor_job(model.classify, samples)

    async def async_classify_event(self, uid: str) -> None:
        """Classifies one event's current samples and persists the verdict. Tries Coral first
        (when configured) via `CoralRecognizer.async_classify_event`: a bare `None` there means
        Coral could not answer this event AT ALL right now (not configured, no model yet, or
        CoralHub is known to be down), so the histogram model answers instead; a real
        `identity.Verdict` (even an inconclusive `label=None` one) is Coral's own answer and is
        used as-is, never re-checked against the histogram model. A safe no-op when NEITHER
        model can produce a verdict (no samples yet), or when the event is already reviewed
        (`store.py`'s own `WHERE reviewed=0` guard) -- a human's word always wins and this never
        overwrites it."""
        verdict: identity.Verdict | None = None
        if self._coral is not None:
            verdict = await self._coral.async_classify_event(uid)
        if verdict is None:
            features = await self._store.async_features_for_event(uid)
            if not features:
                return
            verdict = await self._classify(features)
            if verdict is None:
                return
        sample_uids = await self._store.async_sample_uids_ordered(uid)
        if verdict.label is None:
            cat, status = None, "unknown"
        elif verdict.label == identity.NOT_A_CAT:
            cat, status = None, identity.NOT_A_CAT
        else:
            cat, status = verdict.label, "auto"
        await self._store.async_set_event_classification(
            uid,
            cat=cat,
            identity_status=status,
            confidence=verdict.confidence,
            per_sample_uids=sample_uids,
            per_sample=verdict.per_sample,
        )

    async def async_replan_session(self, uid: str) -> bool:
        """Reconcile every part after classification, ingestion, or a human write."""
        scores = await self.async_subject_scores(uid) if sessions.SESSION_SPLIT_ENABLED else {}
        return await self._store.async_replan_session(uid, scores)

    async def async_subject_scores(self, uid: str) -> dict[int, sessions.IdentityScores]:
        """Build one current engine score per subject id in this session family."""
        if not sessions.SESSION_SPLIT_ENABLED:
            return {}
        if self.backend == "coral":
            assert self._coral is not None
            model = self._coral.model
            assert model is not None
            by_sid = await self._store.async_coral_features_by_sid_for_family(
                uid, coral_identity.CORAL_BODY_MODEL_ID, coral_identity.CORAL_FACE_MODEL_ID
            )
            top_threshold, margin = coral_identity.DECISION_TOP_THRESHOLD, coral_identity.DECISION_MARGIN
        else:
            model = self._model
            if model is None:
                return {}
            by_sid = await self._store.async_features_by_sid_for_family(uid)
            top_threshold, margin = identity.DECISION_TOP_THRESHOLD, identity.DECISION_MARGIN

        def _score_all() -> dict[int, sessions.IdentityScores]:
            out: dict[int, sessions.IdentityScores] = {}
            for sid, features in by_sid.items():
                distribution = model.class_distribution(features)
                if distribution is not None:
                    out[sid] = sessions.IdentityScores(
                        probabilities=tuple(distribution), top_threshold=top_threshold, margin=margin
                    )
            return out

        return await self._hass.async_add_executor_job(_score_all)

    async def async_classify_one(self, features: identity.Features) -> identity.Verdict | None:
        """Classifies a single detached sample, not from a stored event -- the upload
        pipeline's "does this look like a cat at all" gate (`views.py`'s training upload view).
        Same model, same executor rule as `async_classify_event`, just with no `samples`/
        `events` row on either side to read from or write back to."""
        return await self._classify([features])

    async def async_maybe_auto_learn(self, uid: str) -> bool:
        """Auto-learn's entry point: called for every event during ingest, idempotent by
        construction (this module's own docstring) so a repeat call for an already-processed
        event is always safe -- a few cheap reads that find nothing left to do. `True` only
        when it actually added at least one training sample, the signal `Ingestor.async_ingest`
        uses to decide whether a model rebuild is worth running. See `autolearn.py`'s module
        docstring for the full five-gate design (confidence+consistency, never contradicted by
        a human, diversity, rolling-accuracy pause/resume, never a judge verdict either)."""
        ctx = await self._store.async_event_training_context(uid)
        if ctx is None or ctx["open"] or ctx["reviewed"] or ctx["identity_status"] != "auto" or not ctx["cat"]:
            return False
        cat = ctx["cat"]
        if autolearn.is_judge_sourced(cat, ctx["judge_cat"], ctx["judge_at"]):
            return False
        if await self._store.async_auto_learn_paused(cat):
            return False
        samples = await self._store.async_sample_guesses(uid)
        untouched = [s for s in samples if s["review"] is None]  # a per-sample override is left alone
        label = autolearn.session_label([(s["guess"], s["guess_confidence"]) for s in untouched])
        if label != cat:
            return False
        qualifying = [
            s for s in untouched
            if s["guess"] == cat and (s["guess_confidence"] or 0.0) >= autolearn.AUTO_LEARN_MIN_CONFIDENCE
        ]
        # docs/40-vision-judge.md's crop-geometry note: a legacy sample whose stored body/face
        # crop is untrustworthy shows mostly unrelated content, not this cat -- it must never
        # become new training data, even though its (likely wrong) guess already counted toward
        # `session_label`'s consistency check above.
        qualifying = [s for s in qualifying if crop_geometry.is_legacy_crop_trustworthy(s["box"], s["t"])]
        if not qualifying:
            return False
        mode = qualifying[0]["mode"]
        existing = await self._store.async_training_feats_for_cat(cat, mode)
        keep_idx = await self._hass.async_add_executor_job(
            autolearn.select_diverse, [s["feat"] for s in qualifying], existing
        )
        added = False
        for i in keep_idx:
            sample = qualifying[i]
            ok = await self._store.async_add_auto_training(
                cat=cat, sample_uid=sample["uid"], confidence=sample["guess_confidence"]
            )
            added = added or ok
        return added

    async def async_reclassify_unreviewed(self, retention_cutoff: int) -> None:
        """Re-runs classification for every unreviewed event inside retention -- called after
        every training change (a label, or `kibble/training/remove`), so a name that only
        became learnable with the new data shows up without waiting for that cat's next
        visit."""
        for uid in await self._store.async_events_for_reclassify(retention_cutoff):
            await self.async_classify_event(uid)
            await self.async_replan_session(uid)


class Ingestor:
    """Consumes one poll/push cycle's `events`/`feeds` for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client: KibbleClient,
        store: KibbleStore,
        engine: IdentityEngine,
        clip_linker: EatingClipLinker | None = None,
        vision_judge: VisionJudge | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._client = client
        self._store = store
        self._engine = engine
        # `None` in every test that has no reason to exercise eating-clip linking (see
        # `tests/test_ingest.py`'s bare `Ingestor.__new__` fixtures) and in any install that
        # never wires one up; real setup (`__init__.py`) always passes a real instance, whose
        # own `enabled` gates every call here on whether a Scrypted clips server is configured
        # at all (`docs/39-eating-clips.md`).
        self._clip_linker = clip_linker
        # Same `None`-in-tests, `enabled`-gated shape as `_clip_linker` above, but public:
        # unlike the clip linker, `diagnostics.py` reads this instance directly through
        # `coordinator.ingestor.vision_judge` (docs/40-vision-judge.md).
        self.vision_judge = vision_judge
        # Wired by `__init__.py` right after both objects exist: `KibbleCoordinator.__init__`
        # itself takes this `Ingestor`, so the reverse reference cannot be a constructor
        # parameter here without a chicken-and-egg problem. Always set before any ingest can
        # actually run -- the first one is scheduled from the coordinator's own first refresh,
        # strictly after `__init__.py` makes this assignment.
        self.coordinator: KibbleCoordinator | None = None

    async def async_ingest(
        self, events: Sequence[DetectionEvent], feeds: Sequence[FeedRecord]
    ) -> None:
        assert self.coordinator is not None  # wired by `__init__.py` before any ingest can run
        self.coordinator.bowl_fill_settle_pending()
        auto_learn_changed = False
        for event in events:
            try:
                if await self._ingest_event(event):
                    auto_learn_changed = True
            except Exception:  # noqa: BLE001 -- one bad row must never abort the whole batch
                _LOGGER.exception("Failed to ingest event %s", event.event_id)
        for feed in feeds:
            try:
                await self._ingest_feed(feed)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to ingest feed %s", feed.id)
        if auto_learn_changed:
            # Model rebuild + reclassify only, off the event-loop path this whole module
            # already runs on -- the identity-snapshot push both of those want happens for
            # free right after, unconditionally, in `coordinator._async_run_ingest`.
            try:
                await self._engine.async_rebuild()
                if self.coordinator is not None:
                    await self._engine.async_reclassify_unreviewed(self.coordinator.retention_cutoff())
            except Exception:  # noqa: BLE001 -- a rebuild failure must not crash the ingest pass
                _LOGGER.exception("Auto-learn triggered model rebuild failed")

    # --- events ------------------------------------------------------------------------------

    async def _ingest_event(self, event: DetectionEvent) -> bool:
        uid = event_uid(self._entry_id, event)
        date = _utc_date(event.ts)

        # Check the DB *before* touching the network: once a field is durably recorded it has
        # already been fetched and acknowledged (or, on the rare crash between the two, the
        # ack was still already attempted), so re-processing it here would either waste a
        # round trip or, worse, the device may have already nulled it out on its own next
        # response -- never regress an already-durable reference back to null either way.
        existing = await self._store.async_event_asset_fields(uid) or {}
        scene = existing.get("scene")
        # Contract 4 (docs/40-vision-judge.md): `librefeedd` rewrites `scene` under a NEW
        # filename whenever a better sample supersedes it, unlike `before`/`after` (one-shot
        # brackets, never revisited below). A changed filename is a replacement, not a first
        # fetch -- refetch it, and remember the superseded asset id so it can be cleaned up and
        # this event's judge evidence invalidated once the new value is durably written.
        superseded_scene: str | None = None
        if event.scene and (scene is None or scene.rsplit("/", 1)[-1] != event.scene):
            new_scene, _data = await self._fetch_event_asset(event.scene, date)
            if new_scene is not None:
                superseded_scene = scene
                scene = new_scene
        before = existing.get("before")
        if before is None and event.image_before:
            before, _data = await self._fetch_event_asset(event.image_before, date)
        after = existing.get("after")
        if after is None and event.image_after:
            after, _data = await self._fetch_event_asset(event.image_after, date)

        await self._store.async_upsert_event(
            uid=uid,
            device_event_id=event.event_id,
            kind=event.kind,
            start=event.ts,
            end=event.end,
            open_=event.open,
            eat_start=event.eat_start,
            scene=scene,
            scene_k=event.scene_k,
            subjects=[subject.as_json() for subject in event.subjects] if event.subjects is not None else None,
            before=before,
            after=after,
        )
        if superseded_scene is not None:
            await self._store.async_invalidate_scene_asset(uid, superseded_scene)
        if self._clip_linker is not None and event.kind == "eat" and not event.open and event.end is not None:
            # A closing "eat" -- start (or leave running) the background clip-lookup arc for
            # this session (`docs/39-eating-clips.md`). `schedule_link` is its own dedup: a
            # device that keeps re-reporting the same closed, still-unlinked event on later
            # polls never restarts the retry schedule from the top.
            self._clip_linker.schedule_link(uid, event.ts, event.end)

        existing_samples = await self._store.async_existing_sample_uids(uid)
        new_samples = False
        for sample in event.samples:
            frame_boxes = []
            if sample.box is not None:
                frame_boxes.append([sample.sid, *sample.box])
            frame_boxes.extend(
                [other.sid, *other.box] for other in sample.others if other.box is not None
            )
            s_uid = sample_uid(uid, sample)
            if s_uid not in existing_samples:
                await self._ingest_sample(uid, s_uid, date, sample, frame_boxes)
                existing_samples.add(s_uid)
                new_samples = True
            for other in sample.others:
                if other.sid is None:
                    continue
                other_uid = f"{uid}-s{sample.k}-o{other.sid}"
                if other_uid in existing_samples:
                    continue
                added = await self._ingest_other_sample(uid, other_uid, date, sample, other, frame_boxes)
                if added:
                    existing_samples.add(other_uid)
                    new_samples = True

        if new_samples:
            await self._engine.async_classify_event(uid)
        await self._engine.async_replan_session(uid)
        if self.vision_judge is not None and not event.open and event.kind in ("eat", "visit"):
            # Debounced (docs/40-vision-judge.md): scheduling again on every poll of the same
            # closed event is a cheap restart, so a late-arriving `after` frame or a scene
            # replacement that just landed above is included in whichever run actually fires.
            self.vision_judge.schedule_judge(uid)
        if self.coordinator is not None and not self.coordinator.auto_learn_enabled:
            return False
        return await self._engine.async_maybe_auto_learn(uid)

    async def _ingest_sample(
        self,
        event_uid_: str,
        s_uid: str,
        date: str,
        sample: Sample,
        frame_boxes: Sequence[Sequence[int | float | None]],
    ) -> None:
        body_id, body_bytes = await self._fetch_event_asset(sample.body, date)
        face_name = sample.face.jpeg if sample.face else None
        emb_name = sample.face.emb if sample.face else None
        face_id, face_bytes = await self._fetch_event_asset(face_name, date)
        _emb_id, emb_bytes = await self._fetch_event_asset(emb_name, date)
        features = await self._hass.async_add_executor_job(
            identity.features_from, body_bytes, face_bytes, emb_bytes
        )
        await self._store.async_insert_sample(
            uid=s_uid, event_uid=event_uid_, t=sample.t, body=body_id, face=face_id,
            features=features, box=sample.box, score=sample.score, sid=sample.sid,
            frame_k=sample.k, is_primary=True, bowl=sample.bowl, frame_boxes=frame_boxes,
        )

    async def _ingest_other_sample(
        self,
        event_uid_: str,
        s_uid: str,
        date: str,
        sample: Sample,
        other: OtherSubjectSample,
        frame_boxes: Sequence[Sequence[int | float | None]],
    ) -> bool:
        body_id, body_bytes = await self._fetch_event_asset(other.body, date)
        face_name = other.face.jpeg if other.face else None
        emb_name = other.face.emb if other.face else None
        face_id, face_bytes = await self._fetch_event_asset(face_name, date)
        _emb_id, emb_bytes = await self._fetch_event_asset(emb_name, date)
        if body_bytes is None and face_bytes is None and emb_bytes is None:
            return False
        features = await self._hass.async_add_executor_job(
            identity.features_from, body_bytes, face_bytes, emb_bytes
        )
        await self._store.async_insert_sample(
            uid=s_uid, event_uid=event_uid_, t=sample.t, body=body_id, face=face_id,
            features=features, box=other.box, score=other.score, sid=other.sid,
            frame_k=sample.k, is_primary=False, bowl=other.bowl, frame_boxes=frame_boxes,
        )
        return True

    async def _fetch_event_asset(self, name: str | None, date: str) -> tuple[str | None, bytes | None]:
        """One evidence asset from the feeder's `/events/<name>` spool: fetched (or, if already
        durable locally, read back) and acknowledged. Returns `(None, None)` for a null name or
        a failed fetch -- never raises, so one missing/evicted asset never aborts the sample or
        event it belongs to."""
        if not name:
            return None, None
        if await self._store.async_media_exists(date, name):
            data = await self._read_local(date, name)
        else:
            try:
                data = await self._client.asset_bytes(name)
            except KibbleError as err:
                _LOGGER.debug("Could not fetch evidence asset %s: %s", name, err)
                return None, None
            await self._store.async_write_media(date, name, data)
        try:
            await self._client.delete_asset(name)
        except KibbleError as err:
            _LOGGER.debug("Could not acknowledge evidence asset %s: %s", name, err)
        return f"{date}/{name}", data

    async def _read_local(self, date: str, name: str) -> bytes:
        path = self._store.asset_path(f"{date}/{name}")
        assert path is not None  # `date`/`name` are both server-generated, never unsafe
        return await self._hass.async_add_executor_job(path.read_bytes)

    # --- feeds -------------------------------------------------------------------------------

    async def _ingest_feed(self, feed: FeedRecord) -> None:
        uid = feed_uid(self._entry_id, feed)
        date = _utc_date(feed.ts)
        assert self.coordinator is not None  # wired by `__init__.py` before any ingest can run

        existing_raw = await self._store.async_feed_asset_fields(uid)
        if existing_raw is None:
            self.coordinator.async_apply_bowl_fill_feed(feed)
        existing = existing_raw or {}
        before = existing.get("before")
        if before is None and feed.before:
            before = await self._archive_feed_asset(feed.before, date)
        after = existing.get("after")
        if after is None and feed.after:
            after = await self._archive_feed_asset(feed.after, date)

        portions: float | None = None
        if feed.amount1 is not None or feed.amount2 is not None:
            portions = float((feed.amount1 or 0) + (feed.amount2 or 0))

        # `amount1`/`amount2`/`food1`/`food2`/`single` are recorded once, at first insert, from
        # the device record and the coordinator's state at this exact moment -- historical
        # accuracy (docs/37-hopper-full.md): `store.upsert_feed` freezes them and ignores these
        # arguments on every later re-upsert of the same `uid`, so a divider flip or a food
        # rename afterward never rewrites what already happened.
        await self._store.async_upsert_feed(
            uid=uid,
            device_feed_id=feed.id,
            ts=feed.ts,
            portions=portions,
            scheduled=not feed.manual,
            confirmed=feed.confirmed,
            before=before,
            after=after,
            amount1=feed.amount1,
            amount2=feed.amount2,
            food1=self.coordinator.hopper_food(1),
            food2=self.coordinator.hopper_food(2),
            single=self.coordinator.single_hopper,
        )

    async def _archive_feed_asset(self, name: str | None, date: str) -> str | None:
        """A feed's before/after photo. LibreFeed spools it like any other evidence, so it is
        fetched and acknowledged through `/events/<name>`; the vendor stack keeps it at
        `/feeds/<name>` with no acknowledgement route, which is only tried when the spool route
        does not have it."""
        if not name:
            return None
        asset_id, _data = await self._fetch_event_asset(name, date)
        if asset_id is not None:
            return asset_id
        try:
            data = await self._client.feed_bytes(name)
        except KibbleError as err:
            _LOGGER.debug("Could not fetch feed evidence %s: %s", name, err)
            return None
        await self._store.async_write_media(date, name, data)
        return f"{date}/{name}"
