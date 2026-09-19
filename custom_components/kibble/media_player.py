"""The feeder's speaker as a media player.

`PLAY_MEDIA`/`announce` resolve an HA media reference (a plain URL or a media-source URI --
what `tts.speak` produces) to raw PCM and `POST /speak` once
(`KibbleCoordinator.async_play_media_content`). `announce` needs no special handling beyond
accepting the kwarg: this is a single mono speaker with exactly one playback session possible
at a time (`audioout.rs`'s `SpeakerOwner`), so there is no concurrent "main" audio to duck --
an announcement is just an immediate play, same as any other. `kibble.play_clip` is the
separate, cheaper path for something already stored on the device: it posts straight to
`/clips/<name>/play` with no download/convert step, since the agent already holds it pre-
encoded (see `__init__.py`).

**Audible playback is currently blocked on the device side, not here.** `agent/src/
audioout.rs`'s write path is confirmed byte-correct -- every `/speak` call gets a normal
200 + real sample count back from the live agent (verified against the real device this
session: a 100ms silent test returned `{"ok":true,"samples":1600,"estimated_ms":100}`) -- but
the vendor's own `audio_out_thread` does not yet consume anything written to its ring slot:
`SndFrm` on `/proc/ax_proc/ao` never moves across a `/speak` call (`docs/23-audio-codec.md`
"What does not yet work"). That is a device-side gap a sibling session is actively closing,
not a bug in this entity -- the clip store, the format conversion, the HTTP round trip and the
409/busy handling below are all real and independently verified against the live agent; only
the actual sound is silent until that lands. This entity does not fake success: a failed
request still raises `HomeAssistantError`, and a 409 (speaker already has a writer) is
reported as exactly that.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .api import KibbleError, KibbleSpeakerBusyError
from .const import MAX_DEVICE_VOLUME
from .coordinator import KibbleConfigEntry, KibbleCoordinator
from .entity import KibbleEntity
from .errors import raise_agent_action_failed, raise_speaker_busy
from .stacks import applies_to

# Writes are coordinator-mediated and serialised by api.py's own lock; see coordinator.py's
# module docstring and the parallel-updates quality-scale rule.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KibbleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if applies_to(Platform.MEDIA_PLAYER, "speaker", entry.runtime_data.data.detected_stack):
        async_add_entities([KibbleSpeaker(entry.runtime_data)])


class KibbleSpeaker(KibbleEntity, MediaPlayerEntity):
    """The feeder's speaker. See the module docstring for the current audible-playback caveat."""

    _attr_translation_key = "speaker"
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
    )

    def __init__(self, coordinator: KibbleCoordinator) -> None:
        super().__init__(coordinator, "speaker")
        self._playing = False
        self._unsub_idle: CALLBACK_TYPE | None = None

    @property
    def state(self) -> MediaPlayerState:
        return MediaPlayerState.PLAYING if self._playing else MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float | None:
        """The device's own `config["volume"]` (0-9, the only writable integer setting in
        `agent/src/settings.rs`'s table), scaled to HA's 0.0-1.0."""
        value = self.coordinator.data.config.get("volume")
        return None if value is None else value / MAX_DEVICE_VOLUME

    async def async_set_volume_level(self, volume: float) -> None:
        device_volume = round(max(0.0, min(1.0, volume)) * MAX_DEVICE_VOLUME)
        try:
            await self.coordinator.async_set_config("volume", device_volume)
        except KibbleError as err:
            raise_agent_action_failed("Set volume", err)

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        try:
            result = await self.coordinator.async_play_media_content(media_id)
        except KibbleSpeakerBusyError as err:
            raise_speaker_busy(err)
        except KibbleError as err:
            raise_agent_action_failed("Play", err)
        self._mark_playing(result.get("estimated_ms"))

    def _mark_playing(self, estimated_ms: float | int | None) -> None:
        """Flips to `playing` now and schedules the revert to `idle`. `estimated_ms` is the
        agent's own `samples * 1000 / 16000` for the exact bytes just sent (`agent/src/
        main.rs`'s `speak`), not a guess made here -- there is no live "is speaking" flag
        anywhere in `GET /state` to poll instead (see module docstring). `kibble.play_clip`
        deliberately does not call this: `/clips/<name>/play`'s response carries no duration
        estimate, so this entity doesn't fabricate one for it."""
        if self._unsub_idle is not None:
            self._unsub_idle()
        self._playing = True
        self.async_write_ha_state()
        self._unsub_idle = async_call_later(
            self.hass, max(float(estimated_ms or 0), 0.0) / 1000, self._async_revert_to_idle
        )

    @callback
    def _async_revert_to_idle(self, _now: Any) -> None:
        self._unsub_idle = None
        self._playing = False
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_idle is not None:
            self._unsub_idle()
            self._unsub_idle = None
        await super().async_will_remove_from_hass()
