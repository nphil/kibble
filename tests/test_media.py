"""Media-player/dish-image logic worth pinning on its own, independent of a running Home
Assistant core instance or a real ffmpeg binary:

- `coordinator._pcm_convert_args`: the ffmpeg output-side argument selection that turns
  whatever `-i` decoded into the exact raw PCM shape `/speak`/`PUT /clips/<name>` require, with
  and without `record_clip`'s `-t` duration cap.
- `image._h264_to_jpeg_args`: why the dish-snapshot decode can't just call
  `ffmpeg.async_get_image` -- `-f h264` has to land *before* `-i`, which that helper can't
  express.
- `image._latest_dish_snapshot`: picking the before/after pair from the same (newest) feed
  record, never independently "whichever record happens to have my half".
- `coordinator._media_player_entity_id`/`_resolve_media_to_pcm`: `media_source.
  async_resolve_media` must always be passed an explicit `target_media_player` (the feeder's
  own `KibbleSpeaker`, resolved through the entity registry) -- never left at its `UNDEFINED`
  default, which trips a deprecation warning on every single call.
- `api.KibbleClient.speak`: a 409 from the agent raises the distinct `KibbleSpeakerBusyError`,
  not a generic `KibbleError` -- the one thing `media_player.py`/`__init__.py`'s clip services
  need to tell "speaker busy" from "the agent rejected the request" and give a clear message.
- `api.KibbleClient.label_face`/`unlabel_face`: a 404 names a specific crop that is no longer
  pending/labelled (a race, an eviction, a stale UI reference) -- `KibbleNotFoundError` with the
  agent's own message, not the default "not supported by this agent version" `KibbleError` that
  used to make it read like the agent lacked the route entirely.

`coordinator.py`/`image.py`/`media_player.py` import real `homeassistant` components (`ffmpeg`,
`media_player`, `media_source`), unlike the BLE-only modules the rest of this test suite
covers -- these tests need the project's own `.venv` (real `homeassistant` installed), the same
as `test_cat_id.py` already does for `select.py`/`binary_sensor.py`/`image.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.util import dt as dt_util
from kibble.api import DetectionEvent, FeedRecord, KibbleClient, KibbleNotFoundError, KibbleSpeakerBusyError
from kibble.const import DOMAIN
from kibble.coordinator import (
    KibbleCoordinator,
    _media_player_entity_id,
    _pcm_convert_args,
    _resolve_media_to_pcm,
)
from kibble.image import KibbleLastDetectionImage, _h264_to_jpeg_args, _latest_dish_snapshot


# --- coordinator._pcm_convert_args -------------------------------------------------------------


def test_pcm_convert_args_one_shot_fetch_has_no_duration_cap() -> None:
    """A downloaded URL (PLAY_MEDIA/save_clip) already ends on its own -- no `-t`."""
    args = _pcm_convert_args()
    assert args == ["-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", "-f", "s16le"]
    assert "-t" not in args


def test_pcm_convert_args_live_capture_adds_duration_cap() -> None:
    """`record_clip`'s live RTSP source needs `-t` or ffmpeg would read forever."""
    args = _pcm_convert_args(duration=5.0)
    # the base conversion shape is unchanged; "-t 5.000" is appended to bound the capture
    assert args == [
        "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", "-f", "s16le", "-t", "5.000",
    ]


# --- image._h264_to_jpeg_args --------------------------------------------------------------------


def test_h264_to_jpeg_args_forces_raw_h264_on_the_input_side() -> None:
    """The agent's `/feeds/<name>` URL has no extension/container for ffmpeg's prober to key
    off, so `-f h264` must be part of the *input* side -- `ffmpeg.async_get_image`'s `extra_cmd`
    only lands after `-i` and can't express this, which is why this can't just call it."""
    cmd, input_source, output = _h264_to_jpeg_args("http://192.168.4.85:8765/feeds/1-before.h264")
    assert input_source == "-f h264 -i http://192.168.4.85:8765/feeds/1-before.h264"
    assert cmd == ["-frames:v", "1", "-c:v", "mjpeg"]
    assert output == "-f image2pipe -"


# --- image._latest_dish_snapshot -----------------------------------------------------------------


def _record(ts: int, id_: str, before: str | None, after: str | None) -> FeedRecord:
    return FeedRecord(ts=ts, id=id_, amount1=None, amount2=None, manual=True, before=before, after=after)


def test_latest_dish_snapshot_empty_feeds_is_none_for_both_sides() -> None:
    assert _latest_dish_snapshot((), "before") == (None, None)
    assert _latest_dish_snapshot((), "after") == (None, None)


# --- image.KibbleLastDetectionImage._apply ---------------------------------------------------


def _detection(seq: int, ts: int, cls: str, image: str | None) -> DetectionEvent:
    return DetectionEvent(
        seq=seq, ts=ts, cls=cls, image=image, cat=None, score=None, pet_id=None, total_score=None
    )


def test_last_detection_image_keeps_the_newest_crop_when_a_track_event_is_newer() -> None:
    """A `track` event (the vendor's identification) has no crop. It must not blank the image
    entity -- the regression that made `image.*_last_detection` read unknown the moment the
    first identification landed after the newest visit crop."""
    fake = SimpleNamespace(_entry=SimpleNamespace(data={"host": "h", "port": 1}))
    events = (
        _detection(1, 100, "visit", "100-visit.jpg"),
        _detection(2, 200, "track", None),
    )
    KibbleLastDetectionImage._apply(fake, events)
    assert fake._event.seq == 1
    assert fake._attr_image_url.endswith("/events/100-visit.jpg")
    assert fake._attr_image_last_updated == dt_util.utc_from_timestamp(100)

    KibbleLastDetectionImage._apply(fake, (_detection(2, 200, "track", None),))
    assert fake._event is None and fake._attr_image_url is None


def test_latest_dish_snapshot_uses_the_newest_records_own_side_not_an_older_records() -> None:
    """`GET /feeds` is oldest-first. The newest record here has no `after` shot (still mid-
    settle when polled); the *older* record does have one. The `after` side must come back
    `None` -- picking the older record's `after` instead would pair feed #1's "after" next to
    feed #2's "before", a mismatched pair from two different feed cycles."""
    older = _record(100, "feed-1", before="100-feed-1-before.h264", after="100-feed-1-after.h264")
    newest = _record(200, "feed-2", before="200-feed-2-before.h264", after=None)
    feeds = (older, newest)

    assert _latest_dish_snapshot(feeds, "before") == (
        "200-feed-2-before.h264",
        dt_util.utc_from_timestamp(200),
    )
    assert _latest_dish_snapshot(feeds, "after") == (None, None)


# --- coordinator._media_player_entity_id / _resolve_media_to_pcm -----------------------------


def test_media_player_entity_id_looks_up_the_speakers_known_unique_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """`media_player.py`'s `async_setup_entry` always registers `KibbleSpeaker` under
    `f"{serial}_speaker"` (`entity.py`'s `unique_id` scheme) -- resolved through the entity
    registry, never reconstructed from a (renamable) display name."""
    registry = SimpleNamespace(async_get_entity_id=Mock(return_value="media_player.cat_feeder_speaker"))
    monkeypatch.setattr("kibble.coordinator.er.async_get", Mock(return_value=registry))

    result = _media_player_entity_id(SimpleNamespace(), "ABC123")

    assert result == "media_player.cat_feeder_speaker"
    registry.async_get_entity_id.assert_called_once_with("media_player", DOMAIN, "ABC123_speaker")


async def test_resolve_media_to_pcm_never_leaves_target_media_player_at_its_deprecated_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`media_source.async_resolve_media` warns (`homeassistant.helpers.frame`'s `report_usage`)
    the instant `target_media_player` is left at its `UNDEFINED` default -- the actual
    2026-09-18 log entry this integration triggered. Passing an explicit third argument, even a
    real one resolved from the registry, structurally rules that out."""
    hass = SimpleNamespace()
    resolve = AsyncMock(return_value=SimpleNamespace(url="http://x/resolved.mp3"))
    monkeypatch.setattr("kibble.coordinator.async_resolve_media", resolve)
    monkeypatch.setattr("kibble.coordinator.async_process_play_media_url", lambda _hass, url: url)
    monkeypatch.setattr("kibble.coordinator._pcm_from_ffmpeg", AsyncMock(return_value=b"pcm"))

    pcm = await _resolve_media_to_pcm(hass, "media-source://tts/x", "media_player.cat_feeder_speaker")

    assert pcm == b"pcm"
    resolve.assert_awaited_once_with(hass, "media-source://tts/x", "media_player.cat_feeder_speaker")


async def test_resolve_media_to_pcm_skips_resolution_entirely_for_a_plain_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain (non media-source) URL never reaches `async_resolve_media` at all -- `entity_id`
    is irrelevant to this path, so a bogus one must not matter."""
    hass = SimpleNamespace()
    resolve = AsyncMock()
    monkeypatch.setattr("kibble.coordinator.async_resolve_media", resolve)
    monkeypatch.setattr("kibble.coordinator.async_process_play_media_url", lambda _hass, url: url)
    monkeypatch.setattr("kibble.coordinator._pcm_from_ffmpeg", AsyncMock(return_value=b"pcm"))

    pcm = await _resolve_media_to_pcm(hass, "http://example.com/song.mp3", "bogus.entity")

    assert pcm == b"pcm"
    resolve.assert_not_awaited()


async def test_async_resolve_and_convert_passes_the_serials_speaker_entity_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator-level glue: `self.data.state.serial` -> `_media_player_entity_id` ->
    `_resolve_media_to_pcm`'s `entity_id`, so a real feeder's `KibbleSpeaker` entity actually
    ends up passed through both `async_play_media_content` and `async_save_clip`, not a
    hardcoded stand-in."""
    registry = SimpleNamespace(async_get_entity_id=Mock(return_value="media_player.cat_feeder_speaker"))
    monkeypatch.setattr("kibble.coordinator.er.async_get", Mock(return_value=registry))
    resolve_to_pcm = AsyncMock(return_value=b"pcm")
    monkeypatch.setattr("kibble.coordinator._resolve_media_to_pcm", resolve_to_pcm)
    hass = SimpleNamespace()
    fake_self = SimpleNamespace(hass=hass, data=SimpleNamespace(state=SimpleNamespace(serial="ABC123")))

    pcm = await KibbleCoordinator.async_resolve_and_convert(fake_self, "media-source://tts/x")

    assert pcm == b"pcm"
    resolve_to_pcm.assert_awaited_once_with(hass, "media-source://tts/x", "media_player.cat_feeder_speaker")
    registry.async_get_entity_id.assert_called_once_with("media_player", DOMAIN, "ABC123_speaker")


# --- api.KibbleClient.speak / 409 handling --------------------------------------------------------


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


async def test_speak_409_raises_speaker_busy_error_not_a_generic_kibble_error() -> None:
    session = _FakeSession(409, {"error": "the speaker is already in use by another kibbled session"})
    client = KibbleClient(session, "192.168.4.85", 8765)

    with pytest.raises(KibbleSpeakerBusyError, match="already in use"):
        await client.speak(b"\x00\x00" * 100)


# --- api.KibbleClient.label_face / unlabel_face 404 handling ---------------------------------


async def test_label_face_404_surfaces_the_agents_own_not_found_message() -> None:
    """A 404 from `POST /faces/label` names a specific crop that is no longer pending (already
    labelled by a race, evicted, a stale/double-submitted UI reference) -- a real
    `KibbleNotFoundError` carrying the agent's own message, not the default "not supported by
    this agent version" `KibbleError` that used to make it read exactly like the agent lacked
    this route entirely (the actual 2026-09-18 log: "Label face failed: /faces/label not
    supported by this agent version")."""
    session = _FakeSession(404, {"error": "no such pending face crop"})
    client = KibbleClient(session, "192.168.4.85", 8765)

    with pytest.raises(KibbleNotFoundError, match="no such pending face crop"):
        await client.label_face("1-unknown.jpg", "Kitty")


async def test_unlabel_face_404_surfaces_the_agents_own_not_found_message() -> None:
    """Same reasoning as `label_face` -- `unlabel_face`'s crop/cat pair not currently being
    labelled is a real 404, not evidence the route is unsupported."""
    session = _FakeSession(404, {"error": "no such labelled crop"})
    client = KibbleClient(session, "192.168.4.85", 8765)

    with pytest.raises(KibbleNotFoundError, match="no such labelled crop"):
        await client.unlabel_face("1-unknown.jpg", "Kitty")
