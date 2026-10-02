"""The cat identity engine: appearance-descriptor determinism and day/IR mode detection,
Hellinger/cosine distance sanity, `Model.classify` decision behaviour (separable clusters,
genuine ambiguity, NOT_A_CAT, missing modalities), `loo_accuracy`'s sample-count gate, and the
`pack`/`unpack`/`load_embedding` byte-level round trips.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from kibble import identity
from PIL import Image

# --- fixtures ----------------------------------------------------------------------------------


def _jpeg(arr_uint8: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr_uint8, mode="RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _solid_noisy(rng: np.random.Generator, rgb: tuple[int, int, int], size: int = 96, noise: float = 10) -> np.ndarray:
    arr = np.full((size, size, 3), rgb, dtype=np.float32)
    arr += rng.normal(0, noise, size=(size, size, 3))
    return np.clip(arr, 0, 255).astype(np.uint8)


def _gray_noisy(rng: np.random.Generator, base: float, size: int = 96, noise: float = 15) -> np.ndarray:
    g = np.clip(base + rng.normal(0, noise, size=(size, size)), 0, 255).astype(np.uint8)
    return np.stack([g, g, g], axis=-1)


_FEAT_DIM = 6


def _jittered_onehot(i: int, rng: np.random.Generator, *, mass: float = 0.85, jitter: float = 0.01) -> np.ndarray:
    """A valid L1-normalised, non-negative histogram-like vector clustered near basis vector
    `i`. Distinct per call (via `rng`) so k-NN never has to break an exact floating-point tie."""
    v = np.full(_FEAT_DIM, (1 - mass) / (_FEAT_DIM - 1), dtype=np.float64)
    v[i] = mass
    v = np.clip(v + rng.normal(0, jitter, size=_FEAT_DIM), 1e-4, None)
    return (v / v.sum()).astype(np.float32)


def _feat(
    body: np.ndarray | None = None,
    face: np.ndarray | None = None,
    emb: np.ndarray | None = None,
    mode: str | None = identity.MODE_DAY,
) -> identity.Features:
    return identity.Features(face_emb=emb, body_feat=body, face_feat=face, mode=mode)


# --- appearance(): determinism and day/IR mode ---------------------------------------------------


def test_appearance_is_deterministic_for_the_same_bytes() -> None:
    jpeg = _jpeg(_solid_noisy(np.random.default_rng(1), (180, 100, 60)))
    vec1, mode1 = identity.appearance(jpeg)
    vec2, mode2 = identity.appearance(jpeg)
    assert mode1 == mode2
    assert np.array_equal(vec1, vec2)


def test_appearance_detects_ir_for_a_grayscale_crop() -> None:
    jpeg = _jpeg(_gray_noisy(np.random.default_rng(2), base=100))
    vec, mode = identity.appearance(jpeg)
    assert mode == identity.MODE_IR
    assert len(vec) == identity.FEATURE_DIM_IR


def test_appearance_detects_day_for_a_saturated_color_crop() -> None:
    jpeg = _jpeg(_solid_noisy(np.random.default_rng(3), (210, 90, 40)))
    vec, mode = identity.appearance(jpeg)
    assert mode == identity.MODE_DAY
    assert len(vec) == identity.FEATURE_DIM_DAY


def test_appearance_raises_value_error_on_undecodable_bytes() -> None:
    with pytest.raises(ValueError):
        identity.appearance(b"this is not an image")


def test_appearance_blocks_are_l1_normalised() -> None:
    """Each concatenated histogram block sums to 1 on its own (a prerequisite for `_hellinger`
    treating the whole vector as a sum of independent per-block Hellinger distances)."""
    jpeg = _jpeg(_solid_noisy(np.random.default_rng(4), (200, 60, 120)))
    vec, mode = identity.appearance(jpeg)
    assert mode == identity.MODE_DAY
    hsv, luma, grad = vec[: identity.HSV_DIM], vec[identity.HSV_DIM : identity.HSV_DIM + identity.LUMA_BINS], vec[-identity.GRADIENT_BINS :]
    for block in (hsv, luma, grad):
        assert float(block.sum()) == pytest.approx(1.0, abs=1e-5)


# --- distances -----------------------------------------------------------------------------------


def test_hellinger_distance_is_zero_for_identical_vectors() -> None:
    a = np.array([0.5, 0.5, 0.0], dtype=np.float32)
    assert identity._hellinger(a, a) == pytest.approx(0.0, abs=1e-6)


def test_hellinger_distance_is_symmetric() -> None:
    a = np.array([0.5, 0.5, 0.0], dtype=np.float32)
    b = np.array([0.0, 0.2, 0.8], dtype=np.float32)
    assert identity._hellinger(a, b) == pytest.approx(identity._hellinger(b, a))


def test_hellinger_distance_between_disjoint_distributions_is_one() -> None:
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert identity._hellinger(a, b) == pytest.approx(1.0, abs=1e-6)


def test_cosine_distance_batch_matches_known_angles() -> None:
    query = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    matrix = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]], dtype=np.float32)
    got = identity._cosine_distance_batch(query, matrix)
    np.testing.assert_allclose(got, [0.0, 1.0, 2.0], atol=1e-6)


# --- per-modality scorers: _global_knn_scores (body/face appearance) vs ------------------------
# --- _class_balanced_scores (face embedding) ----------------------------------------------------


def _vec_at_cosine_distance(d: float) -> np.ndarray:
    """A 2D unit vector whose cosine distance from `(1, 0)` is exactly `d`."""
    theta = np.arccos(1 - d)
    return np.array([np.cos(theta), np.sin(theta)], dtype=np.float32)


def _imbalanced_scorer_scenario() -> tuple[np.ndarray, np.ndarray, list[str], list[str]]:
    """A query, a training matrix/labels/classes where "Small" (2 samples) is genuinely closer
    to the query on its own two nearest members than "Big" (4 samples) is on its own two
    nearest, but "Big" claims 3 of the global top-5 nearest slots (2 Small + 3 Big, all 6 points
    used) and so out-votes Small under plain distance-weighted k-NN. Every number below is
    reachable by hand: mean(0.02, 0.025) = 0.0225 < mean(0.028, 0.032) = 0.030, confirming
    Small is the closer class; global weights are 1/(d+DISTANCE_EPS) summed per class."""
    query = np.array([1.0, 0.0], dtype=np.float32)
    big = np.stack([_vec_at_cosine_distance(d) for d in (0.028, 0.032, 0.038, 0.042)])
    small = np.stack([_vec_at_cosine_distance(d) for d in (0.02, 0.025)])
    matrix = np.concatenate([big, small], axis=0)
    labels = ["Big"] * 4 + ["Small"] * 2
    return query, matrix, labels, ["Big", "Small"]


def test_global_knn_scores_lets_a_large_class_outvote_a_closer_small_one() -> None:
    """Documents the bias `_class_balanced_scores` exists to fix: plain distance-weighted k-NN
    over the whole pool favours "Big" even though "Small" is closer on its own nearest members,
    simply because "Big" claims more of the global top-`K_NEIGHBORS` slots."""
    query, matrix, labels, classes = _imbalanced_scorer_scenario()
    scores = identity._global_knn_scores(query, matrix, labels, classes, identity._cosine_distance_batch)
    assert scores["Big"] > scores["Small"]


def test_class_balanced_scores_favors_the_genuinely_closer_small_class() -> None:
    query, matrix, labels, classes = _imbalanced_scorer_scenario()
    scores = identity._class_balanced_scores(query, matrix, labels, classes, identity._cosine_distance_batch)
    assert scores["Small"] > scores["Big"]


def test_class_balanced_scores_is_none_for_an_empty_pool() -> None:
    query = np.array([1.0, 0.0], dtype=np.float32)
    empty = np.zeros((0, 2), dtype=np.float32)
    assert identity._class_balanced_scores(query, empty, [], ["A", "B"], identity._cosine_distance_batch) is None


def test_class_balanced_scores_is_none_on_a_dimension_mismatch() -> None:
    query = np.array([1.0, 0.0, 0.0], dtype=np.float32)  # 3D query against a 2D pool
    matrix = np.array([[1.0, 0.0]], dtype=np.float32)
    got = identity._class_balanced_scores(query, matrix, ["A"], ["A"], identity._cosine_distance_batch)
    assert got is None


def test_class_balanced_scores_uses_fewer_than_m_for_a_smaller_class() -> None:
    """A class with only one sample still gets scored (`CLASS_BALANCED_M` is a ceiling, not a
    requirement) instead of being skipped."""
    query, matrix, labels, classes = _imbalanced_scorer_scenario()
    # drop one of "Small"'s two samples so it only has one left
    matrix, labels = matrix[:-1], labels[:-1]
    scores = identity._class_balanced_scores(query, matrix, labels, classes, identity._cosine_distance_batch)
    assert scores is not None
    assert scores["Small"] > scores["Big"]


def test_model_classify_uses_the_class_balanced_scorer_for_face_emb() -> None:
    """End-to-end through `Model.classify`: the same imbalanced scenario, now as `face_emb`
    training data, resolves to the genuinely closer minority class -- confirming `Model` really
    does route face_emb through `_class_balanced_scores` and not `_global_knn_scores`. The
    scenario is engineered to flip which class wins, not to clear the 0.75 decision gate, so
    this checks the per-sample (ungated) guess rather than the track-level verdict label."""
    query, matrix, labels, _classes = _imbalanced_scorer_scenario()
    training = [(lbl, _feat(emb=vec)) for lbl, vec in zip(labels, matrix, strict=True)]
    model = identity.Model(training)
    verdict = model.classify([_feat(emb=query)])
    assert verdict.per_sample == [("Small", pytest.approx(0.6791791927022334))]


# --- Model.classify: separable clusters, ambiguity, NOT_A_CAT, missing modalities ----------------


def test_classify_picks_the_right_class_on_clearly_separable_real_crops() -> None:
    """End-to-end through the real `appearance()` pipeline, not hand-built vectors: two
    distinctly coloured synthetic "cats" with per-sample noise, held-out queries from each."""

    def samples(rgb: tuple[int, int, int], n: int, seed: int) -> list[identity.Features]:
        rng = np.random.default_rng(seed)
        return [
            identity.features_from(body_jpeg=_jpeg(_solid_noisy(rng, rgb)), face_jpeg=None, face_emb=None)
            for _ in range(n)
        ]

    ginger = samples((210, 110, 40), 8, seed=10)
    slate = samples((70, 90, 130), 8, seed=20)
    model = identity.Model([("Ginger", f) for f in ginger] + [("Slate", f) for f in slate])

    ginger_query = samples((210, 110, 40), 1, seed=30)[0]
    slate_query = samples((70, 90, 130), 1, seed=40)[0]

    ginger_verdict = model.classify([ginger_query])
    slate_verdict = model.classify([slate_query])
    assert ginger_verdict.label == "Ginger"
    assert ginger_verdict.confidence >= identity.DECISION_TOP_THRESHOLD
    assert slate_verdict.label == "Slate"
    assert slate_verdict.confidence >= identity.DECISION_TOP_THRESHOLD


def test_classify_is_unknown_when_a_tracks_samples_contradict_each_other() -> None:
    """Each sample alone is a confident, correct match for a *different* class. Log-probability
    averaging across samples then makes the track itself genuinely ambiguous -- this is the
    fusion rule at work, not a threshold-tuning artifact."""
    rng = np.random.default_rng(50)
    train_a = [_feat(body=_jittered_onehot(0, rng)) for _ in range(6)]
    train_b = [_feat(body=_jittered_onehot(1, rng)) for _ in range(6)]
    model = identity.Model([("A", f) for f in train_a] + [("B", f) for f in train_b])

    sample_favors_a = _feat(body=_jittered_onehot(0, np.random.default_rng(51)))
    sample_favors_b = _feat(body=_jittered_onehot(1, np.random.default_rng(52)))

    # each sample alone is confidently, correctly resolved
    assert model.classify([sample_favors_a]).label == "A"
    assert model.classify([sample_favors_b]).label == "B"

    verdict = model.classify([sample_favors_a, sample_favors_b])
    assert verdict.label is None
    assert verdict.confidence is None
    # per_sample stays the individual, ungated best guess for each sample, in order
    assert verdict.per_sample[0][0] == "A"
    assert verdict.per_sample[1][0] == "B"


def test_classify_returns_not_a_cat_with_the_same_gates_as_a_real_cat() -> None:
    rng = np.random.default_rng(60)
    train = (
        [("Kitty", _feat(body=_jittered_onehot(0, rng))) for _ in range(6)]
        + [("Pancake", _feat(body=_jittered_onehot(1, rng))) for _ in range(6)]
        + [(identity.NOT_A_CAT, _feat(body=_jittered_onehot(2, rng))) for _ in range(6)]
    )
    model = identity.Model(train)
    query = _feat(body=_jittered_onehot(2, np.random.default_rng(61)))
    verdict = model.classify([query])
    assert verdict.label == identity.NOT_A_CAT
    assert verdict.confidence >= identity.DECISION_TOP_THRESHOLD


def test_classify_uses_whichever_modalities_a_sample_actually_has() -> None:
    """Training only has body_feat data; a query with only body_feat still resolves, a sample
    with every modality missing contributes nothing but does not break the rest of the track."""
    rng = np.random.default_rng(70)
    train = [("Kitty", _feat(body=_jittered_onehot(0, rng))) for _ in range(6)] + [
        ("Pancake", _feat(body=_jittered_onehot(1, rng))) for _ in range(6)
    ]
    model = identity.Model(train)

    body_only_query = _feat(body=_jittered_onehot(0, np.random.default_rng(71)))
    assert model.classify([body_only_query]).label == "Kitty"

    empty = _feat(body=None, face=None, emb=None, mode=None)
    mixed = model.classify([body_only_query, empty])
    assert mixed.label == "Kitty"
    assert mixed.per_sample[1] == (None, None)

    all_empty = model.classify([empty, empty])
    assert all_empty.label is None
    assert all_empty.confidence is None
    assert all_empty.per_sample == [(None, None), (None, None)]


# --- Model degenerate cases -----------------------------------------------------------------------


def test_classify_with_no_training_data_is_unknown() -> None:
    model = identity.Model([])
    assert model.classes == []
    verdict = model.classify([_feat(body=_jittered_onehot(0, np.random.default_rng(80)))])
    assert verdict.label is None
    assert verdict.confidence is None
    assert verdict.per_sample == [(None, None)]


def test_classify_with_a_single_training_sample_does_not_crash() -> None:
    model = identity.Model([("Solo", _feat(body=_jittered_onehot(0, np.random.default_rng(81), mass=0.9)))])
    verdict = model.classify([_feat(body=_jittered_onehot(0, np.random.default_rng(82), mass=0.95))])
    assert verdict.label == "Solo"


def test_loo_accuracy_is_none_under_five_samples_and_a_real_number_otherwise() -> None:
    rng = np.random.default_rng(90)
    few = [_feat(body=_jittered_onehot(0, rng)) for _ in range(3)]
    many = [_feat(body=_jittered_onehot(1, rng)) for _ in range(6)]
    model = identity.Model([("Few", f) for f in few] + [("Many", f) for f in many])
    acc = model.loo_accuracy()
    assert acc["Few"] is None
    assert acc["Many"] == pytest.approx(1.0)


# --- load_embedding / features_from / pack / unpack -----------------------------------------------


def test_load_embedding_normalises_a_valid_512d_vector() -> None:
    raw = np.random.default_rng(100).standard_normal(identity.FACE_EMB_DIM).astype("<f4").tobytes()
    emb = identity.load_embedding(raw)
    assert emb is not None
    assert emb.shape == (identity.FACE_EMB_DIM,)
    assert float(np.linalg.norm(emb)) == pytest.approx(1.0, abs=1e-5)


@pytest.mark.parametrize(
    "raw",
    [
        b"\x00" * 100,  # wrong length
        None,
        b"\x00" * identity.FACE_EMB_BYTES,  # all-zero, cannot be normalised
    ],
)
def test_load_embedding_returns_none_for_invalid_input(raw: bytes | None) -> None:
    assert identity.load_embedding(raw) is None


def test_load_embedding_returns_none_when_the_vector_has_a_nan() -> None:
    vec = np.random.default_rng(101).standard_normal(identity.FACE_EMB_DIM).astype(np.float32)
    vec[0] = np.nan
    assert identity.load_embedding(vec.astype("<f4").tobytes()) is None


def test_features_from_with_every_input_missing_is_all_none() -> None:
    feat = identity.features_from(None, None, None)
    assert feat == identity.Features(face_emb=None, body_feat=None, face_feat=None, mode=None)


def test_features_from_prefers_the_body_crops_mode() -> None:
    body = _jpeg(_solid_noisy(np.random.default_rng(110), (200, 90, 40)))  # day
    face = _jpeg(_gray_noisy(np.random.default_rng(111), base=100))  # ir
    feat = identity.features_from(body, face, None)
    assert feat.mode == identity.MODE_DAY
    assert feat.body_feat is not None
    assert feat.face_feat is not None


def test_features_from_falls_back_to_the_face_crops_mode_without_a_body_crop() -> None:
    face = _jpeg(_gray_noisy(np.random.default_rng(112), base=100))
    feat = identity.features_from(None, face, None)
    assert feat.mode == identity.MODE_IR
    assert feat.body_feat is None


def test_features_from_never_raises_on_an_undecodable_crop() -> None:
    good_face = _jpeg(_solid_noisy(np.random.default_rng(113), (200, 90, 40)))
    feat = identity.features_from(b"garbage bytes", good_face, None)
    assert feat.body_feat is None  # undecodable crop -> None, not an exception
    assert feat.face_feat is not None  # the other crop still decodes


def test_pack_unpack_round_trips_a_float32_vector() -> None:
    vec = np.random.default_rng(120).standard_normal(37).astype(np.float32)
    restored = identity.unpack(identity.pack(vec))
    assert restored is not None
    np.testing.assert_allclose(restored, vec)


def test_pack_unpack_round_trips_a_loaded_embedding() -> None:
    raw = np.random.default_rng(121).standard_normal(identity.FACE_EMB_DIM).astype("<f4").tobytes()
    emb = identity.load_embedding(raw)
    restored = identity.unpack(identity.pack(emb))
    np.testing.assert_allclose(restored, emb)


def test_pack_and_unpack_of_none_are_none() -> None:
    assert identity.pack(None) is None
    assert identity.unpack(None) is None


# --- dataclass shape -------------------------------------------------------------------------------


def test_features_and_verdict_are_frozen() -> None:
    feat = identity.Features(face_emb=None, body_feat=None, face_feat=None, mode=None)
    with pytest.raises(AttributeError):
        feat.mode = identity.MODE_DAY  # type: ignore[misc]
    verdict = identity.Verdict(label=None, confidence=None, per_sample=[])
    with pytest.raises(AttributeError):
        verdict.label = "x"  # type: ignore[misc]


# --- is_blank_image / nearest_distance: upload-pipeline gates ----------------------------------


def test_is_blank_image_true_for_a_solid_colour_capture() -> None:
    jpeg = _jpeg(np.full((96, 96, 3), 200, dtype=np.uint8))
    assert identity.is_blank_image(jpeg) is True


def test_is_blank_image_true_for_undecodable_bytes() -> None:
    assert identity.is_blank_image(b"not an image") is True


def test_is_blank_image_false_for_real_multiscale_texture() -> None:
    """Deliberately multi-octave, not per-pixel iid noise: real photo texture (fur, edges) has
    spatial structure at more than one scale, which is what survives `is_blank_image`'s 32x32
    downsample probe -- see `test_upload_views.py`'s `_jpeg_bytes` docstring for the same point
    proven the other way (pure per-pixel noise does NOT survive it)."""
    rng = np.random.default_rng(5)
    base = np.full((96, 96, 3), 150.0)
    for scale in (4, 8, 16, 32):
        small = rng.normal(0, 40, size=(max(96 // scale, 1), max(96 // scale, 1), 3))
        layer = np.array(
            Image.fromarray(np.clip(small + 128, 0, 255).astype(np.uint8)).resize((96, 96), Image.BILINEAR),
            dtype=np.float64,
        ) - 128
        base += layer / 3
    arr = np.clip(base, 0, 255).astype(np.uint8)
    assert identity.is_blank_image(_jpeg(arr)) is False


def test_nearest_distance_finds_the_closest_comparable_vector() -> None:
    query = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    close = np.array([0.9, 0.1, 0.0], dtype=np.float32)
    far = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    dist = identity.nearest_distance(query, [far, close])
    assert dist == pytest.approx(identity._hellinger(query, close))


def test_nearest_distance_ignores_dimension_mismatched_entries() -> None:
    query = np.array([1.0, 0.0], dtype=np.float32)
    wrong_dim = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert identity.nearest_distance(query, [wrong_dim]) is None


def test_nearest_distance_is_none_for_an_empty_pool() -> None:
    query = np.array([1.0, 0.0], dtype=np.float32)
    assert identity.nearest_distance(query, []) is None
