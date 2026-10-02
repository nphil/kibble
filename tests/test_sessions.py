"""`sessions.py`'s pure split planner: `plan_split`'s run-detection and boundary rules. No
database, no Home Assistant -- plain functions over plain data, exactly like
`test_autolearn.py`'s own coverage of `autolearn.py`.
"""

from __future__ import annotations

import pytest

from kibble import identity
from kibble.sessions import (
    IdentityScores,
    PlanningSample,
    SampleGuess,
    SubjectSpan,
    plan_parts,
    plan_split,
)


def _s(uid: str, t: int, guess: str | None, confidence: float | None) -> SampleGuess:
    return SampleGuess(uid=uid, t=t, guess=guess, confidence=confidence)


def _p(
    uid: str,
    t: int,
    sid: int | None,
    frame_k: int | None,
    guess: str | None,
    confidence: float | None = 0.95,
    *,
    review: str | None = None,
    review_src: str | None = None,
    has_crop: bool = True,
) -> PlanningSample:
    return PlanningSample(uid, t, sid, frame_k, has_crop, guess, confidence, review, review_src)


def _score(cat: str, confidence: float = 0.95) -> IdentityScores:
    return IdentityScores(((cat, confidence),), top_threshold=0.75, margin=0.2)


def _automatic_samples() -> tuple[list[PlanningSample], dict[int, IdentityScores], dict[int, SubjectSpan]]:
    # Kitty is represented by two subject fragments; all three subjects co-occur in these frames.
    samples = [
        _p("k1a", 100, 1, 10, "Kitty"), _p("k1b", 104, 1, 20, "Kitty"),
        _p("p1a", 100, 2, 10, "Pancake"), _p("p1b", 104, 2, 20, "Pancake"),
        _p("k2a", 110, 3, 10, "Kitty"), _p("k2b", 114, 3, 20, "Kitty"),
        _p("sidless", 116, None, None, "Pancake"),
    ]
    scores = {1: _score("Kitty"), 2: _score("Pancake"), 3: _score("Kitty")}
    spans = {
        1: SubjectSpan(1, 100, 108, 101),
        2: SubjectSpan(2, 100, 108, 102),
        3: SubjectSpan(3, 110, 118, None),
    }
    return samples, scores, spans


def test_a_single_cat_session_never_splits() -> None:
    samples = [_s(f"s{i}", i, "Kitty", 0.95) for i in range(6)]
    assert plan_split(samples, session_start=0, session_end=10) is None


def test_a_single_sample_outlier_run_never_splits() -> None:
    # One stray Pancake-guessed sample in the middle of an otherwise all-Kitty session -- a run
    # of length 1 can never qualify on its own.
    samples = [
        _s("s0", 0, "Kitty", 0.95),
        _s("s1", 4, "Kitty", 0.94),
        _s("s2", 8, "Pancake", 0.9),
        _s("s3", 12, "Kitty", 0.93),
        _s("s4", 16, "Kitty", 0.92),
    ]
    assert plan_split(samples, session_start=0, session_end=20) is None


def test_a_low_confidence_second_cat_run_never_splits() -> None:
    samples = [
        _s("s0", 0, "Kitty", 0.95),
        _s("s1", 4, "Kitty", 0.94),
        _s("s2", 8, "Pancake", 0.5),
        _s("s3", 12, "Pancake", 0.6),
    ]
    assert plan_split(samples, session_start=0, session_end=20) is None


def test_two_qualifying_runs_of_the_same_cat_never_splits() -> None:
    # Kitty visits, a brief no-guess dip, Kitty again -- still one cat, not evidence of a
    # second one, even though it is technically two separate runs.
    samples = [
        _s("s0", 0, "Kitty", 0.95),
        _s("s1", 4, "Kitty", 0.94),
        _s("s2", 8, None, None),
        _s("s3", 12, "Kitty", 0.93),
        _s("s4", 16, "Kitty", 0.92),
    ]
    assert plan_split(samples, session_start=0, session_end=20) is None


def test_a_not_a_cat_run_never_becomes_its_own_segment() -> None:
    samples = [
        _s("s0", 0, "Kitty", 0.95),
        _s("s1", 4, "Kitty", 0.94),
        _s("s2", 8, "not_a_cat", 0.9),
        _s("s3", 12, "not_a_cat", 0.9),
    ]
    assert plan_split(samples, session_start=0, session_end=16) is None, "only one real qualifying cat run exists (not_a_cat never counts)"


def test_two_contiguous_cat_runs_split_into_two_segments() -> None:
    samples = [
        _s("s0", 100, "Kitty", 0.94),
        _s("s1", 104, "Kitty", 0.94),
        _s("s2", 108, "Kitty", 0.94),
        _s("s3", 130, "Pancake", 0.9),
        _s("s4", 134, "Pancake", 0.9),
        _s("s5", 138, "Pancake", 0.9),
    ]
    result = plan_split(samples, session_start=100, session_end=140)
    assert result is not None
    assert [seg.cat for seg in result] == ["Kitty", "Pancake"]
    kitty, pancake = result
    boundary = (108 + 130) // 2
    assert kitty.start == 100
    assert kitty.end == boundary
    assert pancake.start == boundary
    assert pancake.end == 140
    assert set(kitty.sample_uids) == {"s0", "s1", "s2"}
    assert set(pancake.sample_uids) == {"s3", "s4", "s5"}
    assert kitty.confidence == pytest.approx(0.94)
    assert pancake.confidence == pytest.approx(0.9)


def test_three_runs_two_cats_split_into_three_chronological_segments() -> None:
    # Kitty, then Pancake, then Kitty comes back -- a real, temporally distinct second visit
    # within the same device track. Non-adjacent same-cat runs are never merged: each stays its
    # own chronological segment.
    samples = [
        _s("s0", 0, "Kitty", 0.9),
        _s("s1", 4, "Kitty", 0.9),
        _s("s2", 20, "Pancake", 0.9),
        _s("s3", 24, "Pancake", 0.9),
        _s("s4", 40, "Kitty", 0.9),
        _s("s5", 44, "Kitty", 0.9),
    ]
    result = plan_split(samples, session_start=0, session_end=48)
    assert result is not None
    assert [seg.cat for seg in result] == ["Kitty", "Pancake", "Kitty"]


def test_boundary_time_is_the_midpoint_and_an_odd_gap_favours_the_earlier_segment() -> None:
    samples = [
        _s("s0", 0, "Kitty", 0.9),
        _s("s1", 1, "Kitty", 0.9),
        _s("s2", 10, "Pancake", 0.9),
        _s("s3", 11, "Pancake", 0.9),
    ]
    # gap 1 -> 10, odd width 9, midpoint 5 (floor of 5.5) -- the earlier segment gets the tie.
    result = plan_split(samples, session_start=0, session_end=20)
    assert result is not None
    assert result[0].end == 5
    assert result[1].start == 5


def test_a_non_qualifying_interior_sample_lands_in_whichever_segment_its_own_timestamp_falls_in() -> None:
    samples = [
        _s("s0", 0, "Kitty", 0.9),
        _s("s1", 4, "Kitty", 0.9),
        _s("s2", 8, None, None),  # a single stray outlier, right at the swap
        _s("s3", 12, "Pancake", 0.9),
        _s("s4", 16, "Pancake", 0.9),
    ]
    result = plan_split(samples, session_start=0, session_end=20)
    assert result is not None
    # boundary = (4 + 12) // 2 = 8; s2's own t=8 sits exactly ON it and must land in the SECOND
    # segment (start <= t < end is the rule for an interior/last segment).
    assert "s2" in result[1].sample_uids
    assert "s2" not in result[0].sample_uids


def test_every_sample_is_assigned_to_exactly_one_segment() -> None:
    samples = [
        _s("s0", -5, "Kitty", 0.9),  # before session_start -- must still land somewhere
        _s("s1", 0, "Kitty", 0.9),
        _s("s2", 4, "Kitty", 0.9),
        _s("s3", 20, "Pancake", 0.9),
        _s("s4", 24, "Pancake", 0.9),
        _s("s5", 999, "Pancake", 0.9),  # after session_end -- must still land somewhere
    ]
    result = plan_split(samples, session_start=0, session_end=30)
    assert result is not None
    all_assigned = [uid for seg in result for uid in seg.sample_uids]
    assert sorted(all_assigned) == sorted(s.uid for s in samples)
    assert len(all_assigned) == len(set(all_assigned)), "no sample must be double-assigned"


def test_cooccurring_confident_cats_merge_same_cat_subject_fragments_and_keep_sidless_photos() -> None:
    samples, scores, spans = _automatic_samples()
    parts = plan_parts(samples, scores, spans)
    assert parts is not None
    by_cat = {part.cat: part for part in parts}
    assert set(by_cat) == {"Kitty", "Pancake"}
    assert set(by_cat["Kitty"].sample_uids) == {"k1a", "k1b", "k2a", "k2b", "sidless"}
    assert set(by_cat["Pancake"].sample_uids) == {"p1a", "p1b"}
    assigned = [uid for part in parts for uid in part.sample_uids]
    assert sorted(assigned) == sorted(sample.uid for sample in samples)
    assert len(assigned) == len(set(assigned))


def test_not_a_cat_and_unknown_guesses_never_create_automatic_parts() -> None:
    samples = [
        _p("k1", 0, 1, 10, "Kitty"), _p("k2", 1, 1, 20, "Kitty"),
        _p("junk1", 0, 2, 10, identity.NOT_A_CAT), _p("junk2", 1, 2, 20, identity.NOT_A_CAT),
        _p("unknown1", 0, 3, 10, "unknown"), _p("unknown2", 1, 3, 20, "unknown"),
    ]
    scores = {1: _score("Kitty"), 2: _score(identity.NOT_A_CAT), 3: _score("unknown")}
    assert plan_parts(samples, scores, {}) is None


def test_human_cat_set_is_exact_includes_empty_parts_and_routes_removed_cats_to_main() -> None:
    samples = [
        _p("k1", 0, 1, 10, "Kitty"), _p("k2", 1, 1, 20, "Kitty"),
        _p("p1", 0, 2, 10, "Pancake"), _p("p2", 1, 2, 20, "Pancake"),
        _p("other1", 0, 3, 10, "Muffin"), _p("other2", 1, 3, 20, "Muffin"),
        _p("sidless", 2, None, None, "Muffin"),
    ]
    scores = {1: _score("Kitty"), 2: _score("Pancake"), 3: _score("Muffin")}
    spans = {sid: SubjectSpan(sid, 0, 3, None) for sid in scores}
    parts = plan_parts(
        samples, scores, spans,
        human_cats=[("Kitty", True), ("Pancake", False), ("Pancake", False), ("Whiskers", True)],
    )
    assert parts is not None
    by_cat = {part.cat: part for part in parts}
    assert list(by_cat) == ["Kitty", "Pancake", "Whiskers"]
    assert by_cat["Kitty"].ate is True and by_cat["Pancake"].ate is False
    assert by_cat["Whiskers"].sample_uids == ()
    assert set(by_cat["Kitty"].sample_uids) == {"k1", "k2", "other1", "other2", "sidless"}

    removed = plan_parts(samples, scores, spans, human_cats=[("Kitty", True), ("Pancake", False)])
    assert removed is not None and {part.cat for part in removed} == {"Kitty", "Pancake"}
    assert "Whiskers" not in {part.cat for part in removed}


def test_photo_review_wins_over_subject_review_and_engine_decision() -> None:
    samples = [
        _p("photo", 0, 1, 10, "Kitty", review="Kitty", review_src="photo"),
        _p("subject1", 1, 1, 20, "Kitty", review="Pancake", review_src="subject"),
        _p("subject2", 2, 1, 30, "Kitty", review="Pancake", review_src="subject"),
    ]
    parts = plan_parts(
        samples, {1: _score("Kitty")}, {1: SubjectSpan(1, 0, 3, None)},
        human_cats=[("Kitty", False), ("Pancake", False)],
    )
    assert parts is not None
    by_cat = {part.cat: set(part.sample_uids) for part in parts}
    assert by_cat == {"Kitty": {"photo"}, "Pancake": {"subject1", "subject2"}}


def test_automatic_split_requires_two_cropped_photos_and_a_shared_frame() -> None:
    samples = [
        _p("k1", 0, 1, 10, "Kitty"), _p("k2", 1, 1, 20, "Kitty"),
        _p("p1", 0, 2, 30, "Pancake"), _p("p2", 1, 2, 40, "Pancake", has_crop=False),
    ]
    scores = {1: _score("Kitty"), 2: _score("Pancake")}
    assert plan_parts(samples, scores, {}) is None

    samples[-1] = _p("p2", 1, 2, 40, "Pancake")
    assert plan_parts(samples, scores, {}) is None  # enough crops, but no frame showing both cats
    samples[-1] = _p("p2", 1, 2, 20, "Pancake")
    assert plan_parts(samples, scores, {}) is not None
