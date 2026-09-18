"""End-to-end coverage for `views.KibbleImageView`: Home Assistant's own aiohttp router
(`HomeAssistantView.register()`, unmocked -- not `test_views.py`'s direct `.get()` calls) driving
a real `KibbleClient` against a fake feeder HTTP server standing in for the on-device agent. This
is the level the 2026-09-18 "every kind 404s" report needed proving at: routing itself was never
the bug (`pending` already worked end to end before this file existed; kept here as its
regression guard); `feed` did not work -- LibreFeed's dish-snapshot bytes are already a real
JPEG, but the view used to force every one of them through an H.264-only ffmpeg decode that
chokes on real JPEG input; `sample` did not work either -- LibreFeed's daemon had no
`/faces/samples/<cat>/<name>` route to answer at all, a gap this change also closes
(`librefeed/daemon/src/faces.rs`/`main.rs`, proven by that project's own `cargo test`, not
reachable from here). This file proves the *view* behaves correctly against a feeder that lacks
the route (today) and one that has it (after that daemon fix is deployed).

Needs the project's own `.venv` (real `homeassistant`/`aiohttp`), like `test_media.py`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from homeassistant.components.ffmpeg import DATA_FFMPEG
from homeassistant.components.http import KEY_AUTHENTICATED, KEY_HASS
from homeassistant.config_entries import ConfigEntryState
from kibble.api import KibbleClient
from kibble.const import CONF_HOST, CONF_PORT, DOMAIN
from kibble.views import KibbleImageView

# A minimal, but genuinely magic-byte-valid, JPEG-shaped payload -- `_is_jpeg` (image.py) keys
# off exactly this SOI marker, so this fixture must actually carry it, unlike `test_views.py`'s
# illustrative `b"\xff\xd8jpeg-bytes"` (which does too, coincidentally: both start `\xff\xd8`).
_JPEG = b"\xff\xd8\xff\xe0fake-but-magic-byte-valid-jpeg"


def _fake_feeder_app(*, sample_route_exists: bool) -> web.Application:
    """Stands in for the on-device agent so this suite can prove the real bug -- LibreFeed
    serving a real JPEG where the view assumed raw H.264, and LibreFeed lacking
    `/faces/samples/<cat>/<name>` altogether -- without a live device or the real Rust daemon.
    `sample_route_exists=False` reproduces LibreFeed as of the 2026-09-18 report;
    `sample_route_exists=True` reproduces it once the daemon fix in this same change is built
    and deployed."""
    app = web.Application()

    async def _jpeg(_request: web.Request) -> web.Response:
        return web.Response(body=_JPEG, content_type="image/jpeg")

    app.router.add_get("/faces/pending/{name}", _jpeg)
    app.router.add_get("/feeds/{name}", _jpeg)  # daemon/src/feeds.rs::read_feed_file
    if sample_route_exists:
        app.router.add_get("/faces/samples/{cat}/{name}", _jpeg)
    return app


@web.middleware
async def _authenticated(request: web.Request, handler):
    """Stands in for HA's own auth middleware, which this suite never wires up.
    `KibbleImageView.requires_auth` itself is exercised on its own in `test_views.py`; this file
    is about routing and byte-fetching."""
    request[KEY_AUTHENTICATED] = True
    return await handler(request)


@asynccontextmanager
async def _running_view(*, sample_route_exists: bool = True):
    """Wires a real `KibbleImageView` onto a real aiohttp router (`HomeAssistantView.register()`,
    unmocked) backed by a real `KibbleClient` talking to a fake feeder over a real socket --
    yields an `aiohttp.test_utils.TestClient` to issue requests against."""
    feeder = TestServer(_fake_feeder_app(sample_route_exists=sample_route_exists))
    await feeder.start_server()
    session = aiohttp.ClientSession()
    client = KibbleClient(session, feeder.host, feeder.port)
    entry = SimpleNamespace(
        domain=DOMAIN,
        state=ConfigEntryState.LOADED,
        data={CONF_HOST: feeder.host, CONF_PORT: feeder.port},
        runtime_data=SimpleNamespace(client=client),
    )
    hass = SimpleNamespace(
        is_stopping=False,
        # Just enough for `get_ffmpeg_manager` (image.py's `_h264_keyframe_to_jpeg`, reached
        # whenever a fetch isn't already a JPEG) to find a real `ffmpeg` binary on PATH --
        # bootstrapping the whole `ffmpeg` component is unnecessary for what this file tests.
        data={DATA_FFMPEG: SimpleNamespace(binary="ffmpeg")},
        config_entries=SimpleNamespace(
            async_get_entry=Mock(side_effect=lambda entry_id: entry if entry_id == "e1" else None)
        ),
    )
    app = web.Application(middlewares=[_authenticated])
    app[KEY_HASS] = hass
    KibbleImageView().register(hass, app, app.router)
    ha_client = TestClient(TestServer(app))
    await ha_client.start_server()
    try:
        yield ha_client
    finally:
        await ha_client.close()
        await session.close()
        await feeder.close()


async def test_pending_kind_serves_real_bytes_end_to_end() -> None:
    """Already worked before this change -- the "every kind 404s" report's routing candidates
    (async_setup not running, the route pattern not matching a 6-segment path, entry lookup
    failing) all check out fine here. Kept as this file's regression guard for the one kind that
    never had a bug."""
    async with _running_view() as ha:
        resp = await ha.get("/api/kibble/e1/image/pending/1789752794-unknown.jpg")
        assert resp.status == HTTPStatus.OK
        assert resp.content_type == "image/jpeg"
        assert await resp.read() == _JPEG


async def test_feed_kind_serves_librefeeds_real_jpeg_end_to_end() -> None:
    """The actual reported bug, reproduced through the real router and a real `KibbleClient`:
    `views.py` used to force every `feed` fetch through an H.264-only ffmpeg decode, which fails
    against LibreFeed's real JPEG bytes (confirmed against the live device) and 404s. Runs a real
    ffmpeg subprocess against pre-fix code -- and genuinely fails, not merely because a binary is
    missing -- and no ffmpeg at all post-fix, since the magic-byte check short-circuits it."""
    async with _running_view() as ha:
        resp = await ha.get("/api/kibble/e1/image/feed/1789752779-sched-test-chime-2026-09-18-before.jpg")
        assert resp.status == HTTPStatus.OK
        assert await resp.read() == _JPEG


async def test_sample_kind_404s_end_to_end_while_librefeed_lacks_the_route() -> None:
    """Today: LibreFeed's daemon has no `/faces/samples/<cat>/<name>` route at all, so the agent
    itself answers with a generic route-not-found 404 -- correctly surfaced by `_get_bytes`'s
    existing `KibbleNotFoundError` mapping. This is the real, current LibreFeed shape; it is not
    itself a view bug, which is exactly why the daemon needed the fix, not this file."""
    async with _running_view(sample_route_exists=False) as ha:
        resp = await ha.get("/api/kibble/e1/image/sample/Kitty/1789752797-unknown.jpg")
        assert resp.status == HTTPStatus.NOT_FOUND


async def test_sample_kind_serves_real_bytes_end_to_end_once_librefeed_has_the_route() -> None:
    """Once the daemon fix in this same change (`librefeed/daemon/src/faces.rs`/`main.rs`,
    proven separately by `cargo test`) is built and deployed, the identical request succeeds --
    proving the view's own `sample` dispatch (`client.sample_bytes(cat, name)`, already correct
    before this change) was never the missing piece."""
    async with _running_view(sample_route_exists=True) as ha:
        resp = await ha.get("/api/kibble/e1/image/sample/Kitty/1789752797-unknown.jpg")
        assert resp.status == HTTPStatus.OK
        assert await resp.read() == _JPEG


async def test_unknown_entry_id_404s_end_to_end() -> None:
    async with _running_view() as ha:
        resp = await ha.get("/api/kibble/bogus-entry-id/image/pending/a.jpg")
        assert resp.status == HTTPStatus.NOT_FOUND

