# Appendix: HA entity inventory (2026-09-16)

Point-in-time inventory of every entity and service the Kibble integration exposes, and what each reads
from `KibbleData`. Produced from the platform sources on the date above; it is the compatibility list
`33-local-push.md` is held to. §5's mapping proposal is historical -- the `vendor_pet_ids`
option (options flow) was implemented the same day. Entities added the same day and not in the tables
below: `sensor.vendor_last_seen_pet`.

---

# Kibble HA Integration Entity Inventory

## 1. Platform Entity Tables

### sensor.py (33 entities)

| Translation Key | Entity Class | Endpoint/Field | Device Class | State Class | Entity Category | Enabled by Default | Icon | Strings |
|---|---|---|---|---|---|---|---|---|
| bowl_fill_1 | KibbleSensor | GET /state → bowl_fill[0] | — | MEASUREMENT | — | Yes | gauge | ✓ |
| bowl_fill_2 | KibbleSensor | GET /state → bowl_fill[1] | — | MEASUREMENT | — | Yes | gauge | ✓ |
| desiccant_days | KibbleSensor | GET /state → desiccant_days | — | — | DIAGNOSTIC | No | air-filter | ✓ |
| firmware | KibbleSensor | GET /state → firmware | — | — | DIAGNOSTIC | No | chip | ✓ |
| ble_firmware | KibbleSensor | GET /state → ble_firmware | — | — | DIAGNOSTIC | No | memory | ✓ |
| move_sensitivity | KibbleSettingSensor | GET /config → move_sensitivity | — | — | DIAGNOSTIC | No | run | ✓ |
| pet_sensitivity | KibbleSettingSensor | GET /config → pet_sensitivity | — | — | DIAGNOSTIC | No | cat | ✓ |
| eat_sensitivity | KibbleSettingSensor | GET /config → eat_sensitivity | — | — | DIAGNOSTIC | No | silverware-fork-knife | ✓ |
| detect_interval | KibbleSettingSensor | GET /config → detect_interval (seconds) | — | — | DIAGNOSTIC | No | timer-outline | ✓ |
| detect_range_from | KibbleSettingSensor | GET /config → detect_range_from | — | — | DIAGNOSTIC | No | clock-start | ✓ |
| detect_range_till | KibbleSettingSensor | GET /config → detect_range_till | — | — | DIAGNOSTIC | No | clock-end | ✓ |
| selected_sound | KibbleSettingSensor | GET /config → selected_sound | — | — | DIAGNOSTIC | No | music-note | ✓ |
| factor1 | KibbleSettingSensor | GET /config → factor1 | — | — | DIAGNOSTIC | No | scale-balance | ✓ |
| factor2 | KibbleSettingSensor | GET /config → factor2 | — | — | DIAGNOSTIC | No | scale-balance | ✓ |
| light_range_from | KibbleSettingSensor | GET /config → light_range_from | — | — | DIAGNOSTIC | No | clock-start | ✓ |
| light_range_till | KibbleSettingSensor | GET /config → light_range_till | — | — | DIAGNOSTIC | No | clock-end | ✓ |
| tone_range_from | KibbleSettingSensor | GET /config → tone_range_from | — | — | DIAGNOSTIC | No | clock-start | ✓ |
| tone_range_till | KibbleSettingSensor | GET /config → tone_range_till | — | — | DIAGNOSTIC | No | clock-end | ✓ |
| surplus_control | KibbleSettingSensor | GET /config → surplus_control | — | — | DIAGNOSTIC | No | food-off-outline | ✓ |
| surplus_standard | KibbleSettingSensor | GET /config → surplus_standard (%) | — | MEASUREMENT | DIAGNOSTIC | No | food-off-outline | ✓ |
| schedule | KibbleScheduleSensor | GET /schedule → entries.length | — | — | — | Yes | calendar-clock | ✓ |
| schedule_card_state | KibbleScheduleCardStateSensor | GET /schedule → packed regex (dispenser-card adapter) | — | — | — | Yes | calendar-text-outline | ✓ |
| cloud_connection | KibbleCloudConnectionSensor | GET /cloud → ESTABLISHED socket state | ENUM | — | DIAGNOSTIC | No | cloud-question-outline | ✓ |
| control_path | KibbleControlPathSensor | coordinator.control_path (set post-feed) | ENUM | — | DIAGNOSTIC | No | transit-connection-variant | ✓ |
| wifi | KibbleWifiNetworkSensor | GET /wifi → ssid | — | — | DIAGNOSTIC | No | wifi | ✓ |
| wifi_signal | KibbleWifiSignalSensor | GET /wifi → signal_dbm | SIGNAL_STRENGTH | MEASUREMENT | DIAGNOSTIC | No | wifi-strength-outline | ✓ |
| last_seen_pet | KibbleLastSeenPetSensor | GET /identify → cat (name or 'unknown') | — | — | — | Yes | cat | ✓ |
| identification_score | KibbleIdentificationScoreSensor | GET /identify → score (0.0-1.0) | — | MEASUREMENT | DIAGNOSTIC | No | percent-outline | ✓ |
| pending_faces | KibblePendingFacesSensor | coordinator.pending_face_count | — | MEASUREMENT | DIAGNOSTIC | No | image-multiple-outline | ✓ |
| clips | KibbleClipsSensor | GET /clips → array.length | — | MEASUREMENT | DIAGNOSTIC | No | folder-music-outline | ✓ |
| last_detection | KibbleLastDetectionSensor | GET /events → max(ts, seq) | TIMESTAMP | — | — | Yes | motion-sensor | ✓ |
| detections_today | KibbleDetectionsTodaySensor | GET /events → filtered by local date | — | MEASUREMENT | — | Yes | counter | ✓ |
| agent_starts | KibbleAgentStartsSensor | GET /state → kibbled_start_count | — | — | DIAGNOSTIC | No | restart | ✓ |

### binary_sensor.py (17 static + dynamic per-cat)

| Translation Key | Entity Class | Endpoint/Field | Device Class | Entity Category | Enabled by Default | Icon |
|---|---|---|---|---|---|---|
| feeding | KibbleFeedingSensor | GET /state → feeding | RUNNING | — | Yes | bowl-mix-outline |
| reachable | KibbleReachableBinarySensor | coordinator.feeder_reachable | CONNECTIVITY | DIAGNOSTIC | No | lan-connect |
| time_display | KibbleSettingBinarySensor | GET /config → time_display | — | DIAGNOSTIC | No | clock-outline |
| camera | KibbleSettingBinarySensor | GET /config → camera | — | DIAGNOSTIC | No | cctv |
| move_detection | KibbleSettingBinarySensor | GET /config → move_detection | — | DIAGNOSTIC | No | run |
| pet_detection | KibbleSettingBinarySensor | GET /config → pet_detection | — | DIAGNOSTIC | No | cat |
| eat_detection | KibbleSettingBinarySensor | GET /config → eat_detection | — | DIAGNOSTIC | No | silverware-fork-knife |
| feed_picture | KibbleSettingBinarySensor | GET /config → feed_picture | — | DIAGNOSTIC | No | camera-outline |
| eat_video | KibbleSettingBinarySensor | GET /config → eat_video | — | DIAGNOSTIC | No | video-outline |
| sound_enable | KibbleSettingBinarySensor | GET /config → sound_enable | — | DIAGNOSTIC | No | bullhorn-outline |
| system_sound_enable | KibbleSettingBinarySensor | GET /config → system_sound_enable | — | DIAGNOSTIC | No | account-voice |
| feed_sound | KibbleSettingBinarySensor | GET /config → feed_sound | — | DIAGNOSTIC | No | music-note-outline |
| food_warn | KibbleSettingBinarySensor | GET /config → food_warn | — | DIAGNOSTIC | No | alert-outline |
| light_mode | KibbleSettingBinarySensor | GET /config → light_mode | — | DIAGNOSTIC | No | led-variant-outline |
| tone_mode | KibbleSettingBinarySensor | GET /config → tone_mode | — | DIAGNOSTIC | No | volume-off |
| manual_lock | KibbleSettingBinarySensor | GET /config → manual_lock | — | DIAGNOSTIC | No | lock-outline |
| smart_frame | KibbleSettingBinarySensor | GET /config → smart_frame | — | DIAGNOSTIC | No | crop-free |
| cat_present (dynamic) | KibbleCatPresentBinarySensor | GET /identify: cat==name & ts within 15m | — | — | Yes | cat |

`vomit_detection` (binary_sensor, `GET /config → vomit_detection`) was implemented as a
diagnostic readout, later promoted to a writable `switch.py` control plus a separate
`vomit_detected`/`vomit_detected_at` diagnostic binary sensor, and removed entirely on
2026-09-18: `librefeed-media`'s RSS measured 36.7 MB (6.9 MB `MemAvailable`) with the feature
enabled vs. 26.5 MB (16.7 MB `MemAvailable`) disabled, on a 92 MB device.

**Per-cat presence derivation (binary_sensor.py:163-177, 271):**
- Created dynamically for each cat in GET /cats
- is_present() checks: identify.cat == cat_name AND (now - utc_from_timestamp(identify.ts)) < 15 min
- Source: coordinator.data.identify (from GET /identify)

### switch.py (4 entities)

| Translation Key | Entity Class | Endpoint/Field | Entity Category | Enabled by Default | Icon |
|---|---|---|---|---|---|
| night | KibbleSettingSwitch | POST /config → night (writable) | CONFIG | No | weather-night |
| light | KibbleSettingSwitch | POST /config → light (writable) | CONFIG | No | led-on |
| microphone | KibbleSettingSwitch | POST /config → microphone (writable) | CONFIG | No | microphone |
| cloud | KibbleCloudSwitch | POST /cloud → enabled (writable) | CONFIG | Yes | cloud-off-outline |

### number.py (3 entities)

| Translation Key | Entity Class | Endpoint/Field | Mode | Min | Max | Step | Category | Default | Icon |
|---|---|---|---|---|---|---|---|---|---|
| feed_amount | KibbleFeedAmount | HA RestoreEntity (local state) | BOX | 1 | 20 | 1 | — | 1 | counter |
| feed_amount_hopper_1 | KibbleFeedAmount | HA RestoreEntity (local state) | BOX | 1 | 20 | 1 | — | 1 | numeric-1-box-outline |
| feed_amount_hopper_2 | KibbleFeedAmount | HA RestoreEntity (local state) | BOX | 1 | 20 | 1 | — | 1 | numeric-2-box-outline |

### button.py (4 entities)

| Translation Key | Entity Class | Hopper | Amount Source | Icon |
|---|---|---|---|---|
| feed | KibbleFeedButton | both | number.feed_amount | bowl-mix |
| feed_hopper_1 | KibbleFeedButton | 1 | number.feed_amount_hopper_1 | numeric-1-circle |
| feed_hopper_2 | KibbleFeedButton | 2 | number.feed_amount_hopper_2 | numeric-2-circle |
| cancel_feed | KibbleCancelButton | — | — | stop-circle-outline |

### select.py (2 entities)

| Translation Key | Entity Class | Endpoint/Field | Default | Icon |
|---|---|---|---|---|
| wifi | KibbleWifiSelect | GET /wifi/scan + current SSID | Yes | access-point-network |
| label_face | KibbleLabelFaceSelect | GET /cats + reserved (Skip/Not a cat) | Yes | tag-outline |

### image.py (4 entities)

| Translation Key | Entity Class | Endpoint/Field | Default | Icon |
|---|---|---|---|---|
| pending_face | KibblePendingFaceImage | GET /faces/current (cache-bust: status+name) | Yes | image-search-outline |
| last_detection | KibbleLastDetectionImage | GET /events max, then GET /events/{image} | Yes | image-search-outline |
| dish_before | KibbleDishImage | GET /feeds latest before (H.264→JPEG ffmpeg) | Yes | image-outline |
| dish_after | KibbleDishImage | GET /feeds latest after (H.264→JPEG ffmpeg) | Yes | image-check-outline |

### camera.py (1 entity)

| Translation Key | Entity Class | Source | Features | Default | Icon |
|---|---|---|---|---|---|
| (device) | KibbleCamera | Scrypted URL or rtsp://host:8554/sub | STREAM | Yes | — |

### media_player.py (1 entity)

| Translation Key | Entity Class | Endpoint/Field | Device Class | Features | Default | Icon |
|---|---|---|---|---|---|---|
| speaker | KibbleSpeaker | POST /speak (raw PCM), GET /config volume (0-9→0.0-1.0) | SPEAKER | VOLUME_SET, PLAY_MEDIA, ANNOUNCE | Yes | speaker |

## 2. Services in services.yaml (17 total)

All routed through coordinator.py:

| Service | Handler | Endpoint | Key Fields |
|---|---|---|---|
| feed | async_feed() line 459 | POST /feed | hopper (1/2/both), amount (1-20), id |
| cancel_feed | async_cancel_feed() line 474 | POST /feed/cancel | — |
| schedule_set | async_set_schedule() line 479 | PUT /schedule | entries (list) |
| schedule_add | async_add_schedule() line 503 | POST /schedule/entry | time, hopper1_g, hopper2_g, enabled |
| schedule_remove | async_remove_schedule() line 516 | DELETE /schedule/entry | entry_id |
| schedule_set_enabled | async_set_schedule_enabled() line 521 | POST /schedule/entry/enabled | entry_id, enabled |
| schedule_card_add/edit/remove/toggle | async_schedule_card_action() line 526 | PUT /schedule (rebui...) | id, hour, minute, amount |
| wifi_connect | async_wifi_connect() line 549 | POST /wifi/connect | ssid, password (optional) |
| label_face | async_label_face() line 565 | POST /faces/label | crop_id, cat |
| add_cat | async_add_cat() line 570 | POST /cats | name |
| identify | async_identify() line 508 | GET /identify (refresh) | (triggers re-eval + poll) |
| save_clip | async_save_clip() line 575 | PUT /clips/<name> | media_content_id (resolved to URL) |
| record_clip | async_record_clip() line 585 | RTSP→ffmpeg→POST /speak→PUT /clips | name, seconds (1-30) |
| play_clip | async_play_clip() line 593 | POST /clips/<name>/play | name |

## 3. Consumption of events[], identify, cats, state

### DetectionEvent[] (GET /events → coordinator.py:281 as tuple)

**sensor.py (line 27, 588-670):**
- Line 588: _latest_detection() → max by (ts, seq)
- Line 615-626: KibbleLastDetectionSensor → state=timestamp; attrs: class, image, cat, score, pet_id
- Line 649-655: KibbleDetectionsTodaySensor._today() → filter by local date
- Line 653, 670: Count & group by class; capped flag if ≥50

**image.py (line 18, 124-144):**
- Line 124-128: KibbleLastDetectionImage._apply() → newest by (ts, seq) → GET /events/{image} URL
- Line 141: Poll update → refetch if image changed

### IdentifyResult (GET /identify → coordinator.py:276)

**binary_sensor.py (line 163, 167, 271):**
- Line 163: is_present() free function → cat == name AND (now - ts) < 15 min
- Line 167: _add_new_cats() → watch GET /cats, create per-cat binary_sensors
- Line 271: KibbleCatPresentBinarySensor.is_on → calls is_present()

**sensor.py (line 512-527, 545):**
- Line 512: KibbleLastSeenPetSensor.native_value → identify.cat
- Line 516-527: Attrs: source, score, second_best_cat/score, last_identified (iso ts)
- Line 545: KibbleIdentificationScoreSensor.native_value → identify.score

### CatInfo[] (GET /cats → coordinator.py:275 as tuple)

**binary_sensor.py (line 166-177):**
- Async callback watches coordinator.data.cats, creates new KibbleCatPresentBinarySensor for each

**select.py (line 115):**
- KibbleLabelFaceSelect.options → [cat.name for cat in coordinator.data.cats] + reserved labels

### FeederState (GET /state → coordinator.py:268 as single FeederState)

**sensor.py (line 43-68, 696):**
- Line 43-68: KibbleSensor tuple reads bowl_fill[0/1], desiccant_days, firmware, ble_firmware
- Line 696: KibbleAgentStartsSensor → state.agent_starts

**binary_sensor.py (line 193):**
- KibbleFeedingSensor.is_on → state.feeding

**button.py (line 81):**
- KibbleFeedButton._amount() → uses state.serial for entity_id lookup

## 4. Sync Gap Analysis: NONE FOUND ✓

**Scope:** strings.json, en.json, icons.json, services.yaml

- **51 entity keys (static):** All present in strings.json entity sections; en.json is identical; icons.json has default + state icons
- **Dynamic cat_present:** Uses translation_key="cat_present" with {cat_name} placeholder
- **17 services:** All named in services.yaml; all have strings.json entries; all have icon entries in icons.json
- **Icon coverage:** Every entity type has ≥1 icon (default and/or state-based variants for binary_sensor/sensor enums)

## 5. Pet ID → Cat Name Mapping: Storage Location

### Current State
No mapping exists. Cats enrolled via crop labeling (`label_face` → `POST /faces/label`). Agent holds cat list but no reverse pet_id→name.

### Recommended Implementation: Hybrid Approach

**Best location:** Agent-side `/opt/kibble/pet_id_map.json` (source of truth) + HA sync

1. **Agent stores:** `{"vendor_pet_1": "fluffy", "vendor_pet_2": "whiskers"}`
2. **HA fetches:** New endpoint GET /pet_id_map on each poll (coordinator.py:_fetch_all) or post-label_face
3. **HA sensor:** sensor.kibble_pet_id_mapping (JSON state + per-id attributes)
4. **Service hook:** label_face triggers agent map update + HA refresh

**Why:** Agent owns cat gallery; HA should sync, not duplicate. Keeps on-device logs correct.

**Alternative if HA owns it:** Store in .storage/kibble_pet_mapping as JSON; provide service to set mapping manually if agent's guess is wrong.', 'details': {'resolvedPath': '/data/agent/sessions/-Homelabber/2026-09-16T02-18-13-875Z_01a0a801-edf3-762e-ab39-5f876ab78801/HaInventory.json', 'contentType': 'text/markdown', 'meta': {'source': {'type': 'internal', 'value': 'agent://HaInventory?q=.report'}}}}
