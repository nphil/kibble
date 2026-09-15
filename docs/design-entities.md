# DESIGN-entities.md — Kibble Home Assistant Entity Model

Status: design document only. No integration code is written here (per assignment scope). Device
slug assumed throughout: `cat_feeder` (HA domain `kibble`, entity_ids `<platform>.cat_feeder_<suffix>`).
Device: Petkit YumShare Dual-hopper 2 ("Rashy", 192.168.4.85), hopper divider physically removed —
see §3.1 for the design consequence.

## Sources

Read in full, in the order specified by the assignment: `STUDY.md`, `LOCALKIT-HARVEST.md`,
`STUDY-config.md` + `study/config_layout.json`, `STUDY-feedtest.md`, `STUDY-mcu.md`, `STUDY-ble.md`,
`STUDY-alg.md`. Cross-referenced against `STUDY-app.md` §3/§4/§6/§7/§8/§9/§10 (control seam, config
schema, cloud protocol, UART protocol, BLE, pktool, media/alg/audio) and `STUDY-msgids.md`/
`STUDY-dispatch.md` (bus msg_id registries) where the primary seven left a gap. Every claim below
cites its evidence inline; confidence is stated per row using the same HIGH/MEDIUM/LOW scale the
source studies use (HIGH = disassembly- or live-test-proven; MEDIUM = one strong piece of structural
evidence, not independently cross-checked; LOW = plausible region/name match only).

## Conventions applied throughout

**House rule 4 (zero settings/diagnostic entities exposed by default).** Every entity whose
`EntityCategory` is `CONFIG` or `DIAGNOSTIC` below ships **registry-disabled** at integration setup
(`entity_registry_enabled_default = False`). Only three classes of entity are enabled by default:
(a) **control** entities with no `entity_category` (feed button/number, schedule services, camera
enable — see the one deliberate exception noted at §3.1), (b) **primary status** sensors a user
checks routinely (feeding-in-progress, error/problem, last-seen pet), and (c) the `update` entity.
This mirrors the same policy the user's ESPHome/Zigbee/Z-Wave devices already carry
(`.agents/AGENTS.md` rule 4) — Kibble is a new integration, not one of those three platforms, but the
assignment directs the same policy be applied here, and it is applied identically: nothing in the
CONFIG/DIAGNOSTIC columns below is visible until the user opts in via the entity registry.

**House rule 9 (display names never repeat the area).** HA's `has_entity_name` convention means an
entity's own `name` is shown appended to the device name ("Cat Feeder <name>"), which itself is shown
grouped under the assigned area — so neither the entity `name` nor the device `name` should restate
the area, and per the same convention entity names should not restate the device name either. Every
capability in this document is named for **just the capability** (e.g. `name: "Hopper 1 Calibration
Factor"`, never `"Cat Feeder Hopper 1 Calibration Factor"` or `"Kitchen Hopper 1 Calibration
Factor"`); the "Capability" column below is written in exactly this display-name-ready form. The
device's own display name is simply "Cat Feeder" (or whatever Nitin names it at setup) — never
prefixed with the area, consistent with `name_curator`'s existing behavior on every other device.

**Confidence scale** (matches the source studies): **HIGH** = disassembly-proven or directly observed
live (e.g. the 0x6004 feed path, the feed_sta transient at config_shm offset 10238). **MEDIUM** = one
strong, named piece of evidence (a confirmed handler name, a confirmed exported function signature)
not independently cross-checked end-to-end. **LOW** = a plausible region or name match only, needing
a follow-up live diff/disassembly pass to pin down exactly. **NONE** = capability wanted by the
assignment but no storage location was found by this study at all (exactly one case — desiccant,
§7.1).

## 1. Master table — every Localkit `Configuration.php` key

One row per key in `LOCALKIT-HARVEST.md` §3's "Device Settings (property/set payload)" table (the
47-key authoritative app-settings list per the assignment). **All 47 keys appear below, exactly
once.** Six are deliberately not exposed (cloud-only artifacts with no local meaning once de-clouded);
two are folded into an existing surface rather than getting a standalone entity; the remaining 39 get
a concrete HA entity. Read mechanisms are given for all 47 — none is unknown. Write mechanisms are
UNKNOWN for 30 of the 47; every one of those 30 is carried into the prioritised queue in §9.

Two structural facts drive almost every "read" cell below and are stated once here instead of once
per row: (1) `usr.app_conf.*` — the struct section holding the overwhelming majority of these 47
keys — is only known at **cluster granularity**: `config_layout.json` resolves it to one 812-byte
region (offset 2856–3668) with individual field identity **not** separated out (`STUDY-config.md` §7 describes the region;
`STUDY-config.md` §8, Open Question 3 is where this gap is numbered). Table A (STUDY-config.md Appendix, the 228-entry pktool debug-string dictionary)
gives the exact **field name, type and print-order position** for every one of these keys with HIGH
confidence — this is what "read mechanism" cites below — but not yet the exact **byte offset**, which
is why almost every row below is capped at LOW confidence on the offset itself even though the field's
existence and type are solid. (2) Every WRITE mechanism marked UNKNOWN below is UNKNOWN for the same
underlying reason: `ctrl`'s cloud-side settings parser (`parse_recv_property_set_feed_param`-adjacent
code, `STUDY-app.md` §6) reads the incoming `property/set` JSON and is presumed — architecturally, not
disassembly-proven — to write straight into the matching `usr.app_conf.*` field kibbled will own after
the cutover (`STUDY.md`'s control-seam decision, `STUDY-app.md` §3). What is **not** established is
whether any given setting **also** needs an explicit bus push to `ble`/`media` to take effect
immediately, or whether those processes simply re-read `config_shm` on their own cycle. Rows where a
named bus handler proves an explicit push exists (LED, schedule, WiFi, RTC, MCU reset) say so; every
other row assumes poll-based propagation is sufficient but flags this as unconfirmed.

### 1.1 Deliberately not exposed (6 keys)

| Localkit key | Reason |
|---|---|
| `upload` | Cloud recording toggle; Localkit's own doc calls it "kept for cloud config" (i.e. vestigial even there). Kibble has no cloud video-upload feature — local-only architecture per `STUDY.md`'s control-seam decision (§3: replace `ctrl`+`cloud` wholesale, no reimplementation of `/dev_upload_file_info_v2`/OSS upload). |
| `shareOpen` | Petkit-cloud-account family-sharing feature. Kibble has no Petkit account/cloud identity layer for a second user to be invited into. |
| `multiConfig` | Legacy app feature-gate that unlocks more than one schedule entry in the OEM UI. Kibble's own schedule service (§3) natively supports an arbitrary number of entries with no gate — this flag has no purpose here. |
| `autoUpgrade` | Localkit's own doc states this is "ignored for D4H2" — no OTA support on this hardware (`STUDY.md`, `STUDY-boot.md`). Kibble's own `update.cat_feeder_firmware` entity (§7) is the meaningful firmware-update surface. |
| `serviceStatus` | Cloud subscription/binding status (Localkit's `toDeviceInfo` example shows a static `serviceStatus: 2`). Meaningless once de-clouded from Petkit's account system; Kibble has no equivalent subscription concept. |
| `capacity` | Petkit cloud video-storage subscription quota (fullVideo/eventImage/highLight/dynamicVideo). Kibble has no CVR/cloud-upload feature (see `upload` above), so there is no capacity to report. |

### 1.2 Folded into an existing surface rather than a standalone entity (2 keys)

| Localkit key | Where it goes |
|---|---|
| `typeCode` | HA Device Registry `model` field at integration setup (redundant with `dev.name`="D4SH", config_shm offset 4832, HIGH confidence — `STUDY-config.md` Table A #2/`config_layout.json`). |
| `CTime` | Surfaced as a `last_modified` attribute on the schedule list (§3), not a separate entity — it is metadata *about* the schedule (device-updated on every write per Localkit's own doc), not its own capability. |

### 1.3 The 47-key master table

| Localkit key | Capability | HA entity | Read mechanism | Write mechanism | BLE (Wi-Fi down) survival | EntityCategory |
|---|---|---|---|---|---|---|
| `amount1` | Hopper 1 manual-feed default amount (g) | number.cat_feeder_hopper_1_amount | N/A — not a persisted device setting (absent from both independently-extracted config_shm schemas: STUDY-config.md Table A 228 fields, STUDY-app.md §4 grep). HA holds this as local helper state. | Not a device write at all — value is passed as the `amount1` byte (offset +65) of the 67-byte feed_ctrl payload on every `kibble.feed` call (msg_id 0x6004→ble, STUDY-feedtest.md). HIGH confidence. | N/A (HA-local state) | CONFIG |
| `amount2` | Hopper 2 manual-feed default amount (g) | number.cat_feeder_hopper_2_amount | N/A — same reasoning as amount1. | Not a device write — `amount2` byte (offset +66) of the feed_ctrl payload, same call. HIGH confidence. | N/A (HA-local state) | CONFIG |
| `factor1` | Hopper 1 calibration factor | number.cat_feeder_hopper_1_calibration_factor | config_shm `usr.app_conf.factor1` (Table A #120) — region offset 2856-3668 packed cluster, exact sub-offset NOT individually resolved. LOW confidence on exact bytes, MEDIUM on field existing in that cluster. | UNKNOWN — no dispatch handler name or cloud parse function was found dedicated to calibration; presumed same path as other app_conf scalars (direct config_shm write once kibbled owns it), but needs the live diff pass to confirm offset before it can be written safely. | Unknown — depends on whether MCU or Linux applies the factor | CONFIG |
| `factor2` | Hopper 2 calibration factor | number.cat_feeder_hopper_2_calibration_factor | Same cluster/region as factor1 (Table A #121). LOW confidence exact offset. | UNKNOWN, same as factor1. | Unknown | CONFIG |
| `foodWarn` | Low-food warning enable | switch.cat_feeder_low_food_warning | config_shm `usr.app_conf.foodWarn` (Table A #122), same low-confidence cluster (2856-3668). | UNKNOWN — direct config_shm write hypothesized (no dedicated bus handler found); unconfirmed whether any consumer needs a push vs. reads config_shm on its own poll. | Unknown | CONFIG |
| `foodWarnRange` | Low-food warning active hours | text.cat_feeder_low_food_warning_hours (JSON `{from,till}` in minutes-of-day) | config_shm `usr.app_conf.foodWarnRange`, stored as a `%.*s` (string) field per Table A #123 — same cluster, exact bytes/serialization format LOW confidence. | UNKNOWN — serialization format of the range string not reverse engineered (JSON vs custom delimiter). | Unknown | CONFIG |
| `manualLock` | Child lock (physical buttons) | switch.cat_feeder_child_lock | config_shm `usr.app_conf.manualLock` (Table A #128), same cluster. LOW confidence exact offset. | UNKNOWN — direct config_shm write hypothesized; no dedicated bus handler evidenced. | Unknown | CONFIG |
| `lightMode` | Status LED enable | switch.cat_feeder_status_led | config_shm `usr.app_conf.lightMode`/`ledlight_enable` (Table A #124), cluster region. LOW confidence exact offset. | `ctrl` msg 0x0010 = `dispatch_handler_ledlight_mode_set`, and msg 0x101d = `dispatch_handler_sync_led_mod` (STUDY-msgids.md ADDENDUM, STUDY-app.md are ctrl-side registrations — HIGH confidence these exist; the numeric ctrl→ble outbound msg_id for the LED command itself, and its payload shape, are NOT recovered (same class of gap as schedule-set). MEDIUM confidence overall: handler existence proven, exact wire bytes not. | Likely yes — LED is GPIO-driven by ble/T31, not cloud-dependent | CONFIG |
| `lightMultiRange` | Status LED active-hours schedule | text.cat_feeder_status_led_hours (JSON range list) | config_shm `usr.app_conf.lightMultiRange` (Table A #125), `%.*s` string field, cluster region. LOW confidence. | UNKNOWN — same LED path as lightMode plus unresolved range serialization format. | Likely yes | CONFIG |
| `camera` | Camera stream enable | switch.cat_feeder_camera | config_shm `usr.app_conf.camera_enable` (Table A #84) + `state.dev_pro.device_camera_enable`/`tmp_devCamera_enable` (Table A #184-185, low-confidence `state.dev_pro` cluster 4892-7316). MEDIUM confidence (two independent mirrors of the same concept, region known). | UNKNOWN — no dedicated bus handler name found; presumed direct config_shm write, needs confirmation `media` re-checks this live (per-frame or periodic) rather than only at its own startup. | No — camera/ISP is a Linux-side (media) function, T31 has no camera | PRIMARY (control, not CONFIG) |
| `cameraMultiRange` | Camera active-hours schedule | text.cat_feeder_camera_hours (JSON range list) | config_shm `usr.app_conf.cameraMultiRange` (Table A #85), `%.*s` string, cluster region. LOW confidence. | UNKNOWN, same class as lightMultiRange. | No | CONFIG |
| `cameraRangeTable` | Camera per-weekday active-hours | text.cat_feeder_camera_weekday_hours (JSON per-weekday range table) | NOT individually located in Table A by name (no `cameraRangeTable` string found in the 228-entry pktool dictionary) — presumed folded into the same `cameraMultiRange`/app_conf cluster region, unconfirmed. LOW confidence — region only. | UNKNOWN. | No | CONFIG |
| `microphone` | Microphone enable | switch.cat_feeder_microphone | config_shm `usr.app_conf.mic_enable` (Table A #83), cluster region. LOW confidence exact offset. | UNKNOWN — direct config_shm write hypothesized. | No | CONFIG |
| `night` | Night vision (IR) enable | switch.cat_feeder_night_vision | config_shm `usr.app_conf.irlight_enable` (Table A #80), cluster region. LOW confidence exact offset. Related: `dev.hw_param.ircut_inverse` (factory IR-cut polarity, separate concept, Table A #16, also unresolved offset). | UNKNOWN — `pktool set_gpio_value` is confirmed to drive the physical IR-cut relay/illuminator GPIOs (STUDY-app.md §9) but exact GPIO numbers are unresolved; the app-level toggle's own write path (config_shm vs. direct GPIO bus msg) is unconfirmed. | No | CONFIG |
| `timeDisplay` | Video timestamp overlay | switch.cat_feeder_video_timestamp | config_shm `usr.app_conf.timestamp_enable` (Table A #81), cluster region. LOW confidence exact offset. | UNKNOWN — direct config_shm write hypothesized; media's IVPS OSD compositor (STUDY-app.md §10) is the consumer. | No | CONFIG |
| `eatVideo` | Record video clip on eat detection | switch.cat_feeder_eat_video | config_shm `usr.app_conf.eatVideo` (Table A #115), cluster region. LOW confidence. | UNKNOWN. | No | CONFIG |
| `moveDetection` | Motion detection enable | switch.cat_feeder_motion_detection | config_shm `usr.app_conf.move_det.algoEnable` (Table A #93), cluster region. LOW confidence. | UNKNOWN — direct config_shm write hypothesized; `libalgo.so`'s `petkit_algo_init` reads detector-enable flags at its own init, so a live toggle may require re-init, not just a value flip — unconfirmed (STUDY-alg.md §1/§6). | No | CONFIG |
| `moveSensitivity` | Motion detection sensitivity (1-9) | number.cat_feeder_motion_sensitivity | config_shm `usr.app_conf.move_det.sensitivity` (Table A #95), cluster region. LOW confidence. | UNKNOWN target field write; but the ACTUAL consumer call is disassembly-CONFIRMED: `petkit_modify_algo_threshold(bodyThresh,faceThresh,keypointThresh,featThresh,bodyMoveRatio,bodyPlateThresh)` (STUDY-alg.md §2, exact struct offsets) — sensitivity maps to one of these floats via an app-side lookup table not captured in this study. HIGH confidence a live mechanism exists, MEDIUM on which parameter it maps to. | No | CONFIG |
| `petDetection` | Pet-visit (AI) detection enable | switch.cat_feeder_pet_detection | config_shm `usr.app_conf.pet_det.algoEnable` (Table A #100), cluster region. LOW confidence. | UNKNOWN, same class as moveDetection. | No | CONFIG |
| `petSensitivity` | Pet detection sensitivity (1-9) | number.cat_feeder_pet_sensitivity | config_shm `usr.app_conf.pet_det.sensitivity` (Table A #102), cluster region. LOW confidence. | Likely `petkit_modify_algo_threshold`'s `bodyThresh`/`faceThresh` args, same evidence class as moveSensitivity. | No | CONFIG |
| `eatDetection` | Eating detection enable | switch.cat_feeder_eat_detection | config_shm `usr.app_conf.eat_det.algoEnable` (Table A #107), cluster region. LOW confidence. | UNKNOWN, same class as moveDetection. | No | CONFIG |
| `eatSensitivity` | Eating detection sensitivity (1-9) | number.cat_feeder_eat_sensitivity | config_shm `usr.app_conf.eat_det.sensitivity` (Table A #109), cluster region. LOW confidence. | Likely `petkit_modify_algo_threshold`'s `featThresh`, same evidence class as moveSensitivity. | No | CONFIG |
| `detectInterval` | Minimum seconds between detections (global) | number.cat_feeder_detect_interval | config_shm `usr.app_conf.detectInterval` (Table A #91), cluster region. LOW confidence. | Likely `petkit_set_tracker_interval(int)` (STUDY-alg.md §1, confirmed exported function, single int arg) — MEDIUM confidence this is the consumer, not disassembly-traced end to end from config_shm to this call. | No | CONFIG |
| `detectMultiRange` | Detection active-hours schedule (global) | text.cat_feeder_detection_hours (JSON range list) | NOT individually located in Table A by exact name; presumed part of the app_conf cluster region alongside per-type alarmTime fields (#97/#104/#111). LOW confidence. | UNKNOWN, same class as lightMultiRange. | No | CONFIG |
| `toneMode` | Do-not-disturb (mute all sounds) | switch.cat_feeder_do_not_disturb | config_shm `usr.app_conf.toneMode` (Table A #126), cluster region. LOW confidence. | UNKNOWN — `state.dev_pro.toneTimeAllow` (Table A #163) is a separate, live-computed "is DND currently active" flag in the low-confidence `state.dev_pro` cluster (4892-7316); the write path for the *setting* itself is unconfirmed. | No | CONFIG |
| `toneMultiRange` | Do-not-disturb hours | text.cat_feeder_do_not_disturb_hours (JSON range list) | config_shm `usr.app_conf.toneMultiRange` (Table A #127), `%.*s` string, cluster region. LOW confidence. | UNKNOWN, same class as lightMultiRange. | No | CONFIG |
| `soundEnable` | Voice prompt on feed dispense | switch.cat_feeder_feed_voice_prompt | config_shm `usr.app_conf.soundEnable` (Table A #116), cluster region. LOW confidence. | UNKNOWN — consumer is confirmed to be `media: dispatch_handler_play_aac_file` selected by `ble` via `_pk_get_user_feed_over_aac_id` (STUDY-app.md §6/§10); which bus message carries the enable/disable decision itself vs. just the per-event play-this-file command is unconfirmed. | No | CONFIG |
| `systemSoundEnable` | System guidance voice | switch.cat_feeder_system_voice | config_shm `usr.app_conf.systemSoundEnable` (Table A #117), cluster region. LOW confidence. | UNKNOWN, same class as soundEnable. | No | CONFIG |
| `feedSound` | Sound on feed complete | switch.cat_feeder_feed_complete_sound | config_shm `usr.app_conf.feedSound` (Table A #118), cluster region. LOW confidence. | UNKNOWN, same class as soundEnable. | No | CONFIG |
| `volume` | Speaker volume (0-9 app scale) | number.cat_feeder_speaker_volume | config_shm — NOT individually named in Table A under `usr.app_conf`; likely `usr.mtu`-adjacent or a top-level `usr.*` scalar not in the 228-entry list. LOW confidence, region not even pinned. | `pktool set_spk_vol` is a confirmed actuating CLI subcommand, **range 0-100** (STUDY-app.md §9) — a DIFFERENT scale than the app's 0-9, meaning the app value is multiplied ~11x before reaching this layer. HIGH confidence a working write mechanism exists (pktool), MEDIUM on whether the production app-facing path uses the same call or a bus message to `media`/`ble` instead of shelling to pktool. | No | CONFIG |
| `selectedSound` | Selected notification sound ID | select.cat_feeder_notification_sound | config_shm `usr.app_conf.selectedSound` (Table A #119), cluster region. LOW confidence exact offset; valid ID list obtainable read-only from the `.aac` filenames embedded in `ble`'s string table (STUDY-app.md §10) — MEDIUM confidence for the option list itself. | UNKNOWN — write mechanism for the *default* selection unconfirmed; per-event playback is confirmed (`dispatch_handler_play_aac_file`). | No | CONFIG |
| `surplusControl` | Leftover-food detection state (read-only) | sensor.cat_feeder_leftover_food_state | config_shm `usr.app_conf.surplusControl` (Table A #130) + `state.dev_pro.leftover` (Table A #161, low-confidence `state.dev_pro` cluster). LOW confidence exact offsets, MEDIUM on cluster region. | N/A — Localkit's own doc marks this read-only (device-computed, not app-set). | No | DIAGNOSTIC |
| `surplusStandard` | Leftover-food threshold | number.cat_feeder_leftover_threshold | config_shm `usr.app_conf.surplusStandard` (Table A #131), cluster region. LOW confidence. | UNKNOWN — presumed direct config_shm write, consumed by `CPetkitAlgoFoodDetect`'s food_model pipeline (STUDY-alg.md §1) which re-reads it per detection cycle (unconfirmed whether push or poll). | No | CONFIG |
| `smartFrame` | Pet auto-tracking/framing in video | switch.cat_feeder_auto_tracking | config_shm `usr.app_conf.smartFrame` (Table A #132), cluster region. LOW confidence. | UNKNOWN — consumer likely `CPetkitSortTrack` (SORT tracker, STUDY-alg.md §1) framing decision; write path unconfirmed. | No | CONFIG |
| `vomitDetection` | Vomit detection enable | switch.cat_feeder_vomit_detection | config_shm `usr.app_conf.vomit_det.algoEnable` (Table A #133), cluster region. LOW confidence. (No separate sensitivity field exists for this detector — confirmed absent from Table A, matching Localkit's own key list which likewise has no `vomitSensitivity`.) | UNKNOWN, same class as moveDetection. | No | CONFIG |
| `feedPicture` | Capture photo on feed | switch.cat_feeder_feed_photo | config_shm `usr.app_conf.feedPicture` (Table A #114), cluster region. LOW confidence. | UNKNOWN — presumed direct config_shm write; consumer is media's snapshot path (`/tmp/fPre_compStart.jpeg` etc., STUDY-app.md §10). | No | CONFIG |
| `upload` | Cloud recording (kept for cloud config) | NOT EXPOSED | N/A | N/A | N/A | N/A |
| `shareOpen` | Share device access (Petkit account sharing) | NOT EXPOSED | N/A | N/A | N/A | N/A |
| `multiConfig` | Multi-schedule support unlocked | NOT EXPOSED | N/A | N/A | N/A | N/A |
| `autoUpgrade` | Auto OTA update | NOT EXPOSED | N/A | N/A | N/A | N/A |
| `typeCode` | Device model code | NOT A STANDALONE ENTITY | config_shm `dev.name`="D4SH" (Table A #2, HIGH confidence, offset 4832) doubles as the model identifier. | N/A — factory-programmed, not app-writable per Localkit's own doc (no setter documented). | Yes (static) | n/a |
| `hertz` | Mains/camera frequency (50/60Hz) | select.cat_feeder_mains_frequency | config_shm `usr.hertz` (Table A #23), listed directly under `usr.*` not in the low-confidence cluster — still no confirmed byte offset in config_layout.json. LOW confidence exact offset, MEDIUM on field existing at top level of `usr.*`. | `pktool set_ispHz` is a confirmed actuating CLI subcommand (STUDY-app.md §9) — HIGH confidence a working mechanism exists; whether production app-writes go through this same call or a config_shm write + media poll is unconfirmed. | No | CONFIG |
| `CTime` | Schedule last-modified timestamp | NOT A STANDALONE ENTITY | config_shm `usr.app_conf.CTime` (Table A #129), cluster region. LOW confidence exact offset. | Device-computed on every schedule write (per Localkit's own doc: "CTime timestamp is updated by device and reported back"), not independently app-writable. | Unknown | n/a |
| `logo_cn` | OSD logo/display variant | select.cat_feeder_osd_logo (grouped with attireId, see below) | config_shm `usr.app_conf.logo_cn` (Table A #139), cluster region. LOW confidence. | UNKNOWN — valid values not enumerated by this study. | No | CONFIG |
| `serviceStatus` | Cloud service/subscription status | NOT EXPOSED | N/A | N/A | N/A | N/A |
| `capacity` | Cloud storage-plan capacity (fullVideo/eventImage/highLight/dynamicVideo) | NOT EXPOSED | N/A | N/A | N/A | N/A |
| `attireId` | OSD costume/skin overlay selection | select.cat_feeder_osd_costume | config_shm `usr.app_conf.attireId` (Table A #138), cluster region. LOW confidence exact offset. | UNKNOWN — option list is read-only recoverable (`etc/defAttire.tar.gz`/`/opt/osdAttire.tar.gz` filenames enumerate valid costumes, STUDY-app.md §10), but the write mechanism itself (config_shm field vs. bus message to media's IVPS OSD compositor) is unconfirmed. | No | CONFIG |

## 2. Additional app-visible entities outside the 47-key settings table

`LOCALKIT-HARVEST.md` §3 documents a second JSON object beyond `Configuration.php`'s settings — the
live device-state object carried in `property_post`/`feed_stop` events (`food1`, `food2`, `feeding`,
`door`, `bowl`, `other`, and a derived `error`). These are not configuration keys (no acceptance
criterion applies to them) but they are capabilities "the app offers" — status the app surfaces to
the user — so they are included here for completeness. `food1`/`food2` (per-hopper "has food"
booleans) are folded into §3.3's hopper design; `feeding` (dispensing-in-progress) is folded into
§3.3 as a PRIMARY, control-adjacent status of the Feeding surface — both are listed there, not
repeated here. `error` is DIAGNOSTIC per the assignment's own explicit categorization (§7).

| Capability | HA entity | Read mechanism | Write mechanism | BLE survival | EntityCategory |
|---|---|---|---|---|---|
| Feed tray/door open | binary_sensor.cat_feeder_door (device_class: problem) | MQTT `property_post` state object's `door` field per Localkit's own device-state doc (`door=0` → open/error, `door=1` → closed) — MEDIUM confidence (Localkit's cloud-facing schema, not yet independently cross-checked against a config_shm offset in this device's own study). No config_shm offset resolved for a local-bus equivalent; kibbled would need to derive this from whichever `state.dev_pro`/`state.ble` field the local `dev_state_report`/`property_post` builder (`ctrl` msg 0x1010, `dispatch_handler_dev_state_report`) reads before composing that same JSON — same class of gap as the rest of the low-confidence `state.*` clusters. | N/A — read-only, device-computed. | Unknown — depends which struct section feeds it | — (PRIMARY, enabled by default; safety-relevant — Localkit's own error logic treats `door==0` as an error condition) |
| Bowl status code | sensor.cat_feeder_bowl_status | MQTT `property_post` state object's `bowl` field (Localkit doc: "Bowl status code", exact value meanings not documented anywhere in this study). LOW confidence — field exists, semantics undecoded. | N/A — read-only, device-computed. | Unknown | DIAGNOSTIC (undecoded values — surfaced as a raw integer sensor for troubleshooting only) |
| Current error condition | sensor.cat_feeder_error (state: none/food_empty/door_open/other) | MQTT `property_post`'s derived `error` field (Localkit's own logic: `food1==0 or food2==0 → food_empty`; `door==0 → door_closed`; else `null`) plus the local bus's dedicated error event pair `error_start`/`error_over` (`ctrl`'s event-report JSON shapes, `STUDY-app.md` §6, includes `err`/`start_time` fields) — MEDIUM confidence on the local (non-cloud) equivalent field location. | N/A — read-only, device-computed. | Likely yes (MCU-side fault conditions at minimum) | DIAGNOSTIC (disabled) — assignment explicitly places "error state" in the all-DIAGNOSTIC diagnostics bucket (§7); overrides my own instinct to treat it as PRIMARY. |
| Device LAN IP address | NOT A STANDALONE ENTITY | MQTT `property_post`'s `other` field (Localkit doc: contains `Ip:x.x.x.x`) / config_shm `usr.wifi.net_inf.ipaddr` (HIGH confidence, offset 1336, `config_layout.json`). | N/A | Yes | n/a — Folded into the HA integration's own connection info (the config entry already knows the configured host) rather than a standalone entity — redundant. |

## 3. Feeding surface (non-trivial surface 1 of 5)

### 3.1 The hopper-divider decision

**Fact:** this unit's physical hopper divider is removed, so both augers draw from one shared bin.
**Evidence the hardware still has two independent mechanisms despite the shared bin:** the proven feed
payload (`STUDY-feedtest.md`) carries **independent** `amount1`/`amount2` bytes (offsets +65/+66 of the
67-byte `feed_ctrl` struct) that reach two **independent** motors — `state.ble.moto_runt_data.ctrl_ID`
(Table A #214) records *which* motor ran, and the T31's `Motor Run Config Cmd` (UART CMD 0x0A,
`STUDY-mcu.md` §4) is per-auger, not per-bin. Similarly `state.ble.sta_data.food1_lack`/`food2_lack`
(Table A #198-199) and `adc_data.proxl_rw`/`proxr_rw` (Table A #191-192) are **two independent physical
sensor channels**, one per auger throat, not one shared bin-level sensor.

**Recommendation: present ONE logical food source with per-auger targeting as an advanced/secondary
capability, not two parallel always-visible "Hopper 1"/"Hopper 2" feeding controls.**

Justification: the vendor UI's "dual hopper" framing exists to let a user feed two *different foods*
to two *different cats* from two independent bins — that use case is what "Hopper 1" / "Hopper 2" as
parallel, equally-prominent top-level controls would communicate to Nitin. With the divider removed,
that premise is false: there is exactly one food and one bin. Surfacing two equally-weighted "Hopper 1
Amount" / "Hopper 2 Amount" controls at the top level would misrepresent the hardware and invite feed
amounts to silently drift out of sync (one auger wears faster, one gets a stale amount configured and
forgotten). At the same time, the two augers remain a genuine **mechanical** feature worth keeping
controllable — driving both halves the load (auger wear-leveling) and gives a manual fallback if one
auger jams (STUDY-mcu.md's `moto_runt_data.scram_reason`/`err_code` fields exist precisely for this).
So: one primary, prominent "Feed" control that defaults to splitting evenly across both augers, with
independent per-auger amounts available as an explicit, secondary/advanced (CONFIG, disabled by
default) capability for the wear-leveling/jam-workaround case — not hidden entirely, but not given
equal billing with the primary action either.

### 3.2 `kibble.feed` service

```yaml
service: kibble.feed
target:
  entity_id: button.cat_feeder_feed   # or omit target and call on the device
fields:
  amount:        # grams, total across whichever auger(s) run; 0-100. Required unless hopper split given explicitly.
    example: 10
  hopper:        # "1" | "2" | "both" (default: "both" — splits `amount` as evenly as the 2 single-byte
    example: both  #   fields allow, floor/ceil; STUDY-feedtest.md confirms amount1/amount2 are independent
                    #   single bytes, 0-50 each, so `amount` above 100 or an uneven split beyond ±1g needs
                    #   an explicit two-call sequence, not a single service call — this is a hard hopper-byte
                    #   limit, not an HA design choice)
  feed_id:        # optional correlation id echoed back in the kibble_feed event and feed history;
    example: "manual-2026-09-15T08:00"  #   auto-generated (uuid) if omitted, matching the proven payload's
                                          #   `id[64]` field (STUDY-feedtest.md struct layout)
  cancel:         # bool, default false. Maps to the proven `cancel` byte (offset +0) of the same struct
    example: false  #   — STUDY-app.md §6 confirms `feed_realtime_cancel` sets this byte to 1, same msg_id/dst.
```

Write mechanism: **HIGH confidence, proven live** — msg_id `0x6004` → dst 8 (`ble`), 67-byte
`feed_ctrl` payload `{cancel:u8, id:char[64], amount1:u8, amount2:u8}` (`STUDY-feedtest.md`). This is
the one capability in this entire document with a fully proven, tested write path; every other
mechanism below is inference from disassembly or naming evidence, not a live test.

BLE survival: **NO for phone-initiated BLE feed on stock firmware** — `STUDY-ble.md` §4.2 disassembled
`ctrl`'s BLE-message dispatcher exhaustively (5 recognized sub-types, none reaching
`dispatch_handler_feed`) and concludes feed-over-BLE does not exist in the stock binary. **YES for
kibbled once it replaces `ctrl`**: kibbled is the one process that both (a) owns the `ctrl`-side BLE
message dispatch (so it can add the missing branch) and (b) already knows the exact 0x6004 payload —
`STUDY-ble.md` §7 states this explicitly as a zero-MCU-firmware-change addition. This is a genuine,
concrete piece of remaining integration work (not a design gap): kibbled's own BLE-sourced dispatch
handler should call the same feed path stock `ctrl` uses locally, giving Wi-Fi-down feed for free.

### 3.3 Entities

| Capability | HA entity | Read mechanism | Write mechanism | EntityCategory |
|---|---|---|---|---|
| Manual feed (primary) | `button.cat_feeder_feed` | N/A (action entity) | `kibble.feed` with `hopper: both`, `amount: <number.cat_feeder_feed_amount>`. HIGH confidence. | — (control, enabled) |
| Feed amount (primary) | `number.cat_feeder_feed_amount` (0-100g) | HA-local helper state (see §1.3 `amount1`/`amount2` rows — not a persisted device setting). | Feeds the button above; no independent device write. | — (control, enabled) |
| Cancel feed | `button.cat_feeder_cancel_feed` | N/A | `kibble.feed` with `cancel: true`. HIGH confidence — same proven struct, `STUDY-app.md` §6 confirms the cancel byte. | — (control, enabled — safety-relevant, matches Localkit's own always-available cancel) |
| Hopper 1 amount (advanced) | `number.cat_feeder_hopper_1_amount` (0-50g) | HA-local, see §1.3 `amount1`. | `kibble.feed` with `hopper: "1"`. HIGH confidence. | CONFIG (disabled) |
| Hopper 2 amount (advanced) | `number.cat_feeder_hopper_2_amount` (0-50g) | HA-local, see §1.3 `amount2`. | `kibble.feed` with `hopper: "2"`. HIGH confidence. | CONFIG (disabled) |
| Dispensing in progress | `binary_sensor.cat_feeder_feeding` | config_shm offset 10238, HIGH confidence live-observed (see also §2). | N/A | — (PRIMARY, enabled) |
| Last feed amount / result | `sensor.cat_feeder_last_feed` (state: timestamp; attrs: amount1, amount2, manual, result, err_code) | `feed_over`/`feed_start` bus events and their cloud-JSON mirror `{id,day,manual,time,real_amount1,real_amount2,result,err_code}` (`STUDY-app.md` §6 "Feed-completion report with photo" shape) — MEDIUM confidence (cloud JSON shape proven from strings; the equivalent purely-local bus payload struct was not individually disassembled). | N/A — read-only. | — (PRIMARY, enabled) |
| Hopper food level (bin-wide, derived) | `binary_sensor.cat_feeder_food_low` | OR of the two per-auger sensors below (same table). | N/A | DIAGNOSTIC (disabled) — assignment explicitly places food level under Diagnostics |
| Hopper 1 / 2 food level (raw, per-auger) | `binary_sensor.cat_feeder_hopper_1_food_low` / `_hopper_2_food_low` | `state.ble.sta_data.food1_lack`/`food2_lack` (Table A #198-199), region only (`state.*`, offset ≥4900), exact byte NOT resolved (`STUDY-config.md` §7's "state.ble.* telemetry" subsection, numbered as `STUDY-config.md` §8 Open Question 1). LOW confidence offset, HIGH confidence field exists and is genuinely per-auger (independent physical sensors, not calibrated to agree once the bin is shared — see §3.1). | N/A — read-only. | DIAGNOSTIC (disabled) |

Kept as two independent per-auger diagnostics *and* one derived bin-wide primary-ish diagnostic
because the two raw sensors answer a different question ("is this specific auger's throat blocked?")
than the bin-wide derived one ("does the shared bin need a refill?") — collapsing to only one would
lose the auger-specific troubleshooting signal that STUDY-mcu.md's own per-auger telemetry design
implies the hardware still supports.

## 4. Schedule surface (non-trivial surface 2 of 5)

### 4.1 Ownership and shape

The MCU owns the real schedule (assignment framing, corroborated by: the T31 UART command table has a
dedicated `FEED_SCH ACK` response, CMD 0x04, `STUDY-mcu.md` §4; `ble`'s own outbound frame-builder has
two schedule-shaped send sites at CMD 0x04/0x05, each carrying "up to 65 bytes... schedule-shaped
payload", `STUDY-ble.md` §2.4; and `ctrl` registers a dedicated `dispatch_handler_ble_get_schedule`,
msg 0x101a, `STUDY-ble.md` §4.1/§5). Localkit's own cloud schema confirms the wire shape:
`feed: [{id, time, a1, a2, enable}, ...]` (`LOCALKIT-HARVEST.md` §3, "Feeding Schedule Format").

**Modeled as a list, not ~40 opaque per-slot entities**, per the assignment's own direction — one
`sensor.cat_feeder_schedule` whose state is the entry count and whose attributes hold the full list
(`entries: [{id, time, hopper1_g, hopper2_g, enabled}, ...]`, plus `last_modified` carrying the
`CTime` value, §1.2), and three services that mutate it.

### 4.2 Services

```yaml
service: kibble.schedule_add
fields:
  time:      { example: "07:30" }         # HH:MM, 24h — matches Localkit's `time` field verbatim
  hopper1_g: { example: 10 }              # grams, 0-50 — matches Localkit's `a1`
  hopper2_g: { example: 10 }              # grams, 0-50 — matches Localkit's `a2`
  enabled:   { example: true, default: true }

service: kibble.schedule_remove
fields:
  entry_id: { example: "a3f9..." }        # the `id` from sensor.cat_feeder_schedule's entries attribute

service: kibble.schedule_set_enabled
fields:
  entry_id: { example: "a3f9..." }
  enabled:  { example: false }
```

`kibble.schedule_set_enabled` is split out from a generic "edit" call because it is the one schedule
mutation the assignment's own event/entity model needs to support without a full remove+re-add
round-trip (e.g. an automation that pauses feeding while boarding the cats), and because `enable` is
independently named in Localkit's own per-entry shape — it is cheap to expose as its own small,
unambiguous call rather than overload `schedule_add` with an implicit upsert-by-id.

### 4.3 Read mechanism — HIGH confidence design choice, MEDIUM confidence on why it's necessary

**No "read the full schedule back from the MCU" UART command was recovered.** The 28-entry T31 command
table (`STUDY-mcu.md` §4, disassembly-complete, TBH jump table fully decoded) has exactly one
schedule-adjacent entry, CMD 0x04, and it is a **per-entry set + ack** pair (host sends one entry down,
MCU acks with a result code), not a bulk read-back. This was checked, not assumed: all 28 CMD values
0x00-0x1B are enumerated with their debug-string names, and none reads "get schedule" or similar.

Design: **kibbled maintains its own authoritative cache of the schedule**, updated synchronously on
every write it performs. This is sound *because* every schedule mutation is proven to flow through the
one process kibbled replaces: `ctrl` is the only local consumer of `property/set`'s `feed` key
(`STUDY-app.md` §6 "Feed data-path function names") and the only local sender of the ble-bound
schedule-set message (`dispatch_handler_ble_set_schedule` is an inbound handler on `ble`'s side, sent
*by* `ctrl` — `STUDY-msgids.md` §2, `STUDY-ble.md` §2.4); a BLE-sourced schedule *edit* was not found at
all (`dispatch_handler_ble_get_schedule`, msg 0x101a, is a **read**, per its own name, and even that
routes through `ctrl` — `STUDY-ble.md` §4.1). So once kibbled is the running `ctrl`, no other path can
change the schedule out from under it.

The one gap this design choice does not close: **on a cold boot of kibbled itself** (device power
cycle, or kibbled restarting after a crash), the in-memory cache starts empty with no MCU query to
repopulate it, and the *previous* on-device schedule (whatever was last ACKed) keeps running
autonomously on the MCU regardless (that is the whole point of MCU ownership) with kibbled unaware of
its contents until the user next edits it. This is flagged as the #2 item in the unknown-queue (§9) —
not a read mechanism that is missing outright (the design above is concrete and implementable) but a
correctness gap in that mechanism worth closing before shipping.

### 4.4 Entities

| Capability | HA entity | Read mechanism | Write mechanism | BLE survival | EntityCategory |
|---|---|---|---|---|---|
| Schedule list | `sensor.cat_feeder_schedule` (state: entry count; attrs: entries[], last_modified) | kibbled's own cache, populated from every `kibble.schedule_*` call it services — HIGH confidence *as a design*, see §4.3 caveat on cold-start. | N/A (read surface only; see services above for mutation) | Read: yes, once cached. Write: not yet — see below. | — (PRIMARY, enabled) |
| Add schedule entry | service `kibble.schedule_add` | — | `ble`-bound bus message → `dispatch_handler_ble_set_schedule` → UART CMD 0x04/0x05 per entry (`STUDY-ble.md` §2.4, handler name and UART CMD confirmed). **Exact ctrl→ble numeric `msg_id` and payload byte layout UNKNOWN** — same class of gap `STUDY-feedtest.md` closed for feed (0x6004); needs the identical disassembly-of-the-send-call-site technique. | Not yet proven — architecturally should work once kibbled owns both the BLE dispatch and the schedule-write path (same reasoning as feed, §3.2), but unlike feed this has not been disassembly-confirmed end to end. | n/a (service) |
| Remove schedule entry | service `kibble.schedule_remove` | — | Same UNKNOWN numeric msg_id/payload as add (an entry removal is presumed to be a set with `enable:false` and/or a zero-amount entry — Localkit's own schema has no separate delete op, only add/edit via the full array, so kibbled's `schedule_remove` most likely needs to resend the whole entry list minus one, not a single-entry delete message — **this delete-semantics question is itself unresolved and is folded into the same queue item**). | Same as add. | n/a (service) |
| Enable/disable schedule entry | service `kibble.schedule_set_enabled` | — | Same UNKNOWN msg_id family; `enable` is a named field in the proven wire shape (Localkit `{id,time,a1,a2,enable}`) so this is very likely the *same* set-entry message with only the `enable` byte changed, not a new mechanism — MEDIUM confidence this reuses schedule_add's eventual implementation exactly. | Same as add. | n/a (service) |

## 5. Camera + events surface (non-trivial surface 3 of 5)

### 5.1 Camera entity

**No local RTSP endpoint exists today.** `STUDY-app.md` §10 searched `media`'s full string table for
`rtsp://` and found nothing — live video on stock firmware only ever leaves via the Agora RTC relay.
The underlying Axera BSP *does* ship an RTSP server sample (`sample_vin_ivps_joint_venc_rtsp`,
`STUDY-soc.md`) — Petkit simply never wired it into this app — so the SDK-level capability exists,
Petkit's own binary just doesn't expose it. Kibble supplies this itself: per `STUDY.md`'s own agent
design constraint ("Camera: attach as an extra reader to media's existing H.264 ring
(`/dev/shm/media_buffer_frame_buf` + `sem.media_buffer_reader_N`); RTSP passthrough, zero re-encode, no
ISP ownership"), kibbled becomes a second reader of the H.264 NAL stream `media` already produces (no
change to `media`, no second encode, no ISP ownership contention) and re-packages those NALs into a
minimal RTSP stream of its own.

| Capability | HA entity | Read mechanism | Write mechanism | BLE survival | EntityCategory |
|---|---|---|---|---|---|
| Live camera stream | `camera.cat_feeder_live` | kibbled's own RTSP server (e.g. `rtsp://192.168.4.85:8554/live`, port TBD at implementation time), fed zero-recode from `media`'s existing H.264 shm ring — MEDIUM confidence (the ring-buffer attach point and format are named in `STUDY.md`'s constraints; the RTSP server itself is new code this document does not spec, per the assignment's no-code scope). HA reaches it through its built-in **go2rtc** add-on configured with that RTSP URL as a source, exposing a standard `camera` entity — no `rtsp_to_webrtc`/`generic camera` custom wiring needed. | N/A (streaming, not settable) | No — video pipeline is entirely Linux/`media`-side; the T31 has no camera. | — (control, enabled) |

Camera **enable** itself is `usr.app_conf.camera_enable` — already covered in §1.3's `camera` row
(promoted to an enabled-by-default control, not CONFIG, for the privacy-toggle reasoning given there).
When `camera_enable` is off, `camera.cat_feeder_live`'s state should reflect `unavailable`/`off`
rather than kibbled trying to keep serving RTSP from a `media` pipeline that has stopped producing
frames for that reason.

### 5.2 Image entities

`media`'s own snapshot pipeline already writes named JPEG files to `/tmp` on each relevant event
(`STUDY-app.md` §10): `/tmp/fPre_compStart.jpeg`/`fPre_compOver.jpeg` (feed), `/tmp/fPre_eat.jpeg`
(eat), `/tmp/fPre_pet.jpeg` (generic pet/motion visit). `libalgo.so` separately writes
`/tmp/saveFace.jpg` specifically on a face **match/enrollment** event (`STUDY-alg.md` §5) — a
higher-quality, algo-selected crop of just the matched face, not a full-frame snapshot.

| Capability | HA entity | Read mechanism | Write mechanism | BLE survival | EntityCategory |
|---|---|---|---|---|---|
| Last feed snapshot | `image.cat_feeder_last_feed` | `/tmp/fPre_compOver.jpeg`, refreshed by kibbled on each `feed_over` bus event — MEDIUM confidence (file path and trigger-adjacent event both confirmed by string evidence, `STUDY-app.md` §10/§6; the exact bus event that fires *after* this specific file is written was not disassembly-traced). | N/A — read-only. | No (media/alg only) | — (PRIMARY, enabled) |
| Last eat snapshot | `image.cat_feeder_last_eat` | `/tmp/fPre_eat.jpeg`, refreshed on `eat_start`/`eat_over` bus events. MEDIUM confidence, same evidence class. | N/A | No | — (PRIMARY, enabled) |
| Last visit snapshot | `image.cat_feeder_last_visit` | `/tmp/fPre_pet.jpeg` on `pet_detect`; kibbled should prefer `/tmp/saveFace.jpg` instead when a `pet_discern` event immediately follows the same `pet_detect` (higher-quality, algo-cropped face vs. the generic full-frame preview) — MEDIUM confidence on the preference logic (both files and both triggering events are independently confirmed; the "prefer the better one when both exist" policy is this document's own recommendation, not found in the vendor code, since stock `ctrl` never had to make this choice — it uploads both to the cloud rather than picking one for local display). | N/A | No | — (PRIMARY, enabled) |

### 5.3 HA events

Sourced from the disassembly-confirmed `petkit_event_result_info` struct
(`index:int, pet_id:u32, score:float, action_type:i32, exception_type:i32, event_pet_id:u32`,
field order and types HIGH confidence — verbatim printf format string, `STUDY-alg.md` §4) plus the
cloud event-report JSON shapes `ctrl` already builds for the same underlying events
(`STUDY-app.md` §6), reshaped into local HA event payloads (no cloud round-trip needed — kibbled
listens on its own inbox for the same bus messages `ctrl` used to consume).

```yaml
event: kibble_visit          # fires on pet_detect + (if it arrives) the matching pet_discern
data:
  event_id: "..."             # correlates detect -> discern, mirrors the proven `event_id`/`related_event` pair
  pet_id: 1                   # 0 = unknown/no gallery match (STUDY-alg.md §3: pet_id 0 is the documented "no match" sentinel)
  score: 0.87                 # re-id cosine-similarity-derived confidence, 0-1 float
  action_type: 2               # from petkit_event_result_info, meaning not decoded by this study (see §9 queue)
  image_entity: image.cat_feeder_last_visit
  start_time: "2026-09-15T08:00:03-04:00"

event: kibble_feed           # fires on feed_start (manual only) and feed_over (scheduled + manual)
data:
  event_id: "..."
  manual: true                 # 0=scheduled, 1=manual, per the proven feed_over content shape (STUDY-app.md §6)
  hopper1_g: 10
  hopper2_g: 10
  result: 0                    # 0 = success; nonzero values not enumerated by this study (§6 queue)
  image_entity: image.cat_feeder_last_feed
  time: "2026-09-15T08:00:00-04:00"

event: kibble_eat            # fires on eat_start and eat_over (two deliveries per visit; `phase` distinguishes)
data:
  event_id: "..."
  phase: start                 # start | over
  pet_id: 1                    # 0 = unknown
  score: 0.87
  start_time: "2026-09-15T08:01:10-04:00"
  end_time: null                # populated only on the `over` delivery
  image_entity: image.cat_feeder_last_eat
```

`image_entity` is included instead of an inline image blob so automations can reference the live HA
`image` entity (always current) rather than a stale byte payload frozen at event time — the underlying
JPEG the entity serves is the same file the event corresponds to at the moment it fires, since
kibbled refreshes the `image` entity synchronously before emitting the event.

## 6. Cat identification surface (non-trivial surface 4 of 5)

### 6.1 Mechanism

The gallery match is `libalgo.so`'s `CPetkitAlgoPetfeat::petkit_petfeat_process`/
`petkit_petfeat_cos_distance` — a 512-float re-id embedding (`feat_model`/
`petkit_face_rec_mtl_s2_v5_sim.axmodel`, output tensor shape confirmed directly from the compiled
model container, HIGH confidence, `STUDY-alg.md` §3.1) compared by cosine distance against
`/opt/feature.bin`, a versioned (`"v0.0.3"`), CRC32-checked gallery file keyed by `(pet_id,
face_feature_id)` (HIGH confidence — confirmed by adjacent debug strings and the
`petkit_feature_add(pet_id, face_feature_id, path)` exported signature, `STUDY-alg.md` §3.3). The
public result — all a consumer gets without reverse-engineering private C++ object layouts,
`STUDY-alg.md` §3.2/§7 recommends against that — is `petkit_get_event_result_info`'s struct: `pet_id`
(0 = no match), `score`, plus timing/action fields (§5.3 above). **The raw embedding itself never
crosses the public API boundary** — `STUDY-alg.md` §7 recommends the first-party agent run the same
`feat_model` independently in its own process against its own crops rather than trying to read
libalgo's internal vectors, and that recommendation is adopted here: kibbled's cat-ID pipeline is a
second, independent NPU client, not a hook into `media`'s existing one.

Nitin has **two enrolled cats**. `/opt/feature.bin`'s exact binary layout (header/CRC placement/
per-entry size) was not recoverable offline — it is populated at runtime under `/opt`, outside the
static backup this study worked from (`STUDY-alg.md` §8 item 2) — but its **existence, path, format
version tag, and keying scheme** are all confirmed, which is what "read mechanism" below relies on.

### 6.2 Entities

Per-cat entities are **not** two hardcoded rows — they are generated dynamically from whatever
`pet_id`s are actually enrolled in `/opt/feature.bin` at integration setup (currently 2, for Nitin's
two cats), so the design holds if a third cat is ever enrolled without a document change.

| Capability | HA entity | Read mechanism | Write mechanism | BLE survival | EntityCategory |
|---|---|---|---|---|---|
| Last-seen pet | `sensor.cat_feeder_last_seen_pet` (state: pet name/id or "unknown"; attrs: score, timestamp, event_id) | `pet_discern` bus event → `petkit_get_event_result_info` struct, `pet_id`/`score`/`event_pet_id` fields (HIGH confidence field order/types, `STUDY-alg.md` §4; MEDIUM on exact byte offsets within the struct, `STUDY-alg.md` §8 Open Question 1). | N/A — read-only. | No (NPU/camera pipeline is Linux-only). | — (PRIMARY, enabled) |
| Per-cat presence (one per enrolled `pet_id`, e.g. `binary_sensor.cat_feeder_<cat_name>_present`) | `binary_sensor.cat_feeder_<slug>_present` | Same `pet_discern` event, filtered to `pet_id == <this cat's id>` and `score` above the configured `featThresh` (see `petkit_modify_algo_threshold`, `STUDY-alg.md` §2 — exact struct offsets disassembly-confirmed); "present" latched for a short window (implementation detail — not specified here) after the last matching detection. Entity is instantiated per pet_id discovered by enumerating `/opt/feature.bin` at setup (mechanism confirmed to exist; exact record layout not resolved, `STUDY-alg.md` §8 item 2 — enumerating by *reading* the file is safe/read-only regardless of layout uncertainty, since kibbled only needs the count and ids, recoverable once the record boundary is found). | N/A — read-only. | No | — (PRIMARY, enabled) |
| Unknown pet detected | `binary_sensor.cat_feeder_unknown_pet_detected` | Same `pet_discern` event, filtered to `pet_id == 0` (the documented "no match" sentinel, `STUDY-alg.md` §3.1) OR `score` below `featThresh` for every enrolled id. | N/A — read-only. | No | — (PRIMARY, enabled — a genuinely new/unrecognized visitor is actionable information, e.g. a neighborhood cat getting into the feeder) |

Cat *names* (the `<slug>` above) are not recoverable from the device at all — `/opt/feature.bin` keys
on `pet_id`, an opaque integer, with no name string anywhere in the studied binaries. Naming is a
one-time HA-side mapping step (config flow asks the user to name each discovered `pet_id` once,
comparable to how the phone app itself must have asked Nitin to name "Cat A"/"Cat B" at enrollment
time) — not a device capability, so it is not modeled as a read/write row here.

## 7. Diagnostics surface (non-trivial surface 5 of 5)

All entities below are DIAGNOSTIC (disabled by default) per the assignment's own explicit list, except
the `update` entity, which house rule 4 always keeps enabled. Food level per hopper is designed in
§3.3 (feeding surface) rather than repeated here, since it shares that section's hopper-divider
reasoning; error state is designed in §2.

| Capability | HA entity | Read mechanism | Write mechanism | BLE survival | EntityCategory |
|---|---|---|---|---|---|
| Desiccant days remaining | `sensor.cat_feeder_desiccant_days` | **NOT LOCATED — see §7.1, the one exception to "every entity is readable" in this document.** | N/A — device-computed countdown, not app-writable in the stock protocol (Localkit's schema has no `desiccant`/`resetDesiccant` key at all — this capability is inferred to exist from `pypetkitapi`/community prior art on other Petkit feeders, `PRIOR-ART.md`, not from this device's own studied protocol). | Unknown | DIAGNOSTIC (disabled) |
| Battery backup capacity | `sensor.cat_feeder_battery` (device_class: battery, %) | `state.ble.sta_data.bat_capac` (Table A #197) — region only (`state.*`, offset ≥4900), exact byte **not resolved** (`STUDY-config.md` §8 Open Question 1, same gap as the rest of `state.ble.*`). Fallback raw signal: `state.ble.adc_data.bat_ADC` (Table A #188), same region/confidence. LOW confidence on exact offset, HIGH confidence the field exists and is exactly this concept (4×D backup batteries are a documented feature of this feeder family). | N/A — read-only. | Yes (battery state is meaningful specifically when mains/Wi-Fi power might be down) | DIAGNOSTIC (disabled) |
| Battery voltage (raw) | `sensor.cat_feeder_battery_voltage` | `state.ble.sta_data.ubat` (Table A #203), same region/confidence as above. | N/A | Yes | DIAGNOSTIC (disabled) |
| Wi-Fi signal (RSSI) | `sensor.cat_feeder_rssi` (device_class: signal_strength) | `usr.wifi.net_inf.rsq`/`.signal` (Table A #57-58) — same low-confidence `usr.*` cluster region as most of §1's rows (2856-3668-adjacent); LOW confidence exact offset. | N/A — read-only. | No (meaningless once Wi-Fi is down) | DIAGNOSTIC (disabled) |
| Main SoC firmware version | `sensor.cat_feeder_firmware` | `dev.version_info.ota_param.firmwareVer` — **HIGH confidence**, offset 4832, `config_layout.json` (value `"895"`, stored as a decimal string not an int). | N/A — factory/OTA-written, not app-settable. | Yes (static factory field) | DIAGNOSTIC (disabled) — also feeds `update.cat_feeder_firmware`'s `installed_version` |
| T31/BLE MCU firmware version | `sensor.cat_feeder_mcu_firmware` | `dev.version_info.ota_param.firmware_ble` — **HIGH confidence**, offset 4876, value `159` (`config_layout.json`). | N/A | Yes | DIAGNOSTIC (disabled) |
| Firmware update available | `update.cat_feeder_firmware` | Compares the two sensors above against whatever kibbled's own release channel reports — this is new, Kibble-side infrastructure (the vendor OTA channel is explicitly out of scope: `STUDY.md`/`LOCALKIT-HARVEST.md` both confirm this hardware has **no OTA support** at all). Read mechanism for the *installed* half is therefore HIGH confidence (the two firmware sensors above); the *latest available* half is Kibble's own release feed, not a device capability this study can source. | Installing an update is **out of scope for the vendor firmware** (no OTA path exists on this hardware) — this entity tracks **Kibble agent** (`kibbled`) version updates, not Petkit firmware updates. | N/A (Kibble's own update channel) | — (always enabled, house rule 4 exception) |

### 7.1 The desiccant exception

**This is the one capability in this document without a concrete read mechanism**, flagged explicitly
rather than papered over, because a genuine gap exists: the scout-stage `PRIOR-ART.md` claimed
`"desiccant_status per pktool strings"`, but this study's own two **independent, verified** extractions
of the full config schema — `STUDY-config.md`'s 228-entry pktool debug-string dictionary (Table A,
directly extracted from `.rodata`) and `STUDY-app.md` §4's independent `g_config->` string grep —
**both come up with no field named `desiccant` anywhere**. The `desiccant=30d` value referenced in
`STUDY-config.md` §1 was a ground-truth anchor value handed to that study from the wider task brief,
used as a search target during value-anchoring, not a value that was actually located in the dump.
Community prior art (`pypetkitapi`, `PRIOR-ART.md`) confirms desiccant tracking is a real feature on
Petkit feeders generally, so the capability almost certainly exists on this hardware too — it simply
has not been pinned to a byte yet.

Two concrete, safe next steps (not yet executed — read-only, no live device contact was made by any
part of this study): (1) a second live diff capture, replace/reset the desiccant packet and diff
`config_shm` before/after — the exact methodology `STUDY-config.md` §1 already used successfully for
every other field it resolved; (2) a targeted string search for `"desiccant"`/`"drying"`/
transliterated-Chinese variants directly in `pktool`'s `.rodata`, since Table A's 228 entries were
extracted from one specific format-string pattern (`"g_config-><path>\t= <fmt>\n"`) and a
differently-formatted debug string for this field would not have been caught by that extraction. Both
are listed as the top item in §9's queue.

## 8. Beyond the app — root-only capabilities

Six capabilities the Petkit app cannot offer at all — not "the app has a worse version," but
capabilities that require the local root access this project already has and the stock cloud-app
architecture structurally cannot reach. Each is modeled as a first-class entity/service below, with
its own read/write mechanism and confidence, exactly like every other row in this document. A short
"will NOT expose despite root" list closes the section.

### 8.1 Instant local feed + real-time feeding status

**Why the app cannot do this, structurally:** stock `ctrl` is exclusively a cloud MQTT/HTTP client —
`STUDY-app.md` §3/§6 establish there is no local HTTP/mDNS control surface in the shipped firmware at
all. Even with the phone on the same LAN as the feeder, the app's feed command still leaves the house:
phone → Petkit's Alibaba-hosted MQTT broker → back down to the device (`STUDY-app.md` §6, "Backend
confirmed as Alibaba Cloud IoT Platform"). Both the command AND Petkit's cloud being reachable at all
are on the critical path every single time, even at home.

| Capability | HA entity | Read mechanism | Write mechanism | Confidence |
|---|---|---|---|---|
| Instant local feed | `button.cat_feeder_feed` / `kibble.feed` (§3.2, unchanged) | — | Same proven msg_id `0x6004` → dst 8, local `mq_send` — a same-host IPC call, not a network round trip. `STUDY-feedtest.md`'s own measured "1.8s from local message to device reporting a real feed" is dominated by the **observation** side of that test (a still-cloud-routed HA sensor confirming the motor ran) — the **command** write itself is a single synchronous `mq_send`, sub-millisecond, with zero network hop. | **HIGH — proven live**, the one fully tested mechanism in this document (§3.2). |
| Real feeding-in-progress status | `binary_sensor.cat_feeder_feeding` (§3.3, unchanged) | config_shm offset **10238**, observed transient 0→1→0 during the proven feed test — also a same-host `mmap` read, no cloud round trip, no polling delay beyond kibbled's own read cadence. | N/A — read-only. | **HIGH — proven live** (§3.3/§2). |

Both entities already exist in §3 — this subsection exists to name explicitly, per Nitin's request,
*why* they are a root-only win even on a fully healthy Wi-Fi/cloud connection: latency and a
dependency Kibble removes, not features Kibble adds.

### 8.2 Portion size and schedule capacity beyond the app's UI limits

| Capability | HA entity | Read mechanism | Write mechanism | Confidence |
|---|---|---|---|---|
| Portion size up to 255 g/hopper | `kibble.feed`'s `amount`/`hopper` fields (§3.2) accept the full wire range; the always-visible `number.cat_feeder_feed_amount`/`hopper_1_amount`/`hopper_2_amount` entities (§3.3) intentionally **keep their existing 0-50/0-100 default max** rather than widening to 255. | N/A (write capability, not a stored setting) | The proven 67-byte `feed_ctrl` struct's `amount1`/`amount2` fields are literal `uint8_t` (`STUDY-feedtest.md`), i.e. 0-255 at the wire level — Localkit's documented app UI range (0-50) is a phone-app validation choice, not a protocol ceiling; nothing in the disassembled struct or the MCU's `Motor Run Config Cmd` handler clamps it lower. | **HIGH on the wire capability** (struct field width is unambiguous); **judgment call, not evidence**, on keeping the default-visible entities conservative — dispensing 255 g in one cycle has no compensating benefit and a real overfeeding/jam risk with two cats sharing one bin (§3.1), so the full range is reachable only through the service call, not the default slider, until Nitin says otherwise. |
| Schedule entries beyond the app's per-slot cap | `sensor.cat_feeder_schedule` / `kibble.schedule_add` (§4) already impose **no artificial limit** — this is "beyond the app" by construction, not by a discovered protocol number. | — | — | **TO-VERIFY, explicitly**, per Nitin's own instruction: no MCU-side storage-capacity constant was found anywhere in this study. The per-entry UART write (CMD 0x04/0x05, up to 65 bytes) has no accompanying "N of M" or batch-count field to infer a ceiling from (`STUDY-mcu.md` §4, `STUDY-ble.md` §2.4), and the T31's own flash-storage budget for the schedule array is opaque (TC32 firmware, no disassembler support — `STUDY-mcu.md` §2 item 6). The real ceiling is whatever the MCU firmware actually allocates; finding it requires either adding schedule entries live and watching for a MCU-side rejection (an ACK-code check, non-destructive) or extracting the TC32 firmware's own data-segment layout, neither done by this study. |

### 8.3 Local, unlimited feed/visit/eat history — no Care+ gate

Petkit's cloud "Care+" subscription is what gates event/video retention in the stock app (short free
tiers, longer retention behind a paid plan — general Petkit-product-line knowledge, not something this
device's own binaries need to confirm, since Kibble simply never routes through that gate at all).
Every `kibble_feed`/`kibble_eat`/`kibble_visit` event (§5.3) already carries everything needed for a
permanent local record; this subsection adds the **retrieval** side.

```yaml
service: kibble.get_event_history
fields:
  since:      { example: "2026-09-01T00:00:00-04:00" }   # optional, default: all recorded history
  until:      { example: "2026-09-15T00:00:00-04:00" }   # optional, default: now
  event_type: { example: feed }                            # optional: feed | eat | visit
  pet_id:     { example: 1 }                                # optional filter, from §6's gallery ids
  limit:      { example: 100 }
# returns: [{event_id, type, time, pet_id, score, amount1, amount2, image_url}, ...]
```

Read/write mechanism: **new Kibble-side infrastructure**, not a device protocol question — every
underlying fact (feed/eat/visit event triggers, image file paths) is already sourced with its own
confidence in §5/§6; what is new here is that kibbled **persists** every occurrence instead of only
emitting a live HA event. One real engineering constraint this design must respect and is called out
explicitly: `STUDY.md`'s own measured budget gives the device only **57 MB free under `/opt`** total,
shared with everything else Kibble needs — nowhere near enough for "unlimited" **images** on-device.
The recommended split: kibbled keeps the full event **metadata** log locally (a few hundred bytes per
record — even years of history costs low single-digit MB), and pushes each **image** to HA (or
wherever HA's own media storage lives) at generation time rather than retaining images on the
embedded device — HA's own disk, not the feeder's 57 MB, is what makes "unlimited" real. This is a
design recommendation grounded in §7's own capacity numbers, not a discovered device capability.

### 8.4 Hardware health diagnostics the app hides

| Capability | HA entity | Read mechanism | Confidence |
|---|---|---|---|
| Motor current per feed (real-time + peak) | `sensor.cat_feeder_motor_current` / `_motor_current_peak` (attrs on `sensor.cat_feeder_last_feed`, §3.3) | `state.ble.moto_runt_data.rt_curt` / `.curt_max` (Table A #210-211) | LOW — region only (`state.*`, offset ≥4900), exact byte not resolved, same gap as all `state.ble.*` (`STUDY-config.md` §8 Open Question 1). |
| Hall-sensor auger position/timing per feed | `sensor.cat_feeder_auger_position` (attrs: `hall_run_pos`, `delta_hall_time_ms`) | `state.ble.moto_runt_data.pos.hall_run_pos` / `.pos.delta_hall_time_ms` (Table A #206-207) | LOW, same class. |
| Motor scram/fault reason | `sensor.cat_feeder_motor_fault` (attrs on last-feed sensor) | `state.ble.moto_runt_data.scram_reason` / `.result` (Table A #204-205) | LOW, same class. |
| Hopper proximity, raw per side | `sensor.cat_feeder_hopper_1_proximity_raw` / `_hopper_2_proximity_raw` | `state.ble.adc_data.proxl_rw` / `.proxr_rw` (Table A #191-192) — the un-thresholded version of §3.3's `food1_lack`/`food2_lack` booleans | LOW, same class — genuinely more useful than the boolean once resolved, since it would show *trend* (auger throat slowly clogging) not just a binary flip. |
| MCU RTC drift | `sensor.cat_feeder_rtc_drift` (seconds, Linux clock minus MCU-reported clock) | **Proposed, not proven**: UART CMD 0x13 ("RTC data") has a 0-byte-payload host→MCU send variant among its three confirmed call sites (`STUDY-ble.md` §2.4) — plausibly a bare "send me your current time" query, with the MCU's reply parsed the same way `state.ble.*` telemetry already is. Kibbled would diff that against its own (NTP-disciplined) Linux clock on each poll. | **TO-VERIFY** — the 0-byte call site's semantics were not disassembled to confirm it is a query rather than e.g. a "sync now" trigger with an implicit current-time argument; this is this document's best-evidenced hypothesis, not a confirmed mechanism. |
| NAND flash ECC/bad-block health | `sensor.cat_feeder_nand_health` (attrs: bad_blocks, ecc_corrected) | Standard Linux MTD subsystem — `/sys/class/mtd/mtdX/{bad_blocks,ecc_stats}` (or the equivalent `/proc/mtd` summary), and/or the already-confirmed-safe `pktool get_mtd_info_and_badblocks` read-only subcommand (`STUDY-app.md` §9) | **HIGH** that the mechanism exists (standard kernel subsystem on a confirmed SPI-NAND/UBI boot chain, `STUDY.md`'s architecture map, `STUDY-boot.md`) — **MEDIUM** on the exact sysfs paths for this specific kernel build (4.19.125), not independently walked live by this study. |
| System uptime | `sensor.cat_feeder_uptime` | `/proc/uptime` — standard Linux, no device-specific reverse engineering needed. Distinct from `state.dev_pro.http_online_time_s` (Table A #181), which tracks *HTTP-connectivity* uptime specifically, not general system uptime — both are worth keeping as separate diagnostics. | **HIGH** for `/proc/uptime` itself; **LOW** for the http_online_time_s companion (same low-confidence `state.dev_pro` cluster, offset 4892-7316 region, as the rest of that section). |
| Reboot cause | `sensor.cat_feeder_last_reboot_reason` | `state.dev_pro.power_on_src` (Table A #146, "Power-on reason") — low-confidence `state.dev_pro` cluster region; secondary source: `watchdog`'s own kill/reboot log lines (`"watchdog =================kill %s[%d]===================="`, `"reboot -f"` — confirmed strings, `STUDY-msgids.md` §3) if kibbled can tail wherever `watchdog` writes them. | LOW on the config_shm field's exact offset; MEDIUM on the watchdog-log fallback existing at all (strings confirmed, log destination/persistence not confirmed). |

Wi-Fi RSSI is already designed in §7 (`sensor.cat_feeder_rssi`) and is not repeated here.

### 8.5 Per-cat feeding rules (root-only automation, not offered by the app at all)

Composes two already-designed, independently-evidenced primitives — §6's cat-ID event stream and
§3.2's proven feed path — into a capability the app has no concept of at all: auto-feed *this* cat
when it's identified, never mind the clock.

```yaml
service: kibble.set_pet_feeding_rule
fields:
  rule_id:          { example: "isabel_evening" }
  pet_id:            { example: 2 }                 # required — from §6's gallery ids; this is the whole point
  hopper:            { example: both }               # "1" | "2" | "both"
  amount:            { example: 15 }
  cooldown_minutes:  { example: 240, required: true } # minimum time between auto-feeds for THIS pet under
                                                        #   THIS rule — required, not optional: without it a
                                                        #   cat that visits repeatedly gets fed repeatedly,
                                                        #   which is an animal-welfare problem, not a UX nit
  active_hours:      { example: "06:00-20:00" }       # optional
  enabled:           { example: true, default: true }
```

Read mechanism: the same `pet_discern`-derived stream §6.2 already designs (`petkit_get_event_result_info`,
`pet_id`/`score` fields, HIGH confidence field order, `STUDY-alg.md` §4). Write mechanism: the same
proven `0x6004` feed path (§3.2, HIGH confidence). Trigger logic (match `pet_id`, check `score` against
`featThresh`, check cooldown/active-hours, then feed) runs **inside kibbled**, not as an HA automation
— deliberately, so it keeps working even if HA itself is down, and so the pet-to-camera-frame-to-feed
latency stays a local, same-process decision rather than a round trip through HA's own automation
engine. The resulting feed still emits the normal `kibble_feed` HA event (§5.3) for visibility/logging,
tagged with `rule_id` so the history (§8.3) can distinguish rule-triggered feeds from manual/scheduled
ones.

### 8.6 Event clips straight from the encoder ring — no subscription

Extends §5.1 (kibbled already reads `media`'s H.264 ring, zero-recode, for the live RTSP stream) and
§5.3 (event payloads): on each `kibble_visit`/`kibble_feed`/`kibble_eat` event, kibbled muxes a short
local clip (e.g. -10s/+10s around the trigger) from the same H.264 NAL stream it already has open — no
second encode, no ISP re-ownership, and no Petkit cloud/Care+ subscription anywhere in the path. §5.3's
event payloads gain one additional optional field once this is built: `clip_url`, alongside the
existing `image_entity`. Read mechanism: identical to §5.1 (HIGH confidence the ring-buffer attach
point exists and is zero-recode-capable, MEDIUM on the muxing logic since it is new code this document
does not spec, consistent with the assignment's no-code scope). Storage: same recommendation as §8.3 —
push clips to HA-side storage rather than retain them on the feeder's constrained `/opt`.

### 8.7 What Kibble will NOT expose despite having root

| Capability | Why not |
|---|---|
| Direct motor PWM/current overrides | The MCU's own closed-loop motor sequencing (Hall-feedback stop position, current-limit scram — `state.ble.moto_runt_data.scram_reason`, §8.4) is the only proven-safe way to run the auger. A raw PWM/current override bypasses that safety loop entirely for zero capability gain over the existing amount-based `kibble.feed` (§3.2) — pure animal-welfare/mechanical risk, no upside. |
| Watchdog control (reconfigure, disable, or kill) | The watchdog is the device's only crash-recovery path (stale heartbeat → kill → `reboot -f`, `STUDY.md`). Disabling or reconfiguring it trades away the sole failsafe for no capability gain — and killing any supervised process is separately, explicitly forbidden by this project's own operating constraints, for the same underlying reason (the watchdog reboots the device in response). |
| ISP/sensor register-level tuning (exposure curves, AWB internals, sensor register writes) | `pktool` already exposes the handful of ISP knobs that matter at the app level (`set_fps`, `set_ispHz`, `set_offset` — §1.3's `hertz` row), matching every camera setting the app itself offers (§1.3). Deeper 3A-algorithm/sensor-register tuning has no user-facing benefit beyond what is already modeled and risks image-quality regressions or corrupting factory calibration state for a capability nobody asked for. |
| Raw `/dev/ttyS3` access from a second reader | `ble` already owns this UART exclusively; a second reader steals bytes mid-frame from the one process correctly speaking the T31 protocol (this exact action is separately forbidden by this project's own constraints). Every capability the MCU exposes is reachable through `ble`'s existing bus handlers (§1, §3, §4, §8.4) without ever opening the device node directly. |
| Arbitrary `pktool set_*` passthrough as a general-purpose HA service | Individual `set_*` subcommands are cited throughout this document (§1.3 `volume`/`hertz`) as *evidence a mechanism exists*, each with its own specific, bounded HA entity. A blanket "run any pktool set command" service would re-expose the `Aging_feed_ctrl`/`PT_feed_ctrl` production-test actuators (`STUDY-app.md` §9) that this project's own constraints forbid dispensing food through outside the proven `kibble.feed` path — the bounded, per-capability entities in this document are the safe surface; a raw passthrough is not. |

## 9. Consolidated unknown-write-path queue

Every write mechanism marked UNKNOWN anywhere in this document, in one list, ordered by how much it
blocks a designed surface from being real (Tier 1) down to purely cosmetic (Tier 3). This is the next
reverse-engineering queue. The proven method for closing a Tier-1 item is already demonstrated twice
in this study's own history — `STUDY-feedtest.md` resolved msg_id `0x6004` for feed by disassembling
`ctrl`'s `dispatch_send_msg` call site and reading the immediate `movw r0, #0x6004` constant directly;
the same technique (find the `bl dispatch_send_msg` call site inside the relevant handler, read back
the immediate `msg_id`/`dst` operands) is the concrete next step for every bus-message item below, not
a new methodology to invent.

A companion, non-write gap worth stating once here rather than re-flagging per row: **all of
`state.ble.*`'s 27 fields (battery, food-lack flags, error code, motor telemetry — Table A #187-214)
are read-located only to a region, not an exact byte offset** (`STUDY-config.md` §8 Open Question 1),
and the **desiccant field has no location at all** (§7.1). These are read-precision gaps, not write
gaps, so they are not repeated as queue entries below, but closing `state.ble.*`'s offsets in one pass
(live diff during an actual feed cycle, which STUDY-feedtest.md's own delta table shows already
produces exactly this kind of transient signal at offset 10238) would sharpen the confidence on more
rows in §3.3 and §7 than any single item in the write-queue below.

### Tier 1 — blocks a whole designed surface from being fully real

1. **Schedule per-entry write** (§4.4) — numeric `ctrl→ble` outbound `msg_id` and exact payload byte
   layout for `dispatch_handler_ble_set_schedule` (handler name and target UART CMD 0x04/0x05 known;
   the wire bytes are not). Blocks all three schedule services from being implementable at all.
2. **MCU schedule read-back / cold-start cache seeding** (§4.3) — no "get schedule" UART command was
   found in the fully-decoded 28-entry T31 command table. Without this, `sensor.cat_feeder_schedule`
   is wrong from boot until the first edit. Needs either (a) confirming no such command exists and a
   local persistence file is the only fix, or (b) finding an undocumented response path on CMD 0x0C
   (`MCU_BASE_CFG req`), the most plausible untraced candidate.
3. **Status LED numeric msg_id + payload** (§1.3 `lightMode`) — two ctrl-side handler names are
   confirmed (`dispatch_handler_ledlight_mode_set` msg 0x0010, `dispatch_handler_sync_led_mod` msg
   0x101d) but the ble-bound message they trigger and its payload are not. Most physically-visible
   feedback control in the whole device.
4. **Camera enable propagation** (§1.3 `camera`) — confirm whether `media` polls `config_shm` live
   (in which case a raw memory write, already the presumed mechanism for most CONFIG rows, is
   sufficient) or needs an explicit push notification to stop/start the ISP pipeline immediately. The
   one setting promoted to an enabled-by-default control (§1.3), so its correctness matters more than
   any other single `usr.app_conf.*` row.
5. **BLE-sourced feed dispatch** (§3.2) — not a config_shm question at all: kibbled's own future BLE
   message handler needs to add the missing branch to reach `dispatch_handler_ble_feed_ctrl`, exactly
   as `STUDY-ble.md` §7 already specifies. This is implementation work more than reverse-engineering,
   but it is the one remaining piece standing between "feed works over Wi-Fi" (proven) and "feed works
   with Wi-Fi down" (designed, not yet built).

### Tier 2 — tuning/quality surfaces; sensible defaults work without these

6. **Detection enable flags** — `moveDetection`, `petDetection`, `eatDetection`, `vomitDetection`
   (§1.3). Needed to let the user turn cat-ID/event generation off; defaults (presumably all-on from
   factory) work fine without a fix here.
7. **Detection sensitivity mapping** — `moveSensitivity`, `petSensitivity`, `eatSensitivity` (§1.3).
   The consumer function is disassembly-confirmed (`petkit_modify_algo_threshold`, exact struct
   offsets known, `STUDY-alg.md` §2) but which UI 1-9 value maps to which of its six float args is not
   traced end-to-end from `config_shm` through the app-side lookup table.
8. **`detectInterval` end-to-end trace** (§1.3) — plausibly `petkit_set_tracker_interval(int)`, a
   confirmed exported single-int function, but not traced from `config_shm` to that call.
9. **Calibration factors** `factor1`/`factor2` (§1.3) — affects per-gram dispense *accuracy*, not
   whether feeding works at all; a user can work around this today by adjusting the requested amount
   empirically.
10. **Leftover-food threshold** `surplusStandard` (§1.3) — tunes the food_model's leftover-detection
    algorithm; defaults are usable.
11. **Low-food warning** `foodWarn` + `foodWarnRange` (§1.3) — includes the unresolved range-string
    serialization format (see item 15).
12. **Sound cluster** — `soundEnable`, `systemSoundEnable`, `feedSound`, `selectedSound` (§1.3). Note
    `volume`'s *write* mechanism is separately MEDIUM confidence via `pktool set_spk_vol` (0-100 scale,
    confirmed actuating CLI command) — usable today via a shell-out fallback even before the "proper"
    config_shm/bus path is found, so `volume` itself is not repeated in this queue.
13. **Child lock** `manualLock` (§1.3) — also flagged as a candidate for a rule-4 operational exception
    (like the BLE-proxy Restart buttons) — worth a product decision from Nitin alongside the RE work.
14. **Camera-adjacent secondary toggles** — `microphone`, `night`, `timeDisplay`, `eatVideo`,
    `feedPicture`, `smartFrame` (§1.3). Lower priority than `camera` itself (item 4) since these only
    matter once the camera is already on.
15. **Do-not-disturb** `toneMode` + `toneMultiRange` (§1.3).
16. **Multi-range time-window serialization format** — shared unknown across `lightMultiRange`,
    `cameraMultiRange`, `cameraRangeTable`, `detectMultiRange`, `toneMultiRange`, `foodWarnRange`
    (six rows in §1.3, all `%.*s`-typed string fields per Table A). Resolving the string format once
    (JSON vs. custom delimiter — a single live read of one populated field settles it) unblocks all
    six simultaneously, which is why it is one queue item rather than six.

### Tier 3 — cosmetic, rarely touched

17. **OSD costume/logo** `attireId` + `logo_cn` (§1.3) — the valid-value *list* is already
    read-recoverable (`etc/defAttire.tar.gz` filenames, no live contact needed); only the write
    mechanism itself is missing.
18. **`hertz` production-path confirmation** (§1.3) — `pktool set_ispHz` is a confirmed, already-usable
    actuating fallback; this item is only about confirming whether the "real" app-parity path differs.
19. **Desiccant field location** (§7.1) — listed last only because it is a DIAGNOSTIC-category,
    disabled-by-default entity with no user-facing control attached to it, not because it is
    unimportant; it is the only capability in this entire document with no located read mechanism at
    all, so closing it converts this document's one explicit exception into a fully-specified row.
