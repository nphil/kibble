"""Cat identity engine: appearance descriptors, face-embedding handling and a distance-weighted
k-NN classifier fusing both across samples of a track.

Pure CPU code -- numpy and Pillow only, both of which ship with the Home Assistant runtime.
Callers on the event loop MUST run everything here through `hass.async_add_executor_job`;
nothing in this module is async itself.

Design: docs/36-ai-pipeline.md, "Identity engine".
"""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

# --- Constants (the one block referenced by the design doc) -----------------------------------

MODE_DAY = "day"
MODE_IR = "ir"
NOT_A_CAT = "not_a_cat"

APPEARANCE_SIZE = 96  # crop is resized to this square before any histogram is built

# Day descriptor: joint HSV histogram + luminance histogram + gradient-orientation histogram.
HSV_HUE_BINS = 8
HSV_SAT_BINS = 3
HSV_VAL_BINS = 4
HSV_DIM = HSV_HUE_BINS * HSV_SAT_BINS * HSV_VAL_BINS  # 96

# Shared between day and IR.
LUMA_BINS = 16
GRADIENT_BINS = 8

# IR descriptor: luminance histogram + uniform-LBP histogram + gradient-orientation histogram.
# 10 bins = rotation-invariant uniform LBP over 8 neighbours: 9 "uniform" bins keyed by the
# popcount of the thresholded ring (0..8 ones) plus 1 catch-all bin for non-uniform patterns
# (more than 2 circular 0/1 transitions).
LBP_BINS = 10
LBP_NON_UNIFORM_BIN = 9
_LBP_OFFSETS = ((-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1))

FEATURE_DIM_DAY = HSV_DIM + LUMA_BINS + GRADIENT_BINS  # 120
FEATURE_DIM_IR = LUMA_BINS + LBP_BINS + GRADIENT_BINS  # 34

# Center weighting: pixels near the middle of the 96x96 crop count more than pixels near the
# edge (background/other-cat contamination lives at the edges). Gaussian falloff, not a hard
# crop, so legs/tail/ears reaching toward the edge still contribute something. Calibrated in
# tools/eval_identity.py: 0.4-0.6 all beat 0.8+, 0.5 chosen as the middle of that flat region
# (this same weight also covers body crops, which no calibration sample set covers yet).
_CENTER_SIGMA_FRAC = 0.5

# Night-vision detection: IR/low-light captures are effectively grayscale (R == G == B), so the
# mean per-pixel chroma (max channel - min channel) collapses toward zero. A *low* threshold is
# essential: tools/eval_identity.py's real gallery showed a hard grayscale spike at chroma < 2
# (true IR) and a separate, gradually-rising day population starting around chroma 8-10 -- a
# black cat's own day photos can read as low as chroma ~10 purely because black fur carries
# little color regardless of lighting. A threshold anywhere near that day population (the doc's
# illustrative default was 14) misclassifies black-cat day photos as IR and fragments her
# already-scarce training data across the wrong mode. 6.0 sits in the empty valley between the
# two populations.
CHROMA_IR_THRESHOLD = 6.0

# Face embeddings: 512-D float32, L2-normalised, 2048 raw little-endian bytes on the wire.
FACE_EMB_DIM = 512
FACE_EMB_BYTES = FACE_EMB_DIM * 4

# k-NN. Used directly for body_feat and face_feat (appearance); see CLASS_BALANCED_* below for
# why face_emb uses a different scorer.
K_NEIGHBORS = 5
DISTANCE_EPS = 1e-3  # inverse-distance weight = 1 / (distance + DISTANCE_EPS); not sensitive

# face_emb's scorer: per class, the mean distance of that class's own CLASS_BALANCED_M nearest
# training samples (fewer if the class has less than that), turned into a probability with a
# softmax at this temperature. Unlike K_NEIGHBORS' global vote, every class nominates its own
# candidates regardless of its training-set size, so a 15-sample class is not swamped by a
# 144-sample one in the neighbour pool. tools/eval_identity.py's real-gallery LOO, face_emb in
# isolation: this alone took accuracy for a minority-enrolled, hard-to-embed cat from 6.7% to
# 60.0% (a global k=5 vote over 144 Kitty vs 15 Pancake samples leant on Kitty by sheer density).
# A grid search over CLASS_BALANCED_M in 2..7 and CLASS_BALANCED_TEMPERATURE in 0.01..0.2 for the
# *fused* decision never beat the global scorer on that cat's accuracy once averaged with
# appearance, so the swap is face_emb-only: m=2, T=0.01 ties her fused accuracy exactly while
# cutting her wrong-rate 26.7% -> 20.0%, and is strictly better-or-equal on every other class and
# the overall wrong rate too (see tools/eval_identity.py output).
CLASS_BALANCED_M = 2
CLASS_BALANCED_TEMPERATURE = 0.01

# Floor applied to every per-modality, per-class k-NN vote before it is logged and averaged.
# 1e-6 (effectively no floor) let a single modality whose 5 nearest neighbours all happened to
# be one class output *exactly* 1.0 for that class -- with a training set this size (144 Kitty
# vs 15 Pancake), that happens to the majority class often enough that wrong calls saturated to
# the same ~1.0 confidence as right ones, so no gate threshold could tell them apart. Raised
# until wrong-rate stopped improving without also erasing Pancake's correct calls (see
# tools/eval_identity.py: floor 0.025-0.03 is the knee; 0.035+ starts trading her correct calls
# for unknowns with no further wrong-rate gain).
LOG_PROB_FLOOR = 0.03

# Decision gates -- identical for a cat name and for NOT_A_CAT. The doc's defaults, unchanged:
# once CHROMA_IR_THRESHOLD and LOG_PROB_FLOOR were fixed these turned out to already sit at the
# knee of the accuracy/wrong-rate trade-off (tools/eval_identity.py sweep of top in 0.6..0.9).
DECISION_TOP_THRESHOLD = 0.75
DECISION_MARGIN = 0.2

# Leave-one-out accuracy needs at least this many samples of a class to mean anything.
MIN_LOO_SAMPLES = 5

_INV_SQRT2 = 1.0 / math.sqrt(2.0)


def _center_weight(size: int) -> np.ndarray:
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float64)
    c = (size - 1) / 2.0
    r2 = (ys - c) ** 2 + (xs - c) ** 2
    sigma = size * _CENTER_SIGMA_FRAC
    return np.exp(-r2 / (2.0 * sigma * sigma))


_CENTER_WEIGHT = _center_weight(APPEARANCE_SIZE)
_CENTER_WEIGHT_INTERIOR = _CENTER_WEIGHT[1:-1, 1:-1]  # LBP needs a 1px border for its 3x3 ring


# --- Appearance descriptor ----------------------------------------------------------------------


def _l1_normalize(hist: np.ndarray) -> np.ndarray:
    total = hist.sum()
    if total <= 0:
        return np.zeros_like(hist, dtype=np.float32)
    return (hist / total).astype(np.float32)


def _luma(rgb: np.ndarray) -> np.ndarray:
    """ITU-R BT.601 luma from an (H, W, 3) float array in 0..255."""
    return 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]


def _detect_mode(rgb: np.ndarray) -> str:
    maxc = rgb.max(axis=-1)
    minc = rgb.min(axis=-1)
    chroma = float((maxc - minc).mean())
    return MODE_IR if chroma < CHROMA_IR_THRESHOLD else MODE_DAY


def _hsv_hist(rgb_img: Image.Image, weight: np.ndarray) -> np.ndarray:
    hsv = np.asarray(rgb_img.convert("HSV"), dtype=np.int64)
    h = (hsv[..., 0] * HSV_HUE_BINS) // 256
    s = (hsv[..., 1] * HSV_SAT_BINS) // 256
    v = (hsv[..., 2] * HSV_VAL_BINS) // 256
    idx = (h * HSV_SAT_BINS + s) * HSV_VAL_BINS + v
    hist = np.bincount(idx.ravel(), weights=weight.ravel(), minlength=HSV_DIM)[:HSV_DIM]
    return _l1_normalize(hist)


def _luma_hist(gray: np.ndarray, weight: np.ndarray) -> np.ndarray:
    idx = np.clip((gray.astype(np.int64) * LUMA_BINS) // 256, 0, LUMA_BINS - 1)
    hist = np.bincount(idx.ravel(), weights=weight.ravel(), minlength=LUMA_BINS)[:LUMA_BINS]
    return _l1_normalize(hist)


def _gradient_hist(gray: np.ndarray, weight: np.ndarray) -> np.ndarray:
    gy, gx = np.gradient(gray)
    magnitude = np.hypot(gy, gx)
    # Unsigned orientation (mod pi): texture direction matters, dark-to-light vs. light-to-dark
    # does not.
    angle = np.arctan2(gy, gx) % np.pi
    idx = np.clip((angle / np.pi * GRADIENT_BINS).astype(np.int64), 0, GRADIENT_BINS - 1)
    hist = np.bincount(idx.ravel(), weights=(magnitude * weight).ravel(), minlength=GRADIENT_BINS)
    return _l1_normalize(hist[:GRADIENT_BINS])


def _lbp_hist(gray: np.ndarray, weight: np.ndarray) -> np.ndarray:
    h, w = gray.shape
    center = gray[1:-1, 1:-1]
    bits = np.empty((*center.shape, 8), dtype=np.uint8)
    for i, (dy, dx) in enumerate(_LBP_OFFSETS):
        neighbor = gray[1 + dy : h - 1 + dy, 1 + dx : w - 1 + dx]
        bits[..., i] = neighbor >= center
    shifted = np.roll(bits, -1, axis=-1)
    transitions = np.sum(np.abs(bits.astype(np.int16) - shifted.astype(np.int16)), axis=-1)
    popcount = np.sum(bits, axis=-1)
    bin_idx = np.where(transitions <= 2, popcount, LBP_NON_UNIFORM_BIN).astype(np.int64)
    hist = np.bincount(bin_idx.ravel(), weights=weight.ravel(), minlength=LBP_BINS)[:LBP_BINS]
    return _l1_normalize(hist)


def appearance(jpeg: bytes) -> tuple[np.ndarray, str]:
    """Center-weighted appearance descriptor for one crop, resized to 96x96 first.

    Returns `(descriptor, mode)` where `mode` is `MODE_DAY` or `MODE_IR` depending on the
    crop's own mean chroma, and `descriptor` is a float32 vector whose length depends on that
    mode (`FEATURE_DIM_DAY` or `FEATURE_DIM_IR`) -- every histogram block inside it is
    individually L1-normalised, so two descriptors of the same mode are comparable with
    `_hellinger` out of the box. Raises `ValueError` when `jpeg` cannot be decoded as an image.
    """
    try:
        with Image.open(io.BytesIO(jpeg)) as im:
            im.load()
            rgb_img = im.convert("RGB").resize(
                (APPEARANCE_SIZE, APPEARANCE_SIZE), Image.Resampling.BILINEAR
            )
    except Exception as exc:  # noqa: BLE001 - any decode failure becomes one clean ValueError
        raise ValueError(f"undecodable appearance crop: {exc}") from exc

    rgb = np.asarray(rgb_img, dtype=np.float64)
    gray = _luma(rgb)
    mode = _detect_mode(rgb)
    luma_h = _luma_hist(gray, _CENTER_WEIGHT)
    grad_h = _gradient_hist(gray, _CENTER_WEIGHT)
    if mode == MODE_DAY:
        hsv_h = _hsv_hist(rgb_img, _CENTER_WEIGHT)
        vec = np.concatenate([hsv_h, luma_h, grad_h])
    else:
        lbp_h = _lbp_hist(gray, _CENTER_WEIGHT_INTERIOR)
        vec = np.concatenate([luma_h, lbp_h, grad_h])
    return vec.astype(np.float32), mode


# Baseline "is there anything here at all" gate for an arbitrary uploaded photo, independent of
# whether a trained not_a_cat class exists yet (a fresh install has none). A solid-colour capture
# (a bad screenshot, a lens-covered shot, a mis-picked file) has almost no luma variance once
# downsampled; any real photo -- cat or not -- has far more texture than this. Deliberately low:
# this only catches "nothing was photographed", never "the wrong subject was photographed" (that
# is what the classifier gate and human review are for).
BLANK_LUMA_STD_THRESHOLD = 3.0
_BLANK_PROBE_SIZE = 32


def is_blank_image(jpeg: bytes) -> bool:
    """True for an undecodable image or one whose downsampled luminance is almost uniform.
    Used by the upload pipeline as the always-available half of the "no cat" reject gate --
    the other half, the trained classifier's own `not_a_cat` verdict, only exists once there is
    training data to learn it from."""
    try:
        with Image.open(io.BytesIO(jpeg)) as im:
            im.load()
            small = im.convert("L").resize((_BLANK_PROBE_SIZE, _BLANK_PROBE_SIZE), Image.Resampling.BILINEAR)
    except Exception:  # noqa: BLE001 - undecodable is treated the same as "nothing usable"
        return True
    return float(np.asarray(small, dtype=np.float64).std()) < BLANK_LUMA_STD_THRESHOLD


# --- Face embeddings -----------------------------------------------------------------------------


def load_embedding(raw: bytes | None) -> np.ndarray | None:
    """2048 raw little-endian float32 bytes -> L2-normalised (512,) float32, else `None`."""
    if raw is None or len(raw) != FACE_EMB_BYTES:
        return None
    vec = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    if not np.all(np.isfinite(vec)):
        return None
    norm = float(np.linalg.norm(vec))
    if norm < 1e-6:
        return None
    return (vec / norm).astype(np.float32)


# --- Features --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Features:
    """One sample's features. Any field may be `None` when that input was missing or
    undecodable. `mode` describes the sample as a whole (from the body crop when it decoded,
    else the face crop), and is what both `body_feat` and `face_feat` are compared against when
    looking up training data of the same mode."""

    face_emb: np.ndarray | None
    body_feat: np.ndarray | None
    face_feat: np.ndarray | None
    mode: str | None


def features_from(
    body_jpeg: bytes | None, face_jpeg: bytes | None, face_emb: bytes | None
) -> Features:
    """Builds `Features` from raw asset bytes. Never raises: an undecodable body or face crop
    just becomes `None` for that field rather than failing the whole sample."""
    body_feat: np.ndarray | None = None
    body_mode: str | None = None
    if body_jpeg:
        try:
            body_feat, body_mode = appearance(body_jpeg)
        except ValueError:
            body_feat, body_mode = None, None

    face_feat: np.ndarray | None = None
    face_mode: str | None = None
    if face_jpeg:
        try:
            face_feat, face_mode = appearance(face_jpeg)
        except ValueError:
            face_feat, face_mode = None, None

    emb = load_embedding(face_emb) if face_emb else None
    mode = body_mode if body_mode is not None else face_mode
    return Features(face_emb=emb, body_feat=body_feat, face_feat=face_feat, mode=mode)


def pack(a: np.ndarray | None) -> bytes | None:
    """Float32 blob for SQLite storage: little-endian, no header."""
    if a is None:
        return None
    return np.ascontiguousarray(a, dtype="<f4").tobytes()


def unpack(b: bytes | None) -> np.ndarray | None:
    if b is None:
        return None
    return np.frombuffer(b, dtype="<f4").astype(np.float32)


# --- Distances -------------------------------------------------------------------------------


def _hellinger(a: np.ndarray, b: np.ndarray) -> float:
    """Hellinger distance between two vectors made of L1-normalised histogram blocks
    concatenated together. Because each block sums to 1 on its own, this is exactly the L2 norm
    of the vector of per-block Hellinger distances -- blocks contribute equally regardless of
    their bin count."""
    diff = np.sqrt(np.clip(a, 0, None)) - np.sqrt(np.clip(b, 0, None))
    return float(np.sqrt(np.sum(diff * diff)) * _INV_SQRT2)


def _hellinger_batch(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    diff = np.sqrt(np.clip(matrix, 0, None)) - np.sqrt(np.clip(query, 0, None))[None, :]
    return np.sqrt(np.sum(diff * diff, axis=1)) * _INV_SQRT2


def _cosine_distance_batch(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """`query` and every row of `matrix` are already L2-normalised, so the dot product is the
    cosine similarity directly."""
    return 1.0 - (matrix @ query)


# Near-duplicate cutoff shared by upload dedupe (store.py) and auto-learn diversity selection
# (autolearn.py): two body/face appearance descriptors of the same mode whose Hellinger distance
# falls below this are treated as "the same shot again", not new information. No calibration
# gallery covers this yet (the same honest gap `_modality_log_probs`' body_feat comment notes for
# body crops) -- 0.12 is a starting default, a fifth of the [0, 1] Hellinger range, picked to
# catch near-identical burst frames without also rejecting a genuinely different pose. Revisit
# with tools/eval_identity.py once upload/auto-learn data accumulates.
NEAR_DUPLICATE_DISTANCE = 0.12


def nearest_distance(query: np.ndarray, pool: Sequence[np.ndarray]) -> float | None:
    """The smallest Hellinger distance from `query` to any vector in `pool`, or `None` when
    `pool` is empty or every entry has a different dimensionality (a mode mismatch -- day and
    IR descriptors are never comparable). Pure and side-effect-free so upload dedupe and
    auto-learn diversity selection can both call it directly against whatever pool they build."""
    comparable = [p for p in pool if p.shape == query.shape]
    if not comparable:
        return None
    matrix = np.stack(comparable).astype(np.float32)
    return float(_hellinger_batch(query, matrix).min())


# --- k-NN + fusion -----------------------------------------------------------------------------


def _global_knn_scores(
    query: np.ndarray,
    matrix: np.ndarray,
    labels: list[str],
    classes: list[str],
    distance_batch,
) -> dict[str, float] | None:
    """Distance-weighted k-NN class scores over `classes`, or `None` when nothing is comparable
    (empty pool, or a dimension mismatch between `query` and `matrix` -- appearance vectors of
    different modes never compare, and a stale pool is treated as absent rather than crashing).
    A global vote over the whole pool: classes with more training samples get more candidates
    in the neighbourhood purely by density. Used for body_feat and face_feat, where a real grid
    search found no benefit from class-balancing (see CLASS_BALANCED_* above)."""
    n = matrix.shape[0]
    if n == 0 or matrix.shape[1] != query.shape[0]:
        return None
    distances = distance_batch(query, matrix)
    k_eff = min(K_NEIGHBORS, n)
    nearest = np.argpartition(distances, k_eff - 1)[:k_eff]
    weights = 1.0 / (distances[nearest] + DISTANCE_EPS)
    scores = dict.fromkeys(classes, 0.0)
    for i, w in zip(nearest, weights, strict=True):
        lbl = labels[i]
        if lbl in scores:
            scores[lbl] += float(w)
    total = sum(scores.values())
    if total <= 0:
        return None
    return {c: v / total for c, v in scores.items()}


def _class_balanced_scores(
    query: np.ndarray,
    matrix: np.ndarray,
    labels: list[str],
    classes: list[str],
    distance_batch,
) -> dict[str, float] | None:
    """Per-class scores immune to how many training samples each class happens to have. For
    every class with at least one sample in the pool, take the mean distance of its own
    `CLASS_BALANCED_M` nearest samples (fewer if it has less than that), then turn the resulting
    per-class mean distances into a probability with a temperature-scaled softmax. A class
    absent from the pool gets 0.0, same as `_global_knn_scores`. Used for face_emb only -- see
    CLASS_BALANCED_M's comment for why."""
    n = matrix.shape[0]
    if n == 0 or matrix.shape[1] != query.shape[0]:
        return None
    distances = distance_batch(query, matrix)
    labels_arr = np.asarray(labels)
    mean_distance: dict[str, float] = {}
    for c in classes:
        class_distances = distances[labels_arr == c]
        if class_distances.size == 0:
            continue
        m_eff = min(CLASS_BALANCED_M, class_distances.size)
        nearest = np.partition(class_distances, m_eff - 1)[:m_eff]
        mean_distance[c] = float(nearest.mean())
    if not mean_distance:
        return None
    present = list(mean_distance.keys())
    neg_scaled = -np.array([mean_distance[c] for c in present]) / CLASS_BALANCED_TEMPERATURE
    exp = np.exp(neg_scaled - neg_scaled.max())
    probs = exp / exp.sum()
    scores = dict.fromkeys(classes, 0.0)
    for c, p in zip(present, probs, strict=True):
        scores[c] = float(p)
    return scores


def to_log_probs(scores: dict[str, float], classes: Sequence[str], floor: float) -> np.ndarray:
    """One modality's per-class probability dict -> a log-probability vector over `classes`,
    each floored at `floor` before the log so a single saturated modality (every neighbour of
    one class, scoring it exactly 1.0) can never veto or singlehandedly dominate a fused
    decision (see `LOG_PROB_FLOOR`'s own calibration note for how the histogram recognizer's
    floor was picked). Shared by `identity.Model` and `coral_identity.CoralModel`: both fuse
    across modalities and samples the same way -- this function and `fuse_log_probs` below are
    that shared mechanism -- only how each modality's own per-class scores are computed first
    (k-NN distance vote here, cosine-to-centroid over there) differs."""
    p = np.array([max(scores.get(c, 0.0), floor) for c in classes], dtype=np.float64)
    return np.log(p)


def fuse_log_probs(log_prob_vectors: Sequence[np.ndarray]) -> np.ndarray:
    """Mean log-probability across modalities/samples, turned back into a probability
    distribution with a max-subtracted softmax (numerically stable, and equivalent to a
    geometric-mean fusion of the per-modality probabilities). Shared by `identity.Model` and
    `coral_identity.CoralModel` -- see `to_log_probs` above."""
    avg = np.mean(np.stack(list(log_prob_vectors), axis=0), axis=0)
    p = np.exp(avg - avg.max())
    return p / p.sum()


@dataclass(frozen=True, slots=True)
class Verdict:
    label: str | None
    confidence: float | None
    per_sample: list[tuple[str | None, float | None]] = field(default_factory=list)


class Model:
    """Trains three independent modality pools (face embedding, body appearance, face
    appearance) from `training`, scores each with the scorer `_modality_log_probs` picks for
    it, then fuses across samples and modalities per track by averaging class
    log-probabilities."""

    def __init__(self, training: list[tuple[str, Features]]) -> None:
        self._training = list(training)
        self.classes: list[str] = sorted({label for label, _ in self._training})

        emb_vecs: list[np.ndarray] = []
        emb_labels: list[str] = []
        body_by_mode: dict[str, tuple[list[np.ndarray], list[str]]] = {}
        face_by_mode: dict[str, tuple[list[np.ndarray], list[str]]] = {}

        for label, feat in self._training:
            if feat.face_emb is not None:
                emb_vecs.append(feat.face_emb)
                emb_labels.append(label)
            if feat.body_feat is not None and feat.mode is not None:
                vecs, labels = body_by_mode.setdefault(feat.mode, ([], []))
                vecs.append(feat.body_feat)
                labels.append(label)
            if feat.face_feat is not None and feat.mode is not None:
                vecs, labels = face_by_mode.setdefault(feat.mode, ([], []))
                vecs.append(feat.face_feat)
                labels.append(label)

        self._face_emb_matrix = (
            np.stack(emb_vecs).astype(np.float32) if emb_vecs else np.zeros((0, FACE_EMB_DIM), np.float32)
        )
        self._face_emb_labels = emb_labels
        self._body_pools = {
            mode: (np.stack(vecs).astype(np.float32), labels) for mode, (vecs, labels) in body_by_mode.items()
        }
        self._face_pools = {
            mode: (np.stack(vecs).astype(np.float32), labels) for mode, (vecs, labels) in face_by_mode.items()
        }

    def _to_log_probs(self, scores: dict[str, float]) -> np.ndarray:
        return to_log_probs(scores, self.classes, LOG_PROB_FLOOR)

    def _modality_log_probs(self, feat: Features) -> list[np.ndarray]:
        """One log-probability vector over `self.classes` per available, comparable modality.
        face_emb uses `_class_balanced_scores` (see CLASS_BALANCED_M); body_feat and face_feat
        use `_global_knn_scores`. body_feat has no calibration data yet -- no body crop exists
        anywhere in the real gallery tools/eval_identity.py was tuned against -- so it defaults
        to the same global scorer as face_feat rather than an unvalidated guess. If body_feat
        turns out to have the same small-class-swamped-by-a-large-one problem once live body
        crops accumulate, re-run tools/eval_identity.py's grid search against real body crops
        and switch its call below to `_class_balanced_scores` the same way face_emb's is."""
        out: list[np.ndarray] = []

        if feat.face_emb is not None:
            scores = _class_balanced_scores(
                feat.face_emb, self._face_emb_matrix, self._face_emb_labels, self.classes, _cosine_distance_batch
            )
            if scores is not None:
                out.append(self._to_log_probs(scores))

        if feat.body_feat is not None and feat.mode is not None:
            pool = self._body_pools.get(feat.mode)
            if pool is not None:
                scores = _global_knn_scores(feat.body_feat, pool[0], pool[1], self.classes, _hellinger_batch)
                if scores is not None:
                    out.append(self._to_log_probs(scores))

        if feat.face_feat is not None and feat.mode is not None:
            pool = self._face_pools.get(feat.mode)
            if pool is not None:
                scores = _global_knn_scores(feat.face_feat, pool[0], pool[1], self.classes, _hellinger_batch)
                if scores is not None:
                    out.append(self._to_log_probs(scores))

        return out

    @staticmethod
    def _fuse(log_prob_vectors: list[np.ndarray]) -> np.ndarray:
        return fuse_log_probs(log_prob_vectors)

    def classify(self, samples: list[Features]) -> Verdict:
        per_sample: list[tuple[str | None, float | None]] = []
        all_log_probs: list[np.ndarray] = []

        for feat in samples:
            modality_logs = self._modality_log_probs(feat)
            if modality_logs:
                dist = self._fuse(modality_logs)
                idx = int(np.argmax(dist))
                per_sample.append((self.classes[idx], float(dist[idx])))
                all_log_probs.extend(modality_logs)
            else:
                per_sample.append((None, None))

        if not self.classes or not all_log_probs:
            return Verdict(label=None, confidence=None, per_sample=per_sample)

        fused = self._fuse(all_log_probs)
        order = np.argsort(fused)[::-1]
        top_idx = int(order[0])
        top_p = float(fused[top_idx])
        second_p = float(fused[order[1]]) if len(order) > 1 else 0.0

        if top_p >= DECISION_TOP_THRESHOLD and (top_p - second_p) >= DECISION_MARGIN:
            return Verdict(label=self.classes[top_idx], confidence=top_p, per_sample=per_sample)
        return Verdict(label=None, confidence=None, per_sample=per_sample)

    def class_distribution(self, samples: list[Features]) -> list[tuple[str, float]] | None:
        """Same per-sample-then-fused shape as `classify` (fuse each sample's own
        modalities, then fuse across samples) but returns the full fused class distribution
        instead of gating it to one winner. `IdentityEngine` uses this for per-subject session
        scoring. `None` under the exact same condition `classify` itself falls back to an
        inconclusive verdict for: no trained classes, or not one sample had any comparable
        modality at all."""
        all_log_probs: list[np.ndarray] = []
        for feat in samples:
            all_log_probs.extend(self._modality_log_probs(feat))
        if not self.classes or not all_log_probs:
            return None
        fused = self._fuse(all_log_probs)
        return list(zip(self.classes, (float(p) for p in fused), strict=True))

    def loo_accuracy(self) -> dict[str, float | None]:
        """Leave-one-out accuracy per class, through the exact same gated `classify()` decision
        used in production. `None` when a class has fewer than `MIN_LOO_SAMPLES` samples."""
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
                fold = Model(reduced)
                verdict = fold.classify([self._training[i][1]])
                if verdict.label == cls:
                    correct += 1
            result[cls] = correct / len(rows)
        return result
