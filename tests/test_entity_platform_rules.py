"""Regression test for platform-rule validity across every Kibble entity description.

Direct regression test for the `EntityCategory.CONFIG`-on-`sensor`/`binary_sensor` defect that
shipped and made 7 entities fail to add on every startup (fixed in `b40d104`, before this repo's
current `main`): HA's own `SensorEntity`/`BinarySensorEntity.async_internal_added_to_hass`
raises `HomeAssistantError` for exactly that combination --
`homeassistant/components/sensor/__init__.py` and `.../binary_sensor/__init__.py` in the
installed `homeassistant==2026.9.2`, both:

    if self.entity_category == EntityCategory.CONFIG:
        raise HomeAssistantError(...)

This file collects every entity Kibble registers -- both the data-driven `EntityDescription`
tuples (`SENSORS`, `SWITCHES`, ...) and the one-off hardcoded entity classes -- as a flat list
of facts, then checks each against the platform rules HA actually enforces (the above, plus the
`SensorDeviceClass.ENUM` <-> `options` pairing HA's own `SensorEntity.native_value` requires) and
against this project's own house rules (rule 4: every `CONFIG`/`DIAGNOSTIC` entity is disabled
by default, with documented exceptions only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import DEVICE_CLASS_UNITS, SensorDeviceClass, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import EntityDescription

from kibble import binary_sensor, button, camera, image, media_player, number, select, sensor, switch


@dataclass(frozen=True)
class EntityFact:
    """Everything the checks below need about one entity, normalised whether it came from an
    `EntityDescription` tuple or a hardcoded entity class's `_attr_*` values."""

    label: str
    domain: str
    entity_category: EntityCategory | None
    device_class: Any
    state_class: Any
    native_unit: Any
    options: list[str] | None
    entity_registry_enabled_default: bool
    translation_key: str | None


def _fact_from_description(domain: str, desc: EntityDescription) -> EntityFact:
    return EntityFact(
        label=f"{domain}.{desc.key}",
        domain=domain,
        entity_category=desc.entity_category,
        device_class=getattr(desc, "device_class", None),
        state_class=getattr(desc, "state_class", None),
        native_unit=getattr(desc, "native_unit_of_measurement", None),
        options=getattr(desc, "options", None),
        entity_registry_enabled_default=desc.entity_registry_enabled_default,
        translation_key=desc.translation_key,
    )


def _fact_from_class(domain: str, cls: type) -> EntityFact:
    """Reads a hardcoded entity class's `_attr_*` values -- the same values its `translation_
    key`/`device_class`/`entity_category`/... properties fall back to on a real instance
    (`homeassistant.helpers.entity.Entity`). HA's own `cached_property`-with-attr machinery
    rewrites a class-body `_attr_foo = ...` into a `property` (for cache invalidation) backed
    by the real value under `__attr_foo` -- `getattr(cls, "_attr_foo")` on the *class* (not an
    instance) returns that `property` object itself, not the value, so this reads `__attr_foo`
    directly instead."""

    def attr(name: str, default: object = None) -> object:
        return getattr(cls, f"__attr_{name}", default)

    translation_key = attr("translation_key")
    return EntityFact(
        label=f"{domain}.{translation_key or cls.__name__} ({cls.__name__})",
        domain=domain,
        entity_category=attr("entity_category"),
        device_class=attr("device_class"),
        state_class=attr("state_class"),
        native_unit=attr("native_unit_of_measurement"),
        options=attr("options"),
        entity_registry_enabled_default=attr("entity_registry_enabled_default", True),
        translation_key=translation_key,
    )


def _all_facts() -> list[EntityFact]:
    facts: list[EntityFact] = []
    for desc in sensor.SENSORS:
        facts.append(_fact_from_description("sensor", desc))
    for desc in sensor.SETTING_SENSORS:
        facts.append(_fact_from_description("sensor", desc))
    for desc in binary_sensor.SETTING_SENSORS:
        facts.append(_fact_from_description("binary_sensor", desc))
    for desc in binary_sensor.HOPPER_EMPTY_SENSORS:
        facts.append(_fact_from_description("binary_sensor", desc))
    for desc in switch.SWITCHES:
        facts.append(_fact_from_description("switch", desc))
    for desc in number.AMOUNTS:
        facts.append(_fact_from_description("number", desc))
    for desc in button.FEEDS:
        facts.append(_fact_from_description("button", desc))
    for desc in image.DISH_IMAGES:
        facts.append(_fact_from_description("image", desc))

    # One-off hardcoded entities: no shared EntityDescription tuple, but each has its own
    # `_attr_*` values to check the exact same way.
    facts.append(_fact_from_class("sensor", sensor.KibbleCloudConnectionSensor))
    facts.append(_fact_from_class("sensor", sensor.KibbleControlPathSensor))
    facts.append(_fact_from_class("sensor", sensor.KibbleWifiNetworkSensor))
    facts.append(_fact_from_class("sensor", sensor.KibbleWifiSignalSensor))
    facts.append(_fact_from_class("sensor", sensor.KibbleLastSeenPetSensor))
    facts.append(_fact_from_class("sensor", sensor.KibbleIdentificationScoreSensor))
    facts.append(_fact_from_class("sensor", sensor.KibblePendingFacesSensor))
    facts.append(_fact_from_class("sensor", sensor.KibbleClipsSensor))
    facts.append(_fact_from_class("sensor", sensor.KibbleScheduleSensor))
    facts.append(_fact_from_class("sensor", sensor.KibbleScheduleCardStateSensor))
    facts.append(_fact_from_class("binary_sensor", binary_sensor.KibbleFeedingSensor))
    facts.append(_fact_from_class("binary_sensor", binary_sensor.KibbleReachableBinarySensor))
    facts.append(_fact_from_class("binary_sensor", binary_sensor.KibbleCatPresentBinarySensor))
    facts.append(_fact_from_class("switch", switch.KibbleCloudSwitch))
    facts.append(_fact_from_class("button", button.KibbleCancelButton))
    facts.append(_fact_from_class("select", select.KibbleWifiSelect))
    facts.append(_fact_from_class("select", select.KibbleLabelFaceSelect))
    facts.append(_fact_from_class("media_player", media_player.KibbleSpeaker))
    facts.append(_fact_from_class("camera", camera.KibbleCamera))
    facts.append(_fact_from_class("image", image.KibblePendingFaceImage))
    return facts


ALL_FACTS = _all_facts()

# Read-only platforms HA itself refuses to add a `CONFIG` entity to (a *setting*, i.e. something
# the user can change) -- see this file's module docstring. Every other platform here is a
# control surface, so `CONFIG` is legitimate on it.
_CONFIG_FORBIDDEN_DOMAINS = {"sensor", "binary_sensor"}

# `label -> reason`: documented, deliberate exceptions to house rule 4 ("config/diagnostic
# entities are disabled by default"). Each entry here must correspond to a comment in the
# source justifying it, not just an omission.
_ENABLED_BY_DEFAULT_EXCEPTIONS = {
    "switch.cloud (KibbleCloudSwitch)": (
        "the privacy control the integration exists for; ships visible by design, "
        "documented in switch.py's KibbleCloudSwitch docstring"
    ),
}


def _assert_platform_rules(fact: EntityFact) -> None:
    """The subset of HA's own platform rules this repo can check statically."""
    if fact.entity_category is EntityCategory.CONFIG:
        assert fact.domain not in _CONFIG_FORBIDDEN_DOMAINS, (
            f"{fact.label}: EntityCategory.CONFIG is not allowed on a {fact.domain} entity -- "
            f"HA's {fact.domain}.async_internal_added_to_hass raises HomeAssistantError for "
            "this at add-to-hass time (this is exactly the defect fixed in b40d104)"
        )

    has_enum_class = fact.device_class in (SensorDeviceClass.ENUM, "enum")
    if fact.options is not None:
        assert has_enum_class, (
            f"{fact.label}: sets `options` but device_class is {fact.device_class!r}, not "
            "SensorDeviceClass.ENUM -- HA's SensorEntity.native_value raises ValueError for this"
        )
    if has_enum_class:
        assert fact.options is not None, (
            f"{fact.label}: device_class is SensorDeviceClass.ENUM but sets no `options` -- "
            "HA's SensorEntity.native_value raises ValueError for this"
        )
        assert fact.state_class is None, (
            f"{fact.label}: SensorDeviceClass.ENUM entities must not set a state_class "
            "(DEVICE_CLASS_STATE_CLASSES[ENUM] is empty in homeassistant.components.sensor.const)"
        )

    if fact.domain == "sensor" and fact.device_class is not None and fact.native_unit is not None:
        allowed = DEVICE_CLASS_UNITS.get(fact.device_class)
        if allowed is not None:
            assert fact.native_unit in allowed, (
                f"{fact.label}: native unit {fact.native_unit!r} is not valid for device_class "
                f"{fact.device_class!r}; HA accepts one of {sorted(str(u) for u in allowed)} -- "
                "this is exactly the class of mistake a removed/renamed unit constant causes"
            )


def _assert_house_rule_4(fact: EntityFact) -> None:
    if fact.entity_category not in (EntityCategory.CONFIG, EntityCategory.DIAGNOSTIC):
        return
    if fact.entity_registry_enabled_default:
        assert fact.label in _ENABLED_BY_DEFAULT_EXCEPTIONS, (
            f"{fact.label}: entity_category={fact.entity_category} but "
            "entity_registry_enabled_default is not False -- house rule 4 requires every "
            "config/diagnostic entity disabled by default unless explicitly exempted (and "
            "documented) in this test's _ENABLED_BY_DEFAULT_EXCEPTIONS"
        )


@pytest.mark.parametrize("fact", ALL_FACTS, ids=lambda f: f.label)
def test_entity_satisfies_platform_rules(fact: EntityFact) -> None:
    _assert_platform_rules(fact)


@pytest.mark.parametrize("fact", ALL_FACTS, ids=lambda f: f.label)
def test_entity_satisfies_house_rule_4(fact: EntityFact) -> None:
    _assert_house_rule_4(fact)


def test_every_config_or_diagnostic_sensor_or_binary_sensor_entity_is_actually_covered() -> None:
    """Sanity check on the test itself: if this ever collects zero sensor/binary_sensor
    descriptions with entity_category set, the parametrized tests above would trivially pass
    without checking anything real."""
    covered = [
        f for f in ALL_FACTS if f.domain in _CONFIG_FORBIDDEN_DOMAINS and f.entity_category is not None
    ]
    assert len(covered) >= 10


def test_validator_rejects_the_exact_shape_of_the_shipped_defect() -> None:
    """Proves the check above has teeth: `EntityCategory.CONFIG` on a `sensor`/`binary_sensor`
    description is exactly today's-pre-fix-code shape (see this file's module docstring). If
    this stops raising, `_assert_platform_rules` has stopped meaningfully checking anything."""
    bad = _fact_from_description(
        "sensor", SensorEntityDescription(key="bad", entity_category=EntityCategory.CONFIG)
    )
    with pytest.raises(AssertionError):
        _assert_platform_rules(bad)


def test_validator_rejects_enabled_by_default_diagnostic_with_no_exception_on_record() -> None:
    """Proves `_assert_house_rule_4` has teeth too."""
    bad = EntityFact(
        label="sensor.unlisted_diagnostic",
        domain="sensor",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=None,
        native_unit=None,
        options=None,
        entity_registry_enabled_default=True,
        translation_key="unlisted_diagnostic",
    )
    with pytest.raises(AssertionError):
        _assert_house_rule_4(bad)
