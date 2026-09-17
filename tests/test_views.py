"""`views.py`'s `KibbleImageView`: name/cat path-safety, entry_id resolution, per-`kind`
dispatch (including the `feed` kind's ffmpeg-decode reuse and the `track` kind's numeric-name
requirement), auth requirement, cache headers, and 404/502 mapping. Same duck-typed style as
the rest of this suite -- a `SimpleNamespace` stand-in for the aiohttp `Request` (just
`.app[KEY_HASS]`, which is all `get()` reads off it) rather than a real HTTP server.
"""

from __future__ import annotations

import time
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.http import KEY_HASS
from homeassistant.config_entries import ConfigEntryState
from kibble.api import KibbleConnectionError, KibbleNotFoundError
from kibble.const import DOMAIN
from kibble.views import CACHE_CONTROL, KibbleImageView, _is_safe_name

# --- _is_safe_name -------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["1789580000-101321488.jpg", "a.jpg", "Kitty"])
def test_is_safe_name_accepts_ordinary_names(name: str) -> None:
    assert _is_safe_name(name) is True


@pytest.mark.parametrize("name", ["", "..", "../../etc/passwd", "a/b.jpg", "a\\b.jpg", ".hidden"])
def test_is_safe_name_rejects_traversal_and_hidden_names(name: str) -> None:
    assert _is_safe_name(name) is False


# --- KibbleImageView.get ----------------------------------------------------------------------


def _fake_request(hass: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(app={KEY_HASS: hass})


def _fake_hass(entry: SimpleNamespace | None) -> SimpleNamespace:
    return SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=Mock(return_value=entry)))


def _fake_entry(client, state=ConfigEntryState.LOADED) -> SimpleNamespace:
    return SimpleNamespace(domain=DOMAIN, state=state, runtime_data=SimpleNamespace(client=client))


def test_requires_auth_is_true() -> None:
    """The whole point of this view over `image.py`'s existing direct-URL entities: a card not
    on the feeder's LAN needs HA's own auth, not the agent's (nonexistent) auth."""
    assert KibbleImageView.requires_auth is True


async def test_get_404s_for_an_unknown_entry() -> None:
    view = KibbleImageView()
    request = _fake_request(_fake_hass(None))
    resp = await view.get(request, entry_id="bogus", name="a.jpg", kind="event")
    assert resp.status == HTTPStatus.NOT_FOUND


async def test_get_404s_for_an_entry_not_currently_loaded() -> None:
    view = KibbleImageView()
    entry = _fake_entry(AsyncMock(), state=ConfigEntryState.NOT_LOADED)
    resp = await view.get(_fake_request(_fake_hass(entry)), entry_id="e1", name="a.jpg", kind="event")
    assert resp.status == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("name", ["..", "../x.jpg", "a/b.jpg"])
async def test_get_404s_for_an_unsafe_name(name: str) -> None:
    view = KibbleImageView()
    entry = _fake_entry(AsyncMock())
    resp = await view.get(_fake_request(_fake_hass(entry)), entry_id="e1", name=name, kind="event")
    assert resp.status == HTTPStatus.NOT_FOUND


async def test_get_404s_for_an_unsafe_cat() -> None:
    view = KibbleImageView()
    entry = _fake_entry(AsyncMock())
    resp = await view.get(
        _fake_request(_fake_hass(entry)), entry_id="e1", name="a.jpg", kind="sample", cat=".."
    )
    assert resp.status == HTTPStatus.NOT_FOUND


async def test_get_404s_for_an_unknown_kind() -> None:
    view = KibbleImageView()
    entry = _fake_entry(AsyncMock())
    resp = await view.get(_fake_request(_fake_hass(entry)), entry_id="e1", name="a.jpg", kind="bogus")
    assert resp.status == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(
    ("kind", "client_attr", "extra_kwargs"),
    [
        ("event", "event_bytes", {}),
        ("pending", "pending_bytes", {}),
    ],
)
async def test_get_serves_a_passthrough_jpeg_with_the_right_content_type_and_cache_header(
    kind: str, client_attr: str, extra_kwargs: dict
) -> None:
    client = AsyncMock()
    getattr(client, client_attr).return_value = b"\xff\xd8jpeg-bytes"
    entry = _fake_entry(client)
    resp = await view_get(entry, kind=kind, **extra_kwargs)
    assert resp.status == HTTPStatus.OK
    assert resp.content_type == "image/jpeg"
    assert resp.headers["Cache-Control"] == CACHE_CONTROL
    assert resp.body == b"\xff\xd8jpeg-bytes"
    getattr(client, client_attr).assert_awaited_once_with("a.jpg")


async def view_get(
    entry: SimpleNamespace, *, kind: str, cat: str | None = None, name: str = "a.jpg"
):
    view = KibbleImageView()
    kwargs = {"entry_id": "e1", "name": name, "kind": kind}
    if cat is not None:
        kwargs["cat"] = cat
    return await view.get(_fake_request(_fake_hass(entry)), **kwargs)


async def test_get_track_kind_calls_track_image_bytes_with_ts_as_int() -> None:
    client = AsyncMock(track_image_bytes=AsyncMock(return_value=b"\xff\xd8live-jpeg"))
    entry = _fake_entry(client)
    resp = await view_get(entry, kind="track", name="1789528799")
    assert resp.status == HTTPStatus.OK
    assert resp.content_type == "image/jpeg"
    assert resp.headers["Cache-Control"] == CACHE_CONTROL
    assert resp.body == b"\xff\xd8live-jpeg"
    client.track_image_bytes.assert_awaited_once_with(1789528799)


async def test_get_track_kind_404s_for_a_non_numeric_name() -> None:
    client = AsyncMock()
    entry = _fake_entry(client)
    resp = await view_get(entry, kind="track", name="not-a-timestamp")
    assert resp.status == HTTPStatus.NOT_FOUND
    client.track_image_bytes.assert_not_awaited()


async def test_get_404s_when_no_track_image_is_paired() -> None:
    client = AsyncMock(track_image_bytes=AsyncMock(side_effect=KibbleNotFoundError("gone")))
    entry = _fake_entry(client)
    resp = await view_get(entry, kind="track", name="123")
    assert resp.status == HTTPStatus.NOT_FOUND


async def test_get_track_kind_does_not_cache_before_the_pairing_window_settles() -> None:
    """A `track` image's pairing (`websocket._track_pair`) can still change until
    `ts + TRACK_PAIR_LOOKAHEAD_SECONDS`: a later, closer `eat`/`visit` recorded after this
    exact request could still join the window and become the new answer for the same `ts`.
    Caching it as immutable this early would let a browser keep serving a stale pairing
    forever, even once the agent itself would answer differently."""
    client = AsyncMock(track_image_bytes=AsyncMock(return_value=b"\xff\xd8live-jpeg"))
    entry = _fake_entry(client)
    recent_ts = int(time.time())
    resp = await view_get(entry, kind="track", name=str(recent_ts))
    assert resp.status == HTTPStatus.OK
    assert resp.headers["Cache-Control"] == "no-store"


async def test_get_sample_kind_calls_the_client_with_both_cat_and_name() -> None:
    client = AsyncMock(sample_bytes=AsyncMock(return_value=b"sample-jpeg"))
    entry = _fake_entry(client)
    resp = await view_get(entry, kind="sample", cat="Kitty")
    assert resp.status == HTTPStatus.OK
    client.sample_bytes.assert_awaited_once_with("Kitty", "a.jpg")


async def test_get_feed_kind_reuses_the_ffmpeg_h264_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    decode = AsyncMock(return_value=b"decoded-jpeg")
    monkeypatch.setattr("kibble.views._h264_keyframe_to_jpeg", decode)
    monkeypatch.setattr("kibble.views._feed_snapshot_url", lambda entry, name: f"http://x/feeds/{name}")
    client = AsyncMock()
    entry = _fake_entry(client)
    resp = await view_get(entry, kind="feed")
    assert resp.status == HTTPStatus.OK
    assert resp.body == b"decoded-jpeg"
    decode.assert_awaited_once()
    client.event_bytes.assert_not_awaited()


async def test_get_feed_kind_404s_when_ffmpeg_decode_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_h264_keyframe_to_jpeg` reports a decode failure as `None`, not an exception."""
    monkeypatch.setattr("kibble.views._h264_keyframe_to_jpeg", AsyncMock(return_value=None))
    monkeypatch.setattr("kibble.views._feed_snapshot_url", lambda entry, name: "http://x/feeds/a.jpg")
    entry = _fake_entry(AsyncMock())
    resp = await view_get(entry, kind="feed")
    assert resp.status == HTTPStatus.NOT_FOUND


async def test_get_404s_when_the_client_reports_the_crop_is_gone() -> None:
    client = AsyncMock(event_bytes=AsyncMock(side_effect=KibbleNotFoundError("gone")))
    entry = _fake_entry(client)
    resp = await view_get(entry, kind="event")
    assert resp.status == HTTPStatus.NOT_FOUND


async def test_get_502s_when_the_feeder_is_unreachable() -> None:
    client = AsyncMock(event_bytes=AsyncMock(side_effect=KibbleConnectionError("down")))
    entry = _fake_entry(client)
    resp = await view_get(entry, kind="event")
    assert resp.status == HTTPStatus.BAD_GATEWAY
