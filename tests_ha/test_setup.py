"""Real-Home-Assistant integration smoke test for the pipeline-v2 rebuild.

Runs `custom_components/kibble` against a real `homeassistant` core via
pytest-homeassistant-custom-component (`/data/home/tmp/hatest/bin/python -m pytest tests_ha`)
-- the only place `__init__.py`, `config_flow.py`, `coordinator.py`, `websocket.py` and every
entity platform file actually execute against real Home Assistant before a live deploy.

No network is used anywhere. `KibbleClient` is patched at the `_request`/`_get_bytes` level --
the only two methods that ever touch `aiohttp` -- so every dataclass parser (`FeederState.
from_json`, `DetectionEvent.from_json`, ...) still runs for real against a canned but
realistic agent response built from api.py's own documented shapes. The agent's separate local
push channel (`push.py`, a raw WebSocket the coordinator dials on its own) is patched to report
itself unsupported so the coordinator falls back to (already-patched) polling instead of ever
opening a real socket -- pytest-socket is active in this environment and blocks any real
socket a gap in either patch would otherwise reach for.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.kibble.coordinator as coordinator_mod
import custom_components.kibble.crop_geometry as crop_geometry_mod
from custom_components.kibble import PLATFORMS
from custom_components.kibble.api import KibbleClient, KibbleNotFoundError
from custom_components.kibble.const import CONF_HOST, CONF_PORT, DOMAIN, HOPPER_BOTH
from custom_components.kibble.push import KibblePushUnsupported

# The real production entry id this will eventually run against (per the assignment).
ENTRY_ID = "01M2HWYNKK5HMBR0J4J62XP5S2"
SECOND_ENTRY_ID = "01M2HWYNKK5HMBR0J4J62XSECN"
# TEST-NET-1 (RFC 5737): guaranteed non-routable, deliberately distinct from the real feeder
# (192.168.1.85) and the real HA host (192.168.1.146) -- nothing here is ever meant to resolve.
HOST = "192.0.2.85"
PORT = 8765
SERIAL = "KIBBLE-TEST-0001"
CAT_NAME = "Kitty"
CAT_SLUG = "kitty"


def _jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    """A tiny, real, Pillow-decodable JPEG -- not a hand-rolled fixture. `identity.
    features_from` runs its real appearance-descriptor pipeline against these bytes."""
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, format="JPEG")
    return buf.getvalue()


def _embedding_bytes(seed: int) -> bytes:
    """512 little-endian float32 -- exactly `identity.FACE_EMB_BYTES`/docs/36-ai-pipeline.md's
    "512 little-endian f32, L2-normalised" device embedding shape."""
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(512).astype("<f4")
    vec /= np.linalg.norm(vec)
    return vec.tobytes()


def _sample_json(k: int, t: int, prefix: str) -> dict[str, Any]:
    """One docs/36-ai-pipeline.md-shaped `samples[]` entry: a usable box (>=4% of frame,
    nowhere near the edge) with both a body and a face crop, so `store.pick_thumb` always
    has something to choose and `identity.features_from` gets real bytes for every field."""
    return {
        "k": k,
        "t": t,
        "box": [0.3, 0.3, 0.7, 0.7],
        "score": 0.93,
        "body": f"{prefix}-body.jpg",
        "face": {"jpeg": f"{prefix}-face.jpg", "emb": f"{prefix}-face.emb", "score": 0.81},
    }


class FakeFeeder:
    """An in-memory stand-in for one real LibreFeed or vendor agent's HTTP surface: every
    route `_fetch_all`, `ingest.py` and `websocket.py`'s own live `client.spool_stats()` call
    touch, built from api.py's documented request/response shapes -- never a loosened subset.

    Two events: a closed "eat" (`event_id=101`) and a still-open "visit" (`event_id=102`,
    more recent) -- both timestamped early today (local time) so `KibbleCatMealsTodaySensor`'s
    "since local midnight" filter and the store's 48h `recent_meals` window both include them
    regardless of what time this test happens to run.

    `stack`: what `GET /mode`'s `running` reports -- "librefeed" (default) or "vendor". Drives
    `stacks.detect_stack`/`applies_to`, so a "vendor" feeder must never create a
    LibreFeed-only entity (`docs/37-hopper-full.md`'s hopper buttons/sensors, among others).
    """

    def __init__(self, *, stack: str = "librefeed") -> None:
        self.stack = stack
        today = int(dt_util.start_of_local_day().timestamp())
        self.feed_ts = today + 15
        self.eat_ts = today + 30
        self.visit_ts = today + 90  # newer than eat_ts -> becomes last_seen once labelled

        self.assets: dict[str, bytes] = {
            "e101-scene.jpg": _jpeg_bytes((10, 10, 10)),
            "e101-s1-body.jpg": _jpeg_bytes((200, 150, 100)),
            "e101-s1-face.jpg": _jpeg_bytes((180, 140, 90)),
            "e101-s1-face.emb": _embedding_bytes(101),
            "e102-scene.jpg": _jpeg_bytes((20, 20, 20)),
            "e102-s1-body.jpg": _jpeg_bytes((190, 130, 90)),
            "e102-s1-face.jpg": _jpeg_bytes((170, 120, 80)),
            "e102-s1-face.emb": _embedding_bytes(102),
        }
        self.feed_assets: dict[str, bytes] = {
            "f1-before.jpg": _jpeg_bytes((60, 60, 60)),
            "f1-after.jpg": _jpeg_bytes((60, 80, 60)),
        }
        self.deleted_assets: list[str] = []
        self.hopper_full_calls: list[str] = []
        self.feed_calls: list[dict[str, Any]] = []
        self._feeds: list[dict[str, Any]] = [
            {
                "ts": self.feed_ts, "id": "f1", "amount1": 5, "amount2": 0, "manual": True,
                "before": "f1-before.jpg", "after": "f1-after.jpg", "confirmed": True,
            }
        ]

    def state_json(self) -> dict[str, Any]:
        return {
            "serial": SERIAL,
            "firmware": f"1.2.3-{self.stack}",
            "ble_firmware": 4,
            "volume": 5,
            "desiccant_days": 30,
            "feeding": False,
            "eating": False,
            "bowl_fill": 12,
            "bowl_empty": False,
            "bowl_occluded": False,
            "hopper_empty": [False, False],
            "hopper_level": [2, 2],
            # hopper 1 marked full once, 3 portions dispensed since, daemon has learned this
            # hopper runs 10 portions full-to-low -> 7 remaining, a real assertable number.
            # hopper 2 has never been marked full -> stays unknown.
            "hopper_full_at": [self.eat_ts, None],
            "hopper_portions_since_full": [3, None],
            "hopper_full_to_low": [10, None],
            "bowl_fill_local": [15, self.eat_ts],
            "event_counter": 2,
            "kibbled_start_count": 1,
            "kibbled_last_start_unix": self.eat_ts - 100,
            "kibbled_last_exit_code": None,
            "last_key": None,
            "keys": [],
        }

    def events_json(self) -> list[dict[str, Any]]:
        # Newest first, matching the real device's own ordering (docs/36-ai-pipeline.md).
        return [
            {
                "event_id": 102, "seq": 2, "ts": self.visit_ts, "end": None, "open": True,
                "class": "visit", "eat_start": None, "scene": "e102-scene.jpg",
                "samples": [_sample_json(1, self.visit_ts + 1, "e102-s1")],
                "image": "e102-s1-body.jpg", "image_before": None, "image_after": None,
            },
            {
                "event_id": 101, "seq": 1, "ts": self.eat_ts, "end": self.eat_ts + 30,
                "open": False, "class": "eat", "eat_start": self.eat_ts,
                "scene": "e101-scene.jpg",
                "samples": [_sample_json(1, self.eat_ts + 1, "e101-s1")],
                "image": "e101-s1-body.jpg", "image_before": None, "image_after": None,
            },
        ]

    def feeds_json(self) -> list[dict[str, Any]]:
        return list(self._feeds)

    def add_feed(self, **fields: Any) -> None:
        """Appends one more `GET /feeds` record the next poll will report -- lets a test push
        a feed the coordinator has not already ingested, without disturbing the canned "f1"
        every other test already relies on."""
        self._feeds.append(fields)


class _NoPushKibblePush:
    """Stands in for `push.KibblePush`: reports the (fake) agent as not offering the push
    protocol at all -- `_push_loop`'s own real, already-designed-for fallback for an agent
    build old enough to lack it -- so the coordinator polls exclusively and never opens a
    real socket to try."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def listen(self):
        raise KibblePushUnsupported("push disabled for the smoke test")
        yield  # pragma: no cover -- unreachable; makes this a generator function

    async def close(self) -> None:
        pass

    async def resync(self) -> None:
        pass


def _install_fake_agent(monkeypatch: pytest.MonkeyPatch, feeder: FakeFeeder) -> None:
    async def fake_request(
        self: KibbleClient,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        data: bytes | None = None,
        timeout: Any = None,
        not_found_is_missing: bool = False,
        nullable: bool = False,
        busy_error: Any = None,
    ) -> Any:
        route = (method, path)
        if route == ("GET", "/state"):
            return feeder.state_json()
        if route == ("GET", "/schedule"):
            return {"entries": [], "last_modified": 0}
        if route == ("GET", "/config"):
            return {}
        if route == ("GET", "/cloud"):
            return {"enabled": True, "last_error": None, "routes": [], "connections": []}
        if route == ("GET", "/mode"):
            return {
                "running": feeder.stack, "next": feeder.stack,
                "librefeed_installed": feeder.stack == "librefeed",
            }
        if route == ("GET", "/led"):
            return {"white": "auto", "green": 0, "camera": "auto"}
        if route == ("GET", "/desiccant"):
            return {"days_left": 30, "replaced_unix": feeder.eat_ts - 1000, "interval_days": 60}
        if route == ("GET", "/calibration"):
            return {"hoppers": [None, None]}
        if route == ("GET", "/wifi"):
            return {
                "ssid": "IoT", "bssid": "AA:BB:CC:DD:EE:FF", "freq_mhz": 2437,
                "band": "2.4GHz", "signal_dbm": -50, "ip": HOST, "state": "connected",
                "desired_ssid": "IoT", "last_error": None,
            }
        if route == ("GET", "/wifi/scan"):
            return []
        if route == ("GET", "/clips"):
            return []
        if route == ("GET", "/feeds"):
            return feeder.feeds_json()
        if route == ("GET", "/events"):
            return feeder.events_json()
        if route == ("GET", "/spool"):
            return {
                "used_bytes": 4096, "cap_bytes": 8 * 1024 * 1024, "files": 4,
                "evicted_total": 0, "opt_free_bytes": 40 * 1024 * 1024,
            }
        if method == "POST" and path == "/feed":
            feeder.feed_calls.append(dict(payload or {}))
            return {"ok": True}
        if method == "POST" and path == "/hopper/full":
            feeder.hopper_full_calls.append(payload["hopper"])
            return {"ok": True}
        if method == "DELETE" and path.startswith("/events/"):
            feeder.deleted_assets.append(path.removeprefix("/events/"))
            return {}
        raise AssertionError(f"FakeFeeder: unexpected request {method} {path}")

    async def fake_get_bytes(self: KibbleClient, path: str) -> bytes:
        if path.startswith("/events/"):
            data = feeder.assets.get(path.removeprefix("/events/"))
        elif path.startswith("/feeds/"):
            data = feeder.feed_assets.get(path.removeprefix("/feeds/"))
        else:
            data = None
        if data is None:
            raise KibbleNotFoundError(path)
        return data

    monkeypatch.setattr(KibbleClient, "_request", fake_request)
    monkeypatch.setattr(KibbleClient, "_get_bytes", fake_get_bytes)
    monkeypatch.setattr(coordinator_mod, "KibblePush", _NoPushKibblePush)
    # The fake feeder stands in for the fixed librefeed-media build. Its captures sit at "today"
    # plus a few seconds, which falls before the real crop-fix cutoff whenever the suite runs on
    # or before 2026-09-25; without this, the outcome would depend on the calendar date.
    monkeypatch.setattr(crop_geometry_mod, "LEGACY_CROP_BEFORE", 0)


@pytest.fixture
def feeder(hass: HomeAssistant) -> FakeFeeder:
    """Depends on `hass` (unused directly) so `dt_util`'s process-wide default timezone is
    already set to the test instance's own (US/Pacific -- see `async_test_home_assistant`)
    before "today"'s timestamps are computed, matching what `identity_summary`/
    `KibbleCatMealsTodaySensor` will later use to decide what counts as "today"."""
    return FakeFeeder()


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch, feeder: FakeFeeder) -> FakeFeeder:
    _install_fake_agent(monkeypatch, feeder)
    return feeder


@pytest.fixture
def vendor_fake_agent(monkeypatch: pytest.MonkeyPatch, hass: HomeAssistant) -> FakeFeeder:
    """Same canned data as `fake_agent`, but `GET /mode` reports the vendor stack -- proves
    LibreFeed-only entities (docs/37-hopper-full.md's hopper buttons/sensors, among others)
    stay off a feeder that cannot actually serve `POST /hopper/full`/`hopper_full_*` fields."""
    vendor_feeder = FakeFeeder(stack="vendor")
    _install_fake_agent(monkeypatch, vendor_feeder)
    return vendor_feeder


def _make_entry(entry_id: str = ENTRY_ID, *, unique_id: str = SERIAL) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        unique_id=unique_id,
        data={CONF_HOST: HOST, CONF_PORT: PORT},
        options={},
    )


@pytest.fixture
async def configured_entry(
    hass: HomeAssistant, enable_custom_integrations: None, fake_agent: FakeFeeder
) -> MockConfigEntry:
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    # `_async_forward_platforms_isolated` swallows one bad platform's exception so the other
    # eleven still load (__init__.py's own docstring) -- state LOADED alone only proves at
    # least one did. Every platform must actually have loaded against this canned agent, or
    # a real per-platform setup bug would pass silently.
    assert set(entry.runtime_data.loaded_platforms) == set(PLATFORMS)
    return entry


# --- setup / unload lifecycle -------------------------------------------------------------


async def test_setup_and_unload_round_trip(
    hass: HomeAssistant, enable_custom_integrations: None, fake_agent: FakeFeeder
) -> None:
    """async_setup_entry succeeds (state LOADED), async_unload_entry succeeds cleanly and
    closes the store, and a fresh async_setup_entry on a brand new entry works right after --
    nothing about the store's executor or KibbleClient's connection lock leaked."""
    entry = _make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert set(entry.runtime_data.loaded_platforms) == set(PLATFORMS)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    entry2 = _make_entry(entry_id=SECOND_ENTRY_ID, unique_id=SERIAL + "-2")
    entry2.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry2.entry_id)
    await hass.async_block_till_done()
    assert entry2.state is ConfigEntryState.LOADED
    assert set(entry2.runtime_data.loaded_platforms) == set(PLATFORMS)

    assert await hass.config_entries.async_unload(entry2.entry_id)
    await hass.async_block_till_done()
    assert entry2.state is ConfigEntryState.NOT_LOADED


async def test_ingest_writes_real_store_files(
    hass: HomeAssistant, configured_entry: MockConfigEntry, fake_agent: FakeFeeder
) -> None:
    """After setup, ingest has actually run: `kibble.db` and archived media exist as real
    files under `hass.config.path('kibble', entry_id)`, and the feeder was acknowledged."""
    root_path = Path(hass.config.path(DOMAIN, configured_entry.entry_id))
    assert (root_path / "kibble.db").is_file()
    media_files = list((root_path / "media").rglob("*.jpg"))
    assert media_files, "ingest should have archived at least one JPEG under media/"
    # every body/face/scene crop was fetched then acknowledged (DELETE /events/<name>)
    assert set(fake_agent.deleted_assets) >= {
        "e101-scene.jpg", "e101-s1-body.jpg", "e101-s1-face.jpg", "e101-s1-face.emb",
        "e102-scene.jpg", "e102-s1-body.jpg", "e102-s1-face.jpg", "e102-s1-face.emb",
    }


# --- hopper buttons/sensors (docs/37-hopper-full.md) ---------------------------------------


async def test_hopper_entities_created_and_mark_full_button_writes_through(
    hass: HomeAssistant, configured_entry: MockConfigEntry, fake_agent: FakeFeeder
) -> None:
    """The three hopper buttons and two hopper-remaining sensors exist (LibreFeed stack, per
    stacks.py's `_LIBREFEED_ONLY` gate), the remaining-portions math is real
    (`hopper_remaining(10, 3) == 7`), and pressing a button really calls `POST /hopper/full`
    with the right hopper id end to end through button.py -> coordinator.py -> api.py."""
    registry = er.async_get(hass)

    button_ids: dict[str, str] = {}
    for key in ("hopper_1_full", "hopper_2_full", "hopper_full"):
        entity_id = registry.async_get_entity_id("button", DOMAIN, f"{SERIAL}_{key}")
        assert entity_id is not None, f"missing button entity for {key}"
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state != STATE_UNAVAILABLE
        button_ids[key] = entity_id

    remaining_1 = registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_hopper_1_remaining")
    remaining_2 = registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_hopper_2_remaining")
    assert remaining_1 is not None
    assert remaining_2 is not None
    assert hass.states.get(remaining_1).state == "7"
    assert hass.states.get(remaining_2).state == STATE_UNKNOWN  # never marked full

    await hass.services.async_call(
        "button", "press", {"entity_id": button_ids["hopper_full"]}, blocking=True
    )
    await hass.async_block_till_done()
    assert fake_agent.hopper_full_calls == ["both"]


async def test_hopper_divider_defaults_to_two_compartments_and_survives_a_reload(
    hass: HomeAssistant, configured_entry: MockConfigEntry, fake_agent: FakeFeeder
) -> None:
    """The divider is a remembered choice with no feeder behind it: on by default, turning it
    off sticks, and a config entry reload (as after an HA restart) restores it rather than
    snapping back to the default."""
    entity_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{SERIAL}_hopper_divider")
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "on"

    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "off"

    assert await hass.config_entries.async_reload(configured_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"


async def test_hopper_food_names_and_mode_are_recorded_per_feed_and_single_mode_routes_to_hopper_1(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    fake_agent: FakeFeeder,
    hass_ws_client,
) -> None:
    """Naming both hoppers (`text.py`'s local `KibbleHopperFoodText`) records into the
    coordinator immediately; a feed ingested while still in the default dual mode freezes its
    own amounts/food names/mode onto the `feeds` row (docs/37-hopper-full.md's "historical
    accuracy"), and the timeline's `feed.sides` reflects exactly that split. Flipping the
    divider afterward never rewrites that already-recorded row, and reroutes a `hopper="both"`
    feed onto dispenser 1 alone at the full amount."""
    entry_id = configured_entry.entry_id
    coordinator = configured_entry.runtime_data
    registry = er.async_get(hass)
    client = await hass_ws_client(hass)

    food1_id = registry.async_get_entity_id("text", DOMAIN, f"{SERIAL}_hopper_1_food")
    food2_id = registry.async_get_entity_id("text", DOMAIN, f"{SERIAL}_hopper_2_food")
    assert food1_id is not None and food2_id is not None
    await hass.services.async_call(
        "text", "set_value", {"entity_id": food1_id, "value": "Kibble"}, blocking=True
    )
    await hass.services.async_call(
        "text", "set_value", {"entity_id": food2_id, "value": "Freeze-Dried"}, blocking=True
    )
    assert coordinator.hopper_food(1) == "Kibble"
    assert coordinator.hopper_food(2) == "Freeze-Dried"

    dual_ts = fake_agent.feed_ts + 100
    fake_agent.add_feed(
        ts=dual_ts, id="f-dual", amount1=1, amount2=2, manual=True,
        before=None, after=None, confirmed=True,
    )
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()

    stored = await coordinator.store.async_timeline_page(limit=30, cursor=None)
    [dual_row] = [i for i in stored["items"] if i.get("start") == dual_ts]
    assert (dual_row["feed"]["amount1"], dual_row["feed"]["amount2"]) == (1, 2)
    assert (dual_row["feed"]["food1"], dual_row["feed"]["food2"]) == ("Kibble", "Freeze-Dried")
    assert dual_row["feed"]["single"] is False  # dual was active at ingest

    await client.send_json_auto_id({"type": "kibble/timeline", "entry_id": entry_id})
    timeline = await client.receive_json()
    assert timeline["success"]
    [dual_item] = [i for i in timeline["result"]["items"] if i.get("start") == dual_ts]
    assert dual_item["feed"] == {
        "portions": 3.0, "scheduled": False, "confirmed": True, "single": False,
        "sides": [
            {"hopper": 1, "portions": 1, "food": "Kibble"},
            {"hopper": 2, "portions": 2, "food": "Freeze-Dried"},
        ],
    }

    # Flip the divider to single mode -- the already-recorded row must not change.
    divider_id = registry.async_get_entity_id("switch", DOMAIN, f"{SERIAL}_hopper_divider")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": divider_id}, blocking=True
    )
    await hass.async_block_till_done()

    await client.send_json_auto_id({"type": "kibble/timeline", "entry_id": entry_id})
    timeline_after = await client.receive_json()
    [dual_item_after] = [i for i in timeline_after["result"]["items"] if i.get("start") == dual_ts]
    assert dual_item_after["feed"] == dual_item["feed"]

    # "both" in single mode reaches the agent as dispenser 1 alone, at the full amount.
    await coordinator.async_feed(HOPPER_BOTH, 2)
    await hass.async_block_till_done()
    assert fake_agent.feed_calls[-1] == {"hopper": "1", "amount": 2}


async def test_hopper_entities_absent_on_vendor_stack(
    hass: HomeAssistant, enable_custom_integrations: None, vendor_fake_agent: FakeFeeder
) -> None:
    """kibbled (the vendor stack) has no `/hopper/full` route or `hopper_full_*` `/state`
    fields (docs/37-hopper-full.md) -- stacks.py's `ENTITY_STACKS` gates the three hopper
    buttons and two hopper-remaining sensors `_LIBREFEED_ONLY` specifically so they are never
    created there. Regression coverage for sensor.py's `async_setup_entry`: its
    `HOPPER_REMAINING_SENSORS` extend once built the entities unconditionally, skipping the
    `applies_to` gate every other stack-dependent list in the same function already used, so
    the two hopper-remaining sensors were created on the vendor stack too."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert set(entry.runtime_data.loaded_platforms) == set(PLATFORMS)

    registry = er.async_get(hass)
    for platform, key in (
        ("button", "hopper_1_full"), ("button", "hopper_2_full"), ("button", "hopper_full"),
        ("sensor", "hopper_1_remaining"), ("sensor", "hopper_2_remaining"),
    ):
        assert registry.async_get_entity_id(platform, DOMAIN, f"{SERIAL}_{key}") is None, (
            f"{platform}.{key} must not exist on the vendor stack"
        )

    # setup genuinely ran (not just an empty entity set): a both-stacks entity is present.
    assert registry.async_get_entity_id("button", DOMAIN, f"{SERIAL}_feed") is not None


# --- cat enrollment, labelling, and the per-cat entities it drives -------------------------


async def test_cat_labeling_workflow_through_websocket_and_entities(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    fake_agent: FakeFeeder,
    hass_ws_client,
) -> None:
    """`kibble/cats/add` creates the per-cat binary_sensor/sensor entities dynamically
    (binary_sensor.py/sensor.py's `_add_new_cats` listener); `kibble/label` marks both
    ingested events reviewed, copies their samples into training, and the presence/last-seen/
    last-meal/meals-today entities reflect exactly that afterward -- consumer-observable state,
    not internals."""
    entry_id = configured_entry.entry_id
    client = await hass_ws_client(hass)
    registry = er.async_get(hass)

    await client.send_json_auto_id(
        {"type": "kibble/cats/add", "entry_id": entry_id, "name": CAT_NAME}
    )
    add_resp = await client.receive_json()
    assert add_resp["success"]
    await hass.async_block_till_done()

    present_id = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{SERIAL}_cat_present_{CAT_SLUG}"
    )
    last_seen_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL}_cat_last_seen_{CAT_SLUG}"
    )
    last_meal_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL}_cat_last_meal_{CAT_SLUG}"
    )
    meals_today_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL}_cat_meals_today_{CAT_SLUG}"
    )
    assert present_id and last_seen_id and last_meal_id and meals_today_id
    # enrolled, but not yet identified in any event
    assert hass.states.get(present_id).state == "off"
    assert hass.states.get(meals_today_id).state == "0"

    await client.send_json_auto_id({"type": "kibble/timeline", "entry_id": entry_id})
    timeline = await client.receive_json()
    assert timeline["success"]
    items = timeline["result"]["items"]
    assert len(items) == 3  # the eat, the still-open visit, and one feed record -- merged
    [feed_item] = [item for item in items if item["kind"] == "feed"]
    assert feed_item["feed"] == {
        "portions": 5.0, "scheduled": False, "confirmed": True, "single": False,
        "sides": [{"hopper": 1, "portions": 5, "food": None}],
    }
    event_items = [item for item in items if item["kind"] in ("visit", "eat")]
    assert len(event_items) == 2
    assert all(item["identity"] == "unknown" for item in event_items)  # no training yet
    assert all(item["thumb"] is not None for item in event_items)
    uids = [item["uid"] for item in event_items]

    await client.send_json_auto_id(
        {"type": "kibble/label", "entry_id": entry_id, "uids": uids, "label": CAT_NAME}
    )
    label_resp = await client.receive_json()
    assert label_resp["success"]
    labeled_events = label_resp["result"]["events"]
    assert len(labeled_events) == 2
    assert {e["cat"] for e in labeled_events} == {CAT_NAME}
    assert {e["identity"] for e in labeled_events} == {"reviewed"}
    await hass.async_block_till_done()  # background: training copy, rebuild, identity refresh

    assert hass.states.get(present_id).state == "on"  # the open "visit" is now Kitty's
    present_attrs = hass.states.get(present_id).attributes
    assert present_attrs["last_seen"] is not None
    assert present_attrs["last_ate"] is not None

    assert hass.states.get(last_seen_id).state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    assert hass.states.get(last_meal_id).state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    assert hass.states.get(meals_today_id).state == "1"  # exactly one labelled eat, today

    # kibble/cats: the same roster, with real training/presence numbers behind it
    await client.send_json_auto_id({"type": "kibble/cats", "entry_id": entry_id})
    cats_resp = await client.receive_json()
    assert cats_resp["success"]
    [cat] = cats_resp["result"]["cats"]
    assert cat["name"] == CAT_NAME
    assert cat["present"] is True
    assert cat["training"] == {"total": 2, "face": 2, "body": 2}
    assert cats_resp["result"]["storage"]["device_spool"] == {
        "used_bytes": 4096, "cap_bytes": 8 * 1024 * 1024,
    }

    # kibble/review: both events are now reviewed -> nothing left to review
    await client.send_json_auto_id({"type": "kibble/review", "entry_id": entry_id})
    review_resp = await client.receive_json()
    assert review_resp["success"]
    assert review_resp["result"]["items"] == []
    assert review_resp["result"]["total"] == 0

    # kibble/event: full detail for one labelled event -- its sample now teaches CAT_NAME,
    # having followed the event's own label with no override of its own
    await client.send_json_auto_id(
        {"type": "kibble/event", "entry_id": entry_id, "uid": uids[0]}
    )
    detail_resp = await client.receive_json()
    assert detail_resp["success"]
    assert detail_resp["result"]["event"]["uid"] == uids[0]
    [sample] = detail_resp["result"]["samples"]
    assert sample["review"] is None
    assert sample["label"] == CAT_NAME
    assert sample["body"] is not None and sample["face"] is not None


# --- HTTP media view -------------------------------------------------------------------------


async def test_media_view_serves_archived_asset_and_blocks_traversal(
    hass: HomeAssistant,
    configured_entry: MockConfigEntry,
    fake_agent: FakeFeeder,
    hass_ws_client,
    hass_client,
) -> None:
    """GET /api/kibble/<entry_id>/media/<asset> for a real archived asset returns 200 with
    the exact bytes ingest wrote, immutably cacheable; a path-traversal attempt (an absolute-
    looking segment, or an encoded `..`) serves nothing."""
    entry_id = configured_entry.entry_id
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "kibble/timeline", "entry_id": entry_id})
    timeline = await ws.receive_json()
    [eat_item] = [i for i in timeline["result"]["items"] if i["kind"] == "eat"]
    thumb = eat_item["thumb"]
    assert thumb["id"].endswith("e101-s1-body.jpg")

    client = await hass_client()

    resp = await client.get(thumb["url"])
    assert resp.status == 200
    assert resp.content_type == "image/jpeg"
    assert resp.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    body = await resp.read()
    assert body == fake_agent.assets["e101-s1-body.jpg"]

    # an absolute-looking asset segment -- resolve_asset_path rejects a leading "/" outright
    traversal_resp = await client.get(f"/api/kibble/{entry_id}/media//etc/passwd")
    assert traversal_resp.status == 404

    # an encoded ".." -- aiohttp's own path normalization already 404s this before the view
    # handler (views.py's docstring) even runs, on top of resolve_asset_path's own rejection
    encoded_resp = await client.get(
        f"/api/kibble/{entry_id}/media/%2e%2e/%2e%2e/%2e%2e/etc/passwd"
    )
    assert encoded_resp.status == 404

    # a name that was never archived -- a clean 404, not an error
    missing_resp = await client.get(f"/api/kibble/{entry_id}/media/2020-01-01/nope.jpg")
    assert missing_resp.status == 404


# --- diagnostics -----------------------------------------------------------------------------


async def test_diagnostics_runs_clean_and_redacts_identifying_fields(
    hass: HomeAssistant, configured_entry: MockConfigEntry, fake_agent: FakeFeeder
) -> None:
    """diagnostics.py's async_get_config_entry_diagnostics runs against a real coordinator
    snapshot (asdict() over the live KibbleData, including the nested identity summary) and
    redacts the feeder's host/serial/Wi-Fi identifiers -- never raises, never leaks them."""
    from homeassistant.components.diagnostics import REDACTED

    from custom_components.kibble.diagnostics import async_get_config_entry_diagnostics

    diagnostics = await async_get_config_entry_diagnostics(hass, configured_entry)

    assert diagnostics["entry_data"][CONF_HOST] == REDACTED
    assert diagnostics["data"]["state"]["serial"] == REDACTED
    assert diagnostics["data"]["wifi"]["ssid"] == REDACTED
    assert set(diagnostics["coordinator"]["loaded_platforms"]) == {p.value for p in PLATFORMS}
    assert diagnostics["coordinator"]["feeder_reachable"] is True
