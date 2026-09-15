# STUDY-config-encryption.md — `/opt/user.conf`/`/opt/dev.conf` content is encrypted, not a struct mirror

**Date:** 2026-09-15
**Scope:** live measurement against the running device (192.168.4.85), superseding the
"presumably AES-encrypted (MEDIUM confidence)" language in `15-settings-write.md` §4 and the
open question in `07-config.md` §8 item 6. This is the answer to both: **content is confirmed
encrypted, not a plaintext copy of any part of `config_shm`, and the key/IV were not recovered.**

## 1. Why this needed checking

`docs/15-settings-write.md` §4 disassembled `config_save()` down to the exact `fopen`/`fwrite`
sequence and confirmed the on-disk layout is `<32-byte lowercase-hex MD5 of content><content>`,
but could not confirm from static analysis alone whether `content` is a plaintext copy of the
`config_shm` section being saved, or the AES-encrypted output the same binary's own
`AES_set_encrypt_key`/`AES_cbc_encrypt` imports suggest (`07-config.md` §3). A settings-write
implementation that assumes "plaintext, same layout as config_shm" and is wrong would silently
corrupt every other setting and credential in the file the moment it tries to patch one field and
rewrite the MD5 header — this needed a live answer before any code touched the file, not an
inference.

## 2. Method

Live-pulled both files off the device with the device's own `busybox nc` (no new tooling
installed, no filesystem-mutating command run): a one-shot Python TCP listener on `beastnas`
(`192.168.1.69`, LAN-reachable from the feeder's IoT VLAN per `07-config.md`'s own network notes)
receives what the device pushes with `nc <listener> <port> < /opt/user.conf`. Read-only on the
device end (`wc -c`, `nc` as a client), no service touched, no process signalled.

```
wc -c < /opt/user.conf          # 2600
nc -w 5 192.168.1.69 5002 < /opt/user.conf
```

## 3. The header format is confirmed correct

```python
header, content = data[:32], data[32:]
hashlib.md5(content).hexdigest() == header.decode()   # True, for the live user.conf pull
```

`/opt/user.conf`: **header validates** (`211f75b7c5504709b86b00d2b692c03d` == `md5(content)`,
content length 2568 bytes). This confirms `15-settings-write.md` §4's disassembly-derived layout
is exactly right at the envelope level — any settings-write implementation can trust
`<32-hex-char MD5><content>` as the outer format.

`/opt/dev.conf` (376 bytes, content 344 bytes): header does **not** validate against its
content's MD5 (unexplained; the file's mtime is `Dec 7 2025`, long before this session, so it may
predate a format change, or `dev.conf`'s header covers something other than the raw remainder —
not chased further since Kibble's settings work has no field in the `dev.*` section). Flagged here
for whoever next touches `dev.conf`, not resolved.

## 4. The content is not a struct mirror — it's encrypted

Searched the live `config_shm` dump for every known-good field value from the settings-write
table (`night`=1, `light`=0, `camera`=1, `microphone`=1, `volume`=6 — the last matching this
study's own previously-cited "known ground-truth value", independently confirming the `volume`
offset is correct) as raw little-endian bytes, and for the 824-byte window spanning the entire
`usr.app_conf` settings cluster (offsets 3064–3888), against `/opt/user.conf`'s content. **Zero
matches, down to 4-byte windows.**

Shannon entropy of `user.conf`'s content: **7.915 of a possible 8.0 bits/byte** — indistinguishable
from a good cipher or PRNG output, and completely unlike the low-entropy mix of small integers,
zero padding and ASCII strings a plaintext `config_t` section actually contains (`07-config.md` §2
describes exactly that low-entropy shape for the real struct). `dev.conf`'s content measures 7.364
bits/byte, same conclusion. Not gzip/zlib (neither's magic bytes appear at the front); consistent
with AES-CBC or another block cipher, matching the `AES_set_encrypt_key`/`AES_cbc_encrypt` imports
`07-config.md` §3 already found in this binary family. Content length (2568 bytes) is not itself a
multiple of the 16-byte AES block size, so either a short unencrypted prefix precedes the
ciphertext or a stream mode (CTR/CFB/OFB, no padding) is in use — not pinned further, see §6.

Both files' content begins with the identical 8-byte sequence `55 aa f1 e2 d3 c4 b5 a6`, suggesting
a fixed magic/IV rather than one randomised per save (both files were saved independently, at
different times, yet share this prefix byte-for-byte).

## 5. Practical consequence for Kibble

**Kibble does not hand-construct `/opt/user.conf` content.** The key/IV/mode needed to do that
safely were never recovered by any study in this repo (`07-config.md` §8 Open Question 5 already
flagged this as unresolved; this pass upgrades "unresolved" to "confirmed necessary and still
unrecovered" for anyone considering the reimplement-the-file-format path) and recovering them is a
standalone reverse-engineering project (tracing `AES_set_encrypt_key`'s key-material argument back
to its source in `ctrl`), not something a settings read/write feature should gate on.

Kibble's actual design (`agent/src/persist.rs`, `agent/src/desired.rs`): write the setting into
live `config_shm` (every vendor process reads the same shared mapping, confirmed effective and
immediate), record the desired value in Kibble's own plaintext `/opt/kibble/settings.json`, and
re-apply every recorded value both at startup and on an ongoing timer. This needs no vendor key,
degrades safely (a value that drifts gets corrected, not silently lost), and — if Kibble is ever
removed — leaves the device exactly as the vendor's own app last configured it, since Kibble never
touched that file. See `agent/src/persist.rs`'s module doc and the project report for the live
durability measurements (does `ctrl`, or the still-enabled Petkit cloud path, ever revert a
Kibble-set value, and how the reconciler responds).

## 6. Still unknown / next steps

1. **AES key/IV/mode.** Not attempted here — deliberately out of scope (§5). Whoever next needs
   real `/opt/user.conf` write access would trace `ctrl 0x81e98`'s (`AES_set_encrypt_key`) key
   argument back to its source.
2. **Exact content framing** (fixed prefix length before block-aligned ciphertext, vs. a
   non-block-aligned stream mode) — only the length arithmetic in §4 was checked, not the cipher
   itself.
3. **`dev.conf`'s non-validating header** (§3) — separate question, not chased.

---

Cross-referenced from `07-config.md` §3/§8 and `15-settings-write.md` §4.1/§7 item 5.
