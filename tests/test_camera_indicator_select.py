"""HA-side logic for the camera-indicator select (`GET`/`POST /led`'s `camera` field,
LibreFeed-only): same fake-`self`/fake-session style as `test_status_light.py` -- exercises the
real, unbound `KibbleCameraIndicatorSelect`/`KibbleCoordinator` methods without constructing a
real Home-Assistant-backed entity or coordinator.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kibble.api import FeederState, KibbleClient, KibbleNotFoundError, StackState
from kibble.coordinator import KibbleCoordinator
from kibble.select import KibbleCameraIndicatorSelect

HOST = "192.168.4.85"
PORT = 8765


class _FakeResponse:
    """Duck-types the subset of `aiohttp.ClientResponse` `api.py`'s `_request` actually uses."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type: str | None = None) -> Any:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _RecordingSession:
    """Duck-types the subset of `aiohttp.ClientSession` `api.py`'s `_request` actually calls,
    and records every outgoing request so a test can assert on the exact method/url/JSON body
    `KibbleClient` sent -- not just that some coordinator-level mock was awaited."""

    def __init__(self, status: int, body: Any) -> None:
        self._status = status
        self._body = body
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((method, url, kwargs.get("json")))
        return _FakeResponse(self._status, self._body)


# --- (1) GET /led parsed -> current_option maps camera's three wire states both ways -----------


@pytest.mark.parametrize(
    ("camera_wire", "expected_option"),
    [("auto", "auto"), (1, "on"), (0, "off")],
)
async def test_current_option_reflects_the_three_wire_states(
    camera_wire: str | int, expected_option: str
) -> None:
    session = _RecordingSession(200, {"white": "auto", "green": 0, "camera": camera_wire})
    led = await KibbleClient(session, HOST, PORT).led()

    fake_self = SimpleNamespace(coordinator=SimpleNamespace(data=SimpleNamespace(led=led)))
    assert KibbleCameraIndicatorSelect.current_option.fget(fake_self) == expected_option


# --- (2) selecting each option POSTs the matching wire value -----------------------------------


@pytest.mark.parametrize(
    ("option", "expected_payload"),
    [("auto", {"camera": "auto"}), ("on", {"camera": 1}), ("off", {"camera": 0})],
)
async def test_selecting_each_option_posts_the_matching_wire_value(
    option: str, expected_payload: dict[str, Any]
) -> None:
    session = _RecordingSession(200, {"white": "auto", "green": 0, "camera": 0})
    client = KibbleClient(session, HOST, PORT)
    coordinator = SimpleNamespace(client=client, async_request_refresh=AsyncMock())
    coordinator.async_set_led = MethodType(KibbleCoordinator.async_set_led, coordinator)

    fake_self = SimpleNamespace(coordinator=coordinator)

    await KibbleCameraIndicatorSelect.async_select_option(fake_self, option)

    assert session.calls == [("POST", f"http://{HOST}:{PORT}/led", expected_payload)]


# --- (3) GET /led 404ing (vendor stack) leaves data.led None; the entity is unavailable, the ----
# --- rest of the poll still succeeds -------------------------------------------------------------


def _coordinator_for_fetch(client: AsyncMock) -> KibbleCoordinator:
    """A real (uninitialized) `KibbleCoordinator` with only what `_fetch_all` reads set by
    hand -- same `object.__new__` approach as `test_status_light.py`."""
    coord = object.__new__(KibbleCoordinator)
    coord.client = client
    coord.entry = SimpleNamespace(options={})
    return coord


async def test_get_led_404_leaves_led_none_while_the_rest_of_the_poll_still_updates() -> None:
    state = FeederState.from_json({})
    cloud = object()
    client = AsyncMock(
        state=AsyncMock(return_value=state),
        schedule=AsyncMock(return_value=object()),
        config=AsyncMock(return_value={}),
        cloud=AsyncMock(return_value=cloud),
        mode=AsyncMock(return_value=StackState.from_json({"running": "vendor"})),
        led=AsyncMock(side_effect=KibbleNotFoundError("not found")),
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
    coord = _coordinator_for_fetch(client)

    data = await coord._fetch_all()

    # The vendor stack's missing /led route goes to None instead of raising out of the whole
    # poll cycle -- everything else fetched in the same batch is still fresh.
    assert data.led is None
    assert data.cloud is cloud
    assert data.state is state

    fake_select = object.__new__(KibbleCameraIndicatorSelect)
    fake_select.coordinator = SimpleNamespace(last_update_success=True, data=data)
    assert fake_select.available is False
