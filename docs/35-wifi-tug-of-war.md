# STUDY-wifi-tug-of-war.md — Vendor/Kibble Wi-Fi reselect fight: root cause, and the reset_wifi.sh neutralization (2026-09-17)

**Status: a root-cause write into the vendor's own store (making it agree with Kibble on the SSID)
was investigated and found not provably safe — documented in §3 as a next step, not attempted.
The actual dominant disruption turned out to be a *different*, fully disassembly-traced mechanism:
`ctrl`'s own `wifi_monitor_timer` calls `/app/script/reset_wifi.sh` roughly every 180s on its own
*healthy* code path (§5) — not a failure response, and not something writing a `config_shm`
field can safely neutralize (§6). It is fixed in `agent/src/resetguard.rs` (§7) by bind-mounting a
no-op over the vendor's read-only script. Separately, `agent/src/wifi.rs`'s reconciler was made
non-disruptive regardless of which mechanism causes a given reselect (settled-state-only reactions,
a redrift cooldown, and DHCP-refresh gating).**

## 1. What actually runs at boot — live script content, not the prior assumption

`agent/src/wifi.rs`'s own module doc previously asserted `/tmp/wpa_supplicant.conf` "gets fully
regenerated from the vendor's own stored credentials by its `wifi_connect.sh` on every boot." That
claim was never disassembly- or live-verified — it was a reasonable-sounding inference. Pulling
both live copies of the script in full corrects it:

```sh
# /app/script/wifi_connect.sh (live, in full)
...
WPA_CONF=/tmp/wpa_supplicant.conf
SYS_WPA_CONF=/opt/wpa_supplicant.conf
DEFAULT_SCR=/app/script/default.script

echo "kill wpa_supplicant"
killall -9 wpa_supplicant
if [ -f "$WPA_CONF" ]; then
    wpa_supplicant -D nl80211 -i wlan0 -c $WPA_CONF -B
else
    wpa_supplicant -D nl80211 -i wlan0 -c $SYS_WPA_CONF -B
fi

sleep 1
echo "kill udhcpc"
killall -9 udhcpc
if [ -f "$DEFAULT_SCR" ]; then
    udhcpc -i wlan0 -b -s $DEFAULT_SCR
else
    udhcpc -i wlan0 -b
fi
```

`/soc/scripts/wifi_connect.sh` (the earlier-loaded, pre-`/app`-mount copy) is a near-duplicate,
differing only in its fallback path (`/soc/scripts/wpa_supplicant.conf`, a static factory template
inside the read-only `soc.img` squashfs) and using plain `killall` instead of `killall -9`.

**Neither script contains any decryption, credential-derivation, or `config_shm` access at all.**
Both just pick between a tmpfs path and a static fallback, then launch `wpa_supplicant` pointed at
whichever exists. There is no regeneration step here to write into.

## 2. What the live device actually shows

- `/opt/wpa_supplicant.conf` **does not exist** on this device (`ls` -> `No such file or
  directory`). The app-tier fallback path in §1 is therefore currently unreachable.
- The **currently running** `wpa_supplicant` process's real argv, read straight from
  `/proc/<pid>/cmdline`: `wpa_supplicant -D nl80211 -i wlan0 -c /tmp/wpa_supplicant.conf -B`. It is
  using the tmpfs path.
- `/tmp/wpa_supplicant.conf` is tmpfs — it cannot survive a real reboot — yet the device boots with
  a working Wi-Fi config every time. Something populates it fresh, every boot, before either script
  in §1 runs (or as part of an earlier attempt this session's evidence didn't capture directly).

Given `/opt/user.conf`'s confirmed AES-256 encryption (`21-config-encryption.md`) is the only place
the vendor's real, working Wi-Fi password is durably stored, and neither `wifi_connect.sh` copy
touches it, the only mechanism consistent with every fact above is: **`ctrl` decrypts
`/opt/user.conf` at its own startup and writes `/tmp/wpa_supplicant.conf`'s real content itself**
(or drives `wpa_supplicant` to the same effect over the control socket), before handing off to
`wifi_connect.sh`. This is inferred, not disassembly-proven — `ctrl`'s own Wi-Fi-provisioning code
path was not traced this session — but it is the only explanation that fits (a) the encrypted
store being the sole durable source of the real password, (b) neither script decrypting anything,
and (c) the file being tmpfs yet always populated correctly at boot.

## 3. Why a root-cause write into the vendor's store was not attempted

Task-requested question: can Kibble write the vendor's own Wi-Fi credentials so `wifi_connect.sh`
regenerates network 0 as `BEAST_ROUTER`? Three candidate targets were considered, in order of how
close each sits to "the thing that actually determines network 0's identity":

1. **`/opt/user.conf` directly (the encrypted on-flash store).** Re-confirmed live this session,
   independently of `21-config-encryption.md`: `/opt/kibble/user.conf.bak` (a byte-identical
   snapshot from that earlier study, `wc -c` = 2600, matching the live `/opt/user.conf`'s current
   2600 bytes) hex-dumps to the *exact* 32-hex-char MD5 header (`211f75b7c5504709b86...`) and the
   *exact* fixed 8-byte content prefix (`55 aa f1 e2 d3 c4 b5 a6`) that document cites. The AES
   key/IV remain unrecovered. **Not attempted**: a hand-constructed ciphertext would pass the outer
   MD5-of-content check while silently corrupting every other credential and setting in the file —
   exactly the risk `persist.rs`'s whole design (`config_shm` writes + Kibble's own plaintext
   desired-state file, never touching this file) exists to avoid.
2. **`/opt/wpa_supplicant.conf` or the live `/tmp/wpa_supplicant.conf`.** Both are plaintext, so
   technically writable with no crypto blocker. But per §2, the app-tier static path doesn't even
   exist/isn't in use, and the live tmpfs copy is (almost certainly) rebuilt by `ctrl` from the
   encrypted store on every boot regardless of what's on disk beforehand — a write here would not
   survive a reboot, and there is no live evidence for whether `ctrl`'s own periodic Wi-Fi
   health-check re-reads that file's content or only acts against `wpa_supplicant`'s already-loaded
   in-memory network list (untested — doing so would mean directly editing the vendor's own network
   id 0 in place, which this project's own established convention (`VENDOR_NETWORK_ID`: "Kibble
   ... never rewrites ... id 0") deliberately avoids, and which was not verified safe this session).
3. **`config_shm`'s live `usr.wifi.conf.ssid`/`usr.wifi.conf.pwd` fields**, mirroring exactly how
   `persist.rs` already safely re-applies every other Kibble-owned setting without touching the
   encrypted file. This is the *theoretically correct* target — if `ctrl` consults it live, this
   would make both sides agree with no flash write and no vendor process touched, and (like every
   other `persist.rs`-owned field) could be re-applied at every Kibble startup so it also survives
   a reboot. **Not attempted**: unlike every other field `settings.rs` ships as `writable: true`,
   this field's exact byte offset was never confirmed. `07-config.md` §1 item 3 traced the one
   disassembly attempt made against a neighbouring field
   (`"g_config->usr.wifi.conn_sta = %d"`) and explicitly could not resolve it to a struct offset in
   the time available; `usr.wifi.conf.ssid`/`.pwd` were never attempted at all. `settings.rs`'s own
   documented policy is that "getting one field's offset wrong would silently corrupt its
   neighbour" — guessing here, on a live device actively feeding a real animal, is exactly the risk
   that policy exists to prevent.

**Conclusion: none of the three candidates is both reachable and provably safe this session.**
Path (3) is the one worth pursuing for real — see "Next steps" below.

## 4. The honest boot-time behaviour

The device **always boots onto the vendor's own network first** (`ctrl`/`wifi_connect.sh` win the
boot race by construction — Kibble does not run until well after), then Kibble's own
`boot_reapply` switches it to `BEAST_ROUTER` once `wpa_supplicant`'s control socket is up (bounded
by `SOCKET_WAIT_MAX` + `CONNECT_TIMEOUT`, so typically under a minute). This is unchanged by this
session's work and is not fixable without the path in §3 landing — it is stated here plainly per
the assignment's own request, not left implicit.

## 5. What is actually driving the *repeated* reselects — ctrl's own `reset_wifi.sh`, not a hardware fault

Live `dmesg` on this device, independently corroborating a finding `StreamFluidity` (running a
concurrent RTSP-fluidity investigation on the same device) reported from timestamp-correlated
capture stalls:

```
[16213.503554] usb 1-1: USB disconnect, device number 51
[16214.951151] usb 1-1: New USB device found, idVendor=0bda, idProduct=f72b ...
[16214.977703] usb 1-1: Product: 802.11n  WLAN Adapter
[16402.560489] usb 1-1: USB disconnect, device number 52
[16404.006190] usb 1-1: New USB device found ...
[16591.939181] usb 1-1: USB disconnect, device number 53
...
```

The RTL8733BU USB Wi-Fi adapter disconnects and re-enumerates on a consistent **~189-190 second
period, continuously**. This was *initially* (wrongly) read as a kernel/USB hardware fault
independent of `ctrl`. Disassembling `/app/bin/ctrl` (`/tmp/ctrl_full.bin`, md5
`c645c0665da2cf73db93ffa8d9d0ea68`, confirmed live-matching via `md5sum /app/bin/ctrl` — capstone +
pyelftools + `arm-linux-gnueabihf-objdump -M force-thumb`, all available in this environment)
corrects that: **the disconnect *is* `reset_wifi.sh` running**, not a separate cause.

### 5a. The exact mechanism

`reset_wifi.sh` (`/app/script/`, pulled live, 380 bytes, in full):
```sh
#!/bin/sh
killall -9 wpa_supplicant
killall -9 udhcpc
ifconfig wlan0 down
WIFI_PIN=/sys/class/gpio/gpio60
if [ ! -d "$WIFI_PIN" ]; then echo 60 > /sys/class/gpio/export; fi
echo out > /sys/class/gpio/gpio60/direction
echo 0 > /sys/class/gpio/gpio60/value
/app/script/wifi_connect.sh $1 $2
```
It powers the radio off (GPIO60) and hands off to `wifi_connect.sh`, which itself does its own
off/1s/on/1s power-cycle — matching the observed ~1.4-1.5s gap between "USB disconnect" and
"New USB device found" exactly.

`ctrl` calls this from three sites inside a `wifi_ctrl.c`-sourced cluster (~0x2d744-0x2f649:
`FsmConnectWifi`/`FsmFailRetryWifi`/`run_wifi_fsm`/`wifi_monitor_timer`, all real function/state
names still present in `.dynsym` despite the binary otherwise being `strip`ped), each building
`"/app/script/reset_wifi.sh mtu %d &"` via `snprintf` then executing it with `popen(cmd, "r")`
(confirmed at file offset `0x8ec5a`).

### 5b. The dominant trigger is the *healthy* path, gated by a `config_shm` state word — not a failure

`wifi_monitor_timer`'s health-check helper (`0x2e3b4`) is entirely local: `popen()`-wrapped
`ifconfig | grep eth0` / `ifconfig | grep wlan0` / `wpa_cli -i wlan0 status` (checked via `strstr`
for `wpa_state=COMPLETED`, falling back to `SCANNING` + `wpa_cli -i wlan0 mib | grep
dot11RSNA4WayHandshakeFailures`) — **no hostname, socket, or cloud/MQTT probe anywhere in it.**
When association is genuinely `COMPLETED` *and* a follow-up MAC-address readback also succeeds, it
returns code 8 (or 9 for eth0, not applicable — this device has none). `wifi_monitor_timer` remaps
both 8 and 9 to a local `r5 = 7`, and the actual reset call at file offset `0x2f210` fires only when
**all** of these hold (traced at `0x2f154`-`0x2f228`):

1. `r5 == 7` (i.e. the *healthy* result above).
2. `byte@(g_config+2860) != 0`.
3. `word@(g_config+10120) == 0xFFFFFFFF` exactly.
4. `byte@(g_config+9949, 0x26dd) == 0` (this is the same flag noted in §1/module docs of
   `wifi.rs` — it only chooses a 20s-vs-180s threshold below, it does not gate whether the reset
   fires at all).
5. `>= 180s` (`> 179`) since a reference timestamp, else it takes a harmless `run_wifi_fsm(4)`
   branch instead.

Live-read (`od`, read-only, `/dev/shm/config_shm`, at the moment of this study): `byte@2860 = 1`,
`byte@9949 = 0`, `word@10120 = 0xFFFFFFFF`. **All three side-conditions are permanently satisfied
on this unit right now.** The only real gate left is the 180s timer — this is a scheduled/proactive
cycle riding on the *healthy* code path, not a reaction to anything Kibble or the cloud-blackhole
does directly (see §6 for what does drive it).

Side-effects of this specific call path, checked so a fix does not blind anyone to something else:
two `AX_SYS_LogPrint` calls and an internal `run_wifi_fsm(4)` state transition — no LED, no BLE
advertising call, no other `config_shm` write visible in this block. The other two call sites
(inside the pre-`run_wifi_fsm` static helper and the `connect_wifi` region) were not traced to the
same depth (time-boxed) but build the identical command through the identical `popen` wrapper, so
neutralizing the script neutralizes all three regardless.

Cross-referencing with Kibble's own reconcile log over one ~33-minute `kibbled` uptime window:
only **one** `drifted ... re-selecting` pair was logged against roughly ten 180s cycles in that
window — most cycles land `wpa_supplicant` back on whatever was last `save_config`'d as enabled
(in practice `BEAST_ROUTER`, since `/tmp/wpa_supplicant.conf` survives a mere interface replug)
with no Kibble intervention needed; the vendor's own network is only actually left selected on the
rarer cycles that race Kibble's `RECONCILE_INTERVAL` (60s) unfavourably.

## 6. `config_shm+10120`: what it is, and why writing it was rejected in favour of the bind-mount

Offset 10120 sits in `07-config.md`'s `state.*` section (4900-11952: live-only telemetry, never
persisted to the encrypted store) — *not* the `usr.ircut.*`/`usr.app_conf.*_det.*` threshold
cluster at 2856-3468 that offset 2860 (the *other* gating field, left untouched) sits inside.
Writer search (`movw r*, #10120` immediately followed by a `str`, whole-binary scan): three sites,
all inside one function region (~0x27de8-0x28090, one linker-kept symbol away from `do_online_stop`,
adjacent to an `iot_service_start`-labeled routine at `0x1b5e4`):

- `0x27f6c`: `g_config[10120] = 0xFFFFFFFF`, always paired with `g_config[10192] = 0`, taken only
  when a preceding call returns negative (`cmp r0,#0; bge -> skip`) — a failed connect/start.
- `0x27e1e`: `g_config[10120] = 2` — taken when a status query (`bl 0x1b5e4`) is *not* already 2 or
  3 — reads as "marking connecting/(re)starting".
- `0x28080`: `g_config[10120] = 1` — the sibling branch when `g_config[10192] == 0`.

This is a small connection-state enum (not a timestamp): `0xFFFFFFFF` = failed/never-connected,
1/2 = some connecting/connected distinction. With the cloud deliberately blackholed, every attempt
this logic makes returns negative, so it is permanently re-stamped back to `0xFFFFFFFF` — this is
almost certainly **`ctrl`'s own Petkit-cloud/IoT (MQTT) connection-state marker**, invalidated by
the very blackhole this project depends on, which is what keeps §5b's gate permanently armed.

**Why this was not turned into the fix despite being the precise, named root condition:** the same
word is read at 13+ *other* sites scattered across a large, untraced code region (~0x32000-0x39000),
every one gating `cmp r3,#1; bne <skip>` before doing (shape-consistent with, not confirmed)
per-property MQTT push work. Writing `1` there to satisfy §5b's gate would also open every one of
those 13+ gates, and this session did not trace what they do or whether *their* resulting attempts
against the blackholed cloud have their own failure path back to `reset_wifi.sh` — that is real,
unverified risk on a live device this session was not willing to take on the same night. Writing
`2` (the value `ctrl` itself uses for "connecting", which does not appear among the 13+ `==1`
checks) was also considered, but `0x27f6c`'s failed-connect path would keep re-stamping it back to
`0xFFFFFFFF` on every one of `ctrl`'s own cloud-connect attempts — Kibble would be in a permanent
rewrite fight against `ctrl`'s own logic for no benefit over just neutralizing the script.

## 7. The fix: `agent/src/resetguard.rs`

`/app` is confirmed read-only squashfs (`mount`: `/dev/loop1 on /app type squashfs (ro,relatime)`),
so `reset_wifi.sh` cannot be edited in place, and this project's rule is to never touch vendor
flash contents regardless. `resetguard::install()` (called once, early, from `main()`, alongside
`backup::backup_once()`) bind-mounts a no-op script over it:

```sh
#!/bin/sh
echo "$(date +%s) ctrl requested reset_wifi.sh $*" >> /opt/kibble/reset_wifi_suppressed.log
exit 0
```

`ctrl`'s `popen("/app/script/reset_wifi.sh mtu %d &", "r")` still succeeds — the GPIO/kill/restart
side effects just never happen. The suppressed-request log is kept (not silently discarded)
specifically so the one diagnostic signal `reset_wifi.sh` used to carry — that `ctrl` thought a
reset was warranted — is still observable if a genuinely wedged radio ever needs one for real.

Idempotent across Kibble restarts (checked against `/proc/mounts` before mounting again — a bind
mount outlives the process that created it) and re-applied fresh every Kibble startup, before
`ctrl`'s first 180s tick could otherwise fire.

**Manual restore, from the device itself, no reboot needed:**
```sh
umount /app/script/reset_wifi.sh
```
This immediately restores the vendor's own script — the bind mount was the only thing shadowing
it, and nothing on flash was ever written. A genuine device reboot also reverts it on its own (a
bind mount never survives one); Kibble simply re-establishes it on its next startup unless this
module is also removed.

## 8. Also found, out of scope for this session

`wifi.rs`'s `spawn_reconciler` calls `scan_json()` — which triggers a real `wpa_cli scan` (an
actual radio channel-hop away from the associated AP) plus a 2s settle — unconditionally on every
`RECONCILE_INTERVAL` (60s) tick, purely to diff-and-push `WifiScan` to HA, whether or not anything
is listening. This is a real, periodic, unconditional radio disruption independent of everything
else in this document. Flagged for `StreamFluidity`'s fluidity report and a possible follow-up;
not one of this session's assigned fixes, so left untouched.

## 9. Next steps

1. **Trace the 13+ `config_shm+10120 == 1` consumer sites** (~0x32000-0x39000) properly before
   anyone writes to that offset — §6 is the blocker on the more surgical fix.
2. **Name `do_online_stop`'s actual callers** and the `0x1b5e4` (`iot_service_start`-adjacent)
   status codes 2/3, to confirm the "connecting/connected" reading of values 1/2 with certainty
   rather than shape-inference.
3. If (1) comes back clean (no other consumer misbehaves on a stale `== 1`), a periodic write of a
   plausible non-`0xFFFFFFFF` value to `config_shm+10120` (same `/tmp/config.lock`-guarded mmap
   write `persist.rs` already uses, tmpfs — no flash wear) would disable §5b's gate at its own
   source and could replace the bind-mount — kept as a documented possible upgrade, not attempted
   this session.
4. Root-cause SSID agreement (making the vendor's own store say `BEAST_ROUTER`) remains as
   described in §3 — separately motivated (it would also fix the *rare* genuine reselects that
   still occasionally race `RECONCILE_INTERVAL`), still blocked on the same unresolved `usr.wifi.*`
   offset and the unrecovered `/opt/user.conf` AES key.

---

Cross-referenced from `agent/src/wifi.rs` and `agent/src/resetguard.rs`'s module docs, `02-boot.md`
§3c/§4f, `07-config.md` §2, and `21-config-encryption.md` §4/§5. USB flap correlation credited to
`StreamFluidity`'s concurrent RTSP-fluidity investigation on the same device; disassembly review
prompted by `Main`'s live-evidence update and follow-up questions.

