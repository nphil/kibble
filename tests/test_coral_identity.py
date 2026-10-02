"""`coral_identity.py`'s pure classification math (`CoralModel`) and `CoralRecognizer`'s
orchestration: reachability-gated rebuild ("last known good" on a transient failure), the
embedding-cache catch-up, and the `None`-vs-`Verdict` fallback contract `ingest.py`'s
`IdentityEngine` depends on -- see `CoralRecognizer.async_classify_event`'s own docstring for
exactly what each return shape means (bare `None` = "Coral could not answer at all, fall back to
the histogram recognizer"; a real `identity.Verdict`, even an inconclusive `label=None` one, is
Coral's own answer and must never be second-guessed).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from kibble import coral_identity, identity
from kibble.coral_client import HealthStatus
from kibble.coral_identity import CoralFeatures, CoralModel, CoralRecognizer


def _unit(vec: list[float]) -> np.ndarray:
    arr = np.array(vec, dtype=np.float32)
    return arr / np.linalg.norm(arr)


# --- CoralModel: centroid classification, fusion, gate ------------------------------------------


def test_classify_picks_the_nearest_centroid() -> None:
    training = [
        ("Kitty", CoralFeatures(body_emb=_unit([1, 0, 0, 0]), face_emb=None, mode="day")),
        ("Kitty", CoralFeatures(body_emb=_unit([0.95, 0.05, 0, 0]), face_emb=None, mode="day")),
        ("Pancake", CoralFeatures(body_emb=_unit([0, 1, 0, 0]), face_emb=None, mode="day")),
        ("Pancake", CoralFeatures(body_emb=_unit([0.05, 0.95, 0, 0]), face_emb=None, mode="day")),
    ]
    model = CoralModel(training)

    verdict = model.classify([CoralFeatures(body_emb=_unit([1, 0.02, 0, 0]), face_emb=None, mode="day")])

    assert verdict.label == "Kitty"
    assert verdict.confidence is not None and verdict.confidence >= coral_identity.DECISION_TOP_THRESHOLD


def test_classify_uses_face_alone_when_body_is_missing() -> None:
    training = [
        ("Kitty", CoralFeatures(body_emb=None, face_emb=_unit([1, 0, 0]), mode="day")),
        ("Pancake", CoralFeatures(body_emb=None, face_emb=_unit([0, 1, 0]), mode="day")),
    ]
    model = CoralModel(training)

    verdict = model.classify([CoralFeatures(body_emb=None, face_emb=_unit([0.98, 0.1, 0]), mode="day")])

    assert verdict.label == "Kitty"


def test_classify_fuses_body_and_face_more_confidently_than_either_alone() -> None:
    training = [
        ("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=_unit([1, 0]), mode="day")),
        ("Pancake", CoralFeatures(body_emb=_unit([0, 1]), face_emb=_unit([0, 1]), mode="day")),
    ]
    model = CoralModel(training)
    body_only = model._modality_log_probs(CoralFeatures(body_emb=_unit([0.9, 0.1]), face_emb=None, mode="day"))
    both = model._modality_log_probs(
        CoralFeatures(body_emb=_unit([0.9, 0.1]), face_emb=_unit([0.9, 0.1]), mode="day")
    )

    fused_body_only = identity.fuse_log_probs(body_only)
    fused_both = identity.fuse_log_probs(both)

    # Two modalities agreeing pushes the fused top probability at least as high as one alone.
    assert fused_both.max() >= fused_body_only.max()
    verdict = model.classify([CoralFeatures(body_emb=_unit([0.9, 0.1]), face_emb=_unit([0.9, 0.1]), mode="day")])
    assert verdict.label == "Kitty"
    assert verdict.per_sample == [("Kitty", verdict.confidence)]


def test_classify_returns_no_label_with_no_training_data_at_all() -> None:
    model = CoralModel([])

    verdict = model.classify([CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day")])

    assert verdict.label is None and verdict.confidence is None
    assert verdict.per_sample == [(None, None)]


def test_classify_returns_no_label_for_a_sample_with_no_embeddings_at_all() -> None:
    training = [("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day"))]
    model = CoralModel(training)

    verdict = model.classify([CoralFeatures(body_emb=None, face_emb=None, mode="day")])

    assert verdict.per_sample == [(None, None)]


def test_classify_gate_requires_both_top_probability_and_margin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate mechanism's own AND logic, pinned against explicit threshold values so it never
    depends on wherever live-data calibration currently sets `DECISION_TOP_THRESHOLD`/
    `DECISION_MARGIN` (docs/41-coral-recognition.md, "Calibration")."""
    monkeypatch.setattr(coral_identity, "DECISION_TOP_THRESHOLD", 0.9)
    monkeypatch.setattr(coral_identity, "DECISION_MARGIN", 0.3)
    training = [
        ("Kitty", CoralFeatures(body_emb=_unit([1, 0.001]), face_emb=None, mode="day")),
        ("Pancake", CoralFeatures(body_emb=_unit([0.001, 1]), face_emb=None, mode="day")),
    ]
    model = CoralModel(training)

    # Equidistant from both centroids -- clears neither the top-probability floor nor the margin.
    ambiguous = model.classify([CoralFeatures(body_emb=_unit([1, 1]), face_emb=None, mode="day")])
    assert ambiguous.label is None

    # Unambiguous -- clears both.
    confident = model.classify([CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day")])
    assert confident.label == "Kitty"


def test_not_a_cat_is_an_ordinary_class_with_no_special_case() -> None:
    training = [
        ("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day")),
        (identity.NOT_A_CAT, CoralFeatures(body_emb=_unit([0, 1]), face_emb=None, mode="day")),
    ]
    model = CoralModel(training)
    assert identity.NOT_A_CAT in model.classes

    verdict = model.classify([CoralFeatures(body_emb=_unit([0.02, 1]), face_emb=None, mode="day")])

    assert verdict.label == identity.NOT_A_CAT


def test_a_single_body_not_a_cat_sample_is_used_as_is_never_specially_excluded() -> None:
    """docs/41-coral-recognition.md: the benchmark found only one labelled not-a-cat BODY photo
    in the whole corpus -- this module must not invent a minimum-sample gate for it that
    `identity.Model` itself does not have; whatever data exists is simply used."""
    training = [
        ("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day")),
        (identity.NOT_A_CAT, CoralFeatures(body_emb=_unit([0, 1]), face_emb=None, mode="day")),
    ]
    model = CoralModel(training)

    assert identity.NOT_A_CAT in model._pools(lambda f: f.body_emb)["day"]


def test_loo_accuracy_is_none_under_the_minimum_sample_floor() -> None:
    training = [("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day")) for _ in range(3)]
    model = CoralModel(training)

    assert model.loo_accuracy() == {"Kitty": None}


def test_loo_accuracy_is_measured_once_the_minimum_sample_floor_is_met() -> None:
    training = [
        ("Kitty", CoralFeatures(body_emb=_unit([1, 0.01 * i]), face_emb=None, mode="day"))
        for i in range(coral_identity.MIN_LOO_SAMPLES + 1)
    ]
    model = CoralModel(training)

    result = model.loo_accuracy()

    assert result["Kitty"] is not None and 0.0 <= result["Kitty"] <= 1.0


# --- CoralModel: mode-split pools (docs/41-coral-recognition.md, "Calibration") -----------------


def test_day_and_ir_pools_never_cross_contaminate() -> None:
    """The live-data calibration run's own headline finding: pooling day+ir centroids together
    measurably hurt body-crop accuracy (fold-unstable, sometimes worse than the histogram
    recognizer); splitting them, mirroring `identity.Model`'s own `_body_pools`/`_face_pools`,
    fixed it. Constructed so day and ir data for the SAME two classes would classify OPPOSITELY
    if the pools were ever pooled together instead of kept separate."""
    training = [
        ("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day")),
        ("Pancake", CoralFeatures(body_emb=_unit([0, 1]), face_emb=None, mode="day")),
        # Under `ir`, the SAME two vectors are swapped between classes -- a pooled model would
        # average these into near-identical, uninformative centroids; separate pools keep each
        # mode's own decision correct.
        ("Kitty", CoralFeatures(body_emb=_unit([0, 1]), face_emb=None, mode="ir")),
        ("Pancake", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="ir")),
    ]
    model = CoralModel(training)

    day_verdict = model.classify([CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day")])
    ir_verdict = model.classify([CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="ir")])

    assert day_verdict.label == "Kitty"
    assert ir_verdict.label == "Pancake"


def test_a_sample_with_no_recorded_mode_contributes_no_signal() -> None:
    training = [
        ("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day")),
        ("Pancake", CoralFeatures(body_emb=_unit([0, 1]), face_emb=None, mode="day")),
    ]
    model = CoralModel(training)

    verdict = model.classify([CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode=None)])

    assert verdict.per_sample == [(None, None)]


def test_a_query_mode_with_no_matching_training_pool_contributes_no_signal() -> None:
    training = [("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day"))]
    model = CoralModel(training)

    verdict = model.classify([CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="ir")])

    assert verdict.per_sample == [(None, None)]


# --- CoralRecognizer: reachability-gated rebuild, embedding catch-up, fallback contract ----------


class _FakeClient:
    """Enough of `CoralHubClient`'s surface for `CoralRecognizer`."""

    def __init__(self, *, health_ok: bool = True, vectors: dict[str, list[float]] | None = None) -> None:
        self._health_ok = health_ok
        self._vectors = vectors or {}
        self.last_error: str | None = None
        self.embed_calls: list[tuple[str, int]] = []

    async def health(self) -> HealthStatus:
        if not self._health_ok:
            self.last_error = "unreachable"
            return HealthStatus(ok=False, error="unreachable")
        return HealthStatus(ok=True, device_status="ALIVE")

    async def embed(self, model: str, images: list[bytes]) -> list[list[float]] | None:
        self.embed_calls.append((model, len(images)))
        if not self._health_ok:
            self.last_error = "embed failed"
            return None
        return [self._vectors.get(model, [0.0]) for _ in images]


class _FakeStore:
    """Just enough of `KibbleStore`'s Coral-facing async surface for `CoralRecognizer`'s own
    orchestration tests -- the real SQL-backed behaviour is already proven in
    `tests/test_store.py`; this only proves `CoralRecognizer` calls the right thing with the
    right arguments and handles the result correctly."""

    def __init__(self) -> None:
        self.training_rows_needed: list[dict[str, Any]] = []
        self.sample_rows_needed: list[dict[str, Any]] = []
        self.event_rows_needed: dict[str, list[dict[str, Any]]] = {}
        self.training_features: list[tuple[str, CoralFeatures]] = []
        self.event_features: dict[str, list[CoralFeatures]] = {}
        self.cached: dict[tuple[str, str, str, str], bytes] = {}

    async def async_training_rows_needing_coral(self, body_model: str, face_model: str, limit: int):
        return self.training_rows_needed[:limit]

    async def async_samples_needing_coral(self, body_model: str, face_model: str, limit: int):
        return self.sample_rows_needed[:limit]

    async def async_samples_needing_coral_for_event(self, event_uid: str, body_model: str, face_model: str):
        return self.event_rows_needed.get(event_uid, [])

    async def async_set_coral_embedding(
        self, row_kind: str, row_uid: str, crop_kind: str, model_id: str, embedding: bytes
    ) -> None:
        self.cached[(row_kind, row_uid, crop_kind, model_id)] = embedding

    async def async_all_training_coral_features(self, body_model: str, face_model: str):
        return self.training_features

    async def async_coral_features_for_event(self, event_uid: str, body_model: str, face_model: str):
        return self.event_features.get(event_uid, [])


class _FakeHass:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _recognizer(store: _FakeStore, client: _FakeClient) -> CoralRecognizer:
    return CoralRecognizer(_FakeHass(), store, client)


async def test_rebuild_fails_and_stays_unavailable_when_coralhub_is_unreachable() -> None:
    recognizer = _recognizer(_FakeStore(), _FakeClient(health_ok=False))

    ok = await recognizer.async_rebuild()

    assert ok is False
    assert recognizer.available is False


async def test_rebuild_builds_a_model_once_healthy_with_training_features_present() -> None:
    store = _FakeStore()
    store.training_features = [("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day"))]
    recognizer = _recognizer(store, _FakeClient(health_ok=True))

    ok = await recognizer.async_rebuild()

    assert ok is True
    assert recognizer.available is True


async def test_rebuild_is_false_when_healthy_but_no_training_feature_exists_yet() -> None:
    recognizer = _recognizer(_FakeStore(), _FakeClient(health_ok=True))

    ok = await recognizer.async_rebuild()

    assert ok is False
    assert recognizer.available is False


async def test_a_later_unreachable_rebuild_keeps_the_last_known_good_model(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    store.training_features = [("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day"))]
    client = _FakeClient(health_ok=True)
    recognizer = _recognizer(store, client)
    assert await recognizer.async_rebuild() is True
    assert recognizer.available is True

    client._health_ok = False
    ok = await recognizer.async_rebuild()

    assert ok is False
    assert recognizer.available is True, "a transient health-check failure must not drop a good model"


async def test_embed_rows_caches_returned_vectors_under_the_right_keys() -> None:
    store = _FakeStore()
    client = _FakeClient(health_ok=True, vectors={coral_identity.CORAL_BODY_MODEL_ID: [1.0, 0.0]})
    recognizer = _recognizer(store, client)

    await recognizer._embed_rows("training", [{"uid": "t1", "body_bytes": b"jpeg", "face_bytes": None}])

    key = ("training", "t1", "body", coral_identity.CORAL_BODY_MODEL_ID)
    assert key in store.cached
    np.testing.assert_allclose(identity.unpack(store.cached[key]), [1.0, 0.0])
    assert client.embed_calls == [(coral_identity.CORAL_BODY_MODEL_ID, 1)]


async def test_classify_event_returns_none_when_not_available_yet() -> None:
    recognizer = _recognizer(_FakeStore(), _FakeClient(health_ok=True))

    assert await recognizer.async_classify_event("e1") is None


async def test_classify_event_returns_a_real_verdict_once_available_never_bare_none() -> None:
    store = _FakeStore()
    store.training_features = [
        ("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day")),
        ("Pancake", CoralFeatures(body_emb=_unit([0, 1]), face_emb=None, mode="day")),
    ]
    store.event_features["e1"] = [CoralFeatures(body_emb=_unit([1, 1]), face_emb=None, mode="day")]  # ambiguous
    client = _FakeClient(health_ok=True)
    recognizer = _recognizer(store, client)
    assert await recognizer.async_rebuild() is True

    verdict = await recognizer.async_classify_event("e1")

    assert verdict is not None, "available Coral must answer with a real Verdict, even if inconclusive"
    assert verdict.label is None  # the ambiguous fixture is inconclusive, but it IS an answer


async def test_classify_event_returns_none_when_coralhub_is_known_unreachable_even_with_a_good_model() -> None:
    store = _FakeStore()
    store.training_features = [("Kitty", CoralFeatures(body_emb=_unit([1, 0]), face_emb=None, mode="day"))]
    client = _FakeClient(health_ok=True)
    recognizer = _recognizer(store, client)
    assert await recognizer.async_rebuild() is True

    client._health_ok = False
    client.last_error = "down"  # a previous request already failed

    assert await recognizer.async_classify_event("e1") is None, (
        "a known-unreachable CoralHub must fall back even while `available` (a stale model) is True"
    )


async def test_backfill_stops_once_nothing_is_left_to_embed() -> None:
    recognizer = _recognizer(_FakeStore(), _FakeClient(health_ok=True))

    await recognizer.async_backfill()  # both queues empty -- must return immediately, never hang


async def test_backfill_calls_on_progress_after_a_successful_training_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coral_identity, "BACKFILL_PACE_S", 0)
    store = _FakeStore()
    store.training_rows_needed = [{"uid": "t1", "body_bytes": b"jpeg", "face_bytes": None}]
    client = _FakeClient(health_ok=True, vectors={coral_identity.CORAL_BODY_MODEL_ID: [1.0, 0.0]})
    recognizer = _recognizer(store, client)
    progressed: list[bool] = []

    async def _on_progress() -> None:
        progressed.append(True)
        store.training_rows_needed = []  # simulates the rebuild catching up on the new data

    recognizer.on_progress = _on_progress

    await recognizer.async_backfill()

    assert progressed == [True]
    assert ("training", "t1", "body", coral_identity.CORAL_BODY_MODEL_ID) in store.cached
