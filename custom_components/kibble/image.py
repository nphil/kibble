"""The face crop for Nitin to label next.

The oldest pending crop awaiting a label, or the most recently labelled one once the queue is
empty (`agent/src/faces.rs`'s `review_target`) -- so the picture is never blank once caught up.
Paired with `select.cat_feeder_label_face`, which acts on the same crop.
"""

from __future__ import annotations

from urllib.parse import quote

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import ReviewFace
from .const import CONF_HOST, CONF_PORT
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([KibblePendingFaceImage(hass, entry)])


def _image_url(entry: KibbleConfigEntry, review: ReviewFace) -> str | None:
    """The agent's `GET /faces/current` URL, with the crop's status and name folded into a
    cache-busting query parameter -- `agent/src/main.rs` ignores the parameter's value, but
    `ImageEntity` only refetches (and only bumps its "last updated" timestamp) when this URL
    *string* itself changes, so encoding *which* crop is showing into it is what makes a new
    crop actually appear without a manual refresh. `None` (no URL at all) only when nothing has
    ever been captured."""
    if review.name is None:
        return None
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    cache_key = quote(f"{review.status}-{review.name}", safe="")
    return f"http://{host}:{port}/faces/current?id={cache_key}"


class KibblePendingFaceImage(KibbleEntity, ImageEntity):
    """The crop for Nitin to label next."""

    _attr_translation_key = "pending_face"

    def __init__(self, hass: HomeAssistant, entry: KibbleConfigEntry) -> None:
        KibbleEntity.__init__(self, entry.runtime_data, "pending_face")
        ImageEntity.__init__(self, hass)
        self._entry = entry
        self._attr_image_url = _image_url(entry, entry.runtime_data.data.review_face)
        self._attr_image_last_updated = dt_util.utcnow()

    @callback
    def _handle_coordinator_update(self) -> None:
        url = _image_url(self._entry, self.coordinator.data.review_face)
        if url != self._attr_image_url:
            self._attr_image_url = url
            self._cached_image = None
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        review = self.coordinator.data.review_face
        attrs = {"status": review.status}
        if review.cat is not None:
            attrs["cat"] = review.cat
        return attrs
