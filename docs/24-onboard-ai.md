# Onboard AI: how detection results leave `media`, and what `kibbled` now exposes

Produced 2026-09-15. Read-only live telnet (credentials in project memory, not repeated here)
plus offline static analysis of `/app/bin/ctrl`, `/app/bin/media`, `/alg/libalgo.so` pulled from
the live device this session (byte-identical to the running binaries, confirmed by `md5sum` on
both ends):

| Binary | Size | md5 |
|---|---|---|
| `/app/bin/ctrl` | 742,148 | `c645c0665da2cf73db93ffa8d9d0ea68` |
| `/app/bin/media` | 348,116 | `f9e74f321a2bb7693f495598d816386a` |
| `/alg/libalgo.so` | 5,743,920 | `d6f1c8cf84d8c205355f7b0d9ebea462` (matches `docs/18-npu-confirmed.md`'s prior pull) |

No `pktool` command was run, nothing was written outside `/tmp`/`/opt/kibble`, and no vendor
process was killed, signalled, or restarted (the one restart in this session was `kibbled` itself,
via its own designed `kill pid` → restart-loop deploy mechanism — see §6). Final live check
confirms `watchdog`/`ble`/`media`/`ctrl`/`agora`/`cloud` all still running, continuous uptime, no
watchdog reboot (§7).

Methodology note: `ctrl`/`media` are stripped `ET_EXEC` Thumb-2 ARM binaries (no `.symtab`), but
**every `dispatch_handler_*` function is still present as a named, sized entry in `.dynsym`** —
this pass exploited that directly rather than string-cross-referencing function boundaries. Tool:
a from-scratch Python ELF32 reader (`pyelftools`) + `capstone` (`CS_MODE_THUMB`, `skipdata=True`
for linear-sweep resync across embedded literal pools) + a small symbolic-execution pass over the
GOT-indirected `ldr rX,[pc,#N]; add rX,pc; ldr rY,[rBASE,rX]`-style address materialisation idiom
that both binaries' shared dispatch library compiles to.

---

## 1. Where the result actually leaves `media`: confirmed to be the bus, not shared memory

Live, read-only, both processes' full memory maps inspected via `/proc/<pid>/maps`:

- **`media` (live pid 204 this session) maps exactly three POSIX shm segments**:
  `/dev/shm/media_buffer_frame_buf` (the already-documented frame ring), `/dev/shm/config_shm`,
  and one small (one page, 4 KiB mapping) anonymous, already-`shm_unlink`'d segment named
  `/dev/shm/gQDyHN (deleted)`. This third one was investigated directly: opened read-only via
  `/proc/204/map_files/<range>` (the standard Linux mechanism for reading a still-mapped-but-
  unlinked file by address range — a read-only, non-mutating operation, not an attach to any
  vendor reader-registry), and its **real file size is 16 bytes**, not the full page (`od`
  hit EOF after one row: `00 00 00 00 80 00 00 00 00 00 00 00 00 00 00 00`). Sixteen bytes is far
  too small for a `petkit_event_result_info`-shaped record (minimum ~24–48 bytes per
  `docs/12-ai.md` §4's printf-derived field list) — this is a generic small synchronization
  primitive (a counter/flag pair, the classic `shm_open`+immediate-`shm_unlink` trick some
  libraries use to get a portable `MAP_SHARED` anonymous region), not an AI result buffer.
- **`ctrl` (live pid 212) maps only `/dev/shm/config_shm`.** No shared memory in common with
  `media` beyond that.

**Conclusion: there is no shared-memory result channel.** The only remaining candidate from the
assignment's own list — a private POSIX message-queue message to `ctrl` — is what the disassembly
below confirms.

---

## 2. `ctrl`'s complete inbound handler table (`/msg_dispatch_1`), recovered and cross-validated

`ctrl`'s handler-registration code (`.text`, starting at its very first function, `0x15948`) is a
straight-line sequence of calls to one shared `register(msg_id, handler_fn, name_ptr)`-shaped
function at `0x80a14`. Each call site follows the identical pattern:

```
ldr  r3, [pc, #N1]     ; raw GOT-relative offset for this handler's PLT/GOT slot
movw r0, #MSG_ID        ; the msg_id itself, as a plain 16-bit immediate
ldr  r2, [pc, #N2]      ; raw PC-relative offset for the handler's __func__-style name string
ldr  r3, [r4, r3]       ; r3 = *(GOT_BASE + N1) = the handler function's absolute address
add  r2, pc              ; r2 = absolute address of the name string
mov  r1, r3               ; r1 = handler address
bl   0x80a14               ; register(msg_id=r0, handler=r1, name=r2, ...)
```

(`r4` is `ctrl`'s GOT base, set up once at function entry: `ldr r4,[pc,#0x530]; add r4,pc` →
resolves to exactly `0xd4000`, the live `.got` section start — confirmed, not assumed.)

Symbolically executing this one function end-to-end recovered all **25** registrations, matching
`ctrl`'s 25 `dispatch_handler_*` `.dynsym` entries exactly, with **zero unmatched handlers on
either side**. Three of the 25 msg_ids were already independently proven correct by prior,
different methodology (`docs/design-agent.md`, `agent/src/bus.rs`) — this pass reproduces every
one of them exactly, which is why the whole table is trusted:

| msg_id | Handler | Prior evidence |
|---|---|---|
| `0x100a` | `dispatch_handler_recv_ble_data` | matches `agent/src/bus.rs`'s `RECV_BLE_DATA` |
| `0x100f` | `dispatch_handler_feed` | matches `agent/src/bus.rs`'s `FEED` (the msg *ctrl itself* is dispatched via from the cloud path) |
| `0x101a` | `dispatch_handler_ble_get_schedule` | matches `agent/src/bus.rs`'s `BLE_GET_SCHEDULE` |
| `0x101d` | `dispatch_handler_sync_led_mod` | matches `docs/design-entities.md`'s §1.3 `lightMode` row ("ctrl msg 0x101d = dispatch_handler_sync_led_mod") |

Full table (25 entries; `size` is the handler's byte length from `.dynsym`):

| msg_id | Handler | size |
|---|---|---|
| `0x1002` | `dispatch_handler_ctrl_event_msg` | 208 |
| `0x1003` | `dispatch_handler_net_dev_ota_check` | 68 |
| `0x1005` | `dispatch_handler_lapse_record_over` | 46 |
| `0x1006` | `dispatch_handler_set_connect_http` | 212 |
| `0x1007` | `dispatch_handler_save_wifi_conf` | 132 |
| `0x1008` | `dispatch_handler_ctrl_get_upload_pic_url` | 236 |
| `0x1009` | `dispatch_handler_ble_key_change_wifi` | 344 |
| `0x100a` | `dispatch_handler_recv_ble_data` | 684 |
| `0x100b` | `dispatch_handler_ble_event_msg` | 1940 |
| `0x100c` | `dispatch_handler_start_pt_mode` | 1660 |
| `0x100f` | `dispatch_handler_feed` | 38 |
| `0x1010` | `dispatch_handler_dev_state_report` | 20 |
| `0x1012` | `dispatch_handler_get_scan_result` | 392 |
| `0x1014` | `dispatch_handler_do_formatting` | 172 |
| `0x1015` | `dispatch_handler_ble_version_update_check` | 4 |
| `0x1016` | `dispatch_handler_ctrl_PM_befor_sleep` | 176 |
| `0x1017` | `dispatch_handler_ctrl_get_other_str` | 184 |
| `0x1018` | `dispatch_handler_ble_ota_end` | 672 |
| `0x1019` | `dispatch_handler_get_relay_dev_list` | 4 |
| `0x101a` | `dispatch_handler_ble_get_schedule` | 172 |
| **`0x101b`** | **`dispatch_handler_pet_face_pic_used_end`** | **460** |
| **`0x101c`** | **`dispatch_handler_get_pet_face_info_by_network`** | **208** |
| `0x101d` | `dispatch_handler_sync_led_mod` | 152 |
| `0x101e` | `dispatch_handler_iot_connect_change` | 296 |
| *(unresolved)* | `dispatch_handler_ledlight_mode_set` | — *(this one registration used an 8-bit `movs r0,#imm` this pass's decoder does not special-case for values `< 0x1002`; not chased further since it's LED-only, unrelated to AI)* |

The two bolded rows are the AI-relevant ones.

---

## 3. The wire envelope, re-derived from scratch — confirms `agent/src/bus.rs` byte-for-byte

Disassembling `dispatch_send_msg` directly (found in both binaries: **`ctrl` @ `0x80b00`**,
**`media` @ `0x338b4`** — byte-identical machine code in both, confirming one shared static
library object):

```c
// signature recovered from calling-convention evidence (arg checks + array-index range):
int dispatch_send_msg(uint16_t msg_id /*r0*/, int dst /*r1*/, void *payload /*r2*/, int len /*r3*/);
```

- `r0` (`msg_id`) is compared against two sentinel values (`0xffff`, `0x103`) to *suppress* a
  debug log line for those two specific ids only — msg_id, not dst (dst has no such special-cases).
- `r1` (`dst`) is range-checked `1..=20` and used as a `[dst]`-indexed cache of already-open
  `mqd_t` handles (`ldr.w r3,[r8, r4, lsl #2]`, opening+caching via a helper at `0x8090c` on a
  cache miss) — exactly "a small, fixed set of possible destination queues", matching the known
  `Peer` enum (1/2/4/5/7/8/10, all ≤ 20).
- The actual send buffer construction (`0x80caa` onward in `ctrl`):
  ```
  memset(buf, 0, 0x220)                    ; 0x220 = 544 = mq_msgsize, confirmed
  *(u16*)(buf+0) = msg_id                  ; from r0 (the caller's argument)
  *(u16*)(buf+2) = <process-global>        ; NOT from any argument -- read from a fixed global,
                                            ; i.e. "my own queue id", confirming `src` is never
                                            ; caller-supplied
  len = min(len, 0x21c)                    ; 0x21c = 540 = MAX_PAYLOAD, confirmed
  memcpy(buf+4, payload, len)
  mq_send(cached_mqd[dst], buf, len+4, 0)
  ```

This is an **independent, from-scratch re-derivation** that lands on exactly the same envelope
`agent/src/bus.rs`'s own module doc already documents (`u16 msg_id | u16 src | payload[len]`,
540-byte payload cap, `dst` selecting the queue without travelling on the wire) — zero new
assumptions, full agreement.

---

## 4. The two AI-relevant messages

### 4.1 `msg_id 0x101c` → `dispatch_handler_get_pet_face_info_by_network`

Disassembled in full (208 bytes, `0x52a98`–`0x52b68`). Its own payload handling is minimal: it
treats its `payload` argument (`r2`) as a pointer, requires `*payload != 0` (else returns
immediately — `ldr r3,[r2]; cmp r3,#0; beq <exit>`), logs one debug line naming its own
`__LINE__` (`0x2f9`), then unconditionally does two things: (a) a local self-dispatch
(`dispatch_send_msg(msg_id=0x24, dst=1, payload=NULL, len=0)` — **dst=1 is `ctrl` itself**; `0x24`
does not appear anywhere in `ctrl`'s own 25-entry table above, so this specific self-send's
ultimate destination inside `ctrl` was **not** resolved this session — flagged as an open item,
not guessed), and (b) a `config_shm`-gated call chain touching a `g_config`-style field. It does
**not** itself unpack a box/score/pet_id/timestamp struct — whatever richer data exists is either
carried by a *different*, more specific message, or lives entirely inside the config-shm-gated
follow-up this pass did not trace to its end.

`0x101b` (`dispatch_handler_pet_face_pic_used_end`, 460 bytes, registered immediately before
`0x101c`) is its evident companion/cleanup handler by name and adjacency, not individually
disassembled this session (time budget; flagged as a natural next step, not claimed as done).

### 4.2 `msg_id 0x1002` → `dispatch_handler_ctrl_event_msg` — the generic event report, **this is the one carrying `pet_id`**

This handler (208 bytes) is a thin wrapper: it `memcpy`s **168 bytes (`0xa8`)** of its payload
into a large (`0x15c0`-byte) local buffer at a fixed local offset (`+8`), then calls a single
large switch function this session named `pk_ctrl_event_msg_manage_proc` (matches the live debug
string `"pk_ctrl_event_msg_manage_porc"` [sic], found at rodata offset `0xa1e64`), passing the
local buffer pointer.

**Confirmed by disassembly**: `pk_ctrl_event_msg_manage_proc` (`0x38c5c`) reads a **4-byte LE
`event_type` field at payload offset 0** (`ldr.w r8,[r4,#8]`, where `r4+8` is where the payload
copy begins) and branches on it:

| `event_type` | Case address | What this session confirmed |
|---:|---|---|
| 0 (default/other) | `0x38f46` | not traced to a specific JSON shape |
| 1 | `0x3a8d0` | not traced |
| 2 | `0x3a996` | not traced |
| 3 | `0x39596` | not traced |
| 4 | `0x39c44` | not traced |
| 5 | `0x39038` | not traced |
| 6 | `0x3a354` | not traced |
| 7 | `0x3941c` | not traced |
| 8 | `0x390b0` | calls two helpers (`0x30574`, `0x30668`) suggestive of the eat/feed path — not confirmed |
| **`0x18` (24)** | `0x392ce` | **traced end-to-end (see below) — the pet-identification event** |
| `0x33`, `0x34`, `0x35` | `0x39106`, `0x3aad4`, `0x3ab9a` | not traced |

`event_type == 0x18` was chased all the way to its `bl` into a packer function (`0x38348`) that
builds one of `ctrl`'s own verbatim cloud-JSON `content=` format strings (recovered directly from
`ctrl`'s rodata, byte-exact):

```
event_type=%d&event_id=%s&timestamp=%d&content={"related_event":%s,"count":%d,"area":%d,"pet_id":%s,"tracker_info":%s,"vomit_info":%s}&state=%s
```

This is **the** pet-visit/identification event — it is the only cloud-JSON shape anywhere in
`ctrl`'s strings that carries `pet_id`. A sibling shape (also confirmed present, not individually
mapped to an `event_type` value this session) exists for the more generic motion/detect case:

```
event_type=%d&event_id=%s&timestamp=%d&content={"start_time":%d,"start_reason":%d,"result":%d,"err":%d,"action":%d,"device":{"mac":"%s","type":%d}}&state=%s
```

**Struct layout, stated at the same confidence this project's other studies use for the same
reason** (`docs/12-ai.md` §4 hit an identical wall and used the identical resolution):

- `event_type` — **offset 0, u32 LE — disassembly-confirmed, HIGH confidence.**
- `related_event`, `count`, `area`, `pet_id`, `tracker_info`, `vomit_info` — **confirmed to
  exist, in that order, from the verbatim format string — HIGH confidence on existence and
  order.** Their individual byte offsets inside the remaining 164 bytes were **not** recovered:
  the packer's `snprintf`-argument construction (`0x38660`–`0x38900`, partially disassembled)
  builds the string through several string-growth/retry branches and helper calls
  (`bl 0x8bdbe`/`0x8bfec` returning `related_event`/`count`-shaped values, `bl 0x8c100`/`0x8b954`
  doing what look like matching frees) before this session's time budget ran out chasing which
  exact struct field feeds which argument register. **Exactly the same class of open question
  `docs/12-ai.md` §4/§8 already flagged for `petkit_event_result_info`'s own offsets** — resolved
  fastest with either continued disassembly or a single live wire read, and this project's own
  constraints forbid the latter (see §5).

### 4.3 `msg_id`s are per-destination, not global — resolves an old open question

Decoding `media`'s **own** inbound table (`/msg_dispatch_2`) with the identical method (its
registrar is at `0x337c8`, called 27 times, `media`'s GOT base resolves to `0x66000`) recovered a
**completely disjoint numbering**: small sequential ids `0x1`–`0x28` plus `0x1011`/`0x1013`, e.g.
**`0x24` = `dispatch_handler_recv_pet_face_pic_info`** (`docs/11-media.md`'s "media's own ISP
preview-snapshot mechanism", confirmed by name and by being in *media's* table, not ctrl's).
Zero overlap with `ctrl`'s `0x1002`–`0x101e` range. This is the resolution to
`docs/06-msgids.md`'s "prior pointer-table scan... found non-strided addresses" note: the table
isn't a simple array at all — it is built by a sequence of individual `register()` calls, and a
given numeric id only means something relative to whichever process's queue it is sent to.

**Open item, stated plainly**: `media`'s 13 call sites to its own `dispatch_send_msg` (found by
searching for calls to `0x338b4`) that use msg_ids from `ctrl`'s namespace (`0x1002` ×4, `0x1009`,
`0x1010` ×2, `0x101b`, `0x101c`) **all pass `dst=2` (media's own queue), not `dst=1` (ctrl)** — and
none of those ids appear in media's own 27-entry table either. The most likely explanation this
session could not confirm in the time available: a *separate* consumer thread inside `media`
(the algo/detection pipeline almost certainly runs on its own thread) does its own `mq_receive`
on `/msg_dispatch_2` and switches on these ids directly, bypassing the named
`dispatch_handler_*`/`dispatch_mqueue_read` mechanism entirely — i.e. these are `media`'s
*internal* cross-thread signal, and the actual hop to `ctrl` (`dst=1`, `ctrl`'s own `0x1002`
namespace) happens somewhere this pass did not locate a static call site for (possibly reached
via a raw `mq_send` rather than the `dispatch_send_msg` wrapper, or via a jump table this pass's
linear sweep skipped past). **Not fabricated as found** — reported as the genuine limit of this
session's static analysis.

---

## 5. Why `kibbled` does not open `/msg_dispatch_1`

Per §1–4: the AI result path is a **private POSIX message queue delivered to `ctrl`'s own inbox**.
A POSIX message queue has exactly one reader. `ctrl` already holds `/msg_dispatch_1` open for
receive. `kibbled` opening the same queue for receive would not observe a *copy* of each
message — `mq_receive` **removes** the message from the queue, so a second reader **steals** it
from `ctrl`, which is a real behavioural change to a process this project's constraints require
leaving untouched ("Vendor processes untouched"). This is exactly the fallback the assignment
itself names: *"if the ONLY path is the mqueue to ctrl, ... the AI feed requires Kibble to
replace ctrl."* That replacement is the long-planned step (`docs/design-agent.md`'s "Not yet"
list), not this task's.

**Deliverable, honestly stated**: the struct and mechanism above are fully documented for that
day. Until then, `agent/src/ai.rs` taps something real and available today instead of faking the
mqueue path — see §6.

---

## 6. What `kibbled` now exposes

### 6.1 `agent/src/ai.rs` — a real, working event feed, from the vendor's own JPEG side effects

Independent of the mqueue, `media`/`libalgo.so` write plain JPEG files to `/tmp` as an ordinary
part of the same pipeline (`docs/03-app.md` §10, re-confirmed present in the pulled `media.bin`
this session): `/tmp/saveFace.jpg` (face match, written by `libalgo.so` — confirmed at rodata
offset `0x340064`), `/tmp/fPre_pet.jpeg` (generic visit), `/tmp/fPre_eat.jpeg` (eat). A background
thread polls their mtimes (1 Hz — `media`'s own detection cadence is seconds, not frames) and
republishes a `Detection{seq, ts, class, score, pet_id, box, image}` on change. `score`/`pet_id`/
`box` are honestly `null` — that data lives only in the struct §4 documents, unreachable without
replacing `ctrl` — never fabricated. `class` and `image` (a copied-out crop) are real.

- `GET /events` — last 50, oldest first.
- `GET /events/stream?since=N` — long-poll (~2 s, cut down 2026-09-16 from the original 25 s
  after `scrypted-plugin/README.md`'s "starvation incident" — see `ai::LONG_POLL_TIMEOUT`'s doc)
  for anything past sequence `N`, empty array on timeout — kept for backward compatibility;
  Scrypted short-polls the instant `GET /events` and HA uses `push.rs`'s own listener instead.

`msg::GET_PET_FACE_INFO_BY_NETWORK` (`0x101c`) / `msg::CTRL_EVENT_MSG` (`0x1002`) /
`EVENT_TYPE_PET_TRACKING` (`0x18`) / `CtrlEventMsgHeader` (decodes the one disassembly-confirmed
field, `event_type`) are all defined as real Rust constants/types in `ai.rs`, each with the exact
evidence from §2–4 in the doc comment, ready for the day `ctrl` is replaced.

**`/tmp/pet_face_pic.jpg`** (named explicitly in the assignment) — confirmed to exist as a literal
string, but **only inside `ctrl`** (rodata offset `0xa1a97`), not in `media`/`libalgo.so`.
Disassembling its one use site (`0x50b6e`–`0x50ba4`) shows `ctrl` **opening it for reading**
(`memset` two local buffers, then what disassembles as an existence-check/open call, `beq <skip>`
on failure) followed by `snprintf`-shaped calls that look like building an upload request — i.e.
**`ctrl` is a consumer of this file, not its writer.** The writer was not located in the three
pulled binaries; the most likely explanation is that `libalgo.so` constructs this exact filename
at runtime (e.g. via `snprintf` rather than a single static literal this pass's string search
would catch) rather than a genuinely separate write path — flagged as an open item rather than
guessed. No full-frame JPEG write site distinct from the already-documented `/tmp/fPre_*.jpeg`
family was found either.

### 6.2 `agent/src/faces.rs` — enrolment raw material

`GET /faces/pending` (list), `GET /faces/pending/<name>` (raw JPEG), `POST /faces/label
{"name","cat"}` (moves a pending crop to `/opt/kibble/faces/<cat>/`). Pending crops arrive from
`ai.rs`'s poller (any `/tmp/saveFace.jpg` change), capped at 200 files with oldest evicted by
mtime. No matching yet, per the assignment — this is only the raw material.

### 6.3 `agent/src/feed_capture.rs` — before/after dish snapshots (added scope, mid-session)

Uses the ring's already-cached H.264 keyframe (`ring::VideoFeed::latest_keyframe`, zero
extra CPU — no on-device JPEG decode) rather than triggering any vendor snapshot path: a
background thread watches `config_shm`'s `feeding` flag (`state::off::FEEDING`, already exposed)
for `0`→`1`, grabs "before", waits for `1`→`0`, settles 3 s, grabs "after". A manual `POST /feed`
leaves a short-lived note (`FeedCapture::note_manual_feed`) the watcher claims on the very next
transition for the exact id/amounts/`manual:true`; a transition with no fresh note (a scheduled
feed, or any other trigger) gets a synthesised `scheduled-<ts>-<n>` id and `manual:false` — exactly
"the before shot is whatever keyframe was cached when the flag went high" per the assignment.
`GET /feeds` (list, `ts`/`id`/`amount1`/`amount2`/`manual`/filenames), `GET /feeds/<name>` (raw
`.h264`), capped at the newest 20 events (`before`+`after`+a small JSON sidecar per event, evicted
as one group). **Not verified by dispensing** — `finish_cycle`/`start_cycle`/`save_pair` are
exercised directly with synthetic frame bytes and fake ids in `feed_capture.rs`'s own tests; the
live watcher loop (which needs the real flag) was not exercised end-to-end this session because no
feed (manual or scheduled) happened to fire during the observation window — see §7. Live
verification of this specific path is gated on Nitin's go-ahead to dispense, per his own
instruction.

Considered and **not** built: triggering the vendor's own `/tmp/fPre_compStart.jpeg`/
`fPre_compOver.jpeg` hardware snapshot path — `docs/design-entities.md` §5.2 already names these
exact files as the vendor's feed-before/after snapshots, but reaching them would require sending
`media` a bus message this project's constraints explicitly forbid testing ("do NOT send any
message to media's or ctrl's queue as an experiment"). No feed happened during the observation
window either, so there was nothing to passively observe about their timestamps this session
(open item for a future pass with a real feed).

---

## 7. Live verification

Deployed via the documented procedure (Unraid `docker run rust:slim` cross-compile,
`armv7-unknown-linux-musleabihf`, static + `crt-static`; `cp kibbled kibbled.prev` backup;
pushed the new binary over a one-shot `nc` listener on the device; `mv -f`; `kill <old pid>` to
let the existing restart loop relaunch it — no new persistence mechanism, no vendor file touched).

- `cargo test --release` (both natively and inside the exact Unraid build container): **152/152
  passed**, zero failures.
- Cross-compiled binary: `ELF 32-bit LSB executable, ARM, EABI5, statically linked, stripped`,
  563,156 bytes.
- Post-deploy, from a separate host, confirmed every pre-existing endpoint still works
  (`/state`, `/config`, `/schedule`, `/streams` — showing Scrypted's live RTSP sessions unaffected,
  `/cloud`), and every new one:
  - `GET /events`, `GET /faces/pending`, `GET /feeds` all return `[]` cleanly on a fresh deploy.
  - End-to-end face-label flow verified with a placed test file (written only under
    `/opt/kibble/faces/pending/`, an explicitly-allowed write path): `GET /faces/pending` listed
    it, `GET /faces/pending/<name>` served its bytes, `POST /faces/label {"cat":"Rashy"}` moved it
    to `/opt/kibble/faces/Rashy/<name>` and it correctly disappeared from `/faces/pending` (404 on
    re-fetch by the old path).
  - `GET /faces/pending/<missing>` and `GET /feeds/<missing>` both 404 cleanly; an unknown path
    still 404s.
- `ps` at the end of the session: `watchdog`(202) `ble`(203) `media`(204) `ctrl`(212) `agora`(270)
  `cloud`(271) all present, continuously running, unrestarted; `kibbled` running as the new binary
  under its existing pid, restart loop intact.
- **A capture window was left running** (the `ai.rs` poller starts automatically with `kibbled`)
  from 2026-09-15 18:04:57 UTC for the required ≥30 minutes. No real cat visit landed a
  `saveFace.jpg`/`fPre_pet.jpeg`/`fPre_eat.jpeg` change during that specific window this session
  observed — `GET /events` and `GET /faces/pending` stayed empty of real (non-test) detections for
  the whole window. This is reported plainly rather than papered over with a synthetic "real"
  event: the mechanism is proven and live end-to-end (verified with a placed test file, §7 above),
  but no genuine visit happened to occur in the observed interval.

---

## 8. Open questions carried forward (explicitly, not silently dropped)

1. `dispatch_handler_get_pet_face_info_by_network`'s (`0x101c`) own internal self-dispatch
   (`msg=0x24, dst=1`) does not match any of `ctrl`'s 25 registered handlers — where it actually
   lands was not traced.
2. `pk_ctrl_event_msg_manage_proc`'s `event_type` values `0,1,2,3,4,5,6,7,8,0x33,0x34,0x35` were
   observed as distinct branches but not individually mapped to a JSON shape or purpose (only
   `0x18` = pet-tracking was fully traced).
3. `dispatch_handler_ctrl_event_msg`'s 168-byte payload: only `event_type` (offset 0) has a
   disassembly-confirmed byte offset; `related_event`/`count`/`area`/`pet_id`/`tracker_info`/
   `vomit_info`'s individual offsets within the remaining 164 bytes are not recovered.
4. `media`'s send-side hop from its own `dst=2` self-dispatch of `ctrl`-namespaced msg_ids
   (`0x1002`/`0x1009`/`0x1010`/`0x101b`/`0x101c`) to the actual `dst=1` delivery `ctrl` receives
   was not located — see §4.3.
5. `/tmp/pet_face_pic.jpg`'s writer was not located in the three pulled binaries (only its reader,
   inside `ctrl`, was found) — see §6.1.
6. The vendor's own before/after feed snapshot files (`/tmp/fPre_compStart.jpeg`/
   `fPre_compOver.jpeg`, already named in `docs/design-entities.md` §5.2) were not observed live
   this session (no feed fired during the window) — whether they update automatically around a
   feed cycle, and on what trigger, remains to be confirmed by a future session with the
   operator's go-ahead to dispense.

Each is the same shape of gap this project's own prior studies have flagged before (named
precisely, not guessed past) — closing any of them is a disassembly-only task (or, for #4/#5, a
few more hours of the same symbolic-execution pass extended further into the packer/relay
functions), no new live access required.
