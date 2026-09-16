# Local push: design for moving the HA integration off polling

**Status:** implemented and live 2026-09-16 (agent `65129f1`, HA `9f1cd37`+`d39e223`).
Written as a design first, then annotated with what the implementation and the live tests
showed (§8). Against HA 2026.9.2. Every reference citation below was read in the installed HA source
(`homeassistant/components/{wled,unifiprotect,shelly,reolink,esphome}` and
`homeassistant/helpers/update_coordinator.py`); every Kibble citation was read in this repo.

**Hard acceptance criteria, in priority order**

1. **No existing entity or service changes behaviour.** All 52 entities and 17 services keep
   their entity ids, unique ids, state semantics, attributes, availability rules and write
   paths (the per-entity contract is `appendix-ha-inventory.md`,
   folded into §7 here).
2. **Less load on the feeder, never more.** The device runs the vendor's encoder at loadavg ~8
   on two A53 cores with ~25 MB free; its HTTP server is one thread, one connection at a time
   (`agent/src/http.rs:79-100`). Nothing in this design may hold that server, add a runtime,
   or write flash.
3. **Platinum-tier shape** for a local_push integration, mirroring the reference that matches
   Kibble's topology (one device, one persistent connection, no library): WLED.

---

## 1. Why polling is the wrong shape here, in numbers

| Fact | Value | Source |
|---|---|---|
| Poll cycle | 12 sequential GETs every 45 s | `coordinator.py:_fetch_all` |
| Per-request latency, healthy | 0.6–1.5 s | `const.py` `DEFAULT_SCAN_INTERVAL` comment |
| Cycle cost | 8–18 s of the feeder's single HTTP thread per 45 s (18–40 % duty) | same |
| Fields that change without user action | `state.{feeding,bowl_fill,desiccant_days,track}`, `events`, `feeds`, `wifi.signal` | `coordinator.py` field map |
| Fields that change only on our own write | `schedule`, `config`, `cloud.enabled`, `clips`, `cats`, `wifi.desired` | same |
| Static | `serial`, `firmware`, `ble_firmware` | same |
| Latency to HA for a device event | up to 45 s (+ cycle) | poll period |

Most of every cycle re-fetches things that cannot have changed. Push inverts it: the agent
already *knows* when each of these changes (§3), so it should tell HA once, when it happens.

---

## 2. Architecture

```
feeder (kibbled)                                  Home Assistant (kibble integration)
─────────────────                                 ───────────────────────────────────
:8765  HTTP  ── one thread, serial ──────────────  KibbleClient  (writes + fallback poll, unchanged)
:8766  WS    ── one thread, ONE client ──────────  KibblePush    (persistent, read-only, JSON frames)
        ▲
   change bus (crossbeam-less: Mutex<Vec<Sender>> + Condvar, see §3)
        ▲
   existing change detectors: ai::Feed, feed_capture, state sampler, reconcilers, scheduler
```

### 2.1 A second listener, not a long-poll on the first

The existing server handles one connection at a time (`http.rs:83-98`: `for stream in
listener.incoming()` → `handle_one`). A held connection there blocks every other client; this
was observed live — Scrypted's `detectionFeed.ts` explains it chose 5 s short-polling of
`GET /events` precisely because one held `GET /events/stream` made every HA entity
unavailable. Therefore:

- Push gets its **own `TcpListener` on :8766 and its own thread**. The :8765 server is untouched.
- The push listener accepts **exactly one client**; a second connect replaces the first (the
  old socket is closed with a `replaced` close frame). HA is the only intended client; one
  socket is the whole budget.
- `GET /events/stream` stays for backward compatibility but is not used by HA.

### 2.2 Protocol: WebSocket, hand-rolled, text frames of JSON

RFC 6455 server side is ~150 lines without extensions: HTTP/1.1 upgrade handshake
(`Sec-WebSocket-Accept` = base64(SHA-1(key + GUID)) — the agent already has `md5.rs`; SHA-1 is
~60 lines), unmasking client frames, sending unmasked server frames, ping/pong, close. No
compression, no fragmentation of our own frames (every message < 64 KiB), client frames
limited to control frames + a tiny `{"type":"resync"}` text.

Why WebSocket and not SSE/long-poll: HA's `aiohttp` client gives a first-class
`session.ws_connect()` with heartbeat, and platinum references (wled, unifiprotect) are
websocket-shaped; SSE would need a bespoke parser on the HA side and has no standard
bidirectional ping.

**Frames (server → client), all JSON objects with `"type"`:**

| type | when | body |
|---|---|---|
| `hello` | on connect | `{"proto":1,"agent_start_unix":…,"seq":N}` — `seq` is the agent's monotonic change counter |
| `snapshot` | immediately after `hello`, and on `resync` | the full `KibbleData` equivalent: `{"state":…,"schedule":…,"config":…,"cloud":…,"wifi":…,"wifi_scan":…,"cats":…,"identify":…,"review_face":…,"pending_face_count":N,"clips":…,"feeds":…,"events":…}` — **byte-identical JSON bodies to the corresponding GET endpoints**, so `api.py`'s existing `from_json` parsers are reused unchanged |
| `update` | on any change | `{"seq":N,"fields":{"<field>":<full value>}}` — one or more *whole* top-level fields, same JSON as the GET; never a partial diff of a field |
| `ping`/`pong` | keepalive | RFC 6455 control frames, 30 s interval from the agent, HA answers |

Whole-field replacement (not JSON-patch) is deliberate: it keeps the HA side a one-line merge
into an immutable `KibbleData` and makes "what does this field contain" have exactly one
answer, the GET endpoint's. The largest field (`events`, 50 entries) is ~8 KB; `wifi_scan` is
the next; everything else is < 1 KB. A busy evening emits maybe 30 updates/hour.

### 2.3 Change detection on the agent: reuse, don't add pollers

Every source already has a change point (`KibbleCurrent` inventory, verified in code):

| field | detector today | cadence | hook |
|---|---|---|---|
| `events` | `ai::Feed::push*` (Condvar) | on vendor JPEG mtime (1 s) / PetTrack change (1 s) | notify after `push_detection` |
| `state.feeding` | `feed_capture` FEEDING watcher | 200 ms | notify on 0→1 and 1→0 |
| `feeds` | `feed_capture::save_pair` | per feed cycle | notify after the sidecar is written |
| `state.{bowl_fill,desiccant_days,track,event_counter}` | `ai::poll_loop` already samples shm every 1 s | 1 s | compare a `Snapshot` per tick; notify on inequality (cheap: `Snapshot: PartialEq`, ~100 B) |
| `schedule`, `config`, `cloud`, `wifi`, `clips`, `cats`, `identify`, `review_face`, `pending_face_count` | mutated only by our own HTTP handlers or reconcilers | on write | notify at the end of each mutating `route()` arm / reconciler apply |
| `wifi.signal_dbm`, `wifi_scan` | `wifi.rs` reconciler tick | its existing interval | notify on change |

No new timers. The one new comparison (a `Snapshot` diff per second) is a few field compares
on already-read memory.

**Change bus:** `push::Bus { changed: Mutex<BTreeSet<Field>>, seq: AtomicU64, cv: Condvar }`.
Producers call `bus.mark(Field::Events)`; the push thread waits on the condvar, drains the
set, serialises the marked fields **by calling the same functions the GET routes call**, and
sends one `update` frame. Coalescing is free: ten marks in one tick = one frame.

### 2.4 Device budget

- Idle: zero HTTP requests, one TCP socket, one blocked thread, one ping/30 s. Versus today's
  16 requests/min.
- Memory: one 8 KB write buffer + the thread stack (set to 64 KB). No queue growth: if the
  client is slow the push thread blocks on `write` (10 s timeout) and then drops the client;
  HA reconnects and gets a fresh snapshot.
- CPU: serialisation of only the changed field(s), on change.
- Flash: nothing.

### 2.5 HA side: coordinator with push, WLED-shaped

Reference: `components/wled/coordinator.py:93-138` (`_use_websocket` / `listen`), verified in
the installed source:

```python
class KibbleCoordinator(DataUpdateCoordinator[KibbleData]):
    def __init__(...):
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN,
                         update_interval=timedelta(seconds=FALLBACK_SCAN_INTERVAL),
                         update_method=self._async_fetch_all)
        self._push: KibblePush | None = None
        self._push_task: asyncio.Task | None = None
```

- `update_method` stays: it is the **fallback poll** (§2.6) and the first refresh
  (`async_config_entry_first_refresh` proves the HTTP API before entities exist — rule
  `test-before-setup`, unchanged).
- After the first refresh, `async_setup_entry` starts the listen task with
  `entry.async_create_background_task(...)` and registers
  `entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close))`
  (wled `coordinator.py:129-135`; `__init__.py:66-67` disconnects on unload).
- Listen loop (mirrors wled `listen()` line for line):
  1. `ws_connect` via `async_get_clientsession(hass)` (rule `inject-websession`), `heartbeat=30`.
  2. On `hello`+`snapshot`: `self.update_interval = None` (wled: "Stop polling as long as we
     have a websocket"), `async_set_updated_data(snapshot)` — verified
     `update_coordinator.py:619-634`: cancels the pending refresh, sets
     `last_update_success = True`, notifies listeners.
  3. On `update`: `async_set_updated_data(dataclasses.replace(self.data, **fields))`.
  4. On any close/error: `last_update_success = False` only if the *fallback poll* then also
     fails — see §2.6 — and `finally:` `self.update_interval = FALLBACK_SCAN_INTERVAL;
     async_request_refresh()` (wled `coordinator.py:117-120`), then reconnect with
     exponential backoff 1 s → 60 s with jitter, capped, forever.
  5. Single-connection guard (wled `coordinator.py:95`): never start a second task while
     `_push_task` is live.

### 2.6 Availability: identical policy, different trigger

Today: 3 consecutive poll failures → `UpdateFailed` → entities unavailable, repair issue,
backoff (`coordinator.py` module doc). That policy is kept verbatim; what changes is only
*what counts as an attempt*:

- While push is connected, every received frame is a success (`consecutive_failures = 0`).
- If push drops, the fallback poll takes over immediately and the existing counter runs
  exactly as now. A push drop alone **never** marks entities unavailable — the device may be
  fine and only the socket died (HA restart, wifi blip). This is the wled behaviour
  (`WLEDConnectionClosedError` → `last_update_success=False` *then* an immediate poll decides).
- `binary_sensor.reachable` keeps its definition (`consecutive_failures == 0`).
- Staleness guard: if no frame (data or pong) arrives for 90 s, the client closes the socket
  itself (aiohttp `heartbeat`/`receive_timeout`) and falls into the reconnect path — the
  silent-drop case a NAT or a wedged agent produces.

### 2.7 Writes and the refresh-after-service pattern

Every service today does `client.<write>()` then `async_request_refresh()` in `finally`
(`coordinator.py`). Unchanged in code; with push connected the agent marks the affected
field at the end of the write handler, so the `update` frame lands *before* the refresh would
have, and the refresh is a cheap no-op confirmation. If push is down, the refresh does what it
does today. No service loses its immediate-feedback guarantee.

### 2.8 Presence becomes event-driven

With sub-second delivery, `*_present` no longer needs a 2-minute latch to be visible:
`PRESENCE_WINDOW` shrinks to a small hold (30 s) and the vendor's live tracker session
(`state.track` cleared = session over) can turn it off early. This is the one entity whose
*semantics* improve; its id, attributes and on/off meaning do not change.

---

## 3. What does not change (compatibility contract)

- **`api.py`**: every `from_json` parser and every write method unchanged. The push client is a
  new class alongside `KibbleClient`, not a rewrite.
- **`KibbleData`**: same dataclass, same fields. `update` frames are applied with
  `dataclasses.replace`; `vendor_sightings` is re-derived exactly as now.
- **Entities**: all read `coordinator.data.<field>` today; that access path is untouched, so no
  platform file needs to know push exists. `unique_id`s, translation keys, categories,
  icons, `entity_registry_enabled_default` all unchanged (rule check remains
  `tests/test_entity_platform_rules.py`).
- **Services**: unchanged signatures and handlers (§2.7).
- **Scrypted**: keeps polling `GET /events` on :8765 every 5 s; it is unaffected because the
  push listener is a different port and thread.
- **Options flow**: unchanged. Push has no configuration; it is on when the agent offers it.
  An agent without :8766 (older build) is detected by the connect failure and the integration
  simply stays in polling mode — the two are release-independent.
- **Timing-dependent behaviours preserved**: `events` cap 50 (agent side, unchanged);
  `detections_today`'s `capped` attribute; `feed_capture` 3 s settle; availability counter.

Explicit non-goals: no new entities, no entity renames, no change to the classifier surface
(`last_seen_pet` etc. — that decision is deferred), no change to the agent's HTTP API.

---

## 4. Quality-scale deltas

| rule | today | after |
|---|---|---|
| `iot_class` | `local_polling` | `local_push` |
| `appropriate-polling` | done | exempt — "push over WebSocket; polling is the fallback only" (wording as unifiprotect/wled) |
| `entity-unavailable`, `log-when-unavailable` | done via coordinator | unchanged; coordinator logs the first failure |
| `test-before-setup` | first refresh over HTTP | unchanged |
| `parallel-updates` | `PARALLEL_UPDATES = 0` everywhere | unchanged |
| `inject-websession` | done | push uses the same session |
| `strict-typing`, `runtime-data`, `diagnostics` | done | diagnostics gains `push: {connected, last_frame_unix, reconnects}` |
| `dependency-transparency` / library | n/a (no PyPI dep) | unchanged — `KibbleClient` + `KibblePush` are the "library" layer, per the references' structure |

---

## 5. Implementation plan (each step ships alone and is independently revertible)

1. **Agent: `push.rs`** — `Bus`, WebSocket server thread on :8766, `hello`/`snapshot`/`update`,
   ping, single-client replacement. Marks wired into: `ai::Feed::push_detection`,
   `feed_capture` (feeding edges, save_pair), `ai::poll_loop`'s shm tick (Snapshot diff),
   every mutating `route()` arm, `cloud`/`wifi`/`persist` reconciler apply points. Tests:
   handshake accept-key vector (RFC 6455 §1.3 example), frame encode/decode, coalescing,
   snapshot == GET bodies (same function, asserted).
   Deploy; verify with a throwaway Python `websockets` client that a manual feed produces
   `feeding` true→false frames and an `events` frame within 1 s, while Scrypted's 5 s poll
   and `curl /state` keep working. Watch `ps`/RSS for the new thread.
2. **HA: `push.py`** (`KibblePush`: connect, iterate frames, typed parse into `KibbleData`
   fields via the existing `from_json`s) + coordinator listen loop + fallback semantics
   (§2.5–2.6) + diagnostics. `manifest.json`/`quality_scale.yaml` deltas. Tests: frame→
   `KibbleData` merge, disconnect → fallback poll → reconnect, single-task guard, no change
   to any entity test (the full suite must pass untouched).
   Deploy; verify entity latency with a stopwatch on `binary_sensor.*_feeding` during a feed
   (ask before dispensing), and `pancake_present` on the next vendor track.
3. **Presence hold** (§2.8) — after 1–2 prove sub-second delivery.

Not in scope until 1–2 land: the classifier-surface cutover, `track_value` naming.

---

## 6. Risks and how each is bounded

| risk | bound |
|---|---|
| Push thread bug hangs the agent | separate thread; the HTTP thread and every reconciler are untouched; `panic=abort` restarts kibbled via the existing supervisor (remote-syslogged) |
| Client never reads, agent blocks on write | 10 s write timeout → drop client → HA reconnects with backoff |
| Reconnect storm | HA backoff 1→60 s jittered; agent accepts one socket, cost of a rejected connect is one TCP handshake |
| Frame/GET drift | snapshot/update bodies are produced by the same serialisers as the GET routes; a test asserts equality |
| Old agent + new HA, or the reverse | connect fails → polling as today; old HA ignores :8766 |
| Missed change (a producer forgets to `mark`) | fallback poll is not disabled while connected? — **No**: it *is* disabled (wled pattern), so a missed mark would show stale data until the next event. Mitigation: a 10-minute safety `resync` request from HA (one frame, one snapshot) — cheaper than any poll and bounds staleness to 10 min for any field we forgot |

---

## 7. Entity contract (verbatim list this design is held to)

See `appendix-ha-inventory.md` (2026-09-16): 33 sensors, 19 binary sensors
(incl. dynamic `cat_present_*`), 4 switches, 4 numbers, 4 buttons, 2 selects, 4 images,
camera, media_player; 17 services. Every row's `reads` column is a `coordinator.data.<field>`
path that §3 guarantees unchanged.

---

## 8. As built, and what the live tests showed

| design item | as built |
|---|---|
| agent listener | `agent/src/push.rs`, :8766, own thread (64 KiB stack), one client; `sha1.rs` for the handshake |
| frame bodies == GET bodies | enforced by construction: `main.rs` builds `push::Serialize` from the same functions the routes call (four fallible handlers were refactored into `*_json() -> Result<String,String>` shared by both) |
| marks | `ai::Feed::push_detection` (events); `ai::poll_loop` 1 s `Snapshot` diff (state); `feed_capture` FEEDING edges + `save_pair` (state, feeds); `schedule::save`; `cloud::save` + a status-JSON diff on its 15 s reconciler tick; `wifi::save` + status/scan diffs on its 60 s tick; `clips::{save,delete}`; `persist::apply_value` (config); `faces::{save_pending,label,unlabel,add_cat}` (cats/identify/review_face/pending_faces together) |
| HA transport | `custom_components/kibble/push.py`: `session.ws_connect(heartbeat=30, receive_timeout=90)`, frames parsed with the existing `from_json`s, `merge_frame` = `dataclasses.replace` |
| coordinator | `async_start_push` (entry background task, single-task guard), `_push_loop` (jittered backoff 1..60 s), `_consume` (10-min resync), `_apply_frame` (resets the failure counter, `async_set_updated_data`); `update_interval=None` while connected; cancelled on unload, closed cleanly on `EVENT_HOMEASSISTANT_STOP` |
| quality scale | `iot_class: local_push`; `appropriate-polling: exempt`; diagnostics `push:{connected, unsupported_by_agent, reconnects, seconds_since_last_frame, update_interval_seconds}` |

**Live, measured (2026-09-16):**

- Agent alone: hello → 13-field snapshot; `config/schedule/cloud/cats/clips/feeds/review_face`
  byte-equal to their GETs; `GET /state` answered in 10 ms while the socket was held; an HTTP
  write produced an `update` with the four face fields inside the same second; RSS 1.4 MB.
- HA: connected 30 s after restart, `update_interval_seconds: null`; a write reached HA as a
  frame in ~0.2 s (vs ≤45 s before). All 41 registered entities kept their ids and states.
- Drop test (`kill kibbled`, the supervisor respawns it in ~5 s): HA fell back to 45 s polling
  within 5 s, **no entity went unavailable**, reconnected and re-entered push mode at 15 s.
- One defect found and fixed by that test: the first build classified *connection refused*
  as "agent has no push" and stopped retrying. Only a rejected handshake or a foreign `proto`
  is permanent now; refused/unreachable always retries. Regression test in `tests/test_push.py`.

**Deliberately unchanged:** every entity and service (`appendix-ha-inventory.md`); Scrypted's
5 s `GET /events` poll; the HTTP API. `PRESENCE_WINDOW` stays at 2 min for now -- with push it
could shrink, but that is a semantics change to decide separately (§2.8).

**`total_score`:** the vendor's per-visit float published on `track` events was traced in
`libalgo` (`kibble-agent-tmp/study/TrackValue.md`): the sum over the visit's qualifying frames
of the best-candidate confidence, accumulated with `vadd.f32` and copied out unmodified; the
vendor's own name for the threshold it is compared against is `discern_total_score`. It is
exposed under that name (attribute of `sensor.vendor_last_seen_pet`, field on `track`
events), never as the entity state, because it is a length-weighted total, not a probability.

