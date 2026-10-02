"""Bowl-fill estimation: an immediate post-feed guess for `sensor.py`'s `bowl_fill` entity,
bridging the gap between "a feed just finished" and "the camera reassessed the bowl" (which can
be seconds to minutes later, however long vision takes to get a clear, unoccluded frame).

## The idea

`KibbleCoordinator.async_apply_bowl_fill_feed` fires the instant a feed is first ingested
(`ingest.py`'s `Ingestor._ingest_feed`, the same "first INSERT only" moment that freezes that
feed's `amount1`/`amount2`/`food1`/`food2`/`single`): it adds `portions * fill_per_portion` to
the coordinator's own current baseline (whatever the sensor is already showing -- a still-active
estimate from an earlier feed if one exists, else the last real measurement), clamped to a real
percentage, and that becomes the new reading with `source: "estimate"`. `fill_per_portion` is
looked up per hopper (`"hopper1"`/`"hopper2"` -- a shared bin in single mode never touches the
second bucket, since `amount2` is always `0` there), learned as an exponential moving average of
real measured deltas. `KibbleCoordinator.bowl_fill_settle_pending` clears the override back to
`None` -- letting the entity show the camera's own reading again -- once `SETTLE_SECONDS` have
passed since the *last* feed applied, and only once a real reading is actually available; a raw
reading that simply has not caught up yet (still the stale pre-feed number) never flashes past
the smoother projection, but any real reading is always trusted the moment that window closes,
with no dependency on whether any particular bracket resolved cleanly.

## Learning a real delta

A feed registers a before/after bracket (`PendingFillSample`) only when it dispensed from
exactly one hopper -- a "both" feed still moves the estimate (using both buckets' own current
rates), but a single shared delta can't be cleanly split back into two per-food rates, so it
teaches neither bucket anything. A bracket's `fill_before` is the coordinator's own baseline at
the moment its feed happened (the same value the estimate itself was just computed from) rather
than simply the last real measurement, so a second feed following close behind an already-active
estimate does not have the first feed's own contribution double-counted into its own sample.
`KibbleCoordinator.bowl_fill_settle_pending` runs once per ingest pass (`Ingestor.async_ingest`,
so on every poll or push, not just when something changed): it taints every outstanding bracket
the moment `eating` is observed (a cat visiting the shared bowl mid-window means the eventual
delta measures "feed minus whatever got eaten", not the feed alone -- unusable for learning
regardless of which hopper it started from); `async_apply_bowl_fill_feed` taints every
outstanding bracket the same way whenever a *new* feed of any kind (single or "both") lands
before an older bracket's own deadline, since that bracket's eventual delta would otherwise
measure both feeds' portions, not just the one it started from. An untainted bracket resolves at
its own settle deadline (`SETTLE_SECONDS` after its feed, giving vision time to reassess an
unobstructed frame) into one EWMA sample -- or drops untainted if no measurement exists yet,
never retried. A bracket the camera never confirms at all expires after `EXPIRE_SECONDS` rather
than waiting forever.

## Defaults before any learning

`default_fill_per_portion` derives a starting rate from that hopper's own *finished* bowl-fill
calibration curve when one exists (`sensor.py`'s `_calibration_state`/`GET /calibration`): the
operator dispensed `full_portions` portions from a known-empty bowl to their own "full" mark, so
`100 / full_portions` is that curve's own implied percent-per-portion. Otherwise
`DEFAULT_FILL_PER_PORTION`, a conservative constant checked against real production history --
see that constant's own comment for the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Conservative starting rate (percentage points of bowl capacity per portion) for a hopper with
# no calibration curve and no learned samples yet. Measured against the real feeder's own
# production history (plant_room_cat_feeder, 7 days, 25 single-hopper feeds, 11 with a clean
# before/after camera reading not overlapping an eating event): the median observed delta was
# 1.75 points/portion (mean 2.6) -- and most of that came from the 3 feeds landing on an
# already-near-empty bowl (median 8.5, consistent with the vision score's own well-documented
# empty-vs-one-portion jump, `binary_sensor.py`'s KibbleBowlEmptySensor docstring); the other 8,
# landing on a bowl that already had visible food, moved the score by a median of just 0.75 --
# inside the sensor's own noise floor (unrelated swings of 10-80 points inside one minute, no
# feed nearby, are common in that same history). The old constant (4.0) overshot the common
# (already-has-food) case by 5x+ and undershot the rarer near-empty jump; 2.0 sits closer to the
# overall median while staying a deliberate underestimate of the near-empty jump -- a low guess
# that nudges up once the camera confirms reads better than a high guess that has to visibly
# deflate.
DEFAULT_FILL_PER_PORTION = 2.0

# EWMA weight for each new real sample -- mirrors the daemon's own agent/src/hopper.rs
# `round_mean` (25% weight to the newest observation, 75% kept from the running average):
# responsive to real drift (a different bag, a different fill height) without one noisy sample
# swinging the estimate.
_EWMA_NEW_WEIGHT = 0.25

# How long after a feed to wait before trusting whatever the camera currently reports as the
# "after" measurement -- long enough for an animal (if any) to clear the bowl and vision to
# reassess an unobstructed frame, short enough that "the next camera assessment" still lands
# close to real time. Mirrors the daemon's own SETTLE_AFTER_FEED (feeds.rs, 3s) with margin for
# poll/push latency on top.
SETTLE_SECONDS = 10.0

# A bracket nobody ever resolves (the camera never reassesses, vision stays occluded) is dropped
# rather than kept forever -- the same "give up rather than watch forever" bound as every other
# timed wait in this project (feeds.rs's MAX_CYCLE_WAIT, kibble-bowl.ts's MAX_DROP_MS).
EXPIRE_SECONDS = 30 * 60.0


@dataclass
class PendingFillSample:
    """One hopper's before/after bracket for one feed, waiting for the camera to confirm the
    "after" side. `eating_seen` taints the whole bracket the moment any eating is observed while
    it is outstanding, regardless of which hopper actually fed it -- there is only one shared
    bowl, so an animal eating mid-window poisons every bracket currently in flight. `superseded`
    taints it the same way the moment another feed (of either hopper, single or "both") lands
    before this bracket's own settle deadline -- the eventual delta would then measure this
    feed's portions plus whatever that later feed also added, not this feed's own rate."""

    bucket: str
    fill_before: float
    portions: float
    ready_at: float
    eating_seen: bool = False
    superseded: bool = False


def mark_eating_seen(pending: list[PendingFillSample]) -> None:
    """Taints every currently outstanding bracket -- called once per ingest pass where `eating`
    is true."""
    for sample in pending:
        sample.eating_seen = True


def mark_superseded(pending: list[PendingFillSample]) -> None:
    """Taints every currently outstanding bracket the same way `mark_eating_seen` does -- called
    whenever a new feed is about to change the bowl's contents before an older bracket's own
    settle deadline, since that bracket's eventual delta can no longer be attributed solely to
    the feed that started it."""
    for sample in pending:
        sample.superseded = True


def resolve_ready(
    pending: list[PendingFillSample], now: float, measured_fill: float | None
) -> tuple[list[PendingFillSample], list[tuple[str, float]]]:
    """Splits `pending` into what is still waiting and, for whatever just reached its settle
    deadline, a `(bucket, sample)` per-portion delta ready to fold into that bucket's EWMA. A
    bracket tainted by eating or superseded by a later feed, or one that reached its deadline
    with no measured reading at all (`measured_fill is None`), contributes nothing -- it is
    simply dropped, never learned from and never re-tried on a later pass."""
    still_pending: list[PendingFillSample] = []
    learned: list[tuple[str, float]] = []
    for sample in pending:
        if now < sample.ready_at:
            still_pending.append(sample)
            continue
        if (
            not sample.eating_seen
            and not sample.superseded
            and measured_fill is not None
            and sample.portions > 0
        ):
            learned.append((sample.bucket, (measured_fill - sample.fill_before) / sample.portions))
    return still_pending, learned


def expire_stale(pending: list[PendingFillSample], now: float) -> list[PendingFillSample]:
    """Drops a bracket that has been waiting past `EXPIRE_SECONDS` since its own settle
    deadline -- called every pass, independent of `resolve_ready`'s own deadline check, since a
    bracket the camera never confirms must not accumulate forever."""
    return [sample for sample in pending if now < sample.ready_at + EXPIRE_SECONDS]


def ewma_update(existing: tuple[float, int] | None, sample: float) -> tuple[float, int]:
    """One EWMA step: the first-ever sample for a bucket seeds the average outright (nothing to
    blend with yet); every one after that keeps `1 - _EWMA_NEW_WEIGHT` of the running value and
    `_EWMA_NEW_WEIGHT` of the new reading. `samples` is a plain count, uncapped -- it exists so a
    dashboard can see how much this has actually learned from, not to gate behaviour."""
    if existing is None:
        return (sample, 1)
    old_fill_per_portion, old_samples = existing
    new_fill_per_portion = old_fill_per_portion * (1 - _EWMA_NEW_WEIGHT) + sample * _EWMA_NEW_WEIGHT
    return (new_fill_per_portion, old_samples + 1)


def default_fill_per_portion(calibration_hopper: dict[str, Any] | None) -> float:
    """The starting rate before any real learning -- see the module docstring's "Defaults"
    section. An unfinished curve (`full_portions` still `None`, or `0`) has no "full" reference
    yet, so it falls through to the constant exactly like no curve at all."""
    if calibration_hopper is not None:
        full_portions = calibration_hopper.get("full_portions")
        if full_portions:
            return 100.0 / full_portions
    return DEFAULT_FILL_PER_PORTION


def estimate_value(last_fill: float, delta: float) -> float:
    """`last_fill + delta`, clamped to a valid 0-100 percentage -- the ceiling is the documented
    contract (a feed can never push the reading past "full"), the floor is defensive (a bucket's
    own learned rate could in principle go negative from a noisy sample; the displayed number
    must still be a real percentage either way)."""
    return max(0.0, min(100.0, last_fill + delta))
