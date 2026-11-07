"""Every Kibble entity that does work in `async_added_to_hass` / `async_will_remove_from_hass`
must also implement `async_entity_id_changed`.

Home Assistant 2026.11 changes an entity's id in place instead of removing and re-adding the
entity. An entity class defined outside Home Assistant Core that has its own add/remove methods
is only changed in place once it (or a subclass of it) also implements `async_entity_id_changed`;
otherwise Home Assistant keeps the old remove-and-re-add path, and stops supporting it in 2027.11.
`helpers/entity.py` (`_entity_class_requires_readd`) holds the rule; it is written out here
instead of imported because it is private and absent from older Home Assistant, and this check
has to fail on every version the moment a new entity class forgets the hook. The behavioural
proof for the existing classes is `test_renaming_an_entity_id_is_done_in_place...` in
test_setup.py.
"""

from __future__ import annotations

import importlib

import pytest
from homeassistant.helpers.entity import Entity

from custom_components.kibble import PLATFORMS

ADD_REMOVE_METHODS = (
    "async_prepare_to_add_to_hass",
    "async_added_to_hass",
    "async_will_remove_from_hass",
)


def _kibble_entity_classes() -> list[type[Entity]]:
    """Every Entity subclass the integration defines, found through the platforms it loads."""
    importlib.import_module("custom_components.kibble.entity")
    for platform in PLATFORMS:
        importlib.import_module(f"custom_components.kibble.{platform}")
    found: dict[type[Entity], None] = {}
    todo: list[type[Entity]] = [Entity]
    while todo:
        for subclass in todo.pop().__subclasses__():
            if subclass not in found:
                found[subclass] = None
                todo.append(subclass)
    return [cls for cls in found if cls.__module__.startswith("custom_components.kibble.")]


def _add_remove_methods_without_the_hook(entity_class: type[Entity]) -> list[str]:
    """Classes in the entity's MRO whose add/remove methods the hook does not cover."""
    mro = [cls for cls in entity_class.__mro__ if not cls.__module__.startswith("homeassistant.")]
    hook_owners = [cls for cls in mro if "async_entity_id_changed" in cls.__dict__]
    return [
        cls.__qualname__
        for cls in mro
        if any(method in cls.__dict__ for method in ADD_REMOVE_METHODS)
        and not any(issubclass(owner, cls) for owner in hook_owners)
    ]


KIBBLE_ENTITY_CLASSES = _kibble_entity_classes()


def test_the_entity_classes_with_add_remove_methods_are_all_found() -> None:
    """The check below is only worth anything if it really sees the classes that matter."""
    with_methods = {
        cls.__name__
        for cls in KIBBLE_ENTITY_CLASSES
        if any(method in cls.__dict__ for method in ADD_REMOVE_METHODS)
    }
    assert {
        "KibbleFeedAmount",
        "KibbleHopperDividerSwitch",
        "KibbleAutoLearnSwitch",
        "KibbleHopperFoodText",
        "KibbleSpeaker",
    } <= with_methods


@pytest.mark.parametrize("entity_class", KIBBLE_ENTITY_CLASSES, ids=lambda cls: cls.__qualname__)
def test_entity_class_changes_its_entity_id_in_place(entity_class: type[Entity]) -> None:
    assert _add_remove_methods_without_the_hook(entity_class) == []
