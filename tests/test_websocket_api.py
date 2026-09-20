"""`websocket.py`: the pure `kibble/timeline`/`kibble/cats` item-shaping functions, and every
`kibble/*` command handler's `entry_id` resolution / error-code mapping. Same duck-typed style
as `test_coordinator_availability.py` -- no real `HomeAssistant` core instance; `ws_*`'s
original coroutine is reached via `.__wrapped__`, which `async_response`
(`homeassistant.components.websocket_api.decorators`) attaches via `functools.wraps` --
calling the decorated name directly would only schedule a background task on a real event loop,
exactly what these tests don't have and don't need.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from kibble.api import (
    CatInfo,
    DetectionEvent,
    FeedRecord,
    IdentifyScore,
    KibbleConnectionError,
    KibbleError,
    KibbleNotFoundError,
    PendingFace,
)
from kibble.const import DOMAIN
from kibble.websocket import (
    ERR_AGENT_REJECTED,
    ERR_FEEDER_UNREACHABLE,
    ERR_NOT_FOUND,
    TRACK_PAIR_LOOKAHEAD_SECONDS,
    TRACK_PAIR_LOOKBACK_SECONDS,
    cats_items,
    timeline_items,
    ws_cats,
    ws_cats_delete,
    ws_faces_delete_sample,
    ws_faces_pending,
    ws_faces_samples,
    ws_faces_upload,
    ws_timeline,
    ws_vision_last,
)


def _event(
    seq: int,
    ts: int,
    cls: str,
    pet_id: str | None = None,
    cat: str | None = None,
    image: str | None = None,
    score: float | None = None,
    image_before: str | None = None,
    image_after: str | None = None,
) -> DetectionEvent:
    return DetectionEvent(
        seq=seq,
        ts=ts,
        cls=cls,
        image=image,
        cat=cat,
        score=score,
        pet_id=pet_id,
        total_score=None,
        image_before=image_before,
        image_after=image_after,
    )


def _feed(
    ts: int,
    id_: str,
    amount1: int | None,
    amount2: int | None,
    before: str | None = None,
    after: str | None = None,
    manual: bool = False,
) -> FeedRecord:
    return FeedRecord(
        ts=ts, id=id_, amount1=amount1, amount2=amount2, manual=manual, before=before, after=after
    )


# --- timeline_items: merging, capping, feed shape -----------------------------------------------


def test_timeline_merges_detections_and_feeds_newest_first() -> None:
    events = (_event(1, 10, "eat"), _event(2, 30, "eat"))
    feeds = (_feed(20, "a", 3, 2, before="b.h264", after="a.h264"),)
    items = timeline_items(events, feeds, {})
    assert [i["ts"] for i in items] == [30, 20, 10]
    assert items[0]["kind"] == "eat"
    assert items[1]["kind"] == "feed"


def test_timeline_caps_at_100_newest_first() -> None:
    events = tuple(_event(i, i, "eat") for i in range(150))
    items = timeline_items(events, (), {})
    assert len(items) == 100
    assert items[0]["ts"] == 149


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
    """A spontaneous/scheduled cycle with no claimable amount at all -- genuinely unknown, not
    a zero-portion feed."""
    item = timeline_items((), (_feed(1, "scheduled-1", None, None),), {})[0]
    assert item["amount"] is None
    assert item["hopper"] is None


@pytest.mark.parametrize("manual", [True, False])
def test_timeline_feed_carries_the_manual_flag_through(manual: bool) -> None:
    item = timeline_items((), (_feed(1, "a", 1, 1, manual=manual),), {})[0]
    assert item["manual"] is manual


def test_timeline_feed_before_after_are_the_bare_filenames() -> None:
    item = timeline_items((), (_feed(1, "a", 1, 1, before="b.h264", after="a.h264"),), {})[0]
    assert item["before"] == "b.h264"
    assert item["after"] == "a.h264"


# --- timeline_items: kind split (identified/eat/visit) and track pairing ------------------------


def test_timeline_unlabelled_face_events_produce_no_row() -> None:
    """A `face` crop nobody has named yet is `kibble/faces/*` training material, not activity."""
    assert timeline_items((_event(1, 10, "face", image="10-face.jpg"),), (), {}) == []


def test_timeline_labelled_face_event_is_a_named_sighting_with_its_own_crop() -> None:
    """Once the crop carries a cat (classifier or human label), it is the sighting the cats
    tile's "last here" is measured from, so it must appear -- served through the `event` image
    kind, unlike a track's server-side pairing."""
    item = timeline_items((_event(1, 10, "face", cat="Pancake", image="10-face.jpg"),), (), {})[0]
    assert item == {
        "kind": "identified",
        "ts": 10,
        "cat": "Pancake",
        "paired_class": "face",
        "image": "10-face.jpg",
        "image_kind": "event",
    }


def test_timeline_visit_is_hidden_by_default() -> None:
    assert timeline_items((_event(1, 10, "visit"),), (), {}) == []


def test_timeline_visit_is_included_when_include_visits_is_true() -> None:
    items = timeline_items(
        (_event(1, 10, "visit", image="10-visit.jpg"),), (), {}, include_visits=True
    )
    assert len(items) == 1
    assert items[0]["kind"] == "visit"
    assert items[0]["image"] == "10-visit.jpg"


def test_timeline_bare_eat_image_is_the_bare_event_filename() -> None:
    item = timeline_items((_event(1, 10, "eat", image="10-eat.jpg"),), (), {})[0]
    assert item["kind"] == "eat"
    assert item["image"] == "10-eat.jpg"


def test_timeline_identified_resolves_cat_name_from_the_option() -> None:
    events = (_event(1, 10, "track", pet_id="5"),)
    item = timeline_items(events, (), {"5": "Pancake"})[0]
    assert item["kind"] == "identified"
    assert item["cat"] == "Pancake"


def test_timeline_identified_falls_back_to_unknown_cat_with_no_mapping() -> None:
    item = timeline_items((_event(1, 10, "track", pet_id="99"),), (), {"5": "Pancake"})[0]
    assert item["cat"] == "Unknown cat"


def test_timeline_track_pairs_with_the_closest_eat_in_window() -> None:
    events = (
        _event(1, 100, "track", pet_id="5"),
        _event(2, 105, "eat", image="105-eat.jpg"),
        _event(3, 225, "eat", image="225-eat.jpg"),  # outside the pairing window
    )
    items = timeline_items(events, (), {"5": "Pancake"})
    identified = next(i for i in items if i["kind"] == "identified")
    assert identified["paired_class"] == "eat"
    assert identified["image"] == "100"
    # the unpaired eat outside the window still gets its own row
    assert sum(1 for i in items if i["kind"] == "eat") == 1


def test_timeline_track_falls_back_to_visit_when_no_eat_qualifies() -> None:
    events = (_event(1, 100, "track", pet_id="5"), _event(2, 102, "visit", image="102-visit.jpg"))
    item = timeline_items(events, (), {"5": "Pancake"})[0]
    assert item["paired_class"] == "visit"
    assert item["image"] == "100"


def test_timeline_track_has_no_image_when_nothing_qualifies() -> None:
    item = timeline_items((_event(1, 100, "track", pet_id="5"),), (), {"5": "Pancake"})[0]
    assert item["paired_class"] is None
    assert item["image"] is None


def test_timeline_track_pairing_window_bounds_are_inclusive() -> None:
    events = (
        _event(1, 100, "track", pet_id="5"),
        _event(2, 100 - TRACK_PAIR_LOOKBACK_SECONDS, "eat", image="e.jpg"),
    )
    item = timeline_items(events, (), {"5": "Pancake"})[0]
    assert item["paired_class"] == "eat"


def test_timeline_track_pairing_respects_the_lookback_and_lookahead_bounds() -> None:
    events = (
        _event(1, 100, "track", pet_id="5"),
        _event(2, 100 - TRACK_PAIR_LOOKBACK_SECONDS - 1, "eat", image="early.jpg"),
        _event(3, 100 + TRACK_PAIR_LOOKAHEAD_SECONDS + 1, "eat", image="late.jpg"),
    )
    items = timeline_items(events, (), {"5": "Pancake"})
    identified = next(i for i in items if i["kind"] == "identified")
    assert identified["paired_class"] is None
    assert identified["image"] is None
    # both eats stayed outside the window, so both are still their own unclaimed rows
    assert sum(1 for i in items if i["kind"] == "eat") == 2


def test_timeline_claimed_eat_does_not_also_appear_as_its_own_bare_row() -> None:
    events = (_event(1, 100, "track", pet_id="5"), _event(2, 105, "eat", image="105-eat.jpg"))
    items = timeline_items(events, (), {"5": "Pancake"})
    assert [i["kind"] for i in items] == ["identified"]


def test_timeline_claimed_visit_stays_hidden_even_with_include_visits_true() -> None:
    events = (_event(1, 100, "track", pet_id="5"), _event(2, 102, "visit", image="v.jpg"))
    items = timeline_items(events, (), {"5": "Pancake"}, include_visits=True)
    assert [i["kind"] for i in items] == ["identified"]


def test_timeline_unclaimed_eat_stays_its_own_row() -> None:
    events = (_event(1, 100, "track", pet_id="5"), _event(2, 500, "eat", image="500-eat.jpg"))
    items = timeline_items(events, (), {"5": "Pancake"})
    assert {i["kind"] for i in items} == {"identified", "eat"}


def test_timeline_two_close_tracks_can_independently_claim_the_same_eat() -> None:
    """No exclusivity between tracks: the agent's own per-request pairing lookup would
    likewise serve the same image for either track's own `ts`."""
    events = (
        _event(1, 100, "track", pet_id="5"),
        _event(2, 110, "track", pet_id="6"),
        _event(3, 105, "eat", image="105-eat.jpg"),
    )
    items = timeline_items(events, (), {"5": "Pancake", "6": "Kitty"})
    identified = [i for i in items if i["kind"] == "identified"]
    assert len(identified) == 2
    assert all(i["paired_class"] == "eat" for i in identified)
    assert "eat" not in [i["kind"] for i in items]


# --- timeline_items: LibreFeed-shaped visit/eat carrying their own `cat` directly ---------------


def test_timeline_librefeed_visit_with_a_cat_is_one_identified_row() -> None:
    """LibreFeed never emits a `track`; the identification lives on the `visit`/`eat` event
    itself. `score` is carried through when the event has one."""
    events = (_event(1, 10, "visit", cat="Pancake", image="10-visit.jpg", score=0.83),)
    items = timeline_items(events, (), {})
    assert items == [
        {
            "kind": "identified",
            "ts": 10,
            "cat": "Pancake",
            "paired_class": "visit",
            "image": "10-visit.jpg",
            "image_kind": "event",
            "score": 0.83,
        }
    ]


def test_timeline_librefeed_visit_without_a_cat_stays_gated_behind_include_visits() -> None:
    """The exact same visit, minus the identification, is bare noise again -- same gate as
    the vendor stack's own unclaimed `visit`."""
    events = (_event(1, 10, "visit", image="10-visit.jpg"),)
    assert timeline_items(events, (), {}) == []
    items = timeline_items(events, (), {}, include_visits=True)
    assert items[0]["kind"] == "visit"


def test_timeline_librefeed_eat_without_a_cat_still_shows() -> None:
    """An `eat` never needs `include_visits` -- identified or not, on either stack."""
    items = timeline_items((_event(1, 10, "eat", image="10-eat.jpg"),), (), {})
    assert [i["kind"] for i in items] == ["eat"]


def test_timeline_librefeed_eat_with_a_cat_is_one_identified_row_with_no_score_key() -> None:
    """No `score` on the event means no `score` key on the row -- not a `None` placeholder."""
    items = timeline_items((_event(1, 10, "eat", cat="Kitty", image="10-eat.jpg"),), (), {})
    assert items == [
        {
            "kind": "identified",
            "ts": 10,
            "cat": "Kitty",
            "paired_class": "eat",
            "image": "10-eat.jpg",
            "image_kind": "event",
        }
    ]


def test_timeline_vendor_track_and_visit_pair_stays_a_single_identified_row() -> None:
    """Regression: a vendor-shaped `track` paired with a bare `visit` (no `cat` of its own,
    since the vendor's identification lives on the `track`, not the `visit`) must still
    collapse to exactly one `identified` row, unaffected by the LibreFeed direct-cat path."""
    events = (_event(1, 100, "track", pet_id="5"), _event(2, 102, "visit", image="102-visit.jpg"))
    items = timeline_items(events, (), {"5": "Pancake"}, include_visits=True)
    assert [i["kind"] for i in items] == ["identified"]
    assert items[0]["cat"] == "Pancake"
    assert items[0]["image"] == "100"


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


# --- WS command handlers: entry_id resolution / error mapping -----------------------------------


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
    await ws_timeline.__wrapped__(hass, connection, {"id": 3, "entry_id": "e1", "include_visits": False})
    connection.send_result.assert_called_once()
    msg_id, payload = connection.send_result.call_args.args
    assert msg_id == 3
    assert payload["items"][0]["ts"] == 10
    connection.send_error.assert_not_called()


async def test_ws_timeline_forwards_include_visits_to_timeline_items() -> None:
    data = SimpleNamespace(events=(_event(1, 10, "visit"),), feeds=())
    coordinator = _fake_coordinator(data=data)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_timeline.__wrapped__(hass, connection, {"id": 3, "entry_id": "e1", "include_visits": True})
    _, payload = connection.send_result.call_args.args
    assert [i["kind"] for i in payload["items"]] == ["visit"]


async def test_ws_cats_reverse_maps_the_vendor_pet_ids_option() -> None:
    data = SimpleNamespace(cats=(CatInfo(name="Kitty", samples=1, last_seen=None, avatar=None),))
    coordinator = _fake_coordinator(data=data, options={"vendor_pet_ids": "101320712=Kitty"})
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_cats.__wrapped__(hass, connection, {"id": 4, "entry_id": "e1"})
    _, payload = connection.send_result.call_args.args
    assert payload["cats"][0]["vendor_pet_id"] == "101320712"


async def test_ws_cats_delete_calls_the_coordinator_and_returns_its_result() -> None:
    coordinator = _fake_coordinator()
    coordinator.async_delete_cat = AsyncMock(return_value={})
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_cats_delete.__wrapped__(hass, connection, {"id": 9, "entry_id": "e1", "name": "Kitty"})
    coordinator.async_delete_cat.assert_awaited_once_with("Kitty")
    connection.send_result.assert_called_once_with(9, {})
    connection.send_error.assert_not_called()


async def test_ws_cats_delete_maps_an_unknown_cat_to_not_found() -> None:
    coordinator = _fake_coordinator()
    coordinator.async_delete_cat = AsyncMock(side_effect=KibbleNotFoundError("no such cat"))
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_cats_delete.__wrapped__(hass, connection, {"id": 10, "entry_id": "e1", "name": "Ghost"})
    connection.send_error.assert_called_once_with(10, ERR_NOT_FOUND, "no such cat")
    connection.send_result.assert_not_called()


async def test_ws_cats_delete_maps_a_connection_failure_to_feeder_unreachable() -> None:
    coordinator = _fake_coordinator()
    coordinator.async_delete_cat = AsyncMock(side_effect=KibbleConnectionError("down"))
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_cats_delete.__wrapped__(hass, connection, {"id": 11, "entry_id": "e1", "name": "Kitty"})
    assert connection.send_error.call_args.args[1] == ERR_FEEDER_UNREACHABLE


async def test_ws_cats_delete_maps_any_other_rejection_to_agent_rejected() -> None:
    coordinator = _fake_coordinator()
    coordinator.async_delete_cat = AsyncMock(side_effect=KibbleError("bad name"))
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_cats_delete.__wrapped__(hass, connection, {"id": 12, "entry_id": "e1", "name": "??"})
    assert connection.send_error.call_args.args[1] == ERR_AGENT_REJECTED


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


async def test_ws_faces_upload_decodes_base64_and_forwards_raw_bytes() -> None:
    coordinator = _fake_coordinator()
    coordinator.async_upload_face_sample = AsyncMock(
        return_value={"name": "upload-1.jpg", "samples": 2}
    )
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    jpeg_b64 = base64.b64encode(b"\xff\xd8\xff-fake-jpeg-bytes").decode()
    await ws_faces_upload.__wrapped__(
        hass, connection, {"id": 13, "entry_id": "e1", "cat": "Kitty", "jpeg_b64": jpeg_b64}
    )
    coordinator.async_upload_face_sample.assert_awaited_once_with(
        "Kitty", b"\xff\xd8\xff-fake-jpeg-bytes"
    )
    connection.send_result.assert_called_once_with(13, {"name": "upload-1.jpg", "samples": 2})


async def test_ws_faces_upload_maps_an_unknown_cat_to_not_found() -> None:
    coordinator = _fake_coordinator()
    coordinator.async_upload_face_sample = AsyncMock(side_effect=KibbleNotFoundError("no such cat"))
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    jpeg_b64 = base64.b64encode(b"jpeg").decode()
    await ws_faces_upload.__wrapped__(
        hass, connection, {"id": 14, "entry_id": "e1", "cat": "Ghost", "jpeg_b64": jpeg_b64}
    )
    assert connection.send_error.call_args.args[1] == ERR_NOT_FOUND
    connection.send_result.assert_not_called()


async def test_ws_faces_upload_rejects_malformed_base64_without_calling_the_agent() -> None:
    coordinator = _fake_coordinator()
    coordinator.async_upload_face_sample = AsyncMock()
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_faces_upload.__wrapped__(
        hass, connection, {"id": 16, "entry_id": "e1", "cat": "Kitty", "jpeg_b64": "not-base64!!"}
    )
    assert connection.send_error.call_args.args[1] == websocket_api.ERR_INVALID_FORMAT
    connection.send_result.assert_not_called()
    coordinator.async_upload_face_sample.assert_not_awaited()


async def test_ws_faces_delete_sample_calls_the_coordinator_with_cat_and_name() -> None:
    coordinator = _fake_coordinator()
    coordinator.async_delete_face_sample = AsyncMock(return_value={})
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_faces_delete_sample.__wrapped__(
        hass, connection, {"id": 15, "entry_id": "e1", "cat": "Kitty", "name": "upload-1.jpg"}
    )
    coordinator.async_delete_face_sample.assert_awaited_once_with("Kitty", "upload-1.jpg")
    connection.send_result.assert_called_once_with(15, {})


async def test_ws_faces_delete_sample_maps_an_unknown_sample_to_not_found() -> None:
    coordinator = _fake_coordinator()
    coordinator.async_delete_face_sample = AsyncMock(side_effect=KibbleNotFoundError("gone"))
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_faces_delete_sample.__wrapped__(
        hass, connection, {"id": 16, "entry_id": "e1", "cat": "Kitty", "name": "gone.jpg"}
    )
    assert connection.send_error.call_args.args[1] == ERR_NOT_FOUND


# --- ws_vision_last: on-demand fetch, 404-from-an-old-daemon handling --------------------------


async def test_ws_vision_last_maps_an_old_daemons_404_to_a_null_frame_not_an_error() -> None:
    """`/vision/last` is brand new -- an agent old enough to predate it 404s exactly like any
    other unimplemented route (`api.py`'s `vision_last`, `not_found_is_missing`). The card
    polls this once a second purely to draw an overlay; there being nothing to draw yet is not
    a feeder connectivity problem, so this must resolve as a null frame, like the agent's own
    "nothing analysed yet" -- not `ERR_FEEDER_UNREACHABLE`."""
    client = AsyncMock(
        vision_last=AsyncMock(
            side_effect=KibbleNotFoundError("/vision/last not supported by this agent version")
        )
    )
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()
    await ws_vision_last.__wrapped__(hass, connection, {"id": 20, "entry_id": "e1"})
    connection.send_result.assert_called_once_with(20, {"frame": None})
    connection.send_error.assert_not_called()


def test_an_eat_row_carries_the_dish_photos_taken_around_the_meal() -> None:
    """LibreFeed photographs the dish when a meal starts and when it ends, so the card can
    show how much actually went. Kitty's 01:51 meal on 2026-09-20 was recorded with both
    photos on disk and served by the daemon, and the card showed neither: `DetectionEvent`
    had no field for them, so they were dropped at the parse step and never reached a row."""
    named = _event(1, 100, "eat", cat="Kitty", image="face.jpg", image_before="b.jpg", image_after="a.jpg")
    bare = _event(2, 200, "eat", image="crop.jpg", image_before="b2.jpg", image_after="a2.jpg")
    rows = timeline_items((named, bare), (), {})
    by_ts = {row["ts"]: row for row in rows}
    assert (by_ts[100]["image_before"], by_ts[100]["image_after"]) == ("b.jpg", "a.jpg")
    assert (by_ts[200]["image_before"], by_ts[200]["image_after"]) == ("b2.jpg", "a2.jpg")


def test_a_row_with_no_meal_to_compare_grows_no_photo_keys() -> None:
    """A visit has no meal, and a vendor-stack agent never sends these at all. Emitting empty
    keys anyway would make "the pair was not captured" indistinguishable from "this kind of
    row never has one", which is the distinction the card's compare view keys off."""
    visit = _event(1, 100, "visit", cat="Kitty", image="face.jpg")
    eat_without = _event(2, 200, "eat", cat="Kitty", image="face2.jpg")
    rows = {row["ts"]: row for row in timeline_items((visit, eat_without), (), {}, include_visits=True)}
    assert "image_before" not in rows[100] and "image_after" not in rows[100]
    assert "image_before" not in rows[200] and "image_after" not in rows[200]
