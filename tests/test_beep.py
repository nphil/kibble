"""HA-side logic for the MCU-buzzer beep entity/service (`POST /beep`, LibreFeed-only): same
fake-`self`/fake-session style as `test_status_light.py` -- exercises the real, unbound
`KibbleBeepButton`/`KibbleClient` without constructing a real Home-Assistant-backed entity or
coordinator. `BEEP_SCHEMA` itself is loaded via the same direct-file-load technique as
`test_service_validation.py`: `conftest.py` deliberately stubs `sys.modules["kibble"]` to a
namespace package that skips running the real `__init__.py`, so this loads it under a distinct
module name to reach the module-level schema.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kibble.api import KibbleClient
from kibble.button import KibbleBeepButton
from kibble.coordinator import KibbleCoordinator

_spec = importlib.util.spec_from_file_location(
    "kibble.__init__",
    pathlib.Path(__file__).parent.parent / "custom_components" / "kibble" / "__init__.py",
    submodule_search_locations=[],
)
_kibble_init = importlib.util.module_from_spec(_spec)
_kibble_init.__package__ = "kibble"
_spec.loader.exec_module(_kibble_init)
BEEP_SCHEMA = _kibble_init.BEEP_SCHEMA
# Home Assistant's own `install_as_voluptuous()` shadows the real `voluptuous` module with its
# `probatio` shim -- but only for imports that happen *after* it runs. Sourcing `Invalid` from
# `_kibble_init`'s own `vol` binding (rather than a fresh top-level `import voluptuous as vol`
# in this file, which may bind to the pre-shadow module depending on collection order)
# guarantees it is the exact exception class `BEEP_SCHEMA` actually raises.
vol = _kibble_init.vol

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


# --- (1) the button is unavailable when the LibreFeed `led` marker is None, available otherwise -


def test_available_when_led_data_present_unavailable_when_none() -> None:
    button = object.__new__(KibbleBeepButton)
    button.coordinator = SimpleNamespace(
        last_update_success=True, data=SimpleNamespace(led=None)
    )
    assert button.available is False

    button.coordinator.data.led = object()
    assert button.available is True


# --- (2) pressing the button calls the api with the defaults (count=2, on_ms=100, off_ms=100) --


async def test_press_calls_api_with_defaults() -> None:
    session = _RecordingSession(200, {"ok": True, "count": 2, "on_ms": 100, "off_ms": 100})
    client = KibbleClient(session, HOST, PORT)
    coordinator = SimpleNamespace(client=client, async_request_refresh=AsyncMock())
    coordinator.async_beep = MethodType(KibbleCoordinator.async_beep, coordinator)

    button = object.__new__(KibbleBeepButton)
    button.coordinator = coordinator

    await button.async_press()

    assert session.calls == [
        ("POST", f"http://{HOST}:{PORT}/beep", {"count": 2, "on_ms": 100, "off_ms": 100})
    ]
    coordinator.async_request_refresh.assert_awaited_once()


# --- (3) the service schema clamps/validates: an out-of-range count is rejected -----------------


def test_schema_rejects_out_of_range_count() -> None:
    with pytest.raises(vol.Invalid):
        BEEP_SCHEMA({"device_id": "abc123", "count": 11})

    with pytest.raises(vol.Invalid):
        BEEP_SCHEMA({"device_id": "abc123", "count": 0})


def test_schema_rejects_out_of_range_on_ms_and_off_ms() -> None:
    with pytest.raises(vol.Invalid):
        BEEP_SCHEMA({"device_id": "abc123", "on_ms": 19})

    with pytest.raises(vol.Invalid):
        BEEP_SCHEMA({"device_id": "abc123", "on_ms": 2001})

    with pytest.raises(vol.Invalid):
        BEEP_SCHEMA({"device_id": "abc123", "off_ms": -1})

    with pytest.raises(vol.Invalid):
        BEEP_SCHEMA({"device_id": "abc123", "off_ms": 2001})


def test_schema_applies_defaults_when_fields_omitted() -> None:
    validated = BEEP_SCHEMA({"device_id": "abc123"})
    assert validated == {"device_id": "abc123", "count": 2, "on_ms": 100, "off_ms": 100}
