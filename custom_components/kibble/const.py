"""Constants for the Kibble integration."""

from __future__ import annotations

DOMAIN = "kibble"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_STREAM_URL = "stream_url"
# The feeder's BLE MAC, once a Bluetooth proxy has actually seen it advertise
# (docs/25-ble-feed-frame.md). Optional: with it unset, an unreachable agent simply reports
# "unreachable" instead of trying a BLE fallback.
CONF_BLE_ADDRESS = "ble_address"
# Second-entity, third-party card adapter: gate on this option until the MCU's per-entry
# time encoding is confirmed (docs/schedule.md) -- see `KibbleCoordinator.
# _require_schedule_writes_enabled`. Default off; a wrong table could dispense at the wrong
# time or amount.
CONF_ENABLE_SCHEDULE_WRITES = "enable_schedule_writes"

DEFAULT_PORT = 8765
DEFAULT_RTSP_PORT = 8554
DEFAULT_RTSP_PATH = "/sub"
# kibbled reads /dev/shm/config_shm with plain loads, so polling is nearly free on the
# device; the limit is the feeder's single-client HTTP server, not the data.
DEFAULT_SCAN_INTERVAL = 10

MANUFACTURER = "Petkit"
MODEL = "YumShare Dual 2"

SERVICE_FEED = "feed"
SERVICE_CANCEL_FEED = "cancel_feed"

ATTR_HOPPER = "hopper"
ATTR_AMOUNT = "amount"
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

SERVICE_WIFI_CONNECT = "wifi_connect"
ATTR_SSID = "ssid"
ATTR_PASSWORD = "password"

SERVICE_LABEL_FACE = "label_face"
SERVICE_ADD_CAT = "add_cat"
SERVICE_IDENTIFY = "identify"

ATTR_CROP_ID = "crop_id"
ATTR_CAT = "cat"
ATTR_CAT_NAME = "name"

# The two reserved `cat` bucket values `agent/src/faces.rs` treats specially: moved and
# embedded like any real cat, but never counted as one (excluded from `GET /cats` and the
# classifier). Display strings are what `select.cat_feeder_label_face` shows in the picker;
# the bucket values are the wire values `POST /faces/label` actually receives.
CAT_LABEL_SKIP = "Skip"
CAT_LABEL_NOT_A_CAT = "Not a cat"
CAT_BUCKET_SKIP = "other"
CAT_BUCKET_NOT_A_CAT = "not_a_cat"

SERVICE_SAVE_CLIP = "save_clip"
SERVICE_RECORD_CLIP = "record_clip"
SERVICE_PLAY_CLIP = "play_clip"

ATTR_CLIP_NAME = "name"
ATTR_MEDIA_CONTENT_ID = "media_content_id"
ATTR_SECONDS = "seconds"

# `media_player.*.volume_level` is HA's own 0.0-1.0 (shown as 0-100% in the UI); the device's
# own writable range is `agent/src/settings.rs`'s "volume" setting (`Kind::Int{min:0,max:9}`),
# the exact same `config["volume"]` number.py's `KibbleVolumeNumber` already reads/writes
# through `POST /config`. Not `GET /state`'s own (different config_shm offset, currently
# unused by this integration) `volume` field.
MAX_DEVICE_VOLUME = 9

# A "clip" is a short prompt/announcement, not a recording -- bounds `record_clip`'s capture
# length. Not a device-confirmed limit, an integration-side sanity bound.
MIN_CLIP_SECONDS = 1
MAX_CLIP_SECONDS = 30
