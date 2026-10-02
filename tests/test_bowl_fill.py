"""Bowl-fill estimation (`bowl_fill.py`): the pure EWMA/bracket bookkeeping, and
`KibbleCoordinator`'s wiring of it through `async_apply_bowl_fill_feed`/`bowl_fill_settle_pending`.

Same `object.__new__` bare-coordinator approach already established elsewhere in this suite
(`test_calibration.py`, `test_desiccant.py`) for the coordinator-level cases; the module-level
pure functions in `bowl_fill.py` need no fixture at all, and the sensor-level case constructs a
real `KibbleBowlFillSensor` through its actual constructor (safe here since `BaseCoordinatorEntity.
__init__` is a plain attribute assignment -- see `homeassistant.helpers.update_coordinator`).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import PERCENTAGE
from kibble import bowl_fill
from kibble.api import FeedRecord, FeederState
from kibble.coordinator import KibbleCoordinator
from kibble.sensor import KibbleBowlFillSensor

# --- pure functions: EWMA ---------------------------------------------------------------------


def test_ewma_seeds_outright_on_the_first_ever_sample() -> None:
    """Nothing to blend with yet -- the first sample simply becomes the rate."""
    assert bowl_fill.ewma_update(None, 5.0) == (5.0, 1)


def test_ewma_blends_75_percent_old_25_percent_new() -> None:
    assert bowl_fill.ewma_update((4.0, 1), 8.0) == (4.0 * 0.75 + 8.0 * 0.25, 2) == (5.0, 2)


def test_ewma_sample_count_keeps_climbing_uncapped() -> None:
    learned = (4.0, 1)
    for _ in range(5):
        learned = bowl_fill.ewma_update(learned, 4.0)
    assert learned == (4.0, 6)  # every sample already matches the running rate -- unchanged


# --- pure functions: the clamp ------------------------------------------------------------------


def test_estimate_value_clamps_to_100_not_past_full() -> None:
    assert bowl_fill.estimate_value(99.0, 5.0) == 100.0


def test_estimate_value_clamps_to_0_not_below_empty() -> None:
    assert bowl_fill.estimate_value(1.0, -5.0) == 0.0


def test_estimate_value_passes_through_an_unclamped_delta() -> None:
    assert bowl_fill.estimate_value(50.0, 8.0) == 58.0


# --- pure functions: eating exclusion, superseding, and expiry ---------------------------------


def _sample(**overrides) -> bowl_fill.PendingFillSample:
    fields = {"bucket": "hopper1", "fill_before": 50.0, "portions": 2.0, "ready_at": 100.0}
    fields.update(overrides)
    return bowl_fill.PendingFillSample(**fields)


def test_resolve_ready_learns_an_untainted_bracket_at_its_deadline() -> None:
    still_pending, learned = bowl_fill.resolve_ready([_sample()], now=100.0, measured_fill=58.0)

    assert still_pending == []
    assert learned == [("hopper1", 4.0)]  # (58 - 50) / 2 portions


def test_resolve_ready_keeps_a_bracket_that_has_not_reached_its_deadline_yet() -> None:
    sample = _sample(ready_at=200.0)
    still_pending, learned = bowl_fill.resolve_ready([sample], now=100.0, measured_fill=58.0)

    assert still_pending == [sample]
    assert learned == []


def test_eating_taints_a_bracket_so_it_is_dropped_not_learned() -> None:
    sample = _sample()
    bowl_fill.mark_eating_seen([sample])

    still_pending, learned = bowl_fill.resolve_ready([sample], now=100.0, measured_fill=58.0)

    assert still_pending == []  # gone -- not kept around for a retry
    assert learned == []
    assert sample.eating_seen is True


def test_eating_observed_well_before_the_deadline_still_taints_once_it_arrives() -> None:
    """`mark_eating_seen` fires on whatever pass eating happens on; the taint must survive on
    the mutated bracket until its own, later, settle deadline."""
    sample = _sample(ready_at=200.0)
    bowl_fill.mark_eating_seen([sample])

    still_pending, _learned = bowl_fill.resolve_ready([sample], now=150.0, measured_fill=None)
    assert still_pending == [sample]  # not due yet, taint or not

    still_pending, learned = bowl_fill.resolve_ready(still_pending, now=200.0, measured_fill=58.0)
    assert still_pending == []
    assert learned == []


def test_resolve_ready_drops_a_bracket_with_no_measurement_at_its_deadline_without_retrying() -> None:
    still_pending, learned = bowl_fill.resolve_ready([_sample()], now=100.0, measured_fill=None)

    assert still_pending == []  # gone, per the documented "never retried" contract
    assert learned == []


def test_resolve_ready_defensively_excludes_a_zero_portion_bracket() -> None:
    """Guards the division in the learned-delta calculation -- a portions=0 bracket should never
    exist in practice, but must not raise if one somehow does."""
    _still_pending, learned = bowl_fill.resolve_ready([_sample(portions=0.0)], now=100.0, measured_fill=58.0)
    assert learned == []


def test_superseded_taints_a_bracket_so_it_is_dropped_not_learned() -> None:
    """A later feed landing before this bracket's own deadline poisons it the same way eating
    does -- the eventual delta can no longer be attributed solely to the feed that started it."""
    sample = _sample()
    bowl_fill.mark_superseded([sample])

    _still_pending, learned = bowl_fill.resolve_ready([sample], now=100.0, measured_fill=58.0)

    assert learned == []
    assert sample.superseded is True


def test_expire_stale_drops_a_bracket_the_camera_never_confirmed() -> None:
    kept = bowl_fill.expire_stale([_sample(ready_at=100.0)], now=100.0 + bowl_fill.EXPIRE_SECONDS + 1)
    assert kept == []


def test_expire_stale_keeps_a_bracket_still_within_its_grace_window() -> None:
    sample = _sample(ready_at=100.0)
    kept = bowl_fill.expire_stale([sample], now=100.0 + bowl_fill.EXPIRE_SECONDS - 1)
    assert kept == [sample]


# --- pure functions: defaults -------------------------------------------------------------------


def test_default_fill_per_portion_derives_from_a_finished_calibration_curve() -> None:
    assert bowl_fill.default_fill_per_portion({"full_portions": 25}) == 4.0  # 100 / 25


def test_default_fill_per_portion_falls_back_to_the_constant_when_unusable() -> None:
    assert bowl_fill.default_fill_per_portion({"full_portions": None}) == bowl_fill.DEFAULT_FILL_PER_PORTION
    assert bowl_fill.default_fill_per_portion({"full_portions": 0}) == bowl_fill.DEFAULT_FILL_PER_PORTION
    assert bowl_fill.default_fill_per_portion(None) == bowl_fill.DEFAULT_FILL_PER_PORTION


# --- coordinator wiring: async_apply_bowl_fill_feed / bowl_fill_settle_pending ------------------


def _coordinator(bowl_fill_value=None, eating=False, calibration=None) -> KibbleCoordinator:
    """A real (uninitialized) `KibbleCoordinator` with only the bowl-fill fields its own methods
    touch set by hand -- same `object.__new__` approach as `test_calibration.py`/`test_desiccant.
    py`. `async_create_task` closes the scheduled coroutine instead of running it, same stand-in
    as `test_optional_routes.py`/`test_stack_reload.py`."""
    coord = object.__new__(KibbleCoordinator)
    coord.hass = SimpleNamespace(
        async_create_task=lambda coro: coro.close() if hasattr(coro, "close") else None
    )
    coord.store = SimpleNamespace(async_set_bowl_fill_learning=AsyncMock())
    coord.data = SimpleNamespace(
        state=SimpleNamespace(bowl_fill=bowl_fill_value, eating=eating),
        calibration=calibration,
    )
    coord.async_update_listeners = lambda: None
    coord._bowl_fill_last_measured = None
    coord._bowl_fill_learned = {}
    coord._bowl_fill_pending = []
    coord._bowl_fill_estimate = None
    coord._bowl_fill_clear_after = None
    return coord


def _feed(amount1=None, amount2=None) -> FeedRecord:
    return FeedRecord(ts=0, id="f1", amount1=amount1, amount2=amount2, manual=True, before=None, after=None)


def test_bowl_fill_per_portion_prefers_the_learned_rate_over_any_default() -> None:
    coord = _coordinator()
    coord._bowl_fill_learned["hopper1"] = (5.5, 4)
    assert coord.bowl_fill_per_portion("hopper1") == (5.5, 4)


def test_bowl_fill_per_portion_falls_back_to_the_calibration_curve_when_unlearned() -> None:
    coord = _coordinator(calibration={"hoppers": [{"full_portions": 20}, None]})
    assert coord.bowl_fill_per_portion("hopper1") == (5.0, 0)  # 100 / 20
    assert coord.bowl_fill_per_portion("hopper2") == (bowl_fill.DEFAULT_FILL_PER_PORTION, 0)


def test_apply_feed_is_a_no_op_before_any_real_measurement_ever_exists() -> None:
    coord = _coordinator(bowl_fill_value=None)
    coord.async_apply_bowl_fill_feed(_feed(amount1=2, amount2=0))
    assert coord.bowl_fill_estimate is None
    assert coord._bowl_fill_pending == []


def test_single_hopper_feed_creates_an_estimate_and_a_bracket_using_the_learned_rate() -> None:
    coord = _coordinator(bowl_fill_value=50)
    coord._bowl_fill_last_measured = 50.0
    coord._bowl_fill_learned["hopper1"] = (4.0, 3)

    coord.async_apply_bowl_fill_feed(_feed(amount1=2, amount2=0))

    value, attrs = coord.bowl_fill_estimate
    assert value == 58.0  # 50 + 2 portions * 4.0/portion
    assert attrs["fill_per_portion"] == [4.0, bowl_fill.DEFAULT_FILL_PER_PORTION]
    assert len(coord._bowl_fill_pending) == 1
    bracket = coord._bowl_fill_pending[0]
    assert (bracket.bucket, bracket.fill_before, bracket.portions) == ("hopper1", 50.0, 2.0)


def test_both_hopper_feed_moves_the_estimate_using_both_rates_but_creates_no_bracket() -> None:
    coord = _coordinator(bowl_fill_value=50)
    coord._bowl_fill_last_measured = 50.0
    coord._bowl_fill_learned["hopper1"] = (4.0, 3)
    coord._bowl_fill_learned["hopper2"] = (6.0, 1)

    coord.async_apply_bowl_fill_feed(_feed(amount1=2, amount2=1))

    value, _attrs = coord.bowl_fill_estimate
    assert value == 50.0 + 2 * 4.0 + 1 * 6.0  # both buckets' current rates applied -- 64.0
    assert coord._bowl_fill_pending == []  # a shared delta teaches neither bucket


def test_both_hopper_estimate_is_still_overridden_by_the_next_real_reading() -> None:
    """The bug this guards: a "both" feed registers no bracket at all, so without a mechanism
    decoupled from bracket resolution, the estimate would never be superseded."""
    coord = _coordinator(bowl_fill_value=50)
    coord._bowl_fill_last_measured = 50.0
    coord._bowl_fill_learned["hopper1"] = (4.0, 3)
    coord._bowl_fill_learned["hopper2"] = (6.0, 1)

    coord.async_apply_bowl_fill_feed(_feed(amount1=2, amount2=1))
    assert coord.bowl_fill_estimate is not None

    # Too soon: the settle window has not elapsed yet, even though a reading already exists.
    coord.data.state.bowl_fill = 50  # the stale pre-feed value, camera has not caught up
    coord.bowl_fill_settle_pending()
    assert coord.bowl_fill_estimate is not None

    # The settle window has passed and a real reading now exists: it must win, unconditionally.
    coord._bowl_fill_clear_after = 0.0
    coord.data.state.bowl_fill = 63
    coord.bowl_fill_settle_pending()

    assert coord.bowl_fill_estimate is None


def test_a_real_reading_overrides_a_single_hopper_estimate_once_its_settle_window_closes() -> None:
    coord = _coordinator(bowl_fill_value=50)
    coord._bowl_fill_last_measured = 50.0
    coord._bowl_fill_learned["hopper1"] = (4.0, 3)
    coord.async_apply_bowl_fill_feed(_feed(amount1=2, amount2=0))
    assert coord.bowl_fill_estimate is not None

    coord._bowl_fill_pending[0].ready_at = 0.0  # due immediately
    coord._bowl_fill_clear_after = 0.0  # settle window also due immediately
    coord.data.state.bowl_fill = 61  # the camera's own fresh reading

    coord.bowl_fill_settle_pending()

    assert coord.bowl_fill_estimate is None  # the real reading wins
    assert coord._bowl_fill_learned["hopper1"] == (4.375, 4)  # 0.75*4.0 + 0.25*((61-50)/2)
    coord.store.async_set_bowl_fill_learning.assert_called_once_with("hopper1", 4.375, 4)


def test_a_second_single_hopper_feed_uses_the_updated_baseline_not_the_stale_measurement() -> None:
    """The bug this guards: using the last real measurement (rather than the coordinator's own
    current baseline) as `fill_before` would double-count the first feed's own contribution into
    the second feed's learned sample."""
    coord = _coordinator(bowl_fill_value=50)
    coord._bowl_fill_last_measured = 50.0
    coord._bowl_fill_learned["hopper1"] = (4.0, 3)

    coord.async_apply_bowl_fill_feed(_feed(amount1=2, amount2=0))  # baseline 50 -> estimate 58
    assert coord.bowl_fill_estimate[0] == 58.0
    first_bracket = coord._bowl_fill_pending[0]

    coord.async_apply_bowl_fill_feed(_feed(amount1=1, amount2=0))  # must start from 58, not 50

    assert coord.bowl_fill_estimate[0] == 62.0  # 58 + 1 * 4.0
    assert len(coord._bowl_fill_pending) == 2
    assert coord._bowl_fill_pending[1].fill_before == 58.0  # not 50.0
    assert first_bracket.superseded is True  # poisoned by the second feed landing first


def test_a_superseded_bracket_never_teaches_even_once_its_own_deadline_arrives() -> None:
    coord = _coordinator(bowl_fill_value=50)
    coord._bowl_fill_last_measured = 50.0
    coord._bowl_fill_learned["hopper1"] = (4.0, 3)
    coord.async_apply_bowl_fill_feed(_feed(amount1=2, amount2=0))
    first_bracket = coord._bowl_fill_pending[0]
    coord.async_apply_bowl_fill_feed(_feed(amount1=1, amount2=0))  # supersedes first_bracket
    assert first_bracket.superseded is True

    first_bracket.ready_at = 0.0  # due immediately; the second bracket is not
    coord.data.state.bowl_fill = 70  # a plausible-looking "after" value -- still must not teach
    coord.bowl_fill_settle_pending()

    assert coord._bowl_fill_learned["hopper1"] == (4.0, 3)  # untouched -- the taint held
    coord.store.async_set_bowl_fill_learning.assert_not_called()


def test_eating_observed_by_the_coordinator_taints_the_learning_bracket() -> None:
    coord = _coordinator(bowl_fill_value=50)
    coord._bowl_fill_last_measured = 50.0
    coord._bowl_fill_learned["hopper1"] = (4.0, 3)
    coord.async_apply_bowl_fill_feed(_feed(amount1=2, amount2=0))
    coord._bowl_fill_pending[0].ready_at = 0.0  # due immediately

    coord.data.state.eating = True
    coord.data.state.bowl_fill = 58  # exactly what an untainted bracket would have learned from

    coord.bowl_fill_settle_pending()

    assert coord._bowl_fill_learned["hopper1"] == (4.0, 3)  # unchanged -- eating poisoned it
    assert coord._bowl_fill_pending == []  # still dropped, not kept around for a retry


def test_a_feed_that_would_overflow_past_full_is_clamped() -> None:
    coord = _coordinator(bowl_fill_value=99)
    coord._bowl_fill_last_measured = 99.0
    coord._bowl_fill_learned["hopper1"] = (10.0, 5)

    coord.async_apply_bowl_fill_feed(_feed(amount1=1, amount2=0))

    assert coord.bowl_fill_estimate[0] == 100.0


def test_settle_pending_on_a_fresh_coordinator_just_records_the_first_real_reading() -> None:
    """The cold-start / post-restart case: no bracket, no estimate, and nothing learned yet --
    must not crash, and must simply adopt the first real reading as the new baseline."""
    coord = _coordinator(bowl_fill_value=42)

    coord.bowl_fill_settle_pending()

    assert coord._bowl_fill_last_measured == 42.0
    assert coord.bowl_fill_estimate is None
    assert coord._bowl_fill_pending == []


# --- sensor: unique_id / entity_id / unit / state class preservation ---------------------------


def test_bowl_fill_sensor_preserves_unique_id_unit_and_state_class() -> None:
    """Hard constraint: `sensor.plant_room_cat_feeder_bowl_fill` must keep its unique_id (and
    therefore its entity_id), unit, and state class through this rewrite, or its statistics are
    orphaned. `key="bowl_fill"` -- unchanged from the old generic `KibbleSensorDescription` this
    class replaced -- is what makes `f"{serial}_{key}"` come out byte-for-byte identical."""
    coordinator = SimpleNamespace(
        data=SimpleNamespace(
            state=FeederState.from_json({"serial": "PLANT_ROOM_SERIAL", "firmware": "895", "event_counter": 0})
        ),
        entry=SimpleNamespace(data={"host": "192.0.2.1", "port": 8765}),
    )

    ent = KibbleBowlFillSensor(coordinator)

    assert ent.unique_id == "PLANT_ROOM_SERIAL_bowl_fill"
    assert ent.native_unit_of_measurement == PERCENTAGE
    assert ent.state_class == SensorStateClass.MEASUREMENT
