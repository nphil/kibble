"""The feeder's camera, as an entity on the Kibble device.

Design rule (Nitin): **the feeder serves its video to exactly one consumer — Scrypted.** Everything
else, this entity included, consumes Scrypted's rebroadcast of it. The device's encoder runs
regardless of viewers, so a second direct session would buy nothing and cost the SoC a thread and
a TCP writer it does not have to spare. Scrypted's prebuffer is the fan-out point.

That is a preference, though, never a requirement: **Scrypted is not a dependency of this
integration.** Three sources are supported, in priority order, and a fresh install with none of
them configured still shows a picture straight from the feeder.

1. `stream_entity` -- another camera entity that already has this device's video (Scrypted's,
   Frigate's, go2rtc's). Preferred, because HA resolves its stream source at the moment of use.
2. `stream_url` -- a fixed RTSP URL. Works, but pin it only at something genuinely stable:
   Scrypted's own rebroadcast port is EPHEMERAL, and a hardcoded URL pointing at it goes black
   the next time Scrypted restarts (observed 2026-09-19 -- the configured port had simply
   stopped listening, and the camera dialog showed a dead player at 0:00).
3. Neither -- the feeder's own substream, direct. This is the correct configuration for anyone
   running without a video hub at all.
"""

from __future__ import annotations

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    async_get_image as async_get_camera_image,
    async_get_stream_source,
)
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_HOST,
    CONF_STREAM_ENTITY,
    CONF_STREAM_URL,
    DEFAULT_RTSP_PATH,
    DEFAULT_RTSP_PORT,
)
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity
from .stacks import applies_to

# One coordinator-backed entity; the stream itself is Scrypted's/the device's RTSP, entirely
# outside HA's own update cycle. See coordinator.py's module docstring and the
# parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if applies_to(Platform.CAMERA, "camera", entry.runtime_data.data.detected_stack):
        async_add_entities([KibbleCamera(entry)])


class KibbleCamera(KibbleEntity, Camera):
    """Live view from the bowl."""

    # The camera is the device's main visual feature, so it carries the device name alone.
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_brand = "Petkit"

    def __init__(self, entry: KibbleConfigEntry) -> None:
        KibbleEntity.__init__(self, entry.runtime_data, "camera")
        Camera.__init__(self)
        self._entry = entry

    @property
    def _delegate(self) -> str | None:
        """The camera entity this one borrows its video from, if configured."""
        entity_id = self._entry.options.get(CONF_STREAM_ENTITY)
        return entity_id or None

    @property
    def _url(self) -> str:
        configured = self._entry.options.get(CONF_STREAM_URL)
        if configured:
            return configured
        host = self._entry.data[CONF_HOST]
        return f"rtsp://{host}:{DEFAULT_RTSP_PORT}{DEFAULT_RTSP_PATH}"

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        # Which of the three sources is actually in effect, so "why is this black" is one
        # glance rather than an investigation.
        delegate = self._delegate
        if delegate:
            return {"source": "entity", "source_entity": delegate}
        if self._entry.options.get(CONF_STREAM_URL):
            return {"source": "url", "source_url": self._entry.options[CONF_STREAM_URL]}
        return {"source": "device"}

    @property
    def is_streaming(self) -> bool:
        # The vendor encoder runs continuously whether or not anyone is watching.
        return True

    async def stream_source(self) -> str:
        """Resolved per call, never cached: a delegated entity's underlying URL can change
        under us (a rebroadcast port reassigned on restart is the normal case, not an edge
        one), and the whole point of delegating is to pick that up without reconfiguration."""
        delegate = self._delegate
        if delegate:
            source = await async_get_stream_source(self.hass, delegate)
            if source:
                return source
            # The delegate exists but has no stream right now (its integration is reloading,
            # or the hub is down). Fall through to the device rather than return nothing --
            # the feeder can always serve its own video, and a picture from the wrong source
            # beats no picture.
        return self._url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        delegate = self._delegate
        if delegate:
            # Ask the delegate for its own snapshot: it may have a cheaper path than decoding
            # a frame out of RTSP (Scrypted keeps a prebuffer; ONVIF cameras have a snapshot
            # URI), and going through it keeps the single-consumer rule intact.
            image = await async_get_camera_image(self.hass, delegate, width=width, height=height)
            if image:
                return image.content
        return await async_get_image(self.hass, self._url, width=width, height=height)
