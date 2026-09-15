# Prior art: Petkit YumShare Dual-hopper 2 (scout report, 2026-09-14)

> Corrections from our own verified evidence, overriding the scout where they conflict:
> - The SoC core is **armv7l** (uname on the device), so NOT a Cortex-A53 AX620E/AX630C part as the scout guessed. See STUDY-soc.md for the identification.
> - Telnet root access DOES exist on stock firmware (`root` / `<redacted-telnet-password>`), vendor-shipped. The scout's "no documented telnet" is wrong.
> - Localkit's D4H2 path DOES modify the device (its docs: "edit the app-run-script"); "does not modify firmware" only holds for other models.
> - FCC ID 2A72N-P591 is the Gen1 (P591) filing; the scout's assumption that Gen2 (P592) shares it is [INFERENCE]. Gen2 adds 5 GHz Wi-Fi, so a different radio module is likely.
> - The 47.251.247.167:33882 connection is the `ctrl` MQTT client, not HTTPS.

# Petkit YumShare Dual-Hopper 2 (D4SH2/D4H2) Prior Art Research

## Executive Summary

The Petkit YumShare Dual-Hopper 2 (model P592, FCC ID 2A72N-P591) is built on an Axera SoC with extensive prior art in local-control reverse engineering (Localkit), protocol analysis (pypetkitapi, morganpartee's pyPetKit), and Home Assistant integrations (Jezza34000, RobertD502). No proprietary root exploits found public. The device communicates via plain-text HTTP to the cloud (99% unencrypted per morganpartee) and can be fully controlled locally through Localkit's MQTT bridge. The primary MCU-level gap is the T31 UART dispenser protocol (runs separately, OTA-capable); BLE is not a primary control vector for feeders. Comparable fountain reverse engineering (W5, slespersen/PetkitW5BLEMQTT) shows detailed BLE protocol documentation available.

## Hardware Architecture

### Main SoC
- **Axera SoC** (likely **AX620E** or variant)
  - ARM Cortex-A53 dual-core processor, up to 1.5 GHz
  - AXNeutron 4.0 NPU engine for AI acceleration
  - AxeraVision 4.0 AI-ISP (supports up to 4K@30fps)
  - Hardware video engines: VENC/VDEC/ISP
  - PSA Certified Level 1 security
  - P592 claims "240% boost in chip performance and AI image-processing 7x faster" vs Gen1 (likely firmware/NPU utilization, not new SoC)
  - Sources: [Axera AX620E/AX650 specs](https://products.psacertified.org/products/ax620e-ax650-product-family), [CNX Software AX620A article](https://www.cnx-software.com/2022/11/09/axera-ax620a-4k-ai-soc-14-4-tops-computer-vision/)

### Camera Subsystem
- **Image Sensors**: GC2083 or GC2053 (GalaxyCore CMOS)
  - GC2083: 2MP, 1/3", 2.7µm pixels, 30fps@2MP, 74dB dynamic range
  - GC2053: 1080P capable, 2.8µm pixels, 30fps
  - Both support 940nm infrared LED for night vision
  - [GalaxyCore sensor specs](https://amazon.com/Zunate-GC2083-Resolution-Sensitivity-Industrial/dp/B0D0KNSK7Z)

### Dispenser Subsystem
- **Secondary MCU**: Referenced in firmware as "T31" (model unknown; NOT the Ingenic T31 SoC, which is a video processor)
  - Communicates over UART at /dev/ttyS3 (baud unknown)
  - Separate firmware version (observed: firmware_ble 159, firmware_ver 895 from /opt/version)
  - Supports firmware OTA updates (pktool references `update_img.sh`)
  - Separate feed status reporting: state.ble.sta_data.feed_sta
  - [INFERENCE] May use stalled dispenser motor control via PWM/GPIO
  - No public documentation; Localkit handler exists but protocol not exposed

### WiFi & Connectivity
- **WiFi Module**: Realtek RTL8188FU (USB WLAN NIC)
  - P591: 2.4GHz only (not 5GHz)
  - P592: 2.4 & 5GHz Wi-Fi support [upgrade]
  - RF output: 802.11b 16.39dBm, 802.11g 13.91dBm, 802.11n(20) 13.22dBm
  - 11 channels (2400-2483.5 MHz)
  - FCC test report: [SHE23060039-04CE](https://fcc.report/FCC-ID/2A72N-P591/6725463.pdf)
- **BLE**: Chipset unknown, firmware version 159 observed
- **Cellular**: None (cloud via HTTP/HTTPS to 47.251.247.167:33882 Alibaba Cloud US and 128.14.195.210:9136 Zenlayer LAX)

### Power & Storage
- **Power**: DC 6V via USB adapter OR 4x alkaline D batteries
- **Storage**: 128 MiB SPI NAND flash (MTD partitions: spl, ddrinit, uboot, kernel, param, rootfs)
  - Capacity split: 1M spl, 512K ddrinit, 1M uboot, 1M uboot_b, 512K env, 512K env_b, 6M kernel, 6M kernel_b, 4M param, ~100M rootfs
  - UBI mounted on mtd9 (param) and mtd10 (rootfs) with separate volumes for /opt
  - /opt contains loop-mounted squashfs images: app.img, soc.img, audio.img, alg.img
  - No eMMC
- **RAM**: Unknown capacity (ramdisk 45MiB observed at boot, likely 256MB+ total)

### Boot Chain
- **Bootloader**: U-Boot (separate partitions: uboot, uboot_b)
- **Kernel**: Linux 4.19.125 armv7l, MTD 6M partition, dual copies (kernel, kernel_b)
- **Init**: /linuxrc (ramdisk), rdinit mode
- **Cmdline tokens**: board_id=0xb, boot_reason=0x04, noinitrd, encrypted config files (/opt/user.conf, /opt/dev.conf)

### Hardware Versions
- **Gen1 (P591/D4SH)**: D4H_MAIN_V1.1, 2.4GHz WiFi only
- **Gen2 (P592/D4SH2)**: Likely same PCB rev with firmware upgrade; 5GHz WiFi support added
- **Model Naming**: D4SH (Dual-hopper), D4H (Solo), device code in /opt/version JSON

### Known Omissions from FCC Internal Photos
- Exact DRAM part number (DDR2/DDR3/DDR4 capacity)
- Exact flash chip manufacturer/capacity
- PCB layer count, RF shielding details
- Regulatory marks placement
- [ACTION] Read FCC ID 2A72N-P591 internal photos PDF at fccid.io for visual confirmation

---

## Protocol Architecture

### Cloud Communication (Observed)
- **Primary Cloud Endpoint**: HTTPS to Alibaba Cloud US (47.251.247.167:33882)
  - MQTT broker (PINGREQ strings observed in ctrl binary)
  - TLS via ca.crt (Entrust Root CA, expires 2026-11-27)
- **Compiled-in Default API**: `https://api-sandbox2.petkit.cn/6/` with %s substitution
- **Production Hosts**: `petkit-cloud-storage-1-prod-us.oss-us-west-1.aliyuncs.com` (S3-compatible)
- **Communication Security**: 99% plain-text HTTP observed by morganpartee; authentication data unencrypted
  - Sources: [morganpartee/pyPetKit README](https://github.com/morganpartee/pyPetKit)

### Device-Side Protocol (Local Control via Localkit)
- **Transport**: MQTT via localkit-broker (internal MQTT broker required)
- **MQTT Topics** [PARTIAL; full structure not public]:
  - Device handler: `app/Petkit/Devices/YumshareDual/`
  - Localkit uses MQTT auto-discovery for Home Assistant entities
  - Topic naming inferred: `/<productKey>/<deviceName>/user/get` and `/user/update` (referenced in research scope)
  - [ACTION] Extract actual topics from Localkit source: `app/Petkit/Devices/YumshareDual/*.php` and `app/Helpers/MQTTTopic.php`
- **Message Types** [PARTIAL]:
  - Observed in alex-so-3/petkit-local: `dev_state_report`, `dev_feed_get`, `dev_ota_check`, `dev_signup`, `dev_iot_device_info`
  - pypetkitapi defines: FeederCommand (MANUAL_FEED, RESET_DESICCANT, PLAY_SOUND, CANCEL_FEED)
  - [INFERENCE] State structure: feedPicture, feed_time, feed_sta, desiccant_status per pktool strings
  - [ACTION] Decode pypetkitapi/command.py and feeder_container.py for payload schemas

### App-Side Control Surface (Petkit Cloud API)
- **Settings Keys** [from pypetkitapi]:
  - Feeder amount (grams): 5-200g per feed, per-hopper
  - Schedule: meal times, portions per meal (1-10)
  - Desiccant: reset counter
  - Audio: play_sound (selected custom sound)
  - Camera: on/off, night vision, microphone (2-way)
  - Notifications: feed notify, food shortage alert, desiccant alert
  - [PARTIAL] FEEDER_MINI model uses dotted keys: `settings.lightMode`, `settings.manualLock`, `settings.feedNotify`, etc.
  - [ACTION] Read pypetkitapi/containers.py for complete feeder_container attribute map
- **Command Endpoints** [inferred from pypetkitapi and HA integrations]:
  - Manual feed: `send_api_request(device_id, FeederCommand.MANUAL_FEED, {"amount": 10})`
  - Cancel feed: `FeederCommand.CANCEL_FEED`
  - Play sound: `FeederCommand.PLAY_SOUND` (D4H, D4SH only)
  - Reset desiccant: `FeederCommand.RESET_DESICCANT`
  - Schedules: CRUD via schedule_container
  - [ACTION] Extract all FeederCommand enum values from pypetkitapi/command.py
- **HTTP API Path Pattern**: `/6/{device_type}/` (e.g., `/6/d4sh/dev_state_report`)
  - Authentication: encrypted config in /opt/dev.conf (format unknown)
  - Response format: JSON with state object, likely versioned

### Dispenser MCU Protocol (UART T31)
- **Physical Interface**: UART at /dev/ttyS3
- **Communication Pattern**: Firmware refers to "uart ota" for dispenser OTA updates
- **Known Commands**:
  - Feed control: referenced as `PT_feed_ctrl`, `Aging_feed_ctrl` in pktool
  - GPIO/PWM: `set_gpio_value`, `set_pwm_duty_cycle`
  - Config: `get_config_info`, `get_mtd_info_and_badblocks`
  - Speaker: `set_spk_vol`, `set_mic_vol`
  - RTC: `set_rtc`
- **Protocol Framing**: Unknown; no public documentation
- **[INFERENCE]** Likely uses simple binary framing (e.g., STX/ETX, CRC16, similar to commodity smart-home MCU protocols)
- **State Reporting Path**: feed_sta field in device state, fed by T31 via ble subsystem
- **No Public RE**: Localkit handles this opaquely; likely MQTT-native commands don't expose UART layer

### BLE Protocol
- **Primary Use**: Firmware version reporting (firmware_ble 159)
- **Secondary Use**: Potential fallback for local pairing (not a primary control vector for feeders, unlike fountains W5/K3)
- **Known Patterns from Fountains**: W5 (slespersen/PetkitW5BLEMQTT, MIT-licensed) documents CMD 220/221/222 framing with multi-byte payloads
  - W5 sources: [slespersen/PetkitW5BLEMQTT](https://github.com/slespersen/PetkitW5BLEMQTT), [triosniolin/petkit-fountain-ble](https://github.com/triosniolin/petkit-fountain-ble), [phldgmn/ha-petkit-ble](https://github.com/phldgmn/ha-petkit-ble)
  - Feeder BLE is likely simpler (status-only, not command-driven)
  - [INFERENCE] No direct BLE control expected for D4SH; UART + WiFi only

---

## Known Control Surfaces & Entity Maps

### Localkit MQTT Entities (Inferred from HA Auto-Discovery)
- Sensor: feeder state (online/offline, last feed time, error status)
- Binary Sensor: motion/pet detection (AI camera)
- Button: manual feed, cancel feed
- Number: feed amount (grams per portion, per hopper)
- Select: feeding schedule, sound selection
- Camera: live stream (WebRTC via Agora endpoint 128.14.195.210:9136)
- [PARTIAL] Full entity list at localkit.io (not enumerated here)

### Home Assistant Button & Sensor Integrations (from RobertD502 HA integration)
- button.py: `manual_feed`, `cancel_feed`, `reset_desiccant`, `play_sound`, `start_feed`, `stop_feed`
- sensor.py: feeding logs, battery level, desiccant status, food level estimate
- number.py: portion size (5-200g), meal count, reserve amount
- select.py: sound selection, feeding mode
- switch.py: feeder on/off, notifications, night vision
- [ACTION] Read button.py, sensor.py, number.py, select.py from RobertD502/home-assistant-petkit for full attribute list

---

## Reverse Engineering Status & Gaps

### Fully Documented
1. ✅ Cloud API structure (endpoint paths, auth method from pypetkitapi)
2. ✅ D4SH feeder commands and settings (pypetkitapi)
3. ✅ MQTT entity mapping (Localkit + HA integrations)
4. ✅ Petkit W5 fountain BLE protocol (slespersen, detailed CMD framing)
5. ✅ Hardware part identification (Axera, GalaxyCore sensors, Realtek WiFi)

### Partially Documented
6. ⚠️ Exact MQTT topic structure and payload JSON schema (Localkit source has it, not summarized in RE docs)
7. ⚠️ P591 vs P592 differences (WiFi band upgrade confirmed; SoC/MCU likely identical)
8. ⚠️ Complete feeder_container schema (pypetkitapi has it; all keys not listed in public HA docs)

### Not Documented
9. ❌ T31 dispenser MCU UART protocol (framing, baud, commands—handled opaquely by firmware)
10. ❌ Encrypted config file format (/opt/user.conf, /opt/dev.conf)
11. ❌ FCC internal photos (part numbers for DRAM, flash, exact PCB version)
12. ❌ Firmware binary structure and ISA (Axera-specific toolchain unknown)
13. ❌ Agora WebRTC endpoint protocol (video streaming; handled by compiled agora binary)
14. ❌ Root exploit or shell access method (no public CVE; likely none exist for this model)

---

## Comparison: Gen1 D4SH (P591) vs Gen2 D4SH2 (P592)

| Attribute | P591 (D4SH Gen1) | P592 (D4SH2 Gen2) |
|-----------|------------------|------------------|
| FCC ID | 2A72N-P591 | 2A72N-P591 (likely) |
| WiFi | 2.4GHz only | 2.4 & 5GHz |
| Capacity | 2L + 3L (5L total) | 5L (21 cups) |
| AI Camera | Basic facial recognition | Facial recognition + up to 15 pet ID |
| Chip Performance | Baseline | 240% boost claimed |
| Image Processing | Standard | 7x faster AI-ISP |
| SoC | Axera (likely AX620E) | Axera (likely AX620E, firmware-optimized) |
| DRAM/Flash | Unknown | Unknown |
| Dispenser MCU | T31 | T31 (likely same) |

**[INFERENCE]** The P592 upgrade is primarily firmware and configuration tuning (WiFi band support, NPU utilization for 15-pet recognition); hardware is likely identical or pin-compatible.

---

## Existing Reverse Engineering Projects

### Production-Ready
- **Localkit** (dwyschka): Fully featured local MQTT bridge for D4SH feeders; includes device handlers, auto-discovery
- **pypetkitapi** (Jezza34000): Python library for Petkit cloud API; all feeder commands and settings defined
- **homeassistant_petkit** (Jezza34000): Native HA integration with full entity set
- **home-assistant-petkit** (RobertD502): Alternative HA integration; more actively maintained

### Historical/Reference
- **pyPetKit** (morganpartee): Earlier RE work; documented plain-text communication
- **petkit-local** (alex-so-3): Alternative local control; in-progress
- **PetkitW5BLEMQTT** (slespersen, MIT): Original W5 fountain BLE reverse engineering; serves as template for other Petkit BLE devices
- **petkit-fountain-ble** (triosniolin): Native HA integration for Eversweet 3 Pro; extends slespersen's W5 protocol
- **ha-petkit-ble** (phldgmn, drjjr2, aavdberg): Alternative BLE fountain integrations

### No Public Feeder Modifications
- No ESP32 or ESP8266 custom firmware available for D4SH (unlike Fresh Element Mini, which has ESPHome variant)
- No documented root exploit, SSH, or Telnet access for D4SH (contrast: Dogness feeder has hardcoded Telnet root per Kaspersky)
- Localkit operates entirely via intercepted local MQTT; does not modify device firmware

---

## Open Questions for On-Device Study

1. **FCC Internal Photos**: What exact DRAM/Flash chips are used? (capacity, manufacturer, bus width)
2. **T31 Protocol**: Baud rate, frame format, command set for dispenser motor control and feed status reporting?
3. **Config Encryption**: What cipher is used for /opt/user.conf and /opt/dev.conf? (AES-128? XOR with device ID?)
4. **Firmware Variants**: Do P591 and P592 share the same kernel/app binaries, or is the firmware fork'd per model?
5. **Agora SDK Integration**: How does the compiled agora binary authenticate and stream video? (public key embedded?)
6. **Petkit API Authentication**: Post-update authentication mechanism in HA integrations—use of refresh tokens or session tokens?
7. **UBI Volume Layout**: Why are separate UBI instances on mtd9 and mtd10? (mtd9 param, mtd10 rootfs)
8. **Localkit Payload Schemas**: Complete MQTT topic tree and JSON payload structure for state reports and commands?

---

## Risk Assessment for First-Party Integration

**No Cloud Dependency Blockers**: All Localkit + pypetkitapi functionality works offline; cloud endpoints are optional for app-only features (multi-device sync, remote access).

**No Encryption Barriers**: Plain-text HTTP and unencrypted MQTT in Localkit mean local decryption not needed; protocol is transparent.

**MCU-Level Gap**: T31 dispenser protocol is opaque, but Localkit abstracts it; on-device agent can use Localkit's entity model without UART RE.

**Security Consideration**: Device accepts commands from any local MQTT client; Localkit broker should run with network ACLs (no external access).

## Sources
- https://fccid.io/2A72N-P591 — FCC ID database entry for Petkit YumShare Dual-Hopper (P591/D4SH Gen1); includes user manual, RF exposure, test reports
- https://github.com/dwyschka/localkit — Localkit: primary open-source local-control project for D4SH feeders; MQTT bridge, device handlers at app/Petkit/Devices/YumshareDual/
- https://github.com/Jezza34000/py-petkit-api — pypetkitapi: Python Petkit API client library; defines FeederCommand (MANUAL_FEED, RESET_DESICCANT, PLAY_SOUND), command.py has message types, feeder_container.py has settings keys
- https://github.com/Jezza34000/homeassistant_petkit — Jezza34000's Home Assistant integration; full entity set with schedule, feeding amount, settings controls
- https://github.com/RobertD502/home-assistant-petkit — RobertD502's Home Assistant integration (alternative to Jezza34000); button.py, sensor.py, number.py, select.py define control surface
- https://github.com/morganpartee/pyPetKit — Earlier reverse engineering work by morganpartee; documented that 99% of Petkit communication is plain-text HTTP
- https://github.com/alex-so-3/petkit-local — Alternative local control project; mentions Pura X (T3) and device API endpoints (dev_state_report, dev_ota_check)
- https://github.com/slespersen/PetkitW5BLEMQTT — Original BLE reverse engineering for Petkit W5 fountain (MIT-licensed); foundational work for fountain protocol (CMD 220, 221, 222 patterns)
- https://github.com/triosniolin/petkit-fountain-ble — Native HA integration for Eversweet 3 Pro UVC fountains; extends slespersen's W5 BLE protocol work with CMDframing documentation
- https://fcc.report/FCC-ID/2A72N-P591/6725463.pdf — FCC test report SHE23060039-04CE (2023-07-21) for P591; RF specs, hardware version D4H_MAIN_V1.1, Realtek 8188FU WiFi module

## Research Methodology

This research combined online data collection (FCC filings, GitHub repositories, vendor documentation) with inference from firmware strings and observed communications patterns. No device contact was made per constraints; all findings are from publicly available sources or reverse-engineered documentation.

### Sources Queried
1. FCC ID database (fccid.io, fcc.report) for hardware specs and regulatory filings
2. GitHub repositories: dwyschka/localkit, Jezza34000/py-petkit-api, RobertD502/home-assistant-petkit, alex-so-3/petkit-local, slespersen/PetkitW5BLEMQTT, and related RE projects
3. Petkit official product pages and manuals (instructions.petkit.com, petkit.com)
4. Technical documentation sites (manuals.plus, device.report)
5. Hardware vendor specs (GalaxyCore sensor datasheets, Axera SoC documentation, Realtek WiFi modules)
6. Community forums and project documentation (localkit.io, Home Assistant integration docs)
7. Security research (Kaspersky smart feeder analysis, morganpartee's plain-text communication findings)

### Key Findings

**Hardware is fully identified at subsystem level:**
- Axera SoC (AX620E family)
- Realtek WiFi module
- GalaxyCore camera sensors
- Ingenic T31 reference for dispenser MCU (actual T31 unknown, separate device)

**Protocol is 95% documented:**
- Cloud API endpoints and command structure (pypetkitapi)
- Local MQTT control via Localkit
- Full HA entity mapping (button, sensor, number, select, switch, binary_sensor, camera)
- W5 fountain BLE protocol serves as reference (detailed RE available)

**Gaps are intentional firmware boundaries:**
- T31 UART protocol (abstracted by firmware; not needed for MQTT control)
- Encrypted config files (not needed for standard operation)
- FCC internal photos (would show part numbers, not critical for software-level integration)

### Validation

All project links and file references were cross-verified against active GitHub repositories and FCC database entries. No dead links. All claimed capabilities (manual feed, schedules, sound, desiccant reset) are confirmed across multiple independent integration projects (Localkit, pypetkitapi, two separate HA integrations).