"""`sensor.<device>_learning`: the learning job's status as one diagnostic sensor.

The job itself is tested in `test_learning.py`; this is only the entity around it -- what it
shows, that it never waits on the feeder, how rarely it rewrites its state, and that it lets go of
the job when it is removed. Same fake-coordinator pattern as `test_hopper_empty.py`: the entity is
the real class, its coordinator is a `SimpleNamespace` carrying just what it reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory

from kibble.const import CONF_HOST, CONF_PORT
from kibble.learning import STATES
from kibble.sensor import KibbleLearningSensor

# The frozen status payload, minus the state (which is the sensor's own state).
ATTRIBUTES = {"pending", "done", "total", "starts_at", "retry_at", "started", "last", "error"}


def _status(state: str = "idle", **overrides: Any) -> dict[str, Any]:
    status: dict[str, Any] = {
        "state": state,
        "pending": 0,
        "done": None,
        "total": None,
        "starts_at": None,
        "retry_at": None,
        "started": None,
        "last": None,
        "error": None,
    }
    status.update(overrides)
    return status


class FakeLearning:
    """Just `status()` and `subscribe()`, which is all the entity uses of the job."""

    def __init__(self, status: dict[str, Any] | None = None) -> None:
        self.current = status or _status()
        self.listeners: list[Any] = []
        self.unsubscribed = 0

    def status(self) -> dict[str, Any]:
        return self.current

    def subscribe(self, listener):
        self.listeners.append(listener)

        def unsubscribe() -> None:
            self.listeners.remove(listener)
            self.unsubscribed += 1

        return unsubscribe


def _sensor(learning: FakeLearning | None, *, feeder_up: bool = True):
    coordinator = SimpleNamespace(
        data=SimpleNamespace(state=SimpleNamespace(serial="S1", firmware="895")),
        entry=SimpleNamespace(data={CONF_HOST: "feeder.local", CONF_PORT: 80}),
        learning=learning,
        last_update_success=feeder_up,
        async_add_listener=lambda update, context=None: (lambda: None),
    )
    sensor = KibbleLearningSensor(coordinator)
    sensor.writes = 0

    def write() -> None:
        sensor.writes += 1

    sensor.async_write_ha_state = write
    return sensor


def test_the_state_is_the_jobs_state_and_the_attributes_are_the_rest_of_its_status() -> None:
    last = {"finished": 1000, "duration": 9, "answers": 3, "learned": {"Kitty": 2}, "renamed": 1,
            "rechecked": 40, "stages": {"rebuild_ms": 5}}
    job = FakeLearning(_status("rechecking", pending=2, done=10, total=40, started=990, last=last))
    sensor = _sensor(job)

    assert sensor.native_value == "rechecking"
    attributes = sensor.extra_state_attributes
    assert "state" not in attributes
    assert attributes == {"pending": 2, "done": 10, "total": 40, "starts_at": None, "retry_at": None,
                          "started": 990, "last": last, "error": None}


def test_it_follows_the_job_when_the_status_changes() -> None:
    job = FakeLearning(_status("waiting", pending=1))
    sensor = _sensor(job)
    assert sensor.native_value == "waiting"
    job.current = _status("idle")
    assert sensor.native_value == "idle"
    assert sensor.extra_state_attributes["pending"] == 0


def test_it_stays_available_while_the_feeder_is_down() -> None:
    """Learning is the integration's own background work; it never touches the feeder."""
    assert _sensor(FakeLearning(), feeder_up=False).available is True


def test_with_no_job_it_is_unavailable_and_says_nothing() -> None:
    sensor = _sensor(None)
    assert sensor.available is False
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


def test_it_is_a_diagnostic_enum_of_exactly_the_jobs_states_and_is_off_by_default() -> None:
    sensor = _sensor(FakeLearning())
    assert sensor.device_class == SensorDeviceClass.ENUM
    assert sensor.options == list(STATES)
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor.entity_registry_enabled_default is False
    assert sensor.unique_id == "S1_learning"
    assert sensor.translation_key == "learning"


def test_every_attribute_stays_out_of_history() -> None:
    """History keeps only the state; the attributes change too often to be worth a row each."""
    assert KibbleLearningSensor._unrecorded_attributes == frozenset(ATTRIBUTES)
    assert ATTRIBUTES == set(_status()) - {"state"}


async def test_it_subscribes_when_added_and_lets_go_when_removed() -> None:
    job = FakeLearning()
    sensor = _sensor(job)

    await sensor.async_added_to_hass()
    assert job.listeners == [sensor._on_status]

    for undo in list(sensor._on_remove):  # what Home Assistant runs when the entity is removed
        undo()
    assert job.listeners == []
    assert job.unsubscribed == 1


async def test_added_with_no_job_it_has_nothing_to_subscribe_to() -> None:
    sensor = _sensor(None)
    await sensor.async_added_to_hass()  # nothing raises


def test_it_rewrites_its_state_only_when_something_it_shows_changes() -> None:
    sensor = _sensor(FakeLearning())

    sensor._on_status(_status("waiting", pending=1))
    assert sensor.writes == 1  # the first status is always written

    sensor._on_status(_status("waiting", pending=1))
    assert sensor.writes == 1  # nothing changed

    sensor._on_status(_status("waiting", pending=1, starts_at=123))
    assert sensor.writes == 1  # a countdown or progress figure is not a reason to rewrite

    sensor._on_status(_status("rechecking", pending=1, done=1, total=9))
    assert sensor.writes == 2  # a new state

    sensor._on_status(_status("rechecking", pending=1, done=2, total=9))
    sensor._on_status(_status("rechecking", pending=1, done=3, total=9))
    assert sensor.writes == 2  # progress ticks

    sensor._on_status(_status("rechecking", pending=4, done=3, total=9))
    assert sensor.writes == 3  # more answers are waiting

    sensor._on_status(_status("retrying", pending=4, error="CoralHub is not answering"))
    assert sensor.writes == 4  # a new state (and an error)

    sensor._on_status(_status("retrying", pending=4, error="the store is busy"))
    assert sensor.writes == 5  # a different error

    sensor._on_status(_status("idle", last={"finished": 2000}))
    assert sensor.writes == 6  # a pass finished

    sensor._on_status(_status("idle", last={"finished": 2000}))
    assert sensor.writes == 6

    sensor._on_status(_status("idle", last={"finished": 2100}))
    assert sensor.writes == 7  # the next pass finished
