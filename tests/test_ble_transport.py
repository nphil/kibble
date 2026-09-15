"""`kibble/ble.py`: the GATT transport itself, with `homeassistant.components.bluetooth` and
`bleak_retry_connector.establish_connection` swapped for fakes -- no real adapter or device
needed. `docs/25-ble-feed-frame.md` "Live transport proof" covers the real-hardware side of
this; these tests cover the logic bleak/HA don't exercise for us: which characteristic gets
written, what happens when nothing is found, and that a hung notify doesn't hang the caller.
"""

from __future__ import annotations

import asyncio

import pytest
from bleak.exc import BleakError
from kibble import ble
from kibble.frame import CHAR_NOTIFY_UUID, CHAR_WRITE_UUID, decode_feed_frame

ADDRESS = "AA:BB:CC:DD:EE:FF"
SENTINEL_DEVICE = object()  # stands in for a real `BLEDevice`; ble.py never inspects it


class _FakeCharacteristic:
    def __init__(self, uuid: str, properties: list[str]) -> None:
        self.uuid = uuid
        self.properties = properties


class _FakeService:
    def __init__(self, characteristics: list[_FakeCharacteristic]) -> None:
        self.characteristics = characteristics


class _FakeBleakClient:
    """Duck-types the subset of `BleakClient` `ble.py` actually calls."""

    def __init__(self, *, fire_notify: bool = True, fail_write: bool = False) -> None:
        self.fire_notify = fire_notify
        self.fail_write = fail_write
        self.writes: list[tuple[str, bytes, bool | None]] = []
        self.disconnected = False
        self._notify_callback = None
        self.services = [
            _FakeService(
                [
                    _FakeCharacteristic(CHAR_WRITE_UUID, ["write"]),
                    _FakeCharacteristic(CHAR_NOTIFY_UUID, ["notify"]),
                ]
            )
        ]

    async def start_notify(self, uuid, callback) -> None:
        self._notify_callback = callback

    async def write_gatt_char(self, uuid, data, response=None) -> None:
        if self.fail_write:
            raise BleakError("write rejected")
        self.writes.append((uuid, bytes(data), response))
        if self.fire_notify:
            self._notify_callback(None, bytearray(b"{}"))

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture(autouse=True)
def _fast_notify_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real 5s notify wait would make a timeout test slow for no reason."""
    monkeypatch.setattr(ble, "NOTIFY_TIMEOUT_S", 0.05)


def _patch_device_found(monkeypatch: pytest.MonkeyPatch, found: bool = True) -> None:
    monkeypatch.setattr(
        ble.bluetooth,
        "async_ble_device_from_address",
        lambda hass, address, connectable=True: SENTINEL_DEVICE if found else None,
    )


async def test_device_not_visible_to_any_proxy_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_device_found(monkeypatch, found=False)

    with pytest.raises(ble.BleFeedError, match="not visible to any Bluetooth proxy"):
        await ble.async_feed(hass=object(), address=ADDRESS, hopper="1", amount=5)


async def test_connect_failure_is_wrapped_as_ble_feed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_device_found(monkeypatch)

    async def _boom(*_args, **_kwargs):
        raise BleakError("no route to proxy")

    monkeypatch.setattr(ble, "establish_connection", _boom)

    with pytest.raises(ble.BleFeedError, match="could not connect"):
        await ble.async_feed(hass=object(), address=ADDRESS, hopper="1", amount=5)


async def test_writes_the_correctly_translated_frame_to_the_write_characteristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_device_found(monkeypatch)
    fake_client = _FakeBleakClient()

    async def _connect(*_args, **_kwargs):
        return fake_client

    monkeypatch.setattr(ble, "establish_connection", _connect)

    ok = await ble.async_feed(
        hass=object(), address=ADDRESS, hopper="2", amount=8, feed_id="unit-test"
    )

    assert ok is True
    assert len(fake_client.writes) == 1
    uuid, data, response = fake_client.writes[0]
    assert uuid == CHAR_WRITE_UUID
    assert response is True
    decoded = decode_feed_frame(data)
    # hopper "2" -> (amount1=0, amount2=amount), matching agent/src/main.rs::feed()
    assert (decoded.amount1, decoded.amount2) == (0, 8)
    assert decoded.feed_id == "unit-test"
    assert decoded.cancel is False


async def test_returns_false_when_notify_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_device_found(monkeypatch)
    fake_client = _FakeBleakClient(fire_notify=False)

    async def _connect(*_args, **_kwargs):
        return fake_client

    monkeypatch.setattr(ble, "establish_connection", _connect)

    ok = await ble.async_feed(hass=object(), address=ADDRESS, hopper="1", amount=1)

    assert ok is False
    assert fake_client.disconnected is True  # still cleaned up despite the timeout


async def test_disconnects_even_when_the_write_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_device_found(monkeypatch)
    fake_client = _FakeBleakClient(fail_write=True)

    async def _connect(*_args, **_kwargs):
        return fake_client

    monkeypatch.setattr(ble, "establish_connection", _connect)

    with pytest.raises(ble.BleFeedError, match="GATT write"):
        await ble.async_feed(hass=object(), address=ADDRESS, hopper="1", amount=1)

    assert fake_client.disconnected is True


async def test_probe_reports_characteristic_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_device_found(monkeypatch)
    fake_client = _FakeBleakClient()

    async def _connect(*_args, **_kwargs):
        return fake_client

    monkeypatch.setattr(ble, "establish_connection", _connect)

    result = await ble.async_probe(hass=object(), address=ADDRESS)

    assert result[CHAR_WRITE_UUID] == "write"
    assert result[CHAR_NOTIFY_UUID] == "notify"
    assert fake_client.disconnected is True


async def test_connect_is_bounded_by_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy that accepts the connection but never finishes negotiating must not hang the
    service call forever."""
    _patch_device_found(monkeypatch)
    monkeypatch.setattr(ble, "CONNECT_TIMEOUT_S", 0.05)

    async def _hangs(*_args, **_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(ble, "establish_connection", _hangs)

    with pytest.raises(ble.BleFeedError, match="could not connect"):
        await ble.async_feed(hass=object(), address=ADDRESS, hopper="1", amount=1)
