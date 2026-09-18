"""HA-side logic for the stack select (`GET`/`POST /mode`, `agent/src/mode.rs`): which feeder
userland -- the vendor's own Petkit stack or the open LibreFeed replacement -- is running, and
switching between them. Same fake-`self`/fake-session style as `test_cat_id.py` and
`test_media.py`: exercises the real, unbound `KibbleStackSelect`/`KibbleCoordinator` methods
without constructing a real Home-Assistant-backed entity or coordinator.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from kibble.api import KibbleClient, KibbleConnectionError, StackState
from kibble.coordinator import KibbleCoordinator
from kibble.select import KibbleStackSelect

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


# --- (1) current_option reflects GET /mode -----------------------------------------------------


async def test_current_option_reflects_the_get_mode_response() -> None:
    session = _RecordingSession(
        200, {"running": "librefeed", "next": "librefeed", "librefeed_installed": True}
    )
    client = KibbleClient(session, HOST, PORT)

    stack = await client.mode()

    fake_self = SimpleNamespace(coordinator=SimpleNamespace(data=SimpleNamespace(stack=stack)))
    assert KibbleStackSelect.current_option.fget(fake_self) == "librefeed"


# --- (2) selecting the other option POSTs {"mode": ...}; the current option is a no-op ---------


async def test_selecting_the_other_option_posts_the_new_mode_but_the_current_one_is_a_noop() -> (
    None
):
    session = _RecordingSession(200, {"ok": True, "next": "librefeed", "rebooting": True})
    client = KibbleClient(session, HOST, PORT)
    coordinator = SimpleNamespace(client=client)
    coordinator.async_set_mode = MethodType(KibbleCoordinator.async_set_mode, coordinator)

    fake_self = SimpleNamespace(
        coordinator=SimpleNamespace(
            data=SimpleNamespace(
                stack=StackState(running="vendor", next="vendor", librefeed_installed=True)
            ),
            async_set_mode=coordinator.async_set_mode,
        )
    )

    await KibbleStackSelect.async_select_option(fake_self, "librefeed")

    assert session.calls == [("POST", f"http://{HOST}:{PORT}/mode", {"mode": "librefeed"})]

    # Re-selecting the option that's already running must not touch the agent at all.
    session.calls.clear()
    fake_self.coordinator.data = SimpleNamespace(
        stack=StackState(running="librefeed", next="librefeed", librefeed_installed=True)
    )

    await KibbleStackSelect.async_select_option(fake_self, "librefeed")

    assert session.calls == []


# --- (3) GET /mode failing leaves the select unavailable, the rest of the poll still updates ---


def _coordinator_for_fetch(client: AsyncMock) -> KibbleCoordinator:
    """A real (uninitialized) `KibbleCoordinator` with only what `_fetch_all` reads set by
    hand -- same `object.__new__` approach as `test_coordinator_availability.py`."""
    coord = object.__new__(KibbleCoordinator)
    coord.client = client
    coord.entry = SimpleNamespace(options={})
    return coord


async def test_get_mode_failure_leaves_stack_none_while_the_rest_of_the_poll_still_updates() -> (
    None
):
    state = object()
    cloud = object()
    client = AsyncMock(
        state=AsyncMock(return_value=state),
        schedule=AsyncMock(return_value=object()),
        config=AsyncMock(return_value={}),
        cloud=AsyncMock(return_value=cloud),
        mode=AsyncMock(side_effect=KibbleConnectionError("agent.local: unknown route /mode")),
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

    # The one route this old agent doesn't have goes to None instead of raising out of the
    # whole poll cycle -- everything else fetched in the same batch is still fresh.
    assert data.stack is None
    assert data.cloud is cloud
    assert data.state is state

    fake_select = object.__new__(KibbleStackSelect)
    fake_select.coordinator = SimpleNamespace(last_update_success=True, data=data)
    assert fake_select.available is False
