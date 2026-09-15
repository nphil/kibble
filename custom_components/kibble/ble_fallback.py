"""The Wi-Fi-first, BLE-fallback decision for `kibble.feed`.

Kept free of any Home Assistant or transport-specific import on purpose: this is the one part
of the fallback design with real branching to get right, so it is exercised directly with the
Wi-Fi and BLE calls mocked out (`test_ble_fallback.py`), instead of only indirectly through a
full `KibbleCoordinator` + `HomeAssistant` instance.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .api import KibbleConnectionError, KibbleError

CONTROL_PATH_WIFI = "wifi"
CONTROL_PATH_BLUETOOTH = "bluetooth"
CONTROL_PATH_UNREACHABLE = "unreachable"
CONTROL_PATHS = (CONTROL_PATH_WIFI, CONTROL_PATH_BLUETOOTH, CONTROL_PATH_UNREACHABLE)


@dataclass(frozen=True, slots=True)
class FeedAttempt:
    """The outcome of one `async_feed_with_fallback` call.

    `error` is set whenever the feed did not happen: Wi-Fi reached the agent but the agent
    rejected the call, Wi-Fi was unreachable with no fallback configured, or both transports
    were tried and both failed. The caller re-raises it to the service call; `control_path` is
    recorded regardless, so the "Control path" sensor reflects reality even when the call
    itself ultimately errors.
    """

    control_path: str
    error: Exception | None = None


async def async_feed_with_fallback(
    wifi_feed: Callable[[], Awaitable[None]],
    ble_feed: Callable[[], Awaitable[None]] | None,
) -> FeedAttempt:
    """Try `wifi_feed()`; on a *connection* error, try `ble_feed()` if one was given.

    - Wi-Fi succeeds -> ``("wifi", None)``.
    - Wi-Fi reachable but rejects the request (bad input, agent-side error) -> ``("wifi", the
      error)`` -- the transport worked, so this is not a fallback scenario.
    - Wi-Fi connection error, no `ble_feed` configured -> ``("unreachable", the connection
      error)``.
    - Wi-Fi connection error, `ble_feed` given and it succeeds -> ``("bluetooth", None)``.
    - Wi-Fi connection error, `ble_feed` given and it also raises -> ``("bluetooth", the BLE
      error)`` -- Bluetooth is still the path that was attempted; the error says it didn't
      land.
    """
    try:
        await wifi_feed()
    except KibbleConnectionError as err:
        if ble_feed is None:
            return FeedAttempt(control_path=CONTROL_PATH_UNREACHABLE, error=err)
        try:
            await ble_feed()
        except Exception as ble_err:
            # Deliberately broad: any BLE failure still means "bluetooth was the attempted
            # path" for the sensor; the specific error is surfaced to the caller, not swallowed.
            return FeedAttempt(control_path=CONTROL_PATH_BLUETOOTH, error=ble_err)
        return FeedAttempt(control_path=CONTROL_PATH_BLUETOOTH, error=None)
    except KibbleError as err:
        return FeedAttempt(control_path=CONTROL_PATH_WIFI, error=err)
    return FeedAttempt(control_path=CONTROL_PATH_WIFI, error=None)
