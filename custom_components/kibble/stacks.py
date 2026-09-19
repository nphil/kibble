"""Which feeder userland backs each entity, and how the coordinator turns a poll into that
answer.

## The two stacks

kibbled (this repo's own `agent/src/*.rs`) and LibreFeed's daemon (`librefeedd`, the sibling
`librefeed` repo's `daemon/src/*.rs`) are two independent, mutually exclusive HTTP agents --
`agent/src/stack.rs`: "kibbled runs only on the vendor stack ... `librefeedd` reports
`"librefeed"` from the same route". `kibble.set_mode`/`select.stack` reboot the feeder into
whichever one is not currently running.

Both agents serve the same route surface this integration polls, but not the same routes: some
are common to both (`GET /wifi`, `GET /cloud`, `POST /feed`, ...), some exist only because
kibbled added a route LibreFeed never picked up (nothing left in that column today -- see
below), and most of the newer switches/numbers/selects/text controls exist only because
LibreFeed "owns its own `/config`" and chose to make them writable, where kibbled's own
`agent/src/settings.rs` ships the *same* keys `writable: false` (documented offset, unverified
write -- `persist::write_setting` enforces this with a passing test,
`read_only_setting_is_rejected_before_touching_anything`). A control entity backed by a
`writable: false` vendor key is not merely undocumented on that stack, it is *proven* to 400 on
every write, so it is treated as unavailable there, identically to a route that 404s.

## Why this table, not per-file `if stack == ...`

Twelve platform files independently deciding "does my entity belong on this stack" would drift:
one file forgets a case, another encodes it differently, and nothing catches the mismatch. This
module is the ONLY place that answers that question -- every platform's `async_setup_entry`
(and the dynamically-created per-cat binary sensors) calls `applies_to`, never branches on
`Stack` itself. Correcting or extending stack applicability means editing `ENTITY_STACKS` once,
here.

## Reading `ENTITY_STACKS`

Keyed by `(Platform, key)`, where `key` is the exact string that platform passes as
`KibbleEntity.__init__`'s `key` (== the `unique_id` suffix, `entity_description.key` for the
data-driven platforms) -- NOT the `translation_key`, which sometimes differs (`text.py`'s
`detection_hours` vs. its backing `detect_range_from`/`_till` keys, for instance, is why
`text.py`'s table entries are keyed by the `HourRangeTextDescription.key` actually passed to
`super().__init__`). The `Platform` half matters: `switch.py`'s `"camera"` (the `/config`
stream-enable setting) and `camera.py`'s `"camera"` (the platform's one entity) are the same
bare string on two different platforms with two different answers -- a table keyed on the
string alone would silently conflate them the moment one of the two needed gating (confirmed
live: an earlier draft of this table did exactly that and hid `camera.<feeder>` from the vendor
stack entirely, caught by `test_stack_applicability.py`'s full-platform sweep).

A `(platform, key)` with no entry defaults to `_BOTH` -- both because that is the correct answer
for most of this integration's ~80 entities (telemetry, feed controls, Wi-Fi, the cloud switch,
Kibble's own face-ID pipeline -- all implemented in kibbled itself and confirmed reachable "on
either stack", `websocket.py`'s `timeline_items`), and because it is the SAFE failure mode for a
future entity whose author forgets to add a row here: it simply appears on both stacks, exactly
like every entity did before this module existed, rather than silently vanishing from one.
"""

from __future__ import annotations

from enum import StrEnum

from homeassistant.const import Platform


class Stack(StrEnum):
    """A feeder userland, spelled exactly as `GET /mode`'s `running`/`POST /mode`'s `mode`
    body do (`agent/src/stack.rs`) -- `select.py`'s `KibbleStackSelect` reuses these same two
    values as its own option list."""

    VENDOR = "vendor"
    LIBREFEED = "librefeed"


_VENDOR_ONLY: frozenset[Stack] = frozenset({Stack.VENDOR})
_LIBREFEED_ONLY: frozenset[Stack] = frozenset({Stack.LIBREFEED})
_BOTH: frozenset[Stack] = frozenset(Stack)


# --- The table -----------------------------------------------------------------------------
#
# Only entities NOT applicable to every stack are listed; everything else defaults to `_BOTH`
# (see the module docstring). There are no `_VENDOR_ONLY` rows left: the one vendor-only entity
# this integration ever had (`binary_sensor.manual_lock`, a read-only vendor setting whose MCU
# protocol is undecoded) was removed rather than gated -- see git history for that change. The
# constant stays imported/exported for the day a real one shows up, so adding it back is a
# one-line table edit, not a new code path.
ENTITY_STACKS: dict[tuple[Platform, str], frozenset[Stack]] = {
    # --- switch.py -----------------------------------------------------------------------
    # `night`/`microphone` (writable: true on vendor) and `cloud` (its own `/cloud` route, not
    # a `/config` key) are correctly absent from this table -- both. Every other boolean
    # `/config` setting below, `agent/src/settings.rs`'s `SETTINGS` table ships
    # `writable: false` on the vendor stack (verified offset, write unverified/unsafe) -- a
    # `KibbleSettingSwitch` toggle 400s there today, so it is not a working control on vendor.
    (Platform.SWITCH, "pet_detection"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "move_detection"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "eat_detection"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "feed_picture"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "eat_video"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "food_warn"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "time_display"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "camera"): _LIBREFEED_ONLY,  # the `/config` stream-enable setting
    (Platform.SWITCH, "light_mode"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "tone_mode"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "sound_enable"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "feed_sound"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "system_sound_enable"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "smart_frame"): _LIBREFEED_ONLY,
    # `detection_overlay`: unlike every row above, not merely `writable: false` on vendor --
    # the vendor's `agent/src/settings.rs` has no such key at all, since there is no vision
    # pipeline there to draw a card overlay from.
    # `bowl_empty`: LibreFeed's own hysteretic verdict; the vendor firmware has no such field.
    (Platform.BINARY_SENSOR, "bowl_empty"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "detection_overlay"): _LIBREFEED_ONLY,
    (Platform.SWITCH, "detection_overlay_ignored"): _LIBREFEED_ONLY,
    # --- number.py -------------------------------------------------------------------------
    # `feed_amount*` are local HA preferences with no device round trip at all -- both, and
    # absent below. Every `SETTING_NUMBERS`/`KibbleEatHoldNumber` entry reads a `/config` key
    # that is `writable: false` on vendor, same reasoning as the switches above.
    (Platform.NUMBER, "pet_sensitivity"): _LIBREFEED_ONLY,
    (Platform.NUMBER, "move_sensitivity"): _LIBREFEED_ONLY,
    (Platform.NUMBER, "detect_interval"): _LIBREFEED_ONLY,
    (Platform.NUMBER, "surplus_standard"): _LIBREFEED_ONLY,  # also LibreFeed's own definition -- const.py
    (Platform.NUMBER, "eating_hold"): _LIBREFEED_ONLY,  # backed by `eat_sensitivity`, writable: false on vendor
    # --- select.py -------------------------------------------------------------------------
    # `wifi` (its own `/wifi/scan` route), `label_face` (kibbled's own `agent/src/faces.rs`,
    # confirmed "on either stack" by `websocket.py`'s `timeline_items` doc), and `stack` itself
    # (bridges both by design) are correctly absent -- both.
    (Platform.SELECT, "camera_indicator"): _LIBREFEED_ONLY,  # `/led`'s `camera` field -- vendor has no `/led`
    (Platform.SELECT, "selected_sound"): _LIBREFEED_ONLY,  # `/config` key, writable: false on vendor
    (Platform.SELECT, "surplus_control"): _LIBREFEED_ONLY,  # `/config` key, writable: false on vendor
    # --- text.py -----------------------------------------------------------------------------
    # All three: each backs a `writable: false` `/config` minutes-of-day pair on vendor
    # (`detect_range_from/_till`, `light_range_from/_till`, `tone_range_from/_till`).
    (Platform.TEXT, "detection_hours"): _LIBREFEED_ONLY,
    (Platform.TEXT, "status_led_hours"): _LIBREFEED_ONLY,
    (Platform.TEXT, "do_not_disturb_hours"): _LIBREFEED_ONLY,
    # --- binary_sensor.py --------------------------------------------------------------------
    # `feeding`/`eating`/`reachable`/`hopper_*_empty`/`cat_present_*` (dynamic, gated by the
    # virtual "cat_present" key below) are all both.
    # Virtual key: gates the whole dynamically-created-per-cat listener in `binary_sensor.py`
    # (there is no single fixed `key` for these entities -- see that module's `async_setup_entry`).
    # `GET /cats` is kibbled's own `agent/src/faces.rs` `Gallery`; both -- listed for the reader,
    # not because omitting it (default `_BOTH`) would behave any differently.
    (Platform.BINARY_SENSOR, "cat_present"): _BOTH,
    # --- button.py ---------------------------------------------------------------------------
    # `feed`/`feed_hopper_1`/`feed_hopper_2`/`cancel_feed` are both (`POST /feed(/cancel)`).
    (Platform.BUTTON, "beep"): _LIBREFEED_ONLY,  # `POST /beep` -- absent from kibbled's own route table
    (Platform.BUTTON, "replace_desiccant"): _LIBREFEED_ONLY,  # `POST /desiccant` -- same, absent from kibbled
    # --- event.py ----------------------------------------------------------------------------
    # `GET /state`'s `keys`/`last_key` ring: api.py's `FeederState` docs both fields
    # LibreFeed-only outright (not merely "an old agent lacks them").
    (Platform.EVENT, "button_pairing"): _LIBREFEED_ONLY,
    (Platform.EVENT, "button_1"): _LIBREFEED_ONLY,
    (Platform.EVENT, "button_2"): _LIBREFEED_ONLY,
    # image.py, light.py, media_player.py, camera.py, sensor.py: every entity there is `_BOTH`
    # (dish snapshots and the pending/last-detection crops are kibbled's own
    # `agent/src/feed_capture.rs`/`ai.rs`/`faces.rs`, confirmed serving both raw-H.264 and
    # ready-made-JPEG shapes from the SAME `GET /feeds`/`GET /events` routes; the status light
    # already branches on `/led` vs. `config["light"]` internally instead of needing a
    # creation-time gate; the speaker's `POST /speak`/`POST /clips` routes exist on kibbled too
    # -- the current audible-silence gap is a separate, actively-being-fixed device bug, not a
    # missing route), so none of them need a row here.
    #
    # sensor.py's exceptions:
    (Platform.SENSOR, "agent_starts"): _LIBREFEED_ONLY,  # `kibbled_start_count` -- kibbled's own restart counter
    # `GET /calibration` (`calibration.rs`) is a LibreFeed-only route, same footing as `/led`/
    # `/desiccant` above -- kibbled has no bowl-fill calibration concept at all.
    (Platform.SENSOR, "bowl_fill_calibration_hopper_1"): _LIBREFEED_ONLY,
    (Platform.SENSOR, "bowl_fill_calibration_hopper_2"): _LIBREFEED_ONLY,
}


def applies_to(platform: Platform, key: str, stack: Stack | None) -> bool:
    """Whether the entity (or, for `binary_sensor.py`'s dynamic per-cat sensors, the virtual
    `"cat_present"` key) named `key` on `platform` should be created while `stack` is running.

    `stack=None` -- the coordinator could not tell which userland is running this cycle (an
    agent old enough to 404 on `GET /mode` and report no `"stack"` field in `GET /state`
    either) -- always returns `True`. This is deliberate, not a placeholder: a wrong guess
    would delete entities (and, for `both`-classified ones, their statistics) a user may
    actually be relying on, where creating the superset merely reproduces this integration's
    behaviour from before this module existed. `coordinator.py`'s own `detect_stack` is the
    only place that ever manufactures `None` for this reason.
    """
    if stack is None:
        return True
    return stack in ENTITY_STACKS.get((platform, key), _BOTH)


def detect_stack(*, mode_running: str | None, state_stack_field: object) -> Stack | None:
    """Which stack is running, from the two signals `coordinator.py`'s `_fetch_all` already
    polls every cycle -- never a fresh probe of its own.

    `mode_running`: `coordinator.data.stack.running` (i.e. `GET /mode`'s `"running"`) when that
    fetch succeeded this cycle, else `None`. This is the primary, always-trustworthy signal
    when present: both kibbled and LibreFeed serve `GET /mode` and report their own real
    identity there (`agent/src/stack.rs`: "kibbled runs only on the vendor stack, so `running`
    is always `"vendor"` here; `librefeedd` reports `"librefeed"` from the same route").

    `state_stack_field`: `coordinator.data.state.raw.get("stack")`, i.e. whatever `GET
    /state`'s own raw JSON carries under a `"stack"` key. kibbled's `agent/src/state.rs` never
    emits this key at all (confirmed absent from its `Snapshot` builder), so the literal string
    `"librefeed"` here is an unambiguous fallback for an agent that, for whatever reason,
    didn't answer `/mode` this exact cycle but still self-identifies in `/state`.

    Returns `None` -- undetermined -- when neither signal identifies a stack: `/mode` 404ing
    (an agent old enough to predate `agent/src/stack.rs` entirely) with no `"stack"` field
    either. Callers MUST treat `None` as "create every entity" (`applies_to` already does) --
    never default it to `Stack.VENDOR` just because an old agent can only realistically be
    running the vendor stack: this function cannot tell "genuinely too old" apart from "this
    one cycle's `/mode` call happened to fail for an unrelated reason", and guessing wrong in
    the entity-creation direction deletes entities a wrong guess cannot un-delete for free.
    """
    if mode_running is not None:
        try:
            return Stack(mode_running)
        except ValueError:
            return None  # an unrecognised `running` value (e.g. a future "recovery") -- not a guess
    if state_stack_field == Stack.LIBREFEED.value:
        return Stack.LIBREFEED
    return None
