"""HTTP image proxy for the dashboard cards.

Authenticated (`requires_auth = True`) -- unlike `image.py`'s existing entities, which point
`ImageEntity._attr_image_url` straight at the agent's own LAN address for HA's own built-in
image proxy to fetch, a card is not guaranteed to be able to reach the feeder's LAN address
directly, so it needs a request HA itself fetches and forwards. `kind` selects which agent
store `name` (and, for `sample`, `cat`) names -- every kind is an already-JPEG passthrough
through `api.py`'s `*_bytes` methods (`track`'s `name` is a `track` detection's unix `ts`, not
a filename -- the agent re-resolves and serves whichever `eat`/`visit` it judges paired with
that timestamp live). `feed` alone can still need a decode: a feeder still running the vendor
kibbled stack serves `agent/src/feed_capture.rs`'s raw H.264 keyframe at this same URL instead
of a JPEG, told apart from LibreFeed's own real one by its magic bytes -- never by guessing
which stack is running -- in `image.py`'s `_feed_snapshot_jpeg`.
"""

from __future__ import annotations

import re
import time
from http import HTTPStatus

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.config_entries import ConfigEntryState

from .api import KibbleError, KibbleNotFoundError
from .const import DOMAIN
from .image import _feed_snapshot_jpeg
from .websocket import TRACK_PAIR_LOOKAHEAD_SECONDS

CACHE_CONTROL = "private, max-age=31536000, immutable"


def _track_cache_control(ts: int) -> str:
    """A `track` image's live pairing (`websocket._track_pair`) can still change until
    `ts + TRACK_PAIR_LOOKAHEAD_SECONDS`: a closer `eat`/`visit` recorded after this exact
    request could still join the window and become the new answer for the same `ts`. Caching
    today's answer as immutable before that window has fully elapsed would let a browser keep
    serving a stale -- possibly wrong -- pairing forever, for the life of the cache, even
    after the agent itself would now answer differently. Once the window has fully elapsed no
    future event can ever join it, so the pairing is provably final and safe to cache exactly
    like every other passthrough kind."""
    if time.time() - ts < TRACK_PAIR_LOOKAHEAD_SECONDS:
        return "no-store"
    return CACHE_CONTROL


# Mirrors `agent/src/faces.rs`/`ai.rs`'s own `is_safe_name`: no path separator, no leading dot
# (rules out `.`/`..`/hidden-file games). Checked here too so a bad name 404s before ever
# reaching the network -- aiohttp's own `{name}` route segment can't smuggle a `/` across path
# components either way, but a bare `..` is still a single, otherwise-legal segment.
_UNSAFE_NAME = re.compile(r"[/\\]|^\.")


def _is_safe_name(name: str) -> bool:
    return bool(name) and _UNSAFE_NAME.search(name) is None


class KibbleImageView(HomeAssistantView):
    """`GET /api/kibble/{entry_id}/image/{kind}/{name}`, `kind` one of `event`, `feed`,
    `pending`, `sample/{cat}`, `track`."""

    url = "/api/kibble/{entry_id}/image/{kind}/{name}"
    extra_urls = ["/api/kibble/{entry_id}/image/sample/{cat}/{name}"]
    name = "api:kibble:image"
    requires_auth = True

    async def get(
        self,
        request: web.Request,
        entry_id: str,
        name: str,
        kind: str = "sample",
        cat: str | None = None,
    ) -> web.Response:
        hass = request.app[KEY_HASS]
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        if not _is_safe_name(name) or (cat is not None and not _is_safe_name(cat)):
            return web.Response(status=HTTPStatus.NOT_FOUND)

        client = entry.runtime_data.client
        cache_control = CACHE_CONTROL
        try:
            if kind == "event":
                jpeg: bytes | None = await client.event_bytes(name)
            elif kind == "pending":
                jpeg = await client.pending_bytes(name)
            elif kind == "sample" and cat is not None:
                jpeg = await client.sample_bytes(cat, name)
            elif kind == "track" and name.isdigit():
                jpeg = await client.track_image_bytes(int(name))
                cache_control = _track_cache_control(int(name))
            elif kind == "feed":
                jpeg = await _feed_snapshot_jpeg(hass, entry, name)
            else:
                return web.Response(status=HTTPStatus.NOT_FOUND)
        except KibbleNotFoundError:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        except KibbleError:
            return web.Response(status=HTTPStatus.BAD_GATEWAY)

        if jpeg is None:
            # Only the `feed` branch above can reach this -- `_feed_snapshot_jpeg` delegates a
            # non-JPEG (H.264 keyframe) response to `_h264_keyframe_to_jpeg`, which reports a
            # decode failure as `None`, not an exception (see its own doc comment).
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(
            body=jpeg, content_type="image/jpeg", headers={"Cache-Control": cache_control}
        )
