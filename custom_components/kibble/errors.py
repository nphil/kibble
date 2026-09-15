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

from .api import KibbleSpeakerBusyError
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
