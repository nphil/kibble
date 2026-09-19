"""Wi-Fi network selection, cat-face labelling, feeder-userland switching, the camera-indicator
LED's three-way policy, and (this batch) writable small-integer device settings rendered as
real option labels for Kibble."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.persistent_notification import async_create
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory, Platform
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
from .stacks import applies_to

# Writes are coordinator-mediated and serialised by api.py's own lock; see coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    stack = coordinator.data.detected_stack
    entities: list[SelectEntity] = [
        KibbleWifiSelect(coordinator),
        KibbleLabelFaceSelect(coordinator),
        KibbleStackSelect(coordinator),
    ]
    if applies_to(Platform.SELECT, "camera_indicator", stack):
        entities.append(KibbleCameraIndicatorSelect(coordinator))
    entities.extend(
        KibbleSettingSelect(coordinator, d) for d in SETTING_SELECTS if applies_to(Platform.SELECT, d.key, stack)
    )
    async_add_entities(entities)


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


class KibbleStackSelect(KibbleEntity, SelectEntity):
    """Which feeder userland is running: the vendor's own Petkit stack, or the open LibreFeed
    replacement (`GET /mode`, `agent/src/mode.rs`). Selecting the other option asks the agent
    to switch and reboots the feeder ~1s later -- the switch is confirmed by the next poll
    catching up once the reboot completes, not by anything this entity refreshes itself (see
    `coordinator.py`'s `async_set_mode`).

    Unavailable, rather than broken, on an agent old enough to predate `GET /mode`:
    `coordinator.py`'s `_fetch_all` stores `None` for `data.stack` when that one fetch alone
    fails, and this entity treats that as unavailable on top of the base coordinator-success
    check.
    """

    _attr_translation_key = "stack"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["vendor", "librefeed"]

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "stack")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.stack is not None

    @property
    def current_option(self) -> str | None:
        stack = self.coordinator.data.stack
        return stack.running if stack is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stack = self.coordinator.data.stack
        if stack is None:
            return {}
        return {"next": stack.next, "librefeed_installed": stack.librefeed_installed}

    async def async_select_option(self, option: str) -> None:
        stack = self.coordinator.data.stack
        if stack is not None and option == stack.running:
            return
        try:
            await self.coordinator.async_set_mode(option)
        except KibbleError as err:
            raise_agent_action_failed("Set stack", err)


_CAMERA_BY_OPTION = {"auto": "auto", "on": 1, "off": 0}
_OPTION_BY_CAMERA = {"auto": "auto", 1: "on", 0: "off"}


class KibbleCameraIndicatorSelect(KibbleEntity, SelectEntity):
    """The feeder's camera-in-use indicator LED (`GET`/`POST /led`'s `camera` field,
    LibreFeed-only -- unavailable, not a bare `unknown`, on the vendor stack where `/led`
    404s; see `light.py`'s `KibbleStatusLight.available`, the same `coordinator.data.led is
    None` check). `"auto"` is the device's own policy -- lit while a stream is actively being
    watched -- and the two forced states (`"on"`, `"off"`) override it; modelled as three
    real options, not a boolean, so `"auto"` is never collapsed into (and indistinguishable
    from) whichever state it happens to be showing right now.

    This is the privacy-relevant half of `/led`: it is what lights up while someone is
    watching the camera stream, so unlike the plain device settings in `KibbleSettingSelect`
    below it is CONFIG but ships enabled by default -- the same visible-by-default reasoning
    `switch.py`'s `KibbleCloudSwitch` uses for the other privacy control this integration
    exposes."""

    _attr_translation_key = "camera_indicator"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["auto", "on", "off"]

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "camera_indicator")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.led is not None

    @property
    def current_option(self) -> str | None:
        led = self.coordinator.data.led
        return None if led is None else _OPTION_BY_CAMERA.get(led.camera)

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.async_set_led(camera=_CAMERA_BY_OPTION[option])
        except KibbleError as err:
            raise_agent_action_failed("Set camera indicator", err)


@dataclass(frozen=True, kw_only=True)
class KibbleSettingSelectDescription(SelectEntityDescription):
    """A writable small-integer device setting (`/config`), rendered as a select with real
    human labels -- `select_options[i]` is the device's integer value `i`. Named
    `select_options`, not `options`: `test_entity_platform_rules.py`'s generic
    `getattr(desc, "options", None)` check pairs a bare `options` field with
    `SensorDeviceClass.ENUM`, a sensor-only rule this select-domain field must never trip.
    """

    select_options: tuple[str, ...]


# `selected_sound`: which of `speaker.rs`'s `CHIME_PATTERNS` a chime plays -- labels describe
# the actual synthesized tone pattern (this batch's own report has the full evidence trail on
# why a synthesized chime, not the vendor's `/audio` AAC clips or the MCU buzzer), in the exact
# index order the daemon's own `speaker::CHIME_PATTERNS`/`compat.rs`'s `selected_sound`
# `SettingDef` use -- index 0 is `CHIME_PATTERNS[0]`, and so on. `surplus_control`: LibreFeed's
# own leftover-food mode (`docs/06-entity-audit.md`; explicitly NOT vendor parity -- see
# `const.py`'s `MIN_SURPLUS_STANDARD` doc for why).
SETTING_SELECTS: tuple[KibbleSettingSelectDescription, ...] = (
    KibbleSettingSelectDescription(
        key="selected_sound",
        translation_key="selected_sound",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        select_options=(
            "Single beep",
            "Rising two-tone",
            "Falling two-tone",
            "Three-note chime",
            "Long tone",
        ),
    ),
    KibbleSettingSelectDescription(
        key="surplus_control",
        translation_key="surplus_control",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        select_options=("Off", "Warn only", "Skip feed"),
    ),
)


class KibbleSettingSelect(KibbleEntity, SelectEntity):
    """One writable small-integer device setting (`/config`), rendered as a select with real
    human labels -- `entity_description.select_options[i]` <-> the device's integer value `i`.
    Unlike `KibbleWifiSelect`'s dynamically scanned list, this option list is fixed and known
    ahead of time; unlike `KibbleStackSelect`'s string-valued `/mode`, the device's own value
    here is a plain 0-based integer index, so a write round-trips through that index rather
    than the option string itself.

    Unavailable, rather than a bare `unknown`, when this setting's key is missing from `GET
    /config` altogether, or its persisted value is out of range for the option list -- mirrors
    `switch.py`'s `KibbleSettingSwitch.available`.
    """

    entity_description: KibbleSettingSelectDescription

    def __init__(self, coordinator: KibbleCoordinator, description: KibbleSettingSelectDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_options = list(description.select_options)

    def _index(self) -> int | None:
        value = self.coordinator.data.config.get(self.entity_description.key)
        options = self.entity_description.select_options
        if value is None or not (0 <= value < len(options)):
            return None
        return value

    @property
    def available(self) -> bool:
        return super().available and self._index() is not None

    @property
    def current_option(self) -> str | None:
        index = self._index()
        return None if index is None else self.entity_description.select_options[index]

    async def async_select_option(self, option: str) -> None:
        index = self.entity_description.select_options.index(option)
        try:
            await self.coordinator.async_set_config(self.entity_description.key, index)
        except KibbleError as err:
            raise_agent_action_failed(f"Set {self.entity_description.key}", err)
