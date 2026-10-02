"""HA-side logic for the "call the cats" entity/service (`POST /cue`, LibreFeed-only): same
fake-`self`/fake-session style as `test_beep.py` -- exercises the real, unbound
`KibbleCallCatsButton`/`KibbleClient` without constructing a real Home-Assistant-backed entity or
coordinator. Does not exercise the daemon's own debounce/suppression timing (`speaker.rs`'s own
Rust tests own that); this only proves the HA-side wiring reaches the right route and maps the
agent's 429 cooldown reply to the right typed exception and translated message, not a generic one.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from kibble.api import KibbleClient, KibbleCueCooldownError, KibbleError
from kibble.button import KibbleCallCatsButton
from kibble.coordinator import KibbleCoordinator

HOST = "192.168.4.85"
PORT = 8765


class _FakeResponse:
    """Duck-types the subset of `aiohttp.ClientResponse` `api.py`'s `_request` actually uses."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type: str | None = None) -> Any:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _RecordingSession:
    """Duck-types the subset of `aiohttp.ClientSession` `api.py`'s `_request` actually calls,
    and records every outgoing request so a test can assert on the exact method/url/JSON body
    `KibbleClient` sent -- not just that some coordinator-level mock was awaited."""

    def __init__(self, status: int, body: Any) -> None:
        self._status = status
        self._body = body
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((method, url, kwargs.get("json")))
        return _FakeResponse(self._status, self._body)


def _button_with(coordinator: Any) -> KibbleCallCatsButton:
    button = object.__new__(KibbleCallCatsButton)
    button.coordinator = coordinator
    return button


def _coordinator_for(session: _RecordingSession) -> Any:
    client = KibbleClient(session, HOST, PORT)
    coordinator = SimpleNamespace(client=client, async_request_refresh=AsyncMock())
    coordinator.async_call_cats = MethodType(KibbleCoordinator.async_call_cats, coordinator)
    return coordinator


# --- (1) the button is unavailable when the LibreFeed `led` marker is None, available otherwise -


def test_available_when_led_data_present_unavailable_when_none() -> None:
    button = _button_with(SimpleNamespace(last_update_success=True, data=SimpleNamespace(led=None)))
    assert button.available is False

    button.coordinator.data.led = object()
    assert button.available is True


# --- (2) pressing the button calls POST /cue with an empty body and refreshes the coordinator --


async def test_press_calls_the_cue_route() -> None:
    session = _RecordingSession(200, {"ok": True, "played": True})
    button = _button_with(_coordinator_for(session))

    await button.async_press()

    assert session.calls == [("POST", f"http://{HOST}:{PORT}/cue", {})]
    button.coordinator.async_request_refresh.assert_awaited_once()


# --- (3) a 429 (the agent's per-call cooldown) raises KibbleCueCooldownError at the client level,
#         never a generic KibbleError or the unrelated, default KibbleSpeakerBusyError -----------


async def test_cue_429_raises_cue_cooldown_error_not_a_generic_or_speaker_busy_one() -> None:
    """`_request`'s busy branch defaults to `KibbleSpeakerBusyError` on a 409 -- `call_cats`
    must override both the status (429, not 409) and the exception type, or the agent's own
    per-call debounce would surface as a nonsensical "speaker busy" failure, or not be
    recognised as a busy condition at all (falling through to a plain 429 `KibbleError`)."""
    session = _RecordingSession(429, {"ok": False, "error": "cooldown", "retry_after_ms": 4200})
    client = KibbleClient(session, HOST, PORT)

    with pytest.raises(KibbleCueCooldownError, match="cooldown"):
        await client.call_cats()


async def test_cue_200_succeeds_and_is_not_mistaken_for_a_cooldown() -> None:
    session = _RecordingSession(200, {"ok": True, "played": True})
    client = KibbleClient(session, HOST, PORT)

    result = await client.call_cats()
    assert result == {"ok": True, "played": True}


# --- (4) the button maps a 429 to the dedicated `cue_cooldown` translated message, not the
#         generic `agent_action_failed` every other button-press failure here uses ----------------


async def test_press_maps_a_cooldown_to_its_own_translated_message_not_the_generic_one() -> None:
    session = _RecordingSession(429, {"ok": False, "error": "cooldown", "retry_after_ms": 1000})
    button = _button_with(_coordinator_for(session))

    with pytest.raises(HomeAssistantError) as excinfo:
        await button.async_press()

    assert excinfo.value.translation_key == "cue_cooldown"


def test_kibble_cue_cooldown_error_is_a_kibble_error() -> None:
    """`button.py`'s `except KibbleCueCooldownError` must be checked before the broader
    `except KibbleError` fallback to ever actually run -- this only holds if the narrower type
    really is a subclass, which this asserts directly against the class hierarchy."""
    assert issubclass(KibbleCueCooldownError, KibbleError)
