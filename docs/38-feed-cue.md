# 38. The feed cue

Status: design of record, 2026-09-25. LibreFeed-only throughout: kibbled (the vendor stack) has
no equivalent route, entities, or sound. The device-side design (tone rationale, exactly-once
trigger, call debounce/suppression, why the cue preempts the speaker rather than falling back to
the MCU buzzer) lives in `librefeed/docs/09-architecture.md`'s "Feed cue" section and
`librefeed/daemon/assets/feed-cue/README.md`; this document is the HA-side contract on top of it.

## Why

Nitin is Pavlovian-training Kitty and Pancake: the same sound has to mean "food is coming" every
time, for every way food can arrive, and never mean anything else. That rules out reusing the
existing, user-selectable system chime (`select.selected_sound`, `speaker::CHIME_PATTERNS`) --
a setting a person can change is not a signal an animal can learn to trust -- so the feed cue is
a second, fixed, non-selectable sound with its own contract.

## Device contract (feeder HTTP, port 8765)

### `POST /cue`

No body. Plays `librefeedd`'s embedded `feed-cue.wav` through the speaker on demand ("call the
cats") -- the only trigger for this sound that is not a real dispense. Replies:

```json
{"ok": true, "played": true}
```

`played` is `false` (still `200`) when `sound_enable`/`feed_sound`/`tone_mode`'s DND window
mutes it -- the call still "counts" (see debounce/suppression below) even when silent. A repeat
call inside the 10-second per-call cooldown (`speaker::CALL_COOLDOWN`) is rejected instead:

```json
{"ok": false, "error": "cooldown", "retry_after_ms": 4231}
```

HTTP `429`, not `409` -- deliberately distinct from `/speak`/`/speaker/tone`'s "something else
holds the speaker" `409`, since a cue cooldown is a different, unrelated condition
(`api.py`'s `KibbleClient.call_cats` passes a matching `busy_status=429`,
`busy_error=KibbleCueCooldownError`, so a caller never mistakes one for the other).

### Scheduled dispenses only

Unchanged surface (`POST /feed`, the scheduler, a physical button press, and -- once an
on-device BLE handler exists, `kibble/docs/25-ble-feed-frame.md` -- a BLE-triggered dispense):
no new field, no new route. The cue is a side effect on the device, invisible to any of this
integration's existing polling/parsing.

## The trigger contract

- **Scheduled feeds only (Nitin, 2026-09-27).** The cue plays once per scheduled dispense, at
  the moment the MCU confirms it started -- never at finish, never twice. A manual feed (the
  card's hold-to-feed, `kibble.feed`, the feeder's own button) stays silent: the person is right
  there and can play the cue from the camera card's "call the cats" when they want it.
- **Never without food.** A rejected command, a missed or surplus-skipped schedule occurrence,
  `kibble.beep`/`button.*_beep`, `POST /speaker/tone`'s self-test, and calibration never trigger
  it -- none of those paths are wired to it at all, not merely gated off.
- **A call and a following dispense share one cue.** `kibble.call_cats`/`button.*_call_cats`
  played the cue for a meal Nitin then hand-feeds; a real dispense that follows within 3 minutes
  (`speaker::CALL_SUPPRESSES_FEED_CUE_WINDOW`) skips its own cue rather than doubling it, but
  still dispenses normally, and does not suppress any later, unrelated dispense.
- **Repeated calls are debounced**, not queued: a second `kibble.call_cats` inside the 10-second
  cooldown is rejected outright (`KibbleCueCooldownError`), whether or not the first call's own
  ~1-second cue has finished playing.

## Tone (see the linked docs for the full writeup)

A soft major-triad chime -- C7/E7/G7 (2093/2637/3136 Hz), each note with a 20 ms attack and an
exponential decay, a gentle second harmonic, ~1.02 s total -- chosen for two audiences at once:
inside cats' most sensitive 2-8 kHz hearing band, and deliberately pleasant rather than alarming
for the humans living with it long-term (no square/buzzer timbre, no siren sweep, no sustained
tone above ~3.5 kHz). Committed as a real WAV (`librefeed/daemon/assets/feed-cue/feed-cue.wav`),
generated deterministically by `assets/feed-cue/generate.py`, embedded verbatim into `librefeedd`
and played sample-for-sample -- never resynthesized or approximated on the HA side.

## HA entities

Both gated `_LIBREFEED_ONLY` in `stacks.py`, same footing as `beep`/`hopper_*_full` -- absent
from kibbled's own route table, so a vendor-stack call would 404 rather than do anything.

### Button (`button.py`, `KibbleCallCatsButton`)

| Entity | unique id | translation_key | Calls |
|---|---|---|---|
| `button.<device>_call_cats` | `{serial}_call_cats` | `call_cats` | `POST /cue` |

`available` mirrors `KibbleBeepButton` exactly: the LibreFeed-presence `led` marker, since this
action has no polled state of its own to gate on. `async_press` calls
`coordinator.async_call_cats()`; a `KibbleCueCooldownError` maps to its own translated message
(`cue_cooldown`), not the generic `agent_action_failed` every other button-press failure uses.
Icon `mdi:cat` (`icons.json`).

### Service (`kibble.call_cats`)

`device_id` only (`services.yaml`/`strings.json`/`translations/en.json`, `CALL_CATS_SCHEMA` in
`__init__.py`) -- no other fields, unlike `kibble.beep`'s count/on_ms/off_ms: there is exactly
one cue, nothing to parameterise.

## Card (`kibble-live-hero.ts`)

A round cat-shaped chip in the camera overlay's control group, alongside the mute and talk
chips, matching their size/style (38 px circle) -- but not gated on `_playing`: calling the cats
needs no live video, so it is visible any time the button entity itself is (`callCatsEntity`
prop, resolved automatically by `lib/resolve-entities.ts`'s `callCatsButton` role). Hidden
whenever that entity is missing or reports `unavailable` (feeder unreachable, or the vendor
stack, exactly like `KibbleCallCatsButton.available`). Tapping it presses the button entity
(`hass.callService("button", "press", ...)` -- the same mechanism any card uses); the button then
shows a ~1.1 s soft amber "sound-wave" pulse (two expanding rings, `prefers-reduced-motion`
aware) and stays disabled for a full 10 seconds (`CALL_COOLDOWN_MS`, mirroring
`speaker::CALL_COOLDOWN` exactly) so the UI is never re-enabled before a repeat press would
actually be accepted. `aria-label`/`title` are the fixed string "Call the cats" (unlike mute/
talk's state-dependent labels, since this action has no on/off state of its own).

## Translation keys

`strings.json`/`translations/en.json`/`icons.json` already carry `button.call_cats`,
`services.call_cats`, and `exceptions.cue_cooldown` -- landed alongside the feature, unlike
`37-hopper-full.md`'s buttons, which shipped ahead of their own strings.
