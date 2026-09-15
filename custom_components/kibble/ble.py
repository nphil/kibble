"""BLE fallback transport for `kibble.feed`.

Used only when the agent's HTTP API is unreachable and a `ble_address` is configured
(`coordinator.py` imports this module lazily, inside `async_feed`, precisely so a feeder with
no BLE fallback configured never pulls bleak/bluetooth into the running event loop). See
`docs/25-ble-feed-frame.md` for the wire format and `docs/17-ble-fallback.md` for why this
exists at all.

Reaches the feeder's T31 dispenser MCU (the only BLE radio on the device -- the Wi-Fi module's
Bluetooth is unreachable, `docs/17-ble-fallback.md` SS1) through whichever ESPHome Bluetooth
proxy currently sees it, using Home Assistant's own `bluetooth` integration (`bleak-esphome`)
to resolve the address to a live `BLEDevice`, then `bleak-retry-connector` for a GATT
connection resilient to a proxy that drops mid-negotiation.
"""

from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .frame import CHAR_NOTIFY_UUID, CHAR_WRITE_UUID, encode_feed_frame, hopper_amounts

_LOGGER = logging.getLogger(__name__)

# No ack protocol exists on the device side yet -- the type is new, and stock `ctrl` no-ops it
# by design (docs/25-ble-feed-frame.md). A missing notify is therefore expected today, not an
# error; this only bounds how long a feed call blocks waiting for one that may never come.
NOTIFY_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 15.0


class BleFeedError(Exception):
    """The command was not delivered: no proxy currently sees the device, the GATT connection
    failed, or the write itself failed. Does not cover a merely missing/timed-out notify
    reply -- see `async_feed`'s return value for that."""


async def async_feed(
    hass: HomeAssistant,
    address: str,
    *,
    hopper: str,
    amount: int,
    feed_id: str | None = None,
    cancel: bool = False,
) -> bool:
    """Write a Kibble feed frame to `address` via whichever Bluetooth proxy currently sees it.

    Returns True if a notify reply arrived on 0xAAA1 before `NOTIFY_TIMEOUT_S`, False if the
    write completed but nothing notified back (expected until a Kibble `ctrl` replacement is
    the one answering on-device). Raises `BleFeedError` for anything that means the command
    almost certainly never reached the device at all.
    """
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise BleFeedError(f"{address} is not visible to any Bluetooth proxy right now")

    amount1, amount2 = hopper_amounts(hopper, amount)
    frame = encode_feed_frame(cancel=cancel, feed_id=feed_id, amount1=amount1, amount2=amount2)

    notified = asyncio.Event()

    def _on_notify(_characteristic: BleakGATTCharacteristic, _data: bytearray) -> None:
        notified.set()

    try:
        client = await asyncio.wait_for(
            establish_connection(BleakClient, ble_device, address), timeout=CONNECT_TIMEOUT_S
        )
    except (BleakError, TimeoutError) as err:
        raise BleFeedError(f"could not connect to {address}: {err}") from err

    try:
        await client.start_notify(CHAR_NOTIFY_UUID, _on_notify)
        await client.write_gatt_char(CHAR_WRITE_UUID, frame, response=True)
        try:
            await asyncio.wait_for(notified.wait(), timeout=NOTIFY_TIMEOUT_S)
        except TimeoutError:
            _LOGGER.debug(
                "%s: wrote feed frame, no notify within %ss (expected until Kibble's own "
                "ctrl replacement exists on-device)",
                address,
                NOTIFY_TIMEOUT_S,
            )
            return False
        return True
    except BleakError as err:
        raise BleFeedError(f"GATT write to {address} failed: {err}") from err
    finally:
        await client.disconnect()


async def async_probe(hass: HomeAssistant, address: str) -> dict[str, str]:
    """Connect to `address` and read back its GATT services -- no write, used only to prove
    the transport works (`docs/25-ble-feed-frame.md` "Live transport proof"). Returns
    ``{characteristic_uuid: "read"|"write"|"notify"|... (comma-joined properties)}``.
    """
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise BleFeedError(f"{address} is not visible to any Bluetooth proxy right now")

    try:
        client = await asyncio.wait_for(
            establish_connection(BleakClient, ble_device, address), timeout=CONNECT_TIMEOUT_S
        )
    except (BleakError, TimeoutError) as err:
        raise BleFeedError(f"could not connect to {address}: {err}") from err

    try:
        return {
            str(char.uuid): ",".join(char.properties)
            for service in client.services
            for char in service.characteristics
        }
    finally:
        await client.disconnect()
