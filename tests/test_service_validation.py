"""Direct regression test for `__init__.py`'s `kibble.upload_face_sample` service handler:
malformed `jpeg_b64` must fail with a clean, translated `ServiceValidationError` instead of
`base64.b64decode`'s own uncaught `binascii.Error` -- and must fail *before* resolving a
device, so a bad payload never has to succeed at that first (and it does not depend on device
resolution or the agent to say so).

Uses the same direct-file-load technique as `test_platform_isolation.py`: `conftest.py`
deliberately stubs `sys.modules["kibble"]` to a namespace package that skips running the real
`__init__.py`, so this loads it under a distinct module name to reach
`_async_register_services`'s handler closures, which nothing else needs.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import ServiceValidationError

_spec = importlib.util.spec_from_file_location(
    "kibble.__init__",
    pathlib.Path(__file__).parent.parent / "custom_components" / "kibble" / "__init__.py",
    submodule_search_locations=[],
)
_kibble_init = importlib.util.module_from_spec(_spec)
_kibble_init.__package__ = "kibble"
_spec.loader.exec_module(_kibble_init)


def _register_and_capture() -> dict[str, object]:
    """Runs `_async_register_services` against a `hass` stub that only supports the two
    `hass.services` calls it makes, capturing each registered handler by service name. No
    `config_entries`/`entity_registry` access is provided -- if a handler ever reached
    `_coordinator_for_device` before validating its own input, it would raise `AttributeError`
    here instead of the expected `ServiceValidationError`."""
    handlers: dict[str, object] = {}

    def register(domain, service, handler, schema=None, supports_response=None):
        handlers[service] = handler

    hass = SimpleNamespace(
        services=SimpleNamespace(has_service=lambda domain, service: False, async_register=register)
    )
    _kibble_init._async_register_services(hass)
    return handlers


async def test_upload_face_sample_rejects_malformed_base64_before_resolving_a_device() -> None:
    handler = _register_and_capture()[_kibble_init.SERVICE_UPLOAD_FACE_SAMPLE]
    call = SimpleNamespace(
        data={"device_id": "unresolved", "cat": "Kitty", "jpeg_b64": "not-valid-base64!!"}
    )

    with pytest.raises(ServiceValidationError) as exc_info:
        await handler(call)

    assert exc_info.value.translation_key == "invalid_jpeg_data"
    assert exc_info.value.translation_domain == _kibble_init.DOMAIN
