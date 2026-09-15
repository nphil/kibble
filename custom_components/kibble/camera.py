"""The feeder's camera, as an entity on the Kibble device.

The stream is the vendor's own hardware-encoded H.264 substream (1152x720, 25 fps), served by
`kibbled` over RTSP straight out of the encoder's frame ring — no re-encoding anywhere. We hand
Home Assistant that URL and let its bundled WebRTC provider (go2rtc) do the rest: live view,
HLS fallback, and — once the agent gains an audio backchannel — two-way audio. That is the same
mechanism Home Assistant uses for any RTSP camera, which is exactly the point: nothing here is
Kibble-specific beyond knowing where the stream lives.
"""

from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_HOST, DEFAULT_RTSP_PATH, DEFAULT_RTSP_PORT
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
        self._url = (
            f"rtsp://{entry.data[CONF_HOST]}:{DEFAULT_RTSP_PORT}{DEFAULT_RTSP_PATH}"
        )

    @property
    def is_streaming(self) -> bool:
        # The vendor encoder runs continuously whether or not anyone is watching, so the
        # stream is always live; this is what makes the card show a live badge.
        return True

    async def stream_source(self) -> str:
        return self._url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        # A still is one decoded frame from the stream. The device has a hardware JPEG
        # encoder that would make this free; wiring it into the agent is a later step.
        return await async_get_image(self.hass, self._url, width=width, height=height)
