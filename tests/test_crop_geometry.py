"""`crop_geometry.py`: the Python port of `librefeed/media/src/crop_rect.h`'s `crop_rect_square`,
and the trustworthiness gate built on top of it -- see that header's own history note for the
2026-09-25 crop-shift bug this guards HA's own side
against (a legacy capture's stored body/face crop can show unrelated room content instead of the
cat). `crop_rect_square`'s own test vectors are transcribed verbatim from
`librefeed/media/tests/crop_rect_test.c` -- every one of them must reproduce EXACTLY the same
clamped pixel rectangle as the C source, not just "a plausible-looking" one.
"""

from __future__ import annotations

import pytest

from kibble.crop_geometry import (
    CROP_OVERLAP_TRUST_THRESHOLD,
    VISION_FRAME_H,
    VISION_FRAME_W,
    crop_overlap,
    crop_rect_square,
    is_legacy_crop_trustworthy,
)

# --- crop_rect_square: verbatim parity with crop_rect_test.c -----------------------------------

# (name, x1, y1, x2, y2, pad_frac, frame_w, frame_h, expected (rx0, ry0, rx1, ry1))
_C_TEST_VECTORS = [
    ("e1410_s1_narrow_tall", 563, 16, 759, 434, 0.2, 1280, 720, (410, 0, 912, 476)),
    ("e1492_s1_narrow_tall", 612, 150, 759, 448, 0.2, 1280, 720, (507, 120, 864, 478)),
    ("wide_no_clamp", 400, 340, 880, 400, 0.2, 1280, 720, (352, 82, 928, 658)),
    ("narrow_tall_centred_no_clamp", 600, 100, 680, 500, 0.2, 1280, 720, (400, 60, 880, 540)),
    ("square_dead_centre_no_clamp", 590, 310, 690, 410, 0.2, 1280, 720, (580, 300, 700, 420)),
    ("left_edge_clamped", 0, 300, 80, 460, 0.2, 1280, 720, (0, 284, 136, 476)),
    ("right_edge_clamped", 1200, 300, 1280, 460, 0.2, 1280, 720, (1144, 284, 1280, 476)),
    ("top_edge_clamped", 560, 0, 720, 100, 0.2, 1280, 720, (544, 0, 736, 146)),
    ("bottom_edge_clamped", 560, 650, 720, 720, 0.2, 1280, 720, (544, 589, 736, 720)),
    ("full_frame_box", 0, 0, 1280, 720, 0.2, 1280, 720, (0, 0, 1280, 720)),
    ("oversized_after_padding", 100, 100, 1180, 650, 0.2, 1280, 720, (0, 0, 1280, 720)),
    ("odd_centre_small_frame", 10, 10, 51, 90, 0.2, 640, 360, (0, 2, 79, 98)),
]


@pytest.mark.parametrize(
    "x1,y1,x2,y2,pad_frac,frame_w,frame_h,expected",
    [v[1:] for v in _C_TEST_VECTORS],
    ids=[v[0] for v in _C_TEST_VECTORS],
)
def test_crop_rect_square_matches_the_c_source_exactly(
    x1: int, y1: int, x2: int, y2: int, pad_frac: float, frame_w: int, frame_h: int,
    expected: tuple[int, int, int, int],
) -> None:
    assert crop_rect_square(x1, y1, x2, y2, frame_w=frame_w, frame_h=frame_h, pad_frac=pad_frac) == expected


# --- crop_overlap: the three requested boundaries -----------------------------------------------


def test_crop_overlap_at_rx0_zero_is_exactly_one() -> None:
    """`left_edge_clamped`'s own vector (rx0=0, rx1=136) as a NORMALIZED box on the standard
    1280x720 frame -- a box the shift bug could never have affected, since it already samples
    from the frame's own left edge."""
    box = (0 / 1280, 300 / 720, 80 / 1280, 460 / 720)
    assert crop_overlap(box) == 1.0


def test_crop_overlap_of_exactly_the_trust_threshold_is_trusted() -> None:
    """A box hand-picked (pixel rect (7,300,41,334) on 1280x720 -> rx0=4, cw=40) to overlap at
    EXACTLY 0.9 -- `is_legacy_crop_trustworthy`'s own `>=` must accept it, not just anything
    strictly above."""
    box = (7 / 1280, 300 / 720, 41 / 1280, 334 / 720)
    overlap = crop_overlap(box)
    assert overlap == pytest.approx(0.9)
    assert overlap >= CROP_OVERLAP_TRUST_THRESHOLD
    assert is_legacy_crop_trustworthy(box, t=0) is True


def test_crop_overlap_just_under_the_trust_threshold_is_untrustworthy() -> None:
    """The same rectangle shape as the 0.9 case, shifted one pixel further off-centre (rx0=5
    instead of 4) so `cw` stays 40 and overlap drops to 35/40 = 0.875 -- just under the line."""
    box = (8 / 1280, 300 / 720, 42 / 1280, 334 / 720)
    overlap = crop_overlap(box)
    assert overlap < CROP_OVERLAP_TRUST_THRESHOLD
    assert is_legacy_crop_trustworthy(box, t=0) is False


def test_crop_overlap_of_the_real_night_evidence_regressions_is_low() -> None:
    """The two real mis-cropped events `crop_rect_test.c` itself regression-tests against
    (e1410-s1, e1492-s1) must both compute as untrustworthy."""
    e1410 = (0.440, 0.022, 0.593, 0.603)
    e1492 = (0.478, 0.208, 0.593, 0.622)
    assert crop_overlap(e1410) < CROP_OVERLAP_TRUST_THRESHOLD
    assert crop_overlap(e1492) < CROP_OVERLAP_TRUST_THRESHOLD


def test_crop_overlap_of_a_zero_area_box_is_zero() -> None:
    assert crop_overlap((0.5, 0.5, 0.5, 0.5)) == 0.0


# --- is_legacy_crop_trustworthy: the deploy-cutoff boundary --------------------------------------


BAD_BOX = (0.478, 0.208, 0.593, 0.622)  # e1492-s1 -- overlap 0.0, always untrustworthy pre-fix


def test_the_deploy_cutoff_is_the_real_librefeed_media_fix_timestamp() -> None:
    """librefeed-media's `crop_rect_square` fix deployed 2026-09-25 21:20 America/New_York; the
    old media process was stopped at 1790385638, so 1790385640 is the first trustworthy second."""
    from kibble.crop_geometry import LEGACY_CROP_BEFORE

    assert LEGACY_CROP_BEFORE == 1790385640


def test_every_capture_is_legacy_while_the_cutoff_is_explicitly_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LEGACY_CROP_BEFORE is None` means the device fix has not deployed at all -- a bad box is
    untrustworthy regardless of how recent `t` is. No longer the live default (the fix has
    shipped), but the code path -- and what it would mean for a future re-deploy -- still
    needs coverage."""
    monkeypatch.setattr("kibble.crop_geometry.LEGACY_CROP_BEFORE", None)
    assert is_legacy_crop_trustworthy(BAD_BOX, t=2_000_000_000) is False


def test_a_capture_at_or_after_the_real_deploy_cutoff_is_trusted_even_with_a_bad_box() -> None:
    assert is_legacy_crop_trustworthy(BAD_BOX, t=1790385640) is True  # exactly at the cutoff
    assert is_legacy_crop_trustworthy(BAD_BOX, t=1790385641) is True  # after it


def test_a_capture_before_the_real_deploy_cutoff_still_checks_its_own_overlap() -> None:
    assert is_legacy_crop_trustworthy(BAD_BOX, t=1790385639) is False


def test_a_missing_box_is_never_itself_untrustworthy() -> None:
    """The shift bug is about crop CONTENT relative to a known box; a sample with no box at all
    (a legacy journal row, or a device response that omitted one) carries no such signal either
    way -- `store._box_usable`'s own precedent for a boxless sample."""
    assert is_legacy_crop_trustworthy(None, t=0) is True


def test_frame_dimensions_default_to_the_devices_own_vision_frame_resolution() -> None:
    assert (VISION_FRAME_W, VISION_FRAME_H) == (1280, 720)
