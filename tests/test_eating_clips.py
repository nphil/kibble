"""`eating_clips.py`: `is_valid_video_id`/`parse_clip_candidates`'s parsing gates,
`best_overlap_clip`'s selection among overlapping candidates, the URL builders, and
`ClipLinker`'s retry/dedup/restart-survival behaviour -- see docs/39-eating-clips.md for the
full design this backs.

`ClipLinker`'s own tests never touch a real event loop timer or a real `aiohttp` session:
`_fetch_candidates` is overridden directly (the one place network I/O would happen), and
`entry.async_create_background_task` is faked with plain `asyncio.ensure_future` -- the same
fake-hass pattern `tests/test_upload_views.py` already uses for `hass.async_create_task`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from kibble.const import CONF_SCRYPTED_CLIPS_URL
from kibble.eating_clips import (
    ClipCandidate,
    ClipLinker,
    best_overlap_clip,
    clips_list_url,
    is_valid_video_id,
    parse_clip_candidates,
    videoclip_url,
)


def _clip(video_id: str, start_ms: int, end_ms: int) -> ClipCandidate:
    return ClipCandidate(video_id=video_id, start_ms=start_ms, end_ms=end_ms)


# --- is_valid_video_id ---------------------------------------------------------------------------


def test_a_video_id_in_the_recorders_live_list_form_is_valid() -> None:
    # Exactly what the live clips list returned on 2026-09-25: no extension.
    assert is_valid_video_id("1790330103347_1790330188581_1001000000") is True


@pytest.mark.parametrize(
    "video_id",
    [
        "1758812345678_1758812400123_0001000000.mp4",  # an extension is not the list's form
        "1758812345678_1758812400123_00010000001",  # 11 bits, not 10
        "175881234567_1758812400123_0001000000",  # 12-digit start, not 13
        "1758812345678_1758812400123_000100000a",  # non-binary bit
        "../../etc/passwd",  # traversal attempt
        "1758812345678_1758812400123_0001000000; rm -rf /",  # trailing junk
        "",
    ],
)
def test_a_malformed_video_id_is_rejected(video_id: str) -> None:
    assert is_valid_video_id(video_id) is False


# --- parse_clip_candidates -------------------------------------------------------------------


def test_parse_clip_candidates_keeps_only_well_formed_entries() -> None:
    good = {"videoId": "1000000000000_1000000060000_0001000000", "startTime": 1000000000000, "endTime": 1000000060000}
    payload = [
        good,
        {"videoId": "not-a-real-id", "startTime": 1, "endTime": 2},
        {"videoId": "1000000000000_1000000060000_0001000000"},  # missing times
        {"startTime": 1, "endTime": 2},  # missing videoId
        "not even a dict",
        # end before start -- a malformed/impossible clip, never a candidate.
        {"videoId": "1000000000000_0999999999999_0001000000", "startTime": 1000000000000, "endTime": 999999999999},
    ]
    assert parse_clip_candidates(payload) == [
        ClipCandidate(video_id=good["videoId"], start_ms=good["startTime"], end_ms=good["endTime"])
    ]


def test_parse_clip_candidates_tolerates_a_non_list_payload() -> None:
    assert parse_clip_candidates({"error": "not found"}) == []
    assert parse_clip_candidates(None) == []
    assert parse_clip_candidates("garbage") == []


# --- best_overlap_clip -------------------------------------------------------------------------


def test_no_candidates_returns_none() -> None:
    assert best_overlap_clip([], 1000, 2000) is None


def test_a_single_containing_clip_is_picked() -> None:
    clip = _clip("a", 900, 2100)
    assert best_overlap_clip([clip], 1000, 2000) is clip


def test_a_clip_only_touching_the_session_boundary_is_never_picked() -> None:
    clip = _clip("a", 2000, 2500)  # starts exactly where the session ends -- zero real overlap
    assert best_overlap_clip([clip], 1000, 2000) is None


def test_the_best_overlapping_of_two_adjacent_sessions_own_clips_wins() -> None:
    """The realistic back-to-back-meals shape: a padded query window around Kitty's own
    session also returns Pancake's neighbouring clip, but scoring against Kitty's own REAL
    span picks Kitty's clip -- Pancake's only touches the edge of Kitty's window."""
    kitty_session = (60_000, 120_000)
    kitty_clip = _clip("kitty", 50_000, 135_000)
    pancake_clip = _clip("pancake", 120_000, 205_000)
    assert best_overlap_clip([pancake_clip, kitty_clip], *kitty_session) is kitty_clip


def test_a_tie_resolves_to_the_earlier_starting_clip() -> None:
    # Same 950ms overlap against the [1000, 2000] session either way (each clip is offset 100ms
    # to a different side), so only the tiebreaker -- earliest start -- decides.
    earlier = _clip("earlier", 900, 1950)
    later = _clip("later", 1050, 2100)
    assert best_overlap_clip([later, earlier], 1000, 2000) is earlier


# --- URL builders ----------------------------------------------------------------------------


def test_clips_list_url_strips_a_trailing_slash_and_encodes_the_window() -> None:
    url = clips_list_url("http://scrypted.local:11080/", 1000, 2000)
    parsed = urlparse(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("http", "scrypted.local:11080", "/endpoint/@nphil/kibble-scrypted/public/clips")
    assert parse_qs(parsed.query) == {"start": ["1000"], "end": ["2000"]}


def test_videoclip_url_encodes_the_device_id_and_filename() -> None:
    url = videoclip_url("http://scrypted.local:11080", "1000_2000_0001000000.mp4")
    parsed = urlparse(url)
    assert parsed.path == "/endpoint/@apocaliss92/scrypted-events-recorder/public/videoclip"
    params = json.loads(parse_qs(parsed.query)["params"][0])
    assert params == {"deviceId": "240", "filename": "1000_2000_0001000000.mp4"}


# --- ClipLinker --------------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.linked: list[tuple[str, str, int, int]] = []
        self.needing: list[tuple[str, int, int]] = []

    async def async_set_event_clip(self, uid: str, *, clip_id: str, clip_start_ms: int, clip_end_ms: int) -> None:
        self.linked.append((uid, clip_id, clip_start_ms, clip_end_ms))

    async def async_events_needing_clip_link(self, cutoff: int) -> list[tuple[str, int, int]]:
        return self.needing


def _fake_entry(url: str = "http://scrypted.local:11080") -> SimpleNamespace:
    def _spawn(hass: Any, coro: Any, name: str | None = None) -> asyncio.Task[None]:
        return asyncio.ensure_future(coro)

    return SimpleNamespace(options={CONF_SCRYPTED_CLIPS_URL: url}, async_create_background_task=_spawn)


def _linker(store: _FakeStore, *, url: str = "http://scrypted.local:11080", retry_delays_s: tuple[float, ...] = ()) -> ClipLinker:
    return ClipLinker(hass=None, entry=_fake_entry(url), store=store, retry_delays_s=retry_delays_s)


async def test_a_clip_found_on_the_immediate_attempt_links_with_no_retry() -> None:
    store = _FakeStore()
    linker = _linker(store, retry_delays_s=(10.0, 20.0, 30.0))
    calls = 0

    async def _fetch(session_start_ms: int, session_end_ms: int) -> list[ClipCandidate]:
        nonlocal calls
        calls += 1
        return [_clip("a", session_start_ms, session_end_ms)]

    linker._fetch_candidates = _fetch  # type: ignore[method-assign]
    await linker._link_with_retries("e1", 100, 160)
    assert calls == 1
    assert store.linked == [("e1", "a", 100_000, 160_000)]


async def test_the_retry_schedule_stops_the_instant_a_clip_is_found() -> None:
    store = _FakeStore()
    linker = _linker(store, retry_delays_s=(0.01, 0.01, 0.01))
    calls = 0

    async def _fetch(session_start_ms: int, session_end_ms: int) -> list[ClipCandidate]:
        nonlocal calls
        calls += 1
        return [] if calls < 2 else [_clip("a", session_start_ms, session_end_ms)]

    linker._fetch_candidates = _fetch  # type: ignore[method-assign]
    await linker._link_with_retries("e1", 100, 160)
    assert calls == 2  # the immediate attempt, then exactly one retry -- never a third
    assert store.linked == [("e1", "a", 100_000, 160_000)]


async def test_the_retry_schedule_gives_up_after_exhausting_every_delay() -> None:
    store = _FakeStore()
    linker = _linker(store, retry_delays_s=(0.01, 0.01, 0.01))
    calls = 0

    async def _fetch(session_start_ms: int, session_end_ms: int) -> list[ClipCandidate]:
        nonlocal calls
        calls += 1
        return []

    linker._fetch_candidates = _fetch  # type: ignore[method-assign]
    await linker._link_with_retries("e1", 100, 160)
    assert calls == 4  # the immediate attempt plus all three retries, then stop -- never more
    assert store.linked == []


async def test_a_lookup_failure_counts_as_not_found_and_the_arc_keeps_going() -> None:
    store = _FakeStore()
    linker = _linker(store, retry_delays_s=(0.01,))
    calls = 0

    async def _fetch(session_start_ms: int, session_end_ms: int) -> list[ClipCandidate]:
        nonlocal calls
        calls += 1
        raise TimeoutError("boom")

    linker._fetch_candidates = _fetch  # type: ignore[method-assign]
    await linker._link_with_retries("e1", 100, 160)  # must not raise
    assert calls == 2
    assert store.linked == []


def test_schedule_link_is_a_no_op_while_disabled() -> None:
    store = _FakeStore()
    linker = _linker(store, url="")
    assert linker.enabled is False
    linker.schedule_link("e1", 100, 160)
    assert linker._pending == {}


async def test_schedule_link_never_restarts_an_already_running_arc_for_the_same_uid() -> None:
    store = _FakeStore()
    linker = _linker(store, retry_delays_s=(5.0,))
    calls = 0
    gate = asyncio.Event()

    async def _fetch(session_start_ms: int, session_end_ms: int) -> list[ClipCandidate]:
        nonlocal calls
        calls += 1
        await gate.wait()
        return []

    linker._fetch_candidates = _fetch  # type: ignore[method-assign]
    linker.schedule_link("e1", 100, 160)
    await asyncio.sleep(0)  # let the background task start its first attempt
    linker.schedule_link("e1", 100, 160)  # same still-in-flight uid -- must not double-schedule
    assert calls == 1
    gate.set()
    await linker._pending["e1"]


async def test_async_relink_recent_schedules_every_closed_unlinked_session() -> None:
    store = _FakeStore()
    store.needing = [("e1", 100, 160), ("e2", 200, 260)]
    linker = _linker(store)
    scheduled: list[str] = []
    linker.schedule_link = lambda uid, start_ts, end_ts: scheduled.append(uid)  # type: ignore[method-assign]
    await linker.async_relink_recent()
    assert scheduled == ["e1", "e2"]


async def test_async_relink_recent_is_a_no_op_while_disabled() -> None:
    store = _FakeStore()
    store.needing = [("e1", 100, 160)]
    linker = _linker(store, url="")
    scheduled: list[str] = []
    linker.schedule_link = lambda *a: scheduled.append(a[0])  # type: ignore[method-assign]
    await linker.async_relink_recent()
    assert scheduled == []


async def test_async_cancel_cancels_a_still_sleeping_retry() -> None:
    store = _FakeStore()
    linker = _linker(store, retry_delays_s=(60.0,))

    async def _fetch(session_start_ms: int, session_end_ms: int) -> list[ClipCandidate]:
        return []

    linker._fetch_candidates = _fetch  # type: ignore[method-assign]
    linker.schedule_link("e1", 100, 160)
    await asyncio.sleep(0)
    assert "e1" in linker._pending
    await linker.async_cancel()
    assert linker._pending["e1"].cancelled()
