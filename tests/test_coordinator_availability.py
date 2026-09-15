"""Direct regression test for coordinator.py's degraded-availability policy -- the fix for
defect #1: a starved device HTTP server making a single poll fail used to flip
`last_update_success` immediately, which is what took all 51 entities `unavailable` mid-
dashboard. See `coordinator.py`'s module docstring for the full policy this pins.

Exercises the real, bound `KibbleCoordinator` methods on a "bare" instance (`object.__new__`,
skipping `DataUpdateCoordinator.__init__`, which needs a real `HomeAssistant` core instance --
the same reason `test_coordinator_ble_wiring.py` duck-types `self` instead of constructing a
real coordinator) with only the attributes each method actually reads set by hand.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from kibble import coordinator as coordinator_module
from kibble.api import KibbleConnectionError
from kibble.coordinator import CONSECUTIVE_FAILURES_FOR_UNAVAILABLE, KibbleCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed


def _bare_coordinator(*, data=None, consecutive_failures: int = 0) -> KibbleCoordinator:
    """A real (uninitialized) `KibbleCoordinator` with only what the methods under test read."""
    coord = object.__new__(KibbleCoordinator)
    coord.hass = object()
    coord.entry = SimpleNamespace(entry_id="entry1", title="Cat Feeder")
    coord.client = AsyncMock()
    coord.data = data
    coord.consecutive_failures = consecutive_failures
    coord.last_error = None
    return coord


@pytest.fixture(autouse=True)
def issue_registry(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """`ir.async_create_issue`/`async_delete_issue` are `@callback` (synchronous) functions that
    need a real `HomeAssistant` core instance to resolve the issue registry -- patched for every
    test in this file by default so tests not specifically about repair-issue behaviour don't
    need to know it exists; tests that do can inspect `issue_registry.created`/`.deleted`."""
    created = Mock()
    deleted = Mock()
    monkeypatch.setattr(coordinator_module.ir, "async_create_issue", created)
    monkeypatch.setattr(coordinator_module.ir, "async_delete_issue", deleted)
    return SimpleNamespace(created=created, deleted=deleted)


# --- _handle_poll_failure: the core of the degraded-availability policy -------------------------


def test_first_failure_with_prior_data_serves_stale_data_not_a_raise() -> None:
    """The exact starved-server scenario: one failed poll, prior good data exists -- must NOT
    raise (raising is what flips `last_update_success` and blanks every entity)."""
    stale = object()
    coord = _bare_coordinator(data=stale, consecutive_failures=0)

    result = coord._handle_poll_failure(KibbleConnectionError("timed out"))

    assert result is stale
    assert coord.consecutive_failures == 1


def test_failures_below_threshold_keep_serving_stale_data() -> None:
    coord = _bare_coordinator(data=object(), consecutive_failures=0)
    for expected in range(1, CONSECUTIVE_FAILURES_FOR_UNAVAILABLE):
        result = coord._handle_poll_failure(KibbleConnectionError("timed out"))
        assert result is coord.data
        assert coord.consecutive_failures == expected


def test_failure_at_threshold_raises_update_failed() -> None:
    """Once the tolerance window is exhausted, this must raise so HA's own coordinator marks
    entities unavailable for real -- see `entity-unavailable`/`log-when-unavailable`."""
    coord = _bare_coordinator(
        data=object(), consecutive_failures=CONSECUTIVE_FAILURES_FOR_UNAVAILABLE - 1
    )

    with pytest.raises(UpdateFailed):
        coord._handle_poll_failure(KibbleConnectionError("timed out"))

    assert coord.consecutive_failures == CONSECUTIVE_FAILURES_FOR_UNAVAILABLE


def test_failure_with_no_prior_data_raises_immediately_even_on_the_first_attempt() -> None:
    """`async_config_entry_first_refresh`'s scenario: nothing to fall back on, so this must
    raise on attempt 1 -- HA turns that into `ConfigEntryNotReady` (see `__init__.py`)."""
    coord = _bare_coordinator(data=None, consecutive_failures=0)

    with pytest.raises(UpdateFailed):
        coord._handle_poll_failure(KibbleConnectionError("timed out"))


def test_update_failed_carries_a_retry_after_once_past_threshold() -> None:
    coord = _bare_coordinator(data=object(), consecutive_failures=CONSECUTIVE_FAILURES_FOR_UNAVAILABLE)

    with pytest.raises(UpdateFailed) as excinfo:
        coord._handle_poll_failure(KibbleConnectionError("still down"))

    assert excinfo.value.retry_after is not None
    assert excinfo.value.retry_after > 0


def test_retry_after_backs_off_as_failures_keep_accumulating() -> None:
    """Exponential backoff, capped -- a feeder down for minutes should be polled less often."""
    earlier = _bare_coordinator(consecutive_failures=CONSECUTIVE_FAILURES_FOR_UNAVAILABLE)
    later = _bare_coordinator(consecutive_failures=CONSECUTIVE_FAILURES_FOR_UNAVAILABLE + 3)

    assert later._retry_after_seconds() > earlier._retry_after_seconds()
    assert later._retry_after_seconds() <= coordinator_module.MAX_RETRY_AFTER


# --- _handle_poll_success -------------------------------------------------------------------------


def test_success_resets_the_failure_counter() -> None:
    coord = _bare_coordinator(consecutive_failures=2)
    coord._handle_poll_success()
    assert coord.consecutive_failures == 0
    assert coord.last_error is None


def test_success_with_no_prior_failures_does_not_touch_the_issue_registry(
    issue_registry: SimpleNamespace,
) -> None:
    coord = _bare_coordinator(consecutive_failures=0)

    coord._handle_poll_success()

    issue_registry.deleted.assert_not_called()


def test_recovery_after_failures_clears_the_repair_issue(issue_registry: SimpleNamespace) -> None:
    coord = _bare_coordinator(consecutive_failures=CONSECUTIVE_FAILURES_FOR_UNAVAILABLE)

    coord._handle_poll_success()

    issue_registry.deleted.assert_called_once()
    assert issue_registry.deleted.call_args.args[0] is coord.hass
    assert "entry1" in issue_registry.deleted.call_args.args[2]


# --- feeder_reachable: the stricter, always-visible signal ------------------------------------


def test_feeder_reachable_is_false_after_a_single_failure_even_though_available_stays_true() -> None:
    """This is the whole point of `feeder_reachable`/the "Feeder reachable" binary sensor: it
    diverges from `last_update_success` exactly during the tolerance window."""
    coord = _bare_coordinator(data=object(), consecutive_failures=0)
    assert coord.feeder_reachable is True

    coord._handle_poll_failure(KibbleConnectionError("timed out"))

    assert coord.feeder_reachable is False


def test_feeder_reachable_true_only_with_zero_consecutive_failures() -> None:
    assert _bare_coordinator(consecutive_failures=0).feeder_reachable is True
    assert _bare_coordinator(consecutive_failures=1).feeder_reachable is False


# --- repair issue creation at the threshold crossing --------------------------------------------


def test_crossing_the_threshold_creates_a_repair_issue(issue_registry: SimpleNamespace) -> None:
    coord = _bare_coordinator(
        data=object(), consecutive_failures=CONSECUTIVE_FAILURES_FOR_UNAVAILABLE - 1
    )

    with pytest.raises(UpdateFailed):
        coord._handle_poll_failure(KibbleConnectionError("timed out"))

    issue_registry.created.assert_called_once()
    assert issue_registry.created.call_args.kwargs["translation_key"] == (
        coordinator_module.ISSUE_FEEDER_UNRESPONSIVE
    )


def test_staying_below_threshold_never_creates_a_repair_issue(
    issue_registry: SimpleNamespace,
) -> None:
    coord = _bare_coordinator(data=object(), consecutive_failures=0)

    for _ in range(CONSECUTIVE_FAILURES_FOR_UNAVAILABLE - 1):
        coord._handle_poll_failure(KibbleConnectionError("timed out"))

    issue_registry.created.assert_not_called()


def test_first_ever_failure_with_no_data_does_not_create_a_repair_issue(
    issue_registry: SimpleNamespace,
) -> None:
    """Nothing was ever working -- that is `ConfigEntryNotReady` territory, not a repair issue
    about something that broke."""
    coord = _bare_coordinator(data=None, consecutive_failures=0)

    with pytest.raises(UpdateFailed):
        coord._handle_poll_failure(KibbleConnectionError("timed out"))

    issue_registry.created.assert_not_called()


# --- _async_update_data / _fetch_all: the whole-cycle behaviour -------------------------------


async def test_successful_cycle_resets_failures_and_returns_fresh_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord = _bare_coordinator(data=object(), consecutive_failures=2)
    fresh = object()
    monkeypatch.setattr(coord, "_fetch_all", AsyncMock(return_value=fresh))

    result = await coord._async_update_data()

    assert result is fresh
    assert coord.consecutive_failures == 0


async def test_a_hung_request_is_bounded_by_poll_timeout_not_left_to_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aggregate `POLL_TIMEOUT` must abort a stuck cycle well before it could run away --
    this is what keeps one congested endpoint from ballooning a whole poll cycle (see the
    module docstring)."""
    monkeypatch.setattr(coordinator_module, "POLL_TIMEOUT", 0.05)
    coord = _bare_coordinator(data=object(), consecutive_failures=0)

    async def _hangs() -> None:
        await asyncio.sleep(5)

    monkeypatch.setattr(coord, "_fetch_all", _hangs)

    start = asyncio.get_event_loop().time()
    result = await coord._async_update_data()
    elapsed = asyncio.get_event_loop().time() - start

    # Served stale data (failure #1, prior data exists) rather than hanging for anywhere near
    # the full 5s the fake fetch would otherwise take.
    assert result is coord.data
    assert elapsed < 1.0


async def test_update_data_propagates_kibble_error_through_the_same_failure_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord = _bare_coordinator(
        data=object(), consecutive_failures=CONSECUTIVE_FAILURES_FOR_UNAVAILABLE - 1
    )
    monkeypatch.setattr(coord, "_fetch_all", AsyncMock(side_effect=KibbleConnectionError("down")))

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
