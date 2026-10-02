# 37. Hopper-full tracking

Status: design of record, 2026-09-24. Extends docs/36-ai-pipeline.md's device/HA split with the
"mark hopper full" feature added alongside the pipeline v2 rebuild. LibreFeed-only throughout:
kibbled (the vendor stack) has no equivalent route or fields.

## Why

Neither hopper has a scale, so the feeder cannot answer "how much food is left" from a raw
reading. What it can do instead: count portions dispensed since the operator last confirmed a
hopper was topped off, and learn how many portions that hopper reliably holds before its own
low-food sensor trips. That gives a running estimate without new hardware, at the cost of needing
the operator to say "I just filled this" at least once per hopper before the estimate means
anything.

## Device contract (feeder HTTP, port 8765)

### `POST /hopper/full`

Body `{"hopper": "1" | "2" | "both"}`. Tells the daemon this hopper (or both) was just physically
refilled to capacity: resets that hopper's `hopper_full_at` to now and `hopper_portions_since_full`
to 0. Absent from kibbled's own route table, same footing as `/beep`/`/desiccant` -- a 404 on the
vendor stack is a real failure, not an optional read to fall back on (`api.py`'s
`KibbleClient.mark_hopper_full`).

### `GET /state` fields

Three per-hopper tuples, `[hopper_1, hopper_2]`, every entry `None` until that hopper has been
marked full at least once:

- `hopper_full_at` -- unix time the hopper was last marked full.
- `hopper_portions_since_full` -- portions dispensed from that hopper since then. Every dispense
  from that hopper (scheduled or manual) adds its amount here; it only resets on the next mark-full.
- `hopper_full_to_low` -- the portions the daemon has learned it takes to run that hopper from
  full down to its own low-food threshold tripping. Stays `None` specifically until the hopper has
  actually been seen to run all the way from a marked-full state down to low -- it is a learned
  number, not a configured one (`api.py`'s `FeederState` docstring).

### Learning rule

1. Mark full (`POST /hopper/full`, or the physical gesture below) sets `hopper_full_at = now` and
   `hopper_portions_since_full = 0` for that hopper.
2. Every subsequent dispense from that hopper adds to `hopper_portions_since_full`.
3. The first time that hopper's low-food sensor trips after being marked full, the daemon records
   `hopper_full_to_low = hopper_portions_since_full` at that exact moment -- so "how many portions
   did it actually take to go from full to low this time" becomes the learned capacity.
4. `hopper_full_to_low` is re-learned every time a full-to-low cycle completes, not set once and
   kept forever -- if a hopper's real capacity drifts (a different bag, a different fill height),
   the next full cycle corrects it.

### Physical control: the pairing button

The recessed button (`event.py`'s node 3, exposed as `event.*_button_pairing`) already means "long
press opens BLE pairing" (`state.dev_pro.ble_open_by_key`, docs/03-app.md) -- that meaning does not
change. LibreFeed adds one more gesture on top of it: a double short press (two press-and-release
cycles inside the daemon's own double-press window) marks *both* hoppers full at once, exactly as
`POST /hopper/full {"hopper": "both"}` would, and the daemon plays a double beep to confirm it
registered. Long press is never reinterpreted as a hopper action -- it still only opens BLE
pairing, so an operator mid-press-and-hold for pairing never accidentally resets a hopper's
counters.

This gesture is entirely device-side; Home Assistant does not detect it. Whichever hoppers get
marked full, by button or by the HA buttons below, the result shows up identically in HA's next
poll of `/state`. `event.*_button_pairing` keeps exposing the raw press/long_press/release stream
for diagnostics or the user's own automations, but no automation should treat its `long_press`
event as a hopper trigger -- that event already means the device just opened its BLE pairing
window, and a hopper-full automation firing on it would be acting on the wrong thing.

## HA entities

Both platforms are gated `_LIBREFEED_ONLY` in `stacks.py` (fixed alongside this doc: they were
missing from that table, which meant they would have been created -- non-functional -- on the
vendor stack too, since an unlisted `(platform, key)` defaults to `_BOTH`).

### Buttons (`button.py`, `MARK_HOPPER_FULL`)

| Entity | unique id | translation_key | Calls |
|---|---|---|---|
| `button.<device>_hopper_1_full` | `{serial}_hopper_1_full` | `hopper_1_full` | `POST /hopper/full {"hopper":"1"}` |
| `button.<device>_hopper_2_full` | `{serial}_hopper_2_full` | `hopper_2_full` | `POST /hopper/full {"hopper":"2"}` |
| `button.<device>_hopper_full` | `{serial}_hopper_full` | `hopper_full` | `POST /hopper/full {"hopper":"both"}` |

`KibbleMarkHopperFullButton.async_press` calls `coordinator.async_mark_hopper_full(hopper)`, which
calls the client then immediately refreshes so the remaining sensors below reflect the reset
counters without waiting for the next scheduled poll. Icon (`icons.json`, already present):
`mdi:tray-full` on all three.

### Sensors (`sensor.py`, `HOPPER_REMAINING_SENSORS`)

| Entity | unique id | translation_key | Value |
|---|---|---|---|
| `sensor.<device>_hopper_1_remaining` | `{serial}_hopper_1_remaining` | `hopper_1_remaining` | `hopper_remaining(hopper_full_to_low[0], hopper_portions_since_full[0])` |
| `sensor.<device>_hopper_2_remaining` | `{serial}_hopper_2_remaining` | `hopper_2_remaining` | `hopper_remaining(hopper_full_to_low[1], hopper_portions_since_full[1])` |

`hopper_remaining(full_to_low, portions_since_full)` is `max(0, full_to_low - portions_since_full)`
once both are known, else `None` (shown as unknown) -- covers both "never marked full" and "marked
full but has not yet completed one full-to-low cycle". Unit `portions`, `state_class:
measurement`. Extra state attributes: `full_at` (ISO timestamp or `None`), `portions_since_full`,
`full_to_low` -- the raw numbers behind the estimate, for anyone who wants them on a dashboard.

## Translation keys still needed

`strings.json`/`translations/en.json`/`icons.json` do not yet have display-name strings for any of
the five keys above (`hopper_1_full`, `hopper_2_full`, `hopper_full`, `hopper_1_remaining`,
`hopper_2_remaining`) -- only the three buttons' icons are in place. Until the strings land, all
five entities show with Home Assistant's translation-key-derived fallback name instead of a
written one. This needs whoever owns the integration's `strings.json` copy next (the entity/key
table above is the exact contract); it is not test-covered, so it was left for that pass rather
than fixed here.

## Hopper modes and food names

Status: design of record, 2026-09-24. Independent of hopper-full tracking above -- this section
is about the removable divider between the two hopper compartments, not the low-food estimate --
but it lives in the same file because both are "hopper" features and the divider's mode changes
how the estimate above is even framed (one bin vs. two).

### The two modes

`switch.py`'s `KibbleHopperDividerSwitch` (`switch.<device>_hopper_divider`, translation key
`hopper_divider`) is the single source of truth, mirrored onto `KibbleCoordinator.single_hopper`:

- **Dual** (divider fitted, the switch on, the default): two compartments, each with its own
  dispenser, optionally holding different foods.
- **Single** (divider removed, the switch off): one shared bin. `KibbleCoordinator.async_feed`
  reroutes a `hopper="both"` feed onto dispenser 1 alone rather than running both augers into the
  same bin and dispensing twice the requested amount; `card_amounts`/`async_schedule_card_add`/
  `_edit` put a schedule-card entry's whole amount on dispenser 1 the same way, and
  `async_set_single_hopper` converts the *existing* device schedule when the divider changes
  (dual -> single sums each entry onto side 1; single -> dual splits it back, the odd portion on
  side 1), gated by the `enable_schedule_writes` option like every other schedule write.

### Food names (local only)

`text.py`'s `KibbleHopperFoodText`, one per hopper, both local-only `RestoreEntity`s with no
device round trip -- the feeder has no concept of what is loaded, so this is purely an HA-side
label:

| Entity | unique id | translation_key | Max length |
|---|---|---|---|
| `text.<device>_hopper_1_food` | `{serial}_hopper_1_food` | `hopper_1_food` | 24 |
| `text.<device>_hopper_2_food` | `{serial}_hopper_2_food` | `hopper_2_food` | 24 |

Always available (not gated on the feeder answering polls), icon `mdi:food-drumstick-outline`,
applies to both stacks (no `ENTITY_STACKS` row -- see that module's default). An empty string
means unnamed; there is no requirement to name either hopper. `KibbleCoordinator.hopper_food(n)`
(`n` is `1`/`2`) reads the current name back (`None` when empty) -- kept current by the entity
itself pushing every restore and every write onto the coordinator, the same way the divider
switch keeps `single_hopper` current. Food names are only meaningful in dual mode: with the
divider out there is one bin and nothing to name.

### Per-feed facts recorded at ingest

`store.py`'s `feeds` table (schema version 2) gained five columns: `amount1`/`amount2`
(`INTEGER`, the device's own per-hopper portions for that feed), `food1`/`food2` (`TEXT`, each
hopper's name *at the moment this feed happened*), and `single` (`INTEGER` 0/1, the divider mode
at that moment; `NULL` for a row ingested before this existed). `ingest.py`'s `Ingestor._ingest_feed`
fills all five from the device record and the coordinator's current state -- but only on that
row's first `INSERT`; `upsert_feed`'s `ON CONFLICT` clause deliberately never re-touches them, so
a later re-poll of the same feed id, a divider flip, or a food rename can never rewrite what
actually happened at feed time. `portions`/`scheduled`/`confirmed`/`before`/`after` keep updating
on every upsert exactly as before.

Migrating an existing database is additive only: `_migrate` adds each missing column with
`ALTER TABLE ... ADD COLUMN`, guarded by `PRAGMA table_info` (idempotent -- safe to run on every
open), and bumps `meta.schema_version` to `2`. The old `hopper` column these replace is left in
place, unused, rather than dropped -- nothing has read or written it since.

### The `kibble/timeline` feed shape

A timeline item's `feed` (`kind == "feed"`) is rendered by `websocket.py`'s `_feed_view` from the
stored row above:

```jsonc
{
  "portions": 3.0,
  "scheduled": false,
  "confirmed": true,
  "single": false,
  "sides": [
    {"hopper": 1, "portions": 1, "food": "Kibble"},
    {"hopper": 2, "portions": 2, "food": "Freeze-Dried"}
  ]
}
```

- `single` is the row's own recorded mode; for a `NULL` (pre-migration) row it falls back to the
  coordinator's *current* `single_hopper`.
- `sides` lists only hoppers that actually dispensed more than 0 portions, in hopper order, each
  with the food name recorded on that row -- or, when that hopper had no name of its own at feed
  time (`food1`/`food2` `NULL`, whether a genuinely unnamed feed or a pre-migration row that never
  had one), the coordinator's *current* name for that hopper instead. Naming a hopper later
  therefore also labels every past feed that never got a name of its own; a feed that *did* record
  one keeps saying exactly that even after a rename.
- In single mode `sides` is always `[]` and `portions` is the whole serving from the one bin --
  single mode, one dual feed of `2`/`0` portions, and one dual feed of `0`/`2` portions all look
  identical here, correctly: there is only one bin, so there is nothing to attribute a side to.
- The old `feed.hopper` field (a bare `1`/`2`/`null`) and `_for_hopper_mode` are gone.

Single mode example (the one-bin case above):

```jsonc
{"portions": 3.0, "scheduled": false, "confirmed": true, "single": true, "sides": []}
```

Dual mode, one side only (the other hopper dispensed nothing this feed):

```jsonc
{
  "portions": 5.0, "scheduled": false, "confirmed": true, "single": false,
  "sides": [{"hopper": 1, "portions": 5, "food": null}]
}
```

Because `single`/a side's fallback `food` both depend on live coordinator state, not just the
stored row, `kibble/timeline/subscribe`'s snapshot comparison (`_render_feeds` applied to both the
page and the subscribe check) changes -- and so pushes `{"changed": true}` -- when the divider
flips (every `NULL`-mode row's rendering changes) or a hopper is renamed (every row whose own
`food1`/`food2` is still `NULL` changes), not only when the store itself gains a new row.

`kibble-card/src/types.ts`'s `TimelineEvent.feed` mirrors this shape exactly; `lib/timeline.ts`
turns it into the "Fed ..." wording shown in the timeline (single vs. dual, one side vs. both,
named vs. unnamed).
