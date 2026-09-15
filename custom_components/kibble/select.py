"""Wi-Fi network selection and cat-face labelling for Kibble."""

from __future__ import annotations

from homeassistant.components.persistent_notification import async_create
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import KibbleError
from .const import (
    CAT_BUCKET_NOT_A_CAT,
    CAT_BUCKET_SKIP,
    CAT_LABEL_NOT_A_CAT,
    CAT_LABEL_SKIP,
    DOMAIN,
)
from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity
from .errors import raise_agent_action_failed

# Writes are coordinator-mediated and serialised by api.py's own lock; see coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([KibbleWifiSelect(coordinator), KibbleLabelFaceSelect(coordinator)])


class KibbleWifiSelect(KibbleEntity, SelectEntity):
    """Options are the feeder's own scan (`GET /wifi/scan`), refreshed every coordinator poll;
    the current SSID is always included so it can render as selected even if a given scan
    doesn't currently see it (weak signal, mid-roam, ...).

    `GET /wifi` exposes no list of which SSIDs the agent already holds a password for, only the
    *current* SSID and the *desired* one (`wifi.json` -- see `agent/src/wifi.rs`); those are the
    only two SSIDs this entity can be sure a direct reconnect will work for, since a `psk`-less
    `POST /wifi/connect` only succeeds by reusing an existing Kibble-managed network entry.
    Selecting either of those reconnects immediately. Anything else has no known password to
    send -- a `select` cannot prompt for one inline -- so this fires a persistent notification
    pointing at the `kibble.wifi_connect` service instead of guessing.
    """

    _attr_translation_key = "wifi"

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "wifi")

    @property
    def options(self) -> list[str]:
        options = [network.ssid for network in self.coordinator.data.wifi_scan]
        current = self.coordinator.data.wifi.ssid
        if current and current not in options:
            options.append(current)
        return options

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.wifi.ssid

    async def async_select_option(self, option: str) -> None:
        wifi = self.coordinator.data.wifi
        if option not in (wifi.ssid, wifi.desired_ssid):
            async_create(
                self.hass,
                (
                    f'Kibble doesn\'t have a saved password for "{option}" yet. Use the '
                    "**Kibble: Connect to Wi-Fi network** action (Developer tools \u2192 "
                    "Actions, or an automation) with the password, then select it here again."
                ),
                title="Kibble Wi-Fi",
                notification_id=f"{self.unique_id}_needs_password",
            )
            return
        try:
            await self.coordinator.async_wifi_connect(option)
        except KibbleError as err:
            raise_agent_action_failed(f"Connect to {option}", err)


def cat_for_option(option: str) -> str:
    """Maps a label-select display option to the `cat` bucket value `POST /faces/label`
    expects -- the two reserved buckets (`agent/src/faces.rs`'s `SKIP_BUCKET`/
    `NOT_A_CAT_BUCKET`) get their wire names; anything else (a real cat name) passes through
    unchanged. A free function (not a method) so it's directly unit-testable with no entity or
    coordinator involved."""
    if option == CAT_LABEL_SKIP:
        return CAT_BUCKET_SKIP
    if option == CAT_LABEL_NOT_A_CAT:
        return CAT_BUCKET_NOT_A_CAT
    return option


class KibbleLabelFaceSelect(KibbleEntity, SelectEntity):
    """Options are every known cat name plus the two reserved buckets. Selecting one labels
    whichever crop `image.cat_feeder_pending_face` is currently showing and advances the review
    queue. Raises if nothing is pending right now -- a crop that's already labelled has nothing
    left to decide, and picking an option for it would silently do nothing useful.
    """

    _attr_translation_key = "label_face"

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "label_face")

    @property
    def options(self) -> list[str]:
        return [cat.name for cat in self.coordinator.data.cats] + [
            CAT_LABEL_SKIP,
            CAT_LABEL_NOT_A_CAT,
        ]

    @property
    def current_option(self) -> str | None:
        # A momentary action, not persistent state -- nothing is "currently selected" for
        # whichever crop is showing next.
        return None

    async def async_select_option(self, option: str) -> None:
        review = self.coordinator.data.review_face
        if review.status != "pending" or review.name is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_pending_face"
            )
        try:
            await self.coordinator.async_label_face(review.name, cat_for_option(option))
        except KibbleError as err:
            raise_agent_action_failed("Label", err)
