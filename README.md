<img src="custom_components/kibble/brand/icon.png" width="96" align="right" alt="">

# Kibble

**Local control for Petkit YumShare Dual cat feeders from Home Assistant. No cloud account, no phone app, no MQTT.**

Kibble is a Home Assistant integration. It talks over your home network to a small program that
runs *on the feeder itself*, so feeding, the camera, cat recognition and the feeder's settings all
work locally. You install the integration with HACS; the program on the feeder is separate
(see [The feeder side](#the-feeder-side)).

## What you get

- **Feeding.** Feed hopper 1, hopper 2 or both from a button, an automation or the `kibble.feed`
  action. Cancel a feed in progress. Choose how many portions to give (1 to 20). Read and edit the
  feeding schedule from Home Assistant.
- **Food status.** Hopper level and "empty" sensors, how full the bowl is, how many days the
  desiccant (moisture pack) has left, a "feeding now" indicator, and buttons to say you refilled a
  hopper or swapped the desiccant.
- **Camera and speaker.** The feeder camera as a normal camera entity (it streams straight from the
  feeder, or from any other camera entity you pick, for example one rebroadcast by Scrypted), a
  microphone switch, a night-vision switch, a speaker you can play audio through, a "Call the cats"
  button and a beep.
- **Cats.** Who is at the bowl: a presence sensor for each cat, an "eating" sensor, last-seen and
  detection counters, before/after photos of the dish and an avatar per cat. Kibble learns your cats
  from photos you label (and can keep learning on its own) and keeps a timeline of visits and meals
  in Home Assistant's own storage, for 7, 14, 30 or 90 days as you choose.
- **Instant updates.** When the feeder supports it, changes are pushed to Home Assistant the moment
  they happen. Otherwise Kibble asks the feeder every 45 seconds.
- **A way around Wi-Fi trouble.** If the feeder's network API cannot be reached, feeding can fall
  back to Bluetooth through an ESPHome Bluetooth proxy (optional; you give Kibble the feeder's
  Bluetooth address).
- **Health checks.** Wi-Fi signal, which path commands are taking (Wi-Fi, Bluetooth or none),
  firmware versions, and a repair notice in Home Assistant if the feeder stops answering.

## What you need

- Home Assistant **2026.6** or newer.
- A Petkit YumShare Dual 2 feeder running Kibble's feeder-side software
  ([see below](#the-feeder-side)).
- Optional: the [Kibble card](https://github.com/nphil/kibble-card) for the timeline, labelling
  and training screens; Scrypted for meal video clips; CoralHub for sharper cat recognition; a vision
  model server as a second opinion on meals (see [Optional extras](#optional-extras)).

## Install

1. In Home Assistant open **HACS**, choose the three-dot menu, then **Custom repositories**. Add
   `https://github.com/nphil/kibble` as type **Integration**.
2. Find **Kibble** in HACS, press **Download**, then restart Home Assistant.
3. Go to **Settings, Devices & services, Add integration, Kibble** and enter the feeder's address
   (port 8765 unless you changed it). Kibble does not discover feeders by itself, so type the
   address in.

Manual install: copy `custom_components/kibble` into your Home Assistant `config/custom_components`
folder and restart.

## Options

After setup, press **Configure** on the Kibble integration. All of these are optional.

| Option | What it does |
|---|---|
| Rebroadcast RTSP URL / Camera entity to stream from | Use another source for the camera instead of opening a second stream on the feeder. The camera entity is better when the URL changes (Scrypted's does). |
| Feeder BLE MAC address | Turns on the Bluetooth feeding fallback. |
| Allow schedule card writes | Lets the schedule card add, edit and remove feeds. Off by default (see [Known limits](#known-limits)). |
| Keep feeder history for | How long the timeline, photos and event journal are kept: 7, 14, 30 or 90 days. |
| Scrypted clips server | Adds a short video clip to each meal in the timeline. |
| Vision judge server and model alias | A second-opinion vision model that can hide a false alarm or correct a name. |
| CoralHub server and API token | Uses a Google Coral for more accurate cat recognition. Falls back to the built-in recognizer if it is unreachable. |

## Entities and actions

Entities are grouped under one device per feeder.

| Area | What is there |
|---|---|
| Feeding | Buttons to feed both, hopper 1, hopper 2 and to cancel; amount numbers; "Feeding" sensor |
| Food | Hopper 1 and 2 level and empty sensors, bowl fill, "Bowl empty", desiccant days left; refill and desiccant buttons |
| Schedule | Next feed and the schedule as sensors |
| Cats | Presence sensor per cat, "Eating", last seen pet, last detection and detections today, dish before/after and last-detection images, avatar per cat |
| Camera and sound | Camera, speaker (media player), status light, night vision, microphone, beep, call the cats |
| Feeder buttons | Pairing button, button 1 and button 2 as events |
| Settings | Detection and sound switches, sensitivity numbers, quiet-hours text fields, camera indicator, selected sound. Many of these are disabled until you turn them on. |
| Wi-Fi and health | Wi-Fi network picker, Wi-Fi signal, cloud connection, firmware, control path, stored clips |
| Stack | A select that switches the feeder between the Petkit stack and KibbleOS (it reboots the feeder) |

Actions: `kibble.feed`, `kibble.cancel_feed`, `kibble.schedule_set`, `kibble.schedule_add`,
`kibble.schedule_remove`, `kibble.schedule_set_enabled`, the `kibble.schedule_card_*` actions,
`kibble.wifi_connect`, `kibble.save_clip`, `kibble.record_clip`, `kibble.play_clip`, `kibble.beep`,
`kibble.call_cats`, `kibble.set_desiccant` and `kibble.clear_training`. Their fields are listed in
**Developer tools, Actions**.

## The feeder side

Kibble needs one of two programs on the feeder. They never run at the same time, and Kibble works
out which one it is talking to.

- **`kibbled`** is the Kibble agent in this repository (`agent/`). It runs *next to* the stock
  Petkit firmware and speaks the firmware's own internal message bus, so nothing is flashed and the
  phone app keeps working. Feeding, state, the schedule, the camera and detections work. Many of the
  newer settings do not, because the stock firmware does not allow them to be changed.
- **KibbleOS** is a replacement for the stock feeder software, built by the same author. It is a
  separate project and is not part of this repository. Every entity described here is available
  with it, including the settings switches and numbers, "Call the cats", the hopper refill buttons
  and the buttons on the feeder. The integration's **Stack** select (Petkit stack / KibbleOS) moves
  a feeder between the two.

Entities that need KibbleOS are simply not available while the feeder runs the Petkit stack.

### Installing `kibbled`

The feeder must be reachable over telnet.

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
completely stock boot. `telnetd` is started by `/etc/init.d/S50telnet`, before and independently of
this hook, so the recovery path cannot be locked out by it.

To build the agent yourself:

```sh
cd agent
cargo test
cargo build --release --target armv7-unknown-linux-musleabihf
```

The reverse-engineering notes behind the agent, including the exact wire formats and the addresses
they were recovered from, are in [docs/](docs/README.md).

## Optional extras

- **[Kibble card](https://github.com/nphil/kibble-card)** is the Lovelace card that shows the
  timeline and lets you label visits, add cats and train the recognizer. The integration provides
  its data through Home Assistant's WebSocket API and a few authenticated HTTP routes (photos,
  uploads and clips); without the card these have no screen of their own.
- **Meal video clips** need [Scrypted](https://www.scrypted.app) with this repository's
  `scrypted-plugin/` ([Kibble Feeder plugin](scrypted-plugin/README.md)) and the third-party
  *Events Recorder* plugin. The Scrypted camera is currently looked up by a fixed device id (`240`,
  in `custom_components/kibble/eating_clips.py`), so clips only work if your feeder camera has that
  id in Scrypted.
- **Vision judge** sends each meal's photos to a vision-language model behind an OpenAI-compatible
  address. It is built around a llama-swap server, and the default model alias is `qwen3-vl-4b`.
  Your own labels always win and the judge never trains the recognizer by itself.
- **CoralHub** is the author's own Google Coral server, a separate project. Leave it empty to use
  the built-in recognizer.

## Known limits

- After a Home Assistant restart Kibble does not hold Home Assistant's start-up up: it is ready
  within 5 seconds, and its entities appear as soon as the feeder has answered its first full
  status read (up to about 20 seconds on this slow device; longer if the feeder is off or still
  booting, and Kibble keeps trying by itself). Until then they show as unavailable, and the
  Kibble actions say the feeder has not replied yet.
- Kibble does not find feeders on its own. Type the address in when you add the integration.
- The schedule can be read freely, but the card's add/edit/remove actions are refused until you
  turn on **Allow schedule card writes**. The feeder's per-entry time encoding is still
  unconfirmed, so a wrong table could feed at the wrong time or amount.
- On the stock Petkit firmware the hopper chosen in a feed request is ignored (both augers turn),
  so a one-hopper feed delivers about double into the divider-less bowl.
- On the Petkit stack the feeder's speaker cannot be driven: the vendor firmware never plays what
  Kibble sends it ([docs/20-two-way-audio.md](docs/20-two-way-audio.md)).
- Meal video clips depend on the Scrypted setup described above.
- Developed against a YumShare Dual 2 (`D4SH2`), firmware 895, Axera AX620Q. The message formats
  are firmware-specific; the related `D4H2` model uses a slightly different feed payload (`amount`
  instead of `amount1`/`amount2`) which is documented but untested.

## What is in this repository

| Path | What it is |
|---|---|
| `custom_components/kibble/` | The Home Assistant integration (what HACS installs) |
| `agent/` | `kibbled`, the Rust agent that runs on the feeder next to the stock firmware |
| `scrypted-plugin/` | The Scrypted plugin for detections and meal clips |
| `dashboard/` | A sample Lovelace dashboard |
| `docs/` | Reverse-engineering notes and design records |
| `tests/`, `tests_ha/` | The integration's test suites (see [Development](#development)) |
| `scripts/`, `tools/` | Helpers: the feeder boot hook, the one-off history migration, C message tools, the cat-recognition calibration scripts, and `render_brand.py` for the icons |

Only `custom_components/kibble/` is copied to Home Assistant; nothing else here is installed by HACS.

## Development

The integration has two test tiers, both run from the repository root.

- **Pure Python, no Home Assistant needed** (the identity engine, translation consistency, the
  Bluetooth frame codec). This is the fast local loop:

  ```sh
  uv run --frozen pytest -q tests/test_identity.py tests/test_translations.py tests/test_ble_frame.py
  ```

- **The full suite** (every WebSocket command, entity platform and config flow test) needs a real
  Home Assistant, so it runs from its own virtual environment. Home Assistant 2026.9 needs
  Python 3.14:

  ```sh
  uv venv --python 3.14 .venv-ha
  uv pip install --python .venv-ha/bin/python "pytest-homeassistant-custom-component==0.13.366" \
    numpy pillow ha-ffmpeg PyTurboJPEG serialx bleak bleak-retry-connector pyserial habluetooth \
    bluetooth-adapters bluetooth-auto-recovery bluetooth-data-tools dbus-fast aiousbwatcher
  .venv-ha/bin/python -m pytest tests tests_ha -q
  ```

  `pytest-homeassistant-custom-component` 0.13.366 brings Home Assistant 2026.9.3 with it.

To redraw the icons after changing `tools/render_brand.py`: `python3 tools/render_brand.py`
(Pillow only; `icon.svg` is the hand-drawn vector twin of the mark).

## Licence

MIT.
