"""HA-side logic for the status-light entity: LibreFeed's full `GET`/`POST /led` source
(now including RGB colour, quantised across the `white`/`green` channels -- see `light.py`'s
`KibbleStatusLight` docstring for the exact table), and the vendor stack's `config["light"]`
fallback (`GET`/`POST /config`) used when `/led` is unavailable. The fallback is the fix for
the "a status LED and a Status light entity ... its not clear what the difference is"
duplicate, folding the old `switch.py` `light` entry into this one entity; the RGB colour
picker is the fix for "maybe we could just select the color within the light entity instead
of exposing a separate green LED?", folding the green channel in as well instead of a second
switch entity. Same duck-typed, `object.__new__`-constructed style as
`test_settings_controls.py` -- every test below builds a real (uninitialized)
`KibbleStatusLight`/`KibbleCoordinator` instance, not a bare `SimpleNamespace`, because
`is_on`/`effect`/`rgb_color`/`async_turn_on`/`async_turn_off` all call the entity's own
`_using_led` property internally, which only resolves through a real instance.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from kibble.api import KibbleClient, KibbleNotFoundError
from kibble.coordinator import KibbleCoordinator
from kibble.light import KibbleStatusLight, _quantize_rgb
from kibble.switch import SWITCHES

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


def _fake_light(led: Any = None, config: dict[str, Any] | None = None) -> KibbleStatusLight:
    """A real (uninitialized) `KibbleStatusLight` with only what its properties/writes read
    set by hand -- required because they call `self._using_led` internally, which a bare
    `SimpleNamespace` cannot resolve."""
    ent = object.__new__(KibbleStatusLight)
    ent.coordinator = SimpleNamespace(
        last_update_success=True, data=SimpleNamespace(led=led, config=config or {})
    )
    return ent


def _led(white: str | int, green: int) -> SimpleNamespace:
    return SimpleNamespace(white=white, green=green, camera="auto")


# --- (1) GET /led parsed -> is_on/effect/rgb_color correct for every white/green combination ---


async def test_get_led_parsed_reflects_is_on_effect_and_rgb_for_white_only() -> None:
    session = _RecordingSession(200, {"white": "auto", "green": 0})
    led = await KibbleClient(session, HOST, PORT).led()
    ent = _fake_light(led=led)
    assert ent.is_on is True
    assert ent.effect == "auto"
    assert ent.rgb_color == (255, 255, 255)


async def test_get_led_parsed_reflects_is_on_effect_and_rgb_for_green_only() -> None:
    session = _RecordingSession(200, {"white": 0, "green": 1})
    led = await KibbleClient(session, HOST, PORT).led()
    ent = _fake_light(led=led)
    assert ent.is_on is True
    # 0 is a forced-off value, not one of the three forced-on effects -- no effect applies.
    assert ent.effect is None
    assert ent.rgb_color == (0, 255, 0)


async def test_get_led_parsed_reflects_is_on_and_rgb_for_both_channels_lit() -> None:
    session = _RecordingSession(200, {"white": 1, "green": 1})
    led = await KibbleClient(session, HOST, PORT).led()
    ent = _fake_light(led=led)
    assert ent.is_on is True
    assert ent.effect == "on"
    assert ent.rgb_color == (170, 255, 170)


def test_rgb_color_and_effect_are_none_when_both_channels_are_off() -> None:
    ent = _fake_light(led=_led(0, 0))
    assert ent.is_on is False
    assert ent.rgb_color is None
    assert ent.effect is None


# --- (2) _quantize_rgb: every requested colour -> the nearest achievable channel pair ----------


def test_quantize_rgb_white_bucket_for_low_saturation_colours() -> None:
    assert _quantize_rgb((255, 255, 255)) == (1, 0)
    assert _quantize_rgb((240, 245, 240)) == (1, 0)


def test_quantize_rgb_green_bucket_for_a_strongly_saturated_green() -> None:
    assert _quantize_rgb((0, 255, 0)) == (0, 1)


def test_quantize_rgb_both_bucket_for_unachievable_hues_and_washed_out_green() -> None:
    assert _quantize_rgb((255, 0, 0)) == (1, 1)  # red: no channel for it at all
    assert _quantize_rgb((0, 0, 255)) == (1, 1)  # blue: same
    assert _quantize_rgb((200, 255, 200)) == (1, 1)  # pale green: too washed out to call green-ish


# --- (3) turn_on(effect=...) POSTs {"white": ...} only; green is never touched by an effect ----


async def test_turn_on_with_effect_posts_forced_white_only() -> None:
    session = _RecordingSession(200, {"white": 2, "green": 0})
    client = KibbleClient(session, HOST, PORT)
    coordinator = SimpleNamespace(
        client=client, data=SimpleNamespace(led=_led(0, 0), config={}), async_request_refresh=AsyncMock()
    )
    coordinator.async_set_led = MethodType(KibbleCoordinator.async_set_led, coordinator)
    ent = _fake_light(led=_led(0, 0))
    ent.coordinator = coordinator

    await ent.async_turn_on(effect="blink")

    assert session.calls == [("POST", f"http://{HOST}:{PORT}/led", {"white": 2})]


# --- (4) turn_on(rgb_color=...) quantises and POSTs the exact {"white", "green"} pair ----------


async def test_turn_on_with_rgb_color_posts_the_exact_quantised_pair_for_each_bucket() -> None:
    cases = [
        ((255, 255, 255), {"white": 1, "green": 0}),
        ((0, 255, 0), {"white": 0, "green": 1}),
        ((255, 0, 0), {"white": 1, "green": 1}),
    ]
    for rgb, expected_payload in cases:
        session = _RecordingSession(200, {"white": 0, "green": 0})
        client = KibbleClient(session, HOST, PORT)
        coordinator = SimpleNamespace(
            client=client, data=SimpleNamespace(led=_led(0, 0), config={}), async_request_refresh=AsyncMock()
        )
        coordinator.async_set_led = MethodType(KibbleCoordinator.async_set_led, coordinator)
        ent = _fake_light(led=_led(0, 0))
        ent.coordinator = coordinator

        await ent.async_turn_on(rgb_color=rgb)

        assert session.calls == [("POST", f"http://{HOST}:{PORT}/led", expected_payload)]


# --- (5) turn_off POSTs both channels off, so a lit green does not survive a turn_off ----------


async def test_turn_off_posts_both_channels_off() -> None:
    session = _RecordingSession(200, {"white": 0, "green": 0})
    client = KibbleClient(session, HOST, PORT)
    coordinator = SimpleNamespace(
        client=client, data=SimpleNamespace(led=_led(1, 1), config={}), async_request_refresh=AsyncMock()
    )
    coordinator.async_set_led = MethodType(KibbleCoordinator.async_set_led, coordinator)
    ent = _fake_light(led=_led(1, 1))
    ent.coordinator = coordinator

    await ent.async_turn_off()

    assert session.calls == [("POST", f"http://{HOST}:{PORT}/led", {"white": 0, "green": 0})]


# --- (6) GET /led 404ing (vendor stack) leaves data.led None; the entity is unavailable, the ----
# --- rest of the poll still succeeds -------------------------------------------------------------


def _coordinator_for_fetch(client: AsyncMock) -> KibbleCoordinator:
    """A real (uninitialized) `KibbleCoordinator` with only what `_fetch_all` reads set by
    hand -- same `object.__new__` approach as `test_stack_select.py`."""
    coord = object.__new__(KibbleCoordinator)
    coord.client = client
    coord.entry = SimpleNamespace(options={})
    return coord


async def test_get_led_404_leaves_led_none_while_the_rest_of_the_poll_still_updates() -> None:
    state = object()
    cloud = object()
    client = AsyncMock(
        state=AsyncMock(return_value=state),
        schedule=AsyncMock(return_value=object()),
        config=AsyncMock(return_value={}),
        cloud=AsyncMock(return_value=cloud),
        mode=AsyncMock(return_value=object()),
        led=AsyncMock(side_effect=KibbleNotFoundError("not found")),
        wifi=AsyncMock(return_value=object()),
        wifi_scan=AsyncMock(return_value=[]),
        cats=AsyncMock(return_value=[]),
        identify=AsyncMock(return_value=object()),
        review_face=AsyncMock(return_value=object()),
        pending_faces=AsyncMock(return_value=[]),
        clips=AsyncMock(return_value=[]),
        feeds=AsyncMock(return_value=[]),
        events=AsyncMock(return_value=[]),
    )
    coord = _coordinator_for_fetch(client)

    data = await coord._fetch_all()

    # The vendor stack's missing /led route goes to None instead of raising out of the whole
    # poll cycle -- everything else fetched in the same batch is still fresh.
    assert data.led is None
    assert data.cloud is cloud
    assert data.state is state

    fake_light = object.__new__(KibbleStatusLight)
    fake_light.coordinator = SimpleNamespace(last_update_success=True, data=data)
    assert fake_light.available is False


# --- (7) config["light"] fallback drives is_on and offers no colour/effects when /led absent ---


def test_config_fallback_reflects_is_on_and_offers_no_colour_or_effects_when_led_is_absent() -> None:
    from homeassistant.components.light import ColorMode, LightEntityFeature

    on_ent = _fake_light(led=None, config={"light": 1})
    assert on_ent._using_led is False
    assert on_ent.is_on is True
    assert on_ent.effect is None
    assert on_ent.effect_list is None
    assert on_ent.rgb_color is None
    assert on_ent.supported_features == LightEntityFeature(0)
    assert on_ent.supported_color_modes == {ColorMode.ONOFF}
    assert on_ent.color_mode == ColorMode.ONOFF

    off_ent = _fake_light(led=None, config={"light": 0})
    assert off_ent.is_on is False


def test_led_route_still_offers_colour_and_effects_when_led_is_present() -> None:
    from homeassistant.components.light import ColorMode, LightEntityFeature

    ent = _fake_light(led=_led("auto", 0), config={"light": 0})
    assert ent._using_led is True
    assert ent.effect_list == ["auto", "on", "blink", "fast"]
    assert ent.supported_features == LightEntityFeature.EFFECT
    assert ent.supported_color_modes == {ColorMode.RGB}
    assert ent.color_mode == ColorMode.RGB


# --- (8) turn_on/turn_off write POST /config {"key": "light", ...} when /led is absent ---------


async def test_turn_on_and_off_write_config_light_when_led_is_absent() -> None:
    coordinator = SimpleNamespace(
        data=SimpleNamespace(led=None, config={"light": 0}),
        async_set_config=AsyncMock(),
    )
    ent = _fake_light(led=None, config={"light": 0})
    ent.coordinator = coordinator

    await ent.async_turn_on()
    coordinator.async_set_config.assert_awaited_once_with("light", 1)

    coordinator.async_set_config.reset_mock()
    await ent.async_turn_off()
    coordinator.async_set_config.assert_awaited_once_with("light", 0)


# --- (9) available whenever either source is present; unavailable only when both are missing ---


def test_available_whenever_either_source_is_present() -> None:
    led = _led(1, 0)
    assert _fake_light(led=led, config={}).available is True
    assert _fake_light(led=None, config={"light": 1}).available is True


def test_unavailable_only_when_both_led_and_config_light_are_missing() -> None:
    assert _fake_light(led=None, config={}).available is False
    assert _fake_light(led=None, config={"night": 1}).available is False


# --- (10) the old, overlapping switch.py entry for this LED is gone ----------------------------


def test_no_switch_description_still_carries_the_old_light_key() -> None:
    """`switch.py`'s `SWITCHES` used to have a `light` entry driving this exact physical LED
    through `POST /config` alone -- a strict subset of what this entity already does over
    `/led`. It is removed for good: `KibbleStatusLight`'s own fallback (tests 7-9 above) is
    the only surface for the vendor-stack case now, and its RGB colour picker (tests 2-4) is
    the only surface for the green channel -- no `switch.<feeder>_green_led` either."""
    assert "light" not in {d.key for d in SWITCHES}
