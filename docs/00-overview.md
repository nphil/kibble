# Petkit YumShare Dual-hopper 2 (D4SH2 / D4H2) — device study index

Goal: complete understanding of the device so a FIRST-PARTY on-device agent + Home Assistant
integration can expose every app control directly. No cloud impersonation (no DNS hijack, forged
CA, or fake Petkit broker).

Everything here was produced OFFLINE from the verified stock backup in ../petkit-d4sh2-backup/
(see its RESTORE.md). The live device has only ever been read, never written.

| Document | What it covers |
|---|---|
| STUDY-soc.md    | The SoC (Axera AX620A), public BSP/SDK, toolchain, media pipeline, boot facts |
| STUDY-boot.md   | uImage headers, partition/UBI layout, boot chain, OTA/update scripts, A/B logic, watchdog, kernel modules, SDK libs |
| STUDY-app.md    | Binaries + imports, IPC map, THE CONTROL SEAM, config schema, encryption, cloud protocol, T31 UART, BLE, pktool, media/alg/audio, Agora, live checks |
| INVENTORY.md    | Every file in app/soc/alg/audio with type and size |
| PRIOR-ART.md    | FCC, Localkit, pypetkitapi, HA integrations, fountain BLE RE (with corrections) |
| fs/{app,soc,alg,audio}/ | Extracted squashfs trees. fs/initramfs/ is absent: kernel/uboot/spl partitions are encrypted; read the ramdisk LIVE instead |

## Architecture map (one screen)

    SPI NAND 128MiB: spl | ddrinit | uboot(A/B) | env(A/B) | kernel(A/B, ENCRYPTED, contains ramdisk root) | param(UBI) | rootfs(UBI: /bak,/opt)
    /opt/{app,soc,alg,audio}.img  = 64-byte uImage header + squashfs, CRC32/md5 only, no signatures  -> loop-mounted /app /soc /alg /audio
    /app/script/app_start.sh launches:  watchdog  ble  media  ctrl  agora  cloud  logUpload

    Shared state:  /config_shm  (POSIX shm holding one config_t struct, mmap-ed by ALL nine binaries; flock /tmp/config.lock)
    Message bus:   per-process POSIX mqueues, envelope {msg_id, src, dst, msg_len}, ~100 named dispatch_handler_* callbacks
                   ctrl --dispatch_handler_feed--> pk_ctrl_send_feed_event_msg --> ble: dispatch_handler_ble_feed_ctrl --> UART /dev/ttyS3 --> T31 MCU (motors, hall sensors, hopper level, battery)
    Cloud edge:    ctrl (Alibaba IoT MQTT, HMAC-SHA256, port 33882) + cloud (HTTP /dev_* endpoints) + agora (video relay) + logUpload
    Trust anchor:  /app/bin/ca.crt = Entrust Root CA (expires 2026-11-27)
    Secrets:       /opt/dev.conf, /opt/user.conf = md5 + AES-256 ciphertext (key not recoverable statically)
    Supervisor:    watchdog watches agora ble card cloud ctrl media p2p; restart -> kill -> reboot; idle periodic reboot

## The seam (conclusion)
Replace ctrl + cloud (+ agora, logUpload). Keep ble, media, alg, watchdog untouched. A first-party agent
that speaks the mqueue envelope reaches every dispatch_handler_* in ble/media — the full vendor control
surface — without reimplementing UART framing or the ISP pipeline. Blockers: the numeric msg_id values
and the exact per-process mqueue names are compiled constants, not strings; both are cheap to recover
LIVE (ls /dev/mqueue, read the dispatch table from the running process). See STUDY-app.md §3 and §12.

## Next phase: live observation (read-only), in safety order
1. ls /dev/mqueue /dev/shm; ps; per-pid fds and maps               (pure reads)
2. cat the ramdisk: /linuxrc, /etc/init.d/*, /etc/inittab            (pure reads; not in the backup)
3. pktool get_config_info / get_rtc / get_gpio_value / get_pwm_status (documented read-only subcommands)
4. Recover msg_id table from ctrl/ble rodata via /proc/<pid>/maps + mem (read-only)
5. NOT yet: LD_PRELOAD shims, cat /dev/ttyS3 (steals bytes from ble), any PT_/set_ pktool command, killing processes (watchdog reboots)

## Agent design constraints (Nitin, 2026-09-15: "performant and lightweight" is a hard requirement)
Measured live budget: 2 CPU cores online, 92.9 MB RAM, ~29 MB available, no swap, loadavg ~7.4 (kernel media threads), /opt 57 MB free.
Processes to retire and their RSS: agora 13.7 MB, ctrl 7.3 MB, cloud 4.6 MB, logUpload 4.1 MB  (= ~30 MB freed).
Reference footprint of the drivers we keep: ble 2.7 MB / 3 threads, watchdog 2.5 MB / 3 threads, media 23.7 MB / 26 threads.

Targets for the first-party agent:
- <= 5 MB RSS, single process, <= 4 threads, ~0% CPU idle, no periodic polling loops where an event source exists
- Language: Rust (static, musl or the Axera glibc toolchain) or C. NOT Go/Python/Node (runtime footprint alone exceeds the budget).
- Bus: blocking mq_receive on our own inbox; commands are direct mq_send envelopes to ble/media inboxes (no shell, no pktool).
- State: mmap /dev/shm/config_shm read-only, diff at a low rate (1-2 Hz) or on bus events; never copy the whole struct per read.
- Camera: attach as an extra reader to media's existing H.264 ring (/dev/shm/media_buffer_frame_buf + sem.media_buffer_reader_N); RTSP passthrough, zero re-encode, no ISP ownership.
- Transport to HA: one HTTP listener + WebSocket push (JSON), mDNS advertisement; no MQTT.
- Persistence: /opt/app_init.sh only; stock app.img untouched; must satisfy or replace watchdog (contract from STUDY-dispatch.md).

## Study phase results (closed 2026-09-15) — the facts the Kibble agent is built on
Project name: **Kibble** (HA domain `kibble`, agent `kibbled`). Additional docs since the first index: STUDY-live.md, STUDY-dispatch.md,
STUDY-msgids.md, STUDY-config.md (+config_layout.json), STUDY-mcu.md, STUDY-ble.md, STUDY-bt-linux.md, STUDY-alg.md, LOCALKIT-HARVEST.md,
RESEARCH-npu-scout.md, PETKIT_BLE_RESEARCH_ONLINE.md (+3 quick refs), live/ (config_shm.bin SECRET, ble.img, 60 s shm series).

- SoC: Axera **AX620Q** (AX620E family; Cortex-A53 x2 in AArch32; 256 MiB SiP, 96 MB to Linux + 160 MB CMM). Pulsar2 target AX620E. AX Engine V3.0.0.
- Bus: POSIX mqueues `/msg_dispatch_N` (1 ctrl, 2 media, 4 cloud, 5 watchdog, 7 agora, 8 ble, 10 logUpload); envelope 16 B {u32 msg_id, i32 src, i32 dst, u32 len}; mq 128 x 544.
- **Feed = msg_id 0x6004 to dst 8**, 67-byte payload (ctrl 0x44f98 -> ble dispatch_handler_ble_feed_ctrl -> UART CMD 0x0A). Other ids: 0x100f feed-in, 0x100a recv_ble_data, 0x101a ble_get_schedule, 0x1009 ble_key_change_wifi, 0x1007 save_wifi_conf, 0x1010 dev_state_report.
- Watchdog: per-process u32 alive counters in config_shm — 10284 media / 10288 ctrl / 10296 agora / 10300 cloud / 10304 ble / 10312 logUpload, each followed by the owner's pid at +0x20 (corrected 2026-09-16; the earlier "1-byte toggles, 10296=ctrl" reading was wrong — see 06-msgids.md errata). Stale ~60 s -> restart, or for ctrl past 30 min uptime -> `reboot -f`.
- config_t: 11952 B, usr(4664) | dev(228) | state(7060); 54 offsets mapped, 228 field names; telemetry offsets still need a feed-cycle diff.
- MCU: Telink TLSR82xx (TC32), UART 5A A5 | LEN | CMD | SEQ | FLAGS | payload | CRC16-CCITT; 28-entry command table; OTA image live/ble.img.
- BLE: on the MCU only (RTL8733BU BT is dark and unwirable). GATT 0xAAA0/AAA1/AAA2 (same as Petkit fountains) + Telink OTA service. The MCU is a byte pipe:
  BLE app data -> UART 0x11 -> ble -> bus 0x100a -> ctrl. Stock ctrl maps BLE only to Wi-Fi-provisioning/schedule-read/OTA — **no feed over BLE on stock**;
  Kibble's ctrl replacement can add it (same 0x6004 path). Works with Wi-Fi down while Linux is up; not with Linux off.
- AI: embeddings 512 f32, gallery /opt/feature.bin (v0.0.3, CRC); raw embedding not exported -> run feat model in our own engine handle. NPU multi-process: VNPU exists on AX620E, unverified live.
- Persistence hooks: /opt/system_init.sh (rcS) and /opt/app_init.sh (system_init.sh); stock app.img untouched; telnetd precedes both.
- Publish plan: clean repo `kibble` with own branding; migrate docs with secrets scrubbed and vendor binaries excluded; then delete this study folder (keep ../petkit-d4sh2-backup).
