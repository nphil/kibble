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

    async def async_set_coral_embeddings(self, row_kind: str, items) -> None:
        for row_uid, crop_kind, model_id, embedding in items:
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


# --- CoralModel.loo_accuracy: the running-sum version against the brute-force original ----------

_BODY_DIM, _FACE_DIM = 8, 6


def _reference_loo(training: list[tuple[str, CoralFeatures]]) -> dict[str, float | None]:
    """The leave-one-out `CoralModel.loo_accuracy` computed before it became incremental, kept as
    the yardstick: a whole model built without each held-out row (quadratic in the gallery), asked
    about that row. `None` for a class under the sample floor."""
    by_class: dict[str, list[int]] = {}
    for i, (label, _) in enumerate(training):
        by_class.setdefault(label, []).append(i)
    result: dict[str, float | None] = {}
    for cls in sorted(by_class):
        rows = by_class[cls]
        if len(rows) < coral_identity.MIN_LOO_SAMPLES:
            result[cls] = None
            continue
        correct = 0
        for i in rows:
            fold = CoralModel(training[:i] + training[i + 1 :])
            if fold.classify([training[i][1]]).label == cls:
                correct += 1
        result[cls] = correct / len(rows)
    return result


def _structured_gallery(
    structure: list[tuple[str, bool, bool, str | None]], *, seed: int = 0, sigma: float = 0.8
) -> list[tuple[str, CoralFeatures]]:
    """A seeded gallery with exactly the given shape, one `(label, has_body, has_face, mode)` per
    row. Every class gets its own random centre per crop kind and each row is that centre plus
    noise, so classes overlap enough for some held-out rows to be decided wrongly or not at all."""
    rng = np.random.default_rng(seed)
    centres: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    training = []
    for label, has_body, has_face, mode in structure:
        body_centre, face_centre = centres.setdefault(
            label, (rng.normal(size=_BODY_DIM), rng.normal(size=_FACE_DIM))
        )
        body = _unit(body_centre + sigma * rng.normal(size=_BODY_DIM)) if has_body else None
        face = _unit(face_centre + sigma * rng.normal(size=_FACE_DIM)) if has_face else None
        training.append((label, CoralFeatures(body_emb=body, face_emb=face, mode=mode)))
    return training


def _random_structure(seed: int, sizes: dict[str, int]) -> list[tuple[str, bool, bool, str | None]]:
    """Rows of the given class sizes in random order, each with a body and/or a face crop and a
    day / infrared / unrecorded lighting mode drawn at random."""
    rng = np.random.default_rng(1000 + seed)
    structure: list[tuple[str, bool, bool, str | None]] = []
    for label, count in sizes.items():
        for _ in range(count):
            roll = rng.random()
            mode = None if roll < 0.06 else ("ir" if roll < 0.36 else "day")
            structure.append((label, bool(rng.random() < 0.85), bool(rng.random() < 0.7), mode))
    rng.shuffle(structure)
    return structure


def _loo_both_ways(training: list[tuple[str, CoralFeatures]]) -> dict[str, float | None]:
    """`CoralModel.loo_accuracy()`, after checking it agrees exactly with the brute-force reference."""
    result = CoralModel(training).loo_accuracy()
    assert result == _reference_loo(training)
    return result


_RANDOM_GALLERY_SIZES = {"Kitty": 14, "Pancake": 12, "Waffle": 7, identity.NOT_A_CAT: 6, "Mochi": 3}


@pytest.mark.parametrize("seed", range(8))
def test_loo_accuracy_matches_the_brute_force_reference_on_random_galleries(seed: int) -> None:
    training = _structured_gallery(_random_structure(seed, _RANDOM_GALLERY_SIZES), seed=seed, sigma=1.0)

    result = _loo_both_ways(training)

    assert result["Mochi"] is None, "3 rows is under the sample floor"
    assert all(result[name] is not None for name in ("Kitty", "Pancake", "Waffle", identity.NOT_A_CAT))


def test_the_random_galleries_are_neither_trivially_all_right_nor_all_wrong() -> None:
    """Agreeing on a gallery where every row is decided the same way would prove very little."""
    accuracies = [
        value
        for seed in range(8)
        for value in CoralModel(
            _structured_gallery(_random_structure(seed, _RANDOM_GALLERY_SIZES), seed=seed, sigma=1.0)
        ).loo_accuracy().values()
        if value is not None
    ]

    assert any(0.0 < value < 1.0 for value in accuracies)
    assert len(set(accuracies)) > 4


@pytest.mark.parametrize("seed", range(3))
def test_loo_accuracy_of_a_row_that_is_its_classes_only_vector_in_a_lighting_mode(seed: int) -> None:
    """Without that row the class has no centroid at all in that mode's pool: the one case where
    a centroid is REMOVED rather than just shifted, and the row can never be its own class."""
    structure = (
        [("Kitty", True, True, "day")] * 5 + [("Kitty", True, True, "ir")]
        + [("Pancake", True, True, "day")] * 3 + [("Pancake", True, True, "ir")] * 3
    )

    result = _loo_both_ways(_structured_gallery(structure, seed=seed))

    assert result["Kitty"] is not None and result["Kitty"] <= 5 / 6


@pytest.mark.parametrize("seed", range(3))
def test_loo_accuracy_of_a_row_that_is_the_only_one_in_its_whole_lighting_mode(seed: int) -> None:
    """With it gone the infrared pool does not exist at all, so it gets no verdict whatsoever."""
    structure = (
        [("Kitty", True, True, "day")] * 4 + [("Kitty", True, True, "ir")] + [("Pancake", True, True, "day")] * 5
    )

    result = _loo_both_ways(_structured_gallery(structure, seed=seed))

    assert result["Kitty"] is not None and result["Kitty"] <= 4 / 5


@pytest.mark.parametrize("seed", range(3))
def test_loo_accuracy_counts_rows_without_a_mode_but_they_never_help(seed: int) -> None:
    """A row with no recorded lighting mode adds to no centroid and is never recognised, yet it
    still counts toward the sample floor and the denominator."""
    structure = (
        [("Kitty", True, True, "day")] * 4 + [("Kitty", True, True, None)] * 2 + [("Pancake", True, True, "day")] * 6
    )

    result = _loo_both_ways(_structured_gallery(structure, seed=seed))

    assert result["Kitty"] is not None and result["Kitty"] <= 4 / 6


@pytest.mark.parametrize("seed", range(3))
def test_loo_accuracy_with_face_only_and_body_only_rows(seed: int) -> None:
    def mix(label: str) -> list[tuple[str, bool, bool, str | None]]:
        return (
            [(label, True, False, "day")] * 2 + [(label, False, True, "day")] * 2 + [(label, True, True, "day")] * 2
        )

    never_had_a_body = [("Waffle", False, True, "day")] * 6

    result = _loo_both_ways(_structured_gallery(mix("Kitty") + mix("Pancake") + never_had_a_body, seed=seed))

    assert all(value is not None for value in result.values())


@pytest.mark.parametrize("seed", range(3))
def test_loo_accuracy_needs_the_minimum_sample_floor_per_class(seed: int) -> None:
    floor = coral_identity.MIN_LOO_SAMPLES
    structure = (
        [("Kitty", True, True, "day")] * 8
        + [("Waffle", True, True, "day")] * floor
        + [("Mochi", True, True, "day")] * (floor - 1)
    )

    result = _loo_both_ways(_structured_gallery(structure, seed=seed))

    assert result["Mochi"] is None, "one sample short of the floor"
    assert result["Waffle"] is not None, "exactly at the floor"
    assert result["Kitty"] is not None


@pytest.mark.parametrize("seed", range(3))
def test_loo_accuracy_scores_a_not_a_cat_class_like_any_other_and_floors_a_thin_one(seed: int) -> None:
    cats = [("Kitty", True, True, "day")] * 6 + [("Pancake", True, True, "day")] * 6

    # Not-a-cat photos are almost all body crops (docs/41-coral-recognition.md).
    full = _loo_both_ways(_structured_gallery(cats + [(identity.NOT_A_CAT, True, False, "day")] * 6, seed=seed))
    assert full[identity.NOT_A_CAT] is not None

    thin = _loo_both_ways(_structured_gallery(cats + [(identity.NOT_A_CAT, True, False, "day")] * 3, seed=seed))
    assert thin[identity.NOT_A_CAT] is None
    assert thin["Kitty"] is not None


# --- EmbedStats and the recognizer's own bookkeeping --------------------------------------------


class _CallRecordingStore(_FakeStore):
    """`_FakeStore` that also remembers every `async_set_coral_embeddings` call as it was made."""

    def __init__(self) -> None:
        super().__init__()
        self.set_calls: list[tuple[str, list[tuple[str, str, str, bytes]]]] = []

    async def async_set_coral_embeddings(self, row_kind: str, items) -> None:
        items = list(items)
        self.set_calls.append((row_kind, items))
        await super().async_set_coral_embeddings(row_kind, items)


def _kitty_features() -> CoralFeatures:
    return CoralFeatures(body_emb=_unit([1, 0]), face_emb=_unit([0, 1]), mode="day")


def test_embed_stats_since_reports_only_what_happened_after_the_snapshot() -> None:
    stats = coral_identity.EmbedStats(calls=2, crops=5, ms=100.0, reused=7)
    earlier = stats.snapshot()

    stats.calls += 3
    stats.crops += 9
    stats.ms += 249.6
    stats.reused += 4

    assert stats.since(earlier) == {"calls": 3, "crops": 9, "ms": 250, "reused": 4}
    assert earlier == coral_identity.EmbedStats(calls=2, crops=5, ms=100.0, reused=7), "a snapshot is a copy"
    assert stats.since(stats.snapshot()) == {"calls": 0, "crops": 0, "ms": 0, "reused": 0}


async def test_stats_count_requests_new_crops_and_reused_embeddings() -> None:
    store = _FakeStore()
    store.training_features = [("Kitty", _kitty_features())] * 3
    store.training_rows_needed = [{"uid": "t1", "body_bytes": b"body", "face_bytes": b"face"}]
    client = _FakeClient(
        health_ok=True,
        vectors={coral_identity.CORAL_BODY_MODEL_ID: [1.0, 0.0], coral_identity.CORAL_FACE_MODEL_ID: [0.0, 1.0]},
    )
    recognizer = _recognizer(store, client)

    assert await recognizer.async_rebuild() is True

    stats = recognizer.stats
    # One request per model however many images it carried; the one row needing embedding added
    # a body and a face crop; the other two of the three gallery rows reused a cached embedding.
    assert (stats.calls, stats.crops, stats.reused) == (2, 2, 2)
    assert stats.ms >= 0

    store.training_rows_needed = []  # everything is cached now
    await recognizer.async_rebuild()
    assert (stats.calls, stats.crops, stats.reused) == (2, 2, 5)

    # Classifying one event embeds its one new body crop and reuses the two photos already cached.
    store.event_rows_needed = {"e1": [{"uid": "s1", "body_bytes": b"body", "face_bytes": None}]}
    store.event_features = {"e1": [_kitty_features()] * 3}
    assert await recognizer.async_classify_event("e1") is not None
    assert (stats.calls, stats.crops, stats.reused) == (3, 3, 7)


async def test_a_failed_embedding_request_counts_as_a_request_but_embeds_no_crops() -> None:
    class _EmbedDown(_FakeClient):
        async def embed(self, model: str, images: list[bytes]) -> list[list[float]] | None:
            self.embed_calls.append((model, len(images)))
            self.last_error = "embed failed"
            return None

    store = _CallRecordingStore()
    recognizer = _recognizer(store, _EmbedDown())

    ok = await recognizer._embed_rows("training", [{"uid": "t1", "body_bytes": b"b", "face_bytes": b"f"}])

    assert ok is False
    assert (recognizer.stats.calls, recognizer.stats.crops) == (2, 0)
    assert store.set_calls == [], "nothing was embedded, so nothing is written"


# --- the embedding cache is written once per batch ----------------------------------------------


async def test_embed_rows_writes_every_vector_in_one_store_call() -> None:
    body_model, face_model = coral_identity.CORAL_BODY_MODEL_ID, coral_identity.CORAL_FACE_MODEL_ID
    store = _CallRecordingStore()
    client = _FakeClient(health_ok=True, vectors={body_model: [1.0, 0.0], face_model: [0.0, 1.0]})
    recognizer = _recognizer(store, client)
    rows = [
        {"uid": "t1", "body_bytes": b"b1", "face_bytes": b"f1"},
        {"uid": "t2", "body_bytes": b"b2", "face_bytes": None},
        {"uid": "t3", "body_bytes": None, "face_bytes": b"f3"},
    ]

    assert await recognizer._embed_rows("training", rows) is True

    assert client.embed_calls == [(body_model, 2), (face_model, 2)], "one request per model"
    assert len(store.set_calls) == 1, "one write for the whole batch, however many vectors it carries"
    row_kind, items = store.set_calls[0]
    assert row_kind == "training"
    assert sorted((uid, crop_kind, model_id) for uid, crop_kind, model_id, _blob in items) == [
        ("t1", "body", body_model),
        ("t1", "face", face_model),
        ("t2", "body", body_model),
        ("t3", "face", face_model),
    ]


async def test_embed_rows_keeps_the_vectors_it_did_get_when_the_other_model_fails() -> None:
    class _FaceModelDown(_FakeClient):
        async def embed(self, model: str, images: list[bytes]) -> list[list[float]] | None:
            if model == coral_identity.CORAL_FACE_MODEL_ID:
                self.embed_calls.append((model, len(images)))
                self.last_error = "face model failed"
                return None
            return await super().embed(model, images)

    store = _CallRecordingStore()
    recognizer = _recognizer(store, _FaceModelDown(vectors={coral_identity.CORAL_BODY_MODEL_ID: [1.0, 0.0]}))
    rows = [{"uid": "t1", "body_bytes": b"b1", "face_bytes": b"f1"}, {"uid": "t2", "body_bytes": b"b2", "face_bytes": None}]

    ok = await recognizer._embed_rows("training", rows)

    assert ok is False, "a caller backs off when either model's batch failed"
    assert len(store.set_calls) == 1
    assert [(uid, crop_kind) for uid, crop_kind, _model, _blob in store.set_calls[0][1]] == [
        ("t1", "body"),
        ("t2", "body"),
    ]
    assert (recognizer.stats.calls, recognizer.stats.crops) == (2, 2)


# --- rebuild: timings, why it failed, and an emptied gallery ------------------------------------


async def test_a_healthy_rebuild_times_every_stage() -> None:
    store = _FakeStore()
    store.training_features = [("Kitty", _kitty_features())]
    recognizer = _recognizer(store, _FakeClient(health_ok=True))

    await recognizer.async_rebuild()

    assert set(recognizer.timings) == {"health_ms", "embed_ms", "load_ms", "build_ms", "loo_ms"}
    assert all(isinstance(ms, int) and ms >= 0 for ms in recognizer.timings.values())


async def test_a_rebuild_that_cannot_reach_coralhub_times_only_the_health_check() -> None:
    store = _FakeStore()
    store.training_features = [("Kitty", _kitty_features())]
    client = _FakeClient(health_ok=True)
    recognizer = _recognizer(store, client)
    await recognizer.async_rebuild()

    client._health_ok = False
    await recognizer.async_rebuild()

    assert set(recognizer.timings) == {"health_ms"}, "the figures describe the last rebuild only"


async def test_a_rebuild_with_nothing_to_build_from_times_up_to_loading_the_gallery() -> None:
    recognizer = _recognizer(_FakeStore(), _FakeClient(health_ok=True))

    await recognizer.async_rebuild()

    assert set(recognizer.timings) == {"health_ms", "embed_ms", "load_ms"}


async def test_rebuild_error_says_why_coralhub_was_unreachable_and_clears_once_it_is_back() -> None:
    store = _FakeStore()
    store.training_features = [("Kitty", _kitty_features())]
    client = _FakeClient(health_ok=False)
    recognizer = _recognizer(store, client)
    assert recognizer.rebuild_error is None, "nothing has been tried yet"

    assert await recognizer.async_rebuild() is False
    assert recognizer.rebuild_error == "unreachable", "the health check's own words"

    client._health_ok = True
    assert await recognizer.async_rebuild() is True
    assert recognizer.rebuild_error is None


async def test_rebuild_error_has_a_default_reason_when_the_health_check_gave_none() -> None:
    class _MuteClient(_FakeClient):
        async def health(self) -> HealthStatus:
            return HealthStatus(ok=False)

    recognizer = _recognizer(_FakeStore(), _MuteClient())

    assert await recognizer.async_rebuild() is False
    assert recognizer.rebuild_error == "CoralHub did not answer its health check"


async def test_an_emptied_gallery_rebuild_returns_false_and_drops_the_previous_model() -> None:
    """Every photo was removed: the removed photos' centroids (and their accuracy figures) must
    not keep answering for a gallery that no longer has them."""
    store = _FakeStore()
    store.training_features = [("Kitty", _kitty_features())] * coral_identity.MIN_LOO_SAMPLES
    store.event_features = {"e1": [_kitty_features()]}
    recognizer = _recognizer(store, _FakeClient(health_ok=True))
    assert await recognizer.async_rebuild() is True
    assert recognizer.available is True
    assert recognizer.loo_accuracy("Kitty") is not None
    assert await recognizer.async_classify_event("e1") is not None

    store.training_features = []

    assert await recognizer.async_rebuild() is False
    assert recognizer.available is False
    assert recognizer.model is None
    assert recognizer.loo_accuracy("Kitty") is None
    assert await recognizer.async_classify_event("e1") is None, "so the histogram recognizer takes over"
