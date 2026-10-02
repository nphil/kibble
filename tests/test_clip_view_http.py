"""End-to-end coverage for `views.KibbleClipView`: Home Assistant's own aiohttp router
(`HomeAssistantView.register()`, unmocked -- same harness `test_image_view_http.py` established
for `KibbleMediaView`) proxying a real upstream server that stands in for Scrypted's Events
Recorder `videoclip` webhook, honouring `Range` exactly like the recorder's own source does
(`eating_clips.py`'s module docstring: 206 with `Content-Range`/`Accept-Ranges`/`Content-Length`
on a ranged request, 200 with `Content-Length` otherwise, a plain HTTP 400 -- not 404 -- when
the file is gone).

`coordinator`/`store` are thin fakes (this file is about the view's routing, status codes and
Range passthrough, not `store.py`'s own clip-resolution logic -- that is `test_store.py`'s).
`async_get_clientsession` is monkeypatched to a real, test-owned `aiohttp.ClientSession` so the
view's outbound request actually reaches the fake upstream server over a real loopback socket,
without needing to fake Home Assistant's own client-session cache.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from homeassistant.components.http import KEY_AUTHENTICATED, KEY_HASS
from homeassistant.config_entries import ConfigEntryState

from kibble import views as views_module
from kibble.const import CONF_SCRYPTED_CLIPS_URL, DOMAIN
from kibble.views import KibbleClipView

ENTRY_ID = "e1"
LINKED_UID = "e1-e1201-100"
CLIP_BYTES = b"fake-mp4-bytes-0123456789"  # 26 bytes -- long enough to slice a real mid-range out of


@web.middleware
async def _authenticated(request: web.Request, handler):
    """Stands in for HA's own auth middleware, which this suite never wires up."""
    request[KEY_AUTHENTICATED] = True
    return await handler(request)


async def _fake_videoclip(request: web.Request) -> web.Response:
    """Mirrors the real recorder's own `videoclip` webhook (`eating_clips.py`'s source-derived
    module doc): `filename == "missing.mp4"` simulates a clip Scrypted's quota cleanup already
    deleted -- a plain 400, not a 404, exactly like the real recorder's uncaught-`ENOENT`
    fallthrough. Everything else serves `CLIP_BYTES`, ranged or whole."""
    params = json.loads(request.query["params"])
    if params.get("deviceId") != "240":
        return web.Response(status=400, text="unexpected deviceId")
    if params.get("filename") == "missing.mp4":
        return web.Response(status=400, text='{"error":"ENOENT"}')
    total = len(CLIP_BYTES)
    range_header = request.headers.get("Range")
    if range_header:
        start_s, _, end_s = range_header.removeprefix("bytes=").partition("-")
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
        chunk = CLIP_BYTES[start : end + 1]
        return web.Response(
            status=206,
            body=chunk,
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(chunk)),
                "Content-Type": "video/mp4",
            },
        )
    return web.Response(
        status=200, body=CLIP_BYTES, headers={"Content-Length": str(total), "Content-Type": "video/mp4"}
    )


@asynccontextmanager
async def _running_upstream():
    app = web.Application()
    app.router.add_get("/endpoint/@apocaliss92/scrypted-events-recorder/public/videoclip", _fake_videoclip)
    server = TestServer(app)
    await server.start_server()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        await server.close()


class _FakeStore:
    """Resolves exactly `LINKED_UID` to one clip; every other uid is unlinked -- the view's own
    `resolve_event_clip` gate against serving an arbitrary id (no SSRF)."""

    def __init__(self, clip_id: str = "1758812345678_1758812400123_0001000000.mp4") -> None:
        self._clip_id = clip_id
        self.cleared: list[str] = []

    async def async_resolve_event_clip(self, uid: str) -> dict[str, object] | None:
        if uid != LINKED_UID:
            return None
        return {"owner_uid": LINKED_UID, "clip_id": self._clip_id, "start_ms": 90_000, "end_ms": 175_000}

    async def async_clear_event_clip(self, uid: str) -> None:
        self.cleared.append(uid)


@asynccontextmanager
async def _running_view(monkeypatch: pytest.MonkeyPatch, store: _FakeStore, *, clips_url: str | None):
    """Wires a real `KibbleClipView` onto a real aiohttp router. `clips_url=None` reproduces
    the feature-off configuration (empty option); anything else is the configured Scrypted
    origin the view should proxy against."""
    coordinator = SimpleNamespace(
        entry=SimpleNamespace(options={CONF_SCRYPTED_CLIPS_URL: clips_url} if clips_url is not None else {}),
        store=store,
    )
    entry = SimpleNamespace(domain=DOMAIN, state=ConfigEntryState.LOADED, runtime_data=coordinator)
    hass = SimpleNamespace(
        is_stopping=False,
        config_entries=SimpleNamespace(
            async_get_entry=Mock(side_effect=lambda entry_id: entry if entry_id == ENTRY_ID else None)
        ),
    )
    session = aiohttp.ClientSession()
    monkeypatch.setattr(views_module, "async_get_clientsession", lambda _hass: session)
    app = web.Application(middlewares=[_authenticated])
    app[KEY_HASS] = hass
    KibbleClipView().register(hass, app, app.router)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
        await session.close()


async def test_an_unlinked_event_404s_and_never_reaches_upstream(
    monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    store = _FakeStore()
    async with _running_upstream() as upstream_url:
        async with _running_view(monkeypatch, store, clips_url=upstream_url) as ha:
            resp = await ha.get(f"/api/kibble/{ENTRY_ID}/clip/some-other-uid")
            assert resp.status == HTTPStatus.NOT_FOUND
    assert store.cleared == []


async def test_an_unknown_entry_id_404s(monkeypatch: pytest.MonkeyPatch, socket_enabled: None) -> None:
    store = _FakeStore()
    async with _running_view(monkeypatch, store, clips_url="http://198.51.100.1:11080") as ha:
        resp = await ha.get(f"/api/kibble/bogus/clip/{LINKED_UID}")
        assert resp.status == HTTPStatus.NOT_FOUND


async def test_the_feature_off_with_no_clips_url_404s_even_for_a_linked_event(
    monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    store = _FakeStore()
    async with _running_view(monkeypatch, store, clips_url=None) as ha:
        resp = await ha.get(f"/api/kibble/{ENTRY_ID}/clip/{LINKED_UID}")
        assert resp.status == HTTPStatus.NOT_FOUND


async def test_a_whole_file_request_with_no_range_header_gets_200_with_every_byte(
    monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    store = _FakeStore()
    async with _running_upstream() as upstream_url:
        async with _running_view(monkeypatch, store, clips_url=upstream_url) as ha:
            resp = await ha.get(f"/api/kibble/{ENTRY_ID}/clip/{LINKED_UID}")
            assert resp.status == HTTPStatus.OK
            assert await resp.read() == CLIP_BYTES
            assert resp.headers["Content-Type"] == "video/mp4"
            assert resp.headers["Content-Length"] == str(len(CLIP_BYTES))
            assert resp.headers["Accept-Ranges"] == "bytes"


async def test_a_range_request_gets_206_with_the_exact_slice_and_content_range(
    monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    store = _FakeStore()
    async with _running_upstream() as upstream_url:
        async with _running_view(monkeypatch, store, clips_url=upstream_url) as ha:
            resp = await ha.get(f"/api/kibble/{ENTRY_ID}/clip/{LINKED_UID}", headers={"Range": "bytes=5-10"})
            assert resp.status == HTTPStatus.PARTIAL_CONTENT
            assert await resp.read() == CLIP_BYTES[5:11]
            assert resp.headers["Content-Range"] == f"bytes 5-10/{len(CLIP_BYTES)}"
            assert resp.headers["Content-Length"] == "6"
            assert resp.headers["Accept-Ranges"] == "bytes"


async def test_an_open_ended_range_request_gets_everything_from_the_offset_to_the_end(
    monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    store = _FakeStore()
    async with _running_upstream() as upstream_url:
        async with _running_view(monkeypatch, store, clips_url=upstream_url) as ha:
            resp = await ha.get(f"/api/kibble/{ENTRY_ID}/clip/{LINKED_UID}", headers={"Range": "bytes=20-"})
            assert resp.status == HTTPStatus.PARTIAL_CONTENT
            assert await resp.read() == CLIP_BYTES[20:]


async def test_a_clip_scrypted_no_longer_has_404s_and_clears_the_link(
    monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    """The recorder answers a missing file with a plain 400 (confirmed against its own source,
    `eating_clips.py`'s module doc) -- proves that non-404 upstream status is still treated as
    "gone", and that the link is cleared via the resolved OWNER uid, not the requested one."""
    store = _FakeStore(clip_id="missing.mp4")
    async with _running_upstream() as upstream_url:
        async with _running_view(monkeypatch, store, clips_url=upstream_url) as ha:
            resp = await ha.get(f"/api/kibble/{ENTRY_ID}/clip/{LINKED_UID}")
            assert resp.status == HTTPStatus.NOT_FOUND
    assert store.cleared == [LINKED_UID]


async def test_an_unreachable_upstream_404s_without_raising(
    monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    store = _FakeStore()
    # Nothing is listening on this port -- a closed-connection failure, not a fake 4xx/5xx.
    async with _running_view(monkeypatch, store, clips_url="http://127.0.0.1:1") as ha:
        resp = await ha.get(f"/api/kibble/{ENTRY_ID}/clip/{LINKED_UID}")
        assert resp.status == HTTPStatus.NOT_FOUND
