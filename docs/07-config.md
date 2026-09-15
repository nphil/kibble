# STUDY-config.md — `config_t` Shared-Memory Struct Layout (Petkit D4SH2)

Scope: map the 11,952-byte `config_t` struct backing `/dev/shm/config_shm`, mmap'd by every app
binary (`ctrl`, `ble`, `media`, `cloud`, `agora`, `logUpload`, `pktool`, `watchdog`) as `g_config`
(a `config_t *` global, confirmed in `.dynsym`, 4 bytes = pointer, not an embedded struct). Inputs:
`study/live/config_shm.bin` (11,952-byte live dump), `STUDY-app.md` §4 key schema, and static
analysis of `pktool`/`ctrl`/`ble` (copied locally, analyzed with `pyelftools` + `capstone`, then
deleted — see §6). **No live-device contact was made; this is 100% offline analysis of the existing
dump + binaries.**

## 1. Method

1. **Field-name/type recovery (pktool debug table).** `pktool`'s `.rodata` contains 456 debug
   format strings (228 unique fields × 2 variants — a plain `"[%s][%s][%s][%d]: g_config->KEY\t=
   FMT\n"` and an ANSI-colored duplicate) of the form `g_config-><path>`, each carrying a `printf`
   format specifier (`%lld`, `%llu`, `%.*s`, `%f`) that pins the field's **type**. This is the same
   evidence class STUDY-app.md §4 already used ("the debug macro itself proves these are the actual
   member paths"). Extracted **all 228** in their exact `.rodata` layout order and archived them as
   Table A (§7) — this is a strictly more complete version of STUDY-app.md §4's key list, in the
   compiler's own field order.
   - **Important negative result:** the function that actually consumes this table (backing
     `pktool get_config_info`) could not be located as reachable code. All four binaries are Thumb-2,
     linked as `ET_EXEC` but compiled `-fpie`-style (every non-trivial global/string reference goes
     through a runtime-computed `ldr r,[pc,#N]; add r,pc` pair, not a bare absolute literal or a
     GOT-offset arithmetic I could brute-force in bulk — see §6). A **per-string, per-callsite**
     resolution of this idiom is achievable (I did it successfully once, §6.2) but not economical
     for 228 sites without `objdump -R`/relocation-aware tooling, which is not installed on Unraid
     and could not be fetched offline. **Consequence: field order in Table A is compiler/print order,
     confirmed NOT to equal memory/offset order** (proved directly: `dev.dev_sn` is field #3 in the
     table, but its true value `<device-serial>` sits at byte offset 4768, deep into the struct, while
     the table's early fields — `dev.name`, `dev.dev_sn` — sit where offset-0-ish credential-shaped
     hex actually lives). Treat Table A as a **name+type dictionary**, not an offset map.
2. **Value anchoring in the dump.** Extracted every printable-ASCII run (offset, string) from
   `config_shm.bin`, and every nonzero 4/8-byte word, then matched them against **known ground-truth
   values** from the task brief (volume=6, desiccant=30d, firmware=895, ble_fw=159, serial, MAC, IP,
   tz=America/New_York, hopper sensor states, etc.) and against Table A's key names/types. This is
   the primary source for the offsets in `config_layout.json`.
3. **One successful disassembly-based confirmation.** For `ctrl`'s reachable (non-dead) debug line
   `"g_config->usr.wifi.conn_sta = %d"`, I reverse-engineered the PIC addressing idiom end-to-end
   (§6.2) and confirmed the format-string load; the actual field-value load traces through a
   register set outside the visible call-site window (deeper dataflow than budget allowed to
   finish). This validates the *methodology* is soundand transferable to a future pass with
   proper relocation-aware tooling, but did not itself yield a struct offset.

## 2. Section boundaries (dev / state / usr)

**Key finding: the physical memory layout does NOT match Table A's print order.** By value-anchoring:

| Approx. byte range | Section | Evidence |
|---|---|---|
| `0x0000` (offset 0) | top-level `loaded` flag | u32 = 1 |
| `~0x0058–0x1230` (91–4664) | **`usr.*`** (credentials, network, detection thresholds, app_conf) | `usr.iot_keys`/`usr.p2p_keys`/`usr.id_info` credential-shaped strings (offset ~91–568), `usr.server_info` MQTT hosts/IPs/DNS (601–2616), a large packed numeric block of `usr.ircut.*` + `usr.app_conf.{eat,move,pet,vomit}_det.*` thresholds (2856–3468), a ~280-byte run of `0xFFFFFFFF` sentinels consistent with `usr.app_conf.pet_color[i]` (no pets enrolled) (3168–3448), `usr.user_info.timezone` (float, offset 4384) + tz name string (4388), `usr.user_info.locale`/`userId` (4452–4548), and misc small `usr.*` scalars through ~4664 |
| `~0x1240–0x1330` (4672–4900) | **`dev.*`** (factory identity) | `dev.mac_info.*` MACs (4672, 4712), `dev.dev_sn` (4768, exact match to known serial), `dev.name` (4832, `"D4SH"`), `dev.version_info.ota_param.firmwareVer` (4860, string `"895"`) + `firmware_ble` (4876, int `159`) |
| `~0x1330–0x2EB0` (4900–11952) | **`state.*`** (dev_pro / ble / watchdog / online, all live-only) | T31 MCU OTA info (url/md5/size, 9132–9428, exact match to the known `ble.img` size 153412 and md5), a `state.watchdog` PID/heartbeat cluster (9728–9848, 5 live PIDs + exactly 2 zero slots matching the 2 "vestigial" processes `card`/`p2p` that have no binary — see STUDY-app.md §4), and a cluster of ~253,000–262,000-range counters (4892–7316) consistent with `state.dev_pro.{http_online_time_s,last_httpMsgTime,last_iotMsgTime}`-style multi-day uptime/connectivity seconds counters (device had been up ~2.9–3.0 days at capture) |


**Credential-cluster follow-up (id_info.dev_srt/srt_len — resolved for BleProtocolStudy's BLE-auth
writeup, 2026-09-15):** direct string extraction over bytes 0–700 (shape-classified, secret content
never printed) finds exactly **four tied `hex[32]`-shaped strings** at offsets **91, 124, 267, 536**
— matching the four hex/secret-shaped fields expected in this region (`id_info.dev_srt`,
`id_info.chip_id`, `p2p_keys.product_secret`, `iot_keys.device_secret`). Their *relative* order
matches Table A's leaf-level print order for `id_info` (`dev_id, srt_len, dev_srt, chip_id`) immediately
followed by `p2p_keys` then `iot_keys` — on the reasoning that, unlike the macro-level `dev/usr/state`
section reordering (§2 above, a human-authored display convenience), a flat leaf sub-struct's
auto/macro-generated field-walk has no reason to print out of physical declaration order. This gives
**offset 91 = `usr.id_info.dev_srt`, offset 124 = `usr.id_info.chip_id`, offset 267 =
`usr.p2p_keys.product_secret`, offset 536 = `usr.iot_keys.device_secret`** at **medium confidence**
(reasoned struct-order match, not disassembly-proven — the four candidates are shape-identical, so a
wrong assignment among them is possible without further evidence). `usr.id_info.srt_len` (and
`dev_id`) could **not** be pinned to an exact sub-offset: bytes 40–90 (immediately preceding
offset-91) are entirely zero, consistent with both scalars being 0 at capture time, but an all-zero
run has no distinguishing value to anchor either field to a specific 4/8-byte slot within it.
So the true section order in memory is **`usr` → `dev` → `state`**, not the `dev → usr → state`
order Table A's print sequence suggests — `dev` (factory identity) is a small, simple block sitting
*after* the much larger `usr` credentials/settings block, and `state` (all runtime/live telemetry)
occupies the back half of the struct, roughly the last 7KB.

**Struct-size sanity check:** 11,952 bytes / ~228 named fields ≈ 52 bytes/field average, consistent
with the observed mix (bulk 4–8 byte scalars plus several 16–256 byte credential/URL/hostname
buffers) — no field-count vs. struct-size contradiction.

## 3. Persisted vs. live

- `/opt/dev.conf` and `/opt/user.conf` string literals plus AES-256 (`AES_set_encrypt_key`,
  `AES_cbc_encrypt`) and `flock`/`MD5` imports are present in **all four** binaries checked
  (`pktool`, `ctrl`, `ble`, `media`) — confirms STUDY-app.md §5's finding is universal, not
  `watchdog`-only.
- **Inference from naming + section split** (not disassembly-confirmed — see Open Questions):
  `dev.conf` persists the `dev.*` section (factory identity — matches its small, rarely-changing
  content), `user.conf` persists the `usr.*` section (settings + cloud credentials — matches its
  much larger footprint and the presence of WiFi/MQTT credentials). The `state.*` section (all of
  `state.dev_pro`, `state.ble`, `state.watchdog`, `state.online`) is **not named in either file
  path** and is the section every binary's own `state.watchdog.*_pid` heartbeat table lives in —
  by construction this must be live-only (PIDs are only meaningful for the current boot), so at
  minimum `state.watchdog.*` cannot be file-persisted. Whether `state.ble.*`/`state.dev_pro.*` are
  also purely live (re-initialized to 0 each boot) or partially checkpointed to a file was not
  confirmed.

## 4. `flock(/tmp/config.lock)` usage

- All four binaries import `flock` and reference the literal path `/tmp/config.lock` in `.rodata`.
- Could **not** disassemble far enough to prove whether *read*-only accesses (e.g. `pktool
  get_config_info`, or a future HA poller doing a raw `mmap`+`memcpy` of `/dev/shm/config_shm`) take
  the lock, or whether only *write* paths (`config_save`/`dev.conf`/`user.conf` encrypt+write) do.
  `pktool` itself — which contains the read-only `get_config_info` dump path per STUDY-app.md §9 —
  does import `flock`, which is at least consistent with (but does not prove) readers also locking.
- **Practical recommendation for a read-only sensor poller:** `flock(LOCK_SH)` the same
  `/tmp/config.lock` before `memcpy`-ing `/dev/shm/config_shm`, even though it's unconfirmed whether
  the stock binaries do this for reads — the cost is negligible (a single non-blocking advisory
  lock) and it fully eliminates any risk of tearing a multi-word field mid-write by a stock writer
  that *does* take `LOCK_EX`. Do **not** hold the lock across anything except the single memcpy.

## 5. What a read-only sensor poller needs

1. Open `/dev/shm/config_shm` read-only, `mmap(PROT_READ, MAP_SHARED)`, exactly 11,952 bytes (fixed
   size — not resizable, confirmed by the dump's exact length).
2. `flock(LOCK_SH)` on `/tmp/config.lock` first (see §4 caveat), then read.
3. Poll via a plain re-`memcpy`/re-read (no inotify path found on `/dev/shm/config_shm` itself in
   this study) at whatever cadence the integration needs — sub-second polling is cheap since it's
   pure shared memory, no IPC round-trip.
4. Only the offsets in `config_layout.json` marked `confidence: "high"` or `"medium"` should be
   trusted for a first cut; treat `"low"` entries as structurally-plausible clusters requiring a
   second dump (e.g. before/after toggling one setting in the phone app) to confirm exact byte
   identity — which requires live-device access this study was barred from.

## 6. Disassembly notes (for a future pass with better tooling)

### 6.1 Why bulk offset-immediate search failed

All binaries are `ET_EXEC`, Thumb-2, `e_flags=0x5000400` (ARM EABI v5), yet **every** local
data/string reference goes through a runtime-computed two-instruction idiom:
```
ldr rX, [pc, #N]      ; literal pool holds a *relative delta*, not the absolute address
add rX, pc             ; rX = delta + PC ⇒ resolves to the real absolute address only at this point
```
This defeated a raw-bytes search for any target address (0/456 g_config-strings and the
`"get_config_info"` command string were found as literal 4-byte words anywhere in `.text`/`.rodata`/
`.data`/`.got`/`.data.rel.ro`), and defeated a `movw`/`movt`-immediate search (0/771 distinct
16-bit immediates matched either half of any target address). The binaries are *not* using GOT
slots for these (`.got` is only 720 bytes / 180 slots in `pktool` — far too small to hold even the
228 plain-variant string pointers, ruling out a real per-symbol GOT table for this data).

### 6.2 What worked

Bulk-computing, for **every** `ldr rX,[pc,#N]; add rX,pc` pair in a binary's `.text` (69,918 Thumb
instructions in `pktool`, 211,108 in `ctrl`), the resulting absolute value, then checking that set
for membership — this **does** find real call sites (confirmed for `ctrl`'s
`"g_config->usr.wifi.conn_sta = %d"` string, resolving to exactly one call site at `0x17e54`). This
is a valid, repeatable, but O(n) per binary pass (a few seconds each), not a targeted lookup, and
does not by itself resolve the *register that holds the config-relative offset* — the printed value
is loaded via a `ldr rX, [rBase]` a variable number of instructions earlier, through a register
whose GOT-indirected origin (`ldr r4, [r6, r0]` where `r0` is a *second* small pc-relative GOT-slot
index) requires tracing further backward per call site than the remaining time budget allowed.
**Recommendation:** re-run this with `radare2`/Ghidra (proper GOT-relocation-aware decompilation)
rather than raw capstone, which would resolve every one of the 228 Table A offsets directly from
`pktool`'s own `get_config_info` implementation *if* that function turns out to be reachable code
after all (unresolved — see Open Questions).

## 7. Struct coverage

`config_layout.json` has **55 entries** covering **4,345 of 11,952 bytes directly with offset
evidence (36.4%)** of the struct, spanning `confidence: "high"` (unique string/value match — device
identity, network, OTA info, timezone, PID cluster), `"medium"` (plausible structural match, exact
sub-field identity within a cluster unconfirmed), and `"low"` (region located, individual field
identity not confirmed). **Field *names and types* are known for ~95% of the struct's likely content**
via Table A (§8, 228 entries) — the gap is entirely in mapping those known names to their exact
byte offsets, which requires either live-device correlation (barred by this study's constraints) or
better relocation-aware disassembly (§6.2).

### `state.ble.*` telemetry — offsets (required deliverable)

**Not resolved to individual byte offsets.** All 27 `state.ble.*` fields in Table A
(`io_data.io_det`, `adc_data.{bat_ADC,moto_curr,power_ADC,proxl_rw,proxr_rw}`,
`sta_data.{err_code,err_data,OTA,ble_adv,bat_capac,food1_lack,food2_lack,led_powe,feed_sta,edting,
ubat}`, `moto_runt_data.{result,scram_reason,pos.delta_hall_time_ms,pos.hall_run_pos,sta,speed,
rt_curt,curt_max,mot_runtime,IR_trigger_flag,ctrl_ID}`) are almost certainly **all-zero or
idle-value in this dump** (device was not feeding at capture time, and per §2's ordering finding
these fields sit somewhere in the `state.*` back half of the struct, offset ≥ ~4900) — a value-based
search cannot distinguish an all-zero telemetry field from the struct's extensive zero-padding.
The one telemetry-adjacent thing *is* anchored: `food1_lack`/`food2_lack`'s expected values (0 / 1,
per the task brief: hopper1 sensor off, hopper2 on) could not be located among the 438 nonzero words
in the dump at all — meaning either both are genuinely 0 right now (hopper2's "on" binary_sensor
may reflect a *different*, HA-side-computed threshold rather than a literal 1 bit in this field), or
they live at an offset whose 1-byte value happens to coincide with a byte inside an adjacent
already-explained multi-byte value (an 8-bit field is much harder to rule out via "nonzero word"
scanning than a 4-byte one). **This is the single biggest open item** — see Open Questions.

## 8. Open Questions

1. **`state.ble.*` exact offsets** — needs either (a) a disassembly pass with relocation-aware
   tooling (radare2/Ghidra) tracing `ble`'s own MCU-telemetry-ingest function (which parses UART
   frames from the T31 and writes into `g_config->state.ble.*` — almost certainly reachable, live
   code, unlike `pktool`'s dump table), or (b) a second `config_shm.bin` capture taken *during* an
   active feed cycle, where `moto_runt_data.*`/`sta_data.feed_sta` would go transiently nonzero and
   become value-anchorable — both require live-device access this study was barred from.
2. **Is `pktool get_config_info`'s underlying dump function reachable code at all?** My search
   found zero evidence any of its 228 debug strings are referenced from executable code — either the
   feature is implemented through a completely different (e.g. table-driven, GOT-indirected in a way
   my per-string sweep didn't catch) code path, or it's dead code from a shared debug header whose
   `.rodata` survived link-time GC while its callers didn't. Needs a live `pktool get_config_info`
   invocation (barred) or better tooling to settle.
3. **Exact per-field identity within the low-confidence structural clusters** — the `usr.ircut.*` +
   `{eat,move,pet,vomit}_det.*` numeric block (2856–3468), the `state.dev_pro` connectivity-timer
   cluster (4892–7316), and the tail cluster (10152–10380, includes a value that looks like a
   struct/section checksum at `0x2878`) all have a located *region* but not a confirmed per-offset
   name. Confirmable only via a diffed second capture (toggle one setting, re-dump, diff) — barred.
4. **Which of the two ~1000-valued fields at 10168/10176 is `usr.trackerInterval` vs.
   `usr.trackerLimit`**, and which specific process PID (24789/25085/30237/30257/37088) maps to
   which of `{media,ctrl,agora,cloud,ble}` in the watchdog cluster — order inferred from binary
   inventory, not confirmed by symbol-level evidence.
5. **Read-vs-write `flock` semantics** (§4) — unconfirmed without deeper disassembly of
   `config_save`/`config_load`/`pktool get_config_info`'s call graph.
6. **dev.conf/user.conf ↔ struct-section mapping** (§3) is a naming-convention inference, not
   confirmed by disassembling the actual serialization routine (their AES-256 encryption meant even
   locating a plaintext boundary in the persisted files, which weren't part of this dump anyway, was
   out of scope).

## 9. Local cleanup

Local copies of `config_shm.bin`, `ctrl`, `ble`, `pktool`, `media`, and the scratch analysis venv/
scripts (`/tmp/petkit-cfg/`) are removed at the end of this study; no secret-bearing files were
written outside `/tmp/petkit-cfg` (deleted) and the two deliverables listed in the task contract.

---

## Appendix: Table A — full pktool debug-string field/type dictionary (228 entries, `.rodata` order)

Extracted from `pktool`'s `.rodata` (`"[%s][%s][%s][%d]: g_config-><path>\t= <fmt>\n"` literals).
**Order is compiler/print order, not memory offset order** (§1, §2). `%lld`/`%llu` fields print via
an integer cast (actual storage may be narrower than 8 bytes — not confirmed either way per field).
`%.*s` fields are precision-specified strings (fixed-size `char[]` buffers, exact buffer size not
captured by the format string itself). `usr.user_info.timezone` is the sole `%f` (float) field,
confirmed against the live dump (offset 4384, value −4.0 = EDT UTC offset for `America/New_York` in
September).

| # | Field path (g_config->) | printf format |
|---|---|---|
| 1 | `loaded` | `%lld` |
| 2 | `dev.name` | `%.*s` |
| 3 | `dev.dev_sn` | `%.*s` |
| 4 | `dev.pt_step` | `%lld` |
| 5 | `dev.version_info.ota_param.firmwareVer` | `%.*s` |
| 6 | `dev.version_info.ota_param.firmware_ble` | `%lld` |
| 7 | `dev.version_info.hardware.hardware_t31` | `%lld` |
| 8 | `dev.version_info.hardware.hardware_ble` | `%lld` |
| 9 | `dev.mac_info.a_APmac` | `%.*s` |
| 10 | `dev.mac_info.a_STAmac` | `%.*s` |
| 11 | `dev.mac_info.a_BLEmac` | `%.*s` |
| 12 | `dev.hw_param.ai_gain` | `%lld` |
| 13 | `dev.hw_param.ai_vol` | `%lld` |
| 14 | `dev.hw_param.ao_gain` | `%lld` |
| 15 | `dev.hw_param.ao_vol` | `%lld` |
| 16 | `dev.hw_param.ircut_inverse` | `%lld` |
| 17 | `dev.hw_param.ptz_X_inverse` | `%lld` |
| 18 | `dev.hw_param.ptz_Y_inverse` | `%lld` |
| 19 | `usr.accDomainTime` | `%lld` |
| 20 | `usr.log_level` | `%lld` |
| 21 | `usr.logSaveFlag` | `%lld` |
| 22 | `usr.ali_or_oci` | `%lld` |
| 23 | `usr.hertz` | `%lld` |
| 24 | `usr.id_info.dev_id` | `%lld` |
| 25 | `usr.id_info.srt_len` | `%lld` |
| 26 | `usr.id_info.dev_srt` | `%.*s` |
| 27 | `usr.id_info.chip_id` | `%.*s` |
| 28 | `usr.user_info.userId` | `%lld` |
| 29 | `usr.user_info.timezone` | `%f` |
| 30 | `usr.user_info.locale` | `%.*s` |
| 31 | `usr.user_info.language` | `%.*s` |
| 32 | `usr.pkg_service[i].name` | `%.*s` |
| 33 | `usr.pkg_service[i].start_time` | `%lld` |
| 34 | `usr.pkg_service[i].end_time` | `%lld` |
| 35 | `usr.pkg_service[i].cycle_time` | `%lld` |
| 36 | `usr.p2p_keys.product_id` | `%.*s` |
| 37 | `usr.p2p_keys.device_name` | `%.*s` |
| 38 | `usr.p2p_keys.product_secret` | `%.*s` |
| 39 | `usr.iot_keys.product_key` | `%.*s` |
| 40 | `usr.iot_keys.device_name` | `%.*s` |
| 41 | `usr.iot_keys.device_secret` | `%.*s` |
| 42 | `usr.iot_keys.region_id` | `%.*s` |
| 43 | `usr.iot_keys.mqttHost` | `%.*s` |
| 44 | `usr.iot_keys.id` | `%lld` |
| 45 | `usr.iot_keys.type` | `%lld` |
| 46 | `usr.iot_keys.createdAT` | `%lld` |
| 47 | `usr.wifi.conn_sta` | `%lld` |
| 48 | `usr.wifi.conf.ssid` | `%.*s` |
| 49 | `usr.wifi.conf.pwd` | `%.*s` |
| 50 | `usr.wifi.conf.uuid` | `%.*s` |
| 51 | `usr.wifi.net_inf.mac` | `%.*s` |
| 52 | `usr.wifi.net_inf.bssid` | `%.*s` |
| 53 | `usr.wifi.net_inf.gwmac` | `%.*s` |
| 54 | `usr.wifi.net_inf.ipaddr` | `%.*s` |
| 55 | `usr.wifi.net_inf.gw` | `%.*s` |
| 56 | `usr.wifi.net_inf.mask` | `%.*s` |
| 57 | `usr.wifi.net_inf.rsq` | `%lld` |
| 58 | `usr.wifi.net_inf.signal` | `%lld` |
| 59 | `usr.server_info.linked` | `%lld` |
| 60 | `usr.server_info.nextTick` | `%lld` |
| 61 | `usr.server_info.servers[0].api` | `%.*s` |
| 62 | `usr.server_info.servers[0].ip` | `%.*s` |
| 63 | `usr.server_info.servers[1].api` | `%.*s` |
| 64 | `usr.server_info.servers[1].ip` | `%.*s` |
| 65 | `usr.server_info.servers[2].api` | `%.*s` |
| 66 | `usr.server_info.servers[2].ip` | `%.*s` |
| 67 | `usr.server_info.dns[i].ip` | `%.*s` |
| 68 | `usr.ircut.d2n_iso` | `%lld` |
| 69 | `usr.ircut.lux_val` | `%lld` |
| 70 | `usr.ircut.cut_lux` | `%lld` |
| 71 | `usr.ircut.iso_offset` | `%lld` |
| 72 | `usr.ircut.n2d_iso` | `%lld` |
| 73 | `usr.ircut.n2d_cut_iso` | `%lld` |
| 74 | `usr.ircut.n2d_cut_gb` | `%lld` |
| 75 | `usr.ircut.n2d_gb_offset` | `%lld` |
| 76 | `usr.ircut.lock_time` | `%lld` |
| 77 | `usr.ircut.ir_off_time` | `%lld` |
| 78 | `usr.ircut.print` | `%lld` |
| 79 | `usr.ircut.version` | `%lld` |
| 80 | `usr.app_conf.irlight_enable` | `%lld` |
| 81 | `usr.app_conf.timestamp_enable` | `%lld` |
| 82 | `usr.app_conf.ledlight_enable` | `%lld` |
| 83 | `usr.app_conf.mic_enable` | `%lld` |
| 84 | `usr.app_conf.camera_enable` | `%lld` |
| 85 | `usr.app_conf.cameraMultiRange` | `%.*s` |
| 86 | `usr.app_conf.vedio_flip_enable` | `%lld` |
| 87 | `usr.app_conf.recording_type` | `%lld` |
| 88 | `usr.app_conf.lapseTime` | `%lld` |
| 89 | `usr.app_conf.lapseVideo` | `%lld` |
| 90 | `usr.app_conf.lapseEndTime` | `%lld` |
| 91 | `usr.app_conf.detectInterval` | `%lld` |
| 92 | `usr.app_conf.alarmTime` | `%.*s` |
| 93 | `usr.app_conf.move_det.algoEnable` | `%lld` |
| 94 | `usr.app_conf.move_det.trackEnable` | `%lld` |
| 95 | `usr.app_conf.move_det.sensitivity` | `%lld` |
| 96 | `usr.app_conf.move_det.alarmInterval` | `%lld` |
| 97 | `usr.app_conf.move_det.alarmTime` | `%.*s` |
| 98 | `usr.app_conf.move_det.allDayAlarm` | `%lld` |
| 99 | `usr.app_conf.move_det.notify` | `%lld` |
| 100 | `usr.app_conf.pet_det.algoEnable` | `%lld` |
| 101 | `usr.app_conf.pet_det.trackEnable` | `%lld` |
| 102 | `usr.app_conf.pet_det.sensitivity` | `%lld` |
| 103 | `usr.app_conf.pet_det.alarmInterval` | `%lld` |
| 104 | `usr.app_conf.pet_det.alarmTime` | `%.*s` |
| 105 | `usr.app_conf.pet_det.allDayAlarm` | `%lld` |
| 106 | `usr.app_conf.pet_det.notify` | `%lld` |
| 107 | `usr.app_conf.eat_det.algoEnable` | `%lld` |
| 108 | `usr.app_conf.eat_det.trackEnable` | `%lld` |
| 109 | `usr.app_conf.eat_det.sensitivity` | `%lld` |
| 110 | `usr.app_conf.eat_det.alarmInterval` | `%lld` |
| 111 | `usr.app_conf.eat_det.alarmTime` | `%.*s` |
| 112 | `usr.app_conf.eat_det.allDayAlarm` | `%lld` |
| 113 | `usr.app_conf.eat_det.notify` | `%lld` |
| 114 | `usr.app_conf.feedPicture` | `%lld` |
| 115 | `usr.app_conf.eatVideo` | `%lld` |
| 116 | `usr.app_conf.soundEnable` | `%lld` |
| 117 | `usr.app_conf.systemSoundEnable` | `%lld` |
| 118 | `usr.app_conf.feedSound` | `%lld` |
| 119 | `usr.app_conf.selectedSound` | `%lld` |
| 120 | `usr.app_conf.factor1` | `%lld` |
| 121 | `usr.app_conf.factor2` | `%lld` |
| 122 | `usr.app_conf.foodWarn` | `%lld` |
| 123 | `usr.app_conf.foodWarnRange` | `%.*s` |
| 124 | `usr.app_conf.lightMode` | `%lld` |
| 125 | `usr.app_conf.lightMultiRange` | `%.*s` |
| 126 | `usr.app_conf.toneMode` | `%lld` |
| 127 | `usr.app_conf.toneMultiRange` | `%.*s` |
| 128 | `usr.app_conf.manualLock` | `%lld` |
| 129 | `usr.app_conf.CTime` | `%lld` |
| 130 | `usr.app_conf.surplusControl` | `%lld` |
| 131 | `usr.app_conf.surplusStandard` | `%lld` |
| 132 | `usr.app_conf.smartFrame` | `%lld` |
| 133 | `usr.app_conf.vomit_det.algoEnable` | `%lld` |
| 134 | `usr.app_conf.pet_color[i].petId` | `%lld` |
| 135 | `usr.app_conf.pet_color[i].petColor` | `%lld` |
| 136 | `usr.app_conf.upload` | `%lld` |
| 137 | `usr.app_conf.log_upload` | `%lld` |
| 138 | `usr.app_conf.attireId` | `%lld` |
| 139 | `usr.app_conf.logo_cn` | `%lld` |
| 140 | `usr.mtu` | `%lld` |
| 141 | `usr.rpt_batV` | `%lld` |
| 142 | `usr.trackerLimit` | `%lld` |
| 143 | `usr.trackerInterval` | `%lld` |
| 144 | `usr.bind.step` | `%lld` |
| 145 | `usr.bind.code` | `%lld` |
| 146 | `state.dev_pro.power_on_src` | `%lld` |
| 147 | `state.dev_pro.cvr_indate` | `%lld` |
| 148 | `state.dev_pro.event_indate` | `%lld` |
| 149 | `state.dev_pro.lapse_indate` | `%lld` |
| 150 | `state.dev_pro.cycleTime` | `%lld` |
| 151 | `state.dev_pro.time_synchronized` | `%lld` |
| 152 | `state.dev_pro.irlight_mode` | `%lld` |
| 153 | `state.dev_pro.whitelight_mode` | `%lld` |
| 154 | `state.dev_pro.isp_mode` | `%lld` |
| 155 | `state.dev_pro.lapse_rec_state` | `%lld` |
| 156 | `state.dev_pro.last_httpMsgTime` | `%lld` |
| 157 | `state.dev_pro.last_iotMsgTime` | `%lld` |
| 158 | `state.dev_pro.ota_sta.ota_curr_sta` | `%lld` |
| 159 | `state.dev_pro.ota_sta.error_code` | `%lld` |
| 160 | `state.dev_pro.bowl_food_quantity` | `%lld` |
| 161 | `state.dev_pro.leftover` | `%lld` |
| 162 | `state.dev_pro.full_update_flag` | `%lld` |
| 163 | `state.dev_pro.toneTimeAllow` | `%lld` |
| 164 | `state.dev_pro.pt_mode` | `%lld` |
| 165 | `state.dev_pro.pt_wifi` | `%lld` |
| 166 | `state.dev_pro.stop_cloud` | `%lld` |
| 167 | `state.dev_pro.feed_time` | `%lld` |
| 168 | `state.dev_pro.bind_out_time` | `%lld` |
| 169 | `state.dev_pro.bind_timeout` | `%lld` |
| 170 | `state.dev_pro.in_the_banding` | `%lld` |
| 171 | `state.dev_pro.entry_bind_time` | `%lld` |
| 172 | `state.dev_pro.ble_open_by_bind` | `%lld` |
| 173 | `state.dev_pro.first_linked` | `%lld` |
| 174 | `state.dev_pro.err_bind_step` | `%lld` |
| 175 | `state.dev_pro.ble_open_by_key` | `%lld` |
| 176 | `state.dev_pro.aging_info.aging_enter_f` | `%lld` |
| 177 | `state.dev_pro.aging_info.aging_working_f` | `%lld` |
| 178 | `state.dev_pro.aging_info.aging_start_flag` | `%lld` |
| 179 | `state.dev_pro.sensor_reset` | `%lld` |
| 180 | `state.dev_pro.online_mode` | `%lld` |
| 181 | `state.dev_pro.http_online_time_s` | `%lld` |
| 182 | `state.dev_pro.acc_domain` | `%lld` |
| 183 | `state.online.step` | `%lld` |
| 184 | `state.dev_pro.device_camera_enable` | `%lld` |
| 185 | `state.dev_pro.tmp_devCamera_enable` | `%lld` |
| 186 | `state.dev_pro.feed_less_replay` | `%lld` |
| 187 | `state.ble.io_data.io_det` | `%llu` |
| 188 | `state.ble.adc_data.bat_ADC` | `%llu` |
| 189 | `state.ble.adc_data.moto_curr` | `%llu` |
| 190 | `state.ble.adc_data.power_ADC` | `%llu` |
| 191 | `state.ble.adc_data.proxl_rw` | `%llu` |
| 192 | `state.ble.adc_data.proxr_rw` | `%llu` |
| 193 | `state.ble.sta_data.err_code` | `%llu` |
| 194 | `state.ble.sta_data.err_data` | `%llu` |
| 195 | `state.ble.sta_data.OTA` | `%llu` |
| 196 | `state.ble.sta_data.ble_adv` | `%llu` |
| 197 | `state.ble.sta_data.bat_capac` | `%llu` |
| 198 | `state.ble.sta_data.food1_lack` | `%llu` |
| 199 | `state.ble.sta_data.food2_lack` | `%llu` |
| 200 | `state.ble.sta_data.led_powe` | `%llu` |
| 201 | `state.ble.sta_data.feed_sta` | `%llu` |
| 202 | `state.ble.sta_data.edting` | `%llu` |
| 203 | `state.ble.sta_data.ubat` | `%llu` |
| 204 | `state.ble.moto_runt_data.result` | `%lld` |
| 205 | `state.ble.moto_runt_data.scram_reason` | `%lld` |
| 206 | `state.ble.moto_runt_data.pos.delta_hall_time_ms` | `%lld` |
| 207 | `state.ble.moto_runt_data.pos.hall_run_pos` | `%lld` |
| 208 | `state.ble.moto_runt_data.sta` | `%lld` |
| 209 | `state.ble.moto_runt_data.speed` | `%llu` |
| 210 | `state.ble.moto_runt_data.rt_curt` | `%llu` |
| 211 | `state.ble.moto_runt_data.curt_max` | `%llu` |
| 212 | `state.ble.moto_runt_data.mot_runtime` | `%llu` |
| 213 | `state.ble.moto_runt_data.IR_trigger_flag` | `%llu` |
| 214 | `state.ble.moto_runt_data.ctrl_ID` | `%llu` |
| 215 | `state.watchdog.media_count` | `%lld` |
| 216 | `state.watchdog.ctrl_count` | `%lld` |
| 217 | `state.watchdog.p2p_count` | `%lld` |
| 218 | `state.watchdog.agora_count` | `%lld` |
| 219 | `state.watchdog.cloud_count` | `%lld` |
| 220 | `state.watchdog.ble_count` | `%lld` |
| 221 | `state.watchdog.card_count` | `%lld` |
| 222 | `state.watchdog.media_pid` | `%lld` |
| 223 | `state.watchdog.ctrl_pid` | `%lld` |
| 224 | `state.watchdog.p2p_pid` | `%lld` |
| 225 | `state.watchdog.agora_pid` | `%lld` |
| 226 | `state.watchdog.cloud_pid` | `%lld` |
| 227 | `state.watchdog.ble_pid` | `%lld` |
| 228 | `state.watchdog.card_pid` | `%lld` |