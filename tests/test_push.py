"""Local push (`push.py` + the coordinator's listen loop; docs/33-local-push-design.md).

Pinned here, each on the observable contract rather than the plumbing:

- A frame's fields go through the *same* parsers as the poll (`parse_fields`), whole-field,
  with `null` meaning "keep what you had" and unknown names ignored.
- The coordinator flips between push mode (no scheduled polls) and polling mode on connect /
  disconnect, re-polls immediately on a drop, and treats every frame as a successful contact.
- An agent that offers no push channel leaves the coordinator polling, once, without error.
- The single-connection guard: starting push twice starts one task.

Same test style as `test_coordinator_availability.py`: a real but uninitialised
`KibbleCoordinator` with only the attributes the methods under test read.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from kibble import coordinator as coordinator_module
from kibble.api import DetectionEvent, FeederState
from kibble.coordinator import DEFAULT_SCAN_INTERVAL, KibbleCoordinator, KibbleData
from kibble.push import (
    SNAPSHOT_FIELDS,
    Frame,
    KibblePushClosed,
    KibblePushUnsupported,
    decode_frame,
    merge_frame,
    parse_fields,
)

STATE_JSON = {
    "serial": "SN1", "firmware": "895", "ble_firmware": 159, "volume": 20, "desiccant_days": 3,
    "feeding": False, "bowl_fill": [10, None], "event_counter": 4, "timezone_name": "UTC",
    "scheduler_tz_supported": True, "track": None,
    "kibbled_start_count": 1, "kibbled_last_start_unix": 1, "kibbled_last_exit_code": None,
}


# --- push.py: pure frame handling --------------------------------------------------------------


def test_every_snapshot_field_has_a_parser_and_maps_onto_kibble_data() -> None:
    """The agent's `push::Field::ALL` and `KibbleData` must agree; a field with no parser
    would be silently dropped, which is exactly the class of bug this guards against."""
    data_fields = set(KibbleData.__dataclass_fields__)
    for name in SNAPSHOT_FIELDS:
        attr = "pending_face_count" if name == "pending_faces" else name
        assert attr in data_fields, name


def test_decode_frame_reads_type_seq_fields_and_hello_proto() -> None:
    hello = decode_frame('{"type":"hello","proto":1,"seq":5}')
    assert hello == Frame(type="hello", seq=5, fields={}, proto=1)
    upd = decode_frame('{"type":"update","seq":6,"fields":{"config":{"volume":3}}}')
    assert upd.type == "update" and upd.fields == {"config": {"volume": 3}} and upd.proto is None
    assert decode_frame("not json") is None
    assert decode_frame('{"nope":1}') is None


def test_parse_fields_uses_the_get_parsers_and_skips_null_and_unknown() -> None:
    parsed = parse_fields(
        {
            "state": STATE_JSON,
            "config": {"volume": "7"},
            "pending_faces": ["a.jpg", "b.jpg"],
            "events": [{"seq": 1, "ts": 10, "class": "track", "pet_id": 5, "track_value": 2.5}],
            "cats": None,  # agent could not serialise it right now -> keep the previous value
            "future_field": {"x": 1},  # a newer agent -> ignored
        }
    )
    assert isinstance(parsed["state"], FeederState) and parsed["state"].feeding is False
    assert parsed["config"] == {"volume": 7}
    assert parsed["pending_face_count"] == 2
    assert isinstance(parsed["events"][0], DetectionEvent) and parsed["events"][0].pet_id == "5"
    assert "cats" not in parsed and "future_field" not in parsed


def _data(**overrides) -> KibbleData:
    base = dict(
        state=FeederState.from_json(STATE_JSON), schedule=object(), config={"volume": 1},
        cloud=object(), wifi=object(), wifi_scan=(), cats=(), identify=object(),
        review_face=object(), pending_face_count=0, clips=(), feeds=(), events=(),
        vendor_sightings=(),
    )
    base.update(overrides)
    return KibbleData(**base)


def test_merge_frame_replaces_only_the_carried_fields() -> None:
    before = _data(config={"volume": 1}, pending_face_count=9)
    after = merge_frame(before, Frame(type="update", seq=1, fields={"config": {"volume": 4}}))
    assert after.config == {"volume": 4}
    assert after.pending_face_count == 9 and after.cats is before.cats
    assert merge_frame(before, Frame(type="update", seq=2, fields={})) is before


# --- coordinator: push mode <-> polling mode --------------------------------------------------


class _FakePush:
    """Stands in for `KibblePush`: yields scripted frames, then ends the way the test says."""

    def __init__(self, frames, end: BaseException | None = None) -> None:
        self._frames = frames
        self._end = end
        self.resyncs = 0
        self.closed = False

    async def listen(self):
        for f in self._frames:
            yield f
        if self._end is not None:
            raise self._end

    async def resync(self) -> None:
        self.resyncs += 1

    async def close(self) -> None:
        self.closed = True


def _bare_coordinator(monkeypatch: pytest.MonkeyPatch, data=None) -> KibbleCoordinator:
    coord = object.__new__(KibbleCoordinator)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # a sync test: nothing here schedules anything
        loop = None
    coord.hass = SimpleNamespace(loop=loop, async_create_task=lambda c: loop.create_task(c))
    coord.entry = SimpleNamespace(entry_id="e", title="Cat Feeder", data={"host": "h"}, options={})
    coord.client = AsyncMock()
    coord.data = data
    coord.consecutive_failures = 0
    coord.last_error = None
    coord.update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL)
    coord._push = None
    coord._push_task = None
    coord.push_connected = False
    coord.push_reconnects = 0
    coord.push_last_frame = None
    coord.push_unsupported = False
    # The real `async_set_updated_data` stores `data` then notifies listeners; the store is the
    # part the merge depends on (the next frame builds on it), so the stand-in keeps it.
    published: list = []

    def set_updated(data) -> None:
        coord.data = data
        published.append(data)

    coord.async_set_updated_data = Mock(side_effect=set_updated)
    coord.published = published
    coord.async_request_refresh = AsyncMock()
    monkeypatch.setattr(coordinator_module.ir, "async_delete_issue", Mock())
    monkeypatch.setattr(coordinator_module, "async_get_clientsession", lambda hass: object())
    return coord


async def test_consume_applies_frames_and_leaves_push_mode_on_close(monkeypatch) -> None:
    coord = _bare_coordinator(monkeypatch, data=_data())
    coord.consecutive_failures = 2
    frames = [
        Frame(type="hello", seq=0, fields={}, proto=1),
        Frame(type="snapshot", seq=1, fields={"config": {"volume": 9}}),
        Frame(type="update", seq=2, fields={"pending_faces": ["x.jpg"]}),
    ]
    fake = _FakePush(frames, end=KibblePushClosed("agent restarted"))

    with pytest.raises(KibblePushClosed):
        await coord._consume(fake)

    assert coord.push_connected is True and coord.update_interval is None
    assert coord.consecutive_failures == 0
    assert coord.published[0].config == {"volume": 9}
    assert coord.published[1].pending_face_count == 1 and coord.published[1].config == {"volume": 9}


async def test_push_loop_falls_back_to_polling_and_refreshes_on_drop(monkeypatch) -> None:
    """The wled contract: on a drop, restore the interval, request an immediate refresh, and
    go back around with backoff. Stop after one cycle by making the second connect unsupported."""
    coord = _bare_coordinator(monkeypatch, data=_data())
    attempts = []

    def make_push(session, host):
        attempts.append(host)
        if len(attempts) == 1:
            return _FakePush(
                [Frame(type="snapshot", seq=1, fields={"config": {"volume": 2}})],
                end=KibblePushClosed("closed"),
            )
        return _FakePush([], end=KibblePushUnsupported("refused"))

    monkeypatch.setattr(coordinator_module, "KibblePush", make_push)
    monkeypatch.setattr(coordinator_module, "PUSH_BACKOFF_MIN", 0.0)
    monkeypatch.setattr(coordinator_module.random, "uniform", lambda a, b: 0.0)

    await coord._push_loop()
    await asyncio.sleep(0)  # let the refresh task created on drop run

    assert attempts == ["h", "h"]
    assert coord.push_connected is False
    assert coord.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)
    assert coord.push_reconnects == 1
    assert coord.push_unsupported is True
    coord.async_request_refresh.assert_awaited()


async def test_connection_refused_is_retried_not_treated_as_unsupported(monkeypatch) -> None:
    """A restarting agent refuses connections for a few seconds. That must reconnect, never
    latch `push_unsupported` -- the exact regression seen live on the first deploy."""
    import aiohttp
    from kibble.push import KibblePush

    class _Session:
        async def ws_connect(self, *a, **k):
            raise aiohttp.ClientConnectorError(Mock(), OSError(111, "Connection refused"))

    push = KibblePush(_Session(), "h")
    with pytest.raises(KibblePushClosed) as excinfo:
        async for _ in push.listen():
            pass
    assert not isinstance(excinfo.value, KibblePushUnsupported)


async def test_agent_without_push_leaves_polling_untouched(monkeypatch) -> None:
    coord = _bare_coordinator(monkeypatch, data=_data())
    monkeypatch.setattr(
        coordinator_module, "KibblePush", lambda s, h: _FakePush([], end=KibblePushUnsupported("refused"))
    )
    await coord._push_loop()
    assert coord.push_unsupported is True
    assert coord.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)
    coord.async_set_updated_data.assert_not_called()
    coord.async_request_refresh.assert_not_awaited()


def test_start_push_is_a_single_connection_guard(monkeypatch) -> None:
    coord = _bare_coordinator(monkeypatch)
    created = []

    def create(hass, coro, name):
        coro.close()
        task = Mock()
        task.done.return_value = False
        created.append(task)
        return task

    coord.entry.async_create_background_task = create
    coord.async_start_push()
    coord.async_start_push()
    assert len(created) == 1
    created[0].done.return_value = True
    coord.async_start_push()
    assert len(created) == 2
