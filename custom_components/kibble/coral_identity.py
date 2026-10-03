"""Coral-embedding-backed cat recognizer: nearest-class-centroid classification over CoralHub
image embeddings, and the orchestration that keeps the embedding cache (`store.py` schema v8)
warm and decides, on every rebuild, whether Coral is actually usable right now.

Design: docs/41-coral-recognition.md. Mirrors `identity.py`'s own architecture closely: one
independent pool per crop kind (body, face) AND per day/ir mode, each modality scored on its
own and then fused across modalities the same way `identity.Model` fuses face_emb/body_feat/
face_feat -- `identity.to_log_probs`/`identity.fuse_log_probs` are the SAME functions this
module calls, not a re-implementation.

Mode-splitting the centroid pools (never obvious from the 2026-09-25 benchmark's own smaller
dataset, which pooled day+ir for Coral and still won) turned out to matter once tested against
the full live DB (`tools/coral_verify.py`, docs/41-coral-recognition.md, "Calibration"): pooling
day and ir body embeddings together gave a highly fold-unstable, occasionally worse-than-
baseline body accuracy (0.564-0.972 swinging per fold); splitting by mode (mirroring
`identity.Model`'s own `_body_pools`/`_face_pools` exactly) fixed that instability outright and
pushed body accuracy from 0.754 to 0.872 overall, beating the histogram recognizer in every
condition tested. However lighting-invariant Coral's own embedding may be in isolation, a
cat's own pooled day+night centroid is still a worse stand-in for either lighting condition on
its own than two separate centroids are.

`not_a_cat` gets NO special case anywhere in this module: it is just another class name that may
or may not have training samples, exactly like `identity.Model` treats it today. Body crops have
essentially no not_a_cat training data (one photo, per the benchmark) -- that is a data gap
shared by both recognizers, not something this module works around or hides.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from . import identity
from .coral_client import CoralHubClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .store import KibbleStore

_LOGGER = logging.getLogger(__name__)

# CoralHub installs these two automatically on first start (its own `app/catalog.py`,
# `KIBBLE_REQUIRED_MODEL_IDS`) -- the 2026-09-25 benchmark's recommended pairing: body crops to
# the imprinting base model's own true embedding layer, face crops to the ready-to-use
# extractor (coral-bench.md, section 10, "Model files").
CORAL_BODY_MODEL_ID = "mobilenet_v1_1.0_224_l2norm_quant_edgetpu"
CORAL_FACE_MODEL_ID = "mobilenet_v1_1.0_224_quant_embedding_extractor_edgetpu"

# softmax(COSINE_TEMPERATURE * cosine-similarity-to-centroid) -- the benchmark's own confidence
# construction for its centroid classifier (coral-bench's `scripts/run_evaluation.py::
# centroid_predict`), reused verbatim rather than re-derived: it exists only to give the
# 95%-precision threshold sweep (`tools/coral_verify.py`, mirroring the benchmark's own
# `summarize_results.py::threshold_for_precision`) a monotonic, bounded score to sweep over --
# not independently calibrated against `identity.Model`'s own log-probability scale, and never
# compared to it directly (coral-bench.md, "Assumptions and things I could not do").
COSINE_TEMPERATURE = 10.0

# Same purpose as `identity.LOG_PROB_FLOOR` (never let one saturated modality output exactly
# 1.0 and veto every other modality's own opinion outright) but calibrated separately: Coral's
# centroid-softmax scores are shaped differently from the histogram recognizer's k-NN vote, so
# there is no reason the two floors should land on the same number by coincidence.
# `tools/coral_verify.py`'s live-data sweep (docs/41-coral-recognition.md, "Calibration") found
# no wrong-rate benefit from raising it past the histogram recognizer's own starting point, so
# it stays there.
LOG_PROB_FLOOR = 0.03

# The production decision gate -- identical shape to `identity.DECISION_TOP_THRESHOLD`/
# `DECISION_MARGIN` (top fused probability must clear a floor AND beat the runner-up by a
# margin), calibrated against the live, human-labelled DB via event-grouped 5-fold
# cross-validation over the FUSED per-row decision (whichever modalities a row actually has --
# `tools/coral_verify.py`'s own methodology, mirroring the benchmark's `threshold_for_precision`
# but jointly swept over both threshold and margin). 0.52/0.15 was the coverage-maximising pair
# clearing 95% precision on that run (95.16% precision, 82.4% coverage; the confidence-only
# equivalent from the benchmark's own sweep style landed at 0.56 with materially less coverage) --
# see docs/41-coral-recognition.md, "Calibration", for the full run and its accuracy table.
DECISION_TOP_THRESHOLD = 0.52
DECISION_MARGIN = 0.15

# Leave-one-out accuracy needs at least this many samples of a class to mean anything -- same
# floor `identity.Model.loo_accuracy` uses, reused rather than re-picked since it is about
# statistical significance of a LOO estimate, not about either recognizer's own math.
MIN_LOO_SAMPLES = identity.MIN_LOO_SAMPLES

# Bounded catch-up run inline by every `CoralRecognizer.async_rebuild` (cheap, keeps
# training-mutation latency snappy: a rebuild follows one label/upload/auto-learn action, so at
# most a handful of rows are ever newly missing an embedding at that moment). The dedicated
# background backfill task (`async_backfill`, started once per entry load by `__init__.py`'s
# `_async_coral_startup`, right after the first rebuild) does the bulk of the catching-up over
# time instead, uncapped.
REBUILD_EMBED_LIMIT = 64
BACKFILL_BATCH_LIMIT = 32
BACKFILL_PACE_S = 1.0
BACKFILL_UNREACHABLE_RETRY_S = 300.0


def _centroid_scores(
    query: np.ndarray, centroids: dict[str, np.ndarray], classes: Sequence[str]
) -> dict[str, float] | None:
    """Class scores via softmax(`COSINE_TEMPERATURE` * cosine-similarity-to-centroid) -- the
    benchmark's own confidence construction. `None` when there is no centroid at all yet (an
    empty gallery). A class absent from `centroids` (no training sample of that class had this
    crop kind embedded) gets 0.0, the same convention `identity._global_knn_scores` uses for a
    class absent from its own pool."""
    if not centroids:
        return None
    present = list(centroids.keys())
    sims = np.array([float(np.dot(query, centroids[c])) for c in present], dtype=np.float64)
    scaled = sims * COSINE_TEMPERATURE
    exp = np.exp(scaled - scaled.max())
    probs = exp / exp.sum()
    scores = dict.fromkeys(classes, 0.0)
    for c, p in zip(present, probs, strict=True):
        scores[c] = float(p)
    return scores


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 1e-9 else vec


@dataclass(frozen=True, slots=True)
class CoralFeatures:
    """One sample's Coral embeddings, both already L2-normalised (CoralHub's own wire
    contract), plus the SAME day/ir `mode` `identity.Features` carries (from the stored
    `samples`/`training` row -- both recognizers key their pools by the same value). Either
    embedding field may be `None` when that crop is missing, failed to decode, or has not been
    embedded (yet, or ever -- a permanently unreachable CoralHub is not this dataclass's
    concern; `CoralRecognizer` decides what that means for classification). `mode=None` (an
    embedding exists but its lighting mode was never recorded) makes that modality unusable the
    same way `identity.Model` treats it -- see `CoralModel._modality_log_probs`."""

    body_emb: np.ndarray | None
    face_emb: np.ndarray | None
    mode: str | None


class CoralModel:
    """Nearest-class-centroid classifier over CoralHub embeddings -- the 2026-09-25 benchmark's
    winning classifier for both body and face crops (coral-bench.md, section 3: centroid beat
    both a k=5 distance-weighted vote and multinomial logistic regression on every crop kind
    tested). One centroid pool per crop kind PER DAY/IR MODE, mirroring `identity.Model`'s own
    `_body_pools`/`_face_pools` exactly -- see the module docstring for why pooling modes
    together, fine on the original smaller benchmark, measurably hurt on the full live dataset."""

    def __init__(self, training: Sequence[tuple[str, CoralFeatures]]) -> None:
        self._training = list(training)
        self.classes: list[str] = sorted({label for label, _ in self._training})
        self._body_pools = self._pools(lambda f: f.body_emb)
        self._face_pools = self._pools(lambda f: f.face_emb)

    def _pools(
        self, getter: Callable[[CoralFeatures], np.ndarray | None]
    ) -> dict[str, dict[str, np.ndarray]]:
        """One centroid-per-class dict per mode: `{mode: {class: centroid}}`. A sample with no
        recorded mode contributes to no pool at all -- `identity.Model`'s own rule (`feat.mode
        is not None` gates every mode-keyed pool, both here and at classify time)."""
        by_mode: dict[str, dict[str, list[np.ndarray]]] = {}
        for label, feat in self._training:
            vec = getter(feat)
            if vec is None or feat.mode is None:
                continue
            by_class = by_mode.setdefault(feat.mode, {})
            by_class.setdefault(label, []).append(vec)
        return {
            mode: {
                label: _l2_normalize(np.mean(np.stack(vecs), axis=0).astype(np.float32))
                for label, vecs in by_class.items()
            }
            for mode, by_class in by_mode.items()
        }

    def _modality_log_probs(self, feat: CoralFeatures) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        if feat.body_emb is not None and feat.mode is not None:
            pool = self._body_pools.get(feat.mode)
            if pool is not None:
                scores = _centroid_scores(feat.body_emb, pool, self.classes)
                if scores is not None:
                    out.append(identity.to_log_probs(scores, self.classes, LOG_PROB_FLOOR))
        if feat.face_emb is not None and feat.mode is not None:
            pool = self._face_pools.get(feat.mode)
            if pool is not None:
                scores = _centroid_scores(feat.face_emb, pool, self.classes)
                if scores is not None:
                    out.append(identity.to_log_probs(scores, self.classes, LOG_PROB_FLOOR))
        return out

    def classify(self, samples: Sequence[CoralFeatures]) -> identity.Verdict:
        """Same per-sample-then-fused shape as `identity.Model.classify` (fuse each sample's
        own available modalities, then fuse across samples, gate the fused top class on
        `DECISION_TOP_THRESHOLD`/`DECISION_MARGIN`) -- see that method for the full rationale.
        `identity.Verdict` is reused verbatim so every existing consumer (`ingest.py`'s
        `IdentityEngine`, `store.set_event_classification`) needs no changes to accept a
        Coral-backed verdict."""
        per_sample: list[tuple[str | None, float | None]] = []
        all_log_probs: list[np.ndarray] = []
        for feat in samples:
            modality_logs = self._modality_log_probs(feat)
            if modality_logs:
                dist = identity.fuse_log_probs(modality_logs)
                idx = int(np.argmax(dist))
                per_sample.append((self.classes[idx], float(dist[idx])))
                all_log_probs.extend(modality_logs)
            else:
                per_sample.append((None, None))

        if not self.classes or not all_log_probs:
            return identity.Verdict(label=None, confidence=None, per_sample=per_sample)

        fused = identity.fuse_log_probs(all_log_probs)
        order = np.argsort(fused)[::-1]
        top_idx = int(order[0])
        top_p = float(fused[top_idx])
        second_p = float(fused[order[1]]) if len(order) > 1 else 0.0
        if top_p >= DECISION_TOP_THRESHOLD and (top_p - second_p) >= DECISION_MARGIN:
            return identity.Verdict(label=self.classes[top_idx], confidence=top_p, per_sample=per_sample)
        return identity.Verdict(label=None, confidence=None, per_sample=per_sample)

    def class_distribution(self, samples: Sequence[CoralFeatures]) -> list[tuple[str, float]] | None:
        """Same per-sample-then-fused shape as `classify` but returns the full fused class
        distribution instead of gating it to one winner -- see `identity.Model.
        class_distribution` for the full rationale; this is its Coral-backed mirror."""
        all_log_probs: list[np.ndarray] = []
        for feat in samples:
            all_log_probs.extend(self._modality_log_probs(feat))
        if not self.classes or not all_log_probs:
            return None
        fused = identity.fuse_log_probs(all_log_probs)
        return list(zip(self.classes, (float(p) for p in fused), strict=True))

    def loo_accuracy(self) -> dict[str, float | None]:
        """Same shape/semantics as `identity.Model.loo_accuracy` -- `ingest.py`'s
        `IdentityEngine.loo_accuracy(cat)` (surfaced by `websocket.py`'s `kibble/cats`) reads
        whichever backend built the current model through this one interface, unchanged."""
        result: dict[str, float | None] = {}
        by_class: dict[str, list[int]] = {}
        for i, (label, _) in enumerate(self._training):
            by_class.setdefault(label, []).append(i)
        for cls in self.classes:
            rows = by_class.get(cls, [])
            if len(rows) < MIN_LOO_SAMPLES:
                result[cls] = None
                continue
            correct = 0
            for i in rows:
                reduced = self._training[:i] + self._training[i + 1 :]
                fold = CoralModel(reduced)
                verdict = fold.classify([self._training[i][1]])
                if verdict.label == cls:
                    correct += 1
            result[cls] = correct / len(rows)
        return result


@dataclass(frozen=True, slots=True)
class CoralStatus:
    """Everything `diagnostics.py` needs about the Coral-backed recognizer, in one read."""

    configured: bool
    available: bool
    last_error: str | None


class CoralRecognizer:
    """Owns the CoralHub-backed half of `ingest.py`'s `IdentityEngine`: the embedding cache
    (embed each image at most once -- `store.py` schema v8), the current `CoralModel`, and the
    backfill/rebuild orchestration that keeps both warm. `IdentityEngine` falls back to the
    histogram `identity.Model` whenever a method here returns `None` outright (never a
    `identity.Verdict` with `label=None`, which is a real, inconclusive answer this backend
    itself gave -- see `async_classify_event`'s own docstring); this class never raises past its
    own boundary, every CoralHub failure just means "not available/answerable right now".

    `on_progress` is set (not constructor-injected) by `__init__.py` right after the sibling
    `IdentityEngine` exists -- same chicken-and-egg reason `Ingestor.coordinator`/`VisionJudge.
    coordinator` are wired post-construction: it is `IdentityEngine.async_rebuild` itself, called
    once a backfill batch actually adds new training-row centroids, so freshly caught-up data
    reaches production without waiting for an unrelated training mutation."""

    def __init__(self, hass: "HomeAssistant", store: "KibbleStore", client: CoralHubClient) -> None:
        self._hass = hass
        self._store = store
        self._client = client
        self.on_progress: Callable[[], Awaitable[None]] | None = None
        self._model: CoralModel | None = None
        self._loo: dict[str, float | None] = {}

    @property
    def model(self) -> CoralModel | None:
        """The current built model, or `None` before the first successful `async_rebuild`."""
        return self._model

    @property
    def available(self) -> bool:
        return self._model is not None and bool(self._model.classes)

    @property
    def last_error(self) -> str | None:
        return self._client.last_error

    @property
    def status(self) -> CoralStatus:
        return CoralStatus(configured=True, available=self.available, last_error=self.last_error)

    def loo_accuracy(self, cat: str) -> float | None:
        return self._loo.get(cat)

    # --- rebuild: reachability check + bounded catch-up + fresh CoralModel -------------------

    async def async_rebuild(self) -> bool:
        """`True` (and a fresh `self._model`) only when CoralHub answered its own health check
        AND at least one class ended up with a usable embedding; `False` leaves `self._model` at
        its PREVIOUS value -- a transient health-check failure during an otherwise-routine
        training-mutation rebuild degrades to "still using the last known-good Coral model",
        never a spurious drop back to the histogram recognizer over one bad poll. At startup
        (no previous model yet), a `False` here really does mean "not available yet" --
        `IdentityEngine` reads `self.available`, not this return value, to decide."""
        health = await self._client.health()
        if not health.ok:
            return False
        rows = await self._store.async_training_rows_needing_coral(
            CORAL_BODY_MODEL_ID, CORAL_FACE_MODEL_ID, REBUILD_EMBED_LIMIT
        )
        if rows:
            await self._embed_rows("training", rows)
        pairs = await self._store.async_all_training_coral_features(CORAL_BODY_MODEL_ID, CORAL_FACE_MODEL_ID)
        if not pairs:
            return False

        def _build() -> tuple[CoralModel, dict[str, float | None]]:
            model = CoralModel(pairs)
            return model, model.loo_accuracy()

        self._model, self._loo = await self._hass.async_add_executor_job(_build)
        return True

    async def async_classify_event(self, uid: str) -> identity.Verdict | None:
        """`None` when Coral cannot answer for `uid` at all right now (no model yet, or
        CoralHub's last request failed -- checked via the cheap cached `last_error` rather than
        a fresh health call, since this runs on every ingested event and a real request here
        already gates on it below): `IdentityEngine` falls back to the histogram recognizer for
        this one event. A real `identity.Verdict` (even an inconclusive `label=None` one) means
        Coral was actually asked and this is its answer -- never second-guessed against the
        histogram recognizer.

        Embedding catch-up for this event's own samples is attempted regardless of whether
        Coral is currently the preferred backend, as long as CoralHub is not already known to be
        down (same `last_error` guard) -- keeps the cache warm through a temporary fallback
        window so classification switches back the moment CoralHub (and the next rebuild) come
        back, instead of discovering a large embedding backlog only then."""
        if self._client.last_error is None:
            rows = await self._store.async_samples_needing_coral_for_event(
                uid, CORAL_BODY_MODEL_ID, CORAL_FACE_MODEL_ID
            )
            if rows:
                await self._embed_rows("sample", rows)
        if not self.available or self._client.last_error is not None:
            return None
        feats = await self._store.async_coral_features_for_event(uid, CORAL_BODY_MODEL_ID, CORAL_FACE_MODEL_ID)
        if not feats:
            return None
        model = self._model
        assert model is not None  # guarded by `self.available` above
        return await self._hass.async_add_executor_job(model.classify, feats)

    # --- embedding: the one place that turns crop bytes into a cached vector -----------------

    async def _embed_rows(self, row_kind: str, rows: list[dict[str, Any]]) -> bool:
        """Embeds whichever crop kind each of `rows` still needs (`store.py`'s own
        `training_rows_needing_coral`/`samples_needing_coral*` already worked out which crop, if
        any, needs it -- a row missing neither is never passed here at all) and caches the
        result. `True` if every crop that needed embedding got one; `False` the moment either
        model's batch call fails, so a caller can back off instead of busy-looping against an
        unreachable CoralHub."""
        ok = True
        for crop_kind, model_id in (("body", CORAL_BODY_MODEL_ID), ("face", CORAL_FACE_MODEL_ID)):
            key = f"{crop_kind}_bytes"
            batch = [(r["uid"], r[key]) for r in rows if r.get(key) is not None]
            if not batch:
                continue
            vectors = await self._client.embed(model_id, [data for _, data in batch])
            if vectors is None:
                ok = False
                continue
            for (uid, _), vector in zip(batch, vectors, strict=True):
                blob = identity.pack(np.asarray(vector, dtype=np.float32))
                await self._store.async_set_coral_embedding(row_kind, uid, crop_kind, model_id, blob)
        return ok

    # --- background backfill (docs/41-coral-recognition.md) ----------------------------------

    async def async_backfill(self) -> None:
        """Started once per entry load, as the second step of the entry's CoralHub background
        task (`__init__.py`'s `_async_coral_startup`, after the first rebuild) -- never blocks
        setup or the first poll. Catches up every training row's and every still-reclassifiable
        sample's (an unreviewed event's own -- `store.samples_needing_coral`'s own docstring)
        cached embedding, newest first, in `BACKFILL_BATCH_LIMIT`-sized batches, pacing itself
        (`BACKFILL_PACE_S` while healthy, `BACKFILL_UNREACHABLE_RETRY_S` after any failed batch)
        so it never monopolises the DB executor thread or the one physical TPU CoralHub itself
        serialises every call through. Never gives up outright: an unreachable CoralHub just
        means slower polling, forever, until either it catches up or the entry unloads (the
        background task is cancelled automatically then, same as `vision_judge.
        ensure_descriptions`'s own one-shot background task).

        Calls `on_progress` (`IdentityEngine.async_rebuild`) after each training batch that
        actually embeds something, so newly-caught-up centroids reach production without waiting
        for an unrelated training mutation."""
        while True:
            training_rows = await self._store.async_training_rows_needing_coral(
                CORAL_BODY_MODEL_ID, CORAL_FACE_MODEL_ID, BACKFILL_BATCH_LIMIT
            )
            sample_rows = await self._store.async_samples_needing_coral(
                CORAL_BODY_MODEL_ID, CORAL_FACE_MODEL_ID, BACKFILL_BATCH_LIMIT
            )
            if not training_rows and not sample_rows:
                _LOGGER.debug("Coral embedding backfill caught up; nothing left to embed")
                return
            ok = True
            if training_rows:
                training_ok = await self._embed_rows("training", training_rows)
                ok = training_ok
                if training_ok and self.on_progress is not None:
                    await self.on_progress()
            if sample_rows:
                ok = await self._embed_rows("sample", sample_rows) and ok
            await asyncio.sleep(BACKFILL_PACE_S if ok else BACKFILL_UNREACHABLE_RETRY_S)
