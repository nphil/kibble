"""Where the camera entity gets its video from, and what happens when that source is gone.

Three configurations are all first-class (`camera.py`'s module docstring): delegate to another
camera entity, a fixed RTSP URL, or the feeder's own stream with nothing configured at all.
The third is what an install with no video hub gets, so **Scrypted must never be a hard
dependency** -- that is the property these tests exist to hold.

The delegate path also has to survive its source disappearing. Scrypted assigns its RTSP
rebroadcast an ephemeral port, and on 2026-09-19 the port recorded in `stream_url` simply
stopped listening after a Scrypted restart: HA reported the entity as `streaming` while the
camera dialog showed a dead player at 0:00. Resolving through the entity is what fixes that,
and falling back to the device is what keeps a picture on screen when even that fails.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kibble.camera import KibbleCamera
from kibble.const import CONF_HOST, CONF_STREAM_ENTITY, CONF_STREAM_URL

DEVICE_URL = "rtsp://192.168.4.85:8554/sub"


def _camera(**options) -> KibbleCamera:
    """A real, unconstructed `KibbleCamera` with only the entry its properties read."""
    cam = object.__new__(KibbleCamera)
    cam._entry = SimpleNamespace(data={CONF_HOST: "192.168.4.85"}, options=options)
    cam.hass = object()
    return cam


async def test_no_configuration_streams_straight_from_the_feeder() -> None:
    """The no-hub install. Nothing configured must still produce video."""
    cam = _camera()
    assert await cam.stream_source() == DEVICE_URL
    assert cam.extra_state_attributes == {"source": "device"}


async def test_a_fixed_url_is_used_verbatim() -> None:
    cam = _camera(**{CONF_STREAM_URL: "rtsp://hub.lan:8554/feeder"})
    assert await cam.stream_source() == "rtsp://hub.lan:8554/feeder"
    assert cam.extra_state_attributes["source"] == "url"


async def test_a_delegate_entity_is_resolved_at_the_moment_it_is_asked() -> None:
    """Not cached: the delegate's own URL changes under us whenever its integration restarts,
    which is the entire reason delegating beats pinning a URL."""
    cam = _camera(**{CONF_STREAM_ENTITY: "camera.feeder_nvr"})
    with patch(
        "kibble.camera.async_get_stream_source", AsyncMock(return_value="rtsp://hub.lan:41424/abc")
    ) as resolve:
        assert await cam.stream_source() == "rtsp://hub.lan:41424/abc"
    resolve.assert_awaited_once()
    assert cam.extra_state_attributes == {
        "source": "entity",
        "source_entity": "camera.feeder_nvr",
    }


async def test_a_delegate_with_no_stream_falls_back_to_the_device() -> None:
    """The hub is down or reloading. The feeder can always serve its own video, so a picture
    from the wrong source beats the black player this replaced."""
    cam = _camera(**{CONF_STREAM_ENTITY: "camera.feeder_nvr"})
    with patch("kibble.camera.async_get_stream_source", AsyncMock(return_value=None)):
        assert await cam.stream_source() == DEVICE_URL


async def test_a_delegate_that_cannot_snapshot_falls_back_to_the_device() -> None:
    cam = _camera(**{CONF_STREAM_ENTITY: "camera.feeder_nvr"})
    with (
        patch("kibble.camera.async_get_camera_image", AsyncMock(return_value=None)),
        patch("kibble.camera.async_get_image", AsyncMock(return_value=b"jpeg")) as device_image,
    ):
        assert await cam.async_camera_image() == b"jpeg"
    assert device_image.await_args.args[1] == DEVICE_URL
