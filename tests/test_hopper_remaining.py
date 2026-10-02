"""`sensor.py`'s `hopper_remaining()`: the pure "portions left before low" estimate
docs/37-hopper-full.md defines -- `max(0, full_to_low - portions_since_full)` once both are
known, `None` (shown as unknown) covering both "never marked full" and "marked full but has not
yet completed one full-to-low cycle".
"""

from __future__ import annotations

from kibble.sensor import hopper_remaining


def test_both_known_returns_the_difference() -> None:
    assert hopper_remaining(100, 30) == 70


def test_both_unknown_is_none() -> None:
    """Never marked full at all -- neither number exists yet."""
    assert hopper_remaining(None, None) is None


def test_full_to_low_unknown_is_none_even_with_a_real_portions_count() -> None:
    """Marked full and dispensing normally, but this hopper has never yet completed one
    full-to-low cycle -- the learned capacity does not exist yet, so there is nothing to
    subtract from."""
    assert hopper_remaining(None, 30) is None


def test_portions_since_full_unknown_is_none_even_with_a_learned_capacity() -> None:
    """The daemon learned a capacity on a previous cycle, but this hopper has never been marked
    full since -- there is no "since full" count to subtract."""
    assert hopper_remaining(100, None) is None


def test_portions_since_full_exceeding_full_to_low_clamps_to_zero_not_negative() -> None:
    """The hopper has run for longer than its last learned full-to-low cycle (a fuller-than-
    usual fill, or the estimate is simply stale) -- "essentially empty", never a negative
    portion count."""
    assert hopper_remaining(50, 75) == 0


def test_portions_since_full_exactly_equal_to_full_to_low_is_exactly_zero() -> None:
    assert hopper_remaining(50, 50) == 0
