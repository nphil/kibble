# kibbled — design

`kibbled` is the on-device half of Kibble. It runs on the feeder's own Linux (Axera AX620Q,
2× Cortex-A53, ~29 MB of free RAM) next to the stock Petkit firmware and gives Home Assistant a
local API. Nothing in the stock flash is modified; the vendor's `ble`, `media`, `alg` and
`watchdog` processes keep doing exactly what they do today.

## Why this seam

The vendor firmware is a handful of processes wired together by POSIX message queues, one per
process (`/msg_dispatch_<id>`), plus a shared-memory config (`/dev/shm/config_shm`). Everything the
phone app can do arrives at `ctrl` as JSON from the cloud and leaves `ctrl` as a small binary
message to a peer. That bus is the narrowest, most stable seam in the system:

* it is inside the device, so no TLS, no certificate pinning, no cloud impersonation;
* the receivers (`ble`, `media`) are the same binaries whether the message came from `ctrl` or
  from us, so there is nothing to keep in sync with the vendor;
* the formats are tiny fixed structs, recovered from disassembly and **verified by dispensing food**.

### Wire format (verified)

```
mq_send(mqd, buf, 4 + len, prio 0)
buf: u16 msg_id | u16 src | payload[len]       len <= 540
```

`dst` selects the queue and never travels in the message. `src` is the sender's queue id; we stamp
`1` (ctrl) so that replies land where the vendor expects them and its own feed accounting stays
consistent.

### Feed (verified on both hoppers)

`msg_id 0x6004` to queue 8 (`ble`), payload 67 bytes:

| off | field   | note |
|----:|---------|------|
| 0   | cancel  | 0 dispense, 1 cancel |
| 1   | id[64]  | NUL-terminated record id |
| 65  | amount1 | hopper 1, portions |
| 66  | amount2 | hopper 2, portions |

Latency from `mq_send` to the local "feeding" flag: **24 ms**. To the device reporting the feed
to Petkit's cloud (while it still ran): 1.8 s.

## Budget and shape

| | target | measured (v0.1) |
|---|---|---|
| RSS | ≤ 5 MB | **148 KB** |
| threads | ≤ 4 | **1** |
| binary | | 422 KB static (musl) |
| idle CPU | ~0 % | blocking accept, no timers |
| dependencies | none | none (std + 4 libc externs) |

One accept loop, one request at a time, fixed buffers. Home Assistant is the only client and
polls at a few-second cadence; a framework would cost more RAM than the whole agent.

## Read path

`config_shm` is mapped read-only for `/state` reads. Reads are plain loads.

## Settings write path (v0.2)

Settings are the one place `kibbled` *does* write `config_shm` — under
`flock(/tmp/config.lock, LOCK_EX)`, exactly the discipline the vendor's own `config_save()` uses
(`docs/15-settings-write.md` §4), for the ~40 keys `docs/15-settings-write.md` §3 mapped to exact
offsets. It does **not** persist through the vendor's own `/opt/user.conf`: that file's content is
confirmed AES-encrypted with a key this repo never recovered (`docs/21-config-encryption.md`), so
hand-writing it would risk silently corrupting every other setting and credential in the file.
Instead `kibbled` keeps its own plaintext desired-state record (`agent/src/desired.rs`,
`/opt/kibble/settings.json`) and a background thread re-applies it — once at startup, gated on
`config_shm`'s own `loaded` flag, and continuously afterward — so a value the vendor path or the
(still-enabled) Petkit cloud sync reverts is corrected rather than silently lost. See
`agent/src/persist.rs` for the implementation and the project report for live durability
measurements.

## Watchdog

The stock watchdog checks five one-byte liveness toggles in `config_shm` (`ble`, `media`,
`ctrl`, `agora`, `cloud`). Today `kibbled` runs beside those processes and does not touch the
toggles. When it later replaces `ctrl`/`cloud`/`agora`, it flips their three bytes every 2 s and
the watchdog is none the wiser.

## API (v0.2)

```
GET  /state              {"serial","firmware","ble_firmware","volume","desiccant_days",
                          "feeding","bowl_fill":[h1|null,h2|null],"event_counter"}
GET  /config              every mapped setting's current value, flat {"key": value, ...}
POST /config              {"key": "volume", "value": 5} — verified-writable keys only, others 400
POST /feed                {"hopper":1|2|"both","amount":1..20,"id":"optional"}
POST /feed/cancel
GET  /cloud               {"enabled","last_error","routes","connections":[{"remote","state"}]}
                          -- Petkit-cloud kill switch status, see agent/src/cloud.rs
POST /cloud               {"enabled": bool} -- fails safe: a disable that can't prove LAN
                          reachability after blackholing rolls itself back and returns an
                          error rather than stranding the device
```

`bowl_fill` reads `null` while the vendor has the value invalidated mid-feed (`0xffffffff`).
`GET /state`'s own `"volume"` field predates the settings study and reads a different, unconfirmed
offset (4664) than the settings table's disassembly-proven one (3752, `docs/15-settings-write.md`
§3) — left as-is since fixing it is outside the settings feature's scope, but `GET /config`'s
`"volume"` is the value to trust.

## Persistence (designed, not yet installed)

`/soc/scripts/system_init.sh` (stock, read-only squashfs) mounts `/app` and then:

```sh
if [ -f /opt/app_init.sh ]; then /opt/app_init.sh & ; exit 0
elif [ -f /app/script/app_init.sh ]; then /app/script/app_init.sh & ; fi
```

`/opt` is the writable UBI volume that already holds the vendor's images, so a file there survives
reboots and (from the update script, which only replaces `/opt/*.img`) firmware updates. The hook:

```sh
#!/bin/sh
# /opt/app_init.sh — Kibble boot hook. Stock init runs exactly as before; kibbled starts after it.
/app/script/app_init.sh &
[ -f /opt/kibble/disabled ] && exit 0
( sleep 15; while :; do /opt/kibble/kibbled >/dev/null 2>&1; sleep 5; done ) &
```

Safety properties, in order of importance:

1. **telnetd is started by the ramdisk `rcS` before `system_init.sh`**, so a broken hook never costs
   access. Recovery is `rm /opt/app_init.sh; reboot`.
2. Stock init is invoked first and unconditionally; the hook adds, never replaces.
3. `touch /opt/kibble/disabled` turns Kibble off without removing it.
4. `kibbled` refuses to send bus messages unless `/app/bin/ctrl` matches a known build
   (md5 table in the binary): a vendor OTA that changes the message formats degrades Kibble to
   read-only instead of sending garbage to `ble`.
5. The restart loop is one `sh`; no cron, no init system to fight.

## Not yet

* schedule read/write (MCU-owned; `0x101a` reads it, write path still to recover)
* settings: read/write implemented for the ~37 keys `docs/15-settings-write.md` §3 resolved to an
  exact offset (4 keys' writes independently verified live — see the project report; the rest ship
  read-only). Still missing: the multi-range arrays' full stride/count beyond entry 0
  (`lightMultiRange`/`toneMultiRange`/`detectMultiRange`), `foodWarnRange` (base offset never
  pinned), and the sensitivity settings' derived-threshold-triple writes (`moveSensitivity` et al.
  write their own raw field but not the mapped triple `ctrl` also computes, so they ship read-only)
* events with images (attach as a reader of `media_buffer_frame_buf`)
* ONVIF + RTSP with G.711 backchannel for Scrypted (see scrypted-onboarding.md)
* BLE fallback (handle `0x100a` payloads once `kibbled` replaces `ctrl`)
