# How Kibble knows what it knows

These are the reverse-engineering notes the project is built on. They were produced by studying a
YumShare Dual 2 (`D4SH2`, firmware 895) — static analysis of the vendor binaries plus read-only
observation of the running device. Device identifiers, credentials and keys have been redacted.

Every claim in these documents carries a confidence rating and the evidence it came from: a file
offset, a disassembly address, or the live command whose output was seen. Where something could not
be recovered, that is stated rather than guessed — read those gaps as gaps.

## The short version

| | |
|---|---|
| SoC | Axera **AX620Q** (AX620E family), 2× Cortex-A53 in AArch32, Linux 4.19, glibc 2.25 |
| Memory | 256 MiB SiP: ~96 MB to Linux (~29 MB free), 160 MB reserved for the media/NPU pool |
| Storage | 128 MiB SPI NAND; kernel and bootloader are **encrypted**, app images are plain squashfs |
| Processes | `ctrl` (cloud + logic), `ble` (dispenser MCU + BLE), `media` (camera/audio), `alg` (AI), `agora` (streaming), `cloud`, `watchdog` |
| Bus | POSIX mqueues `/msg_dispatch_<id>`; message = `u16 msg_id \| u16 src \| payload` |
| Dispenser | Telink TLSR82xx MCU on a UART — owns the schedule, RTC, motor and **all** Bluetooth |
| Camera | 3 continuous hardware H.264 channels (1728×1080, 1152×720, 5 fps thumbnail) |

## Documents

**Architecture**
- [00-overview.md](00-overview.md) — start here: the map and the index
- [01-soc.md](01-soc.md) — the chip, memory layout, NPU
- [02-boot.md](02-boot.md) — boot chain, image format, update/verification, persistence hooks
- [03-app.md](03-app.md) — the userspace: every process, library, script and asset
- [04-live.md](04-live.md) — what the running device actually looks like

**Protocols**
- [05-bus.md](05-bus.md) — the internal message bus
- [06-msgids.md](06-msgids.md) — message ids and their handlers
- [07-config.md](07-config.md) — the shared-memory config block and its field offsets
- [08-mcu.md](08-mcu.md) — the dispenser MCU's UART protocol
- [09-ble.md](09-ble.md) — BLE: GATT profile, framing, and what the MCU does on its own
- [10-bt-linux.md](10-bt-linux.md) — why the Wi-Fi module's Bluetooth cannot be used
- [11-media.md](11-media.md) — encoder channels, audio in/out, the frame ring, talkback

**AI**
- [12-ai.md](12-ai.md) — the detection/identification pipeline and its models
- [13-npu-probe.md](13-npu-probe.md) — can a second process use the NPU? (inconclusive; see the review note)

**Results and design**
- [14-feed-test.md](14-feed-test.md) — the proof: a local message that dispenses food
- [design-entities.md](design-entities.md) — every app capability mapped to a Home Assistant entity
- [design-agent.md](design-agent.md) — `kibbled`'s design and budget
- [scrypted-onboarding.md](scrypted-onboarding.md) — what the camera must implement for Scrypted + HomeKit

**Appendices** — [inventory](appendix-inventory.md), [prior art](appendix-prior-art.md),
[Localkit's cloud protocol](appendix-localkit.md), [NPU research](appendix-npu-research.md),
[config layout](appendix-config-layout.json)
