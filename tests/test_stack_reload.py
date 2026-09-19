"""Direct regression test for `coordinator.py`'s `_check_stack_change`: reload the config entry
exactly once when a poll confirms a genuinely different stack than the last one it confirmed,
and never merely because a poll failed or came back inconclusive (`stacks.detect_stack`
returning `None`) -- a feeder rebooting mid-switch must not be read as "the other stack now".

Same `object.__new__`-uninitialized-coordinator style as `test_coordinator_availability.py`;
`_check_stack_change` itself is synchronous (it only schedules a background task, never awaits
one), so most of these are plain sync tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from kibble.api import KibbleConnectionError
from kibble.coordinator import KibbleCoordinator
from kibble.stacks import Stack


def _bare_coordinator(*, last_confirmed_stack: Stack | None) -> KibbleCoordinator:
    """A real (uninitialized) `KibbleCoordinator` with only what `_check_stack_change`/
    `_async_update_data` read set by hand."""
    coord = object.__new__(KibbleCoordinator)
    coord.hass = SimpleNamespace(
        async_create_task=Mock(),
        config_entries=SimpleNamespace(async_reload=Mock(return_value="reload-coro")),
    )
    coord.entry = SimpleNamespace(entry_id="entry1")
    coord._last_confirmed_stack = last_confirmed_stack
    coord.client = AsyncMock()
    coord.data = None
    coord.consecutive_failures = 0
    coord.last_error = None
    return coord


def _data(detected_stack: Stack | None) -> SimpleNamespace:
    return SimpleNamespace(detected_stack=detected_stack)


# --- _check_stack_change: the core reload-on-change decision -----------------------------------


def test_first_ever_confirmation_records_a_baseline_without_reloading() -> None:
    """Right after `async_config_entry_first_refresh`: nothing to compare against yet, so this
    reading becomes the baseline, not a "change" `async_setup_entry` needs to rerun for -- it
    already ran once against this exact first reading."""
    coord = _bare_coordinator(last_confirmed_stack=None)

    coord._check_stack_change(_data(Stack.VENDOR))

    coord.hass.config_entries.async_reload.assert_not_called()
    coord.hass.async_create_task.assert_not_called()
    assert coord._last_confirmed_stack is Stack.VENDOR


def test_same_stack_confirmed_again_does_not_reload() -> None:
    coord = _bare_coordinator(last_confirmed_stack=Stack.LIBREFEED)

    coord._check_stack_change(_data(Stack.LIBREFEED))

    coord.hass.config_entries.async_reload.assert_not_called()
    coord.hass.async_create_task.assert_not_called()
    assert coord._last_confirmed_stack is Stack.LIBREFEED


def test_a_genuine_stack_change_reloads_the_config_entry_exactly_once() -> None:
    coord = _bare_coordinator(last_confirmed_stack=Stack.VENDOR)

    coord._check_stack_change(_data(Stack.LIBREFEED))

    coord.hass.config_entries.async_reload.assert_called_once_with("entry1")
    coord.hass.async_create_task.assert_called_once_with("reload-coro")
    assert coord._last_confirmed_stack is Stack.LIBREFEED


def test_the_reload_task_is_scheduled_not_awaited_inline() -> None:
    """`_check_stack_change` must never `await` the reload itself -- that would tear down and
    replace the very coordinator whose method is executing, mid-call. Scheduling through
    `hass.async_create_task` (never calling it as a bare coroutine) is what proves this."""
    coord = _bare_coordinator(last_confirmed_stack=Stack.VENDOR)

    result = coord._check_stack_change(_data(Stack.LIBREFEED))

    assert result is None  # a plain sync method, not a coroutine to await
    coord.hass.async_create_task.assert_called_once()


def test_an_inconclusive_reading_neither_reloads_nor_overwrites_the_baseline() -> None:
    """The exact "feeder rebooting mid-switch" case: `detect_stack` returned `None` this cycle
    (an inconclusive `GET /mode`) -- must not be read as a change, and must not corrupt the
    baseline a later, real reading is compared against."""
    coord = _bare_coordinator(last_confirmed_stack=Stack.VENDOR)

    coord._check_stack_change(_data(None))

    coord.hass.config_entries.async_reload.assert_not_called()
    coord.hass.async_create_task.assert_not_called()
    assert coord._last_confirmed_stack is Stack.VENDOR  # unchanged, not clobbered to None


def test_repeated_confirmations_of_the_new_stack_reload_only_the_first_time() -> None:
    """Three consecutive polls all reporting the new stack (as would happen once the feeder is
    back up after the reboot, before the reload it triggered has actually swapped the
    coordinator out) must still add up to exactly one reload."""
    coord = _bare_coordinator(last_confirmed_stack=Stack.VENDOR)

    coord._check_stack_change(_data(Stack.LIBREFEED))
    coord._check_stack_change(_data(Stack.LIBREFEED))
    coord._check_stack_change(_data(Stack.LIBREFEED))

    coord.hass.config_entries.async_reload.assert_called_once_with("entry1")


def test_a_change_back_to_the_original_stack_reloads_again() -> None:
    """Flipping back is just as real a change as flipping forward -- not a "return to
    baseline" special case."""
    coord = _bare_coordinator(last_confirmed_stack=Stack.LIBREFEED)

    coord._check_stack_change(_data(Stack.VENDOR))

    coord.hass.config_entries.async_reload.assert_called_once_with("entry1")
    assert coord._last_confirmed_stack is Stack.VENDOR


# --- _async_update_data: a failed poll must never reach _check_stack_change at all -------------


async def test_a_failed_poll_never_triggers_a_reload_even_if_stale_data_disagrees_with_reality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact "unreachable feeder mid-switch" scenario end to end: `_fetch_all` raises (the
    device is mid-reboot), so `_async_update_data` serves stale data through the *existing*
    degraded-availability policy and must never reach the stack-change comparison at all --
    proven here by a `_check_stack_change` that would fail the test outright if called."""
    coord = _bare_coordinator(last_confirmed_stack=Stack.VENDOR)
    coord.data = _data(Stack.VENDOR)  # prior good snapshot for the failure-tolerance policy
    coord.consecutive_failures = 0
    monkeypatch.setattr(coord, "_fetch_all", AsyncMock(side_effect=KibbleConnectionError("down")))
    monkeypatch.setattr(
        coord,
        "_check_stack_change",
        Mock(side_effect=AssertionError("_check_stack_change must not run on a failed poll")),
    )

    result = await coord._async_update_data()

    assert result is coord.data  # stale data served, per the existing tolerance policy
    coord.hass.config_entries.async_reload.assert_not_called()


async def test_a_successful_poll_confirming_the_same_stack_does_not_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord = _bare_coordinator(last_confirmed_stack=Stack.VENDOR)
    coord.data = _data(Stack.VENDOR)
    fresh = _data(Stack.VENDOR)
    monkeypatch.setattr(coord, "_fetch_all", AsyncMock(return_value=fresh))

    result = await coord._async_update_data()

    assert result is fresh
    coord.hass.config_entries.async_reload.assert_not_called()


async def test_a_successful_poll_confirming_a_new_stack_reloads_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other acceptance criterion end to end: a *real* stack change, observed through the
    normal `_async_update_data` poll path, reloads the config entry exactly once."""
    coord = _bare_coordinator(last_confirmed_stack=Stack.VENDOR)
    coord.data = _data(Stack.VENDOR)
    fresh = _data(Stack.LIBREFEED)
    monkeypatch.setattr(coord, "_fetch_all", AsyncMock(return_value=fresh))

    result = await coord._async_update_data()

    assert result is fresh
    coord.hass.config_entries.async_reload.assert_called_once_with("entry1")
    coord.hass.async_create_task.assert_called_once_with("reload-coro")
