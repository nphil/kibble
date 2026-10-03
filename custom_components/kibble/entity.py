"""Shared entity base for Kibble."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_HOST, CONF_PORT, DOMAIN, MANUFACTURER, MODEL
from .coordinator import KibbleConfigEntry, KibbleCoordinator


@callback
def async_when_data_ready(entry: KibbleConfigEntry, setup: Callable[[], None]) -> None:
    """Runs `setup` -- one platform's entity creation -- now if the feeder's first poll has
    already landed, otherwise the moment it does.

    Every entity is built from `coordinator.data` (serial, firmware, which stack is running, see
    `KibbleEntity` and `stacks.applies_to`), so none can exist before that first poll; and
    `__init__.py`'s `async_setup_entry` no longer waits for that poll beyond its setup budget
    (coordinator.py's module docstring, "The first poll runs in the background"). Every platform
    is therefore still forwarded during setup, as Home Assistant requires, and its
    `async_setup_entry` hands the real work to this: a feeder that answered in time gets its
    entities before setup returns, exactly as before, and a slow one gets them when it answers.

    An exception from `setup` propagates when it runs at once (Home Assistant logs it against
    the platform) and is logged by the coordinator when it runs later -- either way the other
    platforms are untouched."""
    coordinator = entry.runtime_data
    if coordinator.data is not None:
        setup()
    else:
        coordinator.async_on_first_data(setup)


class KibbleEntity(CoordinatorEntity[KibbleCoordinator]):
    """An entity belonging to one feeder.

    `unique_id` is always `f"{serial}_{key}"`: the feeder's own serial (stable across reboots
    and address changes -- see `config_flow.py`'s use of the same value for the config entry's
    unique id) plus a key that is stable per platform module (an `EntityDescription.key` for
    the data-driven platforms, a literal string for one-off entities) -- never the entity's
    display name or its position in a list, so reordering or renaming never changes it.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: KibbleCoordinator, key: str) -> None:
        super().__init__(coordinator)
        state = coordinator.data.state
        host = coordinator.entry.data[CONF_HOST]
        port = coordinator.entry.data[CONF_PORT]
        self._attr_unique_id = f"{state.serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, state.serial)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name="Cat Feeder",
            serial_number=state.serial,
            sw_version=state.firmware,
            # The agent has no web UI of its own, but this is still the right target: it is
            # the one address that identifies *this* feeder, and clicking through is a
            # reasonable way for a user to sanity-check "is this thing even on the network"
            # (a plain GET against it 200s) independent of Home Assistant.
            configuration_url=f"http://{host}:{port}",
        )

    @property
    def available(self) -> bool:
        """`CoordinatorEntity.available` (`coordinator.last_update_success`) -- which itself
        only goes `False` once `coordinator.py`'s consecutive-failure tolerance is exhausted,
        not on any single failed poll. See that module's docstring for the full policy; this
        override exists only to make the contract explicit and give every entity one place to
        add device-specific nuance later, rather than relying silently on the base class."""
        return super().available
