"""`websocket.py`: every `kibble/*` command handler driven directly with stub
`connection`/`hass`/`msg` objects, covering entry_id resolution, the WS-level input validation
each command's own schema declares, and response shapes for `kibble/timeline`,
`kibble/timeline/subscribe`, `kibble/event`, `kibble/label`, `kibble/review`, `kibble/cats`
(+`add`/`delete`), `kibble/training`(+`remove`), and the pre-existing vision/calibration
commands (unrelated to the AI pipeline, unchanged).

Every command reads/writes through `coordinator.store`/`coordinator.engine` -- stubbed here as
`AsyncMock`-backed objects, never a real SQLite store (`store.py`'s own test suite proves the
real query/mutation logic); this file's job is the WS *plumbing*: does the right store method
get called with the right arguments, does its result reach `send_result` in the documented
shape, does a failure map to the documented error code.

`async_response`'s wrapper carries `functools.wraps`, so `ws_*.__wrapped__` reaches the raw
coroutine directly -- past both the background-task scheduling `async_response` adds and the
schema tag `websocket_command` bolts on (same object, per that decorator's own source: it
returns `func` unchanged). Schema validation itself is exercised separately below via each
command's own exposed `_ws_schema`, never through `.__wrapped__` (which never sees it).
`ws_timeline_subscribe` is `@callback`-only (no `async_response`), so it is called directly, no
unwrapping needed -- but it schedules its own first check via `hass.async_create_task`, so
those tests use a real `asyncio` task and yield once for it to run.
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from kibble.api import KibbleCalibrationBusyError, KibbleConnectionError, KibbleError, VisionAreas
from kibble.const import DOMAIN
from kibble.store import CatStats, DeviceIdentitySummary
from kibble.websocket import (
    ERR_AGENT_REJECTED,
    ERR_CALIBRATION_BUSY,
    ERR_FEEDER_UNREACHABLE,
    ERR_NOT_FOUND,
    _feed_view,
    _render_feeds,
    vol,
    ws_cats,
    ws_cats_add,
    ws_cats_delete,
    ws_calibration,
    ws_calibration_action,
    ws_event,
    ws_label,
    ws_session_label,
    ws_session_subject,
    ws_sample_label,
    ws_review,
    ws_timeline,
    ws_timeline_subscribe,
    ws_training,
    ws_training_remove,
    ws_vision_areas,
    ws_vision_areas_set,
    ws_vision_bowl,
    ws_vision_bowl_set,
    ws_vision_last,
)

# --- shared fakes --------------------------------------------------------------------------


def _fake_hass(entry: SimpleNamespace | None, *, async_create_task=None) -> SimpleNamespace:
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=Mock(return_value=entry)),
        async_create_task=async_create_task if async_create_task is not None else Mock(),
    )


def _fake_connection() -> Mock:
    connection = Mock(send_result=Mock(), send_error=Mock(), send_message=Mock())
    connection.subscriptions = {}
    return connection


def _fake_entry(coordinator: SimpleNamespace, state: ConfigEntryState = ConfigEntryState.LOADED) -> SimpleNamespace:
    return SimpleNamespace(domain=DOMAIN, state=state, title="Cat Feeder", runtime_data=coordinator)


def _fake_coordinator(
    *,
    store=None,
    engine=None,
    client=None,
    identity: DeviceIdentitySummary | None = None,
    retention_days: int = 14,
    async_calibration_action=None,
    single_hopper: bool = False,
    hopper_food=None,
) -> SimpleNamespace:
    """A duck-typed `KibbleCoordinator` with only what `websocket.py` ever reads: a store, an
    identity engine, a device client, the retention window, `data.identity`, the hopper
    divider mode, and each hopper's current food name (`_feed_view`)."""
    coordinator = SimpleNamespace(
        store=store if store is not None else AsyncMock(),
        engine=engine if engine is not None else Mock(loo_accuracy=Mock(return_value=None)),
        client=client if client is not None else AsyncMock(),
        data=SimpleNamespace(
            identity=identity if identity is not None else DeviceIdentitySummary.empty(),
            calibration=None,
        ),
        async_refresh_identity_snapshot=AsyncMock(),
        async_calibration_action=async_calibration_action if async_calibration_action is not None else AsyncMock(),
        retention_cutoff=Mock(return_value=1_700_000_000),
        retention_days=Mock(return_value=retention_days),
        single_hopper=single_hopper,
        hopper_food=hopper_food if hopper_food is not None else Mock(return_value=None),
    )
    return coordinator


def _capturing_async_create_task() -> tuple[Mock, list]:
    """A `hass.async_create_task` stand-in that records the coroutine it was handed instead of
    running it -- the test decides whether to `await` it (to drive a background follow-up) or
    `.close()` it (when the follow-up itself is not under test)."""
    captured: list = []

    def _capture(coro):
        captured.append(coro)
        return Mock()

    return Mock(side_effect=_capture), captured


# --- kibble/timeline -----------------------------------------------------------------------


async def test_ws_timeline_sends_not_found_for_an_unknown_entry() -> None:
    hass = _fake_hass(None)
    connection = _fake_connection()
    await ws_timeline.__wrapped__(hass, connection, {"id": 1, "entry_id": "bogus", "limit": 30})
    connection.send_error.assert_called_once_with(1, websocket_api.ERR_NOT_FOUND, "Unknown entry")
    connection.send_result.assert_not_called()


async def test_ws_timeline_sends_not_found_for_an_entry_not_currently_loaded() -> None:
    coordinator = _fake_coordinator()
    hass = _fake_hass(_fake_entry(coordinator, state=ConfigEntryState.SETUP_RETRY))
    connection = _fake_connection()
    await ws_timeline.__wrapped__(hass, connection, {"id": 2, "entry_id": "e1", "limit": 30})
    connection.send_error.assert_called_once_with(2, websocket_api.ERR_NOT_FOUND, "Entry not loaded")
    connection.send_result.assert_not_called()


async def test_ws_timeline_forwards_limit_and_cursor_and_sends_the_stores_page_unchanged() -> None:
    page = {"items": [{"uid": "e1"}], "cursor": "abc", "has_more": True}
    store = AsyncMock(async_timeline_page=AsyncMock(return_value=page))
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_timeline.__wrapped__(hass, connection, {"id": 3, "entry_id": "e1", "limit": 10, "cursor": "prev"})

    store.async_timeline_page.assert_awaited_once_with(limit=10, cursor="prev")
    connection.send_result.assert_called_once_with(3, page)
    connection.send_error.assert_not_called()


async def test_ws_timeline_maps_a_malformed_cursor_to_invalid_cursor() -> None:
    store = AsyncMock(async_timeline_page=AsyncMock(side_effect=ValueError("invalid timeline cursor")))
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_timeline.__wrapped__(hass, connection, {"id": 4, "entry_id": "e1", "limit": 30, "cursor": "garbage"})

    connection.send_error.assert_called_once_with(4, "invalid_cursor", "invalid timeline cursor")
    connection.send_result.assert_not_called()


def test_ws_timeline_schema_defaults_limit_to_30_when_omitted() -> None:
    validated = ws_timeline._ws_schema({"id": 1, "type": "kibble/timeline", "entry_id": "e1"})
    assert validated["limit"] == 30


@pytest.mark.parametrize("limit", [0, 101])
def test_ws_timeline_schema_rejects_limit_outside_1_to_100(limit: int) -> None:
    with pytest.raises(vol.Invalid):
        ws_timeline._ws_schema({"id": 1, "type": "kibble/timeline", "entry_id": "e1", "limit": limit})


# --- _feed_view / _render_feeds (per-hopper feed rendering, docs/37-hopper-full.md) --------


def _stored_feed(**overrides) -> dict:
    base = {
        "portions": 3.0, "scheduled": False, "confirmed": True,
        "amount1": 1, "amount2": 2, "food1": None, "food2": None, "single": False,
    }
    return {**base, **overrides}


def test_feed_view_dual_mode_lists_both_sides_with_their_recorded_food() -> None:
    coordinator = _fake_coordinator(single_hopper=False)
    feed = _stored_feed(food1="Kibble", food2="Freeze-Dried")

    assert _feed_view(coordinator, feed) == {
        "portions": 3.0, "scheduled": False, "confirmed": True, "single": False,
        "sides": [
            {"hopper": 1, "portions": 1, "food": "Kibble"},
            {"hopper": 2, "portions": 2, "food": "Freeze-Dried"},
        ],
    }


def test_feed_view_omits_a_side_that_dispensed_zero_portions() -> None:
    coordinator = _fake_coordinator(single_hopper=False)
    feed = _stored_feed(amount1=5, amount2=0, food1="Kibble")

    assert _feed_view(coordinator, feed)["sides"] == [{"hopper": 1, "portions": 5, "food": "Kibble"}]


def test_feed_view_single_mode_never_lists_sides_even_with_both_amounts_recorded() -> None:
    coordinator = _fake_coordinator(single_hopper=False)
    feed = _stored_feed(single=True)

    view = _feed_view(coordinator, feed)
    assert view["single"] is True
    assert view["sides"] == []


def test_feed_view_null_recorded_mode_falls_back_to_the_coordinators_current_mode() -> None:
    """A row ingested before this feature shipped has no recorded mode of its own."""
    feed = _stored_feed(single=None)

    dual = _feed_view(_fake_coordinator(single_hopper=False), feed)
    assert dual["single"] is False and dual["sides"] != []

    single = _feed_view(_fake_coordinator(single_hopper=True), feed)
    assert single["single"] is True and single["sides"] == []


def test_feed_view_falls_back_to_the_coordinators_current_food_only_when_the_row_never_recorded_one() -> None:
    coordinator = _fake_coordinator(
        single_hopper=False, hopper_food=Mock(side_effect=lambda n: {1: "Salmon"}.get(n))
    )

    unnamed = _feed_view(coordinator, _stored_feed(food1=None))
    assert unnamed["sides"][0]["food"] == "Salmon"

    named = _feed_view(coordinator, _stored_feed(food1="Old Kibble"))
    assert named["sides"][0]["food"] == "Old Kibble"  # a rename never rewrites a recorded name


def test_render_feeds_leaves_non_feed_items_untouched() -> None:
    coordinator = _fake_coordinator(single_hopper=False)
    items = [{"uid": "e1", "kind": "visit"}]

    assert _render_feeds(coordinator, items) == items


# --- kibble/timeline/subscribe -------------------------------------------------------------


def _listening_coordinator(**kwargs) -> SimpleNamespace:
    coordinator = _fake_coordinator(**kwargs)
    listeners: list = []
    coordinator.async_add_listener = Mock(
        side_effect=lambda cb: (listeners.append(cb), (lambda: listeners.remove(cb)))[1]
    )
    coordinator.fire = lambda: [cb() for cb in list(listeners)]
    coordinator.listener_count = lambda: len(listeners)
    return coordinator


async def _settle() -> None:
    """Lets a task scheduled through a real `asyncio.ensure_future` (this file's
    `hass.async_create_task` stand-in for the subscribe tests) actually run to completion."""
    for _ in range(3):
        await asyncio.sleep(0)


def _real_task_hass(entry) -> SimpleNamespace:
    return _fake_hass(entry, async_create_task=lambda coro: asyncio.ensure_future(coro))


async def test_timeline_subscribe_sends_changed_on_the_first_check_then_again_on_a_real_change() -> None:
    page_v1 = {"items": [{"uid": "e1", "start": 10}], "cursor": None, "has_more": False}
    page_v2 = {"items": [{"uid": "e2", "start": 20}, {"uid": "e1", "start": 10}], "cursor": None, "has_more": False}
    store = AsyncMock(async_timeline_page=AsyncMock(return_value=page_v1))
    coordinator = _listening_coordinator(store=store)
    hass = _real_task_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    ws_timeline_subscribe(hass, connection, {"id": 5, "entry_id": "e1"})
    await _settle()

    connection.send_result.assert_called_once_with(5)
    assert 5 in connection.subscriptions
    assert connection.send_message.call_count == 1
    assert connection.send_message.call_args.args[0]["event"] == {"changed": True}

    store.async_timeline_page.return_value = page_v2
    coordinator.fire()
    await _settle()

    assert connection.send_message.call_count == 2


async def test_timeline_subscribe_stays_quiet_when_the_page_is_unchanged() -> None:
    page = {"items": [{"uid": "e1", "start": 10}], "cursor": None, "has_more": False}
    store = AsyncMock(async_timeline_page=AsyncMock(return_value=page))
    coordinator = _listening_coordinator(store=store)
    hass = _real_task_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    ws_timeline_subscribe(hass, connection, {"id": 6, "entry_id": "e1"})
    await _settle()
    assert connection.send_message.call_count == 1

    for _ in range(5):
        coordinator.fire()
    await _settle()

    assert connection.send_message.call_count == 1


async def test_timeline_subscribe_unsubscribe_removes_the_coordinator_listener() -> None:
    coordinator = _listening_coordinator()
    hass = _real_task_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    ws_timeline_subscribe(hass, connection, {"id": 7, "entry_id": "e1"})
    await _settle()
    assert coordinator.listener_count() == 1

    connection.subscriptions[7]()
    assert coordinator.listener_count() == 0


def test_timeline_subscribe_rejects_an_unknown_entry_without_subscribing() -> None:
    hass = _fake_hass(None)
    connection = _fake_connection()
    ws_timeline_subscribe(hass, connection, {"id": 8, "entry_id": "bogus"})
    connection.send_error.assert_called_once_with(8, websocket_api.ERR_NOT_FOUND, "Unknown entry")
    assert connection.subscriptions == {}
    connection.send_message.assert_not_called()


# --- kibble/event ----------------------------------------------------------------------------


async def test_ws_event_sends_not_found_for_an_unknown_uid() -> None:
    store = AsyncMock(async_event_detail=AsyncMock(return_value=None))
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_event.__wrapped__(hass, connection, {"id": 9, "entry_id": "e1", "uid": "bogus-uid"})

    connection.send_error.assert_called_once_with(9, ERR_NOT_FOUND, "Unknown event")
    connection.send_result.assert_not_called()


async def test_ws_event_sends_the_stores_detail_shape_unchanged() -> None:
    detail = {"event": {"uid": "e1"}, "samples": [{"uid": "s1"}]}
    store = AsyncMock(async_event_detail=AsyncMock(return_value=detail))
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_event.__wrapped__(hass, connection, {"id": 10, "entry_id": "e1", "uid": "e1"})

    store.async_event_detail.assert_awaited_once_with("e1")
    connection.send_result.assert_called_once_with(10, detail)


# --- kibble/label ------------------------------------------------------------------------------


async def test_ws_label_sends_events_immediately_and_only_refreshes_identity_when_unknown() -> None:
    """`label="unknown"` never touches training -- the response is immediate and the only
    follow-up is the identity-snapshot refresh, never `_async_label_followup`."""
    events = [{"uid": "e1", "identity": "unknown"}]
    store = AsyncMock(async_label_events=AsyncMock(return_value=(events, False)))
    coordinator = _fake_coordinator(store=store)
    create_task, captured = _capturing_async_create_task()
    hass = _fake_hass(_fake_entry(coordinator), async_create_task=create_task)
    connection = _fake_connection()

    await ws_label.__wrapped__(hass, connection, {"id": 11, "entry_id": "e1", "uids": ["e1"], "label": "unknown"})

    store.async_label_events.assert_awaited_once_with(["e1"], "unknown")
    connection.send_result.assert_called_once_with(11, {"events": events})
    assert len(captured) == 1
    await captured[0]
    coordinator.async_refresh_identity_snapshot.assert_awaited_once()


async def test_ws_label_a_cat_label_schedules_the_training_followup_which_reconciles_rebuilds_and_reclassifies() -> None:
    events = [{"uid": "e1", "identity": "reviewed", "cat": "Kitty"}]
    store = AsyncMock(
        async_label_events=AsyncMock(return_value=(events, True)),
        async_reconcile_event_training=AsyncMock(),
    )
    engine = Mock(async_rebuild=AsyncMock(), async_reclassify_unreviewed=AsyncMock())
    coordinator = _fake_coordinator(store=store, engine=engine)
    create_task, captured = _capturing_async_create_task()
    hass = _fake_hass(_fake_entry(coordinator), async_create_task=create_task)
    connection = _fake_connection()

    await ws_label.__wrapped__(hass, connection, {"id": 12, "entry_id": "e1", "uids": ["e1"], "label": "Kitty"})

    connection.send_result.assert_called_once_with(12, {"events": events})
    assert len(captured) == 1
    await captured[0]

    store.async_reconcile_event_training.assert_awaited_once_with(["e1"], "Kitty")
    engine.async_rebuild.assert_awaited_once()
    engine.async_reclassify_unreviewed.assert_awaited_once_with(coordinator.retention_cutoff())
    coordinator.async_refresh_identity_snapshot.assert_awaited_once()


def test_ws_label_schema_rejects_an_empty_uids_list() -> None:
    with pytest.raises(vol.Invalid):
        ws_label._ws_schema({"id": 1, "type": "kibble/label", "entry_id": "e1", "uids": [], "label": "Kitty"})


def test_ws_label_schema_requires_a_label() -> None:
    with pytest.raises(vol.Invalid):
        ws_label._ws_schema({"id": 1, "type": "kibble/label", "entry_id": "e1", "uids": ["e1"]})


def test_ws_label_schema_rejects_sample_uids_now_that_labelling_is_per_photo() -> None:
    with pytest.raises(vol.Invalid):
        ws_label._ws_schema(
            {"id": 1, "type": "kibble/label", "entry_id": "e1", "uids": ["e1"], "label": "Kitty", "sample_uids": ["e1-s1"]}
        )


# --- kibble/sample/label -------------------------------------------------------------------------



async def test_ws_session_label_returns_whole_event_detail_and_reconciles_training() -> None:
    detail = {
        "event": {"uid": "e1", "multiple_cats": True},
        "samples": [{"uid": "e1-s1", "cat": "Kitty"}],
        "scene_subjects": [],
        "cats": [{"name": "Kitty", "ate": True}, {"name": "Pancake", "ate": False}],
    }
    store = AsyncMock(
        async_session_cats=AsyncMock(return_value=detail),
        async_reconcile_session_training=AsyncMock(),
    )
    engine = Mock(async_subject_scores=AsyncMock(), async_rebuild=AsyncMock(), async_reclassify_unreviewed=AsyncMock())
    coordinator = _fake_coordinator(store=store, engine=engine)
    create_task, captured = _capturing_async_create_task()
    hass = _fake_hass(_fake_entry(coordinator), async_create_task=create_task)
    connection = _fake_connection()

    await ws_session_label.__wrapped__(
        hass,
        connection,
        {
            "id": 43,
            "entry_id": "e1",
            "uid": "e1-cat-kitty",
            "cats": [{"cat": "Kitty", "ate": True}, {"cat": "Pancake", "ate": False}],
        },
    )

    store.async_session_cats.assert_awaited_once_with(
        "e1-cat-kitty", [("Kitty", True), ("Pancake", False)]
    )
    connection.send_result.assert_called_once_with(43, detail)
    assert len(captured) == 1
    await captured[0]
    store.async_reconcile_session_training.assert_awaited_once_with("e1")
    engine.async_rebuild.assert_awaited_once()
    engine.async_reclassify_unreviewed.assert_awaited_once_with(coordinator.retention_cutoff())
    coordinator.async_refresh_identity_snapshot.assert_awaited_once()


async def test_ws_session_label_requires_exactly_one_answer_form() -> None:
    coordinator = _fake_coordinator(store=AsyncMock())
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    for msg in (
        {"id": 44, "entry_id": "e1", "uid": "e1"},
        {
            "id": 45,
            "entry_id": "e1",
            "uid": "e1",
            "cats": [{"cat": "Kitty", "ate": True}],
            "verdict": "unknown",
        },
    ):
        await ws_session_label.__wrapped__(hass, connection, msg)

    assert connection.send_error.call_count == 2
    connection.send_error.assert_any_call(44, ERR_AGENT_REJECTED, "Send cats or verdict")
    connection.send_error.assert_any_call(45, ERR_AGENT_REJECTED, "Send cats or verdict")
    coordinator.store.async_session_cats.assert_not_awaited()
    coordinator.store.async_label_events.assert_not_awaited()


async def test_ws_session_subject_uses_event_uid_and_returns_detail_with_followup() -> None:
    detail = {
        "event": {"uid": "e1", "multiple_cats": False},
        "samples": [{"uid": "e1-s1", "cat": "Pancake"}],
        "scene_subjects": [{"sid": 2, "label": "Pancake", "reviewed": True}],
        "cats": [{"name": "Pancake", "ate": True}],
    }
    scores = object()
    store = AsyncMock(
        async_session_subject=AsyncMock(return_value=detail),
        async_reconcile_session_training=AsyncMock(),
    )
    engine = Mock(
        async_subject_scores=AsyncMock(return_value=scores),
        async_rebuild=AsyncMock(),
        async_reclassify_unreviewed=AsyncMock(),
    )
    coordinator = _fake_coordinator(store=store, engine=engine)
    create_task, captured = _capturing_async_create_task()
    hass = _fake_hass(_fake_entry(coordinator), async_create_task=create_task)
    connection = _fake_connection()

    await ws_session_subject.__wrapped__(
        hass,
        connection,
        {"id": 46, "entry_id": "e1", "uid": "e1", "sid": 2, "label": "Pancake"},
    )

    engine.async_subject_scores.assert_awaited_once_with("e1")
    store.async_session_subject.assert_awaited_once_with("e1", 2, "Pancake", scores)
    connection.send_result.assert_called_once_with(46, detail)
    assert len(captured) == 1
    await captured[0]
    store.async_reconcile_session_training.assert_awaited_once_with("e1")
    engine.async_rebuild.assert_awaited_once()
    coordinator.async_refresh_identity_snapshot.assert_awaited_once()


async def test_ws_sample_label_sends_the_saved_sample_and_schedules_the_background_followup() -> None:
    sample = {
        "uid": "e1-s1", "t": 100, "body": None, "face": None, "guess": None,
        "guess_confidence": None, "review": "Kitty", "label": "Kitty",
    }
    scores = object()
    store = AsyncMock(
        async_session_uid_for_sample=AsyncMock(return_value="e1"),
        async_label_sample=AsyncMock(return_value=sample),
        async_reconcile_session_training=AsyncMock(),
    )
    engine = Mock(
        async_subject_scores=AsyncMock(return_value=scores),
        async_rebuild=AsyncMock(),
        async_reclassify_unreviewed=AsyncMock(),
    )
    coordinator = _fake_coordinator(store=store, engine=engine)
    create_task, captured = _capturing_async_create_task()
    hass = _fake_hass(_fake_entry(coordinator), async_create_task=create_task)
    connection = _fake_connection()

    await ws_sample_label.__wrapped__(
        hass, connection, {"id": 40, "entry_id": "e1", "sample_uid": "e1-s1", "label": "Kitty"}
    )

    store.async_session_uid_for_sample.assert_awaited_once_with("e1-s1")
    engine.async_subject_scores.assert_awaited_once_with("e1")
    store.async_label_sample.assert_awaited_once_with("e1-s1", "Kitty", scores)
    connection.send_result.assert_called_once_with(40, {"sample": sample})
    assert len(captured) == 1
    await captured[0]

    store.async_reconcile_session_training.assert_awaited_once_with("e1")
    engine.async_rebuild.assert_awaited_once()
    engine.async_reclassify_unreviewed.assert_awaited_once_with(coordinator.retention_cutoff())
    coordinator.async_refresh_identity_snapshot.assert_awaited_once()


async def test_ws_sample_label_sends_not_found_for_an_unknown_sample() -> None:
    store = AsyncMock(async_session_uid_for_sample=AsyncMock(return_value=None))
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_sample_label.__wrapped__(
        hass, connection, {"id": 41, "entry_id": "e1", "sample_uid": "missing", "label": "Kitty"}
    )

    connection.send_error.assert_called_once_with(41, ERR_NOT_FOUND, "Unknown sample")
    connection.send_result.assert_not_called()


async def test_ws_sample_label_maps_an_unknown_cat_to_agent_rejected() -> None:
    scores = object()
    store = AsyncMock(
        async_session_uid_for_sample=AsyncMock(return_value="e1"),
        async_label_sample=AsyncMock(side_effect=ValueError("unknown cat: Ghost")),
    )
    engine = Mock(async_subject_scores=AsyncMock(return_value=scores))
    coordinator = _fake_coordinator(store=store, engine=engine)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_sample_label.__wrapped__(
        hass, connection, {"id": 42, "entry_id": "e1", "sample_uid": "e1-s1", "label": "Ghost"}
    )

    connection.send_error.assert_called_once_with(42, ERR_AGENT_REJECTED, "unknown cat: Ghost")
    connection.send_result.assert_not_called()


def test_ws_sample_label_schema_requires_a_sample_uid() -> None:
    with pytest.raises(vol.Invalid):
        ws_sample_label._ws_schema({"id": 1, "type": "kibble/sample/label", "entry_id": "e1", "label": "Kitty"})


def test_ws_sample_label_schema_requires_a_label() -> None:
    with pytest.raises(vol.Invalid):
        ws_sample_label._ws_schema({"id": 1, "type": "kibble/sample/label", "entry_id": "e1", "sample_uid": "e1-s1"})


# --- kibble/review -----------------------------------------------------------------------------


async def test_ws_review_forwards_limit_cursor_and_the_retention_cutoff() -> None:
    page = {"items": [{"uid": "e1"}], "total": 1, "cursor": None, "has_more": False}
    store = AsyncMock(async_review_page=AsyncMock(return_value=page))
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_review.__wrapped__(hass, connection, {"id": 13, "entry_id": "e1", "limit": 24})

    store.async_review_page.assert_awaited_once_with(
        limit=24, cursor=None, retention_cutoff=coordinator.retention_cutoff()
    )
    connection.send_result.assert_called_once_with(13, page)


async def test_ws_review_maps_a_malformed_cursor_to_invalid_cursor() -> None:
    store = AsyncMock(async_review_page=AsyncMock(side_effect=ValueError("invalid timeline cursor")))
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_review.__wrapped__(hass, connection, {"id": 14, "entry_id": "e1", "limit": 24, "cursor": "garbage"})

    connection.send_error.assert_called_once_with(14, "invalid_cursor", "invalid timeline cursor")
    connection.send_result.assert_not_called()


@pytest.mark.parametrize("limit", [0, 61])
def test_ws_review_schema_rejects_limit_outside_1_to_60(limit: int) -> None:
    with pytest.raises(vol.Invalid):
        ws_review._ws_schema({"id": 1, "type": "kibble/review", "entry_id": "e1", "limit": limit})


# --- kibble/cats / kibble/cats/add / kibble/cats/delete -----------------------------------------


async def test_ws_cats_assembles_training_accuracy_identity_and_storage_per_cat() -> None:
    store = AsyncMock(
        async_cats=AsyncMock(return_value=[{"name": "Kitty", "color": 0, "created": 1}]),
        async_training_counts=AsyncMock(return_value={"total": 12, "face": 8, "body": 4}),
        async_cat_avatar=AsyncMock(return_value={"id": "training/kitty/a.jpg", "url": "/x"}),
        async_storage_summary=AsyncMock(return_value={"used_bytes": 1, "events": 2, "retention_days": 14, "oldest": 1}),
    )
    engine = Mock(loo_accuracy=Mock(return_value=0.9))
    now = 1_700_000_000
    identity = DeviceIdentitySummary(cats={"Kitty": CatStats(last_seen=now, last_meal=now, recent_meals=(now,), present=True)})
    client = AsyncMock(spool_stats=AsyncMock(return_value=SimpleNamespace(used_bytes=10, cap_bytes=100)))
    coordinator = _fake_coordinator(store=store, engine=engine, identity=identity, client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_cats.__wrapped__(hass, connection, {"id": 15, "entry_id": "e1"})

    payload = connection.send_result.call_args.args[1]
    cat = payload["cats"][0]
    assert cat["name"] == "Kitty"
    assert cat["training"] == {"total": 12, "face": 8, "body": 4}
    assert cat["accuracy"] == 0.9
    assert cat["last_seen"] == now and cat["last_meal"] == now
    assert cat["present"] is True
    assert payload["storage"]["device_spool"] == {"used_bytes": 10, "cap_bytes": 100}


async def test_ws_cats_a_cat_with_no_identity_stats_gets_null_seen_and_zero_meals() -> None:
    store = AsyncMock(
        async_cats=AsyncMock(return_value=[{"name": "Ghost", "color": 1, "created": 1}]),
        async_training_counts=AsyncMock(return_value={"total": 0, "face": 0, "body": 0}),
        async_cat_avatar=AsyncMock(return_value=None),
        async_storage_summary=AsyncMock(return_value={"used_bytes": 0, "events": 0, "retention_days": 14, "oldest": None}),
    )
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_cats.__wrapped__(hass, connection, {"id": 16, "entry_id": "e1"})

    cat = connection.send_result.call_args.args[1]["cats"][0]
    assert cat["last_seen"] is None and cat["last_meal"] is None
    assert cat["meals_today"] == 0
    assert cat["present"] is False


async def test_ws_cats_device_spool_is_null_not_a_failure_when_the_feeder_is_unreachable() -> None:
    store = AsyncMock(
        async_cats=AsyncMock(return_value=[]),
        async_storage_summary=AsyncMock(return_value={"used_bytes": 0, "events": 0, "retention_days": 14, "oldest": None}),
    )
    client = AsyncMock(spool_stats=AsyncMock(side_effect=KibbleConnectionError("down")))
    coordinator = _fake_coordinator(store=store, client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_cats.__wrapped__(hass, connection, {"id": 17, "entry_id": "e1"})

    payload = connection.send_result.call_args.args[1]
    assert payload["cats"] == []
    assert payload["storage"]["device_spool"] is None
    connection.send_error.assert_not_called()


async def test_ws_cats_add_stores_the_cat_then_refreshes_identity_and_sends_empty_result() -> None:
    store = AsyncMock(async_add_cat=AsyncMock())
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_cats_add.__wrapped__(hass, connection, {"id": 18, "entry_id": "e1", "name": "Kitty"})

    store.async_add_cat.assert_awaited_once_with("Kitty")
    coordinator.async_refresh_identity_snapshot.assert_awaited_once()
    connection.send_result.assert_called_once_with(18, {})


async def test_ws_cats_delete_deletes_the_cat_then_refreshes_identity_and_sends_empty_result() -> None:
    store = AsyncMock(async_delete_cat=AsyncMock())
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_cats_delete.__wrapped__(hass, connection, {"id": 19, "entry_id": "e1", "name": "Kitty"})

    store.async_delete_cat.assert_awaited_once_with("Kitty")
    coordinator.async_refresh_identity_snapshot.assert_awaited_once()
    connection.send_result.assert_called_once_with(19, {})


# --- kibble/training / kibble/training/remove ---------------------------------------------------


async def test_ws_training_forwards_cat_limit_and_cursor() -> None:
    page = {"items": [{"uid": "t1"}], "total": 1, "cursor": None, "has_more": False}
    store = AsyncMock(async_training_page=AsyncMock(return_value=page))
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_training.__wrapped__(hass, connection, {"id": 20, "entry_id": "e1", "cat": "Kitty", "limit": 24})

    store.async_training_page.assert_awaited_once_with(cat="Kitty", limit=24, cursor=None)
    connection.send_result.assert_called_once_with(20, page)


async def test_ws_training_maps_a_malformed_cursor_to_invalid_cursor() -> None:
    store = AsyncMock(async_training_page=AsyncMock(side_effect=ValueError("invalid timeline cursor")))
    coordinator = _fake_coordinator(store=store)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_training.__wrapped__(
        hass, connection, {"id": 21, "entry_id": "e1", "cat": "Kitty", "limit": 24, "cursor": "garbage"}
    )

    connection.send_error.assert_called_once_with(21, "invalid_cursor", "invalid timeline cursor")


async def test_ws_training_remove_with_nothing_removed_never_schedules_a_rebuild() -> None:
    store = AsyncMock(async_training_remove=AsyncMock(return_value=0))
    coordinator = _fake_coordinator(store=store)
    create_task, captured = _capturing_async_create_task()
    hass = _fake_hass(_fake_entry(coordinator), async_create_task=create_task)
    connection = _fake_connection()

    await ws_training_remove.__wrapped__(hass, connection, {"id": 22, "entry_id": "e1", "uids": ["t1"]})

    connection.send_result.assert_called_once_with(22, {"removed": 0})
    create_task.assert_not_called()
    assert captured == []


async def test_ws_training_remove_with_a_real_removal_schedules_rebuild_reclassify_and_refresh() -> None:
    store = AsyncMock(async_training_remove=AsyncMock(return_value=2))
    engine = Mock(async_rebuild=AsyncMock(), async_reclassify_unreviewed=AsyncMock())
    coordinator = _fake_coordinator(store=store, engine=engine)
    create_task, captured = _capturing_async_create_task()
    hass = _fake_hass(_fake_entry(coordinator), async_create_task=create_task)
    connection = _fake_connection()

    await ws_training_remove.__wrapped__(hass, connection, {"id": 23, "entry_id": "e1", "uids": ["t1", "t2"]})

    connection.send_result.assert_called_once_with(23, {"removed": 2})
    assert len(captured) == 1
    await captured[0]

    engine.async_rebuild.assert_awaited_once()
    engine.async_reclassify_unreviewed.assert_awaited_once_with(coordinator.retention_cutoff())
    coordinator.async_refresh_identity_snapshot.assert_awaited_once()


# --- kibble/vision/last, kibble/vision/areas(/set), kibble/calibration(/action) -----------------
# Unrelated to the AI pipeline; unchanged contract, kept as regression coverage.


async def test_ws_vision_last_maps_an_old_daemons_404_to_a_null_frame_not_an_error() -> None:
    """`/vision/last` 404s exactly like any other unimplemented route on an old agent
    (`api.py`'s `vision_last`, `not_found_is_missing`) -- a null frame, not `ERR_FEEDER_UNREACHABLE`,
    since there being nothing to draw yet is not a connectivity problem."""
    client = AsyncMock(vision_last=AsyncMock(return_value=None))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_vision_last.__wrapped__(hass, connection, {"id": 24, "entry_id": "e1"})

    connection.send_result.assert_called_once_with(24, {"frame": None})
    connection.send_error.assert_not_called()


def _two_cat_frame() -> dict:
    """A feeder frame with two tracked cats (sids 1 and 2) and one clutter box (no sid)."""
    return {
        "event_id": 1201,
        "detections": [
            {"x1": 0.1, "y1": 0.2, "x2": 0.4, "y2": 0.9, "score": 0.9, "admitted": True, "sid": 1},
            {"x1": 0.5, "y1": 0.2, "x2": 0.8, "y2": 0.9, "score": 0.9, "admitted": True, "sid": 2},
            {"x1": 0.9, "y1": 0.0, "x2": 1.0, "y2": 0.1, "score": 0.8, "admitted": False, "sid": None},
        ],
    }


async def test_ws_vision_last_names_each_tracked_cat_and_leaves_clutter_alone() -> None:
    """docs/42-multi-cat.md: every box with a `sid` gets the name its own timeline row shows
    (`None` while unknown); a box with no `sid` is passed through untouched."""
    client = AsyncMock(vision_last=AsyncMock(return_value=_two_cat_frame()))
    store = AsyncMock(async_vision_cats_for_event=AsyncMock(return_value={1: "Kitty"}))
    coordinator = _fake_coordinator(client=client, store=store)
    connection = _fake_connection()

    await ws_vision_last.__wrapped__(_fake_hass(_fake_entry(coordinator)), connection, {"id": 27, "entry_id": "e1"})

    store.async_vision_cats_for_event.assert_awaited_once_with(1201)
    detections = connection.send_result.call_args.args[1]["frame"]["detections"]
    assert [d.get("cat", "absent") for d in detections] == ["Kitty", None, "absent"]


async def test_ws_vision_last_still_returns_the_boxes_when_the_name_lookup_fails() -> None:
    """Names must never cost the live overlay its boxes: a failing lookup returns the frame
    exactly as the feeder sent it, not an error."""
    client = AsyncMock(vision_last=AsyncMock(return_value=_two_cat_frame()))
    store = AsyncMock(async_vision_cats_for_event=AsyncMock(side_effect=sqlite3.OperationalError("locked")))
    coordinator = _fake_coordinator(client=client, store=store)
    connection = _fake_connection()

    await ws_vision_last.__wrapped__(_fake_hass(_fake_entry(coordinator)), connection, {"id": 28, "entry_id": "e1"})

    connection.send_error.assert_not_called()
    connection.send_result.assert_called_once_with(28, {"frame": _two_cat_frame()})



async def test_ws_vision_areas_reads_typed_exclude() -> None:
    areas = VisionAreas(exclude=[[0.3, 0.4, 0.5, 0.6]])
    client = AsyncMock(vision_areas=AsyncMock(return_value=areas))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_vision_areas.__wrapped__(hass, connection, {"id": 25, "entry_id": "e1"})

    client.vision_areas.assert_awaited_once_with()
    connection.send_result.assert_called_once_with(25, {"exclude": areas.exclude})
    connection.send_error.assert_not_called()


async def test_ws_vision_areas_maps_client_failure_to_feeder_unreachable() -> None:
    client = AsyncMock(vision_areas=AsyncMock(side_effect=KibbleConnectionError("down")))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_vision_areas.__wrapped__(hass, connection, {"id": 26, "entry_id": "e1"})

    connection.send_error.assert_called_once_with(26, ERR_FEEDER_UNREACHABLE, "down")
    connection.send_result.assert_not_called()


async def test_ws_vision_areas_set_forwards_exclude_only() -> None:
    exclude = [[0.3, 0.4, 0.5, 0.6]]
    areas = VisionAreas(exclude=exclude)
    client = AsyncMock(set_vision_areas=AsyncMock(return_value=areas))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_vision_areas_set.__wrapped__(hass, connection, {"id": 27, "entry_id": "e1", "exclude": exclude})

    client.set_vision_areas.assert_awaited_once_with(exclude)
    connection.send_result.assert_called_once_with(27, {"exclude": exclude})
    connection.send_error.assert_not_called()


async def test_ws_vision_areas_set_maps_rejection_to_agent_rejected() -> None:
    client = AsyncMock(set_vision_areas=AsyncMock(side_effect=KibbleError("invalid rectangle")))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_vision_areas_set.__wrapped__(hass, connection, {"id": 28, "entry_id": "e1", "exclude": []})

    connection.send_error.assert_called_once_with(28, ERR_AGENT_REJECTED, "invalid rectangle")
    connection.send_result.assert_not_called()


async def test_ws_vision_bowl_reads_the_daemons_bowl_roi() -> None:
    client = AsyncMock(vision_bowl_roi=AsyncMock(return_value=[0.25, 0.6, 0.55, 1.0]))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_vision_bowl.__wrapped__(hass, connection, {"id": 40, "entry_id": "e1"})

    client.vision_bowl_roi.assert_awaited_once_with()
    connection.send_result.assert_called_once_with(40, {"bowl_roi": [0.25, 0.6, 0.55, 1.0]})
    connection.send_error.assert_not_called()


async def test_ws_vision_bowl_maps_client_failure_to_feeder_unreachable() -> None:
    client = AsyncMock(vision_bowl_roi=AsyncMock(side_effect=KibbleConnectionError("down")))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_vision_bowl.__wrapped__(hass, connection, {"id": 41, "entry_id": "e1"})

    connection.send_error.assert_called_once_with(41, ERR_FEEDER_UNREACHABLE, "down")
    connection.send_result.assert_not_called()


async def test_ws_vision_bowl_set_forwards_the_new_roi() -> None:
    roi = [0.25, 0.6, 0.55, 1.0]
    client = AsyncMock(set_vision_bowl_roi=AsyncMock(return_value=roi))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_vision_bowl_set.__wrapped__(hass, connection, {"id": 42, "entry_id": "e1", "bowl_roi": roi})

    client.set_vision_bowl_roi.assert_awaited_once_with(roi)
    connection.send_result.assert_called_once_with(42, {"bowl_roi": roi})
    connection.send_error.assert_not_called()


async def test_ws_vision_bowl_set_maps_rejection_to_agent_rejected() -> None:
    client = AsyncMock(set_vision_bowl_roi=AsyncMock(side_effect=KibbleError("bad roi")))
    coordinator = _fake_coordinator(client=client)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_vision_bowl_set.__wrapped__(hass, connection, {"id": 43, "entry_id": "e1", "bowl_roi": [0, 0, 1, 1]})

    connection.send_error.assert_called_once_with(43, ERR_AGENT_REJECTED, "bad roi")
    connection.send_result.assert_not_called()


async def test_ws_calibration_action_forwards_optional_fields_only_when_present() -> None:
    action = AsyncMock(return_value={"source": "measured"})
    coordinator = _fake_coordinator(async_calibration_action=action)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_calibration_action.__wrapped__(
        hass, connection, {"id": 29, "entry_id": "e1", "hopper": 0, "action": "point", "portions": 1}
    )

    action.assert_awaited_once_with("point", 0, portions=1)
    connection.send_result.assert_called_once_with(29, {"source": "measured"})


async def test_ws_calibration_action_maps_a_busy_bowl_to_calibration_busy_not_agent_rejected() -> None:
    action = AsyncMock(side_effect=KibbleCalibrationBusyError("an animal is over the bowl"))
    coordinator = _fake_coordinator(async_calibration_action=action)
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_calibration_action.__wrapped__(
        hass, connection, {"id": 30, "entry_id": "e1", "hopper": 0, "action": "point", "portions": 1}
    )

    connection.send_error.assert_called_once_with(30, ERR_CALIBRATION_BUSY, "an animal is over the bowl")
    connection.send_result.assert_not_called()


async def test_ws_calibration_sends_the_coordinators_already_polled_data_unchanged() -> None:
    coordinator = _fake_coordinator()
    coordinator.data.calibration = {"hoppers": [{"source": "measured"}]}
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_calibration.__wrapped__(hass, connection, {"id": 31, "entry_id": "e1"})

    connection.send_result.assert_called_once_with(31, coordinator.data.calibration)


async def test_ws_calibration_falls_back_to_two_null_hoppers_when_never_polled() -> None:
    """`None` (an old agent, or the vendor stack) reports the same shape a fresh LibreFeed
    daemon gives for two never-calibrated hoppers -- not a WS error."""
    coordinator = _fake_coordinator()
    hass = _fake_hass(_fake_entry(coordinator))
    connection = _fake_connection()

    await ws_calibration.__wrapped__(hass, connection, {"id": 32, "entry_id": "e1"})

    connection.send_result.assert_called_once_with(32, {"hoppers": [None, None]})
