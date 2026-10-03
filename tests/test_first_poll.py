"""Direct regression tests for the coordinator's background first poll (coordinator.py's module
docstring, "The first poll runs in the background"): the retry loop that replaced
`async_config_entry_first_refresh`'s raise-and-let-Home-Assistant-retry, the queue of entity
creation the platforms leave while there is no data yet, the repair issue's lifecycle around
it, and the cancellation that stops both background tasks on unload.

Same `object.__new__`-uninitialised-coordinator style as `test_coordinator_availability.py`;
the end-to-end behaviour (setup returning within its budget, entities appearing later, the
fast path creating them before setup returns) is `tests_ha/test_setup.py`'s job.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kibble import coordinator as coordinator_module
from kibble.coordinator import FIRST_POLL_RETRY_MAX, FIRST_POLL_RETRY_MIN, KibbleCoordinator


def _bare_coordinator(*, data=None) -> KibbleCoordinator:
    coord = object.__new__(KibbleCoordinator)
    coord.hass = SimpleNamespace()
    coord.entry = SimpleNamespace(entry_id="entry1", title="Cat Feeder")
    coord.data = data
    coord.last_error = None
    coord.startup_task = None
    coord.coral_task = None
    coord._first_data_callbacks = []
    return coord


@pytest.fixture(autouse=True)
def deleted_issues(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """`ir.async_delete_issue` needs a real `HomeAssistant`; what these tests care about is
    whether and when it is called."""
    deleted = Mock()
    monkeypatch.setattr(coordinator_module.ir, "async_delete_issue", deleted)
    return deleted


# --- async_poll_until_first_data ---------------------------------------------------------------


async def test_first_poll_retries_with_a_growing_capped_back_off_until_data_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A feeder that is off at Home Assistant's start must be picked up soon after it is back:
    the wait between attempts doubles from the floor up to the cap and stays there -- it never
    gives up and never goes back to hammering a device that just failed."""
    coord = _bare_coordinator()
    attempts = 0

    async def refresh() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 7:  # the feeder is finally back
            coord.data = object()

    coord.async_refresh = refresh
    waits: list[float] = []

    async def record_wait(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(coordinator_module.asyncio, "sleep", record_wait)
    monkeypatch.setattr(coordinator_module.random, "uniform", lambda low, high: 0.0)  # no jitter

    await coord.async_poll_until_first_data()

    assert attempts == 7
    expected = []
    delay = FIRST_POLL_RETRY_MIN
    for _ in range(6):
        expected.append(delay)
        delay = min(delay * 2, FIRST_POLL_RETRY_MAX)
    assert waits == expected
    assert waits[0] == FIRST_POLL_RETRY_MIN
    assert waits[-1] == FIRST_POLL_RETRY_MAX
    assert waits == sorted(waits)  # never shorter than the one before


async def test_the_repair_is_cleared_when_the_first_data_lands_and_never_before(
    monkeypatch: pytest.MonkeyPatch, deleted_issues: Mock
) -> None:
    """The repair a feeder that never answered has raised must outlive every failed retry and
    go the moment the feeder really answers -- including when this coordinator is a reload's
    replacement for the one that raised it: it never failed itself, so `_handle_poll_success`'s
    failure counter (zero here) would leave the stale issue standing."""
    coord = _bare_coordinator()
    attempts = 0

    async def refresh() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 4:
            coord.data = object()

    coord.async_refresh = refresh

    async def wait_while_still_failing(delay: float) -> None:
        deleted_issues.assert_not_called()  # a retry is not a recovery

    monkeypatch.setattr(coordinator_module.asyncio, "sleep", wait_while_still_failing)

    await coord.async_poll_until_first_data()

    assert attempts == 4
    deleted_issues.assert_called_once()
    assert deleted_issues.call_args.args[2] == f"{coordinator_module.ISSUE_FEEDER_UNRESPONSIVE}_entry1"


# --- the queue of entity creation --------------------------------------------------------------


def test_queued_entity_creation_runs_once_in_order_and_a_failing_platform_does_not_stop_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each platform queues its entity creation while there is no data. When the first poll
    lands they run in the order queued; one platform failing is logged and the others still get
    their entities (the isolation `_async_forward_platforms_isolated` gives them at setup); and
    a second run -- a second poll landing -- must never create anything twice."""
    coord = _bare_coordinator()
    ran: list[str] = []

    def bad_platform() -> None:
        raise RuntimeError("a bad platform")

    coord.async_on_first_data(lambda: ran.append("sensor"))
    coord.async_on_first_data(bad_platform)
    coord.async_on_first_data(lambda: ran.append("switch"))

    with caplog.at_level(logging.ERROR, logger=coordinator_module.__name__):
        coord.async_run_first_data_callbacks()

    assert ran == ["sensor", "switch"]
    assert "a bad platform" in caplog.text

    coord.async_run_first_data_callbacks()
    assert ran == ["sensor", "switch"]


# --- cancelling the two background tasks on unload ---------------------------------------------


async def test_cancelling_startup_stops_both_tasks_and_drops_queued_entity_creation() -> None:
    """On unload the first-poll task and the CoralHub task it started are stopped before the
    platforms go and the store closes, and whatever creation was still queued is forgotten --
    run later it would add entities to platforms that are gone."""
    coord = _bare_coordinator()
    ran: list[str] = []
    coord.async_on_first_data(lambda: ran.append("sensor"))
    started = asyncio.Event()

    async def never_lands() -> None:
        started.set()
        await asyncio.Event().wait()

    poll = asyncio.create_task(never_lands())
    coral = asyncio.create_task(never_lands())
    coord.startup_task = poll
    coord.coral_task = coral
    await started.wait()

    await coord.async_cancel_startup()

    assert poll.cancelled() and coral.cancelled()
    assert coord.startup_task is None and coord.coral_task is None
    coord.async_run_first_data_callbacks()
    assert ran == []
    await coord.async_cancel_startup()  # nothing left to stop: harmless


async def test_a_coralhub_task_started_by_the_poll_task_as_it_is_cancelled_is_not_left_behind() -> None:
    """The poll task is what starts the CoralHub task, so cancelling must wait for the poll task
    to finish before looking for one -- otherwise a CoralHub task created in the last moment
    outlives the unload and goes on using a store that has just been closed."""
    coord = _bare_coordinator()
    orphans: list[asyncio.Task] = []

    async def coralhub_forever() -> None:
        await asyncio.Event().wait()

    async def poll_task_that_starts_coralhub_while_being_cancelled() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            coral = asyncio.create_task(coralhub_forever())
            coord.coral_task = coral
            orphans.append(coral)

    coord.startup_task = asyncio.create_task(poll_task_that_starts_coralhub_while_being_cancelled())
    await asyncio.sleep(0)  # let it reach its wait

    await coord.async_cancel_startup()

    [coral] = orphans
    assert coral.cancelled()
    assert coord.coral_task is None
