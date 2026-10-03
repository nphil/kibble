"""Constants for the Kibble integration."""

from __future__ import annotations

DOMAIN = "kibble"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_STREAM_URL = "stream_url"
#: Another camera entity to take the live stream from -- typically the one a Scrypted (or
#: Frigate, or go2rtc) integration already publishes for this same device. Preferred over
#: `stream_url` because a rebroadcast URL is not stable: Scrypted assigns its RTSP rebroadcast
#: an EPHEMERAL port, so a hardcoded url silently dies on the next Scrypted restart (observed
#: 2026-09-19: the configured port simply stopped listening and the camera went black).
#: Delegating to the entity lets HA resolve the current source every time it is asked.
CONF_STREAM_ENTITY = "stream_entity"
# The feeder's BLE MAC, once a Bluetooth proxy has actually seen it advertise
# (docs/25-ble-feed-frame.md). Optional: with it unset, an unreachable agent simply reports
# "unreachable" instead of trying a BLE fallback.
CONF_BLE_ADDRESS = "ble_address"
# Second-entity, third-party card adapter: gate on this option until the MCU's per-entry
# time encoding is confirmed (docs/schedule.md) -- see `KibbleCoordinator.
# _require_schedule_writes_enabled`. Default off; a wrong table could dispense at the wrong
# time or amount.
CONF_ENABLE_SCHEDULE_WRITES = "enable_schedule_writes"
# Retention now lives entirely in HA storage (docs/36-ai-pipeline.md) -- the feeder keeps no
# event/feed history of its own past its bounded transient spool. These are the only
# selectable policies; the options flow rejects anything else.
CONF_RETENTION_DAYS = "retention_days"
RETENTION_OPTIONS = (7, 14, 30, 90)
DEFAULT_RETENTION_DAYS = 14
# Scrypted's own HTTP origin (e.g. "http://192.168.1.69:11080") for the eating-clips feature
# (docs/39-eating-clips.md): empty = off entirely, no lookups, no retries, no clip ever shown
# on the timeline -- the feeder's own photos are the whole experience until this is set. Not
# validated against Scrypted itself (unlike `CONF_HOST` at initial setup): this is filled in
# well after the entry already works, and a wrong value should degrade to "no clips", not
# block saving every other option on this same form.
CONF_SCRYPTED_CLIPS_URL = "scrypted_clips_url"
# The judge model's own HTTP endpoint base (an OpenAI-compatible `/v1`, e.g. llama-swap's
# `http://192.168.1.69:9292/v1` -- docs/40-vision-judge.md). Empty (the default) turns the
# whole second-opinion-judge feature off: no requests, no description-filling, no verdict ever
# recorded. Not validated against the endpoint itself at save time (unlike `CONF_HOST` at
# initial setup) -- this is filled in well after the entry already works, and a wrong value
# should degrade to "no verdicts", not block saving every other option on this form.
CONF_VISION_JUDGE_URL = "vision_judge_url"
# The model alias llama-swap should route judge requests to. `qwen3-vl-4b` is the bake-off's
# own chosen persistent llama-swap model id; a development install that hasn't created it yet
# can point this at a stand-in already resident on the same server (e.g. "gemma4-e4b").
CONF_VISION_JUDGE_MODEL = "vision_judge_model"
DEFAULT_VISION_JUDGE_MODEL = "qwen3-vl-4b"
# CoralHub's own HTTP base URL, e.g. "http://192.168.1.69:8720" (docs/41-coral-recognition.md).
# Empty (the default) turns the whole Coral-backed recognizer off: no embed/health requests,
# no backfill, the histogram recognizer (`identity.py`) runs exactly as it always has -- same
# "empty means off" shape as `CONF_VISION_JUDGE_URL`/`CONF_SCRYPTED_CLIPS_URL`. Not validated
# against the endpoint itself at save time, also same as those two: this is filled in well
# after the entry already works, and a wrong value should degrade to "still using the
# histogram recognizer", never block saving every other option on this form.
CONF_CORALHUB_URL = "coralhub_url"
# The named bearer token CoralHub issued for this integration ("Kibble" on its own Settings
# page). Sent as `Authorization: Bearer <token>` on every request; blank when CoralHub has no
# token configured (an open-on-the-LAN server, `app.security.require_token`'s own "no tokens
# configured, no gate" contract) or when Coral is entirely unconfigured.
CONF_CORALHUB_TOKEN = "coralhub_token"


DEFAULT_PORT = 8765
DEFAULT_RTSP_PORT = 8554
DEFAULT_RTSP_PATH = "/sub"
# kibbled reads /dev/shm/config_shm with plain loads, so the DATA is nearly free; the limit is
# the feeder's effectively serial HTTP server, shared with the vendor's encoder on one small ARM
# core (load average ~8 at rest). Measured: a healthy single request is 0.6-1.5s, so one poll
# cycle of ~12 calls is 8-18s of honest work. A 10s interval therefore started a new cycle
# before the previous one could finish -- cycles overlapped, queued behind each other, timed
# out, and every entity went unavailable on a device that was answering fine. 45s is comfortably
# longer than the worst measured cycle, and nothing here (bowl fill, desiccant days, cached
# schedule) changes meaningfully faster than that.
DEFAULT_SCAN_INTERVAL = 45

# `async_setup_entry` returns within this many seconds whatever the feeder is doing (answering
# slowly, off, behind a busy network). Home Assistant reports "started" only once every
# integration's setup has returned, and the feeder's own first poll takes 8-18s (the comment
# above), so setup gives that poll only what is left of this budget; the poll itself carries on
# in a task the config entry owns and the entities that need its answer are created when it
# lands -- see `__init__.py`'s `async_setup_entry`.
SETUP_BUDGET_SECONDS = 5.0

MANUFACTURER = "Petkit"
MODEL = "YumShare Dual 2"

SERVICE_FEED = "feed"
SERVICE_CANCEL_FEED = "cancel_feed"

ATTR_HOPPER = "hopper"
ATTR_AMOUNT = "amount"
# Optional second amount for a `hopper="both"` feed: hopper 1 gets `amount`, hopper 2 gets
# `amount2` (defaults to `amount` when omitted, keeping every pre-existing caller -- schedules,
# automations, the feed buttons -- dispensing the same on both sides exactly as before). The
# daemon itself already honours this (`librefeed/daemon/src/compat.rs::feed`); this plumbs it
# through the service, the coordinator and the BLE fallback frame (`frame.py::hopper_amounts`).
ATTR_AMOUNT2 = "amount2"
ATTR_FEED_ID = "id"

HOPPER_1 = "1"
HOPPER_2 = "2"
HOPPER_BOTH = "both"
HOPPERS = [HOPPER_1, HOPPER_2, HOPPER_BOTH]

SERVICE_SCHEDULE_SET = "schedule_set"
SERVICE_SCHEDULE_ADD = "schedule_add"
SERVICE_SCHEDULE_REMOVE = "schedule_remove"
SERVICE_SCHEDULE_SET_ENABLED = "schedule_set_enabled"

SERVICE_SCHEDULE_CARD_ADD = "schedule_card_add"
SERVICE_SCHEDULE_CARD_EDIT = "schedule_card_edit"
SERVICE_SCHEDULE_CARD_REMOVE = "schedule_card_remove"
SERVICE_SCHEDULE_CARD_TOGGLE = "schedule_card_toggle"

# `dispenser-schedule-card`'s `device.type: custom` adapter's `actions` table (docs/custom.md)
# names this field "id" for add/edit/remove/toggle alike -- distinct from `ATTR_ENTRY_ID`
# ("entry_id"), the field name the pre-existing `schedule_remove`/`schedule_set_enabled`
# already committed to for other consumers.
ATTR_ID = "id"
ATTR_HOUR = "hour"
ATTR_MINUTE = "minute"

ATTR_ENTRIES = "entries"
ATTR_TIME = "time"
ATTR_HOPPER1_G = "hopper1_g"
ATTR_HOPPER2_G = "hopper2_g"
ATTR_ENABLED = "enabled"
ATTR_ENTRY_ID = "entry_id"

# Matches the device's own per-entry byte range (STUDY-schedule.md §3.3 / Localkit's
# Configuration.php `a1`/`a2`: 0-50).
MIN_SCHEDULE_AMOUNT = 0
MAX_SCHEDULE_AMOUNT = 50
# kibbled's own client-side cap: the vendor's 540-byte bus payload clamp allows at most 24
# 22-byte entries behind the 2-byte header before it would silently truncate the table.
MAX_SCHEDULE_ENTRIES = 24

# Portions, not grams: the feed struct carries one byte per auger and the MCU turns each
# unit into one dispense cycle.
MIN_AMOUNT = 1
MAX_AMOUNT = 20
# The two per-hopper feed-amount controls (`number.py`'s `feed_amount_hopper_1`/`_2`) allow 0,
# meaning "nothing from this hopper" -- kibble-card.ts's dual-mode hero sets both directly, one
# row per hopper, and a 0 there means the hopper is left out of the `kibble.feed` call entirely
# (see `dual-feed.ts`), never sent to the device as a 0-portion dispense. The combined
# `feed_amount` control (hopper="both", single-hopper mode's only control) keeps MIN_AMOUNT as
# its own floor -- it always means "dispense something".
MIN_HOPPER_AMOUNT = 0

SERVICE_WIFI_CONNECT = "wifi_connect"
ATTR_SSID = "ssid"
ATTR_PASSWORD = "password"

SERVICE_SAVE_CLIP = "save_clip"
SERVICE_RECORD_CLIP = "record_clip"
SERVICE_PLAY_CLIP = "play_clip"

ATTR_CLIP_NAME = "name"
ATTR_MEDIA_CONTENT_ID = "media_content_id"
ATTR_SECONDS = "seconds"

# `media_player.*.volume_level` is HA's own 0.0-1.0 (shown as 0-100% in the UI); the device's
# own writable range is `agent/src/settings.rs`'s "volume" setting (`Kind::Int{min:0,max:9}`),
# the exact same `config["volume"]` `media_player.py`'s `KibbleSpeaker.volume_level` already
# reads/writes through `POST /config`. Not `GET /state`'s own (different config_shm offset,
# currently unused by this integration) `volume` field.
MAX_DEVICE_VOLUME = 9

# A "clip" is a short prompt/announcement, not a recording -- bounds `record_clip`'s capture
# length. Not a device-confirmed limit, an integration-side sanity bound.
MIN_CLIP_SECONDS = 1
MAX_CLIP_SECONDS = 30

# `coordinator.py`'s repair issue, raised once the feeder has missed
# CONSECUTIVE_FAILURES_FOR_UNAVAILABLE polls in a row -- see its module docstring for the
# full availability policy this backs. One issue per entry (one issue id) with two wordings:
# the second is for a feeder that has not answered at all since Home Assistant started (so
# there is no snapshot, and no "last known values" for the first one's text to promise).
ISSUE_FEEDER_UNRESPONSIVE = "feeder_unresponsive"
ISSUE_FEEDER_UNRESPONSIVE_SINCE_START = "feeder_unresponsive_since_start"

SERVICE_BEEP = "beep"

ATTR_COUNT = "count"
ATTR_ON_MS = "on_ms"
ATTR_OFF_MS = "off_ms"

# The MCU buzzer's own writable ranges (`POST /beep` -- see `api.py`'s `KibbleClient.beep`);
# the agent 400s outside them. Defaults mirror the agent's own when a field is omitted.
MIN_BEEP_COUNT = 1
MAX_BEEP_COUNT = 10
DEFAULT_BEEP_COUNT = 2
MIN_BEEP_ON_MS = 20
MAX_BEEP_ON_MS = 2000
DEFAULT_BEEP_ON_MS = 100
MIN_BEEP_OFF_MS = 0
MAX_BEEP_OFF_MS = 2000
DEFAULT_BEEP_OFF_MS = 100

SERVICE_CALL_CATS = "call_cats"

SERVICE_SET_DESICCANT = "set_desiccant"

ATTR_DAYS_LEFT = "days_left"
ATTR_INTERVAL_DAYS = "interval_days"

# `POST /desiccant`'s own writable ranges (LibreFeed-only -- see `api.py`'s
# `KibbleClient.set_desiccant`); the agent 400s outside them.
MIN_DESICCANT_DAYS_LEFT = 0
MAX_DESICCANT_DAYS_LEFT = 365
MIN_DESICCANT_INTERVAL_DAYS = 1
MAX_DESICCANT_INTERVAL_DAYS = 365

# `pet_sensitivity`/`move_sensitivity`/`eat_sensitivity`'s own writable range -- LibreFeed's
# `/config` contract: 0-100, higher means more sensitive. Not the vendor's original 1-9
# Localkit scale (`docs/appendix-localkit.md`'s harvested app schema); LibreFeed owns this
# config now and defines its own.
MIN_SENSITIVITY = 0
MAX_SENSITIVITY = 100

# `detect_interval`'s own writable range, in seconds. The vendor's own Localkit-harvested
# schema (`docs/appendix-localkit.md`) documents the same key as "int, 0-300, Min seconds
# between detections" -- LibreFeed's daemon keeps that bound for the same reason the vendor
# had it: above a few minutes the body/eat/motion detectors are effectively disabled, so a
# wider range would just be dead UI.
MIN_DETECT_INTERVAL_S = 0
MAX_DETECT_INTERVAL_S = 300

# `surplus_standard`'s own writable range -- LibreFeed's `/config` contract: a bowl-fill
# percentage (0-100) above which food counts as "leftover". This is LibreFeed's own defined
# semantics, not vendor parity: the vendor's `surplusControl`/`surplusStandard` fields were
# never recovered with confidence (`kibble/docs/07-config.md`), and a later study
# (`34-bowl-fill-surplus.md`) found their real on-device meaning is an unrelated BLE-report
# throttle, not a feed-skip threshold.
MIN_SURPLUS_STANDARD = 0
MAX_SURPLUS_STANDARD = 100

# `text.py`'s `KibbleHopperFoodText`: a local, never-written-to-the-feeder label for what is
# loaded in one hopper. Meaningful only with the divider fitted (docs/37-hopper-full.md);
# empty means unnamed. Not a device-confirmed limit, just a sane cap for a dashboard label.
MAX_HOPPER_FOOD_LENGTH = 24

SERVICE_CLEAR_TRAINING = "clear_training"
ATTR_CAT = "cat"
ATTR_KEEP_UPLOADS = "keep_uploads"
