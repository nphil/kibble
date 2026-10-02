"""`autolearn.py`'s pure gates: confidence+consistency (`session_label`), diversity
(`select_diverse`), rolling-accuracy pause/resume hysteresis (`next_paused_state`), and the
judge-sourced exclusion (`is_judge_sourced`). No database, no Home Assistant -- these are plain
functions over plain data, exactly like `store.py`'s `pick_thumb`/`_select_eviction_candidates`.
"""

from __future__ import annotations

import numpy as np
import pytest

from kibble import autolearn, identity

# --- session_label: confidence + consistency gate ----------------------------------------------


def test_session_label_qualifies_with_enough_confident_consistent_samples() -> None:
    per_sample = [("Kitty", 0.95), ("Kitty", 0.92), ("Kitty", 0.91)]
    assert autolearn.session_label(per_sample) == "Kitty"


def test_session_label_is_none_under_the_consistent_sample_count() -> None:
    per_sample = [("Kitty", 0.95), ("Kitty", 0.92)]  # only 2, needs 3
    assert autolearn.session_label(per_sample) is None


def test_session_label_is_none_when_a_qualifying_sample_is_below_the_confidence_floor() -> None:
    per_sample = [("Kitty", 0.95), ("Kitty", 0.92), ("Kitty", 0.5)]
    assert autolearn.session_label(per_sample) is None


def test_session_label_is_none_when_two_cats_each_qualify() -> None:
    """Contradiction, not consistency: two different cats each confidently named a few times in
    the same session must qualify neither -- this is exactly what "never contradicted" guards
    against inside a single session."""
    per_sample = [
        ("Kitty", 0.95), ("Kitty", 0.93), ("Kitty", 0.91),
        ("Pancake", 0.95), ("Pancake", 0.93), ("Pancake", 0.91),
    ]
    assert autolearn.session_label(per_sample) is None


def test_session_label_ignores_not_a_cat_and_none_entries() -> None:
    per_sample = [
        (identity.NOT_A_CAT, 0.99), (None, None),
        ("Kitty", 0.95), ("Kitty", 0.93), ("Kitty", 0.91),
    ]
    assert autolearn.session_label(per_sample) == "Kitty"


def test_session_label_of_an_empty_session_is_none() -> None:
    assert autolearn.session_label([]) is None


# --- select_diverse: near-duplicate rejection against history and within one batch -------------


def test_select_diverse_rejects_a_near_duplicate_of_an_existing_sample() -> None:
    existing = np.array([0.9, 0.1], dtype=np.float32)
    near_dup = np.array([0.89, 0.11], dtype=np.float32)
    far = np.array([0.1, 0.9], dtype=np.float32)
    kept = autolearn.select_diverse([near_dup, far], [existing])
    assert kept == [1]


def test_select_diverse_rejects_a_repeat_within_the_same_batch() -> None:
    a = np.array([0.5, 0.5], dtype=np.float32)
    kept = autolearn.select_diverse([a, a], [])
    assert kept == [0]


def test_select_diverse_skips_a_candidate_with_no_feature() -> None:
    far = np.array([0.1, 0.9], dtype=np.float32)
    kept = autolearn.select_diverse([None, far], [])
    assert kept == [1]


def test_select_diverse_keeps_everything_when_history_is_empty_and_batch_is_varied() -> None:
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert autolearn.select_diverse([a, b], []) == [0, 1]


# --- next_paused_state: hysteresis over rolling review accuracy ---------------------------------


def test_next_paused_state_is_unchanged_under_the_minimum_review_count() -> None:
    assert autolearn.next_paused_state(False, [True] * 4) is False
    assert autolearn.next_paused_state(True, [False] * 4) is True


def test_next_paused_state_pauses_an_active_cat_whose_accuracy_drops() -> None:
    mostly_wrong = [False] * 16 + [True] * 4  # 20% accuracy
    assert autolearn.next_paused_state(False, mostly_wrong) is True


def test_next_paused_state_does_not_pause_above_the_pause_threshold() -> None:
    mostly_right = [True] * 15 + [False] * 5  # 75% -- above PAUSE_BELOW (0.70)
    assert autolearn.next_paused_state(False, mostly_right) is False


def test_next_paused_state_resumes_a_paused_cat_once_accuracy_recovers_above_resume() -> None:
    very_right = [True] * 18 + [False] * 2  # 90% -- above RESUME_ABOVE (0.85)
    assert autolearn.next_paused_state(True, very_right) is False


def test_next_paused_state_hysteresis_keeps_a_paused_cat_paused_in_the_gap_between_thresholds() -> None:
    """75% is above PAUSE_BELOW but below RESUME_ABOVE -- a cat already paused stays paused; a
    cat still active stays active. The gap is the point: it stops a cat oscillating around one
    single threshold from flapping pause/resume every other review."""
    mid = [True] * 15 + [False] * 5  # 75%
    assert autolearn.next_paused_state(True, mid) is True
    assert autolearn.next_paused_state(False, mid) is False


@pytest.mark.parametrize("outcomes", [[True] * 5, [False] * 5, [True, False] * 10])
def test_next_paused_state_never_raises_on_any_review_history_shape(outcomes: list[bool]) -> None:
    autolearn.next_paused_state(False, outcomes)
    autolearn.next_paused_state(True, outcomes)


# --- recognition_score: reviews basis, estimate fallback, coverage cap -------------------------


def test_recognition_score_uses_reviews_basis_once_enough_human_data_points_exist() -> None:
    outcomes = [True, True, True, True, False]  # 5 points, 80% accurate
    score, basis = autolearn.recognition_score(human_outcomes=outcomes, auto_confidences=[], training_samples=60)
    assert basis == "reviews"
    assert score == 80  # coverage is 1.0 at >= SATURATION_MIN_SAMPLES (60)


def test_recognition_score_coverage_caps_a_handful_of_perfect_samples_below_100() -> None:
    """5 perfect reviews on only 5 training samples must NOT read 100% -- coverage
    (5/60) scales it down; "a cat with 5 perfect samples doesn't read 100%" is the literal
    acceptance bar this test pins."""
    outcomes = [True] * 5
    score, basis = autolearn.recognition_score(human_outcomes=outcomes, auto_confidences=[], training_samples=5)
    assert basis == "reviews"
    assert score < 100
    assert score == round(100 * 1.0 * (5 / autolearn.SATURATION_MIN_SAMPLES))




def test_recognition_score_falls_back_to_estimate_under_the_human_data_point_floor() -> None:
    outcomes = [True, True]  # only 2 -- under MIN_HUMAN_DATA_POINTS (5)
    confidences = [0.9, 0.85, 0.95]
    score, basis = autolearn.recognition_score(human_outcomes=outcomes, auto_confidences=confidences, training_samples=60)
    assert basis == "estimate"
    assert score == round(100 * (sum(confidences) / len(confidences)) * 1.0)


def test_recognition_score_is_zero_estimate_with_no_data_at_all() -> None:
    score, basis = autolearn.recognition_score(human_outcomes=[], auto_confidences=[], training_samples=0)
    assert (score, basis) == (0, "estimate")


def test_recognition_score_estimate_also_respects_the_coverage_cap() -> None:
    confidences = [1.0, 1.0, 1.0]
    score, basis = autolearn.recognition_score(human_outcomes=[], auto_confidences=confidences, training_samples=6)
    assert basis == "estimate"
    assert score == round(100 * 1.0 * (6 / autolearn.SATURATION_MIN_SAMPLES))
    assert score < 100


@pytest.mark.parametrize("training_samples", [-5, 0, 1_000_000])
def test_recognition_score_never_raises_or_leaves_the_0_100_range(training_samples: int) -> None:
    score, _basis = autolearn.recognition_score(
        human_outcomes=[True] * 10, auto_confidences=[0.9] * 10, training_samples=training_samples
    )
    assert 0 <= score <= 100


# --- overall_recognition_score: the minimum across cats, not an average ------------------------


def test_overall_recognition_score_is_the_minimum_not_the_average() -> None:
    assert autolearn.overall_recognition_score([95, 40, 100]) == 40


def test_overall_recognition_score_is_none_with_no_cats() -> None:
    assert autolearn.overall_recognition_score([]) is None


def test_overall_recognition_score_of_a_single_cat_is_that_cats_own_score() -> None:
    assert autolearn.overall_recognition_score([77]) == 77


# --- is_judge_sourced: gate 5, never a judge verdict either --------------------------------------


def test_is_judge_sourced_is_true_when_the_judge_set_the_current_cat() -> None:
    assert autolearn.is_judge_sourced("Kitty", "Kitty", 1_700_000_000) is True


def test_is_judge_sourced_is_false_when_the_judge_named_a_different_cat() -> None:
    """A stale judge verdict for a DIFFERENT cat than the one currently set (the local
    recognizer moved on since) must never block a legitimately local `cat`."""
    assert autolearn.is_judge_sourced("Kitty", "Pancake", 1_700_000_000) is False


def test_is_judge_sourced_is_false_when_never_judged() -> None:
    assert autolearn.is_judge_sourced("Kitty", None, None) is False


def test_is_judge_sourced_is_false_without_a_judge_at_timestamp_even_if_names_match() -> None:
    """`judge_cat`/`judge_at` are always written together by `apply_judge_verdict` -- this is a
    defensive companion, not a state `store.py` itself ever actually produces."""
    assert autolearn.is_judge_sourced("Kitty", "Kitty", None) is False
