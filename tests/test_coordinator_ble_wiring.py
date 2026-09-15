"""`KibbleCoordinator._ble_feed`: the one seam connecting the fallback decision logic
(`test_ble_fallback.py`, which mocks the BLE call entirely) to the real transport
(`test_ble_transport.py`, which tests `ble.async_feed` standalone). Exercises the actual
`KibbleCoordinator` method with a duck-typed `self` -- constructing a full `DataUpdateCoordinator`
needs a real `HomeAssistant` core instance, which is plumbing this repo has no fixture for and
this test doesn't need: the only thing worth pinning here is that the closure `_ble_feed`
builds calls `ble.async_feed` with the arguments it actually expects.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from kibble import ble
from kibble.const import CONF_BLE_ADDRESS
from kibble.coordinator import KibbleCoordinator

HASS = object()


def _fake_coordinator(options: dict) -> SimpleNamespace:
    """A stand-in for `self` with just what `_ble_feed` reads: `.hass` and `.entry.options`."""
    return SimpleNamespace(hass=HASS, entry=SimpleNamespace(options=options))


def test_no_configured_address_yields_no_ble_attempt() -> None:
    fake_self = _fake_coordinator({})
    assert KibbleCoordinator._ble_feed(fake_self, "1", 5, "abc") is None


def test_blank_address_also_yields_no_ble_attempt() -> None:
    fake_self = _fake_coordinator({CONF_BLE_ADDRESS: ""})
    assert KibbleCoordinator._ble_feed(fake_self, "1", 5, "abc") is None


async def test_configured_address_calls_ble_async_feed_with_matching_arguments(
    monkeypatch,
) -> None:
    recorded = AsyncMock()
    monkeypatch.setattr(ble, "async_feed", recorded)

    fake_self = _fake_coordinator({CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF"})
    attempt = KibbleCoordinator._ble_feed(fake_self, "both", 12, "feed-1")
    assert attempt is not None

    await attempt()

    recorded.assert_awaited_once_with(
        HASS, "AA:BB:CC:DD:EE:FF", hopper="both", amount=12, feed_id="feed-1"
    )
