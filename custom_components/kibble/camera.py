"""The feeder's camera, as an entity on the Kibble device.

Design rule (Nitin): **the feeder serves its video to exactly one consumer — Scrypted.** Everything
else, this entity included, consumes Scrypted's rebroadcast of it. The device's encoder runs
regardless of viewers, so a second direct session would buy nothing and cost the SoC a thread and
a TCP writer it does not have to spare. Scrypted's prebuffer is the fan-out point.

So this entity's stream source is Scrypted's rebroadcast URL, configured on the integration's
options. Home Assistant's bundled WebRTC provider (go2rtc) takes it from there. Until the
rebroadcast URL is configured, the entity falls back to the device's own substream so a fresh
install still shows a picture.
"""

from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_HOST, CONF_STREAM_URL, DEFAULT_RTSP_PATH, DEFAULT_RTSP_PORT
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
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
    def _url(self) -> str:
        configured = self._entry.options.get(CONF_STREAM_URL)
        if configured:
            return configured
        host = self._entry.data[CONF_HOST]
        return f"rtsp://{host}:{DEFAULT_RTSP_PORT}{DEFAULT_RTSP_PATH}"

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        # Make it visible whether this entity is honouring the single-consumer rule.
        return {
            "source": "scrypted" if self._entry.options.get(CONF_STREAM_URL) else "device",
        }

    @property
    def is_streaming(self) -> bool:
        # The vendor encoder runs continuously whether or not anyone is watching.
        return True

    async def stream_source(self) -> str:
        return self._url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return await async_get_image(self.hass, self._url, width=width, height=height)
