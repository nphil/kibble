"""Direct regression test for `sensor.py`'s `KibbleSettingSensor`/`binary_sensor.py`'s
`KibbleSettingBinarySensor`: every entry in `SETTING_SENSORS` names a device setting read
straight out of `GET /config`. LibreFeed serves only `light`/`night`/`microphone` there today
(`api.py`'s `config()`), so every other setting's key is simply absent from
`coordinator.data.config` on that stack -- not present-but-unset. Before this fix these
entities read a bare `unknown` (`.get()` returning `None`) exactly as they would for a real,
device-confirmed "no value"; they must instead go `unavailable`, mirroring `light.py`'s
`KibbleStatusLight.available`/`select.py`'s `KibbleStackSelect.available`.

Same duck-typed, `object.__new__`-constructed style as `test_status_light.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.components.binary_sensor import BinarySensorEntityDescription
from homeassistant.components.sensor import SensorEntityDescription
from kibble.binary_sensor import KibbleSettingBinarySensor
from kibble.sensor import KibbleSettingSensor


def _fake_setting_sensor(cls, key: str, *, config: dict, last_update_success: bool = True):
    ent = object.__new__(cls)
    ent.entity_description = (
        SensorEntityDescription(key=key) if cls is KibbleSettingSensor else BinarySensorEntityDescription(key=key)
    )
    ent.coordinator = SimpleNamespace(
        data=SimpleNamespace(config=config), last_update_success=last_update_success
    )
    return ent


def test_setting_sensor_is_unavailable_when_its_key_is_absent_from_config() -> None:
    ent = _fake_setting_sensor(KibbleSettingSensor, "factor1", config={"light": 1})
    assert ent.available is False
    assert ent.native_value is None


def test_setting_sensor_is_available_when_its_key_is_present_in_config() -> None:
    ent = _fake_setting_sensor(KibbleSettingSensor, "factor1", config={"factor1": 3})
    assert ent.available is True
    assert ent.native_value == 3


def test_setting_sensor_unavailable_even_with_a_falsy_value_present() -> None:
    """`0` is a real, present value -- distinct from the key being missing altogether. Only
    presence gates availability, not truthiness."""
    ent = _fake_setting_sensor(KibbleSettingSensor, "factor1", config={"factor1": 0})
    assert ent.available is True
    assert ent.native_value == 0


def test_setting_sensor_unreachable_feeder_overrides_a_present_key() -> None:
    ent = _fake_setting_sensor(
        KibbleSettingSensor, "factor1", config={"factor1": 3}, last_update_success=False
    )
    assert ent.available is False


def test_setting_binary_sensor_is_unavailable_when_its_key_is_absent_from_config() -> None:
    ent = _fake_setting_sensor(KibbleSettingBinarySensor, "smart_frame", config={"microphone": 1})
    assert ent.available is False
    assert ent.is_on is None


def test_setting_binary_sensor_is_available_when_its_key_is_present_in_config() -> None:
    ent = _fake_setting_sensor(KibbleSettingBinarySensor, "smart_frame", config={"smart_frame": 0})
    assert ent.available is True
    assert ent.is_on is False
