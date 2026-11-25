"""Local-push companion for the dashboard cards: WebSocket commands over HA's own
`websocket_api`, distinct from `push.py`'s channel to the agent.

`kibble/timeline`, `kibble/event`, `kibble/review/photos`, `kibble/cats`, `kibble/training` and
`kibble/learning` all read straight off `coordinator.store` (SQLite, `store.py`) or memory -- the
HA-side event journal and identity engine that is now the system of record, per
docs/36-ai-pipeline.md. None of them wait for the feeder. `kibble/cats` is the one exception that
also makes a live, on-demand call (`coordinator.async_device_spool()`, bounded to well under a
second) for `Storage.device_spool`, the same "not worth carrying in every poll cycle" reasoning
`kibble/vision/last` below already uses.

Every labelling command -- `kibble/label`, `kibble/session/label`, `kibble/session/subject`,
`kibble/sample/label`, `kibble/session/ate` and `kibble/training/remove` -- writes what it was told
durably and answers at once; none of them relearns anything on the request. The store notices
that the training gallery changed and `learning.py`'s job (debounced, single flight) rebuilds the
models and rechecks the unreviewed visits in the background; `kibble/learning` and
`kibble/learning/subscribe` report where that stands. `kibble/review/photos` serves the
one-photo review queue and `kibble/sample/label` saves one photo's answer (the visit's identity
is derived from the photos' answers); `kibble/label` and `kibble/session/label` are the older
whole-visit commands, kept as idempotent writes. Each ends by asking the coordinator for a fresh
identity snapshot (`KibbleCoordinator.async_schedule_identity_refresh`) so entities and
subscribers see the result the same way an ingest pass would. `kibble/cats/add` and
`kibble/cats/delete` do the same, awaited.

`kibble/vision/last`, `kibble/vision/areas(/set)`, `kibble/calibration(/action)` are unrelated
to the AI pipeline and unchanged: every command takes `entry_id`; `_resolve_coordinator` is the
one place that turns a bad one into the right WS error instead of four copies of the same
lookup.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .api import KibbleCalibrationBusyError, KibbleConnectionError, KibbleError, KibbleNotFoundError
from .const import DOMAIN
from .coordinator import KibbleCoordinator

_LOGGER = logging.getLogger(__name__)

# `kibble/timeline`'s own cap -- matches docs/36-ai-pipeline.md's "at most 100" verbatim.
MAX_TIMELINE_ITEMS = 100
MAX_REVIEW_ITEMS = 60
MAX_TRAINING_ITEMS = 60

ERR_FEEDER_UNREACHABLE = "feeder_unreachable"
# A named cat/event/sample HA reports it doesn't have -- distinct from "the feeder itself is
# unreachable" so the card can say "that's already gone" instead of a generic connectivity
# banner.
ERR_NOT_FOUND = "not_found"
# Any other write rejected outright (bad label value, empty uids list, ...).
ERR_AGENT_REJECTED = "agent_rejected"
# An animal is over the bowl right now (`KibbleCalibrationBusyError`, `POST /calibration`'s own
# 409 on the `point` action) -- distinct from `agent_rejected` so the calibration wizard can say
# "wait for the bowl to clear" instead of a generic failure.
ERR_CALIBRATION_BUSY = "calibration_busy"


@callback
def _resolve_coordinator(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> KibbleCoordinator | None:
    """Resolves `msg["entry_id"]` to its coordinator, or sends the right WS error and returns
    `None` -- every command below calls this first and returns immediately on `None`."""
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown entry")
        return None
    if entry.state is not ConfigEntryState.LOADED:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Entry not loaded")
        return None
    coordinator = entry.runtime_data
    if coordinator.data is None:
        # LOADED, but the feeder has not answered its first poll yet (`__init__.py`'s
        # `async_setup_entry`): the answer an entry that is still setting up has always got.
        # Commands read the snapshot and `kibble/timeline/subscribe` listens to its updates --
        # a listener added now would also start a polling schedule beside the first poll.
        connection.send_error(
            msg["id"], websocket_api.ERR_NOT_FOUND, "Entry not loaded yet: the feeder has not replied"
        )
        return None
    return coordinator


@callback
def _send_agent_error(
    connection: websocket_api.ActiveConnection, msg_id: int, err: KibbleError
) -> None:
    """Maps a `KibbleError` from a live device call (`kibble/cats`'s spool read, calibration)
    to the right WS error code."""
    if isinstance(err, KibbleConnectionError):
        connection.send_error(msg_id, ERR_FEEDER_UNREACHABLE, str(err))
    elif isinstance(err, KibbleCalibrationBusyError):
        connection.send_error(msg_id, ERR_CALIBRATION_BUSY, str(err))
    else:
        connection.send_error(msg_id, ERR_AGENT_REJECTED, str(err))


# --- kibble/timeline / kibble/timeline/subscribe / kibble/event -------------------------------


def _feed_view(coordinator: KibbleCoordinator, feed: dict[str, Any]) -> dict[str, Any]:
    """One stored feed row's frozen per-hopper facts (`store.py`'s `_feed_timeline_dict`) ->
    the timeline's public shape. `single` falls back to the coordinator's current mode for a
    row recorded before this existed (`single` is `None` in the store); a side's `food` falls
    back to the coordinator's current name for that hopper the same way, when that hopper was
    unnamed at feed time -- naming it later also labels every past feed that never got a name
    of its own, while a row that *did* record one keeps saying exactly that even after a
    rename, which is what "recorded at ingest" (docs/37-hopper-full.md) actually buys.

    In single mode there is one bin, so `sides` is always empty and `portions` is the whole
    serving. Folding both live fallbacks in here, rather than only in the stored row, is what
    makes a divider flip or a food rename change what `kibble/timeline/subscribe` sends, even
    for a row whose own recorded facts never change."""
    single = feed["single"]
    if single is None:
        single = coordinator.single_hopper
    sides: list[dict[str, Any]] = []
    if not single:
        for n, amount, food in (
            (1, feed["amount1"], feed["food1"]),
            (2, feed["amount2"], feed["food2"]),
        ):
            if amount and amount > 0:
                sides.append({"hopper": n, "portions": amount, "food": food or coordinator.hopper_food(n)})
    return {
        "portions": feed["portions"],
        "scheduled": feed["scheduled"],
        "confirmed": feed["confirmed"],
        "single": single,
        "sides": sides,
    }


def _render_feeds(coordinator: KibbleCoordinator, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Renders every timeline item's stored `feed` facts through `_feed_view`; a non-feed item
    (an event row) passes through untouched. Applied to both the page and the subscribe
    snapshot -- see `_feed_view`'s own docstring for why that snapshot must change on a divider
    flip or a food rename."""
    return [
        {**item, "feed": _feed_view(coordinator, item["feed"])} if item.get("feed") else item
        for item in items
    ]


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/timeline",
        vol.Required("entry_id"): str,
        vol.Optional("cursor"): str,
        vol.Optional("limit", default=30): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_TIMELINE_ITEMS)
        ),
    }
)
@websocket_api.async_response
async def ws_timeline(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        page = await coordinator.store.async_timeline_page(
            limit=msg["limit"], cursor=msg.get("cursor")
        )
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_cursor", str(err))
        return
    connection.send_result(msg["id"], {**page, "items": _render_feeds(coordinator, page["items"])})


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/timeline/subscribe", vol.Required("entry_id"): str}
)
@callback
def ws_timeline_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Subscribes to timeline changes. No rows are ever pushed here -- only `{"changed":
    true}`; the client already owns pagination/cursors via `kibble/timeline` and just refetches
    its current page on receipt.

    Fires whenever the coordinator's data updates (an ingest pass, a label, a cats/training
    mutation -- see `KibbleCoordinator.async_refresh_identity_snapshot`), but only actually
    sends when the first page's contents differ from what was last sent, so a coordinator
    update with nothing to do with the timeline (a `bowl_fill` tick) sends nothing. The check
    itself is async (a store read), so the sync coordinator-listener callback schedules it as a
    background task rather than blocking; a check already in flight absorbs any update that
    lands while it runs, since it will see the latest state once it resolves.
    """
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    state: dict[str, Any] = {"snapshot": None, "task": None}

    async def _check_and_send() -> None:
        try:
            page = await coordinator.store.async_timeline_page(limit=MAX_TIMELINE_ITEMS, cursor=None)
        except Exception:  # noqa: BLE001 -- a failed check must never crash the listener
            _LOGGER.exception("kibble/timeline/subscribe check failed")
            return
        snapshot = _render_feeds(coordinator, page["items"])
        if snapshot == state["snapshot"]:
            return
        state["snapshot"] = snapshot
        connection.send_message(websocket_api.event_message(msg["id"], {"changed": True}))

    @callback
    def _on_update() -> None:
        task = state["task"]
        if task is not None and not task.done():
            return
        state["task"] = hass.async_create_task(_check_and_send())

    connection.subscriptions[msg["id"]] = coordinator.async_add_listener(_on_update)
    connection.send_result(msg["id"])
    # The first check is the current rows, so a subscriber never has to also call
    # `kibble/timeline` first just to learn whether it should.
    _on_update()


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/event", vol.Required("entry_id"): str, vol.Required("uid"): str}
)
@websocket_api.async_response
async def ws_event(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    detail = await coordinator.store.async_event_detail(msg["uid"])
    if detail is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown event")
        return
    connection.send_result(msg["id"], detail)


# --- kibble/label and whole-session review ------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/label",
        vol.Required("entry_id"): str,
        vol.Required("uids"): vol.All([str], vol.Length(min=1)),
        vol.Required("label"): str,
    }
)
@websocket_api.async_response
async def ws_label(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Applies a timeline quick-pick to each whole visit: saved and answered at once, learned
    from later by the learning job (`learning.py`), which the store wakes by itself. Idempotent --
    a retry after a lost response changes nothing."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        events = await coordinator.store.async_label_events(msg["uids"], msg["label"])
    except ValueError as err:
        connection.send_error(msg["id"], ERR_AGENT_REJECTED, str(err))
        return
    connection.send_result(msg["id"], {"events": events})
    coordinator.async_schedule_identity_refresh()


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/session/label",
        vol.Required("entry_id"): str,
        vol.Required("uid"): str,
        vol.Optional("cats"): vol.All(
            [{vol.Required("cat"): str, vol.Required("ate"): bool}], vol.Length(min=1)
        ),
        vol.Optional("verdict"): vol.In(("not_a_cat", "unknown")),
    }
)
@websocket_api.async_response
async def ws_session_label(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    if ("cats" in msg) == ("verdict" in msg):
        connection.send_error(msg["id"], ERR_AGENT_REJECTED, "Send cats or verdict")
        return
    if "verdict" in msg:
        try:
            events = await coordinator.store.async_label_events([msg["uid"]], msg["verdict"])
        except ValueError as err:
            connection.send_error(msg["id"], ERR_AGENT_REJECTED, str(err))
            return
        if not events:
            connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown session")
            return
        detail = await coordinator.store.async_event_detail(events[0]["uid"])
        if detail is None:
            connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown session")
            return
        connection.send_result(msg["id"], detail)
        coordinator.async_schedule_identity_refresh()
        return
    cats = [(item["cat"], item["ate"]) for item in msg["cats"]]
    try:
        detail = await coordinator.store.async_session_cats(msg["uid"], cats)
    except ValueError as err:
        connection.send_error(msg["id"], ERR_AGENT_REJECTED, str(err))
        return
    if detail is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown session")
        return
    connection.send_result(msg["id"], detail)
    coordinator.async_schedule_identity_refresh()


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/session/subject",
        vol.Required("entry_id"): str,
        vol.Required("uid"): str,
        vol.Required("sid"): int,
        vol.Required("label"): str,
    }
)
@websocket_api.async_response
async def ws_session_subject(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        scores = await coordinator.engine.async_subject_scores(msg["uid"])
        detail = await coordinator.store.async_session_subject(
            msg["uid"], msg["sid"], msg["label"], scores
        )
    except ValueError as err:
        connection.send_error(msg["id"], ERR_AGENT_REJECTED, str(err))
        return
    if detail is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown session")
        return
    connection.send_result(msg["id"], detail)
    coordinator.async_schedule_identity_refresh()


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/session/ate",
        vol.Required("entry_id"): str,
        vol.Required("uid"): str,
        vol.Required("ate"): [str],
    }
)
@websocket_api.async_response
async def ws_session_ate(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Who ate, for a visit that names two cats and a meal the feeder could not split
    (`event.needs_ate`). Result `{"visit": {uid, kind, cats: [{cat, ate}], needs_ate}}`.
    `agent_rejected` for an empty list or a cat the visit does not name, `not_found` for an
    unknown visit. Idempotent, and never trains anything."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        result = await coordinator.store.async_session_ate(msg["uid"], msg["ate"])
    except ValueError as err:
        connection.send_error(msg["id"], ERR_AGENT_REJECTED, str(err))
        return
    if result is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown session")
        return
    connection.send_result(msg["id"], result)
    coordinator.async_schedule_identity_refresh()


# --- kibble/sample/label -------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/sample/label",
        vol.Required("entry_id"): str,
        vol.Required("sample_uid"): str,
        vol.Required("label"): str,
    }
)
@websocket_api.async_response
async def ws_sample_label(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Saves one photo's answer -- a cat's name, `both` (two cats in this one crop; only with
    exactly two enrolled), `not_a_cat`, `skip` ("Don't use") or `follow` (undo) -- and answers
    `{"sample", "visit": {uid, kind, cats: [{cat, ate}], needs_ate}}`: the photo as it now stands
    and what the visit's identity became because of it. Idempotent: the same answer twice is a
    success that changes nothing. The model relearns later, in the learning job."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    session_uid = await coordinator.store.async_session_uid_for_sample(msg["sample_uid"])
    if session_uid is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown sample")
        return
    try:
        scores = await coordinator.engine.async_subject_scores(session_uid)
        result = await coordinator.store.async_label_sample(msg["sample_uid"], msg["label"], scores)
    except ValueError as err:
        connection.send_error(msg["id"], ERR_AGENT_REJECTED, str(err))
        return
    if result is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown sample")
        return
    connection.send_result(msg["id"], result)
    coordinator.async_schedule_identity_refresh()


# --- kibble/review/photos, kibble/learning, kibble/learning/subscribe ---------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/review/photos",
        vol.Required("entry_id"): str,
        vol.Optional("cursor"): str,
        vol.Optional("limit", default=24): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_REVIEW_ITEMS)
        ),
    }
)
@websocket_api.async_response
async def ws_review_photos(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """One page of the one-photo review queue: `{items: [{sample, visit}], remaining, visits,
    cursor, has_more}` -- photos nobody has answered yet, a few per visit, the least certain
    first. Answered photos leave the queue, so the cursor stays valid while the card labels.
    Reads the store only, never a relearn: it answers at the same speed whatever the learning
    job is doing. A cursor this command did not issue is `agent_rejected`."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        page = await coordinator.store.async_review_photos_page(
            limit=msg["limit"], cursor=msg.get("cursor"), retention_cutoff=coordinator.retention_cutoff()
        )
    except ValueError as err:
        connection.send_error(msg["id"], ERR_AGENT_REJECTED, str(err))
        return
    connection.send_result(msg["id"], page)


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/learning", vol.Required("entry_id"): str}
)
@callback
def ws_learning(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Where the learning job stands (`learning.LearningManager.status`), read from memory."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    if coordinator.learning is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Learning is not running")
        return
    connection.send_result(msg["id"], coordinator.learning.status())


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/learning/subscribe", vol.Required("entry_id"): str}
)
@callback
def ws_learning_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Pushes `{"learning": status}` on every change of the learning job's status: once right
    after the result (so a subscriber never has to also ask first), then whenever it changes --
    progress ticks are throttled to about two a second."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    learning = coordinator.learning
    if learning is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Learning is not running")
        return

    @callback
    def _send(status: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"learning": status}))

    connection.subscriptions[msg["id"]] = learning.subscribe(_send)
    connection.send_result(msg["id"])
    _send(learning.status())


# --- kibble/cats / kibble/cats/add / kibble/cats/delete ----------------------------------------


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/cats", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_cats(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    identity = coordinator.data.identity
    overview = await coordinator.store.async_cats_overview(coordinator.retention_days())
    cats: list[dict[str, Any]] = []
    for row in overview["cats"]:
        stats = identity.cats.get(row["name"])
        cats.append(
            {
                **row,
                "accuracy": coordinator.engine.loo_accuracy(row["name"]),
                "last_seen": stats.last_seen if stats else None,
                "last_meal": stats.last_meal if stats else None,
                "meals_today": _meals_today(stats),
                "present": stats.present if stats else False,
            }
        )
    storage = overview["storage"]
    storage["device_spool"] = await coordinator.async_device_spool()
    connection.send_result(msg["id"], {"cats": cats, "storage": storage})


def _meals_today(stats: Any) -> int:
    if stats is None:
        return 0
    start = dt_util.start_of_local_day()
    return sum(1 for ts in stats.recent_meals if dt_util.utc_from_timestamp(ts) >= start)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/cats/add",
        vol.Required("entry_id"): str,
        vol.Required("name"): str,
    }
)
@websocket_api.async_response
async def ws_cats_add(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    await coordinator.store.async_add_cat(msg["name"])
    await coordinator.async_refresh_identity_snapshot()
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/cats/delete",
        vol.Required("entry_id"): str,
        vol.Required("name"): str,
    }
)
@websocket_api.async_response
async def ws_cats_delete(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    await coordinator.store.async_delete_cat(msg["name"])
    await coordinator.async_refresh_identity_snapshot()
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/cats/avatar/set",
        vol.Required("entry_id"): str,
        vol.Required("cat"): str,
        vol.Required("asset_id"): str,
    }
)
@websocket_api.async_response
async def ws_cats_avatar_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """"Choose one of this cat's existing photos" as its avatar -- copies `asset_id` (a
    training or archived-media asset already belonging to this entry) into a dedicated
    `avatars/` file (`store.set_cat_avatar_from_asset`), so it survives that source asset
    later being evicted or purged. `ERR_NOT_FOUND` for an unknown cat or an `asset_id` that
    doesn't resolve to a real file under this entry."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    avatar = await coordinator.store.async_set_cat_avatar_from_asset(msg["cat"], msg["asset_id"])
    if avatar is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown cat or photo")
        return
    connection.send_result(msg["id"], {"avatar": avatar})
    hass.async_create_task(coordinator.async_refresh_identity_snapshot())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/cats/avatar/clear",
        vol.Required("entry_id"): str,
        vol.Required("cat"): str,
    }
)
@websocket_api.async_response
async def ws_cats_avatar_clear(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Clears a custom avatar override, falling back to the newest trained photo
    (`store._avatar_state`'s auto-pick) -- or to the monogram, for a cat with neither."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    cleared = await coordinator.store.async_clear_cat_avatar(msg["cat"])
    if not cleared:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown cat")
        return
    avatar_info = await coordinator.store.async_avatar_info(msg["cat"])
    connection.send_result(msg["id"], {"avatar": avatar_info["avatar"]})
    hass.async_create_task(coordinator.async_refresh_identity_snapshot())


# --- kibble/training / kibble/training/remove --------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/training",
        vol.Required("entry_id"): str,
        vol.Required("cat"): str,
        vol.Optional("cursor"): str,
        vol.Optional("limit", default=24): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_TRAINING_ITEMS)
        ),
    }
)
@websocket_api.async_response
async def ws_training(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        page = await coordinator.store.async_training_page(
            cat=msg["cat"], limit=msg["limit"], cursor=msg.get("cursor")
        )
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_cursor", str(err))
        return
    connection.send_result(msg["id"], page)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/training/remove",
        vol.Required("entry_id"): str,
        vol.Required("uids"): [str],
    }
)
@websocket_api.async_response
async def ws_training_remove(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    removed = await coordinator.store.async_training_remove(msg["uids"])
    connection.send_result(msg["id"], {"removed": removed})
    if removed:
        coordinator.async_schedule_identity_refresh()


# --- kibble/vision/last, kibble/vision/areas(/set), kibble/vision/bowl(/set),
# kibble/calibration(/action) -------------------------------------------------------------------
# Unrelated to the AI pipeline; unchanged except that `vision/areas` dropped its unused
# "include" side (see `kibble-detection-areas-dialog.ts`'s header) and gained a `vision/bowl`
# sibling for the daemon's separate, previously card-invisible `bowl_roi`.


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/vision/last", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_vision_last(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """`GET /vision/last` straight from the agent, on demand: an open card polls this roughly
    once a second to keep its live detection-box overlay in step with the video, far tighter
    than `DEFAULT_SCAN_INTERVAL`, and caching a fetch this frequent in `KibbleData` would mean
    either slowing every other entity's refresh to match or serving the overlay stale between
    polls.

    `KibbleNotFoundError` -- an agent old enough to predate this brand-new route -- folds into
    the same `{"frame": None}` reply as the agent's own "nothing analysed yet" `null`: the card
    has nothing to draw either way, so this is not `ERR_FEEDER_UNREACHABLE` like a real
    connection failure below.

    Each detection with a real (non-null) `sid` gets a `cat` field (docs/42-multi-cat.md):
    one cheap indexed store lookup (`vision_cats_for_event`, off the event loop) keyed by the
    frame's own `event_id`, reused across every detection in this one frame -- never a second
    query per detection. A detection with no `sid` (clutter, tentative, no open track) is left
    exactly as the agent sent it: no `cat` key added at all, matching "missing event_id/sid ->
    leave cat absent"."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        frame = await coordinator.client.vision_last()
    except KibbleNotFoundError:
        frame = None
    except KibbleError as err:
        connection.send_error(msg["id"], ERR_FEEDER_UNREACHABLE, str(err))
        return
    if isinstance(frame, dict) and isinstance(frame.get("event_id"), int):
        detections = frame.get("detections")
        if isinstance(detections, list):
            # Names are a nicety on top of the boxes: a failed lookup must never cost the card
            # its whole overlay, so it degrades to the frame exactly as the feeder sent it.
            try:
                cats_by_sid = await coordinator.store.async_vision_cats_for_event(frame["event_id"])
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Live cat names unavailable for event %s", frame["event_id"], exc_info=True)
                cats_by_sid = None
            if cats_by_sid is not None:
                for detection in detections:
                    if isinstance(detection, dict) and isinstance(detection.get("sid"), int):
                        detection["cat"] = cats_by_sid.get(detection["sid"])
    connection.send_result(msg["id"], {"frame": frame})


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/vision/areas", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_vision_areas(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Read the daemon's normalized ignore-mask detection rectangles on demand."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        areas = await coordinator.client.vision_areas()
    except KibbleError as err:
        connection.send_error(msg["id"], ERR_FEEDER_UNREACHABLE, str(err))
        return
    connection.send_result(msg["id"], {"exclude": areas.exclude})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/vision/areas/set",
        vol.Required("entry_id"): str,
        vol.Required("exclude"): [[vol.Coerce(float)]],
    }
)
@websocket_api.async_response
async def ws_vision_areas_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Replace the daemon's ignore-mask set with one feeder request."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        areas = await coordinator.client.set_vision_areas(msg["exclude"])
    except KibbleError as err:
        _send_agent_error(connection, msg["id"], err)
        return
    connection.send_result(msg["id"], {"exclude": areas.exclude})


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/vision/bowl", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_vision_bowl(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Read the daemon's bowl zone (`bowl_roi`) on demand -- separate from `vision/areas`
    because the daemon itself keeps it on a different route (`GET /vision`, not
    `GET /vision/areas`); see `KibbleClient.vision_bowl_roi`'s own docstring."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        bowl_roi = await coordinator.client.vision_bowl_roi()
    except KibbleError as err:
        connection.send_error(msg["id"], ERR_FEEDER_UNREACHABLE, str(err))
        return
    connection.send_result(msg["id"], {"bowl_roi": bowl_roi})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/vision/bowl/set",
        vol.Required("entry_id"): str,
        vol.Required("bowl_roi"): [vol.Coerce(float)],
    }
)
@websocket_api.async_response
async def ws_vision_bowl_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Replace the daemon's bowl zone with one feeder request."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    try:
        bowl_roi = await coordinator.client.set_vision_bowl_roi(msg["bowl_roi"])
    except KibbleError as err:
        _send_agent_error(connection, msg["id"], err)
        return
    connection.send_result(msg["id"], {"bowl_roi": bowl_roi})


@websocket_api.websocket_command(
    {vol.Required("type"): "kibble/calibration", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_calibration(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """`GET /calibration` straight off the coordinator's already-polled `KibbleData.
    calibration` -- small, and changed only by the wizard's own actions rather than on the
    device's own clock, so this reads the poll cache rather than fetching on demand.

    `None` (an agent old enough to predate this route, or the vendor stack) reports as the same
    `{"hoppers": [null, null]}` shape a fresh LibreFeed daemon gives for two hoppers it has
    never calibrated -- the wizard has nothing to draw either way, so this is not a WS error
    like a real connection failure would be."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    calibration = coordinator.data.calibration
    connection.send_result(
        msg["id"], calibration if calibration is not None else {"hoppers": [None, None]}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kibble/calibration/action",
        vol.Required("entry_id"): str,
        vol.Required("action"): str,
        vol.Required("hopper"): int,
        vol.Optional("portions"): int,
        vol.Optional("from"): int,
        vol.Optional("note"): str,
    }
)
@websocket_api.async_response
async def ws_calibration_action(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """One calibration-wizard step (`action` is `begin`/`point`/`full`/`inherit`/`clear`),
    forwarded to `POST /calibration` via `KibbleCoordinator.async_calibration_action` (which
    refreshes immediately on success, so the follow-up `kibble/calibration` read every wizard
    step makes never sees a stale curve -- see that method's own docstring). `portions`/`from`/
    `note` are forwarded exactly when `msg` carries them; the daemon itself validates which
    fields a given `action` needs and 400s for a wrong combination, the same trust-the-agent
    shape `set_led`/`set_desiccant` already use rather than re-validating here.

    `KibbleCalibrationBusyError` (409, an animal is over the bowl right now) maps to its own
    `calibration_busy` WS error via `_send_agent_error`, distinct from a generic
    `agent_rejected` so the wizard can tell "wait and retry" from "that step was wrong".

    Never dispenses anything: the operator's own feed control/app already put whatever is in
    the bowl there before calling this -- see `api.py`'s `calibration_action` docstring."""
    coordinator = _resolve_coordinator(hass, connection, msg)
    if coordinator is None:
        return
    fields = {key: msg[key] for key in ("portions", "from", "note") if key in msg}
    try:
        result = await coordinator.async_calibration_action(msg["action"], msg["hopper"], **fields)
    except KibbleError as err:
        _send_agent_error(connection, msg["id"], err)
        return
    connection.send_result(msg["id"], result)


@callback
def async_setup_websocket_api(hass: HomeAssistant) -> None:
    """Registers every `kibble/*` websocket command. Called once from `__init__.py`'s
    component-level `async_setup` -- commands are process-global, registering them per config
    entry would try to register the same command more than once."""
    websocket_api.async_register_command(hass, ws_timeline)
    websocket_api.async_register_command(hass, ws_timeline_subscribe)
    websocket_api.async_register_command(hass, ws_event)
    websocket_api.async_register_command(hass, ws_label)
    websocket_api.async_register_command(hass, ws_session_label)
    websocket_api.async_register_command(hass, ws_session_subject)
    websocket_api.async_register_command(hass, ws_session_ate)
    websocket_api.async_register_command(hass, ws_sample_label)
    websocket_api.async_register_command(hass, ws_review_photos)
    websocket_api.async_register_command(hass, ws_learning)
    websocket_api.async_register_command(hass, ws_learning_subscribe)
    websocket_api.async_register_command(hass, ws_cats)
    websocket_api.async_register_command(hass, ws_cats_add)
    websocket_api.async_register_command(hass, ws_cats_delete)
    websocket_api.async_register_command(hass, ws_cats_avatar_set)
    websocket_api.async_register_command(hass, ws_cats_avatar_clear)
    websocket_api.async_register_command(hass, ws_training)
    websocket_api.async_register_command(hass, ws_training_remove)
    websocket_api.async_register_command(hass, ws_vision_areas)
    websocket_api.async_register_command(hass, ws_vision_areas_set)
    websocket_api.async_register_command(hass, ws_vision_bowl)
    websocket_api.async_register_command(hass, ws_vision_bowl_set)
    websocket_api.async_register_command(hass, ws_vision_last)
    websocket_api.async_register_command(hass, ws_calibration)
    websocket_api.async_register_command(hass, ws_calibration_action)
