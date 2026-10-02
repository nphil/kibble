"""End-to-end coverage for `views.KibbleUploadTrainingView`/`KibbleUploadAvatarView`: Home
Assistant's own aiohttp router (`HomeAssistantView.register()`, unmocked -- same harness
`test_image_view_http.py` established for `KibbleMediaView`), a real `_SyncStore` rooted on
disk, and real multipart bodies built with `aiohttp.FormData`.

Covers the acceptance list this feature was specified against: an oversize request is rejected,
a non-image file is rejected, a pixel bomb is rejected, and one bad file in a batch leaves the
others intact (proven here at the HTTP layer, where the one-request-per-file design makes it
true by construction -- there is no shared transaction for a bad file to poison). EXIF-rotation
and quality/downscale behaviour are `media_processing.py`'s own unit tests
(`test_media_processing.py`); this file is about the view's routing, status codes and gates.
"""

from __future__ import annotations

import asyncio
import io
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from homeassistant.components.http import KEY_AUTHENTICATED, KEY_HASS
from homeassistant.config_entries import ConfigEntryState
from PIL import Image

from kibble import identity, media_processing
from kibble.const import DOMAIN
from kibble.store import _SyncStore
from kibble.views import KibbleUploadAvatarView, KibbleUploadTrainingView

ENTRY_ID = "e1"


def _jpeg_bytes(color: tuple[int, int, int], *, size: int = 200, seed: int = 0) -> bytes:
    """A real, decodable, non-blank photo-ish JPEG. Multi-octave texture, not per-pixel iid
    noise: a real photo's texture (fur, edges) survives `identity.is_blank_image`'s 32x32
    downsample probe because it has spatial structure at more than one scale; pure per-pixel
    noise, tried first here, does not -- it averages toward flat under that same downsample,
    which would make this fixture read as blank for reasons that say nothing about real photos."""
    rng = np.random.default_rng(seed)
    base = np.full((size, size, 3), color, dtype=np.float64)
    for scale in (4, 8, 16, 32):
        small = rng.normal(0, 40, size=(max(size // scale, 1), max(size // scale, 1), 3))
        layer = np.array(
            Image.fromarray(np.clip(small + 128, 0, 255).astype(np.uint8)).resize((size, size), Image.BILINEAR),
            dtype=np.float64,
        ) - 128
        base += layer / 3
    arr = np.clip(base, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _blank_jpeg(size: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (200, 200, 200)).save(buf, format="JPEG")
    return buf.getvalue()


class _RealAsyncStore:
    """Delegates every call straight to a real `_SyncStore`, the same "no mock of the actual
    logic" pattern `test_ingest.py`'s own `_RealAsyncStore` uses -- caps, dedupe and avatar
    writes in these tests are the real implementation, not a stand-in for it."""

    def __init__(self, sync: _SyncStore) -> None:
        self._sync = sync

    async def async_cat_exists(self, name: str) -> bool:
        return self._sync.cat_exists(name)

    async def async_free_disk_bytes(self) -> int:
        return self._sync.free_disk_bytes()

    async def async_training_feats_for_cat(self, cat: str, mode: str | None):
        return self._sync.training_feats_for_cat(cat, mode)

    async def async_add_upload_training(self, **kwargs):
        return self._sync.add_upload_training(**kwargs)

    async def async_set_cat_avatar(self, cat: str, data: bytes):
        return self._sync.set_cat_avatar(cat, data)


class _FakeEngine:
    def __init__(self, verdict: identity.Verdict | None = None) -> None:
        self._verdict = verdict

    async def async_classify_one(self, features: identity.Features) -> identity.Verdict | None:
        return self._verdict

    async def async_rebuild(self) -> None:
        pass

    async def async_reclassify_unreviewed(self, cutoff: int) -> None:
        pass


@web.middleware
async def _authenticated(request: web.Request, handler):
    request[KEY_AUTHENTICATED] = True
    return await handler(request)


@asynccontextmanager
async def _running_upload_views(sync_store: _SyncStore, *, engine: _FakeEngine | None = None):
    """Wires both real upload views onto a real aiohttp router; `coordinator.store` is a thin
    async wrapper over a real, on-disk `_SyncStore` (`_RealAsyncStore` above)."""
    coordinator = SimpleNamespace(
        store=_RealAsyncStore(sync_store),
        engine=engine or _FakeEngine(),
        retention_cutoff=lambda: 0,
        async_refresh_identity_snapshot=AsyncMock(),
    )
    entry = SimpleNamespace(domain=DOMAIN, state=ConfigEntryState.LOADED, runtime_data=coordinator)
    hass = SimpleNamespace(
        is_stopping=False,
        async_add_executor_job=AsyncMock(side_effect=lambda fn, *a: fn(*a)),
        async_create_task=lambda coro: asyncio.ensure_future(coro),
        config_entries=SimpleNamespace(
            async_get_entry=Mock(side_effect=lambda entry_id: entry if entry_id == ENTRY_ID else None)
        ),
    )
    app = web.Application(middlewares=[_authenticated])
    app[KEY_HASS] = hass
    KibbleUploadTrainingView().register(hass, app, app.router)
    KibbleUploadAvatarView().register(hass, app, app.router)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def _store(tmp_path: Path, *cats: str) -> _SyncStore:
    store = _SyncStore(tmp_path, ENTRY_ID)
    for cat in cats:
        store.add_cat(cat)
    return store


def _form(data: bytes, filename: str = "photo.jpg") -> FormData:
    form = FormData()
    form.add_field("file", data, filename=filename, content_type="application/octet-stream")
    return form


# --- training upload: happy path, no-cat, duplicate, unknown cat ------------------------------


async def test_a_real_photo_is_added_as_an_upload_sourced_training_row(tmp_path: Path, socket_enabled: None) -> None:
    store = _store(tmp_path, "Kitty")
    async with _running_upload_views(store) as client:
        resp = await client.post("/api/kibble/e1/upload/training/Kitty", data=_form(_jpeg_bytes((180, 90, 40))))
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["status"] == "added"
        assert body["sample"]["cat"] == "Kitty"
    rows = store.conn.execute("SELECT source FROM training WHERE cat='Kitty'").fetchall()
    assert [r["source"] for r in rows] == ["upload"]


async def test_a_blank_photo_is_rejected_as_no_cat(tmp_path: Path, socket_enabled: None) -> None:
    store = _store(tmp_path, "Kitty")
    async with _running_upload_views(store) as client:
        resp = await client.post("/api/kibble/e1/upload/training/Kitty", data=_form(_blank_jpeg()))
        assert resp.status == HTTPStatus.OK
        assert (await resp.json())["status"] == "no_cat"
    assert store.conn.execute("SELECT 1 FROM training").fetchone() is None


async def test_a_not_a_cat_verdict_from_the_trained_model_is_also_rejected_as_no_cat(
    tmp_path: Path, socket_enabled: None
) -> None:
    """The second half of the no-cat gate: once there is a trained model, its own `not_a_cat`
    verdict rejects an upload even though the baseline blank-image heuristic alone would not."""
    store = _store(tmp_path, "Kitty")
    engine = _FakeEngine(verdict=identity.Verdict(label=identity.NOT_A_CAT, confidence=0.9))
    async with _running_upload_views(store, engine=engine) as client:
        resp = await client.post("/api/kibble/e1/upload/training/Kitty", data=_form(_jpeg_bytes((90, 140, 60))))
        assert (await resp.json())["status"] == "no_cat"
    assert store.conn.execute("SELECT 1 FROM training").fetchone() is None


async def test_a_near_duplicate_of_an_already_trained_photo_is_rejected(tmp_path: Path, socket_enabled: None) -> None:
    store = _store(tmp_path, "Kitty")
    photo = _jpeg_bytes((180, 90, 40), seed=7)
    async with _running_upload_views(store) as client:
        first = await client.post("/api/kibble/e1/upload/training/Kitty", data=_form(photo))
        assert (await first.json())["status"] == "added"
        second = await client.post("/api/kibble/e1/upload/training/Kitty", data=_form(photo))
        assert (await second.json())["status"] == "duplicate"
    rows = store.conn.execute("SELECT COUNT(*) AS n FROM training WHERE cat='Kitty'").fetchone()
    assert rows["n"] == 1


async def test_uploading_to_an_unknown_cat_404s(tmp_path: Path, socket_enabled: None) -> None:
    store = _store(tmp_path)  # no cats enrolled
    async with _running_upload_views(store) as client:
        resp = await client.post("/api/kibble/e1/upload/training/Nope", data=_form(_jpeg_bytes((1, 2, 3))))
        assert resp.status == HTTPStatus.NOT_FOUND


# --- acceptance list: oversize / non-image / pixel bomb / one-bad-file-leaves-others-intact ---


async def test_an_oversize_request_is_rejected(tmp_path: Path, socket_enabled: None) -> None:
    store = _store(tmp_path, "Kitty")
    oversize = b"\xff\xd8" + b"0" * (media_processing.MAX_UPLOAD_BYTES + 1024)
    async with _running_upload_views(store) as client:
        resp = await client.post("/api/kibble/e1/upload/training/Kitty", data=_form(oversize))
        assert resp.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        body = await resp.json()
        assert body["status"] == "error" and body["reason"] == "too_large"
    assert store.conn.execute("SELECT 1 FROM training").fetchone() is None


async def test_a_non_image_file_is_rejected(tmp_path: Path, socket_enabled: None) -> None:
    store = _store(tmp_path, "Kitty")
    async with _running_upload_views(store) as client:
        resp = await client.post(
            "/api/kibble/e1/upload/training/Kitty", data=_form(b"this is plainly not an image file")
        )
        assert resp.status == HTTPStatus.BAD_REQUEST
        body = await resp.json()
        assert body["status"] == "error" and body["reason"] == "bad_image"
    assert store.conn.execute("SELECT 1 FROM training").fetchone() is None


async def test_a_pixel_bomb_is_rejected(tmp_path: Path, socket_enabled: None) -> None:
    """An 80-megapixel declared image -- over `media_processing.MAX_MEGAPIXELS` but under
    Pillow's own default decompression-bomb threshold, so this specifically proves Kibble's own
    guard fires, not just Pillow's."""
    store = _store(tmp_path, "Kitty")
    bomb = _crafted_oversized_png(10000, 8000)
    async with _running_upload_views(store) as client:
        resp = await client.post("/api/kibble/e1/upload/training/Kitty", data=_form(bomb))
        assert resp.status == HTTPStatus.BAD_REQUEST
        body = await resp.json()
        assert body["status"] == "error" and body["reason"] == "too_large"
    assert store.conn.execute("SELECT 1 FROM training").fetchone() is None


async def test_one_bad_file_in_a_batch_leaves_the_others_intact(tmp_path: Path, socket_enabled: None) -> None:
    """The card uploads a batch as independent requests (bounded concurrency, never one shared
    multi-file request) precisely so a bad file can never abort the others -- proven here by
    interleaving a garbage upload between two good ones and checking the store afterward."""
    store = _store(tmp_path, "Kitty")
    async with _running_upload_views(store) as client:
        r1 = await client.post("/api/kibble/e1/upload/training/Kitty", data=_form(_jpeg_bytes((180, 90, 40), seed=1)))
        assert (await r1.json())["status"] == "added"
        r2 = await client.post("/api/kibble/e1/upload/training/Kitty", data=_form(b"garbage, not a photo"))
        assert (await r2.json())["status"] == "error"
        r3 = await client.post("/api/kibble/e1/upload/training/Kitty", data=_form(_jpeg_bytes((40, 200, 90), seed=2)))
        assert (await r3.json())["status"] == "added"
    rows = store.conn.execute("SELECT source FROM training WHERE cat='Kitty' ORDER BY created").fetchall()
    assert [r["source"] for r in rows] == ["upload", "upload"]


def _crafted_oversized_png(w: int, h: int) -> bytes:
    import struct
    import zlib

    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 3)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# --- avatar upload ------------------------------------------------------------------------------


async def test_avatar_upload_sets_a_custom_avatar_end_to_end(tmp_path: Path, socket_enabled: None) -> None:
    store = _store(tmp_path, "Kitty")
    async with _running_upload_views(store) as client:
        resp = await client.post("/api/kibble/e1/upload/avatar/Kitty", data=_form(_jpeg_bytes((10, 20, 30))))
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
        assert body["avatar"]["id"] == "avatars/kitty.jpg"
    assert (tmp_path / "avatars" / "kitty.jpg").exists()
    assert store.avatar_info("Kitty")["custom"] is True


async def test_avatar_upload_to_an_unknown_cat_404s(tmp_path: Path, socket_enabled: None) -> None:
    store = _store(tmp_path)
    async with _running_upload_views(store) as client:
        resp = await client.post("/api/kibble/e1/upload/avatar/Nope", data=_form(_jpeg_bytes((1, 2, 3))))
        assert resp.status == HTTPStatus.NOT_FOUND
