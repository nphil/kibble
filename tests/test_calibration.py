"""Bowl-fill calibration: `api.py`'s 409/404 handling, and `sensor.py`'s per-hopper state.

Same duck-typed styles already established elsewhere in this suite: `test_media.py`'s
`_FakeResponse`/`_FakeSession` for the 409 (1), `test_desiccant.py`'s `object.__new__`
`KibbleCoordinator` for the 404-leaves-`None`-without-failing-the-poll case (2), and
`test_bowl_fill_source.py`'s bare pure-function/`object.__new__` entity checks for the
per-hopper state/attribute classification (3).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from kibble.api import (
    FeederState,
    KibbleCalibrationBusyError,
    KibbleClient,
    KibbleNotFoundError,
    StackState,
)
from kibble.coordinator import KibbleCoordinator
from kibble.sensor import CALIBRATION_SENSORS, KibbleCalibrationSensor, _calibration_attributes, _calibration_state

HOST = "192.168.4.85"
PORT = 8765


# --- (1) POST /calibration 409 raises the typed busy error, not the unrelated speaker one ------


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


class _FakeSession:
    """Duck-types the subset of `aiohttp.ClientSession` `api.py`'s `_request` actually calls."""

    def __init__(self, status: int, body: Any) -> None:
        self._status = status
        self._body = body

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._status, self._body)


async def test_calibration_action_409_raises_calibration_busy_error_not_a_generic_one() -> None:
    """`_request`'s 409 branch defaults to `KibbleSpeakerBusyError` for every other write in
    this client -- `calibration_action` must override that with its own `busy_error`, or an
    animal at the bowl would surface to the wizard as a nonsensical "speaker busy" failure."""
    session = _FakeSession(409, {"error": "an animal is over the bowl -- wait for a clear view"})
    client = KibbleClient(session, HOST, PORT)

    with pytest.raises(KibbleCalibrationBusyError, match="animal is over the bowl"):
        await client.calibration_action("point", 0, portions=1)


# --- (2) GET /calibration 404ing (old daemon) leaves data.calibration None, poll still succeeds -


def _coordinator_for_fetch(client: AsyncMock) -> KibbleCoordinator:
    """A real (uninitialized) `KibbleCoordinator` with only what `_fetch_all` reads set by
    hand -- same `object.__new__` approach as `test_desiccant.py`/`test_optional_routes.py`."""
    coord = object.__new__(KibbleCoordinator)
    coord.client = client
    coord.entry = SimpleNamespace(options={})
    return coord


async def test_get_calibration_404_leaves_calibration_none_while_the_rest_of_the_poll_still_updates() -> None:
    state = FeederState.from_json({})
    cloud = object()
    client = AsyncMock(
        state=AsyncMock(return_value=state),
        schedule=AsyncMock(return_value=object()),
        config=AsyncMock(return_value={}),
        cloud=AsyncMock(return_value=cloud),
        mode=AsyncMock(return_value=StackState.from_json({"running": "librefeed"})),
        led=AsyncMock(return_value=object()),
        desiccant=AsyncMock(return_value=object()),
        calibration=AsyncMock(side_effect=KibbleNotFoundError("not found")),
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

    # A LibreFeed build old enough to predate this route goes to None instead of raising out
    # of the whole poll cycle -- everything else fetched in the same batch is still fresh.
    assert data.calibration is None
    assert data.cloud is cloud
    assert data.state is state


# --- (3) sensor.py: measured vs inherited vs uncalibrated, from the daemon's two `source` shapes


def test_calibration_state_is_measured_for_the_plain_source_string() -> None:
    assert _calibration_state({"source": "measured"}) == "measured"


def test_calibration_state_is_inherited_for_the_inherited_from_object_shape() -> None:
    """The shape a naive `source == "measured"` check gets wrong: `{"inherited_from": N}` is
    not the string "measured", but the hopper is very much calibrated -- just copied, not run
    on this hopper. Treating "not measured" as "uncalibrated" would tell an operator a
    perfectly good inherited curve doesn't exist."""
    assert _calibration_state({"source": {"inherited_from": 0}}) == "inherited"


def test_calibration_state_is_uncalibrated_when_the_hopper_entry_is_null() -> None:
    assert _calibration_state(None) == "uncalibrated"


def test_calibration_attributes_reports_full_portions_score_points_count_measured_at_and_note() -> None:
    hopper = {
        "points": [{"portions": 0, "score": 0.03}, {"portions": 1, "score": 0.18}],
        "full_portions": 4,
        "full_score": 0.45,
        "measured_at": 1789855626,
        "source": "measured",
        "note": "dry kibble",
    }
    attrs = _calibration_attributes(hopper)
    assert attrs["full_portions"] == 4
    assert attrs["full_score"] == 0.45
    # A count, not the raw list -- the list itself is calibration-curve detail for the wizard,
    # not a sensor attribute.
    assert attrs["points"] == 2
    assert attrs["measured_at"] == "2026-09-19T22:07:06+00:00"
    assert attrs["note"] == "dry kibble"


def test_calibration_attributes_is_empty_when_the_hopper_entry_is_null() -> None:
    assert _calibration_attributes(None) == {}


def test_calibration_sensor_reads_its_own_hopper_index_not_the_others() -> None:
    """End-to-end through the real entity, not just the pure classifier -- proves hopper 1's
    sensor reads `hoppers[0]` and hopper 2's reads `hoppers[1]`, not each other's slot."""
    calibration = {
        "hoppers": [
            {"source": "measured", "points": []},
            {"source": {"inherited_from": 0}, "points": []},
        ]
    }
    data = SimpleNamespace(calibration=calibration)
    hopper_1 = object.__new__(KibbleCalibrationSensor)
    hopper_1.entity_description = CALIBRATION_SENSORS[0]
    hopper_1.coordinator = SimpleNamespace(last_update_success=True, data=data)
    hopper_2 = object.__new__(KibbleCalibrationSensor)
    hopper_2.entity_description = CALIBRATION_SENSORS[1]
    hopper_2.coordinator = SimpleNamespace(last_update_success=True, data=data)

    assert hopper_1.native_value == "measured"
    assert hopper_2.native_value == "inherited"
    assert hopper_1.available is True


def test_calibration_sensor_is_unavailable_not_merely_uncalibrated_when_the_route_is_missing() -> None:
    """`None` at the top level (the whole `GET /calibration` 404ing -- vendor stack, or an old
    LibreFeed build) is "we don't know", a different fact from "we asked and this hopper has
    never been calibrated" -- so this must go `unavailable`, not report a false `uncalibrated`
    reading as if the daemon had actually answered."""
    ent = object.__new__(KibbleCalibrationSensor)
    ent.entity_description = CALIBRATION_SENSORS[0]
    ent.coordinator = SimpleNamespace(last_update_success=True, data=SimpleNamespace(calibration=None))

    assert ent.available is False
    assert ent.native_value == "uncalibrated"
