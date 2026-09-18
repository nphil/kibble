"""HA-side logic for the desiccant button + `kibble.set_desiccant` service (`GET`/`POST
/desiccant`, LibreFeed-only): same fake-session/fake-`self` style as `test_status_light.py`.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from kibble.api import DesiccantState, KibbleClient, KibbleNotFoundError
from kibble.button import KibbleReplaceDesiccantButton
from kibble.coordinator import KibbleCoordinator

HOST = "192.168.4.85"
PORT = 8765

# Same direct-file-load technique as `test_platform_isolation.py`/`test_service_validation.py`:
# `conftest.py` stubs `sys.modules["kibble"]` to skip running the real `__init__.py`, so this
# loads it under a distinct module name to reach `SET_DESICCANT_SCHEMA`/
# `_async_register_services`'s `handle_set_desiccant` closure, which nothing else needs.
_spec = importlib.util.spec_from_file_location(
    "kibble.__init__",
    pathlib.Path(__file__).parent.parent / "custom_components" / "kibble" / "__init__.py",
    submodule_search_locations=[],
)
_kibble_init = importlib.util.module_from_spec(_spec)
_kibble_init.__package__ = "kibble"
_spec.loader.exec_module(_kibble_init)
SET_DESICCANT_SCHEMA = _kibble_init.SET_DESICCANT_SCHEMA
# Home Assistant's own `install_as_voluptuous()` shadows the real `voluptuous` module with its
# `probatio` shim -- but only for imports that happen *after* it runs. Sourcing `Invalid` from
# `_kibble_init`'s own `vol` binding (rather than a fresh top-level `import voluptuous as vol`
# in this file, which may bind to the pre-shadow module depending on collection order)
# guarantees it is the exact exception class `SET_DESICCANT_SCHEMA` actually raises.
vol = _kibble_init.vol


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
    and records every outgoing request."""

    def __init__(self, status: int, body: Any) -> None:
        self._status = status
        self._body = body
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((method, url, kwargs.get("json")))
        return _FakeResponse(self._status, self._body)


# --- (1) GET /desiccant parsed -------------------------------------------------------------------


async def test_get_desiccant_parses_days_left_replaced_unix_and_interval_days() -> None:
    session = _RecordingSession(
        200, {"days_left": 12, "replaced_unix": 1700000000, "interval_days": 30}
    )
    state = await KibbleClient(session, HOST, PORT).desiccant()
    assert state == DesiccantState(days_left=12, replaced_unix=1700000000, interval_days=30)


# --- (2) POST /desiccant sends exactly the one field given ---------------------------------------


async def test_set_desiccant_replaced_posts_only_replaced() -> None:
    session = _RecordingSession(
        200, {"days_left": 30, "replaced_unix": 1700000000, "interval_days": 30}
    )
    await KibbleClient(session, HOST, PORT).set_desiccant(replaced=True)
    assert session.calls == [("POST", f"http://{HOST}:{PORT}/desiccant", {"replaced": True})]


async def test_set_desiccant_days_left_posts_only_days_left() -> None:
    session = _RecordingSession(200, {"days_left": 5, "replaced_unix": 0, "interval_days": 30})
    await KibbleClient(session, HOST, PORT).set_desiccant(days_left=5)
    assert session.calls == [("POST", f"http://{HOST}:{PORT}/desiccant", {"days_left": 5})]


# --- (3) GET /desiccant 404ing (vendor stack) leaves data.desiccant None, poll still succeeds ----


def _coordinator_for_fetch(client: AsyncMock) -> KibbleCoordinator:
    """A real (uninitialized) `KibbleCoordinator` with only what `_fetch_all` reads set by
    hand -- same `object.__new__` approach as `test_status_light.py`."""
    coord = object.__new__(KibbleCoordinator)
    coord.client = client
    coord.entry = SimpleNamespace(options={})
    return coord


async def test_get_desiccant_404_leaves_desiccant_none_while_the_rest_of_the_poll_still_updates() -> None:
    state = object()
    cloud = object()
    client = AsyncMock(
        state=AsyncMock(return_value=state),
        schedule=AsyncMock(return_value=object()),
        config=AsyncMock(return_value={}),
        cloud=AsyncMock(return_value=cloud),
        mode=AsyncMock(return_value=object()),
        led=AsyncMock(return_value=object()),
        desiccant=AsyncMock(side_effect=KibbleNotFoundError("not found")),
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

    # The vendor stack's missing /desiccant route goes to None instead of raising out of the
    # whole poll cycle -- everything else fetched in the same batch is still fresh.
    assert data.desiccant is None
    assert data.cloud is cloud
    assert data.state is state

    fake_button = object.__new__(KibbleReplaceDesiccantButton)
    fake_button.coordinator = SimpleNamespace(last_update_success=True, data=data)
    assert fake_button.available is False


# --- (4) button press writes {"replaced": true} ---------------------------------------------------


async def test_replace_button_press_posts_replaced_true() -> None:
    session = _RecordingSession(
        200, {"days_left": 30, "replaced_unix": 1700000000, "interval_days": 30}
    )
    client = KibbleClient(session, HOST, PORT)
    coordinator = SimpleNamespace(client=client, async_request_refresh=AsyncMock())
    coordinator.async_set_desiccant = MethodType(KibbleCoordinator.async_set_desiccant, coordinator)

    fake_self = SimpleNamespace(coordinator=coordinator)
    await KibbleReplaceDesiccantButton.async_press(fake_self)

    assert session.calls == [("POST", f"http://{HOST}:{PORT}/desiccant", {"replaced": True})]
    coordinator.async_request_refresh.assert_awaited_once()


# --- (5) async_set_desiccant with both days_left and interval_days issues two sequential --------
# --- single-field POSTs, never combining fields into one body (the agent 400s on that) ----------


async def test_async_set_desiccant_with_both_fields_issues_two_sequential_single_field_posts() -> None:
    session = _RecordingSession(200, {"days_left": 5, "replaced_unix": 0, "interval_days": 45})
    client = KibbleClient(session, HOST, PORT)
    coordinator = SimpleNamespace(client=client, async_request_refresh=AsyncMock())
    coordinator.async_set_desiccant = MethodType(KibbleCoordinator.async_set_desiccant, coordinator)

    await coordinator.async_set_desiccant(days_left=5, interval_days=45)

    assert session.calls == [
        ("POST", f"http://{HOST}:{PORT}/desiccant", {"days_left": 5}),
        ("POST", f"http://{HOST}:{PORT}/desiccant", {"interval_days": 45}),
    ]
    coordinator.async_request_refresh.assert_awaited_once()


# --- (6) kibble.set_desiccant service schema/handler ----------------------------------------------


def test_set_desiccant_schema_rejects_a_call_with_neither_field() -> None:
    with pytest.raises(vol.Invalid):
        _kibble_init.SET_DESICCANT_SCHEMA({"device_id": "abc"})


def test_set_desiccant_schema_accepts_device_id_omitted() -> None:
    validated = _kibble_init.SET_DESICCANT_SCHEMA({"days_left": 10})
    assert "device_id" not in validated
    assert validated["days_left"] == 10


def test_set_desiccant_schema_enforces_writable_ranges() -> None:
    with pytest.raises(vol.Invalid):
        _kibble_init.SET_DESICCANT_SCHEMA({"days_left": 400})
    with pytest.raises(vol.Invalid):
        _kibble_init.SET_DESICCANT_SCHEMA({"interval_days": 0})


async def test_handle_set_desiccant_resolves_the_only_loaded_feeder_when_device_id_is_omitted() -> None:
    """`device_id` is optional at the schema level (`SET_DESICCANT_SCHEMA`'s `vol.Optional`) --
    the handler must read it with `.get`, not `[]`, or a call that omits it would `KeyError`
    before ever reaching `_coordinator_for_device`'s own no-device-id resolution."""
    coordinator = SimpleNamespace(async_set_desiccant=AsyncMock())
    entry = SimpleNamespace(state=_kibble_init.ConfigEntryState.LOADED, runtime_data=coordinator)
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
        services=SimpleNamespace(has_service=lambda domain, service: False, async_register=lambda *a, **k: None),
    )
    handlers: dict[str, object] = {}
    hass.services.async_register = lambda domain, service, handler, schema=None, supports_response=None: handlers.__setitem__(service, handler)
    _kibble_init._async_register_services(hass)

    call = SimpleNamespace(data={"days_left": 7})
    await handlers[_kibble_init.SERVICE_SET_DESICCANT](call)

    coordinator.async_set_desiccant.assert_awaited_once_with(days_left=7, interval_days=None)
