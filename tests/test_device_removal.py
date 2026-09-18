"""Direct regression test for `__init__.py`'s `async_remove_config_entry_device`: without this
hook, HA refuses every device-delete request against a `kibble` device -- including an orphan
left behind by an earlier bug (an empty-serial registration, or any other stale device whose
serial no longer matches the config entry) that will never poll again and has no config-entry-
owned entities left to clean it up. The live feeder -- the device whose `(DOMAIN, serial)`
identifier matches the entry's own unique id (`config_flow.py` sets it to `state.serial`) --
must never be removable this way.

Uses the same direct-file-load technique as `test_platform_isolation.py`/
`test_service_validation.py`: `conftest.py` stubs `sys.modules["kibble"]` to skip running the
real `__init__.py`, so this loads it under a distinct module name to reach
`async_remove_config_entry_device`, which nothing else needs.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "kibble.__init__",
    pathlib.Path(__file__).parent.parent / "custom_components" / "kibble" / "__init__.py",
    submodule_search_locations=[],
)
_kibble_init = importlib.util.module_from_spec(_spec)
_kibble_init.__package__ = "kibble"
_spec.loader.exec_module(_kibble_init)


def _entry(unique_id: str) -> SimpleNamespace:
    return SimpleNamespace(unique_id=unique_id)


def _device(*identifiers: tuple[str, str]) -> SimpleNamespace:
    return SimpleNamespace(identifiers=set(identifiers))


async def test_the_entrys_own_live_feeder_device_cannot_be_removed() -> None:
    entry = _entry("20251204DJ0534")
    device = _device(("kibble", "20251204DJ0534"))

    allowed = await _kibble_init.async_remove_config_entry_device(object(), entry, device)

    assert allowed is False


async def test_an_orphan_device_with_an_empty_serial_can_be_removed() -> None:
    """The exact empty-serial-orphan shape this hook exists for."""
    entry = _entry("20251204DJ0534")
    device = _device(("kibble", ""))

    allowed = await _kibble_init.async_remove_config_entry_device(object(), entry, device)

    assert allowed is True


async def test_an_orphan_device_from_a_stale_replaced_serial_can_be_removed() -> None:
    entry = _entry("20251204DJ0534")
    device = _device(("kibble", "some-old-replaced-serial"))

    allowed = await _kibble_init.async_remove_config_entry_device(object(), entry, device)

    assert allowed is True


async def test_a_device_with_no_kibble_identifier_at_all_can_be_removed() -> None:
    """Defensive: a device registered under this config entry via some other integration's
    identifier (not expected in practice, but the hook must not assume one is always present)."""
    entry = _entry("20251204DJ0534")
    device = _device(("other_domain", "20251204DJ0534"))

    allowed = await _kibble_init.async_remove_config_entry_device(object(), entry, device)

    assert allowed is True
