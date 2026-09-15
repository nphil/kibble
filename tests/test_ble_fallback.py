"""`kibble/ble_fallback.py`: the Wi-Fi-first, BLE-fallback decision for `kibble.feed`.

Both transports are mocked -- this exercises only the branching (`docs/25-ble-feed-frame.md`):
HTTP ok -> wifi; HTTP connection error + BLE configured -> BLE attempted; HTTP connection
error + no BLE configured -> unreachable; a non-connection HTTP error never triggers BLE.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from kibble.api import KibbleConnectionError, KibbleError
from kibble.ble_fallback import (
    CONTROL_PATH_BLUETOOTH,
    CONTROL_PATH_UNREACHABLE,
    CONTROL_PATH_WIFI,
    async_feed_with_fallback,
)


async def test_wifi_success_never_touches_ble() -> None:
    wifi_feed = AsyncMock()
    ble_feed = AsyncMock()

    outcome = await async_feed_with_fallback(wifi_feed, ble_feed)

    assert outcome.control_path == CONTROL_PATH_WIFI
    assert outcome.error is None
    wifi_feed.assert_awaited_once()
    ble_feed.assert_not_awaited()


async def test_wifi_connection_error_without_ble_address_is_unreachable() -> None:
    conn_err = KibbleConnectionError("agent.local timed out")
    wifi_feed = AsyncMock(side_effect=conn_err)

    outcome = await async_feed_with_fallback(wifi_feed, None)

    assert outcome.control_path == CONTROL_PATH_UNREACHABLE
    assert outcome.error is conn_err


async def test_wifi_connection_error_falls_back_to_ble_and_succeeds() -> None:
    wifi_feed = AsyncMock(side_effect=KibbleConnectionError("agent.local timed out"))
    ble_feed = AsyncMock()

    outcome = await async_feed_with_fallback(wifi_feed, ble_feed)

    assert outcome.control_path == CONTROL_PATH_BLUETOOTH
    assert outcome.error is None
    ble_feed.assert_awaited_once()


async def test_wifi_connection_error_and_ble_also_fails() -> None:
    ble_err = RuntimeError("no proxy sees the feeder")
    wifi_feed = AsyncMock(side_effect=KibbleConnectionError("agent.local timed out"))
    ble_feed = AsyncMock(side_effect=ble_err)

    outcome = await async_feed_with_fallback(wifi_feed, ble_feed)

    # Bluetooth is still the path that was *attempted*; the caller learns the real cause via
    # `.error`, but the sensor should show "bluetooth", not "unreachable" -- Wi-Fi is known
    # bad, and something was tried on the only remaining transport.
    assert outcome.control_path == CONTROL_PATH_BLUETOOTH
    assert outcome.error is ble_err


async def test_non_connection_error_does_not_fall_back_to_ble() -> None:
    """A 400/500 from the agent means Wi-Fi *worked* as a transport -- this is not a
    reachability problem, so BLE must not be tried even if it's configured."""
    app_err = KibbleError("amount must be 1..=20 portions")
    wifi_feed = AsyncMock(side_effect=app_err)
    ble_feed = AsyncMock()

    outcome = await async_feed_with_fallback(wifi_feed, ble_feed)

    assert outcome.control_path == CONTROL_PATH_WIFI
    assert outcome.error is app_err
    ble_feed.assert_not_awaited()


async def test_unexpected_exception_propagates_uncaught() -> None:
    """Anything that isn't a `KibbleError` at all (a bug, not a modeled failure mode) should
    not be silently reinterpreted as a control-path outcome."""
    wifi_feed = AsyncMock(side_effect=ValueError("boom"))

    with pytest.raises(ValueError, match="boom"):
        await async_feed_with_fallback(wifi_feed, AsyncMock())
