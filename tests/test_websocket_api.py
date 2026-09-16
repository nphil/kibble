"""`websocket.py`: the pure `kibble/timeline`/`kibble/cats` item-shaping functions, and the
four `kibble/*` command handlers' `entry_id` resolution / feeder-unreachable error handling.
Same duck-typed style as `test_coordinator_availability.py` -- no real `HomeAssistant` core
instance; `ws_*`'s original coroutine is reached via `.__wrapped__`, which `async_response`
(`homeassistant.components.websocket_api.decorators`) attaches via `functools.wraps` --
calling the decorated name directly would only schedule a background task on a real event
loop, exactly what these tests don't have and don't need.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.config_entries import ConfigEntryState
from kibble.api import CatInfo, DetectionEvent, FeedRecord, IdentifyScore, KibbleConnectionError, PendingFace
from kibble.const import DOMAIN
from kibble.websocket import (
    ERR_FEEDER_UNREACHABLE,
    cats_items,
    timeline_items,
    ws_cats,
    ws_faces_pending,
    ws_faces_samples,
    ws_timeline,
)


def _event(
    seq: int, ts: int, cls: str, pet_id: str | None = None, cat: str | None = None, image: str | None = None
) -> DetectionEvent:
    return DetectionEvent(
        seq=seq, ts=ts, cls=cls, image=image, cat=cat, score=None, pet_id=pet_id, total_score=None
    )


def _feed(
    ts: int, id_: str, amount1: int | None, amount2: int | None, before: str | None = None, after: str | None = None
) -> FeedRecord:
    return FeedRecord(ts=ts, id=id_, amount1=amount1, amount2=amount2, manual=False, before=before, after=after)


# --- timeline_items ---------------------------------------------------------------------------


def test_timeline_merges_detections_and_feeds_newest_first() -> None:
    events = (_event(1, 10, "visit"), _event(2, 30, "eat"))
    feeds = (_feed(20, "a", 3, 2, before="b.h264", after="a.h264"),)
    items = timeline_items(events, feeds, {})
    assert [i["ts"] for i in items] == [30, 20, 10]
    assert items[0]["kind"] == "detection"
    assert items[1]["kind"] == "feed"


def test_timeline_caps_at_100_newest_first() -> None:
    events = tuple(_event(i, i, "visit") for i in range(150))
    items = timeline_items(events, (), {})
    assert len(items) == 100
    assert items[0]["ts"] == 149


def test_timeline_detection_resolves_vendor_cat_from_the_option() -> None:
    events = (_event(1, 10, "track", pet_id="5"),)
    item = timeline_items(events, (), {"5": "Pancake"})[0]
    assert item["pet_id"] == "5"
    assert item["vendor_cat"] == "Pancake"


def test_timeline_detection_vendor_cat_is_none_with_no_pet_id() -> None:
    item = timeline_items((_event(1, 10, "eat"),), (), {"5": "Pancake"})[0]
    assert item["vendor_cat"] is None


def test_timeline_detection_image_is_the_bare_event_filename() -> None:
    item = timeline_items((_event(1, 10, "face", image="10-face.jpg"),), (), {})[0]
    assert item["image"] == "10-face.jpg"


def test_timeline_feed_amount_and_hopper_from_both_amounts() -> None:
    item = timeline_items((), (_feed(1, "a", 3, 2),), {})[0]
    assert item["amount"] == 5
    assert item["hopper"] == "both"


@pytest.mark.parametrize(
    ("amount1", "amount2", "expected_hopper", "expected_amount"),
    [(3, None, "1", 3), (None, 4, "2", 4)],
)
def test_timeline_feed_hopper_from_a_single_side(
    amount1: int | None, amount2: int | None, expected_hopper: str, expected_amount: int
) -> None:
    item = timeline_items((), (_feed(1, "a", amount1, amount2),), {})[0]
    assert item["hopper"] == expected_hopper
    assert item["amount"] == expected_amount


def test_timeline_feed_unknown_amounts_stay_none_not_zero() -> None:
    """A spontaneous/scheduled cycle with no claimable manual note (`FeedCapture::start_
    cycle`'s fallback) -- genuinely unknown, not a zero-portion feed."""
    item = timeline_items((), (_feed(1, "scheduled-1", None, None),), {})[0]
    assert item["amount"] is None
    assert item["hopper"] is None


def test_timeline_feed_outcome_is_always_none_but_the_key_stays_present() -> None:
    item = timeline_items((), (_feed(1, "a", 1, 1),), {})[0]
    assert "outcome" in item
    assert item["outcome"] is None


def test_timeline_feed_before_after_are_the_bare_filenames() -> None:
    item = timeline_items((), (_feed(1, "a", 1, 1, before="b.h264", after="a.h264"),), {})[0]
    assert item["before"] == "b.h264"
    assert item["after"] == "a.h264"


# --- cats_items --------------------------------------------------------------------------------


def test_cats_items_sorts_by_name_and_assigns_a_stable_color_index() -> None:
    cats = (
        CatInfo(name="Pancake", samples=9, last_seen=2, avatar="p.jpg"),
        CatInfo(name="Kitty", samples=12, last_seen=1, avatar=None),
    )
    items = cats_items(cats, {})
    assert [c["name"] for c in items] == ["Kitty", "Pancake"]
    assert [c["color_index"] for c in items] == [0, 1]


def test_cats_items_reverse_maps_vendor_pet_id_from_the_option() -> None:
    cats = (CatInfo(name="Kitty", samples=1, last_seen=None, avatar=None),)
    items = cats_items(cats, {"101320712": "Kitty", "5": "Pancake"})
    assert items[0]["vendor_pet_id"] == "101320712"


def test_cats_items_vendor_pet_id_is_none_for_an_unmapped_cat() -> None:
    cats = (CatInfo(name="Ghost", samples=0, last_seen=None, avatar=None),)
    assert cats_items(cats, {"5": "Pancake"})[0]["vendor_pet_id"] is None


def test_cats_items_carries_samples_last_seen_and_avatar_through() -> None:
    cats = (CatInfo(name="Kitty", samples=12, last_seen=1700000000, avatar="k.jpg"),)
    item = cats_items(cats, {})[0]
    assert item["samples"] == 12
    assert item["last_seen"] == 1700000000
    assert item["avatar"] == "k.jpg"


# --- WS command handlers: entry_id resolution / feeder-unreachable errors ----------------------


def _fake_hass(entry: SimpleNamespace | None) -> SimpleNamespace:
    return SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=Mock(return_value=entry)))


def _fake_connection() -> Mock:
    return Mock(send_result=Mock(), send_error=Mock())


def _fake_entry(coordinator: SimpleNamespace, state=ConfigEntryState.LOADED) -> SimpleNamespace:
    return SimpleNamespace(domain=DOMAIN, state=state, title="Cat Feeder", runtime_data=coordinator)


def _fake_coordinator(*, data=None, options=None, client=None) -> SimpleNamespace:
    return SimpleNamespace(
        data=data or SimpleNamespace(events=(), feeds=(), cats=()),
        entry=SimpleNamespace(options=options or {}),
        client=client or AsyncMock(),
    )


async def test_ws_timeline_sends_not_found_for_an_unknown_entry() -> None:
    hass = _fake_hass(None)
    connection = _fake_connection()
    await ws_timeline.__wrapped__(hass, connection, {"id": 1, "entry_id": "bogus"})
    connection.send_error.assert_called_once()
    assert connection.send_error.call_args.args[0] == 1
    connection.send_result.assert_not_called()


async def test_ws_timeline_sends_not_found_for_an_entry_not_currently_loaded() -> None:
    coordinator = _fake_coordinator()
    entry = _fake_entry(coordinator, state=ConfigEntryState.NOT_LOADED)
    hass = _fake_hass(entry)
    connection = _fake_connection()
    await ws_timeline.__wrapped__(hass, connection, {"id": 2, "entry_id": "e1"})
    connection.send_error.assert_called_once()
    connection.send_result.assert_not_called()


async def test_ws_timeline_sends_result_for_a_loaded_entry() -> None:
    data = SimpleNamespace(events=(_event(1, 10, "eat"),), feeds=())
    coordinator = _fake_coordinator(data=data)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_timeline.__wrapped__(hass, connection, {"id": 3, "entry_id": "e1"})
    connection.send_result.assert_called_once()
    msg_id, payload = connection.send_result.call_args.args
    assert msg_id == 3
    assert payload["items"][0]["ts"] == 10
    connection.send_error.assert_not_called()


async def test_ws_cats_reverse_maps_the_vendor_pet_ids_option() -> None:
    data = SimpleNamespace(cats=(CatInfo(name="Kitty", samples=1, last_seen=None, avatar=None),))
    coordinator = _fake_coordinator(data=data, options={"vendor_pet_ids": "101320712=Kitty"})
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_cats.__wrapped__(hass, connection, {"id": 4, "entry_id": "e1"})
    _, payload = connection.send_result.call_args.args
    assert payload["cats"][0]["vendor_pet_id"] == "101320712"


async def test_ws_faces_pending_calls_the_client_on_demand_and_resolves_vendor_cat() -> None:
    guess = IdentifyScore(cat="Pancake", score=0.83)
    crop = PendingFace(name="1-5.jpg", ts=1, vendor_pet_id="5", guess=guess)
    client = AsyncMock(pending_faces=AsyncMock(return_value=[crop]))
    coordinator = _fake_coordinator(options={"vendor_pet_ids": "5=Pancake"}, client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_faces_pending.__wrapped__(hass, connection, {"id": 5, "entry_id": "e1"})
    client.pending_faces.assert_awaited_once()
    _, payload = connection.send_result.call_args.args
    assert payload["crops"] == [
        {
            "name": "1-5.jpg",
            "ts": 1,
            "vendor_pet_id": "5",
            "vendor_cat": "Pancake",
            "guess": {"cat": "Pancake", "score": 0.83},
        }
    ]


async def test_ws_faces_pending_reports_feeder_unreachable_not_a_generic_error() -> None:
    client = AsyncMock(pending_faces=AsyncMock(side_effect=KibbleConnectionError("down")))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_faces_pending.__wrapped__(hass, connection, {"id": 6, "entry_id": "e1"})
    connection.send_error.assert_called_once()
    assert connection.send_error.call_args.args[1] == ERR_FEEDER_UNREACHABLE
    connection.send_result.assert_not_called()


async def test_ws_faces_samples_calls_the_client_with_the_requested_cat() -> None:
    from kibble.api import FaceSample

    client = AsyncMock(faces_samples=AsyncMock(return_value=[FaceSample(name="a.jpg", ts=1)]))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_faces_samples.__wrapped__(hass, connection, {"id": 7, "entry_id": "e1", "cat": "Kitty"})
    client.faces_samples.assert_awaited_once_with("Kitty")
    _, payload = connection.send_result.call_args.args
    assert payload["samples"] == [{"name": "a.jpg", "ts": 1}]


async def test_ws_faces_samples_reports_feeder_unreachable() -> None:
    client = AsyncMock(faces_samples=AsyncMock(side_effect=KibbleConnectionError("down")))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_faces_samples.__wrapped__(hass, connection, {"id": 8, "entry_id": "e1", "cat": "Kitty"})
    connection.send_error.assert_called_once()
    assert connection.send_error.call_args.args[1] == ERR_FEEDER_UNREACHABLE
