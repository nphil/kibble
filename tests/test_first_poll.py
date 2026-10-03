"""Direct regression tests for the coordinator's background first poll (coordinator.py's module
docstring, "The first poll runs in the background"): the retry loop that replaced
`async_config_entry_first_refresh`'s raise-and-let-Home-Assistant-retry, the queue of entity
creation the platforms leave while there is no data yet, and the cancellation that stops both
on unload.

Same `object.__new__`-uninitialised-coordinator style as `test_coordinator_availability.py`;
the end-to-end behaviour (setup returning within its budget, entities appearing later, the
fast path creating them before setup returns) is `tests_ha/test_setup.py`'s job.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from kibble import coordinator as coordinator_module
from kibble.coordinator import FIRST_POLL_RETRY_MAX, FIRST_POLL_RETRY_MIN, KibbleCoordinator


def _bare_coordinator(*, data=None) -> KibbleCoordinator:
    coord = object.__new__(KibbleCoordinator)
    coord.data = data
    coord.last_error = None
    coord.startup_task = None
    coord._first_data_callbacks = []
    return coord


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


async def test_cancelling_startup_stops_the_task_and_drops_queued_entity_creation() -> None:
    """On unload the first-poll task is stopped before the platforms go, and whatever creation
    was still queued is forgotten -- run later it would add entities to platforms that are gone."""
    coord = _bare_coordinator()
    ran: list[str] = []
    coord.async_on_first_data(lambda: ran.append("sensor"))
    started = asyncio.Event()

    async def first_poll_that_never_lands() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(first_poll_that_never_lands())
    coord.startup_task = task
    await started.wait()

    await coord.async_cancel_startup()

    assert task.cancelled()
    assert coord.startup_task is None
    coord.async_run_first_data_callbacks()
    assert ran == []
    await coord.async_cancel_startup()  # nothing left to stop: harmless
