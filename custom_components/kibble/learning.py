"""The learning job: what happens AFTER a person labels something (docs/36-ai-pipeline.md,
"Learning").

A label is written to the store and acknowledged at once; nothing here sits on that path. What
a label changes -- the cat-recognition models, and the names the engine had already guessed for
every unreviewed visit -- is brought up to date by ONE job per entry, in the background:

- Every change to the training gallery bumps a persisted generation counter in the same store
  transaction, and the store tells `LearningManager.notify` after the job that made it
  (`KibbleStore.set_learning_listener`). Nobody has to remember to ask for a relearn.
- Debounced: a pass starts `quiet_s` after the LAST change, but never later than `max_wait_s`
  after the FIRST. Twenty quick labels cost one pass, not twenty.
- Single flight. A change that arrives while a pass runs earns one follow-up pass afterwards.
- One pass = reconcile the sessions whose training was queued -> enforce the training caps ->
  rebuild the models (skipped when they already match the generation) -> recheck every
  unreviewed visit -> refresh the identity snapshot -> record the result. Each stage's time is
  kept in the result (`last.stages`).
- Durable and resumable: the generation the last finished pass covered (`done_gen`) is stored
  next to the current one, so a restart in the middle of a pass -- or before one started --
  finds `gen > done_gen` and runs it again.
- Survives failure: any exception, or CoralHub not answering, ends the pass in `retrying` with a
  backoff (30 s doubling to 15 min) instead of being lost; a success resets it. A new answer
  while retrying does not wait out the backoff: it earns a pass after the usual quiet moment,
  never later than the retry was already due.
- Cancelled cleanly on unload, BEFORE the store closes (`KibbleCoordinator.async_cancel_startup`).
  Cancelling never marks anything done, so the next start picks the work up again.

`status()` is what `kibble/learning`, `kibble/learning/subscribe` and the learning sensor show:
read from memory, so asking never waits for the store, however busy a pass keeps it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .ingest import IdentityEngine
    from .store import KibbleStore

_LOGGER = logging.getLogger(__name__)

STATE_IDLE = "idle"
STATE_WAITING = "waiting"
STATE_LEARNING = "learning"
STATE_RECHECKING = "rechecking"
STATE_RETRYING = "retrying"
STATES = (STATE_IDLE, STATE_WAITING, STATE_LEARNING, STATE_RECHECKING, STATE_RETRYING)

QUIET_S = 20.0  # a pass starts this long after the last change ...
MAX_WAIT_S = 120.0  # ... but never later than this after the first one
RETRY_FIRST_S = 30.0  # first retry delay after a failed pass; doubles each time ...
RETRY_MAX_S = 900.0  # ... up to this
PROGRESS_PUSH_S = 0.5  # at most this often between two non-forced status pushes


class LearningManager:
    """The one learning job of one config entry; see the module docstring."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        store: KibbleStore,
        engine: IdentityEngine,
        *,
        retention_cutoff: Callable[[], int],
        refresh_snapshot: Callable[[], Awaitable[None]],
        quiet_s: float = QUIET_S,
        max_wait_s: float = MAX_WAIT_S,
        retry_first_s: float = RETRY_FIRST_S,
        retry_max_s: float = RETRY_MAX_S,
        progress_push_s: float = PROGRESS_PUSH_S,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = store
        self._engine = engine
        self._retention_cutoff = retention_cutoff
        self._refresh_snapshot = refresh_snapshot
        self._quiet_s = quiet_s
        self._max_wait_s = max_wait_s
        self._retry_first_s = retry_first_s
        self._retry_max_s = retry_max_s
        self._progress_push_s = progress_push_s

        self._state = STATE_IDLE
        self._gen_seen = 0  # the newest training generation heard of
        self._done_gen = 0  # the generation the last finished pass covered
        self._answers = 0  # answers that changed the gallery, ever (persisted by the store)
        self._answers_done = 0  # ... and how many of them a finished pass covered
        self._first_dirty: float | None = None  # loop time of the first change since the last pass
        self._deadline: float | None = None  # loop time the next pass (or retry) starts
        self._retry_deadline: float | None = None  # loop time the pending retry is due, while retrying
        self._backoff = 0.0
        self._started: float | None = None  # unix time the running pass began
        self._progress: tuple[int, int] | None = None
        self._last: dict[str, Any] | None = None
        self._error: str | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._last_push = 0.0
        self._push_handle: asyncio.TimerHandle | None = None
        self.passes = 0  # passes started since this manager was created

    # --- reading ------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """What the card and the sensor show -- see `docs/36-ai-pipeline.md` for every field."""
        loop_now = self._hass.loop.time()
        wall_now = time.time()
        running = self._state in (STATE_LEARNING, STATE_RECHECKING)
        rechecking = self._state == STATE_RECHECKING and self._progress is not None
        scheduled = self._deadline is not None
        return {
            "state": self._state,
            "pending": max(0, self._answers - self._answers_done),
            "done": self._progress[0] if rechecking and self._progress else None,
            "total": self._progress[1] if rechecking and self._progress else None,
            "starts_at": (
                wall_now + (self._deadline - loop_now)
                if self._state == STATE_WAITING and scheduled and self._deadline is not None
                else None
            ),
            "retry_at": (
                wall_now + (self._deadline - loop_now)
                if self._state == STATE_RETRYING and scheduled and self._deadline is not None
                else None
            ),
            "started": self._started if running else None,
            "last": self._last,
            "error": self._error,
        }

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """`listener(status)` on every change of what `status()` returns (progress ticks are
        throttled); returns the unsubscribe callback. Called on the event loop."""
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsubscribe

    # --- life cycle ---------------------------------------------------------------------------

    async def async_start(self) -> None:
        """Loads the persisted state, schedules the pass an interrupted run still owes, and
        starts the worker. Called once from `__init__.py`'s start-up task."""
        state = await self._store.async_learning_state()
        self._gen_seen = state["gen"]
        self._done_gen = state["done_gen"]
        self._answers = state["answers"]
        self._answers_done = state["answers_done"]
        self._last = state["last_result"]
        if state["counts_done"] is None:
            # The baseline "what was learned" is measured from: the gallery as it is now.
            await self._store.async_learning_set_counts(await self._store.async_training_counts_by_cat())
        if state["gen"] > state["done_gen"] or state["queued"] > 0:
            _LOGGER.info(
                "Kibble learning: resuming an unfinished pass (generation %s of %s, %s session(s) queued)",
                state["done_gen"], state["gen"], state["queued"],
            )
            now = self._hass.loop.time()
            self._first_dirty = now
            self._deadline = now + self._quiet_s
            self._state = STATE_WAITING
        self._task = self._hass.async_create_background_task(
            self._worker(), f"kibble learning {self._entry_id[:8]}"
        )

    async def async_stop(self) -> None:
        """Stops the worker (and a pass in the middle of its store calls) and forgets the
        listeners' pending push. Safe to call twice, or without `async_start`. Nothing is marked
        done, so the next start resumes whatever was unfinished."""
        self._stopped = True
        if self._push_handle is not None:
            self._push_handle.cancel()
            self._push_handle = None
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    def notify(self, gen: int, answers: int) -> None:
        """The training gallery changed (`KibbleStore.set_learning_listener`; on the loop)."""
        if self._stopped:
            return
        self._gen_seen = max(self._gen_seen, gen)
        self._answers = max(self._answers, answers)
        if gen <= self._done_gen or self._state in (STATE_LEARNING, STATE_RECHECKING):
            # Nothing to schedule: it is already learned, or a pass is running (it ends by
            # looking at `_gen_seen` and starts the follow-up itself). Only the count of waiting
            # answers moved.
            self._push()
            return
        now = self._hass.loop.time()
        if self._first_dirty is None:
            self._first_dirty = now
        deadline = min(now + self._quiet_s, self._first_dirty + self._max_wait_s)
        if self._state == STATE_RETRYING:
            if self._retry_deadline is not None:
                deadline = min(deadline, self._retry_deadline)
            self._deadline = deadline
            self._push()
        else:
            self._deadline = deadline
            if self._state == STATE_WAITING:
                self._push()
            else:
                self._set_state(STATE_WAITING)
        self._wake.set()

    # --- the worker ---------------------------------------------------------------------------

    async def _worker(self) -> None:
        loop = self._hass.loop
        while True:
            deadline = self._deadline
            if deadline is None:
                await self._wake.wait()
                self._wake.clear()
                continue
            delay = deadline - loop.time()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                else:
                    self._wake.clear()
                    continue  # the deadline may have moved
            await self._run_pass()

    async def _run_pass(self) -> None:
        self.passes += 1
        self._deadline = None
        self._retry_deadline = None
        self._first_dirty = None
        self._started = time.time()
        self._progress = None
        self._set_state(STATE_LEARNING)
        try:
            target, answers_covered, result, coral_error = await self._learn()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 -- the job must survive anything, then retry
            _LOGGER.warning("Kibble learning pass failed: %s", err, exc_info=True)
            await self._fail(str(err) or type(err).__name__)
            return
        if coral_error is not None:
            await self._fail(
                f"CoralHub did not answer ({coral_error}); the built-in recognizer is used until it does"
            )
            return
        self._backoff = 0.0
        self._error = None
        self._done_gen = max(self._done_gen, target)
        self._answers_done = max(self._answers_done, min(answers_covered, self._answers))
        self._last = result
        _LOGGER.info(
            "Kibble learning pass finished in %.1f s (rechecked %d, renamed %d): %s",
            result["duration"], result["rechecked"], result["renamed"], result["stages"],
        )
        if self._gen_seen > target:  # something was labelled while this pass ran
            now = self._hass.loop.time()
            self._first_dirty = now
            self._deadline = now + self._quiet_s
            self._set_state(STATE_WAITING)
        else:
            self._set_state(STATE_IDLE)

    async def _learn(self) -> tuple[int, int, dict[str, Any], str | None]:
        """One pass. Returns `(generation covered, answers covered, result, CoralHub's error or
        None)`; the durable "done" record is written here, so an exception anywhere before it
        leaves the work owed."""
        store, engine = self._store, self._engine
        stages: dict[str, Any] = {}
        started = mark = time.perf_counter()

        def lap(stage: str) -> None:
            nonlocal mark
            now = time.perf_counter()
            stages[stage] = round((now - mark) * 1000)
            mark = now

        await store.async_reconcile_queued_sessions()
        lap("reconcile_ms")
        await store.async_enforce_training_caps()
        lap("caps_ms")
        state = await store.async_learning_state()
        target, answers = state["gen"], state["answers"]
        baseline = state["counts_done"]
        lap("state_ms")

        embeds = engine.embed_stats
        embeds_before = embeds.snapshot() if embeds is not None else None
        coral_error: str | None = None
        if engine.built_gen != target:
            coral_error = await engine.async_rebuild(gen=target)
            stages.update(engine.timings)
        else:
            stages["rebuild_skipped"] = True
        lap("rebuild_ms")
        if embeds is not None and embeds_before is not None:
            stages["coral_requests"] = embeds.since(embeds_before)

        self._set_state(STATE_RECHECKING)
        outcome = await engine.async_reclassify_unreviewed(self._retention_cutoff(), progress=self._on_progress)
        lap("recheck_ms")
        counts = await store.async_training_counts_by_cat()
        try:
            await self._refresh_snapshot()
        except Exception:  # noqa: BLE001 -- the snapshot is cosmetic; it must not fail a pass
            _LOGGER.exception("Refreshing the identity snapshot after learning failed")
        lap("snapshot_ms")

        learned = {
            cat: counts.get(cat, 0) - baseline.get(cat, 0)
            for cat in set(counts) | set(baseline or {})
        } if baseline is not None else {}
        result = {
            "finished": time.time(),
            "duration": round(time.perf_counter() - started, 1),
            "answers": max(0, answers - state["answers_done"]),
            "learned": {cat: n for cat, n in sorted(learned.items()) if n},
            "renamed": outcome["renamed"],
            "rechecked": outcome["rechecked"],
            "stages": stages,
        }
        if coral_error is None:
            await store.async_learning_complete(gen=target, answers=answers, result=result, counts=counts)
        return target, answers, result, coral_error

    async def _fail(self, message: str) -> None:
        self._backoff = (
            self._retry_first_s if not self._backoff else min(self._backoff * 2, self._retry_max_s)
        )
        self._error = message
        self._retry_deadline = self._deadline = self._hass.loop.time() + self._backoff
        self._set_state(STATE_RETRYING)
        _LOGGER.warning("Kibble learning will try again in %.0f s (%s)", self._backoff, message)
        try:
            await self._store.async_learning_failed(message)
        except Exception:  # noqa: BLE001 -- the store may be the very thing that failed
            _LOGGER.debug("Could not record the learning error", exc_info=True)

    def _on_progress(self, done: int, total: int) -> None:
        self._progress = (done, total)
        self._push()

    # --- pushing status to subscribers --------------------------------------------------------

    def _set_state(self, state: str) -> None:
        self._state = state
        self._push(force=True)

    def _push(self, *, force: bool = False) -> None:
        if not self._listeners:
            return
        wait = self._progress_push_s - (time.monotonic() - self._last_push)
        if not force and wait > 0:
            if self._push_handle is None:
                self._push_handle = self._hass.loop.call_later(wait, self._emit)
            return
        self._emit()

    def _emit(self) -> None:
        if self._push_handle is not None:
            self._push_handle.cancel()
            self._push_handle = None
        self._last_push = time.monotonic()
        status = self.status()
        for listener in list(self._listeners):
            try:
                listener(status)
            except Exception:  # noqa: BLE001 -- one bad subscriber must not starve the others
                _LOGGER.exception("A learning-status subscriber failed")
