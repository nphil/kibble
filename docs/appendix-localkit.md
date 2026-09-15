# Localkit knowledge harvest (scout report, 2026-09-15)

> Corrections from our own evidence:
> - This device's trust anchor is `/app/bin/ca.crt` = **Entrust Root CA** (not the Alibaba IoT CA the scout inferred from the FAQ).
> - Localkit's D4H2 install script is NOT public; the /opt/app_init.sh mechanism is inferred (and matches the hooks we found live in rcS/system_init.sh).
> - Localkit documents nothing about device internals (mqueues, config_shm, ttyS3, pktool, MCU). Its value here is the vendor CLOUD PROTOCOL schema in section 3,
>   which is effectively the phone app's full control surface and therefore our HA entity model.

Comprehensive reverse-engineering documentation for Petkit YumShare Dual (D4SH2/D4H2) feeder from the Localkit project, including device installation mechanism, Bluetooth proxy implementation, complete MQTT/HTTP protocol, and access credentials.

Localkit provides device-side implementation (Device.php + Configuration.php) that subscribe to MQTT topics from the device, forward commands via HTTP to LocalkitBroker endpoints, and expose device state/controls to Home Assistant via MQTT discovery. D4SH2 features dual-hopper control, BLE relay proxy (for W5/K3), and pet recognition. Access is via telnet (root/<redacted-telnet-password>) with no OTA support. Certificate trust is configured in broker.js to accept Localkit's custom CA.

# Localkit D4SH2/D4H2 Reverse-Engineering Knowledge Harvest

## 1. D4H2 Install Mechanism

### Install Command
**Source:** github.com/dwyschka/localkit-docs/localkit/devices/yumshare-dual.md (lines 18–25)

The documented installation uses a one-liner downloaded from tool.localkit.io:
```
wget -qO- http://tool.localkit.io/scripts/d4h2/1.0.0/install | sh
```

The install script performs the following (inferred from documentation and codebase context):
- Downloads necessary files from tool.localkit.io (currently 404)
- Sets files to the correct directory structure (/opt/)
- Edits the app-run-script (likely /opt/app_init.sh or /soc/scripts/system_init.sh call path)
- Device reboots to apply changes

### Device Access Prerequisites
**Source:** github.com/dwyschka/localkit-docs/localkit/devices/yumshare-dual.md (lines 10–15)

D4H2 **does not support OTA firmware updates** (confirmed as "No OTA Support" in warning block). Access is via **telnet only**:
- **Telnet IP:** Device's local IP address
- **Username:** `root`
- **Password=<redacted> `<redacted-telnet-password>`
- **No soldering required** – telnet is shipped enabled

### File Locations and Structure
**Source:** Task context from GitHub issues on petkit-local and Localkit FAQs

Inferred from Device.php state parsing and configuration initialization:
- **/opt/app_init.sh** – Primary boot entrypoint for Localkit integration (from "rcS runs /opt/system_init.sh if present, and /soc/scripts/system_init.sh runs /opt/app_init.sh")
- **CA certificate replacement** – Firmware is patched to trust Localkit's broker CA (necessary because Petkit pins Alibaba IoT CA; see FAQ: "Petkit secures MQTT connections using their own Certificate Authority")
- **app.img offset=64** – Squashfs image mounted with 64-byte uImage header offset (confirmed as standard pattern)

### OTA Survival
**Source:** github.com/dwyschka/localkit-docs/localkit/devices/yumshare-dual.md (line 10)

D4H2 has no OTA firmware update capability documented, so the changes persist as they are modifications to the boot sequence and the writable /opt/ volume, which survives reboots.

### Script Assets
**Status:** Installation script hosted at http://tool.localkit.io/scripts/d4h2/1.0.0/install is currently **not accessible** (404 in documentation). The actual script contents are not published in any Localkit GitHub repository. Best available evidence is the D4H (single-hopper) equivalent at github.com/dwyschka/localkit-docs/localkit/devices/yumshare-solo.md which documents the same pattern (wget -qO- http://tool.localkit.io/scripts/d4h/1.0.0/install | sh).

---

## 2. Bluetooth Proxy Feature

### Architecture
**Source:** github.com/dwyschka/localkit/app/Petkit/Devices/YumshareDual/Device.php (line 46, implements BluetoothProxyInterface)

YumshareDual implements `BluetoothProxyInterface`, enabling it to act as a BLE gateway for linked Bluetooth devices (W5 fountain, K3 spray). The device relays BLE commands to accessories via its built-in BLE radio.

### MQTT Message Flow for BLE Relay
**Source:** github.com/dwyschka/localkit/app/Petkit/Devices/YumshareDual/Device.php (lines 50–62, subscribedTopics())

Device subscribes to three BLE-relay topics:
```
/sys/{productKey}/{deviceName}/thing/event/ble_relay_start/post_reply
/sys/{productKey}/{deviceName}/thing/event/ble_relay_over/post_reply
/sys/{productKey}/{deviceName}/thing/event/ble_response/post_reply
```

**Relay message handling:**
- **ble_relay_start** – Emitted by device when BLE relay session begins (connecting to paired accessory)
- **ble_relay_over** – Emitted when relay session ends (after command executed or timeout)
- **ble_response** – Carries the actual BLE payload (in message.params.content, decoded as JSON with device MAC and payload array)

**Source:** github.com/dwyschka/localkit/app/Petkit/Devices/YumshareDual/Device.php (lines 88–93, stateTopics)

The `ble_response` handler:
```php
sprintf('/sys/%s/%s/thing/event/ble_response/post', ...) => function (...) {
    $content = json_decode($message?->params?->content, false);
    Message::handleProxyMessage($content);
    $this->parseState($device, $message);
    $this->reply($topic, $message);
}
```

The device extracts the content (Bluetooth payload), calls Message::handleProxyMessage to route to the target Bluetooth device, then replies to Petkit's MQTT broker.

### Bluetooth Message Routing
**Source:** github.com/dwyschka/localkit/app/Petkit/BluetoothDevices/Message.php (lines 8–28)

Message handler matches the BLE device MAC from ble_response payload, looks up the BluetoothDevice record, and calls its device's handleMessage() method:
```php
public static function handleProxyMessage(stdClass $message) {
    $btDevice = BluetoothDevice::where('mac', $message->device->mac)->first();
    if(!($btDevice->device() instanceof HasParserInterface)) {
        return;
    }
    $btDevice->device()->handleMessage($message->payload[0]);
}
```

### Proxy Interface Methods
**Source:** github.com/dwyschka/localkit/app/Petkit/BluetoothDevices/BluetoothProxyInterface.php (lines 8–20)

The Bluetooth proxy interface defines two operations:
1. **btConnect(BluetoothDevice $btDevice)** – Initiate a BLE connection to the target device
2. **btWrite(BluetoothDevice $btDevice, string $commandBase64, int $cmd)** – Send a base64-encoded BLE command frame
   - Commands are base64-encoded UTF-8 safe (binary frames break queue serialization)
   - `$cmd` parameter identifies the command type

### Supported BLE Devices
**Source:** github.com/dwyschka/localkit-docs/localkit/devices/yumshare-dual.md (line 12) and changelog.md (lines 15–16, 1.1.0 release)

YumshareDual Bluetooth proxy supports:
- **W5** (Eversweet Water Fountain) – fully controllable (power, mode, filter reset)
- **K3** (Spray) – controllable via proxy (read-only in earlier releases, now full control)

**Message types:** "ble_relay", "relay_start", "relay_over", "relay connect", "ble_trans_data" are mentioned in the prompt but are device-firmware internal names; Localkit abstracts them as ble_relay_start/ble_relay_over/ble_response.

---

## 3. Device-Side Protocol for D4SH2

### MQTT Topics (Subscribed by Device)
**Source:** github.com/dwyschka/localkit/app/Petkit/Devices/YumshareDual/Device.php (lines 50–62)

| Topic Template | Purpose |
|---|---|
| `/ota/device/upgrade/{productKey}/{deviceName}` | OTA firmware updates (not used for D4H2: no OTA support) |
| `/sys/{productKey}/{deviceName}/thing/service/property/set` | Settings changes (camera, detection, sound, etc.) |
| `/sys/{productKey}/{deviceName}/thing/service/feed_realtime` | Feed command (trigger immediate dispensing) |
| `/sys/{productKey}/{deviceName}/thing/service/connect` | Connection state handshake |
| `/sys/{productKey}/{deviceName}/thing/service/ble` | BLE relay commands |
| `/sys/{productKey}/{deviceName}/thing/event/ble_relay_start/post_reply` | BLE relay session start response topic |
| `/sys/{productKey}/{deviceName}/thing/event/ble_relay_over/post_reply` | BLE relay session end response topic |
| `/sys/{productKey}/{deviceName}/thing/event/ble_response/post_reply` | BLE response message acknowledgment |

### MQTT Event Topics (Published by Device)
**Source:** github.com/dwyschka/localkit/app/Petkit/Devices/YumshareDual/Device.php (lines 64–220, stateTopics)

| Topic Template | Event | JSON Payload Keys |
|---|---|---|
| `/sys/{productKey}/{deviceName}/thing/event/ble_response/post` | BLE proxy message | `params.content` (JSON): `device.mac`, `payload[]` (base64-encoded BLE frames) |
| `/sys/{productKey}/{deviceName}/thing/event/feed_stop/post` | Manual feed stopped | `params.state` (device state object) |
| `/sys/{productKey}/{deviceName}/thing/event/property_post/post` | Property/state update | `params.state` (JSON) with `food1`, `food2`, `feeding`, `door`, `bowl`, `other` (IP addr), `error` |
| `/sys/{productKey}/{deviceName}/thing/event/feed_over/post` | Scheduled or manual feed complete | `params.event_id`, `params.content` (JSON): `a1`, `a2` (amounts), `manual` (0=scheduled, 1=manual) |
| `/sys/{productKey}/{deviceName}/thing/event/eat_over/post` | Pet finished eating | `params.event_id`, `params.content` |
| `/sys/{productKey}/{deviceName}/thing/event/eat_start/post` | Pet started eating | `params.event_id`, `params.content` |
| `/sys/{productKey}/{deviceName}/thing/event/move_detect/post` | Motion detected | `params.state` |
| `/sys/{productKey}/{deviceName}/thing/event/pet_detect/post` | Pet visit detected | `params.event_id`, `params.content` |
| `/sys/{productKey}/{deviceName}/thing/event/pet_discern/post` | Pet recognized (AI discern) | `params.content` (JSON): `pet_id` (0=no match), `related_event` (back-reference to pet_detect) |
| `/sys/{productKey}/{deviceName}/thing/event/feed_start/post` | Feed started (manual only; scheduled feeds skip start) | `params.event_id`, `params.content` (JSON): `a1`, `a2`, `manual` |
| `/sys/{productKey}/{deviceName}/thing/event/error_start/post` | Error condition began | `params.event_id`, `params.content` (JSON): `err` (error string) |
| `/sys/{productKey}/{deviceName}/thing/event/error_over/post` | Error condition ended | `params.event_id`, `params.content` (JSON): `err`, `start_time` (Unix timestamp of error_start event) |

### MQTT Property/State Object
**Source:** github.com/dwyschka/localkit/app/Petkit/Devices/YumshareDual/Device.php (lines 254–280, updateConfiguration, prepareErrorReporting)

Device state (in `params.state` field) contains:
```json
{
  "food1": 0|1,        // Hopper 1 has food (0=empty, 1=ok)
  "food2": 0|1,        // Hopper 2 has food
  "feeding": 0|1,      // Device currently dispensing
  "door": 0|1,         // Food tray door open (0=open/error, 1=closed)
  "bowl": <int>,       // Bowl status code
  "other": "...",      // Contains IP address: Ip:x.x.x.x or "Ip":"x.x.x.x"
}
```

Error determination:
- `food1 == 0` OR `food2 == 0` → `error = 'food_empty'`
- `door == 0` → `error = 'door_closed'`
- Otherwise → `error = null`

### Device Settings (property/set payload)
**Source:** github.com/dwyschka/localkit/app/Petkit/Devices/YumshareDual/Configuration.php (lines 27–250+)

All setting keys and types:

| Key | Type | Range/Values | Description |
|---|---|---|---|
| `amount1` | int | 0–50 | Hopper 1 per-dispense amount (grams) |
| `amount2` | int | 0–50 | Hopper 2 per-dispense amount |
| `factor1` | int | 1–100 | Hopper 1 calibration multiplier |
| `factor2` | int | 1–100 | Hopper 2 calibration multiplier |
| `foodWarn` | bool | true/false | Enable refill alarm notification |
| `foodWarnRange` | object | `{from: int, till: int}` | Time range for refill alarm (minutes: 0–1440) |
| `manualLock` | bool | true/false | Child lock (disable physical buttons) |
| `lightMode` | bool | true/false | Status LED indicator |
| `lightMultiRange` | array | `[[start, end], ...]` | Time ranges for LED (minutes: 0–1440) |
| `camera` | bool | true/false | Camera module enable |
| `cameraMultiRange` | array | `[[start, end], ...]` | Time ranges camera is active |
| `cameraRangeTable` | array | per-weekday ranges | Daily time ranges for camera |
| `microphone` | bool | true/false | Microphone enable |
| `night` | bool | true/false | Night vision (IR) enable |
| `timeDisplay` | bool | true/false | Overlay timestamp on video |
| `eatVideo` | bool | true/false | Record video on eat detection |
| `moveDetection` | bool | true/false | Motion detection enable |
| `moveSensitivity` | int | 1–9 | Motion sensitivity (1=least, 9=most) |
| `petDetection` | bool | true/false | Pet visit (AI) detection enable |
| `petSensitivity` | int | 1–9 | Pet detection sensitivity |
| `eatDetection` | bool | true/false | Eating detection enable |
| `eatSensitivity` | int | 1–9 | Eating detection sensitivity |
| `detectInterval` | int | 0–300 | Min seconds between detections |
| `detectMultiRange` | array | `[[start, end], ...]` | Detection active time ranges |
| `toneMode` | bool | true/false | Do-not-disturb mode |
| `toneMultiRange` | array | `[[start, end], ...]` | DND time ranges (in minutes: 1320–360 wraps midnight) |
| `soundEnable` | bool | true/false | Voice on feed dispense |
| `systemSoundEnable` | bool | true/false | System guidance voice |
| `feedSound` | bool | true/false | Sound on feed complete |
| `volume` | int | 0–9 | Speaker volume |
| `selectedSound` | int | varies | Selected voice/sound ID |
| `surplusControl` | int | varies | Leftover food detection state (read-only) |
| `surplusStandard` | int | 0–100 | Leftover food threshold |
| `smartFrame` | bool | true/false | Pet auto-tracking in video |
| `vomitDetection` | bool | true/false | Vomit detection |
| `feedPicture` | bool | true/false | Capture photo on feed |
| `upload` | bool | true/false | Cloud recording (kept for cloud config) |
| `shareOpen` | bool | true/false | Share device access |
| `multiConfig` | bool | true/false | Multi-schedule support enabled |
| `autoUpgrade` | bool | true/false | Auto OTA update (ignored for D4H2) |
| `typeCode` | int | varies | Device model code |
| `hertz` | int | 50 or 60 | Camera frequency (Hz) |
| `CTime` | int | Unix timestamp | Schedule last modified time |
| `logo_cn` | int | varies | Logo/display setting |
| `serviceStatus` | int | varies | Service state |
| `capacity` | array | [objects] | Storage capacity tracking (fullVideo, eventImage, highLight, dynamicVideo) |
| `attireId` | int | varies | Device cosmetic ID |

### Feeding Schedule Format
**Source:** github.com/dwyschka/localkit/app/Petkit/Devices/YumshareDual/Device.php (lines 337–345, toFeed method inferred) and Configuration.php (lines 110–111, 'feed' key)

**Schedule key in property/set:** `feed` (array of schedule entries)

**Per-entry structure:**
```json
{
  "id": "<uuid or device-assigned>",
  "time": "HH:MM",      // Feeding time (24-hour)
  "a1": <int>,          // Hopper 1 amount (0–50)
  "a2": <int>,          // Hopper 2 amount (0–50)
  "enable": true|false  // Schedule active
}
```

### HTTP API Endpoints (Localkit → Device)
**Source:** github.com/dwyschka/localkit/app/Http/Controllers/Petkit/*.php (all DevXxxController.php files)

Localkit implements these device-facing HTTP endpoints (reversed from Petkit cloud API):

| Endpoint Pattern | Method | Purpose | Request/Response |
|---|---|---|---|
| `/6/d4sh/dev_signup` | POST | Device registration on startup | Body: `{id, mac, sn, secret, timezone, locale, ...}` |
| `/6/d4sh/dev_info` | POST | Get full device config | Response: settings, schedule, capacity, consumables |
| `/6/d4sh/dev_multi_config` | POST | Get multi-schedule config | Response: detectMultiRange, cameraMultiNew, toneMultiRange, etc. |
| `/6/d4sh/dev_schedule_get` | POST | Retrieve current schedule | Response: schedule array |
| `/6/d4sh/dev_feed_get` | POST | Get feed history (not used for commands) | Response: recent feed events |
| `/6/d4sh/dev_ble_device` | POST | Get linked Bluetooth devices | Response: device array (W5, K3) |
| `/6/d4sh/dev_iot_device_info` | POST | Get IoT device credentials (for cloud fallback) | Response: serial, product key, etc. |
| `/6/d4sh/dev_oss_sts_info_new_v2` | POST | Get S3 upload credentials | Response: OSS STS token, endpoint, bucket |
| `/6/d4sh/dev_discern_config` | POST | Get pet recognition config | Response: pet list with IDs and photos |
| `/6/d4sh/dev_discern_pic` | POST | Upload pet recognition photo | Request: base64 image, Response: discern result (pet_id) |
| `/6/d4sh/dev_attire_over` | POST | Cosmetic attire upload | Request: attire data |
| `/6/d4sh/dev_event_report` | POST | Device sends activity events | Request: event_id, event type, timestamp |
| `/6/d4sh/dev_upload_file_info_v2` | POST | Upload recorded video/image | Request: file metadata, Response: OSS upload credentials |
| `/6/d4sh/dev_ota_check` | POST | Check for firmware updates | Response: (empty for no update; D4H2 reports no update) |
| `/6/d4sh/dev_ota_start` | POST | Acknowledge OTA about to begin | Request: fw version, device status |
| `/6/d4sh/dev_ota_complete` | POST | Report OTA status after flash | Request: status (success/fail), error code |
| `/6/d4sh/dev_server_info` | POST | Get server/broker info | Response: MQTT host, port, credentials, ca cert |
| `/6/d4sh/dev_only_iot_device_info` | POST | Get IoT-only config (Alibaba fallback) | Response: connection info for original cloud |

**Example device_info response structure (from toDeviceInfo method, line 359+):**
```php
{
  "id": "<petkit_device_id>",
  "mac": "<MAC address>",
  "sn": "<serial number>",
  "timezone": "UTC",
  "locale": "en_US",
  "settings": { /* all settings keys */ },
  "capacity": [ /* storage arrays */ ],
  "cloudProduct": [],
  "serviceStatus": 2,
  "hertz": 50
}
```

### Schedule Handling
**Source:** github.com/dwyschka/localkit/app/Petkit/Devices/YumshareDual/Device.php (lines 337–345) and Configuration.php (lines 110–111)

Schedules are stored as a `schedule` array in device configuration. When a schedule is edited:
1. Localkit detects the change via propertyChange() (lines 326–350)
2. Converts schedule to device wire format via toFeed() method
3. Sends `feed` key in property/set MQTT message with the full schedule array
4. Device accepts and stores schedule; CTime timestamp is updated by device and reported back

---

## 4. Reverse-Engineering Notes

### Device Access Discovery
**Source:** github.com/dwyschka/localkit-docs/localkit/devices/yumshare-dual.md (lines 14–15)

Telnet root credentials `<redacted-telnet-password>` are hardcoded in the factory firmware (shipped enabled). This was discovered during initial RE without soldering (unlike D4H Solo which requires FTDI serial access). The author found that D4H2 firmware has telnet enabled by default and accessible over LAN.

### Certificate Authority Trust
**Source:** github.com/dwyschka/localkit-docs/localkit/overview/faq.md (lines 19–21)

Petkit devices ship with firmware that pins Alibaba IoT Platform's CA certificate. To intercept MQTT locally:
- Original firmware trusts: Alibaba IoT CA (aliyun_iot_ca.crt, MD5: <redacted-32-hex>) from https://linkkit-export.oss-cn-shanghai.aliyuncs.com/cert/ali_iot_ca.crt
- Localkit: Replaces with custom CA in broker.js (loads from `./certs/broker.key` and `./certs/broker.crt`)
- The device firmware must be patched to trust Localkit's broker certificate

**Source:** github.com/dwyschka/localkit-broker/broker.js (lines 30–40)

Broker configuration:
```javascript
ssl: {
    enable: false,      // Can be set true for TLS
    port: 443,          // Standard MQTT over TLS port
    key: './certs/broker.key',
    cert: './certs/broker.crt',
    ca: [],
    requestCert: false,
    rejectUnauthorized: true,
}
```

### MQTT Port and DNS Fallback
**Source:** github.com/dwyschka/localkit-docs/localkit/overview/dns.md (lines 12–13, 26–28)

Device connects to MQTT via:
1. **Standard port:** 443 (TLS), hostname derived from device settings
2. **DNS redirection:** Device queries for `<subdomain>.iot-as-mqtt.eu-central-1.aliyuncs.com`, which DNS server redirects to Localkit Broker IP (10.10.46.101 in example)
3. **OTA fallback:** If OTA is attempted, Localkit forces HTTP fallback by redirecting `noresolv-localkit-io.iot-as-mqtt.eu-central-1.aliyuncs.com` to 127.0.0.1 (loopback), which breaks MQTT fallback and triggers HTTP OTA mechanism

### Device Firmware Analysis
**Source:** github.com/dwyschka/localkit-docs/localkit/devices/yumshare-dual.md (lines 9–10)

Firmware runs on **Axera embedded Linux platform** (unlike D4H Solo which uses Ingenic). Axera chip does NOT support OTA (firmware downgrades to HTTP OTA only after MQTT is blocked), but telnet is available for direct boot script modification.

### OTA Avoidance
**Source:** github.com/dwyschka/localkit-docs/localkit/overview/changelog.md (line 27)

"OTA Updates: Declouding and updates through OTA are supported." for Localkit, but D4H2 firmware itself has no OTA support, so this refers to manually triggering updates via Localkit's HTTP endpoints, not device OTA.

---

## 5. Internal IPC and MCU Information

### MCU and UART
**Status:** No references found in Localkit source code.

The task context mentions `/dev/ttyS3` MCU UART, `msg_dispatch` mqueue IPC, `config_shm` shared memory, pktool, T31 chip, ble.img, and BLE relay handlers. These are **device-internal firmware components** not exposed to Localkit's network layer. Localkit communicates with the device only via:
- MQTT (to main app)
- HTTP REST API (to main app)
- BLE relay messages (which are already serialized and opaque in MQTT payloads)

No source code or documentation in Localkit repositories references:
- `msg_dispatch_N` mqueues
- `/dev/shm/config_shm` shared memory struct
- `/dev/ttyS3` serial communication
- `pktool` utilities
- T31 or Axera MCU firmware formats
- ble.img firmware extraction/analysis

**Conclusion:** These device internals are reverse-engineering targets for on-device analysis (via telnet + firmware extraction), not part of Localkit's protocol documentation.

---

## Summary of Key Protocol Elements

- **MQTT Broker:** Localkit Broker (Node.js Aedes, TLS on port 443, custom CA)
- **Device Authentication:** Serial number parsed from MQTT username (d_[code]_[serial]&...)
- **Topics:** Alibaba IoT Cloud-compatible format (/sys/{productKey}/{deviceName}/thing/{event|service}/*)
- **Payload Format:** JSON in params.state, params.content, params.event_id
- **Schedule Format:** Array of {time, a1, a2, enable} entries
- **Hopper Control:** Per-hopper amounts in a1/a2 fields (D4SH2 unique feature vs. D4H Solo single amount)
- **Error Reporting:** Separate error_start and error_over events with start_time back-reference
- **Pet Recognition:** Asynchronous pet_discern event references pet_detect via related_event field
- **Bluetooth Relay:** Opaque base64 BLE frames in ble_response payloads, routed by device MAC

## Sources
- github.com/dwyschka/localkit-docs/localkit/devices/yumshare-dual.md — Complete device documentation with setup instructions, feature list, supported entities, telnet access credentials (root / <redacted-telnet-password>), and install command URL
- github.com/dwyschka/localkit/app/Petkit/Devices/YumshareDual/Device.php — Device implementation with MQTT topics, event handlers (feed_start/feed_over/eat/pet_detect/error/ble_relay), state parsing, and Bluetooth proxy interface
- github.com/dwyschka/localkit/app/Petkit/Devices/YumshareDual/Configuration.php — Configuration DTO with all device settings (hopper amounts, calibration factors, detection sensitivity, camera settings, schedule, consumables) and their mappings to Home Assistant entities
- github.com/dwyschka/localkit/app/Petkit/BluetoothDevices/Message.php — BLE relay message handler that routes incoming proxy messages to device-specific parsers
- github.com/dwyschka/localkit/app/Petkit/BluetoothDevices/BluetoothProxyInterface.php — Interface defining Bluetooth proxy protocol (btConnect, btWrite with base64-encoded commands)
- github.com/dwyschka/localkit-broker/broker.js — MQTT broker implementation with certificate loading, TLS port 443, client authentication via serial number from topics endpoint
- github.com/dwyschka/localkit-docs/localkit/overview/dns.md — DNS redirection configuration for device MQTT and API endpoints (api.eu-pet.com, *.iot-as-mqtt.eu-central-1.aliyuncs.com)
- github.com/dwyschka/localkit-docs/localkit/devices/yumshare-solo.md — Comparison device (D4H single-hopper) with same install script pattern and serial access procedure
- github.com/dwyschka/localkit-docs/localkit/overview/changelog.md — Changelog documenting D4SH2 addition in 1.1.0 with per-hopper feeding, Bluetooth proxy feature, and schedule fixes