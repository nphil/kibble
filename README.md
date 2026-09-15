<img src="brand/logo.png" alt="Kibble" height="96">

**Local control for Petkit YumShare Dual feeders — no cloud, no MQTT discovery, no impersonation.**

Kibble is two pieces:

- **`kibbled`** — a small Rust agent that runs *on the feeder itself*, alongside the stock
  firmware, and speaks the firmware's own internal message bus.
- **`kibble`** — a Home Assistant custom integration (HACS-installable) that talks to that agent.

Nothing is flashed. The vendor's `ble`, `media`, `alg` and `watchdog` processes are left alone, and
the stock filesystem images are never modified — Kibble is one file in the writable `/opt` volume
plus a boot hook that calls the stock init first.

## Why it works this way

The feeder's firmware is a set of processes wired together by POSIX message queues, one per
process, plus a shared-memory config block. Everything the phone app can do arrives from Petkit's
cloud as JSON and leaves as a small binary message on that bus. Kibble sends those same messages.

That seam means no TLS to break, no certificate to forge, no fake broker to run, and nothing to
keep in sync with the vendor: the process that receives our "dispense" message is the same vendor
binary that would have received the cloud's.

```
Home Assistant ──HTTP──> kibbled ──mqueue──> ble ──UART──> dispenser MCU ──> motor
                            │
                            └── mmap(/dev/shm/config_shm, read-only) ──> live state
```

Measured on the device: **24 ms** from HTTP request to the feeder's own "feeding" flag going high.
The agent uses **280 KB of RSS**, one thread, and no external dependencies.

## Status

| | |
|---|---|
| Dispense (per auger, amount, cancel) | working, verified by dispensing |
| Live state (feeding, bowl fill, desiccant, firmware) | working |
| Camera / two-way audio via Scrypted | designed, not implemented ([docs](docs/scrypted-onboarding.md)) |
| Schedule, settings, events, cat identification | see [docs/design-entities.md](docs/design-entities.md) |

See [docs/](docs/) for the reverse-engineering notes this is built on, including the exact wire
formats and the addresses they were recovered from.

## Install

**Agent** (on the feeder, which must be reachable over telnet):

```sh
mkdir -p /opt/kibble
# copy the armv7 musl static binary to /opt/kibble/kibbled, chmod +x, then:
cat > /opt/app_init.sh <<'EOF'
#!/bin/sh
/app/script/app_init.sh &
[ -f /opt/kibble/disabled ] && exit 0
[ -x /opt/kibble/kibbled ] || exit 0
( sleep 20; while :; do /opt/kibble/kibbled >/dev/null 2>&1; sleep 5; done ) &
exit 0
EOF
chmod +x /opt/app_init.sh
```

`touch /opt/kibble/disabled` stops Kibble without removing it. `rm /opt/app_init.sh` restores
completely stock boot — `telnetd` is started by `/etc/init.d/S50telnet`, before and independently
of this hook, so the recovery path cannot be locked out by it.

**Integration**: add this repository to HACS as a custom repository (type: Integration), install,
restart Home Assistant, then add **Kibble** and enter the feeder's address.

## Build

```sh
cd agent
cargo test
cargo build --release --target armv7-unknown-linux-musleabihf
```

## Compatibility

Developed against a YumShare Dual 2 (`D4SH2`), firmware 895, Axera AX620Q. The message formats are
firmware-specific; other models in the same family (`D4H2`) use a slightly different feed payload
(`amount` instead of `amount1`/`amount2`) which is documented but untested.

## Licence

MIT.
