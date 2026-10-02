"""`views.py`'s `KibbleMediaView`: `store.resolve_asset_path`'s own path-safety rules (the
real check backing every dispatch below, not a view-local reimplementation), entry_id
resolution (unknown/unloaded entry -> 404), and the view's own 404/content-type/cache-header
behavior around it. Same duck-typed style as the rest of this suite -- a `SimpleNamespace`
stand-in for the aiohttp `Request` (just `.app[KEY_HASS]`, which is all `get()` reads off it)
rather than a real HTTP server; `test_image_view_http.py` covers the real router and route
pattern end to end.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.http import KEY_HASS
from homeassistant.config_entries import ConfigEntryState
from kibble.const import DOMAIN
from kibble.store import resolve_asset_path
from kibble.views import CACHE_CONTROL, KibbleMediaView

# --- store.resolve_asset_path: the path-safety KibbleMediaView actually relies on -------------


def test_resolve_asset_path_serves_a_legitimate_multi_segment_media_asset(tmp_path: Path) -> None:
    media = tmp_path / "media" / "2026-09-24"
    media.mkdir(parents=True)
    (media / "e1201-s1-body.jpg").write_bytes(b"jpeg")

    assert resolve_asset_path(tmp_path, "2026-09-24/e1201-s1-body.jpg") == media / "e1201-s1-body.jpg"


def test_resolve_asset_path_serves_a_training_asset_without_the_media_prefix(tmp_path: Path) -> None:
    training = tmp_path / "training" / "kitty"
    training.mkdir(parents=True)
    (training / "abc-body.jpg").write_bytes(b"jpeg")

    assert resolve_asset_path(tmp_path, "training/kitty/abc-body.jpg") == training / "abc-body.jpg"


@pytest.mark.parametrize(
    "asset_id",
    [
        "",
        "..",
        "../secrets.txt",
        "2026-09-24/../../../etc/passwd",
        "/etc/passwd",
        "a/../../b.jpg",
    ],
)
def test_resolve_asset_path_rejects_empty_traversal_and_absolute_ids(tmp_path: Path, asset_id: str) -> None:
    assert resolve_asset_path(tmp_path, asset_id) is None


# --- KibbleMediaView.get ------------------------------------------------------------------------


def _fake_request(hass: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(app={KEY_HASS: hass})


def _fake_hass(entry: SimpleNamespace | None) -> SimpleNamespace:
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=Mock(return_value=entry)),
        async_add_executor_job=AsyncMock(side_effect=lambda fn: fn()),
    )


def _fake_entry(store, state: ConfigEntryState = ConfigEntryState.LOADED) -> SimpleNamespace:
    return SimpleNamespace(domain=DOMAIN, state=state, runtime_data=SimpleNamespace(store=store))


def test_requires_auth_is_true() -> None:
    """The whole point of this view over `image.py`'s direct-URL entities: a card not on the
    feeder's LAN needs HA's own auth, and every asset is served from local disk regardless."""
    assert KibbleMediaView.requires_auth is True


async def test_get_404s_for_an_unknown_entry() -> None:
    view = KibbleMediaView()
    resp = await view.get(_fake_request(_fake_hass(None)), "bogus-entry-id", "a.jpg")
    assert resp.status == HTTPStatus.NOT_FOUND


async def test_get_404s_for_an_entry_not_currently_loaded() -> None:
    view = KibbleMediaView()
    entry = _fake_entry(store=Mock(), state=ConfigEntryState.SETUP_RETRY)
    resp = await view.get(_fake_request(_fake_hass(entry)), "e1", "a.jpg")
    assert resp.status == HTTPStatus.NOT_FOUND


async def test_get_404s_when_the_store_rejects_the_asset_id() -> None:
    """A traversal/absolute-path attempt: `store.asset_path` (== `resolve_asset_path`) already
    returned `None`, and the view never even tries to read a file."""
    view = KibbleMediaView()
    store = Mock(asset_path=Mock(return_value=None))
    entry = _fake_entry(store=store)

    resp = await view.get(_fake_request(_fake_hass(entry)), "e1", "../etc/passwd")

    assert resp.status == HTTPStatus.NOT_FOUND
    store.asset_path.assert_called_once_with("../etc/passwd")


async def test_get_404s_when_the_resolved_file_is_missing() -> None:
    """The store named a real path, but the file itself is gone (e.g. retention purged it
    between two requests) -- `FileNotFoundError` must 404, never propagate."""

    def _raise() -> bytes:
        raise FileNotFoundError()

    view = KibbleMediaView()
    entry = _fake_entry(store=Mock(asset_path=Mock(return_value=SimpleNamespace(read_bytes=_raise))))

    resp = await view.get(_fake_request(_fake_hass(entry)), "e1", "2026-09-24/gone.jpg")

    assert resp.status == HTTPStatus.NOT_FOUND


async def test_get_serves_the_resolved_bytes_with_jpeg_content_type_and_immutable_cache() -> None:
    view = KibbleMediaView()
    path = SimpleNamespace(read_bytes=lambda: b"\xff\xd8jpeg-bytes")
    entry = _fake_entry(store=Mock(asset_path=Mock(return_value=path)))

    resp = await view.get(_fake_request(_fake_hass(entry)), "e1", "2026-09-24/e1-s1-body.jpg")

    assert resp.status == HTTPStatus.OK
    assert resp.body == b"\xff\xd8jpeg-bytes"
    assert resp.content_type == "image/jpeg"
    assert resp.headers["Cache-Control"] == CACHE_CONTROL
