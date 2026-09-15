"""Constants for the Kibble integration."""

from __future__ import annotations

DOMAIN = "kibble"

CONF_HOST = "host"
CONF_PORT = "port"

DEFAULT_PORT = 8765
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

# Portions, not grams: the feed struct carries one byte per auger and the MCU turns each
# unit into one dispense cycle.
MIN_AMOUNT = 1
MAX_AMOUNT = 20
