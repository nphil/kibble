# STUDY-app.md — Petkit D4SH2/D4H2 Application-Layer Static Analysis

Scope: offline static analysis of `fs/app` (bin/, script/, lib/, etc/), `fs/audio`, `fs/alg`, `fs/soc`
from the extracted study tree. No contact with the live device (192.168.4.85) was made or attempted.
All 9 app binaries are `ELF 32-bit LSB executable, ARM, EABI5, dynamically linked, stripped` (confirmed
via `file`). No `readelf`/`objdump`/`strings`/`nm` exist on the analysis host, so a hand-rolled Python
ELF32 parser (program-header + `PT_DYNAMIC` walk, robust to a missing section-header table) and a
`rb"[\x20-\x7e]{5,}"` string extractor were used; both were validated first against known-good local
x86-64 ELF binaries (`/bin/ls`, `libc.so.6`, `libcurl.so.4`, exercising both `DT_HASH` and
`DT_GNU_HASH` symbol-table layouts) before being run against the real ARM binaries. Evidence is quoted
verbatim as `binary: "string"`. Statements not directly grounded in an extracted string/symbol are
marked **(inference)**.

Firmware build path recovered from embedded debug strings (`ctrl: "/home/tangwei/AX_D4SH/D4H2_D4SH2-ax620q/sdk/third_party/paho.mqtt.c-1.3.14/src/MQTTClient.c"`)
confirms the SoC is an **Axera AX620Q** and pins the exact MQTT client version in use.

---

## 1. Binaries & Imports

All 9 `fs/app/bin` executables share one link footprint: `libssl.so.1.0.0`, `libcrypto.so.1.0.0`,
`libcurl.so.4`, `libax_sys.so`, `libpthread.so.0`, `librt.so.1`, `libstdc++.so.6`, `libm.so.6`,
`libgcc_s.so.1`, `libc.so.6`. This is strong evidence of one shared internal static library
(config load/save, the mqueue dispatch bus, AES/MD5/SHA1 helpers, curl/SSL wrappers, `AX_SYS_LogPrint`
logging) linked into every binary — explaining why "unrelated" processes like `watchdog` pull in
`libcurl`/`libssl` even though they never make an HTTP call themselves.

| Binary | Size | Extra NEEDED beyond the common set | Role (from imports + strings) |
|---|---|---|---|
| `watchdog` | 79,784 | — | Process supervisor. Opens `/proc` (`watchdog: "/proc"`), opens `/dev/watchdog` (`watchdog: "/dev/watchdog"`), owns AES config-crypto helper symbols (`AES_set_encrypt_key`, `AES_set_decrypt_key`, `decrypt_config_data`, `encrypt_config_data`, `petkitRootfs_Aes_Encrypt_Keys_32`). |
| `ble` | 198,732 | — | Multi-role hub: UART bridge to the T31 dispenser MCU (`/dev/ttyS3`), BLE peripheral for phone provisioning, and a BLE **GATT client** ("relay manager") that scans/connects to other Petkit BLE peripherals (fountains etc.) on the cloud's behalf. No audio/video libs linked. |
| `media` | 348,116 | `libax_venc/ivps/audio/audio_3a/sys/ae/awb/af/proton/engine/mipi/nt_stream/nt_ctrl/ives/skel/interpreter`, `libsamplerate.so.0`, `libtinyalsa.so.2`, `libax_fdk.so`, `libfdk-aac.so.2` | Owns the entire camera pipeline (VIN→ISP→IVPS→VENC), OSD/attire overlay, JPEG snapshots, audio in/out (mic 2-way talk + `.aac` prompt playback), and `dlopen()`s `/alg/libalgo.so` in-process for pet/food detection. |
| `ctrl` | 742,148 (largest) | — | The cloud-facing router/orchestrator: Alibaba IoT MQTT client (Paho), all `dev_*` HTTP endpoints, feed scheduling, WiFi/OTA state machine, and the dispatcher that fan-outs cloud commands to `ble`/`media`/`cloud`/`agora` over the internal message bus. |
| `cloud` | 202,980 | — | OSS (Alibaba Object Storage) upload path: cloud video recording (CVR) lifecycle, picture/record upload, `EVP_aes_128_cbc` media encryption. |
| `agora` | 125,792 | `libagora-rtc-sdk.so` | Thin wrapper around the Agora RTC SDK; owns `agora_rtc_renew_token`. |
| `logUpload` | 83,980 | — | Diagnostic log uploader (curl + SHA1/MD5, `curl_formadd` → multipart upload). |
| `tserver` | 71,512 | — | Opens a **listening** TCP socket (`accept`/`bind`/`listen`) — a factory/production test server counterpart to `pktool`'s test commands. |
| `pktool` | 280,776 | — | Factory/production-test + low-level hardware CLI (GPIO/PWM/RTC/volume/config dump); also opens a listening socket and calls `system()`. |

Supporting libraries analyzed:

| Library | SONAME | NEEDED | Notes |
|---|---|---|---|
| `fs/app/lib/libcurl.so.4` | `libcurl.so.4` | libssl/libcrypto/libc | Vendor cURL build. |
| `fs/app/lib/libagora-rtc-sdk.so` | `libagora-rtc-sdk.so` | libm/libpthread/librt/libc only | Self-contained; embeds Agora's full worldwide edge-server list (`ap-*.agora.io`, `report-*.agora.io`). |
| `fs/app/lib/libsns_gc2053.so`, `libsns_gc2083.so` | matches filename | libpthread/libc | Sensor driver shims for the two supported image sensors (GC2053 day/night, GC2083 day/night — register tables live at `fs/app/etc/sensor/*.bin`). |
| `fs/alg/libalgo.so` | none | `libax_ivps/engine/interpreter/sys`, OpenCV-derived code, libstdc++ | 5.7MB; the actual pet/food/behavior detection engine, `dlopen()`-loaded by `media`. |
| `fs/soc/lib/libax_engine.so` / `libax_interpreter.so` | matches filename | libax_sys | Axera NPU inference runtime that executes the `.axmodel` files. |

---

## 2. IPC Map

| Name | Kind | Producer | Consumers | Evidence |
|---|---|---|---|---|
| `/config_shm` | POSIX shared memory (`shm_open`+`ftruncate`+mmap of a `config_t` struct) | Whichever process starts first (all `O_CREAT`) | **All 9 app binaries** (confirmed present in ctrl/ble/media/cloud/agora/watchdog/pktool/logUpload/tserver strings) | `ctrl: "/config_shm"`, `ctrl: "shm_open O_RDWR\|O_CREAT"`, `ctrl: "config_t.%s . ftruncate errno=%d"` |
| `/tmp/config.lock` | `flock()` file | n/a (mutex, not a data channel) | All 9 binaries (`config_lock` category hit in every strings dump) | `pktool: "/tmp/config.lock"` |
| internal mqueue bus (`dispatch_send_msg`/`dispatch_mqueue_read`) | POSIX message queue, per-process inbox, envelope `{msg_id, src, dst, msg_len}` | any process (symmetric send/recv API baked into the shared static lib) | any process — 108 distinct `dispatch_handler_*` callbacks across ble/media/cloud/ctrl/agora | `ble: "[%s]dispatch_mqueue_read: msg_id=%x,src=%d,dst=%d,msg_len=%d"`, `ble: "[%s]dispatch_send_msg: msg_id=0x%x,src=%d,dst=%d,msg_len=%d"` (byte-identical string in `ctrl`) |
| `/heartbeat` | mqueue/shm name (exact primitive not distinguishable from strings alone) | `ctrl`, `cloud`, `logUpload`, `ble` | `watchdog` (updates `state.watchdog.<name>_count/_pid`) | `ctrl: "/heartbeat"`; corroborated by `g_config->state.watchdog.{ctrl,ble,cloud,media,agora,p2p,card}_{count,pid}` fields |
| `/proc` (liveness) | procfs read | n/a | `watchdog` | `watchdog: "/proc"` — classic supervisor pattern: PID-liveness check to complement the `/heartbeat` "still making progress" check |
| `/audio` | named channel | `ctrl`, `pktool` | `media` (owns `AX_AI/AX_AO/AX_ADEC/AX_AENC`) | `ctrl: "/audio"`, `pktool: "/audio"`, also referenced inside `libax_audio.so` |
| `media_buffer.%s` | POSIX shared memory, per-channel (`%s` = main/sub) frame buffer | `media` | `media`-internal consumers (`libalgo.so` after `dlopen`) — likely also `agora`/`cloud` for streaming/upload, not directly provable from strings alone | `ctrl` (shared lib) / `media: "media_buffer.%s.shm_open,%s"`, `"shm_open O_RDWR\|O_CREAT"` |
| `/opt/user.conf`, `/opt/dev.conf` (+ `/param/user.conf`, `/param/dev.conf` mirrors) | flat files on UBI/SquashFS-adjacent overlay | whichever process calls `config_save` | all 9 binaries via `config_load`/`config_init` (shared lib) | `ctrl: "First Time Save dev.conf"`, `ctrl: "user.conf not found, load fail"`; `/param/*.conf` is (inference) the crash-safe fallback copy — `/opt` is UBI-mounted (`/opt` volume) while `/param` is a **separate** UBI volume per the boot-chain facts, so a corrupt `/opt` copy can be restored from `/param` |
| `/dev/ttyS3` | UART character device | `ble` | T31 dispenser MCU (external, off-SoC) | `ble: "/dev/ttyS3"`, `ble: "UART %s,fd:%d Init success! baudrate:%d"` |
| `/sys/class/gpio/gpio%d/*`, `/sys/class/pwm/pwmchip0/pwm%d/*` | sysfs GPIO/PWM control | `pktool` (and `media` for at least `pwmchip0`) | kernel GPIO/PWM drivers → LEDs/IR-cut/backlight hardware | `pktool: "/sys/class/gpio/export"`, `"/sys/class/pwm/pwmchip0/export"`; also present in `media.strings.txt` |
| listening TCP socket(s) | `AF_INET`/`AF_UNIX` socket (`accept`/`bind`/`listen`) | `pktool`, `tserver` | external factory-test PC tooling (inference — no literal port string recovered) | ELF import buckets: `pktool`/`tserver` both import `accept`,`bind`,`listen`,`setsockopt`,`socket`; `ctrl`/`cloud`/`media` import `bind`/`socket` too but as clients (`connect` present, no `listen`) |
| `/mqtt`, `/opt/mqtt_test` | Paho MQTT internal persistence/test keys | `ctrl` (Paho library internals) | n/a | `ctrl: "/mqtt"`, `ctrl: "/opt/mqtt_test"` — library-internal, not a Petkit-specific IPC channel |

**Note on `g_config->` cross-process references:** every process references the exact same dotted paths
(`g_config->state.ble.sta_data.feed_sta`, `g_config->usr.wifi.conn_sta`, …) because they all `mmap()`
the same `/config_shm` segment as a `config_t*` — this is a real single shared struct, not N independent
copies of a config file.

---

## 3. The Control Seam

**Conclusion: a first-party replacement agent should sit at the internal mqueue dispatch bus, not at
the MQTT/HTTP cloud edge and not inside any single hardware-facing binary.**

Evidence for the architecture:

- Every app binary links one shared static library providing `dispatch_send_msg()` / `dispatch_mqueue_read()`
  (identical debug strings in `ctrl` and `ble`: `"[%s]dispatch_send_msg: msg_id=0x%x,src=%d,dst=%d,msg_len=%d"`).
  Messages carry a small header (`msg_id`, `src`, `dst`, `msg_len`) routed to per-process POSIX message
  queues — this **is** the seam between "cloud-facing logic" and "hardware drivers", not sockets, not
  shared memory (shared memory is reserved for the config struct and raw video frame buffers).
- `ctrl` is the router: it owns every `net_dev_*`/HTTP call and the MQTT client, and translates each
  cloud command into a bus message addressed to whichever binary owns that capability. Concretely for
  feeding: `ctrl: "dispatch_handler_feed"` (cloud → local) calls `ctrl: "pk_ctrl_send_feed_event_msg"`
  which is consumed by `ble: "dispatch_handler_ble_feed_ctrl"` — evidenced by
  `ble: "----------feed_ctrl feed_amount_l=%d-----"` / `"----------feed_ctrl feed_amount_r=%d-----"`
  (separate left/right hopper amounts, matching the dual-hopper D4SH2) immediately followed by
  `ble: "T31 recv: Motor Run Config Cmd! wr:%d"` and a UART write to `/dev/ttyS3`.
- 108 distinct `dispatch_handler_*` functions were enumerated across `ble` (33), `media` (23), `cloud`
  (17), `ctrl` (31+), `agora` (4) — this is effectively the **complete internal capability surface** of
  the device (see full list embedded in section 9's cross-reference and reproduced by binary below).
  A replacement agent that can construct the same `{msg_id,src,dst,payload}` envelope and address any of
  these handlers gets first-party access to every capability the vendor app exposes, without needing to
  reimplement UART/BLE/camera/audio drivers itself — those stay owned by `ble`/`media`.
- The one caveat: the numeric `msg_id` enum values were **not** recoverable from strings alone (they are
  compiled integer constants, not printable literals). Determining the exact `msg_id` for e.g.
  `dispatch_handler_ble_feed_ctrl` needs either disassembly of the dispatch table or one read-only,
  non-invasive live capture (see §12).
- Two practical integration points follow from this: (a) replace `ctrl`+`cloud` wholesale (they are the
  only two binaries that talk to Alibaba's IoT cloud) while leaving `ble`/`media`/`agora`/`watchdog`
  running unmodified and simply feeding them bus messages the same shape `ctrl` used to — lowest risk,
  keeps all hardware-timing-sensitive code (UART framing, camera ISP tuning) untouched; or (b) run
  alongside `ctrl` as a peer process that opens its own inbound mqueue and both sends to and snoops
  traffic destined for `ble`/`media`, if `ctrl` must be kept for warranty/OTA reasons.

---

## 4. Config Schema Table (`g_config->…`, deduplicated)

Recovered by grepping every binary's `g_config->` string literals (all are `[%s][%s][%s][%d]: ` /
`printf`-style debug format strings — the debug macro itself proves these are the *actual* member
paths, not guesses). `pktool` alone references essentially the full struct (it backs the
`get_config_info` debug dump); binaries in the "Also referenced by" column were independently confirmed.
Meaning is **(inference)** unless self-evident from the name.

### `dev.*` — factory-programmed device identity (written once, PT/aging test time)
| Key | Meaning (inference) |
|---|---|
| `dev.dev_sn` | Device serial number |
| `dev.name` | Device display name |
| `dev.pt_step` | Production-test progress step |
| `dev.loaded` | Config-loaded flag |
| `dev.hw_param.ai_gain` / `.ai_vol` | Mic (audio-in) gain/volume trim |
| `dev.hw_param.ao_gain` / `.ao_vol` | Speaker (audio-out) gain/volume trim |
| `dev.hw_param.ircut_inverse` | IR-cut relay polarity |
| `dev.hw_param.ptz_X_inverse` / `.ptz_Y_inverse` | Axis-inversion flags (inference: vestigial from a camera-family SKU with pan/tilt; this feeder has no PTZ motor) |
| `dev.mac_info.a_APmac` / `.a_BLEmac` / `.a_STAmac` | WiFi AP-mode MAC, BLE MAC, WiFi station MAC |
| `dev.version_info.hardware.hardware_ble` / `.hardware_t31` | Hardware revision codes for the BLE radio module and the T31 dispenser MCU |
| `dev.version_info.ota_param.firmwareVer` / `.firmware_ble` | OTA version tracking (matches `/opt/version` JSON fields) |

### `state.ble.*` — live telemetry from the T31 MCU, mirrored into shared config by `ble`
| Key | Meaning (inference) |
|---|---|
| `state.ble.pt_dev` | Production-test device-type marker |
| `state.ble.adc_data.bat_ADC` / `.power_ADC` | Raw battery/power-rail ADC readings |
| `state.ble.adc_data.moto_curr` | Motor current sense (feed-jam / stall detection) |
| `state.ble.adc_data.proxl_rw` / `.proxr_rw` | Left/right hopper proximity (food-level) sensor raw values |
| `state.ble.io_data.io_det` | Digital IO detect (lid/door sensor?) |
| `state.ble.moto_runt_data.ctrl_ID` | Which motor/hopper is being driven |
| `state.ble.moto_runt_data.curt_max` / `.rt_curt` | Peak / real-time motor current |
| `state.ble.moto_runt_data.mot_runtime` | Motor run duration |
| `state.ble.moto_runt_data.pos.delta_hall_time_ms` / `.hall_run_pos` | Hall-sensor position feedback on the dispenser auger |
| `state.ble.moto_runt_data.scram_reason` | Emergency-stop reason code |
| `state.ble.moto_runt_data.speed` / `.sta` / `.result` | Motor speed / state / result code |
| `state.ble.sta_data.OTA` | T31 OTA state |
| `state.ble.sta_data.bat_capac` / `.ubat` | Battery capacity % / battery voltage |
| `state.ble.sta_data.ble_adv` | Whether BLE advertising is currently on |
| `state.ble.sta_data.edting` | "editing" — schedule-edit-in-progress flag |
| `state.ble.sta_data.err_code` / `.err_data` | Dispenser fault code/data |
| `state.ble.sta_data.feed_sta` | **Current feed state** (idle/feeding/jammed etc. — the field named in the original task brief) |
| `state.ble.sta_data.food1_lack` / `.food2_lack` | Hopper-1 / hopper-2 low-food flags (confirms independent dual-hopper level sensing) |
| `state.ble.sta_data.led_powe` | LED/power indicator state |

### `state.dev_pro.*` — device process/business state
| Key | Meaning (inference) |
|---|---|
| `state.dev_pro.acc_domain` | Access/account domain (multi-region cloud) |
| `state.dev_pro.aging_info.aging_enter_f` / `.aging_start_flag` / `.aging_working_f` | Factory burn-in ("aging") test state |
| `state.dev_pro.bind_out_time` / `.bind_timeout` / `.entry_bind_time` / `.err_bind_step` / `.first_linked` / `.in_the_banding` | Cloud account-binding/pairing state machine |
| `state.dev_pro.ble_open_by_bind` / `.ble_open_by_key` | Why BLE window is currently open (pairing flow vs. physical button) |
| `state.dev_pro.bowl_food_quantity` | Current estimated food quantity in bowl (feeds `foodWarn`) |
| `state.dev_pro.cvr_indate` / `.event_indate` / `.lapse_indate` | Cloud-Video-Recording / event / timelapse subscription validity |
| `state.dev_pro.cycleTime` | Feed-schedule cycle timer |
| `state.dev_pro.device_camera_enable` / `.tmp_devCamera_enable` | Camera enable + temporary override |
| `state.dev_pro.feed_less_replay` | "less food than expected" retry/alert flag |
| `state.dev_pro.feed_time` | Next/last scheduled feed time (named in original task brief) |
| `state.dev_pro.http_online_time_s` / `.last_httpMsgTime` / `.last_iotMsgTime` | Connectivity timing/health |
| `state.dev_pro.irlight_mode` / `.whitelight_mode` / `.isp_mode` | Camera lighting/ISP mode state |
| `state.dev_pro.lapse_rec_state` | Timelapse recording state |
| `state.dev_pro.leftover` | Leftover-food tracking |
| `state.dev_pro.online_mode` | Online/offline operating mode |
| `state.dev_pro.ota_sta.error_code` / `.ota_curr_sta` | OTA state machine |
| `state.dev_pro.power_on_src` | Power-on reason |
| `state.dev_pro.pt_mode` / `.pt_wifi` | Production-test mode flags |
| `state.dev_pro.sensor_reset` | Image-sensor reset flag |
| `state.dev_pro.stop_cloud` | Kill-switch for cloud connectivity |
| `state.dev_pro.time_synchronized` | NTP/RTC sync status |
| `state.dev_pro.toneTimeAllow` | Whether sound-prompt playback is currently allowed (quiet hours) |

### `state.watchdog.*` — supervised-process liveness (see §2/§3)
`{agora,ble,card,cloud,ctrl,media,p2p}_count` / `{…}_pid` — heartbeat count + current PID per supervised
process. **`card` and `p2p` have no corresponding binary in `fs/app/bin`** — (inference) vestigial fields
shared from a common Petkit device-family header (camera SKUs with SD-card recording and legacy P2P
streaming); this feeder neither exposes an SD card slot nor runs a `p2p` binary — `agora` fills the
live-streaming role instead.

### `state.online.step` — connection/bring-up step counter (inference).

### `usr.*` — user-configurable / cloud-synced settings
| Key | Meaning (inference) |
|---|---|
| `usr.accDomainTime`, `usr.ali_or_oci` | Region/account-domain selection (Alibaba vs. OCI — alternate cloud backend flag) |
| `usr.bind.code` / `.step` | Pairing/binding code + step |
| `usr.hertz` | Mains/PWM frequency selector (50/60Hz) |
| `usr.id_info.chip_id` / `.dev_id` / `.dev_srt` / `.srt_len` | SoC chip ID, device ID, a device secret/token (`dev_srt`) and its length |
| `usr.iot_keys.{createdAT,device_name,device_secret,id,mqttHost,product_key,region_id,type}` | **Alibaba Cloud Link-IoT three-tuple** (ProductKey/DeviceName/DeviceSecret) plus resolved `mqttHost` and `region_id` — this is the credential set consumed by the MQTT signing scheme in §6 |
| `usr.logSaveFlag` / `.log_level` | Local logging controls |
| `usr.mtu` | IoT link MTU |
| `usr.p2p_keys.{device_name,product_id,product_secret}` | A **second**, separate credential triple — (inference) provisioned for a legacy/alternate P2P transport, parallel to the Alibaba IoT keys and to Agora's own token auth |
| `usr.pkg_service[i].{cycle_time,end_time,name,start_time}` | Feed "package"/subscription service schedule array |
| `usr.rpt_batV` | Reported battery voltage |
| `usr.server_info.dns[i].ip`, `.linked`, `.nextTick`, `.servers[0..2].{api,ip}` | Up to 3 candidate API server host/IPs with DNS fallback — multi-region/failover server list |
| `usr.trackerInterval` / `.trackerLimit` | Pet-tracking sampling interval/limit (feeds the algo pipeline in §10) |
| `usr.user_info.{language,locale,timezone,userId}` | App-side user profile mirrored to device |
| `usr.wifi.conf.{pwd,ssid,uuid}` | WiFi credentials (`uuid` likely a provisioning-session correlation id, not a WiFi standard field) |
| `usr.wifi.conn_sta` | WiFi connection state (referenced directly in `ctrl`) |
| `usr.wifi.net_inf.{bssid,gw,gwmac,ipaddr,mac,mask,rsq,signal}` | Full network-interface status block |
| `usr.agora_keys` | Agora credential blob (only string evidence: `agora: "[agora]----------------------------g_config->usr.agora_keys is null..."` — confirms the field exists and that `agora` treats an unset value as an explicit error condition) |

### `usr.app_conf.*` — the actual "app settings" surface (maps almost 1:1 to phone-app UI toggles)
`CTime`, `alarmTime`, `attireId` (OSD costume/skin selection — see `defAttire.tar.gz`), `cameraMultiRange`
+ `cameraRangeTable`, `camera_enable`, `detectInterval`, `eatVideo`, `eat_det.{alarmInterval,alarmTime,
algoEnable,allDayAlarm,notify,sensitivity,trackEnable}`, `factor1`/`factor2` (inference: algo calibration
factors), `feedPicture`, `feedSound`, `foodWarn`/`foodWarnRange`, `irlight_enable`, `lapseEndTime`/
`lapseTime`/`lapseVideo` (timelapse), `ledlight_enable`, `lightMode`/`lightMultiRange`, `log_upload`,
`logo_cn`, `manualLock`, `mic_enable`, `move_det.{…same shape as eat_det…}`, `pet_color[i].{petColor,
petId}`, `pet_det.{…same shape…}`, `recording_type`, `selectedSound`, `smartFrame`, `soundEnable`,
`surplusControl`/`surplusStandard` (leftover-food threshold — matches `dev_pro.leftover`), `systemSoundEnable`,
`timestamp_enable`, `toneMode`/`toneMultiRange`, `upload`, `vedio_flip_enable`, `vomit_det.algoEnable`.

`{eat,move,pet,vomit}_det.*` all share the identical 7-field shape (`alarmInterval, alarmTime, algoEnable,
allDayAlarm, notify, sensitivity, trackEnable`) — one generic "detection event" config record reused for
four detector types, matching the four detection heads in `libalgo.so` (§10).

---

## 5. Encryption Notes

- **Local config-at-rest encryption** (protects `/opt/user.conf` / `/opt/dev.conf`, and their `/param`
  mirrors): raw OpenSSL 1.0.x low-level API — `AES_set_encrypt_key`, `AES_set_decrypt_key`,
  `AES_cbc_encrypt` — imported identically by **every** app binary (all load/verify their own config).
  `watchdog` additionally owns `decrypt_config_data`/`encrypt_config_data` wrapper functions and a key
  symbol literally named `petkitRootfs_Aes_Encrypt_Keys_32` (per BootChainStudy's find, confirmed
  independently here) — the `_32` suffix matches the AES-256 key length asserted elsewhere
  (`pktool: "Key must be 32 bytes for AES-256"`, also present in `watchdog`). The actual 32 raw key
  bytes are **not** recoverable by printable-string search (they are binary data, not text) — see §12.
- **Per-upload media encryption**: `cloud` additionally imports the modern EVP API —
  `EVP_CIPHER_CTX_new/free`, `EVP_EncryptInit_ex`, `EVP_EncryptUpdate`, `EVP_EncryptFinal_ex`,
  `EVP_aes_128_cbc`, plus `RAND_bytes` — consistent with generating a fresh random AES-128-CBC key per
  photo/clip before OSS upload. This is the `"aesKey":"%s"` field embedded directly in `ctrl`'s outbound
  event-report JSON (§6) — the key is transmitted in-band over the already-authenticated MQTT/HTTPS
  channel rather than derived from a static secret.
- **Integrity/signing helpers**: `MD5_Init/Update/Final` and `SHA1_Init/Update/Final` appear in almost
  every binary (file integrity checks — `pktool: "copy_file_and_check_md5"`, `"md5sum_file"` — and likely
  request signing); `ctrl` additionally imports `BIO_f_base64`/`BIO_new`/`BIO_push`/`BIO_read`/`BIO_write`
  (base64 encode/decode wrapper, e.g. for embedding binary signatures/keys in JSON/query strings).
- No embedded certificate pinning material beyond the already-known `bin/ca.crt` (Entrust Root, expires
  2026-11-27) was found; no additional client certs/keys were present as strings in `ctrl`/`cloud`.
- **Config binary format** (from the live backup, not re-derived here): `dev.conf` begins with a 32-hex
  MD5 prefix followed by ciphertext — consistent with "MD5 checksum of plaintext, then AES-256-CBC over
  the ciphertext-prefixed blob" or "MD5(key-material) stored alongside AES-CBC ciphertext"; disambiguating
  which requires either the raw key or a disassembly of `decrypt_config_data`.

---

## 6. Cloud Protocol

**Backend confirmed as Alibaba Cloud IoT Platform ("Link IoT" / IoT 设备接入), not a Petkit-custom stack.**
Evidence: `ctrl` statically links **Eclipse Paho MQTT C client v1.3.14** (`ctrl: "Eclipse Paho Synchronous MQTT C Client Library"`,
`ctrl: "/home/tangwei/AX_D4SH/D4H2_D4SH2-ax620q/sdk/third_party/paho.mqtt.c-1.3.14/src/MQTTClient.c"`)
wrapped by Alibaba's `aiot_` C-SDK calls (`ctrl: "MQTT user calls aiot_mqtt_connect api, connect"`), and
the MQTT host template `ctrl: "%s.iot-as-mqtt.%s.aliyuncs.com"` (`{productKey}.iot-as-mqtt.{region}.aliyuncs.com`
is Alibaba's standard IoT MQTT endpoint format) plus the previously-observed live connection to
`47.251.247.167:33882` (Alibaba Cloud US).

### MQTT topics (all literal format strings from `ctrl`)
Standard Alibaba Link IoT Thing-Model topics:
```
/sys/%s/%s/thing/event/%s/post                    /sys/+/+/thing/event/+/post_reply
/sys/%s/%s/thing/event/property/batch/post         /sys/+/+/thing/event/property/batch/post_reply
/sys/%s/%s/thing/event/property/post
/sys/%s/%s/thing/model/up_raw                       /sys/+/+/thing/model/up_raw_reply
/sys/%s/%s/thing/property/desired/delete            /sys/+/+/thing/property/desired/delete_reply
/sys/%s/%s/thing/property/desired/get               /sys/+/+/thing/property/desired/get_reply
/sys/%s/%s/thing/service/%s_reply                   /sys/+/+/thing/service/property/set
/sys/%s/%s/thing/service/property/set_reply
/ext/rrpc/%s/sys/%s/%s/thing/model/down_raw         /ext/rrpc/+/sys/+/+/thing/model/down_raw
/ext/rrpc/%s/sys/%s/%s/thing/service/%s             /ext/rrpc/+/sys/+/+/thing/service/+
```
(`%s`/`%s` substitution = `productKey`/`deviceName`.) Plus a simpler, likely-legacy Petkit-specific shadow
scheme also present in `ctrl`: `/%s/%s/user/get`, `/%s/%s/user/update`, `/%s/%s/user/update/event`,
`/%s/%s/user/update/property`.

### MQTT client-id / auth (Alibaba's standard one-device-one-secret HMAC scheme)
```
clientId_field_template: %s.%s|timestamp=%s,_ss=1,_v=%s,securemode=%s,signmethod=hmacsha256,ext=3,%s|   (ctrl — literal clientId field template)
concat_before_hmac: clientId%s.%sdeviceName%sproductKey%stimestamp%s   (ctrl — the string concatenated before HMAC)
```
i.e. `sign = HMAC-SHA256(key=deviceSecret, data="clientId{id}deviceName{dn}productKey{pk}timestamp{ts}")`,
with the MQTT `ClientId` field literally `{clientId}|securemode=2,signmethod=hmacsha256,timestamp={ts}|`.
This is stock Alibaba IoT auth, not a Petkit invention.

### HTTP endpoints (`https://api-sandbox2.petkit.cn/6/` + relative path, `ctrl`+`cloud`)
```
/dev_attire_over        /dev_multi_config              /dev_serverinfo
/dev_ble_device          /dev_only_iot_device_info      /dev_signup
/dev_device_info         /dev_only_iot_device_info_v2   /dev_sound_get
/dev_discern_config      /dev_ota_check                 /dev_state_report
/dev_discern_pic         /dev_ota_complete               /dev_syncTime
/dev_event_report        /dev_ota_heartbeat              /dev_video_device_info
/dev_feed_get            /dev_ota_start
/dev_oss_sts_info_new_v2 (cloud — Alibaba OSS STS credential fetch, for direct-to-OSS upload)
/dev_upload_file_info_v2 (cloud)
```
HTTP auth header: `ctrl: "X-Device:id=%d&nonce=%s&timestamp=%u&type=%s&sign=%s"`.
Factory/WiFi-provisioning query template: `ctrl: "%s?mac=%s&sn=%s&chipId=%s&id=%d&bt_mac=%s&hardware=%d&firmware=%s"`.
Factory-test-only config keys: `PETKIT_PT_WIFI`, `PETKIT_PT_WIFI_1..4`.

### Application-level event protocol (`event_type`/`event_id`, POSTed to `/dev_event_report`)
`ctrl` builds several JSON `content=` shapes keyed by a numeric `event_type`, all sharing
`event_type=%d&event_id=%s&timestamp=%d&content=<json>&state=%s`:
- Generic error: `{"err":"%s"}` / `{"start_time":%d,"err":"%s"}`
- Feed event: `{"id":"%s","day":%d,"manual":%d,"time":%d,"online_state":%d,"eat_video":%d}`
- Motion/pet-detected (progressively richer variants): `{"start_time":%d,"start_reason":%d,"action":%d,"device":{"mac":"%s","type":%d}}`,
  with `"result":%d` and `"err":%d` added for completion/error sub-variants
- Pet-tracking/vomit: `{"related_event":%s,"count":%d,"area":%d,"pet_id":%s,"tracker_info":%s,"vomit_info":%s}`
- **Feed-completion report with photo** (feeds `/dev_upload_file_info_v2` + `/dev_event_report` together):
  `{"img":"%s","aesKey":"%s","mark":%d,"start_time":%d,"id":"%s","day":%d,"manual":%d,"time":%d,`
  `"real_amount":%d,"online_state":%d,"completed_at":%d,"result":%d,"err_code":%d,`
  `"surplus_standard":%d,"media":%d,"eat_video":%d}` — the dual-hopper variant replaces `real_amount`
  with `real_amount1`/`real_amount2`.
- `d4sh_data_get` is the only literal `d4sh_`-prefixed message-type string found (most typing is via the
  numeric `event_type`, not string enums as the task brief's naming convention assumed).

### Feed data-path function names (`ctrl`)
`dispatch_handler_feed` → `pk_ctrl_send_feed_event_msg` / `pk_event_pack_feed_start_event_msg` /
`pk_event_pack_feed_over_event_msg`, backed by a local time-indexed store (`add_feed_info_by_time`,
`get_feed_info_by_time`, `delete_feed_info_by_time`, `feedHistory`), sequence-tracked via
`g_iot_feed_seq_t` / `pk_add_feedSeq_feedEvent` / `iot_recv_del_local_feedSeq` (cloud ack → local
cleanup, i.e. an at-least-once retry queue), and cloud property pushes parsed by
`parse_recv_property_set_feed_param` (the `.../thing/service/property/set` topic in practice). Sound
selection on feed completion: `_pk_get_user_feed_over_aac_id` / `read_check_system_user_feed_aac_file`.

**Naming trap:** `ctrl_feed_dog` is **not** a feeding function — cross-checked against `watchdog: "/dev/watchdog"`
and the `/heartbeat` channel + `state.watchdog.ctrl_count/ctrl_pid` fields, it is `ctrl`'s own liveness
kick to the supervisor (the common embedded-C idiom "feed the [watch]dog").

### BLE-device-relay path (cloud → ctrl → ble)
`/dev_ble_device` + `net_dev_ble_device_list_get` (ctrl fetches the list of other Petkit BLE peripherals —
e.g. fountains — to relay) → `dispatch_handler_get_relay_dev_list` / `pk_schmg_parse_ble_dev_list` (ctrl-side
parse) → bus message → `ble: "dispatch_handler_ble_dev_list_ctrl"` / `"dispatch_handler_WAN_ctrl_ble_relay"`
→ `ble`'s BLE-GATT-client subsystem (§8) scans for and relays those devices' telemetry.

---

## 7. UART / T31 Dispenser MCU Protocol

- Link: `/dev/ttyS3`, opened by `ble` (`ble: "UART %s,fd:%d Init success! baudrate:%d"` — baud rate is a
  runtime/config value, not a literal string constant; **no 300–921600 literal was found**, so the exact
  baud is not recoverable from strings alone — see §12).
- **Framing** (best evidence available without disassembly): frames have a **header with its own CRC**
  and a **separate data/payload CRC** — `ble: "Check CRC32 error ! head_crc = %x,data_crc=%x"` — plus a
  sequence number (`ble: "Fill seq [%d], checksum:0x%x"`) and an overall checksum validated on receipt
  (`ble: "IsCommDataCheckSumErr"`, `"recieve over, wridx:%d, checksum: 0x%x"`). Traffic is logged with
  directional tags suggesting multiple logical endpoints multiplexed on the one UART: `app -> t31`,
  `dev -> t31`, `pt app -> t31` (production-test), and the reverse `t31 -> app`, `t31 -> dev`, `t31 -> ble`
  — i.e. the frame carries a destination/source sub-address distinguishing "normal app", "dev"
  (inference: a debug/dev-mode channel), and "pt" (factory test) traffic.
- **T31 → host message identifiers** (from `"T31 recv: X"` debug prints — this is effectively the command
  ID enum, just not their numeric values): `DEV type`, `NEW DEV type`, `FEED_INFO_RECOED` [sic],
  `FEED_LOG Cmd`, `FEED_SCH ACK`, `Food Surplus Ctrl`, `KEY_EVENT Cmd` (physical button on the feeder),
  `MCU_BASE_CFG req`, `MOT_RUNSTA`, `Motor Run Config Cmd`, `Power manage`, `RTC data`, `Relay connect`
  (inference: BLE-relay-related, not UART relay), `ble trans data`, `get ver mac cmd`, `id secrect set ok`
  [sic], `pt trans data`, `reset mcu cmd`, `uart ota`.
- **Feed command fields** (`ble`): `feed_ctrl event=%d`, `feed_amount_l=%d`, `feed_amount_r=%d` (independent
  left/right hopper dosing, matching the dual-hopper hardware), `item_id_str=%s` (a schedule/item
  correlation id). Feed logging records: `feed_start_log event_id:%s,start_time:%d,day:%d,manual_sta:%d,`
  `time:%d,online_sta:%d` and `feed_over_log event_id:%s,start_time:%d,over_time:%d,day:%d,manual_sta:%d,`
  `time:%d,online_sta:%d,amount_l:%d,amount_r:%d,result:%d,over_err_num:%d`.
- **OTA-over-UART flow**: `ble` pulls an OTA package (`ble: "uart ota start get file url:%s,file md5:%s,`
  `file size:%d"`), then streams it in indexed packets (`"uart ota running pack data, now index:%d!"`,
  retry-capable: `"retry uart ota running pack data, now index:%d!"`), with explicit
  start/running/end/abnormal states (`"uart ota start success!"`, `"uart ota end result:%d,err_num:%d"`,
  `"uart ota running abnormal,err_num:%d!"`) and a final version-check gate
  (`"ota_success_wait_ver success!"` / `"...fail!"`) before considering the T31 update complete.
- `state.ble.sta_data.*` (see §4) is `ble`'s in-memory mirror of T31 telemetry, written into
  `/config_shm` for every other process to read.

---

## 8. BLE

`ble` implements **three distinct roles** in one binary:

1. **Peripheral / advertiser** for phone-app pairing: `ble: "g_config ble_adv:%d, btn_open_ble_sta:%d"` —
   BLE advertising is toggled by a physical button (`state.dev_pro.ble_open_by_key`) or by the pairing
   flow (`state.dev_pro.ble_open_by_bind`). **No literal advertised device name or GATT service/characteristic
   UUID string was recovered** — these are very likely stored as raw 16-byte UUID/binary constants rather
   than printable text, so a live (read-only) BLE scan is the only way to get them (§12). One SKU-family
   string, `ble: "D4SH3"`, appears alongside — (inference) this binary/codebase is shared across the
   D4SH2/D4SH3 hardware variants.
2. **GATT client / "BLE relay manager"** (`bgattc`/`brmg` prefixes, source path
   `ble: "relay/pk_BLE_gattc.c"`): scans for and connects out to *other* Petkit BLE peripherals (e.g.
   water fountains) using an ESP-IDF-flavoured event vocabulary (`ESP_GATTC_NOTIFY_EVT`,
   `ESP_GATTC_DISCONNECT_EVT`, `ESP_GATTC_OPEN_EVT`, `esp_gattc_cb`) — (inference) this event naming was
   very likely copied/ported from Espressif ESP-IDF sample code even though the actual radio here is the
   Realtek RTL8733BU (see `fs/soc/lib/8733bu.ko`), not an ESP32. Functions: `pk_bgattc_init/deinit`,
   `pk_bgattc_connect_dev`/`disconnect_dev`, `pk_bgattc_send`, `pk_brmg_gattc_connect`/`reconnect`,
   `pk_bgattc_mg_proc`. Fed by the cloud-supplied relay-device list (§6).
3. **UART bridge** to the T31 dispenser MCU (§7).

Radio hardware: `fs/soc/lib/8733bu.ko` (Realtek RTL8733BU WiFi+BT combo chip driver) — confirms BLE and
WiFi share one combo radio on the main Axera SoC side; the T31 MCU itself is reached only via UART, not
BLE (the "ble" binary name for the UART bridge is a naming artifact of history/shared code, not a second
radio on the dispenser board).

---

## 9. pktool Reference

Complete subcommand list recovered (22 commands):

| Subcommand | Class | Notes |
|---|---|---|
| `get_config_info` | **safe (read-only)** | Dumps the live `config_t` (the full schema in §4) |
| `get_file_size_by_fp` | safe | |
| `get_gpio_value` | safe | reads `/sys/class/gpio/gpio%d/value` |
| `get_mtd_info_and_badblocks` | safe | MTD/NAND health query (boot-chain adjacent) |
| `get_pwm_status` | safe | reads `/sys/class/pwm/pwmchip0/pwm%d/*` |
| `get_rtc` | safe | |
| `PT_SERIAL_STR` | safe (read) | reads/validates serial-number string |
| `PT_feed_ctrl` | **actuating** | production-test feed trigger (drives the same T31 path as a real feed command) |
| `Aging_feed_ctrl` | **actuating** | burn-in/longevity test — repeatedly cycles the feed motor (`aging_info.aging_*` state) |
| `set_fps` | actuating | camera frame-rate |
| `set_gpio_active_low` | actuating | |
| `set_gpio_direction` | actuating | |
| `set_gpio_value` | **actuating** | drives GPIO-controlled lighting: evidence ties GPIOs to "camera green [status] LED", "IR-cut relay", "IR illuminator", "white light" — e.g. `pktool: "=== disable camera green gpio, enable white light ==="`, `"=== disable ircut, enable gpio camera green light ==="`, `"=== disable irlight gpio, enable ircut ==="`, `"=== disable white light gpio, enable irlight ==="` (exact GPIO numbers not in strings — sysfs paths are parameterized by `%d` at runtime) |
| `set_ispHz` | actuating | ISP clock/flicker frequency (ties to `usr.hertz`) |
| `set_mic_vol` | actuating | **range 0–10** (`pktool: "err vol(%d), need 0-10"`, immediately after `set_mic_vol`) |
| `set_offset` | actuating | |
| `set_pwm_duty_cycle` | **actuating** | single PWM chip only (`/sys/class/pwm/pwmchip0`) — (inference) likely drives the feed-motor speed or an IR-LED dimmer, exact channel→function mapping not recoverable from strings |
| `set_pwm_enable_sta` | actuating | |
| `set_rtc` | actuating | |
| `set_spk_vol` | actuating | **range 0–100** (`pktool: "err vol(%d), need 0-100"`, immediately after `set_spk_vol`) |
| `set_thresh` | actuating (config) | algo-threshold tuning — usage: `Usage(): pktool %s bodyThresh faceThresh keypointThresh featThresh bodyMoveRatio bodyPlateThresh` / example `pktool %s 0.4 0.5 1.0 0.5 0.2 0.2` (maps 1:1 to the detector heads in §10) |
| `set_wifi` | **actuating** | provisions WiFi credentials directly (bypasses the phone-app BLE/QR flow — useful for a first-party agent) |

Additional evidence: config encryption self-test path shares the same AES-256 requirement
(`pktool: "Key must be 32 bytes for AES-256"`, `"Invalid cipherData length"`, `"Failed to generate IV"`).
`pktool` also opens a listening socket and calls `system()` (§1) — treat as a broader remote-control
surface than just the CLI subcommands enumerate; not fully characterized here.

---

## 10. Media / Alg / Audio

**Media pipeline** (`media`, via Axera `AX_*` SDK calls): `AX_VIN` (camera sensor in) → `AX_ISP` +
`AX_ISP_ALG_Ae/Awb` (3A: auto-exposure/white-balance, sensor-registered via `AX_ISP_ALG_AeRegisterSensor`)
→ `AX_MIPI_RX` → `AX_IVPS` (crop/scale/OSD region compositing, `AX_IVPS_RGN_*` for the attire/logo/time
overlays) → `AX_VENC` (H.264/H.265 video encode + `AX_VENC_JpegEncodeOneFrame` for stills, `AX_VENC_RequestIDR`
to force a keyframe before starting a live stream) → `AX_NT_Ctrl`/`AX_NT_Stream` ("network transport" —
hand-off to whichever process streams it out, most plausibly `agora`). Snapshot/preview files: `/tmp/snap_main.jpeg`,
`/tmp/snap_sub.jpeg`, `/tmp/fPre_eat.jpeg`, `/tmp/fPre_pet.jpeg`, `/tmp/fPre_compStart.jpeg`,
`/tmp/fPre_compOver.jpeg`, `/tmp/compTmp.jpeg`. OSD/attire assets: `etc/osd_file/*.argb8888` (logo/time
bitmaps), `etc/defAttire.tar.gz` / `/opt/osdAttire.tar.gz` (costume overlays, extracted to `/tmp/attire/`).
Frame hand-off to other consumers uses named POSIX shm `media_buffer.%s` (§2), not a unix socket or RTSP
server — **no `rtsp://` string was found anywhere**, so there is no local RTSP endpoint; live video only
leaves the device via the Agora RTC path.

**Algorithm engine** (`fs/alg/libalgo.so`, `dlopen()`-loaded by `media` — `media: "algo_process_init dlopen %s Error: %s"`,
`"libalgo_handle"`, `"/alg/libalgo.so"`): built on OpenCV + the Axera NPU runtime (`libax_engine.so`/
`libax_interpreter.so`) executing the models named in `fs/alg/alg_model.txt` (an authoritative manifest):

| Manifest role | Model file | Function (from C++ symbol names) |
|---|---|---|
| `body_model` | `petkit_pet_1class_2026_04_15.axmodel` | Single-class "is there a pet" detector (`CPetkitAlgoBehaviorRec`-adjacent) |
| `face_model` | `petkit_cat_dog_mv2_035_kps_porb_roi_v8_224x224.axmodel` | Cat/dog classification + keypoints + face-ROI localization |
| `feat_model` | `petkit_face_rec_mtl_s2_v5_sim.axmodel` | Per-pet **re-identification** feature extractor — `CPetkitAlgoPetfeat::petkit_petfeat_process/petkit_petfeat_cos_distance` (cosine-similarity matching against enrolled pet profiles), `petkit_from_url_detect_feature` (can enroll a pet directly from a photo URL) |
| `skeleton_model` | `petkit_mobilepose_v1.3.axmodel` | Pose/keypoint estimation |
| `behavior_model` | `petkit_pets_behavior_rec_0601_v1.3.axmodel` | `CPetkitAlgoBehaviorRec` — eating/drinking/other behavior classification |
| `classify_model_bb` + `classify_model_hd` | `tsn_v17_bb.axmodel` + `tsn_v17_head.axmodel` | `CPetkitAlgoBehaviorClassify::run_backbone`/`run_head` — a two-stage (backbone+head) temporal-segment-network behavior classifier, tracked per-pet via `CPetkitSortTrack` (SORT multi-object tracking) |
| `food_model` | `petkit_pp_fooddet_416_128_segreg_0509_u16.axmodel` | `CPetkitAlgoFoodDetect` — food/bowl-level detection via segmentation+regression |

Results surface through a signal+poll pair, not a queue/socket: `petkit_set_event_start_signal(...)` /
`petkit_get_event_result_info(...)`, returning a `petkit_event_result_info` struct with `event_pet_id`
and `pet_score` — (inference) `media` polls or is signalled in-process after each `dlopen`'d call, then
packages the result into the `event_type`/`content` JSON shapes ctrl reports to the cloud (§6). Debug
artifacts: `/tmp/saveFace.jpg`, `./skeleton_result.jpg`, `/tmp/__opencv_temp.XXXXXX`.

**Audio**: asset table is `fs/audio/{cn,en}/*.aac` (~55 event sounds per locale + digit files `0.aac`–`9.aac`
for spoken numeric announcements, e.g. feed-amount readouts). The full filename table is embedded in
**`ble`**, not `media` (`ble.strings.txt` contains every `.aac` name, e.g. `feed_start.aac`,
`ota_fail.aac`, `wifi_ok.aac`) — `ble` is where the event → sound-file decision is made (consistent with
it owning system/wifi/pairing/feed state), but actual playback happens in `media`, which is the only
binary linking the codec/output stack (`libtinyalsa.so.2`, `libax_fdk.so`+`libfdk-aac.so.2`,
`libax_audio.so`) and exposes `media: "dispatch_handler_play_aac_file"` on the internal bus — i.e. `ble`
decides *which* sound, sends a bus message naming the file, `media` decodes it via `AX_ADEC_FdkInit`/
`AX_ADEC_SendStream`/`AX_ADEC_GetFrame` and plays it via `AX_AO_SendFrame`. Two-way talk ("pet call")
uses the same `media` audio-in path (`AX_AI_*`, `dispatch_handler_speak_start/stop`,
`dispatch_handler_speaker_enable`) with WebRTC-style AEC/AGC/NS tuning in `etc/webrtc_profile.ini`
(AEC `kModerateSuppression`, AGC `kFixedDigital` target -6dBFS/20dB gain, NS `kHigh`, VAD disabled).

---

## 11. Agora

`agora` is a thin wrapper (125,792 bytes) around `libagora-rtc-sdk.so` (957,612 bytes, self-contained: only
needs libc/libm/libpthread/librt). The app-level code contributes essentially one piece of business logic —
token lifecycle: `agora: "ret=%d, agora_rtc_renew_token update [conn-%d] rtc_token info success"` — i.e. a
short-lived RTC token (issued by Petkit's backend, not visible in these binaries) is periodically renewed.
No literal App ID / channel-name template was found in `agora` itself, meaning both are supplied at
runtime (inference: passed via the internal bus from `ctrl`, which is the only process with an HTTP
client and cloud session — likely fetched via an as-yet-unidentified `dev_*` endpoint, or derived from
`usr.iot_keys`/a dedicated agora-key field not seen as a literal). The SDK embeds Agora's entire global
edge/access-point and telemetry-reporting server list (`ap-*.agora.io`, `report-*.agora.io`, covering
Asia/Europe/Americas/Africa/Oceania/Russia/Korea/Japan/India/HK, plus IPv6 variants) — this is stock
Agora SDK content, not Petkit-specific configuration, and matches the previously live-observed connection
to `128.14.195.210:9136` (Zenlayer), a typical Agora edge-relay provider. **For a live-stream replacement**,
an agent would need to supply: an Agora App ID, a channel name (almost certainly derived from the device
ID/serial), and a token minted server-side for that (appId, channel, uid) tuple — none of which can be
fabricated locally since Agora tokens are HMAC-signed server-side.

---

## 12. Open Questions & Recommended Live-Observation Checks

All items below are **read-only** and non-actuating; none require the exclusions in this task
(cloud-impersonation, DNS hijack, forged CA, fake MQTT broker) — they only need local shell access to the
already-running device (a separate future authorization) or a passive BLE scan.

1. **Numeric `msg_id` enum values** for the internal dispatch bus (§3) — needed to construct valid bus
   messages. Recommended: `cat /proc/<ctrl_pid>/maps` + read the read-only data segment for the
   dispatch-table literal array, or (once live analysis is authorized) an `LD_PRELOAD` shim on
   `dispatch_send_msg`/`mq_send` logging `{msg_id,src,dst}` while using the phone app normally — no
   cloud/network interception needed, purely a local library shim.
2. **The exact POSIX mqueue *names*** each process opens (we only recovered `/config_shm`, `/heartbeat`,
   `/audio`, `/proc`, `/mqtt` as literal `/`-prefixed tokens; the per-process command inboxes are almost
   certainly also short literal names not yet isolated from the general string noise). Recommended:
   `ls /dev/mqueue/` on the live device (read-only, no device state change).
2b. Confirm `config_t` **struct layout/size** by reading `/dev/shm/config_shm` (or equivalent
   `/dev/mqueue`-adjacent shm mount) and diffing against the `g_config->` key list in §4 — would let a
   first-party agent read live device state without going through any process at all.
3. **T31 UART baud rate and exact frame byte layout** (sync bytes, length field, CRC16 vs CRC32
   placement/polynomial) — not recoverable from strings; requires either disassembling `ble`'s
   UART-frame-build function or (read-only) tapping ttyS3 with a logic analyzer / `cat /dev/ttyS3` while
   the app performs a feed.
4. **BLE advertised name and GATT service/characteristic UUIDs** — no literal strings found (§8); a
   passive BLE scan (`hcitool lescan` / phone-side BLE scanner) from a nearby machine, without connecting,
   would answer this without touching the device.
5. **Agora App ID / channel-name derivation and token endpoint** — not visible in these binaries;
   likely requires a one-time authorized capture of `ctrl`'s outbound HTTPS request that precedes an
   `agora_video_start` bus message (`ctrl: "dispatch_handler_agora_video_start"` exists but its HTTP
   trigger endpoint wasn't isolated among the many `dev_*` paths — plausibly `/dev_video_device_info`).
6. **GPIO/PWM channel numbers** for the four lighting functions (camera-status LED, IR-cut relay, IR
   illuminator, white light) and the PWM-driven function (motor speed vs. LED dimming) — the *purpose*
   of each is known (§9) but not the sysfs `gpio%d`/`pwm%d` index; `cat /sys/class/gpio/gpio*/label` (if
   the kernel provides labels) or correlating `pktool set_gpio_value <n> <0|1>` against observed physical
   behavior would resolve this quickly and safely.
7. **`p2p`/`card` watchdog fields** (§4) — confirm these are genuinely dead code paths on this SKU (never
   `mq_open`'d, no `/proc/<p2p_pid>` ever populated) rather than an optional feature toggled by an
   unseen config flag.
8. **AES key material itself** — not obtainable by static string search (binary, not printable); would
   need either a disassembly of `petkitRootfs_Aes_Encrypt_Keys_32`'s containing `.rodata` region or a
   live memory read of the running `watchdog` process (read-only, no network contact required).
9. **`libalgo.so` → `media` result delivery** — confirm whether `petkit_get_event_result_info` is a
   polled call or an actual OS-level callback/signal (SIGUSR?) by checking `media`'s registered signal
   handlers; affects how tightly a replacement agent could hook into detection events.

Pointers to BootChainStudy's territory (not duplicated here): `/opt/version` JSON schema, `update_*.sh`
script behavior, U-Boot image header format — see `STUDY-boot.md`/`INVENTORY.md`.
