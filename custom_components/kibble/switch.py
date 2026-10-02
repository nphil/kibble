"""Switches for the feeder's writable boolean settings.

`night`, `microphone` were the original writable set (the same keys the
vendor's own `agent/src/settings.rs` also marked writable) -- `light` was too, but it is gone
from here now: it drove the exact same physical LED as `light.py`'s `KibbleStatusLight`
through a strict subset of what that entity already does (plain on/off, via `POST /config`,
versus the light's full on/off/blink/fast-blink/auto over `POST /led`), and having both was
exactly the "a status LED and a Status light entity ... its not clear what the difference
is" duplication a user flagged. `KibbleStatusLight` now falls back to this same `config`
key itself when `/led` is unavailable (see that module's docstring), so there is exactly one
entity for this LED again and it never needs a `SWITCHES` entry.

kibbled deliberately left every other boolean setting read-only rather than risk writing the
vendor's `config_shm` directly (see `binary_sensor.py`). LibreFeed owns its own `/config` now,
so that caution no longer applies -- this platform's booleans now also include
`pet_detection`, `move_detection`, `eat_detection`, `feed_picture`, `eat_video`, `food_warn`,
`time_display`, `camera`, `light_mode`, `tone_mode` (see LibreFeed's own
`docs/06-entity-audit.md`, the `writable-after-plumbing` table), the sound gates:
`sound_enable` (master), `feed_sound` (dispense start/finish chime), `system_sound_enable`
(media/mcu reconnect, a hopper going empty, a scheduled fire the MCU refused), and, this
batch, `smart_frame` (auto-framing: the camera's sub-stream crops to follow the tracked cat,
easing back to the full frame a few seconds after nothing is detected) -- previously a
read-only `binary_sensor.py` entry, moved here now that the IVPS crop-follows-body work is
plumbed and writable, and `detection_overlay` (also this batch: the card's own live
bounding-box overlay, drawn from this same vision pipeline's `GET /vision/last` verdicts --
see its own comment in `SWITCHES` below for why it is enabled by default and LibreFeed-only,
unlike every other setting here). Every setting still missing a plumbed key stays read-only
and lives in `binary_sensor.py` instead, so this platform never exposes a control surface the
agent would reject."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .api import KibbleError
from .coordinator import KibbleConfigEntry
from .entity import KibbleEntity
from .errors import raise_agent_action_failed
from .stacks import applies_to

# Writes are coordinator-mediated and serialised by api.py's own lock; see coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


SWITCHES: tuple[SwitchEntityDescription, ...] = (
    SwitchEntityDescription(
        key="night",
        translation_key="night",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="microphone",
        translation_key="microphone",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="pet_detection",
        translation_key="pet_detection",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="move_detection",
        translation_key="move_detection",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="eat_detection",
        translation_key="eat_detection",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="feed_picture",
        translation_key="feed_picture",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    # `eat_video`: the vendor's own wire spelling (`agent/src/settings.rs`'s `cjson_key
    # "eatVideo"`), kept unchanged as the `/config` key -- and on the vendor stack this
    # setting really does gate a recorded video clip. LibreFeed records no clips at all;
    # here it gates the same before/after *still-photo* capture `feed_picture` uses
    # (`daemon/src/main.rs`'s `compat::setting_flag("eat_video")` feeds the identical
    # `capture_if_enabled`/`capture_snapshot` gate as `feed_picture`'s dispense photos --
    # `compat.rs`'s own doc comment on `capture_if_enabled`). The display name says so
    # plainly; the key does not change, so the entity keeps its existing unique id.
    SwitchEntityDescription(
        key="eat_video",
        translation_key="eat_video",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="food_warn",
        translation_key="food_warn",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="time_display",
        translation_key="time_display",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="camera",
        translation_key="camera",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="light_mode",
        translation_key="light_mode",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="tone_mode",
        translation_key="tone_mode",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="sound_enable",
        translation_key="sound_enable",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="feed_sound",
        translation_key="feed_sound",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    SwitchEntityDescription(
        key="system_sound_enable",
        translation_key="system_sound_enable",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    # `smart_frame`: auto-framing -- the camera's sub-stream crops to follow the tracked
    # cat's body box, easing back to the full frame a few seconds after the tracker loses
    # it. Previously read-only in `binary_sensor.py`'s `SETTING_SENSORS`; moved here, its old
    # entry removed, once the IVPS crop-follows-body write path was plumbed.
    SwitchEntityDescription(
        key="smart_frame",
        translation_key="smart_frame",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    # `detection_overlay`: whether the card draws this same vision pipeline's own detection
    # boxes + the open track's cat name over its live video (`websocket.py`'s
    # `kibble/vision/last`, fed by `GET /vision/last`) -- LibreFeed-only in `stacks.py`, not
    # merely `writable: false` there like every setting above: the vendor agent has no such
    # key, or a vision pipeline for a card to draw from, at all. Deliberately no
    # `entity_registry_enabled_default=False`, unlike every setting above: this is a thing the
    # user actively watches and drives while the card is open, not a diagnostic knob left off
    # by default (see `test_entity_platform_rules.py`'s `_ENABLED_BY_DEFAULT_EXCEPTIONS`, the
    # same carve-out `KibbleCloudSwitch` already has below).
    SwitchEntityDescription(
        key="detection_overlay",
        translation_key="detection_overlay",
        entity_category=EntityCategory.CONFIG,
    ),
    # `detection_overlay_ignored`: whether that overlay ALSO draws the detections the feeder's
    # clutter memory threw away (`admitted: false`). Enabled-by-default like its parent, for the
    # same reason -- but the setting itself defaults OFF on the daemon: the rejected boxes are
    # what make "the feeder is ignoring the cat" visible instead of indistinguishable from an
    # empty room, and a busy room emits several of them on every frame forever, so they belong
    # behind a switch rather than on a dashboard someone lives with. Turning the parent
    # `detection_overlay` off hides everything regardless; this one only narrows what is drawn.
    SwitchEntityDescription(
        key="detection_overlay_ignored",
        translation_key="detection_overlay_ignored",
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    stack = coordinator.data.detected_stack
    entities: list[SwitchEntity] = [
        KibbleSettingSwitch(coordinator, d) for d in SWITCHES if applies_to(Platform.SWITCH, d.key, stack)
    ]
    if applies_to(Platform.SWITCH, "cloud", stack):
        entities.append(KibbleCloudSwitch(coordinator))
    entities.append(KibbleHopperDividerSwitch(coordinator))
    entities.append(KibbleAutoLearnSwitch(coordinator))
    async_add_entities(entities)


class KibbleHopperDividerSwitch(KibbleEntity, SwitchEntity, RestoreEntity):
    """Whether the hopper's removable divider is fitted. On: two compartments (the default,
    matching the feeder as shipped). Off: one shared bin -- the coordinator then feeds "both"
    through dispenser 1 alone, writes schedule-card amounts to that side only, converts the
    existing schedule to match, and the timeline stops naming a hopper
    (`KibbleCoordinator.async_set_single_hopper`).

    Nothing on the feeder can sense the divider, so this is a remembered choice: restored across
    restarts, never unavailable while the entity exists. Restoring it never rewrites the
    schedule -- only an actual change does."""

    _attr_translation_key = "hopper_divider"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "hopper_divider")
        self._attr_is_on = True  # only the restore default; `is_on` reads the coordinator

    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
        self.coordinator.single_hopper = not self._attr_is_on

    @property
    def is_on(self) -> bool:
        return not self.coordinator.single_hopper

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_single_hopper(False)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_single_hopper(True)
        self.async_write_ha_state()


class KibbleAutoLearnSwitch(KibbleEntity, SwitchEntity, RestoreEntity):
    """Whether `ingest.py`'s auto-learn may add new training samples on its own. Off: the
    identity engine keeps classifying every visit exactly as before, and a human label or an
    uploaded photo still trains immediately -- this only gates samples nobody reviewed.

    Local-only, no feeder round trip, restored across restarts -- same pattern as
    `KibbleHopperDividerSwitch` above (`coordinator.auto_learn_enabled` is the single source of
    truth; `is_on` reads it, restoring only seeds it once at startup).

    `EntityCategory.CONFIG` without `entity_registry_enabled_default=False`: every other config
    entity in this integration disables itself by default (kibble docs' "house rule 4"), but
    this one is explicitly requested visible -- the user wants it in Settings from the start,
    not opted into."""

    _attr_translation_key = "auto_learn"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "auto_learn")
        self._attr_is_on = True  # only the restore default; `is_on` reads the coordinator

    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
        self.coordinator.auto_learn_enabled = self._attr_is_on

    @property
    def is_on(self) -> bool:
        return self.coordinator.auto_learn_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_auto_learn_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_auto_learn_enabled(False)
        self.async_write_ha_state()


class KibbleSettingSwitch(KibbleEntity, SwitchEntity):
    """One writable boolean device setting, read from and written to the feeder's shared
    config through the agent's `/config` endpoint.

    Unavailable, rather than a bare `unknown`, when this setting's key is missing from `GET
    /config` altogether -- e.g. `pet_detection` on a daemon old enough to predate serving
    it. Mirrors `light.py`'s `KibbleStatusLight.available`/`select.py`'s
    `KibbleStackSelect.available`/`binary_sensor.py`'s
    `KibbleSettingBinarySensor.available`."""

    entity_description: SwitchEntityDescription

    def __init__(self, coordinator, description: SwitchEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.entity_description.key in self.coordinator.data.config

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.config.get(self.entity_description.key)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(0)

    async def _async_write(self, value: int) -> None:
        try:
            await self.coordinator.async_set_config(self.entity_description.key, value)
        except KibbleError as err:
            raise_agent_action_failed(f"Set {self.entity_description.key}", err)


class KibbleCloudSwitch(KibbleEntity, SwitchEntity):
    """The Petkit-cloud kill switch (`agent/src/cloud.rs`): routes everything but the LAN
    through a kernel route blackhole instead of the vendor's real default route, leaving
    Home Assistant, Scrypted and the router untouched. Enabled by default and CONFIG, not
    disabled-by-default like the settings switches above -- this is the privacy control the
    integration exists for, so it ships visible, matching the device's own out-of-the-box
    (cloud-enabled) behaviour."""

    _attr_translation_key = "cloud"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "cloud")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.cloud.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cloud = self.coordinator.data.cloud
        attrs: dict[str, Any] = {"routes": list(cloud.routes)}
        if cloud.last_error:
            attrs["last_error"] = cloud.last_error
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(False)

    async def _async_write(self, enabled: bool) -> None:
        try:
            await self.coordinator.async_set_cloud(enabled)
        except KibbleError as err:
            raise_agent_action_failed("Set Petkit cloud", err)

