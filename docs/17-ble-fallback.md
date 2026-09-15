# BLE fallback: what it can be, and why it is deferred

The appeal is obvious: if the router is down, reach the feeder over Bluetooth from a nearby ESPHome
proxy instead of the LAN. This document records what was measured and the resulting design
decision, so nobody re-litigates it from first principles.

## 1. The radio

There is exactly one usable BLE radio, and it is not on the Linux side. The Wi-Fi module's
Bluetooth is unreachable — not on USB, its UART is not routed, the kernel has no Bluetooth stack,
and the boot images are encrypted so one cannot be added ([10-bt-linux.md](10-bt-linux.md)).
BLE lives on the Telink dispenser MCU, which owns the GATT profile (`0xAAA0/AAA1/AAA2`, the same
one Petkit's fountains use) ([09-ble.md](09-ble.md)).

## 2. The MCU interprets nothing

The MCU is a byte pipe. Data received from a BLE peer is forwarded verbatim to Linux
(`UART CMD 0x11` → `ble` → bus `msg_id 0x100a` → `ctrl`); no BLE command is serviced on the MCU
itself. Stock `ctrl` maps those bytes to exactly four things: Wi-Fi credential change/recovery
(`0x1009`), Wi-Fi config persist (`0x1007`), schedule read (`0x101a`), version check and OTA end
(`0x1015`, `0x1018`). **There is no feed-over-BLE in stock firmware** — verified by exhaustively
enumerating the five BLE-sourced `type` values `dispatch_handler_recv_ble_data` recognises; none
branches toward the feed handler.

Consequence: BLE control is only possible for a process that *replaces* `ctrl`. It is not something
an agent running beside `ctrl` can add, because `ctrl` is the one receiving the forwarded bytes.

## 3. The feeder does not advertise during normal operation

Measured 2026-09-15 against the live Home Assistant Bluetooth stack — seven ESPHome proxies,
including one in the **same room** as the feeder:

| check | result |
|---|---|
| Advertisement records retained across all 7 proxies | 188 |
| Devices seen by the plant-room proxy (same room) | 23, with names — e.g. `GVH5075_9956` at −61 dBm |
| Occurrences of `Petkit` / `D4SH` in the stored advertisement data | **0** |
| Occurrences of service UUID `aaa0` / `aaa1` / `aaa2` | **0** |
| Devices with the feeder's Wi-Fi OUI `94:BA:06` | **0** |

The proxy is clearly working and clearly close enough (it hears a −61 dBm neighbour). The feeder is
simply silent: the MCU advertises for provisioning, not continuously. So there is nothing for a
proxy to connect *to* while the feeder is happily on Wi-Fi.

Note that the Telink OUI (`A4:C1:38`) also belongs to other devices in the house, so OUI alone
cannot identify the feeder — the service-UUID and name checks above are the load-bearing evidence.

## 4. Design decision

BLE fallback is **deferred by construction**, not abandoned. It needs three things, in order:

1. Kibble replaces `ctrl`, so it receives the forwarded BLE bytes (`0x100a`) and can map them to a
   feed (`0x6004`) — the code path is already proven for LAN feeds.
2. Kibble asks the MCU to advertise. The lever exists on the Linux side (`pktool` has a `bleadv`
   subcommand, and `ble` has relay/advertising commands); which message id drives it, and whether
   advertising can be left on continuously without hurting Wi-Fi coexistence on a shared antenna,
   is not yet established.
3. An ESPHome-proxy-side client that speaks the Petkit frame format over the `0xAAA1/AAA2`
   characteristics.

Until step 1 lands, a BLE fallback would be a fallback to nothing.

## 5. What already survives a network outage

Worth being clear, because it is the actual robustness story and it needs no BLE at all: the
**schedule lives on the MCU**, with its own RTC, and it keeps dispensing on time even if Wi-Fi, the
router, Home Assistant, and the whole Linux side are down. What an outage costs you is *ad-hoc*
control and telemetry, not your cats' meals.
