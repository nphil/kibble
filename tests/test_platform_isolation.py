"""Direct regression test for `__init__.py`'s per-platform setup isolation -- the fix for
defect #2: a single bad import (`UnitOfSignalStrength`, removed in 2026.9) took down the whole
config entry, all 51 entities. `_async_forward_platforms_isolated` forwards each platform on its
own `async_forward_entry_setups` call so one platform's exception cannot fail the others -- see
its docstring in `__init__.py` for exactly why HA's own batched call does not already do this.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

from homeassistant.const import Platform

# `conftest.py` deliberately stubs `sys.modules["kibble"]` to a namespace package pointing at
# `custom_components/kibble` *without* running the real `__init__.py`, so every other test file
# never needs a full `HomeAssistant` core instance. This test is the one exception -- it needs
# `__init__.py`'s own `_async_forward_platforms_isolated` -- so it loads that file directly
# under a distinct module name instead of fighting the shared stub.
_spec = importlib.util.spec_from_file_location(
    "kibble.__init__",
    pathlib.Path(__file__).parent.parent / "custom_components" / "kibble" / "__init__.py",
    submodule_search_locations=[],
)
_kibble_init = importlib.util.module_from_spec(_spec)
_kibble_init.__package__ = "kibble"
_spec.loader.exec_module(_kibble_init)
_async_forward_platforms_isolated = _kibble_init._async_forward_platforms_isolated


def _fake_hass(*, fails: set[Platform]) -> SimpleNamespace:
    async def forward(entry, platforms):
        assert len(platforms) == 1  # one platform per call -- the whole point
        if platforms[0] in fails:
            raise ImportError(f"cannot import name 'Fake' from '{platforms[0]}'")

    return SimpleNamespace(config_entries=SimpleNamespace(async_forward_entry_setups=forward))


async def test_one_bad_platform_does_not_prevent_the_others_from_loading() -> None:
    """Exactly defect #2's shape: `image` (standing in for the bad-import platform) fails;
    `camera`/`button`/`sensor` must still load."""
    platforms = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CAMERA, Platform.IMAGE]
    hass = _fake_hass(fails={Platform.IMAGE})

    loaded = await _async_forward_platforms_isolated(hass, entry=object(), platforms=platforms)

    assert Platform.IMAGE not in loaded
    assert set(loaded) == {Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CAMERA}


async def test_every_platform_failing_yields_an_empty_loaded_list() -> None:
    platforms = [Platform.SENSOR, Platform.BUTTON]
    hass = _fake_hass(fails=set(platforms))

    loaded = await _async_forward_platforms_isolated(hass, entry=object(), platforms=platforms)

    assert loaded == []


async def test_no_failures_loads_every_platform() -> None:
    platforms = [Platform.SENSOR, Platform.BUTTON, Platform.CAMERA]
    hass = _fake_hass(fails=set())

    loaded = await _async_forward_platforms_isolated(hass, entry=object(), platforms=platforms)

    assert loaded == platforms


async def test_a_later_platform_still_loads_after_an_earlier_one_fails() -> None:
    """Isolation must not stop the loop early -- everything after the failure still gets a
    chance."""
    platforms = [Platform.SENSOR, Platform.BUTTON, Platform.CAMERA]
    hass = _fake_hass(fails={Platform.SENSOR})

    loaded = await _async_forward_platforms_isolated(hass, entry=object(), platforms=platforms)

    assert loaded == [Platform.BUTTON, Platform.CAMERA]
