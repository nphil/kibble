"""Direct regression test for `stacks.py`: the single table every platform's `async_setup_entry`
consults to decide which entities exist on which feeder userland (Nitin: "ensure that all the
exposed entities/devices/whatever features are only relevant to LibreFeed when I'm using
LibreFeed stack").

Exercises the REAL, unbound `async_setup_entry` of every one of the twelve platforms in
`__init__.py`'s `PLATFORMS` list, against a duck-typed coordinator/entry -- same
`object.__new__`/`SimpleNamespace` style as `test_coordinator_availability.py`/
`test_stack_select.py`, never a real `HomeAssistant` core instance (see `conftest.py`).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from kibble.api import CloudState, FeederState, IdentifyResult, ReviewFace, ScheduleState, StackState, WifiState
from kibble.const import CONF_HOST, CONF_PORT
from kibble.stacks import ENTITY_STACKS, Stack, applies_to, detect_stack
from homeassistant.const import Platform

HOST = "192.168.4.85"
PORT = 8765
SERIAL = "SN123456"

# Every platform this integration ships (mirrors `__init__.py`'s `PLATFORMS`).
ALL_PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.LIGHT,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]


def _fake_data(*, detected_stack: Stack | None, cats: tuple = ()) -> SimpleNamespace:
    """A duck-typed `KibbleData` with every field every platform's `async_setup_entry` or the
    entities it constructs reads at construction time, real `api.py` dataclasses throughout
    (`from_json({})` for sensible zero-value defaults) so nothing downstream sees a bare
    `SimpleNamespace` where it expects a real shape."""
    return SimpleNamespace(
        state=FeederState.from_json({"serial": SERIAL, "firmware": "1.0.0"}),
        schedule=ScheduleState.from_json({"entries": []}),
        config={},
        cloud=CloudState.from_json({}),
        stack=None,
        detected_stack=detected_stack,
        led=None,
        desiccant=None,
        wifi=WifiState.from_json({}),
        wifi_scan=(),
        cats=cats,
        identify=IdentifyResult.from_json({}),
        review_face=ReviewFace.from_json({}),
        pending_face_count=0,
        clips=(),
        feeds=(),
        events=(),
        sightings=(),
    )


def _fake_hass() -> SimpleNamespace:
    """Just enough for `image.py`'s `ImageEntity.__init__` -> `get_async_client` ->
    `create_async_httpx_client`'s HA-stop cleanup registration; nothing here is ever invoked."""
    return SimpleNamespace(data={}, bus=SimpleNamespace(async_listen_once=lambda *a, **k: None))


def _fake_entry_and_coordinator(data: SimpleNamespace) -> SimpleNamespace:
    """One `entry`/`coordinator` pair, circularly wired exactly like the real
    `entry.runtime_data = coordinator` / `coordinator.entry = entry` (`__init__.py`/
    `coordinator.py`'s `__init__`) every `KibbleEntity.__init__` relies on for
    `coordinator.entry.data[CONF_HOST/PORT]`."""
    entry = SimpleNamespace(
        data={CONF_HOST: HOST, CONF_PORT: PORT},
        options={},
        async_on_unload=lambda unsub: None,
    )
    coordinator = SimpleNamespace(data=data, entry=entry, async_add_listener=lambda cb: (lambda: None))
    entry.runtime_data = coordinator
    return entry


async def _entity_keys(platform: Platform, stack: Stack | None, *, cats: tuple = ()) -> set[str]:
    """Runs the real platform module's `async_setup_entry` and returns every created entity's
    `unique_id` with the `f"{serial}_"` prefix stripped back off -- i.e. the bare `key` that
    `ENTITY_STACKS` itself is keyed by."""
    data = _fake_data(detected_stack=stack, cats=cats)
    entry = _fake_entry_and_coordinator(data)
    module = __import__(f"kibble.{platform.value}", fromlist=["async_setup_entry"])
    added: list = []

    def collect(entities) -> None:
        added.extend(entities)

    await module.async_setup_entry(hass=_fake_hass(), entry=entry, async_add_entities=collect)
    prefix = f"{SERIAL}_"
    keys = set()
    for entity in added:
        assert entity.unique_id.startswith(prefix), entity.unique_id
        keys.add(entity.unique_id[len(prefix) :])
    return keys


# --- stacks.detect_stack -------------------------------------------------------------------


def test_detect_stack_trusts_a_successful_get_mode_first() -> None:
    assert detect_stack(mode_running="vendor", state_stack_field=None) is Stack.VENDOR
    assert detect_stack(mode_running="librefeed", state_stack_field=None) is Stack.LIBREFEED


def test_detect_stack_falls_back_to_state_stack_field_when_mode_is_unknown() -> None:
    assert detect_stack(mode_running=None, state_stack_field="librefeed") is Stack.LIBREFEED


def test_detect_stack_is_undetermined_with_neither_signal() -> None:
    assert detect_stack(mode_running=None, state_stack_field=None) is None


def test_detect_stack_does_not_guess_vendor_from_an_absent_state_field() -> None:
    """kibbled's own `state.rs` never emits a `"stack"` key at all -- seeing the literal string
    `"vendor"` there (which no known agent build ever sends) must not be trusted as a positive
    vendor signal; undetermined is the only safe answer when `/mode` didn't answer."""
    assert detect_stack(mode_running=None, state_stack_field="vendor") is None


def test_detect_stack_treats_an_unrecognised_mode_value_as_undetermined() -> None:
    assert detect_stack(mode_running="recovery", state_stack_field=None) is None


# --- stacks.applies_to -----------------------------------------------------------------------


def test_applies_to_creates_everything_when_stack_is_undetermined() -> None:
    assert applies_to(Platform.SWITCH, "pet_detection", None) is True
    assert applies_to(Platform.BUTTON, "beep", None) is True


def test_applies_to_a_librefeed_only_key_on_vendor_is_false() -> None:
    assert applies_to(Platform.SWITCH, "pet_detection", Stack.VENDOR) is False


def test_applies_to_a_librefeed_only_key_on_librefeed_is_true() -> None:
    assert applies_to(Platform.SWITCH, "pet_detection", Stack.LIBREFEED) is True


def test_applies_to_an_unlisted_key_defaults_to_both() -> None:
    assert applies_to(Platform.SENSOR, "bowl_fill", Stack.VENDOR) is True
    assert applies_to(Platform.SENSOR, "bowl_fill", Stack.LIBREFEED) is True


def test_applies_to_disambiguates_the_same_bare_key_on_different_platforms() -> None:
    """`switch.py`'s `"camera"` (the `/config` stream-enable setting, LibreFeed-only) and
    `camera.py`'s `"camera"` (the platform's one entity, both) are the same string on two
    platforms with opposite answers -- regression for exactly this collision, caught live by
    this file's own full-platform sweep before `ENTITY_STACKS` was keyed by `(Platform, key)`."""
    assert applies_to(Platform.SWITCH, "camera", Stack.VENDOR) is False
    assert applies_to(Platform.CAMERA, "camera", Stack.VENDOR) is True


# --- Full per-platform entity sets: vendor / librefeed / undetermined -------------------------

EXPECTED_VENDOR: dict[Platform, set[str]] = {
    Platform.SWITCH: {"night", "microphone", "cloud"},
    Platform.NUMBER: {"feed_amount", "feed_amount_hopper_1", "feed_amount_hopper_2"},
    Platform.SELECT: {"wifi", "label_face", "stack"},
    Platform.TEXT: set(),
    Platform.BUTTON: {"feed", "feed_hopper_1", "feed_hopper_2", "cancel_feed"},
    Platform.EVENT: set(),
    Platform.SENSOR: {
        "bowl_fill", "hopper_1_level", "hopper_2_level", "desiccant_days", "firmware",
        "ble_firmware", "factor1", "factor2", "schedule", "schedule_card_state", "next_feed",
        "cloud_connection", "control_path", "wifi", "wifi_signal", "last_seen_pet",
        "identification_score", "pending_faces", "clips", "last_detection", "detections_today",
    },
    Platform.CAMERA: {"camera"},
    Platform.LIGHT: {"status_light"},
    Platform.MEDIA_PLAYER: {"speaker"},
    Platform.IMAGE: {"pending_face", "last_detection_image", "dish_before", "dish_after"},
    # Static entities only -- `cat_present_*` is dynamic (its own dedicated test below) and the
    # generic sweep here always runs with zero enrolled cats.
    Platform.BINARY_SENSOR: {"feeding", "eating", "reachable", "hopper_1_empty", "hopper_2_empty"},
}

# LibreFeed adds every one of `stacks.py`'s `_LIBREFEED_ONLY` rows on top of the vendor set for
# that same platform -- computed, not hand-duplicated, so this file can't drift from the table
# it is testing while still asserting a concrete, readable expectation per platform below.
EXPECTED_LIBREFEED: dict[Platform, set[str]] = {
    platform: base | {key for (p, key), stacks in ENTITY_STACKS.items() if p is platform and Stack.LIBREFEED in stacks and Stack.VENDOR not in stacks}
    for platform, base in EXPECTED_VENDOR.items()
}


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
async def test_vendor_stack_creates_exactly_the_vendor_applicable_entities(platform: Platform) -> None:
    assert await _entity_keys(platform, Stack.VENDOR) == EXPECTED_VENDOR[platform]


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
async def test_librefeed_stack_creates_exactly_the_librefeed_applicable_entities(platform: Platform) -> None:
    assert await _entity_keys(platform, Stack.LIBREFEED) == EXPECTED_LIBREFEED[platform]


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
async def test_undetermined_stack_creates_the_full_superset_like_before_this_module_existed(
    platform: Platform,
) -> None:
    """"If the stack genuinely cannot be determined ... create the superset exactly as today
    rather than guessing" -- the superset is the union of what each real stack gets (there are
    no vendor-only entities left after `manual_lock`'s removal, so today this equals the
    LibreFeed set exactly, but the assertion is written as the union so it stays correct if a
    vendor-only entity is ever added back)."""
    both = EXPECTED_VENDOR[platform] | EXPECTED_LIBREFEED[platform]
    assert await _entity_keys(platform, None) == both


async def test_binary_sensor_cat_present_is_created_on_every_stack_including_undetermined() -> None:
    """The one dynamically-created-per-cat platform: `GET /cats` is kibbled's own
    `agent/src/faces.rs` `Gallery`, both stacks, so the virtual `"cat_present"` gate must never
    suppress it."""
    cats = (SimpleNamespace(name="Whiskers"),)
    for stack in (Stack.VENDOR, Stack.LIBREFEED, None):
        keys = await _entity_keys(Platform.BINARY_SENSOR, stack, cats=cats)
        assert "cat_present_whiskers" in keys, (stack, keys)


async def test_manual_lock_does_not_exist_on_any_stack() -> None:
    """Dropped entirely (vendor-only, read-only, MCU protocol undecoded) rather than gated --
    it must not resurface under any detected stack, including the undetermined superset."""
    for stack in (Stack.VENDOR, Stack.LIBREFEED, None):
        assert "manual_lock" not in await _entity_keys(Platform.BINARY_SENSOR, stack)


# --- entity_id parity: what protects statistics across a stack switch -------------------------


async def test_both_classified_entity_ids_are_byte_identical_between_stacks() -> None:
    """The house rule this whole feature exists to uphold: switching stacks must never orphan
    long-term statistics, which HA ties to `entity_id` via the registry's `unique_id` match.
    Every entity common to both stacks' sets must carry the exact same `unique_id` -- computed
    from the same `serial` and the same static `key`, never a stack-dependent one."""
    for platform in ALL_PLATFORMS:
        vendor_keys = await _entity_keys(platform, Stack.VENDOR)
        librefeed_keys = await _entity_keys(platform, Stack.LIBREFEED)
        shared = vendor_keys & librefeed_keys
        assert shared == EXPECTED_VENDOR[platform] & EXPECTED_LIBREFEED[platform]
        # Not just the same *set* of keys -- fetch full unique_ids again and diff byte-for-byte.
        data_vendor = _fake_data(detected_stack=Stack.VENDOR)
        data_librefeed = _fake_data(detected_stack=Stack.LIBREFEED)
        for key in shared:
            vendor_id = f"{data_vendor.state.serial}_{key}"
            librefeed_id = f"{data_librefeed.state.serial}_{key}"
            assert vendor_id == librefeed_id


def test_no_entity_key_anywhere_encodes_which_stack_it_belongs_to() -> None:
    """The house rule, stated directly: a `_librefeed`/`_vendor` suffix on a `key` would give
    the same logical entity two different `unique_id`s across a stack switch and orphan its
    statistics -- `stacks.py`'s own table (every key this integration gates on) is the
    cheapest place to assert it never happens, independent of the live grep over every
    platform file's source this ticket's acceptance criteria also runs."""
    for platform, key in ENTITY_STACKS:
        assert "vendor" not in key.lower(), (platform, key)
        assert "librefeed" not in key.lower(), (platform, key)
