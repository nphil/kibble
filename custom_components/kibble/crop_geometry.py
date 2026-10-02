"""Ports librefeed-media's `crop_rect_square` (`librefeed/media/src/crop_rect.h`) to pure Python,
so HA can tell whether one already-archived sample's body/face crop is trustworthy at all.

2026-09-25 bug (`crop_rect.h`'s own history note): `vision.c` computed the correct clamped crop
rectangle, but a downstream resize helper folded the clamped row offset (`ry0`) into its row
pointer while forgetting to fold the clamped column offset (`rx0`) into its column index -- every
capture before the device fix silently sampled columns `[0, cw)` of the right rows instead of
`[rx0, rx0+cw)`. A centred-enough or over-wide box already clamps `rx0` to ~0, so this was
invisible for most detections; an off-centre narrow/tall box (a standing cat, the feeder's own
common shape) does not -- its stored crop shows unrelated room content pinned to the frame's own
left edge instead of the cat (`crop_rect_test.c`'s regression cases: e1410-s1, e1492-s1).

`crop_overlap` reports how much of what was ACTUALLY sampled (`[0, cw)`) coincides with what
SHOULD have been (`[rx0, rx0+cw)`): `max(0, cw - rx0) / cw`. 1.0 means the bug was invisible for
this box (`rx0` already ~0); low means the stored crop shows mostly the wrong content.
`LEGACY_CROP_BEFORE` is when the librefeed-media fix went live; `None` would mean "not deployed
yet", treating EVERY capture as legacy regardless of its own timestamp.

Consumers: `judge.select_judge_crops` (the vision judge must never see or decide from an
untrustworthy crop -- it falls back to the event's own scene/`after` frame instead),
`ingest.IdentityEngine.async_maybe_auto_learn` (never adds a training row from a bad crop),
`store.all_training_features` (the gallery build skips label/auto rows derived from one; never
deletes a row or file), and `store._thumb_candidates`/`event_detail` (never a thumbnail, never
offered to a person to label).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# The device's own vision-frame resolution -- `librefeed/media/tests/crop_rect_test.c`'s own
# real-world regression cases confirm 1280x720; box coordinates are normalized fractions of
# this frame (`samples.box_x1..y2`), never
# raw pixels, so this is required to reproduce `crop_rect_square`'s integer-pixel
# rounding/clamping exactly.
VISION_FRAME_W = 1280
VISION_FRAME_H = 720

# `crop_rect_square`'s own square-padding fraction -- every real caller (`vision.c`) uses exactly
# this value; there is no per-call override anywhere in the device pipeline.
PAD_FRAC = 0.2

# Unix seconds of the librefeed-media deploy that actually folds `rx0` into the resize's column
# index, or `None` while it has not shipped yet. `None` means EVERY existing capture is legacy,
# regardless of its own timestamp. Set to the real deploy time -- this integration never
# advances it speculatively; a wrong value would silently start trusting still-broken crops.
#
# 1790385640 = librefeed-media's crop_rect_square fix, deployed 2026-09-25 21:20 America/
# New_York (the old media process was stopped at 1790385638; any sample at or after this is
# from the fixed build).
LEGACY_CROP_BEFORE: int | None = 1790385640

# Below this overlap ratio, a legacy sample's own body/face crop shows mostly unrelated content.
# `crop_rect_test.c`'s two real mis-crops (e1410-s1: ~0.18, e1492-s1: 0.0) both fall
# well under this; `0.9` itself is trusted (inclusive), matching an on-the-boundary box the bug
# barely, but genuinely, never affected.
CROP_OVERLAP_TRUST_THRESHOLD = 0.9


def _lround(value: float) -> int:
    """`lroundf`'s round-half-away-from-zero, not Python's own round-half-to-even `round()` --
    `crop_rect_test.c`'s "odd_centre_small_frame" case specifically exercises this on both a
    negative and a positive half-integer boundary, and this port must match it exactly."""
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def crop_rect_square(
    x1: float, y1: float, x2: float, y2: float, *, frame_w: int, frame_h: int, pad_frac: float = PAD_FRAC
) -> tuple[int, int, int, int]:
    """Faithful port of `crop_rect.h`'s `crop_rect_square`: expands `[x1,y1,x2,y2]` (pixels,
    `x1<x2`, `y1<y2`) to a centred square padded by `pad_frac`, clamped to
    `[0,frame_w] x [0,frame_h]`. Returns `(rx0, ry0, rx1, ry1)`, exactly the pixel rectangle a
    caller should sample -- may be empty (`rx1<=rx0` or `ry1<=ry0`) for a zero-area input."""
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    w, h = x2 - x1, y2 - y1
    side = max(w, h) * (1.0 + pad_frac)
    ox1 = _lround(cx - side / 2.0)
    oy1 = _lround(cy - side / 2.0)
    ox2 = _lround(cx + side / 2.0)
    oy2 = _lround(cy + side / 2.0)
    ox1 = max(ox1, 0)
    oy1 = max(oy1, 0)
    ox2 = min(ox2, frame_w)
    oy2 = min(oy2, frame_h)
    return ox1, oy1, ox2, oy2


def crop_overlap(
    box: Sequence[float], *, frame_w: int = VISION_FRAME_W, frame_h: int = VISION_FRAME_H
) -> float:
    """How much of one NORMALIZED (0..1) detection box's own crop the missing-`rx0`-fold bug
    would actually have sampled correctly: `max(0, cw - rx0) / cw`, where `(rx0, cw)` come from
    `crop_rect_square` on `box` scaled to `frame_w`x`frame_h` pixels. `1.0` for a box the bug
    never affected (`rx0` already ~0); `0.0` for a degenerate (zero-width or zero-height) box,
    since there is nothing to verify either way."""
    x1, y1, x2, y2 = box
    rx0, _ry0, rx1, _ry1 = crop_rect_square(
        x1 * frame_w, y1 * frame_h, x2 * frame_w, y2 * frame_h, frame_w=frame_w, frame_h=frame_h
    )
    cw = rx1 - rx0
    if cw <= 0:
        return 0.0
    return max(0.0, cw - rx0) / cw


def is_legacy_crop_trustworthy(box: Sequence[float] | None, t: int) -> bool:
    """Whether one sample's own body/face crop (captured at unix time `t`, with detection `box`)
    is trustworthy enough to actually show -- to the vision judge, or to auto-learn's own
    training-set gallery. A capture at or after `LEGACY_CROP_BEFORE` is trusted outright, no box
    needed -- the device fix already applied to it, so the bug cannot have touched it.
    `LEGACY_CROP_BEFORE is None` means the fix has not deployed at all: every capture is legacy
    regardless of `t`. `box is None` (the device reported none at all -- a legacy journal row, or
    `store.py`'s own `_box_usable` note on a boxless sample) is never itself untrusted by this
    check; the bug is specifically about crop CONTENT relative to a known box, not about whether
    a box exists."""
    if LEGACY_CROP_BEFORE is not None and t >= LEGACY_CROP_BEFORE:
        return True
    if box is None:
        return True
    return crop_overlap(box) >= CROP_OVERLAP_TRUST_THRESHOLD
