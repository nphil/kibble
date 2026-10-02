"""End-to-end coverage for `views.KibbleMediaView`: Home Assistant's own aiohttp router
(`HomeAssistantView.register()`, unmocked -- not `test_views.py`'s direct `.get()` calls)
serving real files off a real on-disk store root through the real `{asset:.+}` route pattern
and the real `store.resolve_asset_path` path-safety check, neither mocked.

Confirms the two claims `views.py`'s own module docstring makes: the route pattern captures a
multi-segment asset id (a date directory, a `training/<cat>/<file>` path) correctly, and a
literal `..` traversal attempt never reaches a file outside the store root -- whether that is
aiohttp's own URL normalization 404ing it before the view ever runs, or `resolve_asset_path`'s
own independent rejection, the observable result this file pins is the same either way: 404,
and the secret file's contents never come back.

Needs the project's own `.venv` (real `homeassistant`/`aiohttp`), like `test_media.py`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from homeassistant.components.http import KEY_AUTHENTICATED, KEY_HASS
from homeassistant.config_entries import ConfigEntryState
from kibble.const import DOMAIN
from kibble.store import resolve_asset_path
from kibble.views import CACHE_CONTROL, KibbleMediaView


@web.middleware
async def _authenticated(request: web.Request, handler):
    """Stands in for HA's own auth middleware, which this suite never wires up.
    `KibbleMediaView.requires_auth` itself is exercised on its own in `test_views.py`; this
    file is about routing and byte-serving."""
    request[KEY_AUTHENTICATED] = True
    return await handler(request)


@asynccontextmanager
async def _running_view(root: Path):
    """Wires a real `KibbleMediaView` onto a real aiohttp router (`HomeAssistantView.register()`,
    unmocked); `store.asset_path` is the real `resolve_asset_path` over `root`, not a mock of
    it -- yields an `aiohttp.test_utils.TestClient` to issue requests against."""
    entry = SimpleNamespace(
        domain=DOMAIN,
        state=ConfigEntryState.LOADED,
        runtime_data=SimpleNamespace(
            store=SimpleNamespace(asset_path=lambda asset_id: resolve_asset_path(root, asset_id))
        ),
    )
    hass = SimpleNamespace(
        is_stopping=False,
        async_add_executor_job=AsyncMock(side_effect=lambda fn: fn()),
        config_entries=SimpleNamespace(
            async_get_entry=Mock(side_effect=lambda entry_id: entry if entry_id == "e1" else None)
        ),
    )
    app = web.Application(middlewares=[_authenticated])
    app[KEY_HASS] = hass
    KibbleMediaView().register(hass, app, app.router)
    ha_client = TestClient(TestServer(app))
    await ha_client.start_server()
    try:
        yield ha_client
    finally:
        await ha_client.close()


async def test_a_legitimate_multi_segment_media_asset_serves_end_to_end(
    tmp_path: Path, socket_enabled: None
) -> None:
    """The route pattern (`{asset:.+}`) has to capture the whole rest of the path, slashes and
    all -- a date directory plus filename is the common shape for every archived crop."""
    media = tmp_path / "media" / "2026-09-24"
    media.mkdir(parents=True)
    (media / "e1201-s1-body.jpg").write_bytes(b"\xff\xd8real-jpeg-bytes")

    async with _running_view(tmp_path) as ha:
        resp = await ha.get("/api/kibble/e1/media/2026-09-24/e1201-s1-body.jpg")
        assert resp.status == HTTPStatus.OK
        assert await resp.read() == b"\xff\xd8real-jpeg-bytes"
        assert resp.headers["Cache-Control"] == CACHE_CONTROL


async def test_a_training_asset_serves_end_to_end_without_the_media_prefix(
    tmp_path: Path, socket_enabled: None
) -> None:
    training = tmp_path / "training" / "kitty"
    training.mkdir(parents=True)
    (training / "abc123-body.jpg").write_bytes(b"\xff\xd8training-crop")

    async with _running_view(tmp_path) as ha:
        resp = await ha.get("/api/kibble/e1/media/training/kitty/abc123-body.jpg")
        assert resp.status == HTTPStatus.OK
        assert await resp.read() == b"\xff\xd8training-crop"


async def test_a_traversal_attempt_never_reaches_a_file_outside_the_store_root(
    tmp_path: Path, socket_enabled: None
) -> None:
    """A secret file that genuinely exists just outside the store root must never be reachable
    through a `..` segment in the URL."""
    (tmp_path / "media").mkdir()
    secret = tmp_path.parent / f"secret-{tmp_path.name}.txt"
    secret.write_text("nope")
    try:
        async with _running_view(tmp_path) as ha:
            resp = await ha.get(f"/api/kibble/e1/media/../secret-{tmp_path.name}.txt")
            assert resp.status == HTTPStatus.NOT_FOUND
    finally:
        secret.unlink()


async def test_an_unknown_entry_id_404s_end_to_end(tmp_path: Path, socket_enabled: None) -> None:
    async with _running_view(tmp_path) as ha:
        resp = await ha.get("/api/kibble/bogus-entry-id/media/2026-09-24/a.jpg")
        assert resp.status == HTTPStatus.NOT_FOUND


async def test_a_well_formed_but_never_written_asset_404s_end_to_end(
    tmp_path: Path, socket_enabled: None
) -> None:
    (tmp_path / "media").mkdir()
    async with _running_view(tmp_path) as ha:
        resp = await ha.get("/api/kibble/e1/media/2026-09-24/never-written.jpg")
        assert resp.status == HTTPStatus.NOT_FOUND
