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
- `api.KibbleClient.speak`: a 409 from the agent raises the distinct `KibbleSpeakerBusyError`,
  not a generic `KibbleError` -- the one thing `media_player.py`/`__init__.py`'s clip services
  need to tell "speaker busy" from "the agent rejected the request" and give a clear message.

`coordinator.py`/`image.py`/`media_player.py` import real `homeassistant` components (`ffmpeg`,
`media_player`, `media_source`), unlike the BLE-only modules the rest of this test suite
covers -- these tests need the project's own `.venv` (real `homeassistant` installed), the same
as `test_cat_id.py` already does for `select.py`/`binary_sensor.py`/`image.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.util import dt as dt_util
from kibble.api import DetectionEvent, FeedRecord, KibbleClient, KibbleSpeakerBusyError
from kibble.coordinator import _pcm_convert_args
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
        seq=seq, ts=ts, cls=cls, image=image, cat=None, score=None, pet_id=None, track_value=None
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
