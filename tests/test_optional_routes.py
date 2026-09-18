"""Direct regression test for coordinator.py's `_optional` helper: LibreFeed serves only part
of `kibbled`'s HTTP API today (`/state`, `/feeds`, `/wifi`, `/cloud`, `/mode`, `/schedule` --
more routes are coming), so `_fetch_all`'s other reads (`config`, `wifi_scan`, `cats`,
`identify`, `review_face`, `pending_faces`, `clips`, `feeds`, `events`, `schedule`) must not
fail the whole poll cycle when the agent 404s a route it hasn't grown yet. Any other
`KibbleError` (connection refused, timeout, a real 5xx) must still fail the poll exactly as
before -- only a 404 is optional.

Same fake-client/bare-coordinator style as `test_stack_select.py`'s
`test_get_mode_failure_leaves_stack_none_while_the_rest_of_the_poll_still_updates`: a real,
uninitialized `KibbleCoordinator` (`object.__new__`, skipping `DataUpdateCoordinator.__init__`)
with only what `_fetch_all`/`_async_update_data` actually read set by hand.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from kibble.api import KibbleConnectionError, KibbleNotFoundError, ScheduleState
from kibble.coordinator import KibbleCoordinator


def _coordinator_for_fetch(client: AsyncMock) -> KibbleCoordinator:
    """A real (uninitialized) `KibbleCoordinator` with only what `_fetch_all`/
    `_async_update_data` read set by hand -- same `object.__new__` approach as
    `test_coordinator_availability.py`/`test_stack_select.py`."""
    coord = object.__new__(KibbleCoordinator)
    coord.hass = object()
    coord.client = client
    coord.entry = SimpleNamespace(entry_id="entry1", title="Cat Feeder", options={})
    coord.data = None
    coord.consecutive_failures = 0
    coord.last_error = None
    return coord


def _librefeed_client(**overrides: AsyncMock) -> AsyncMock:
    """A client stubbed as if talking to LibreFeed: everything it serves today returns an
    innocuous default; `overrides` replaces individual methods (typically with a
    `KibbleNotFoundError`/`KibbleConnectionError` `side_effect`) to set up one scenario."""
    client = AsyncMock(
        state=AsyncMock(return_value=object()),
        schedule=AsyncMock(return_value=ScheduleState.from_json({"entries": []})),
        config=AsyncMock(return_value={}),
        cloud=AsyncMock(return_value=object()),
        mode=AsyncMock(return_value=object()),
        wifi=AsyncMock(return_value=object()),
        wifi_scan=AsyncMock(return_value=[]),
        cats=AsyncMock(return_value=[]),
        identify=AsyncMock(return_value=object()),
        review_face=AsyncMock(return_value=object()),
        pending_faces=AsyncMock(return_value=[]),
        clips=AsyncMock(return_value=[]),
        feeds=AsyncMock(return_value=[]),
        events=AsyncMock(return_value=[]),
    )
    for name, mock in overrides.items():
        setattr(client, name, mock)
    return client


async def test_404_on_cats_and_events_yields_empty_tuples_and_the_poll_succeeds() -> None:
    client = _librefeed_client(
        cats=AsyncMock(side_effect=KibbleNotFoundError("/cats not supported by this agent")),
        events=AsyncMock(side_effect=KibbleNotFoundError("/events not supported by this agent")),
    )
    coord = _coordinator_for_fetch(client)

    data = await coord._async_update_data()

    assert data.cats == ()
    assert data.events == ()
    # The poll itself was treated as a success -- no failure recorded, exactly like
    # `test_coordinator_availability.py`'s `test_successful_cycle_resets_failures_and_returns_fresh_data`.
    assert coord.consecutive_failures == 0
    assert coord.last_error is None


async def test_connection_error_on_cats_still_fails_the_poll() -> None:
    """Unlike a 404, a genuine connection failure on an optional route must not be swallowed --
    `_optional` only catches `KibbleNotFoundError`. With no prior snapshot to fall back on,
    `_handle_poll_failure` raises immediately (see `test_coordinator_availability.py`'s
    `test_failure_with_no_prior_data_raises_immediately_even_on_the_first_attempt`)."""
    client = _librefeed_client(
        cats=AsyncMock(side_effect=KibbleConnectionError("agent.local: connection refused"))
    )
    coord = _coordinator_for_fetch(client)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.consecutive_failures == 1


async def test_404_on_schedule_yields_an_empty_schedule_state_with_no_entries() -> None:
    client = _librefeed_client(
        schedule=AsyncMock(side_effect=KibbleNotFoundError("/schedule not supported"))
    )
    coord = _coordinator_for_fetch(client)

    data = await coord._async_update_data()

    assert data.schedule == ScheduleState.from_json({"entries": []})
    assert data.schedule.entries == ()
