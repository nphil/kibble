"""HA-side logic for the status-light entity (`GET`/`POST /led`, LibreFeed-only): same fake-
`self`/fake-session style as `test_stack_select.py` -- exercises the real, unbound
`KibbleStatusLight`/`KibbleCoordinator` methods without constructing a real Home-Assistant-
backed entity or coordinator.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from kibble.api import KibbleClient, KibbleNotFoundError
from kibble.coordinator import KibbleCoordinator
from kibble.light import KibbleStatusLight

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


# --- (1) GET /led parsed -> is_on/effect correct for auto and for forced off -------------------


async def test_get_led_parsed_reflects_is_on_and_effect_for_auto_and_off() -> None:
    auto_session = _RecordingSession(200, {"white": "auto", "green": 1})
    auto_led = await KibbleClient(auto_session, HOST, PORT).led()

    fake_auto = SimpleNamespace(coordinator=SimpleNamespace(data=SimpleNamespace(led=auto_led)))
    assert KibbleStatusLight.is_on.fget(fake_auto) is True
    assert KibbleStatusLight.effect.fget(fake_auto) == "auto"
    assert KibbleStatusLight.extra_state_attributes.fget(fake_auto) == {"green": True}

    off_session = _RecordingSession(200, {"white": 0, "green": 0})
    off_led = await KibbleClient(off_session, HOST, PORT).led()

    fake_off = SimpleNamespace(coordinator=SimpleNamespace(data=SimpleNamespace(led=off_led)))
    assert KibbleStatusLight.is_on.fget(fake_off) is False
    # 0 is a forced-off value, not one of the three forced-on effects -- no effect applies.
    assert KibbleStatusLight.effect.fget(fake_off) is None


# --- (2) turn_on(effect="blink") POSTs {"white": 2}; turn_off POSTs {"white": 0} ----------------


async def test_turn_on_with_effect_posts_forced_white_and_turn_off_posts_zero() -> None:
    session = _RecordingSession(200, {"white": 2, "green": 0})
    client = KibbleClient(session, HOST, PORT)
    coordinator = SimpleNamespace(client=client, async_request_refresh=AsyncMock())
    coordinator.async_set_led = MethodType(KibbleCoordinator.async_set_led, coordinator)

    fake_self = SimpleNamespace(coordinator=coordinator)

    await KibbleStatusLight.async_turn_on(fake_self, effect="blink")

    assert session.calls == [("POST", f"http://{HOST}:{PORT}/led", {"white": 2})]

    session.calls.clear()
    await KibbleStatusLight.async_turn_off(fake_self)

    assert session.calls == [("POST", f"http://{HOST}:{PORT}/led", {"white": 0})]


# --- (3) GET /led 404ing (vendor stack) leaves data.led None; the entity is unavailable, the ----
# --- rest of the poll still succeeds -------------------------------------------------------------


def _coordinator_for_fetch(client: AsyncMock) -> KibbleCoordinator:
    """A real (uninitialized) `KibbleCoordinator` with only what `_fetch_all` reads set by
    hand -- same `object.__new__` approach as `test_stack_select.py`."""
    coord = object.__new__(KibbleCoordinator)
    coord.client = client
    coord.entry = SimpleNamespace(options={})
    return coord


async def test_get_led_404_leaves_led_none_while_the_rest_of_the_poll_still_updates() -> None:
    state = object()
    cloud = object()
    client = AsyncMock(
        state=AsyncMock(return_value=state),
        schedule=AsyncMock(return_value=object()),
        config=AsyncMock(return_value={}),
        cloud=AsyncMock(return_value=cloud),
        mode=AsyncMock(return_value=object()),
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

    fake_light = object.__new__(KibbleStatusLight)
    fake_light.coordinator = SimpleNamespace(last_update_success=True, data=data)
    assert fake_light.available is False
