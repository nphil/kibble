"""Gated auto-learning: pure decision logic for what the identity engine may add to the
training set on its own, with no human in the loop, and when it must stop doing that for one
cat. Nothing here touches SQLite or the event loop -- `store.py` owns persistence
(`review_outcomes`/`auto_learn_state`, `add_auto_training`), `ingest.py`'s `IdentityEngine`
orchestrates by calling both. Keeping the gates here as plain functions over plain data is what
makes them unit-testable without a database or Home Assistant at all.

Design, per the five gates the feature was specified against:

1. Confidence + consistency ("session gate", `session_label`): a session (one closed event) only
   qualifies when at least `AUTO_LEARN_MIN_CONSISTENT_SAMPLES` of its samples independently
   agree on the same cat, each at or above `AUTO_LEARN_MIN_CONFIDENCE` -- deliberately a much
   stricter bar than `identity.DECISION_TOP_THRESHOLD` (0.75/0.2 margin), which only has to be
   right often enough to be a *useful guess*; a sample the engine adds to its own training set
   has to be right almost all the time, or the model teaches itself its own mistakes.
2. Never contradicted by a human (`ingest.py`'s caller, not this module): only ever invoked for
   an event that is closed and *not* `reviewed` -- a human's later correction simply never
   raises this path for that event again, and a per-sample override (`review IS NOT NULL`)
   already excludes that one sample regardless of the event's own state.
3. Diversity (`select_diverse`): greedily keeps a candidate only when it is not a near-duplicate
   (`identity.NEAR_DUPLICATE_DISTANCE`) of the existing pool *or* of an already-kept candidate
   earlier in the same batch -- so a session's own six near-identical frames do not all land.
4. Rolling accuracy + pause/resume (`next_paused_state`): every human review of an event the
   engine had already auto-classified is one outcome (hit/miss) for that cat. Hysteresis, not a
   single threshold, on purpose: pausing at <70% and only resuming above 85% keeps a cat's
   accuracy oscillating around 75-80% from flapping on and off every other review.
5. Never a judge verdict either (`is_judge_sourced`, `ingest.py`'s caller): an event whose
   current `cat` was set by the vision judge (docs/40-vision-judge.md), not the local
   recognizer, never feeds the training set it will later be graded against -- the judge is a
   second opinion for a human to see, not a teacher for the classifier it is meant to
   double-check.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from . import identity

# A session must be this confident, this many times over, before it teaches itself -- see the
# module docstring's gate 1.
AUTO_LEARN_MIN_CONFIDENCE = 0.9
AUTO_LEARN_MIN_CONSISTENT_SAMPLES = 3

# Rolling human-review accuracy, per cat. `AUTO_LEARN_MIN_REVIEWS` guards the window from acting
# on noise (one unlucky review must never pause a cat that has otherwise never been wrong); the
# pause/resume gap is deliberate hysteresis -- see the module docstring's gate 4.
ROLLING_REVIEW_WINDOW = 20
AUTO_LEARN_MIN_REVIEWS = 5
AUTO_LEARN_PAUSE_BELOW = 0.70
AUTO_LEARN_RESUME_ABOVE = 0.85


def session_label(per_sample: Sequence[tuple[str | None, float | None]]) -> str | None:
    """The cat every auto-learn sample from this session would be labelled as, or `None` if the
    session does not qualify. Counts, per candidate label, how many samples named it at or above
    `AUTO_LEARN_MIN_CONFIDENCE`; the label needs at least `AUTO_LEARN_MIN_CONSISTENT_SAMPLES`
    such votes and must be the *only* label clearing that bar -- two cats each confidently named
    a few times in the same session is contradiction, not consistency, and qualifies neither."""
    counts: dict[str, int] = {}
    for label, confidence in per_sample:
        if label is None or label == identity.NOT_A_CAT or confidence is None:
            continue
        if confidence >= AUTO_LEARN_MIN_CONFIDENCE:
            counts[label] = counts.get(label, 0) + 1
    qualifying = [label for label, n in counts.items() if n >= AUTO_LEARN_MIN_CONSISTENT_SAMPLES]
    return qualifying[0] if len(qualifying) == 1 else None


def is_judge_sourced(cat: str | None, judge_cat: str | None, judge_at: int | None) -> bool:
    """Whether an event's current `cat` was set by the vision judge rather than the local
    recognizer -- gate 5 above. `judge_at` (set) together with `judge_cat == cat` is the only
    signal: the judge's own rule (b) (docs/40-vision-judge.md) is the one path that ever writes
    `cat` to an enrolled name it did not get from local classification, and it always sets both
    columns together, so this can never be true from a local `auto` classification alone."""
    return judge_at is not None and judge_cat is not None and judge_cat == cat


def select_diverse(candidate_feats: Sequence[np.ndarray | None], existing_feats: Sequence[np.ndarray]) -> list[int]:
    """Indices into `candidate_feats` worth adding to training: each is kept only if it is not a
    near-duplicate (`identity.NEAR_DUPLICATE_DISTANCE`) of `existing_feats` or of a candidate
    already accepted earlier in this same call -- diversity against history *and* within one
    burst of qualifying samples. A `None` candidate (no comparable feature at all -- e.g. a
    sample whose crop never decoded) is never kept: there is nothing to compare it against, so
    it cannot be shown to add anything new."""
    kept: list[int] = []
    pool = list(existing_feats)
    for i, feat in enumerate(candidate_feats):
        if feat is None:
            continue
        dist = identity.nearest_distance(feat, pool)
        if dist is None or dist >= identity.NEAR_DUPLICATE_DISTANCE:
            kept.append(i)
            pool.append(feat)
    return kept


def next_paused_state(currently_paused: bool, recent_outcomes: Sequence[bool]) -> bool:
    """Whether auto-learning should be paused for a cat *after* its most recent review outcome,
    given whether it was already paused and its last `ROLLING_REVIEW_WINDOW` outcomes (oldest
    first or any order -- only the count and the mean matter). Fewer than `AUTO_LEARN_MIN_REVIEWS`
    outcomes never changes the current state: not enough evidence either way yet. Otherwise,
    hysteresis: an active cat pauses once its rolling accuracy drops below
    `AUTO_LEARN_PAUSE_BELOW`; a paused cat only resumes once it climbs above
    `AUTO_LEARN_RESUME_ABOVE`. Pure function of the outcome history -- callers own persisting the
    result and computing that history from `review_outcomes`."""
    if len(recent_outcomes) < AUTO_LEARN_MIN_REVIEWS:
        return currently_paused
    accuracy = sum(1 for ok in recent_outcomes if ok) / len(recent_outcomes)
    if currently_paused:
        return accuracy < AUTO_LEARN_RESUME_ABOVE
    return accuracy < AUTO_LEARN_PAUSE_BELOW


# Saturation: once a cat has plenty of samples AND the classifier is already near-perfect
# against real human reviews, additional auto-learned samples add little and just cost disk/
# compute -- auto-learn should taper off on its own once a cat is "well learned" rather than
# accumulate forever.
SATURATION_MIN_SAMPLES = 60
SATURATION_ACCURACY = 0.98


def is_saturated(total_samples: int, rolling_accuracy: float | None) -> bool:
    """Whether a cat has "enough" training: at least `SATURATION_MIN_SAMPLES` samples AND a
    rolling human-reviewed accuracy at or above `SATURATION_ACCURACY`. Below either bar, a new
    diverse sample is still worth adding -- still building up, or still improving."""
    return (
        total_samples >= SATURATION_MIN_SAMPLES
        and rolling_accuracy is not None
        and rolling_accuracy >= SATURATION_ACCURACY
    )


def learning_state(*, paused: bool, saturated: bool) -> str:
    """The per-cat learning indicator: cached once per cat on `store.identity_summary`'s
    `CatStats.learning_state`, read from there by both `sensor.py`'s `KibbleCatLastSeenSensor`
    (an HA attribute for dashboards/automations) and `websocket.py`'s `kibble/cats` (the card's
    own minimal indicator) -- one computation, two consumers, never two answers. `"paused"`
    (rolling accuracy dropped -- see `next_paused_state`) takes priority over `"learned"`
    (saturated), which takes priority over the default `"learning"`."""
    if paused:
        return "paused"
    if saturated:
        return "learned"
    return "learning"


# The per-cat recognition sensor's own window -- wider than `ROLLING_REVIEW_WINDOW` (pause/
# resume needs to react fast to a real regression; a displayed percentage can average over
# more history) but the same underlying `review_outcomes` data, per cat, at a different LIMIT
# -- one table, two windows, never two separate accuracy computations that could disagree.
RECOGNITION_REVIEW_WINDOW = 30
# Below this many real human data points, a rolling accuracy is too noisy to show as "the"
# number -- fall back to the classifier's own mean confidence as a labelled estimate instead.
MIN_HUMAN_DATA_POINTS = 5


def recognition_score(
    *, human_outcomes: Sequence[bool], auto_confidences: Sequence[float], training_samples: int
) -> tuple[int, str]:
    """The per-cat recognition percentage (0..100) and its `basis` (`"reviews"` or
    `"estimate"`). `human_outcomes`: up to `RECOGNITION_REVIEW_WINDOW` hit/miss flags from real
    human review (event-level `kibble/label` or per-sample `kibble/sample/label` corrections --
    the same `review_outcomes` table `next_paused_state` reads). `auto_confidences`: up to
    `RECOGNITION_REVIEW_WINDOW` recent per-sample guess confidences for this cat, used only
    before there is enough human ground truth. Coverage (`min(1, training_samples /
    SATURATION_MIN_SAMPLES)`) scales either signal down so a handful of perfect samples never
    reads as "fully learned" -- deliberately the same `SATURATION_MIN_SAMPLES` auto-learn's own
    saturation gate uses: a cat this sensor calls "ready to stop training" is the same cat
    auto-learn itself would stop adding samples for."""
    coverage = min(1.0, training_samples / SATURATION_MIN_SAMPLES)
    if len(human_outcomes) >= MIN_HUMAN_DATA_POINTS:
        accuracy = sum(1 for ok in human_outcomes if ok) / len(human_outcomes)
        return max(0, min(100, round(100 * accuracy * coverage))), "reviews"
    if auto_confidences:
        mean_confidence = sum(auto_confidences) / len(auto_confidences)
        return max(0, min(100, round(100 * mean_confidence * coverage))), "estimate"
    return 0, "estimate"


def overall_recognition_score(scores: Sequence[int]) -> int | None:
    """The device-wide recognition sensor's value: the weakest link, not an average -- "turn
    training off once it is near full" means every enrolled cat, not most of them. `None` with
    no cats enrolled at all."""
    return min(scores) if scores else None
