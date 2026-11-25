"""`learning.py`: the one background job that brings the recognition models up to date after
people label things (docs/36-ai-pipeline.md, "Learning").

What is pinned here is what Nitin would notice, not the plumbing:

- Labelling never waits for learning. Twenty quick answers cost ONE pass; an answer saved while a
  pass runs is acknowledged at once and earns exactly one follow-up; saying the same thing twice
  changes nothing at all.
- The wait is a quiet moment after the LAST answer, but a steady trickle cannot postpone learning
  past `max_wait_s`.
- The work is owed durably: a restart in the middle of a pass (or before one started) runs it
  again, a restart with nothing owed stays idle, and unloading cancels a running pass without
  marking anything done.
- A failing pass (anything raising, or CoralHub not answering) ends in `retrying` with a doubling
  wait that a success resets, and a new answer does not have to wait that wait out.
- `status()` and the progress events: the frozen payload, throttled progress ticks, one failing
  subscriber not starving the others.

Everything runs against a REAL `KibbleStore` (real SQLite on its own store thread); only the
identity engine is a fake that counts rebuilds and can be made to stall, fail or report CoralHub
down. Timings are shrunk to tenths of a second.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kibble import coral_identity
from kibble.learning import (
    PROGRESS_PUSH_S,
    QUIET_S,
    STATE_IDLE,
    STATE_LEARNING,
    STATE_RETRYING,
    STATE_WAITING,
    STATES,
    LearningManager,
)
from kibble.store import KibbleStore

ENTRY = "entry1"
START = 2_000_000

_INSERT_EVENT = (
    "INSERT INTO events(uid, device_event_id, kind, start, end, open, reviewed, updated, hidden, from_photos) "
    "VALUES (?, 1, 'visit', ?, ?, 0, 0, ?, 0, 0)"
)
_INSERT_PHOTO = (
    "INSERT INTO samples(uid, event_uid, t, body, guess, guess_confidence, score) "
    "VALUES (?, ?, ?, 'b.jpg', NULL, NULL, 0.9)"
)

STATUS_KEYS = {"state", "pending", "done", "total", "starts_at", "retry_at", "started", "last", "error"}
RESULT_KEYS = {"finished", "duration", "answers", "learned", "renamed", "rechecked", "stages"}


class FakeEngine:
    """What `LearningManager` asks of `ingest.IdentityEngine`: `built_gen`, `timings`,
    `embed_stats`, `async_rebuild(gen=)` (a reason string when CoralHub did not answer) and
    `async_reclassify_unreviewed(cutoff, progress=)`."""

    def __init__(self) -> None:
        self.built_gen: int | None = None
        self.timings: dict[str, int] = {}
        self.embed_stats: coral_identity.EmbedStats | None = None
        self.rebuilds: list[int | None] = []
        self.rebuild_times: list[float] = []
        self.rechecks: list[int] = []
        self.error: str | None = None  # CoralHub's reason, returned by a rebuild
        self.raises: Exception | None = None  # raised by a rebuild
        self.gate: asyncio.Event | None = None  # while set up, a rebuild waits for it
        self.recheck_gate: asyncio.Event | None = None  # ... and so does a recheck
        self.visits = 4  # what a recheck reports: this many visits, one progress tick each

    async def async_rebuild(self, gen: int | None = None) -> str | None:
        self.rebuilds.append(gen)
        self.rebuild_times.append(time.monotonic())
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        if self.embed_stats is not None:
            self.embed_stats.calls += 2
        self.timings = {"histogram_ms": 3}
        if self.error is None:
            self.built_gen = gen
        return self.error

    async def async_reclassify_unreviewed(
        self, cutoff: int, progress: Callable[[int, int], None] | None = None
    ) -> dict[str, int]:
        self.rechecks.append(cutoff)
        if progress is not None:
            progress(0, self.visits)
        if self.recheck_gate is not None:
            await self.recheck_gate.wait()
        for done in range(1, self.visits + 1):
            await asyncio.sleep(0)
            if progress is not None:
                progress(done, self.visits)
        return {"rechecked": self.visits, "renamed": 1, "failed": 0}


class Rig:
    """A real store on its own thread, a fake engine and one manager; every status the manager
    pushes is kept in `events` as `(wall time, status)`."""

    def __init__(self, tmp_path: Path, **timing: float) -> None:
        loop = asyncio.get_running_loop()
        self.hass = SimpleNamespace(
            loop=loop,
            config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
            async_create_background_task=lambda coro, name: loop.create_task(coro, name=name),
        )
        self.timing: dict[str, float] = {
            "quiet_s": 0.15,
            "max_wait_s": 1.0,
            "retry_first_s": 0.2,
            "retry_max_s": 0.8,
            "progress_push_s": 0.0,
        } | timing
        self.engine = FakeEngine()
        self.refresh = AsyncMock()
        self.store: KibbleStore | None = None
        self.manager: LearningManager | None = None
        self.events: list[tuple[float, dict[str, Any]]] = []

    async def open(self, *, fresh_engine: bool = False) -> None:
        """Opens the store and builds a manager on it -- also what a restart does."""
        if fresh_engine:
            self.engine = FakeEngine()
        self.events = []
        self.store = KibbleStore(self.hass, ENTRY)
        await self.store.async_setup()
        self.manager = LearningManager(
            self.hass,
            ENTRY,
            self.store,
            self.engine,
            retention_cutoff=lambda: 0,
            refresh_snapshot=self.refresh,
            **self.timing,
        )
        self.store.set_learning_listener(self.manager.notify)
        self.manager.subscribe(lambda status: self.events.append((time.time(), status)))

    async def start(self) -> None:
        await self.manager.async_start()

    async def close(self) -> None:
        if self.manager is not None:
            await self.manager.async_stop()
        if self.store is not None:
            await self.store.async_close()
            # `async_close` leaves the worker thread to wind down on its own; joining it here keeps
            # a test from ending while it is still alive (the cleanup check fails the test then).
            self.store._executor.shutdown(wait=True)
        self.manager = self.store = None

    async def seed(self, visits: int, photos: int) -> list[str]:
        """Two enrolled cats and `visits` unreviewed visits of `photos` photos each; returns the
        photo uids, visit by visit."""
        await self.store.async_add_cat("Kitty")
        await self.store.async_add_cat("Pancake")
        uids = [f"v{v}-{p}" for v in range(visits) for p in range(photos)]

        def insert() -> None:
            conn = self.store._sync.conn
            for v in range(visits):
                start = START + v * 100
                conn.execute(_INSERT_EVENT, (f"v{v}", start, start + 30, start))
                for p in range(photos):
                    conn.execute(_INSERT_PHOTO, (f"v{v}-{p}", f"v{v}", start + p))
            conn.commit()

        await self.store._run(insert)
        return uids

    async def label(self, uid: str, cat: str = "Kitty") -> dict[str, Any] | None:
        return await self.store.async_label_sample(uid, cat)

    def state(self) -> str:
        return self.manager.status()["state"]

    async def until(self, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.005)

    async def until_state(self, state: str, timeout: float = 5.0) -> None:
        await self.until(lambda: self.state() == state, timeout)

    async def until_stuck(self, timeout: float = 5.0) -> None:
        """A pass is inside the engine's (gated) rebuild -- past reading the store's state, so
        answers saved from here on belong to the NEXT pass. Waiting for the `learning` state
        alone is racy: it flips before the pass has read the state it covers."""
        await self.until(lambda: len(self.engine.rebuilds) >= 1 and self.state() == STATE_LEARNING, timeout)

    async def until_idle(self, passes: int = 1, timeout: float = 5.0) -> None:
        """The manager has run `passes` passes and settled."""
        await self.until(lambda: self.manager.passes >= passes and self.state() == STATE_IDLE, timeout)

    def states(self) -> list[str]:
        """The states pushed so far, consecutive repeats collapsed."""
        out: list[str] = []
        for _, status in self.events:
            if not out or out[-1] != status["state"]:
                out.append(status["state"])
        return out


@pytest.fixture
async def make_rig(tmp_path: Path):
    rigs: list[Rig] = []

    async def make(**timing: float) -> Rig:
        rig = Rig(tmp_path, **timing)
        rigs.append(rig)
        await rig.open()
        return rig

    yield make
    for rig in rigs:
        await rig.close()


# --- the payload ---------------------------------------------------------------------------------


async def test_a_manager_that_has_not_started_is_idle_and_empty(make_rig) -> None:
    rig = await make_rig()
    assert rig.manager.status() == {
        "state": "idle", "pending": 0, "done": None, "total": None, "starts_at": None,
        "retry_at": None, "started": None, "last": None, "error": None,
    }  # fmt: skip
    assert set(rig.manager.status()) == STATUS_KEYS
    assert STATES == ("idle", "waiting", "learning", "rechecking", "retrying")


def test_the_default_timings_are_a_quiet_twenty_seconds_and_a_two_minute_cap() -> None:
    assert QUIET_S == 20.0
    assert PROGRESS_PUSH_S == 0.5


async def test_a_finished_pass_records_what_it_did_and_how_long_each_stage_took(make_rig) -> None:
    rig = await make_rig()
    uids = await rig.seed(1, 3)
    await rig.start()
    for uid in uids:
        await rig.label(uid)
    await rig.until_idle()

    status = rig.manager.status()
    last = status["last"]
    assert set(last) == RESULT_KEYS
    assert last["answers"] == 3
    assert last["rechecked"] == rig.engine.visits and last["renamed"] == 1
    assert last["learned"] == {"Kitty": (await rig.store.async_training_counts_by_cat())["Kitty"]}
    assert last["learned"]["Kitty"] >= 1
    assert {"reconcile_ms", "caps_ms", "state_ms", "rebuild_ms", "recheck_ms", "snapshot_ms", "histogram_ms"} <= set(
        last["stages"]
    )
    assert abs(last["finished"] - time.time()) < 10
    assert status["pending"] == 0 and status["error"] is None
    assert status["started"] is None and status["starts_at"] is None and status["retry_at"] is None
    rig.refresh.assert_awaited_once()
    assert (await rig.store.async_learning_state())["last_result"] == last  # durable, not just in memory


async def test_coral_request_totals_are_reported_per_pass(make_rig) -> None:
    rig = await make_rig()
    rig.engine.embed_stats = coral_identity.EmbedStats()
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_idle()
    assert rig.manager.status()["last"]["stages"]["coral_requests"] == {"calls": 2, "crops": 0, "ms": 0, "reused": 0}


# --- answers never wait for learning -------------------------------------------------------------


async def test_twenty_quick_answers_cost_one_pass(make_rig) -> None:
    rig = await make_rig(quiet_s=0.4, max_wait_s=5.0)
    uids = await rig.seed(7, 3)
    await rig.start()
    for uid in uids[:20]:
        await rig.label(uid)

    waiting = rig.manager.status()
    assert waiting["state"] == "waiting" and waiting["pending"] == 20
    assert waiting["starts_at"] is not None and waiting["starts_at"] > time.time()
    assert waiting["retry_at"] is None and waiting["done"] is None

    await rig.until_idle()
    assert rig.manager.passes == 1
    assert len(rig.engine.rebuilds) == 1
    status = rig.manager.status()
    assert status["pending"] == 0 and status["last"]["answers"] == 20


async def test_an_answer_is_saved_at_once_even_while_a_pass_is_stuck(make_rig) -> None:
    """The original failure: every label queued a full relearn on the store's one thread, so the
    next labels timed out. Now a stuck pass costs the labels nothing, and the five answers saved
    meanwhile earn ONE follow-up pass, not five."""
    rig = await make_rig()
    rig.engine.gate = asyncio.Event()
    uids = await rig.seed(2, 3)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_stuck()  # the pass is now stuck inside the engine's rebuild

    started = time.monotonic()
    for uid in uids[1:6]:
        await rig.label(uid)
    assert time.monotonic() - started < 2.0
    assert rig.state() == STATE_LEARNING  # a running pass is not rescheduled ...
    assert rig.manager.status()["pending"] == 6  # ... but every answer is counted

    rig.engine.gate.set()
    await rig.until_idle(passes=2)
    assert rig.manager.passes == 2
    assert rig.manager.status()["last"]["answers"] == 5  # the follow-up covered exactly the five
    assert rig.manager.status()["pending"] == 0
    assert rig.states().count("waiting") >= 2  # waiting for pass one, then again for the follow-up


async def test_the_store_stays_readable_while_a_pass_is_in_the_middle_of_its_work(make_rig) -> None:
    rig = await make_rig()
    rig.engine.gate = asyncio.Event()
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_stuck()

    started = time.monotonic()
    overview = await rig.store.async_cats_overview(14)
    state = await rig.store.async_learning_state()
    assert time.monotonic() - started < 0.5
    assert {cat["name"] for cat in overview["cats"]} == {"Kitty", "Pancake"}
    assert state["gen"] > state["done_gen"]  # and the answer is already durable


async def test_saying_the_same_thing_twice_changes_nothing(make_rig) -> None:
    rig = await make_rig()
    uids = await rig.seed(1, 2)
    await rig.start()
    first = await rig.label(uids[0])
    await rig.until_idle()
    before = await rig.store.async_learning_state()

    again = await rig.label(uids[0])  # a retry of the very same answer

    after = await rig.store.async_learning_state()
    assert again["sample"]["review"] == first["sample"]["review"] == "Kitty"
    assert (after["gen"], after["answers"]) == (before["gen"], before["answers"])
    await asyncio.sleep(rig.timing["quiet_s"] * 3)
    assert rig.manager.passes == 1 and rig.state() == STATE_IDLE and len(rig.engine.rebuilds) == 1


async def test_a_notification_for_a_generation_that_is_already_learned_is_ignored(make_rig) -> None:
    rig = await make_rig()
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_idle()
    state = await rig.store.async_learning_state()
    pushed = len(rig.events)

    rig.manager.notify(state["done_gen"], state["answers"])

    assert rig.state() == STATE_IDLE
    await asyncio.sleep(rig.timing["quiet_s"] * 3)
    assert rig.manager.passes == 1
    assert [status["state"] for _, status in rig.events[pushed:]] in ([], [STATE_IDLE])


async def test_a_pass_skips_the_rebuild_when_the_models_already_match_the_generation(make_rig) -> None:
    rig = await make_rig(quiet_s=0.4)
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    rig.engine.built_gen = (await rig.store.async_learning_state())["gen"]  # built before the pass began
    await rig.until_idle()
    assert rig.engine.rebuilds == []
    assert rig.manager.status()["last"]["stages"]["rebuild_skipped"] is True
    assert rig.engine.rechecks  # the recheck still ran


async def test_a_steady_trickle_of_answers_cannot_postpone_learning_forever(make_rig) -> None:
    rig = await make_rig(quiet_s=0.3, max_wait_s=0.6)
    uids = await rig.seed(4, 3)
    await rig.start()
    first = time.monotonic()
    answered_when_it_began = None
    for count, uid in enumerate(uids, start=1):
        await rig.label(uid)
        await asyncio.sleep(0.15)  # never quiet for the 0.3 s the debounce wants
        if answered_when_it_began is None and rig.engine.rebuilds:
            answered_when_it_began = count
    assert rig.engine.rebuild_times[0] - first < 1.2  # 0.6 s cap plus slack
    assert answered_when_it_began is not None and answered_when_it_began < len(uids)  # it began mid-trickle


# --- progress ------------------------------------------------------------------------------------


async def test_progress_is_reported_while_rechecking(make_rig) -> None:
    rig = await make_rig()
    rig.engine.visits = 5
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_idle()

    assert rig.states() == ["waiting", "learning", "rechecking", "idle"]
    rechecking = [status for _, status in rig.events if status["state"] == "rechecking"]
    assert (rechecking[0]["done"], rechecking[0]["total"]) == (None, None)  # unknown until the first tick
    ticks = [(s["done"], s["total"]) for s in rechecking[1:]]
    assert ticks[0] == (0, 5) and ticks[-1] == (5, 5)
    assert [d for d, _ in ticks] == sorted(d for d, _ in ticks)
    assert all(s["started"] is not None for s in rechecking)
    assert all(s["done"] is None for _, s in rig.events if s["state"] != "rechecking")
    assert rig.events[-1][1]["started"] is None


async def test_progress_ticks_are_throttled(make_rig) -> None:
    rig = await make_rig(progress_push_s=0.3)
    rig.engine.visits = 200
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_idle()
    rechecking = [status for _, status in rig.events if status["state"] == "rechecking"]
    assert 1 <= len(rechecking) <= 3  # 200 ticks inside a few milliseconds


async def test_one_failing_subscriber_does_not_stop_the_others(make_rig) -> None:
    rig = await make_rig()
    uids = await rig.seed(1, 3)

    def broken(_status: dict[str, Any]) -> None:
        raise RuntimeError("bad subscriber")

    rig.manager.subscribe(broken)
    heard: list[dict[str, Any]] = []
    unsubscribe = rig.manager.subscribe(heard.append)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_idle()
    assert heard and heard[-1]["state"] == "idle"
    assert rig.events and rig.events[-1][1]["state"] == "idle"

    unsubscribe()
    unsubscribe()  # twice is fine
    count = len(heard)
    await rig.label(uids[1])
    await rig.until_idle(passes=2)
    assert len(heard) == count


# --- durable and resumable -----------------------------------------------------------------------


async def test_a_restart_in_the_middle_of_a_pass_picks_the_work_up_again(make_rig) -> None:
    rig = await make_rig()
    rig.engine.gate = asyncio.Event()
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_stuck()

    await rig.close()  # Home Assistant restarts while the pass is stuck
    await rig.open(fresh_engine=True)
    owed = await rig.store.async_learning_state()
    assert owed["gen"] > owed["done_gen"]  # cancelling marked nothing done

    await rig.start()
    status = rig.manager.status()
    assert status["state"] == "waiting" and status["pending"] == 1  # owed again straight away
    await rig.until_idle()
    done = await rig.store.async_learning_state()
    assert done["done_gen"] == owed["gen"] and done["answers_done"] == 1
    assert rig.engine.rebuilds == [owed["gen"]]


async def test_a_restart_with_nothing_owed_stays_idle(make_rig) -> None:
    rig = await make_rig()
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_idle()
    last = rig.manager.status()["last"]

    await rig.close()
    await rig.open(fresh_engine=True)
    await rig.start()
    await asyncio.sleep(rig.timing["quiet_s"] * 3)
    assert rig.state() == STATE_IDLE and rig.manager.passes == 0 and rig.engine.rebuilds == []
    assert rig.manager.status()["last"] == last  # what the last pass did is remembered across restarts


async def test_sessions_queued_before_a_restart_are_reconciled_even_when_the_generation_matches(make_rig) -> None:
    rig = await make_rig()
    await rig.seed(1, 2)
    await rig.start()
    sync = rig.store._sync

    def queue() -> None:
        sync._queue_reconcile("v0")
        sync.conn.commit()

    await rig.store._run(queue)
    assert (await rig.store.async_learning_state())["queued"] == 1

    await rig.close()
    await rig.open(fresh_engine=True)
    await rig.start()
    assert rig.state() == STATE_WAITING
    await rig.until_idle()
    assert (await rig.store.async_learning_state())["queued"] == 0


async def test_unloading_cancels_a_running_pass_and_leaves_the_work_owed(make_rig) -> None:
    rig = await make_rig()
    rig.engine.gate = asyncio.Event()
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_stuck()

    await asyncio.wait_for(rig.manager.async_stop(), 2)
    await rig.manager.async_stop()  # twice is fine
    state = await rig.store.async_learning_state()  # the store is still usable afterwards
    assert state["gen"] > state["done_gen"]
    assert all(task.done() for task in asyncio.all_tasks() if (task.get_name() or "").startswith("kibble learning"))

    passes, pushed = rig.manager.passes, len(rig.events)
    rig.manager.notify(state["gen"] + 5, state["answers"] + 5)  # a stopped manager ignores everything
    await asyncio.sleep(rig.timing["quiet_s"] * 3)
    assert rig.manager.passes == passes and len(rig.events) == pushed


async def test_stopping_a_manager_that_never_started_is_fine(make_rig) -> None:
    rig = await make_rig()
    await rig.manager.async_stop()
    await rig.manager.async_stop()


# --- failure and retry ---------------------------------------------------------------------------


async def test_a_failing_pass_retries_with_a_doubling_wait_and_a_success_resets_it(make_rig) -> None:
    rig = await make_rig(retry_first_s=0.1, retry_max_s=0.4)
    rig.engine.raises = RuntimeError("boom")
    uids = await rig.seed(1, 4)
    await rig.start()
    await rig.label(uids[0])
    await rig.until(lambda: len(rig.engine.rebuilds) >= 5, timeout=10)  # the fifth attempt is under way

    def waits() -> list[float]:
        return [
            round(status["retry_at"] - at, 3)
            for at, status in rig.events
            if status["state"] == STATE_RETRYING and status["retry_at"] is not None
        ]

    first = waits()[:4]
    assert 0.05 < first[0] <= 0.11
    assert 0.15 < first[1] <= 0.21
    assert 0.35 < first[2] <= 0.41  # doubled again, now at its cap ...
    assert 0.35 < first[3] <= 0.41  # ... and staying there
    status = rig.manager.status()
    assert status["error"] == "boom" and status["pending"] == 1 and status["last"] is None

    rig.engine.raises = None  # the next retry succeeds
    await rig.until_idle()
    status = rig.manager.status()
    assert status["error"] is None and status["retry_at"] is None and status["last"] is not None
    assert (await rig.store.async_learning_state())["error"] is None

    rig.engine.raises = RuntimeError("again")  # a new failure starts over at the first wait
    before = len(waits())
    await rig.label(uids[1])
    await rig.until(lambda: len(waits()) > before)
    assert 0.05 < waits()[before] <= 0.11


async def test_coral_not_answering_is_retrying_with_the_reason_and_the_fallback_still_ran(make_rig) -> None:
    rig = await make_rig()
    rig.engine.error = "connection refused"
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_state(STATE_RETRYING)

    status = rig.manager.status()
    assert "CoralHub did not answer" in status["error"] and "connection refused" in status["error"]
    assert status["retry_at"] is not None and status["last"] is None and status["pending"] == 1
    assert rig.engine.rechecks == [0]  # the histogram fallback still rechecked the visits
    owed = await rig.store.async_learning_state()
    assert owed["gen"] > owed["done_gen"]  # not marked done: CoralHub never saw it
    assert owed["error"] == status["error"]

    rig.engine.error = None
    await rig.until_idle()
    status = rig.manager.status()
    assert status["error"] is None and status["last"] is not None and status["pending"] == 0


async def test_an_answer_while_retrying_does_not_wait_out_the_backoff(make_rig) -> None:
    rig = await make_rig(retry_first_s=3.0)
    rig.engine.raises = RuntimeError("boom")
    uids = await rig.seed(1, 3)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_state(STATE_RETRYING)  # the retry is three seconds away
    assert rig.manager.status()["retry_at"] > time.time() + 2.0

    rig.engine.raises = None
    started = time.monotonic()
    await rig.label(uids[1])
    await rig.until_idle(timeout=2.0)
    assert time.monotonic() - started < 1.5  # after the 0.15 s quiet moment, not after 3 s
    assert rig.manager.status()["error"] is None


async def test_an_answer_while_retrying_never_postpones_a_retry_that_is_already_due(make_rig) -> None:
    rig = await make_rig(quiet_s=2.0, retry_first_s=0.3)
    rig.engine.raises = RuntimeError("boom")
    uids = await rig.seed(1, 3)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_state(STATE_RETRYING, timeout=4.0)  # first attempt fails after the 2 s quiet moment

    rig.engine.raises = None
    started = time.monotonic()
    await rig.label(uids[1])  # a 2 s quiet moment would be later than the 0.3 s the retry is due in
    await rig.until_idle(timeout=1.5)
    assert time.monotonic() - started < 1.2


async def test_a_store_failure_inside_a_pass_is_retried(make_rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = await make_rig()
    real = rig.store.async_enforce_training_caps
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk hiccup")
        await real()

    monkeypatch.setattr(rig.store, "async_enforce_training_caps", flaky)
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_state(STATE_RETRYING)
    assert rig.manager.status()["error"] == "disk hiccup"
    await rig.until_idle()
    assert rig.manager.status()["error"] is None and calls == 2


async def test_failing_to_record_the_error_does_not_stop_the_retries(make_rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = await make_rig()
    rig.engine.raises = RuntimeError("boom")

    async def broken(_message: str) -> None:
        raise OSError("the store is the thing that failed")

    monkeypatch.setattr(rig.store, "async_learning_failed", broken)
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_state(STATE_RETRYING)
    rig.engine.raises = None
    await rig.until_idle()  # the worker lived through it


async def test_a_failing_snapshot_refresh_does_not_fail_the_pass(make_rig) -> None:
    rig = await make_rig()
    rig.refresh.side_effect = RuntimeError("snapshot broke")
    uids = await rig.seed(1, 2)
    await rig.start()
    await rig.label(uids[0])
    await rig.until_idle()
    status = rig.manager.status()
    assert status["error"] is None and status["last"] is not None
    assert (await rig.store.async_learning_state())["done_gen"] >= 1
