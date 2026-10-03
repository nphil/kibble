"""Shared exception helpers.

Every service handler (`__init__.py`) and every entity action method (`button.py`,
`media_player.py`, `number.py`, `select.py`, `switch.py`) ends the same way: a `KibbleError`
from the agent becomes a translated `HomeAssistantError` naming the action that failed. Sharing
that here means one `strings.json` translation instead of two dozen near-identical ones, and one
place that gets `from err`/`translation_domain`/the exact placeholder names right, instead of
two dozen chances to typo one.
"""

from __future__ import annotations

from typing import NoReturn

from homeassistant.exceptions import HomeAssistantError

from .api import KibbleCueCooldownError, KibbleSpeakerBusyError
from .const import DOMAIN


def raise_agent_action_failed(action: str, err: Exception) -> NoReturn:
    """`action` is a short, capitalised gerund/noun phrase -- "Feed", "Set volume", "Schedule
    card add" -- that fills `strings.json`'s `agent_action_failed`: `"{action} failed: {error}"`.
    """
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="agent_action_failed",
        translation_placeholders={"action": action, "error": str(err)},
    ) from err


def raise_speaker_busy(err: KibbleSpeakerBusyError) -> NoReturn:
    """The speaker's exclusive-owner arbitration (`api.py`'s 409 handling, `audioout.rs`'s
    `SpeakerOwner`) rejected a play -- a real, if transient, condition distinct enough from a
    generic agent failure to get its own message (see `strings.json`'s `speaker_busy`)."""
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="speaker_busy",
        translation_placeholders={"error": str(err)},
    ) from err


def raise_cue_cooldown(err: KibbleCueCooldownError) -> NoReturn:
    """`POST /cue`'s per-call debounce (`api.py`'s 429 handling, `speaker::SpeakerOwner::
    try_start_call`) rejected a call within `CALL_COOLDOWN` of the last one -- a real, if
    transient, condition distinct enough from a generic agent failure to get its own message
    (see `strings.json`'s `cue_cooldown`)."""
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="cue_cooldown",
        translation_placeholders={"error": str(err)},
    ) from err


def raise_feeder_not_ready(name: str) -> NoReturn:
    """A service call reached a feeder that has not answered its first poll since Home Assistant
    started (`__init__.py`'s `_coordinator_for_device`; coordinator.py's "The first poll runs in
    the background"), so there is no snapshot to act through. Transient by construction -- it
    clears the moment the feeder answers -- hence a message of its own (`strings.json`'s
    `feeder_not_ready`) rather than a generic agent failure."""
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="feeder_not_ready",
        translation_placeholders={"name": name},
    )
