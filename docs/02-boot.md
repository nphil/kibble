# STUDY-boot.md — Petkit D4SH2/D4H2 Boot Chain, Persistence, Update/Verification, Watchdog

Offline static analysis performed on Unraid against the read-only backup at
`/mnt/nvme/appdata/petkit-d4sh2-backup/parts/*.img` and the extracted study
tree at `/mnt/nvme/appdata/petkit-d4sh2-study/`. No connection was made to
the live device (192.168.4.85) at any point. All commands ran over
`tailscale ssh root@beastnas`; no bytes under the backup directory were
written.

## 1. uImage headers

All four `/opt/*.img` payloads (`app.img`, `soc.img`, `alg.img`, `audio.img`)
in both the current (`raw/opt/`) and backup (`raw/bak/`) copies are standard
U-Boot `mkimage` **uImages**: 64-byte big-endian header
(`>7I4B32s`: magic, hcrc, time, size, load, ep, dcrc, os, arch, type, comp,
name) immediately followed by the payload, no trailing bytes.

Verified by recomputing both CRCs in python (`zlib.crc32`) and comparing to
the stored fields — **every file matched on both `hcrc` and `dcrc`**:

| file | name field | os/arch/type/comp | payload size | hcrc/dcrc match |
|---|---|---|---|---|
| raw/opt/app.img | `app_D4H2_D4SH2-262667` | linux/**mips**/filesystem/none | 1,724,416 | ✅/✅ |
| raw/opt/soc.img | `soc_D4H2_D4SH2-253703` | linux/**mips**/filesystem/none | 5,316,608 | ✅/✅ |
| raw/opt/alg.img | `alg_D4H2_D4SH2-262606` | linux/**mips**/filesystem/none | 18,108,416 | ✅/✅ |
| raw/opt/audio.img | `audio_D4H2_D4SH2-253502` | linux/**mips**/filesystem/none | 962,560 | ✅/✅ |
| raw/bak/app.img | `app_D4H2_D4SH2-254201` | linux/mips/filesystem/none | 1,400,832 | ✅/✅ (older app version, confirms `/bak` really is a prior version, not a duplicate) |
| raw/bak/soc.img | `soc_D4H2_D4SH2-253703` | linux/mips/filesystem/none | 5,316,608 | ✅/✅ (byte-identical to opt copy) |
| raw/bak/audio.img | `audio_D4H2_D4SH2-253502` | linux/mips/filesystem/none | 962,560 | ✅/✅ (byte-identical to opt copy) |

**Curiosity**: the `arch` field in every one of these headers reads **mips**,
not arm, even though the live device is confirmed `armv7l`. Since `type=filesystem`
(not `kernel`), U-Boot itself never validates this field for these images —
they're mounted as plain squashfs by the app-layer scripts, not booted — so
this is almost certainly a stale/boilerplate value baked into the vendor's
build/packaging tooling (possibly reused from an older or shared Axera SDK
image-signing script) rather than a real architecture mismatch. Flagged as
an open question, not corrected.

`raw/opt/version` confirms the numeric suffixes are exactly the `*_ver` /
`*_ble` fields from the JSON: `app_ver=262667`, `soc=253703`,
`alglib_ver=262606`, `audio_ver=253502`, matching the uImage `name` fields
exactly.

## 2. Kernel/uboot/spl/ddrinit partitions — NOT uImages, NOT extractable offline

`backup/parts/{kernel,kernel_b,uboot,uboot_b,spl,ddrinit}.img` do **not**
start with the U-Boot magic (`27 05 19 56`). Instead all six share a
common proprietary 64-byte header:

```
offset 0-3:   per-image tag (differs per file/content — e.g. kernel=f2d1a805, uboot=85610b07, spl=f13bd7ca, ddrinit=da14ff32)
offset 4-11:  22 33 54 55 fe fa 54 00   (constant across every image — vendor magic/version tag)
offset 12-15: little-endian u32, plausibly a payload-size field (kernel=4,961,640; uboot=352,480; spl=40,596; ddrinit=16)
offset 16-41: mostly zero (spl has extra non-zero fields here — likely multi-segment SPL)
offset 44-63: 00 08 00 02 b5 3c 21 35 8f 9a 51 b9 86 e3 4e 5f 5d 67 65 35 …  (byte-for-byte IDENTICAL across all 6 files regardless of content — fixed template/padding, not data-dependent)
```

`update_uboot.sh` (see §4) independently confirms the header length is
exactly **64 bytes**: it strips the image with
`dd if=/tmp/uboot.img of=/tmp/uboot.bin bs=64 skip=1` before `nandwrite`.

This is **not** a standard U-Boot construct. Per `AxeraScout` (parallel
research on the SoC vendor): public docs for the same Axera chip family
(M5Stack LLM630/AX630C docs) explicitly state *"Due to the special nature
of the Axera firmware format, it does not conform to the standard U-Boot
boot item, making it almost impossible to use standard U-Boot boot
operations."* — i.e. this is a known, deliberately proprietary,
undocumented header format, not something we failed to recognize.

**The payloads themselves are almost certainly AES-encrypted, not merely
compressed**, based on:

- Zero valid compression-magic hits. All 88 occurrences of the 2-byte gzip
  magic (`1f 8b`) found by brute-force scanning `kernel.img` failed to
  decompress with `zlib`/gzip (100% failure rate — consistent with random
  2-byte magic collisions in high-entropy data, not real gzip streams). No
  `xz`/`lzma`/`cpio`/`squashfs` magic appears anywhere in `kernel.img`,
  `uboot.img`, or `spl.img` either.
- Block-entropy scan (64 KiB blocks) of `kernel.img`'s logical payload
  (bytes 64…4,962,664 — the last non-`0xFF`-padding byte) measures
  **7.98–7.997 bits/byte throughout**, i.e. statistically indistinguishable
  from random data end-to-end. `uboot.img`'s payload measures ~7.94–7.99
  bits/byte similarly. A merely-*compressed* (not encrypted) stream would
  still show a recognizable magic at its start and would decompress; this
  does neither.
- Zero real ASCII strings recovered anywhere in the payload (7,901 "hits"
  from a `[\x20-\x7e]{6,}` regex, but every one is garbled high-entropy
  noise, not text — no kernel banner, no `Linux version`, no file paths).
- By contrast, `ddrinit.img`'s payload (only 976 bytes long, offset 64 to
  logical end 1040) measures **entropy 0.185 bits/byte** — a small,
  low-entropy, structured (unencrypted) blob, consistent with a plain DRAM
  PHY/controller register-init table that the boot ROM/SPL needs in the
  clear before it can even initialize DRAM to decrypt anything else. `spl.img`'s
  payload is intermediate (~6.37 bits/byte in its first 64 KiB) — plausibly
  a small amount of real ARM startup code plus an encrypted/compressed tail.

**Consequence for §3 below: `study/fs/initramfs/` was NOT populated.** The
kernel image (which per `rdinit=/linuxrc` and the observed live mount
(`/` is a 45,220 KiB ramdisk, not the UBI "rootfs" partition) must contain
a `CONFIG_BLK_DEV_INITRD`-style embedded initramfs) is encrypted end-to-end
with no recoverable key in this offline environment. This is a hard
cryptographic blocker, not a tooling gap — extraction was attempted via
(a) uImage-header parsing (fails: wrong magic), (b) compression-magic
scanning + decompression at every candidate offset (fails: 0/88 valid
streams), (c) direct cpio-magic (`070701`) scanning of both the raw and
attempted-decompressed bytes (fails: no hits), before concluding this.

Overhead accounting: for both `kernel.img` and `uboot.img`, the file's
logical end (last non-`0xFF` byte) minus the 64-byte header minus the
offset-12 size field leaves exactly **960 bytes** unaccounted for in both
cases — i.e. header(64) + trailer(960) = 1024 bytes of fixed overhead
around the encrypted payload in both images. Plausibly a signature/MAC
block (e.g. an RSA-2048 signature at 256 bytes plus padding, or similar),
but the exact cryptographic primitive is not determinable without the
vendor's toolchain. Open question.

## 3. Boot chain (indirect reconstruction — linuxrc/inittab/rcS not readable)

Because the kernel/initramfs are encrypted, `/linuxrc`, `/etc/inittab`, and
`/etc/init.d/rcS` **cannot be read**. The boot chain below is reconstructed
from everything that *is* readable: the live `/proc/cmdline` given as known
fact, the plaintext U-Boot environment (below), and the app/soc-layer
scripts that run once the encrypted early boot has finished and handed
control to userspace.

### 3a. U-Boot environment — plaintext, CRC32-protected, redundant

Unlike the SPL/kernel/uboot images, `env.img` and `env_b.img` (MTD
partitions `env`/`env_b`, 512 KiB each) are **not** encrypted — they are
the standard U-Boot redundant-environment binary format: 4-byte CRC32 (LE)
+ 1 flags byte + NUL-terminated `key=value` pairs + double-NUL terminator.
Both decoded and CRC-verified successfully:

- `env.img`: flags byte `0x0e` (14)
- `env_b.img`: flags byte `0x0f` (15) — the higher counter, i.e. `env_b` is
  U-Boot's currently-preferred copy in the redundant-env scheme.

Contents (identical between the two copies except `ota_info`, see below):

```
baudrate=115200
bootargs=mem=96M console=ttyS0,115200n8 loglevel=8 earlycon=uart8250,mmio32,0x4880000 board_id=0xb,boot_reason=0x00,noinitrd rdinit=/linuxrc mtdparts=spi4.0:1M(spl),512K(ddrinit),1M(uboot),1M(uboot_b),512K(env),512K(env_b),6M(kernel),6M(kernel_b),4M(param),-(rootfs)
bootcmd=axera_boot
bootdelay=0
fdtcontroladdr=4fbc2688
stderr=serial
stdin=serial
stdout=serial
```

`bootcmd=axera_boot` is a single, opaque, vendor-supplied U-Boot command
compiled into the (encrypted) `uboot.img`. This is the actual boot chain
implementation — SPI-NAND-to-DRAM decrypt/load logic, `boot_reason`
detection, and kernel A/B slot selection — and it is **not visible to this
analysis**. Comparing the env default (`boot_reason=0x00`, no `kernel=`
param) against the live cmdline given in the task
(`boot_reason=0x04,...,kernel=a version=254604`) confirms `axera_boot`
patches/appends `boot_reason` and `kernel=a|b` (plus `version=`) onto
`bootargs` at runtime before jumping to the kernel — those two tokens are
absent from the static env.

### 3b. `ota_info` — the one piece of boot-relevant vendor state we CAN read

Both env copies carry a structured `ota_info` variable, a flat per-component
state machine (confirmed by `pktool`'s exported `pk_OTA_info_load` /
`pk_OTA_info_save` symbols, so this is loaded/saved by name, not by luck):

```
env:    ota_info=sta:2,err:13,bakt:0,bakl:0,bakc:0,klc:0,kcc:0,kvc:0,rlc:0,rcc:0,rvc:0,alc:0,acc:0,avc:0,klo:0,kco:0,kvo:0,rlo:0,rco:0,rvo:0,alo:0,aco:0,avo:0
env_b:  ota_info=sta:0,err:0, bakt:0,bakl:0,bakc:0,klc:0,kcc:0,kvc:0,rlc:0,rcc:0,rvc:0,alc:0,acc:0,avc:0,klo:0,kco:0,kvo:0,rlo:0,rco:0,rvo:0,alo:0,aco:0,avo:0
```

Field-prefix decode (`k`=kernel, `r`=rootfs, `a`=alg; each with `l`=length,
`c`=crc, `v`=version; suffix `c`=current, `o`=old): this tracks
length/CRC/version for kernel, rootfs, and alg across a "current" and "old"
generation, plus a `sta`/`err` overall state and a `bak{t,l,c}` general
backup descriptor. **On this specific unit every one of those fields is
zero** — kernel/rootfs/alg have never been through this A/B path.
`env` (the older-flags copy) shows a stale `sta:2,err:13` while `env_b`
(newer-flags copy) shows a clean `sta:0,err:0` — read together with the
redundant-env flags counter, this is consistent with a **previous failed
operation being recorded, then the environment being safely rolled
forward to a clean state** (the mechanism the redundant-env format exists
to protect against: power loss mid-write). `err:13`'s exact meaning
(CRC mismatch vs. size vs. something else) is not recoverable from env
alone — no decoder for it was found in the readable app-layer strings.

### 3c. What definitely runs after the encrypted stage hands off

`/soc/scripts/system_init.sh` (squashfs, `soc.img`) is the first
userspace-visible mount/link logic recovered, and it self-identifies via
its own echo as running from `/opt/system_init.sh` — i.e. it is copied or
symlinked to `/opt` and invoked from there very early, before `/app` exists:

```sh
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:/app/bin:/app/usr/bin:/app/scripts:/soc/bin:/soc/scripts:/usr/local/bin"
export LD_LIBRARY_PATH='/soc/usr/lib:/soc/lib:/app/bin:/syslib/lib:/app/lib:/opt/syslib:/alg'

APP_PATH="/opt/app.img"
APP_BACKUP_PATH="/bak/app.img"
APP_LINK_PATH="/opt/linkapp.img"
APP_INIT_PATH="/app/script/app_init.sh"

# Ensure /app directory exists
if [ ! -d "/app" ]; then
    mkdir -p /app || { echo "Error: Unable to create the /app directory"; exit 1; }
fi

# Create symlink if not exists
if [ ! -e "$APP_LINK_PATH" ]; then
    if [ -f "$APP_PATH" ]; then
        ln -s "$APP_PATH" "$APP_LINK_PATH"
    elif [ -f "$APP_BACKUP_PATH" ]; then
        ln -s "$APP_BACKUP_PATH" "$APP_LINK_PATH"
    else
        echo "Error: No application image found in $APP_PATH or $APP_BACKUP_PATH"
        exit 1
    fi
fi

# Mount the application image
if [ -L "$APP_LINK_PATH" ] || [ -f "$APP_LINK_PATH" ]; then
    mount -t squashfs -o offset=64 "$APP_LINK_PATH" /app
    if [ $? -ne 0 ]; then
        echo "Error: Mounting $APP_LINK_PATH failed"
        if [  -e "$APP_BACKUP_PATH" ]; then
            cp "$APP_BACKUP_PATH" "$APP_PATH"
            sync
            reboot
        fi
        exit 1
    fi
fi

# Run init script if exists
if [ -f /opt/app_init.sh ]; then
    /opt/app_init.sh &
    exit 0
elif [ -f "$APP_INIT_PATH" ]; then
    "$APP_INIT_PATH" &
fi
```

Key facts this establishes:
- **No CRC/MD5/signature check is performed before mounting `/app`.** The
  only "integrity check" is whether `mount -t squashfs` itself succeeds
  (i.e. a valid squashfs superblock at offset 64) — a corrupted-but-still-
  superblock-valid image would mount and only fail later, deeper inside
  the app.
- **Corruption recovery**: if the mount fails and `/bak/app.img` exists,
  the script overwrites `/opt/app.img` from `/bak/app.img` and
  **unconditionally reboots** (`cp; sync; reboot`) to retry.
- The `/opt/linkapp.img -> /opt/app.img` symlink (already observed live)
  is created here, and would point at `/bak/app.img` instead if the
  primary is missing at boot.
- `/audio` and `/alg` are mounted by `app/script/app_init.sh` (below), not
  here — `/soc` itself is presumably mounted by the (unreadable)
  linuxrc/rcS before this script can even run, since this script lives
  inside `soc.img`.

`app/script/app_init.sh` (runs after `/app` is mounted, presumably invoked
by whatever the (unreadable) linuxrc/rcS chain calls next, or chained from
`system_init.sh`'s `app_init.sh &`):

```sh
/app/script/clean_ntpd.sh &
rm -rf /etc/localtime
...
if [ -f /soc/lib/8733bu.ko ]; then
    insmod /soc/lib/8733bu.ko
elif [ -f /opt/lib/8733bu.ko ]; then
    insmod /opt/lib/8733bu.ko
else
    echo "8733bu.ko not found!"
    exit 1
fi

fcrc /opt/audio.img
if [ $? -ne 0 ]; then
    rm /opt/audio.img
fi
if [ -f /opt/audio.img ]; then
    mkdir -p /audio 2>/dev/null
    mount -t squashfs -o offset=64 /opt/audio.img /audio
fi

fcrc /opt/alg.img
if [ $? -ne 0 ]; then
    rm /opt/alg.img
fi
if [ -f /opt/alg.img ]; then
    mkdir -p /alg 2>/dev/null
    mount -t squashfs -o offset=64 /opt/alg.img /alg
fi

if [ -f "$SYS_WPA_CONF" ];then
    /app/script/wifi_connect.sh &
fi

export LD_LIBRARY_PATH='/soc/usr/lib:/soc/lib:/app/bin:/app/lib:/alg'
/app/script/app_start.sh &
```

This is the **actual integrity check for `/audio` and `/alg`**: `fcrc`
(a binary not present in any of our extracted trees — it is not under
`app/bin`, `soc/bin`, or `soc/scripts`; it must live in the unreadable base
rootfs/busybox environment) is run against the uImage; on a non-zero exit
the image is deleted outright (relying on the *next* boot's "file doesn't
exist" branch to simply skip mounting that subsystem, rather than any
repair). `fcrc` clearly understands **both** uImage headers (used here on
plain-uImage `audio.img`/`alg.img`) **and** the proprietary Axera header
(used by `update_uboot.sh`, §4, on `uboot.img`) — it must contain a format
sniffer that dispatches on the magic bytes.

`app/script/app_start.sh` is the final process launch (confirms known fact,
quoted in full):

```sh
cd /app/bin/
./watchdog &
./ble &
./media &
sleep 1
./ctrl &
sleep 1
./agora &
./cloud &
./logUpload &
```

### 3d. GPIO / factory-reset / kernel A/B — see §4

No dedicated "factory reset button" or GPIO-polling script was found in
`app/script/` or `soc/scripts/`; GPIO usage found is all WiFi-power
(`gpio60`) and camera/IR-LED control (referenced only in `pktool`
strings — `enable ircut`, `enable irlight`, `enable white light`, etc., no
script wrapper). `boot_reason` handling and `kernel=a|b` slot selection are
implemented entirely inside the encrypted `axera_boot` U-Boot command; the
**write side** of the A/B mechanism (how a new kernel/uboot image gets
committed) is fully visible in the update scripts — see §4/§4c.

## 4. Update / OTA scripts (`app/script/*.sh`, all 13 read in full)

There are **two structurally distinct, non-overlapping update mechanisms**
in this firmware:

1. **Raw-MTD A/B** (kernel, u-boot) — write to the *inactive* MTD partition,
   then flip `fw_setenv bootsystem`. Tracked (in principle) by `ota_info`.
2. **UBI-file overwrite** (app, soc, audio, alg) — overwrite the file
   in-place inside the `/opt` UBI volume, with a single backup copy kept
   in `/opt/backup/` or `/bak/`. No A/B, no env involvement, no
   `ota_info` interaction. `rootfs` itself has a *third*, seemingly
   unused/inconsistent path (§4c).

### 4a. `update_kernel.sh` — raw MTD A/B, quoted in full

```sh
kernel_param=$(grep -o 'kernel=[ab]' /proc/cmdline | cut -d'=' -f2)

case "$kernel_param" in
    a)
        CP_PATH="/dev/mtd8"    # cp to partition B
        BOOT_PART="B"          # set boot partition B
        ;;
    b)
        CP_PATH="/dev/mtd7"    # cp to partition A
        BOOT_PART="A"          # set boot partition A
        ;;
    *)
        echo "partition err: $kernel_param"; exit 1 ;;
esac

cd /tmp
[ -f kernel.img ]        || { echo "kernel.img does not exist!"; exit 1; }
[ -f kernel.img.md5 ]    || { echo "kernel.img.md5 does not exist!"; exit 2; }
md5sum -c kernel.img.md5 | grep -q OK || { echo "MD5 check failed!"; exit 3; }

flash_eraseall "$CP_PATH"        || { echo "flash_eraseall failed!"; exit 4; }
nandwrite -p "$CP_PATH" kernel.img || { echo "nandwrite failed!"; exit 5; }

fw_setenv bootsystem "$BOOT_PART" || { echo "fw_setenv failed!"; exit 6; }
exit 0
```

Reads the **currently booted** slot straight off `/proc/cmdline`
(`kernel=a` or `kernel=b`, the exact token `axera_boot` appends at boot —
confirming §3a's inference), targets the *other* raw MTD device
(`kernel`↔`kernel_b` = `/dev/mtd7`↔`/dev/mtd8`), verifies an MD5 sidecar
file dropped in `/tmp` (`kernel.img.md5`, matching the assignment's known
fact about `/tmp/%s.img.md5` sidecars) before touching flash, then commits
the slot flip via `fw_setenv bootsystem A|B` — this is the actual
mechanism that would eventually make `axera_boot` select the other
partition (the env var `bootsystem` is not itself present in either env
copy we decoded, meaning this unit's kernel has never been OTA-updated;
see §3b).

### 4b. `update_uboot.sh` — same A/B pattern, plus a size cap and header strip

```sh
kernel_param=$(grep -o 'kernel=[ab]' /proc/cmdline | cut -d'=' -f2)
case "$kernel_param" in
    a) CP_PATH="/dev/mtd4"; BOOT_PART="B" ;;   # uboot_b
    b) CP_PATH="/dev/mtd3"; BOOT_PART="A" ;;   # uboot
    *) echo "partition err: $kernel_param"; exit 1 ;;
esac

check_file "/tmp/uboot.img"; check_file "/tmp/uboot.img.md5"
md5sum -c /tmp/uboot.img.md5 | grep -q OK || { echo "md5 check failed!"; exit 1; }

img_info=$(fcrc /tmp/uboot.img) || { echo "crc check error!"; exit 1; }
img_len=$(echo "$img_info" | awk 'NR==2{print $3}')
[ "$img_len" -gt $((240 * 1024)) ] && { echo "data size > 240K"; exit 1; }

dd if=/tmp/uboot.img of=/tmp/uboot.bin bs=64 skip=1 status=none   # strip the 64-byte proprietary header
nandwrite -p "$CP_PATH" /tmp/uboot.bin

fw_setenv bootsystem "$BOOT_PART" || exit 1
exit 0
```

Confirms (independently of §2's byte analysis) that `fcrc` parses the
proprietary Axera header (its multi-line output has the payload size as
field 3 of line 2) and that the header is exactly 64 bytes (`dd bs=64
skip=1`) before the raw payload is written to NAND. Also enforces a
240 KiB cap on U-Boot's own payload size — sanity-checking against a
runaway/malformed image before it's ever written to flash.

### 4c. `update_rootfs.sh` — inconsistent with the live partition table (open question / footgun)

```sh
[ -f /tmp/rootfs.img ]       || { echo "does not exist!"; exit 1; }
[ -f /tmp/rootfs.img.md5 ]   || { echo "does not exist!"; exit 2; }
md5sum -c /tmp/rootfs.img.md5 | grep -q OK || exit 3

flash_eraseall /dev/mtd3
flashcp /tmp/rootfs.img /dev/mtd3
exit 0
```

This unconditionally targets **`/dev/mtd3`** — but per the live
`mtdparts=` string (§3a) and `update_uboot.sh`'s own comments in the same
script directory, **`mtd3` is the primary `uboot` partition**, not
`rootfs` (the actual "rootfs" MTD partition, which is UBI-formatted and
holds `/bak`+`/opt`, is `mtd10` per the known live-mount facts). Either
(a) this script is dead/legacy code left over from an earlier, smaller
`mtdparts` layout that predates the current uboot-A/B split, or (b) it is
still invoked under some condition not visible in this static analysis. As
written, **running `update_rootfs.sh` today would erase and overwrite the
live U-Boot partition with rootfs data** — a genuine bug/footgun, not a
misreading on our part (the mtd numbers are corroborated three independent
ways: the cmdline `mtdparts=` string, `update_kernel.sh`'s comments, and
`update_uboot.sh`'s comments, which all agree mtd3=uboot). **Flagged
prominently as an open question and as a script any future on-device
tooling must not blindly invoke.**

### 4d. `update_img.sh` — UBI-file overwrite for app/soc/audio (generic) and alg (different convention)

```sh
update_img() {   # $1=name (app|soc|audio), $2=mountpoint
    cd /tmp
    [ -f $1.img ]      || { echo "does not exist!"; return 1; }
    [ -f $1.img.md5 ]  || { echo "does not exist!"; return 2; }
    MD5=$(md5sum -c $1.img.md5 | grep OK); [ "$MD5"x = ""x ] && return 3

    if [ "$1"x = "app"x ]; then
        rm /opt/$1.img; cp /tmp/$1.img /opt/$1.img      # app: copy only, no remount (picked up on next boot's app_init.sh)
    else
        rm /opt/$1.img; cp /tmp/$1.img /opt/$1.img
        umount -l $2; sync
        [ -f /opt/$1.img ] || return 4
        mkdir -p $2 2>/dev/null
        mount -t squashfs -o offset=64 /opt/$1.img $2   # soc/audio: remount live immediately
    fi
    return 0
}

update_img_alg() {   # different naming convention: /opt/tmp_alg.img + /opt/tmp_alg.img.md5
    cd /opt
    [ -f tmp_$1.img ]      || return 1
    [ -f tmp_$1.img.md5 ]  || return 2
    MD5=$(md5sum -c tmp_$1.img.md5 | grep OK); [ "$MD5"x = ""x ] && return 3
    rm /opt/$1.img; cp /opt/tmp_$1.img /opt/$1.img
    umount -l $2; sync
    [ -f /opt/$1.img ] || return 4
    mkdir -p $2 2>/dev/null
    mount -t squashfs -o offset=64 /opt/$1.img $2
    return 0
}

if [ "$1"x = "alg"x ]; then update_img_alg $1 $2; else update_img $1 $2; fi
exit $?
```

Verification is **MD5 sidecar only** (no `fcrc` call in this script,
unlike `update_uboot.sh`/`update_audio.sh`) before the copy; no backup is
kept for app/soc here (unlike `update_audio.sh`, §4e) — a corrupted write
after the MD5 check (e.g. power loss mid-`cp`) has no rollback in this
script, though `system_init.sh` (§3c) does provide one specifically for
`app.img` at the *next boot* via `/bak/app.img`.

### 4e. `update_audio.sh` — the one script with an explicit backup+rollback

```sh
[ -f audio.img ]      || return 1
[ -f audio.img.md5 ]  || return 1
MD5=$(md5sum -c audio.img.md5 | grep OK); [ "$MD5"x = ""x ] && return 1

fcrc audio.img || { echo "crc check error!"; return 1; }

[ -d /opt/backup ] || mkdir /opt/backup; rm -f /opt/backup/audio.img
[ -f /opt/audio.img ] && mv /opt/audio.img /opt/backup/audio.img

cp audio.img /opt/
if [ $? != 0 ]; then
    rm -f /opt/audio.img
    mv /opt/backup/audio.img /opt/audio.img    # roll back on copy failure
    echo "update_audio failed!"
    return 1
fi

chmod 777 /opt/audio.img
umount /audio; mount -t squashfs -o offset=64 /opt/audio.img /audio
```

Double-verified (MD5 **and** `fcrc`), keeps exactly one prior version in
`/opt/backup/audio.img`, rolls back automatically if the `cp` itself fails,
and remounts live. This is the most defensive of the four UBI-file
scripts.

### 4f. Everything else read

- `nand_mtd_sync.sh <src-char-dev> <dst-char-dev>`: generic raw
  MTD-to-MTD clone via `nanddump | nandwrite` over a named pipe
  (`flash_eraseall "$DST"` first); used by `pktool sync /dev/mtd7
  /dev/mtd8` per `pktool`'s own usage strings — i.e. a manual "make slot B
  identical to slot A" tool, independent of the kernel/uboot update flow.
- `app_init.sh`, `app_start.sh`: covered in §3c.
- `clean_ntpd.sh`: one-shot script that kills `ntpd`, strips it from root's
  crontab, `/etc/cron.d/*`, and `/etc/init.d/S50crontabs` — i.e. the
  vendor build disables the busybox `ntpd` service entirely (the device
  presumably gets time from the cloud/app layer instead).
- `wifi_connect.sh` / `wifi_disconnect.sh` / `reset_wifi.sh`: GPIO60
  power-cycle + `wpa_supplicant`/`udhcpc` bring-up/tear-down for the
  `8733bu.ko` (Realtek RTL8733BU) WiFi module. `app/script/wifi_connect.sh`
  and `soc/scripts/wifi_connect.sh` are near-duplicates (soc's version is
  the earlier-loaded, pre-`/app`-mount copy referenced by `system_init.sh`'s
  `LD_LIBRARY_PATH`; app's is the one `ctrl`/`app_init.sh` call at runtime).
- `pk_pcba_wifi_init.sh <gpio-pin>`: factory/PCBA test variant of the same
  WiFi bring-up, parameterized GPIO pin, used by `pktool pt_mode`
  (production-test mode).

## 5. `watchdog` behaviour (`app/bin/watchdog`, ELF 32-bit ARM, 79,784 bytes)

No `strings`/`objdump`/`readelf` on this Unraid box; extracted via python
`re.findall(rb"[\x20-\x7e]{5,}")` (1,438 printable strings) plus a
hand-rolled ELF `.dynsym` parser (confirms `EM_ARM`, 32-bit, little-endian —
matches the live device's `armv7l`).

**What it supervises**: `pktool`'s shared `g_config->state.watchdog.*`
struct fields (visible via its own strings, since `pktool`/`ctrl`/`watchdog`
all share the same config layout guarded by `/tmp/config.lock`) name
exactly seven supervised processes, each with a `_pid` and `_count` field:

```
g_config->state.watchdog.{agora,ble,card,cloud,ctrl,media,p2p}_pid
g_config->state.watchdog.{agora,ble,card,cloud,ctrl,media,p2p}_count
```

`card` and `p2p` are tracked but not started by `app_start.sh` today (both
are commented out there — `#./card &`, `# ./p2p &` — leftover/optional
components). The `_count` field per process strongly implies a
restart-count / backoff mechanism, not unconditional immediate respawn.

**Observed control-flow strings** (verbatim):

- `"[%s][%s][%s][%d]: watchdog =================kill %s[%d]===================="` and
  `"...watchdog =================reboot %s===================="` — two
  distinct escalation actions exist: **kill a specific named/PID'd
  process**, or **reboot the whole system**, logged identically either way
  — i.e. watchdog does NOT always reboot on a dead child; it has at least
  two tiers.
- `"run time(%ld), ctrl err, reboot"` — a full reboot specifically tied to
  `ctrl` (the MQTT/cloud-control process) erroring.
- `"device is free, runTime(%lld)s (%lld)min; now do reboot -f"` — an
  **idle-time-triggered periodic reboot** independent of any crash (a
  scheduled reboot-when-idle policy), executed via `reboot -f`.
- `"device is in ota, sta=%d, ble_OTA=%d, exit this check"`, `"device is
  in agora, exit this check"`, `"device is in cloud recording, exit this
  check"` — watchdog explicitly **suppresses its own checks** during an
  active OTA, an active Agora (video call) session, or active cloud
  recording — i.e. **killing/restarting `ctrl` or `cloud` mid-stream while
  the device believes it's "in agora"/"in cloud recording" will not
  trigger an immediate watchdog reboot**, but doing so outside those
  windows plausibly will (per the `ctrl err, reboot` string above). This
  is the single most operationally important fact for anyone building an
  on-device daemon that talks to these processes directly: **the exact
  boundary conditions under which watchdog escalates to `reboot -f` are
  state-dependent, not purely "process died"**, and were not further
  decoded (no disassembler available) — treat as `[INFERENCE]` requiring
  live-testing caution *when that testing is authorized*, not as a
  guarantee.
- `reboot -f`, `reboot -d 3 -n -f`, `reboot_process` — the concrete reboot
  invocations used (immediate force-reboot, and a 3-second-delayed
  no-sync force-reboot variant).
- Hardware watchdog device functions present as named symbols:
  `wdt_enable`, `wdt_disable`, `wdt_get_timeout`, `wdt_set_timeout`, and
  the literal string `/dev/watchdog` — confirms this binary is the one
  that pets (or can starve) the SoC's hardware watchdog timer directly.
  The actual configured timeout value could not be recovered from strings
  alone (it's a runtime `ioctl` argument, not a string).
- `cd /app/bin/ && ./pktool pt_mode &` / `cd /opt/ && ./pktool pt_mode &` —
  watchdog itself can launch `pktool` into production-test mode under
  some condition (`dispatch_handler_entry_pt_mode`).
- `decrypt_config_data` / `encrypt_config_data` / `AES_set_decrypt_key` /
  `AES_set_encrypt_key` / `petkitRootfs_Aes_Encrypt_Keys_32` — confirms
  `watchdog` (not just `ctrl`) links against the AES routine used to
  decrypt/encrypt `/opt/user.conf` and `/opt/dev.conf`. Handed off to
  `AppProtocolStudy`'s app-protocol writeup; not pursued further here
  (out of this study's boot-chain scope, and no key bytes were extracted).

`ctrl` and `pktool` (both larger binaries sharing the same `g_config`
struct and logging format) additionally expose, via strings:
`get_mtd_info_and_badblocks`, `compare_mtd_partitions_min_size`,
`config_mtd_erase`, `verify_file_with_mtd`, `read_mtd_with_size`, and the
literal `pktool` usage lines:

```
pktool %s compare <mtd1> <mtd2>            Compare two MTD partitions      (example: compare /dev/mtd7 /dev/mtd8)
pktool %s sync <src-mtd> <dst-mtd>         Sync src to dst MTD partitions  (example: sync /dev/mtd7 /dev/mtd8)
pktool %s verify <mtd> <file>              Verify MTD with file            (example: verify /dev/mtd7 /tmp/kernel.img)
pktool %s verify_size <mtd> <file> <size>  Verify with specific file size  (example: verify_size /dev/mtd7 /tmp/kernel.img 2097152)
```

These are exactly the low-level primitives `update_kernel.sh`/
`nand_mtd_sync.sh` build on, and independently confirm `/dev/mtd7`/`mtd8`
as the kernel A/B pair. `ctrl` also embeds a full Paho-MQTT-derived client
(matches the known live MQTT link to `47.251.247.167:33882`) and mbedTLS/
OpenSSL X.509 error strings — out of scope here, handed to
`AppProtocolStudy`.

## 6. SoC layer: kernel modules, device/driver bring-up, SDK surface

`soc.sq`/`soc.img` (squashfs, verified uImage above) unpacks to:

- **`/soc/ko/`** (27 modules): the Axera media-pipeline stack —
  `ax_sys.ko, ax_cmm.ko, ax_pool.ko, ax_base.ko, ax_npu.ko, ax_ivps.ko,
  ax_vpp.ko, ax_gdc.ko, ax_tdp.ko, ax_vo.ko, ax_venc.ko, ax_jenc.ko,
  ax_mipi_rx.ko, ax_mipi_switch.ko, ax_proton.ko, ax_audio.ko,
  ax_ive.ko, ax_perf_monitor.ko` plus a full unmodified NFS client stack
  (`sunrpc, grace, lockd, nfs, nfsv2, nfsv3, nfsv4.ko` — dated July 2025,
  clearly vendor-BSP boilerplate, not petkit-specific; `/soc/scripts/
  nfs_load.sh` loads/unloads them but nothing else references NFS).
- **`/soc/scripts/auto_load_all_drv.sh`** gives the exact insmod order
  (quoted in full):
  ```
  insmod ax_sys.ko
  insmod ax_cmm.ko cmmpool=anonymous,0,0x46000000,160M
  insmod ax_pool.ko
  insmod ax_base.ko
  insmod ax_npu.ko
  insmod ax_ivps.ko
  insmod ax_vpp.ko
  insmod ax_gdc.ko
  insmod ax_tdp.ko
  insmod ax_vo.ko
  insmod ax_venc.ko
  insmod ax_jenc.ko
  insmod ax_mipi_rx.ko
  insmod ax_proton.ko
  insmod ax_audio.ko
  ```
  (`remove_drv()` unloads in exact reverse order.) `cmmpool=anonymous,0,
  0x46000000,160M` reserves a fixed 160 MiB anonymous CMA-style pool at
  physical address `0x46000000` for the media pipeline — a concrete number
  anyone writing kernel-adjacent tooling needs (96 MiB total system RAM per
  `mem=96M` in bootargs vs. a 160 MiB media pool looks contradictory at
  first glance; either `mem=96M` only bounds the kernel's directly-managed
  region while `ax_cmm` reserves from a separate physically-addressed
  region the kernel doesn't count against its own 96 MiB, or the two
  numbers come from different build variants. Flagged as an open
  question — do not assume `mem=96M` is the whole story about available
  RAM.)
- **`/soc/scripts/npu_set_bw_limiter.sh`** reads `/proc/ax_proc/chip_type`
  and branches:
  ```sh
  if [ "$chip_type" == "AX630C_CHIP" ]; then
      echo 3000 > /proc/ax_proc/bw_limit/npu/limiter_val_sum_rdwr
  else
      echo 2000 > /proc/ax_proc/bw_limit/npu/limiter_val_sum_rdwr
  fi
  ```
  This proves the script (and by extension the wider `/soc` BSP) supports
  **at least two** Axera chip variants generically; it does **not** by
  itself prove which branch *this* unit takes — `/proc/ax_proc/chip_type`'s
  actual runtime value on this device was not observed (would require
  live access, out of scope). **Open question, explicitly not resolved**:
  reconciling this AX630C-family BSP evidence against the separately
  confirmed live `armv7l`/Cortex-A7-class kernel (AX630C's public specs
  are Cortex-A53/aarch64) needs either a live `cat /proc/ax_proc/chip_type`
  or a decap/JTAG identification; this study cannot and does not resolve
  it. It's plausible this unit is a different, armv7l-based member of the
  same Axera family (e.g. AX620-series) running shared BSP scripts.
- **`/soc/scripts/wifi_connect.sh`** and **`/soc/scripts/system_init.sh`**:
  covered in §3c.
- **`/soc/bin/`**: `wpa_supplicant`, `wpa_cli` (standard), `ax_npuscene`
  (Axera NPU scene/demo tool, not further decoded — out of scope).
- **`/soc/lib/`**: the Axera media/vision SDK, all ELF 32-bit ARM
  shared objects. Exported-symbol samples (via the same hand-rolled
  `.dynsym` parser, since no `nm`/`objdump` on this box):
  - `libax_sys.so` (87 exported functions): `AX_SYS_Init/Deinit`,
    `AX_SYS_GetChipType` / `AX_SYS_GetChipType_Internal` (the actual
    read of chip identity happens here — not decoded further),
    `AX_POOL_*` (memory pool allocator), `AX_OS_MEM_*`.
  - `libax_proton.so` (2,113 exported functions — the largest SDK
    library by far): the full ISP/VIN pipeline — `AX_ISP_*` (sensor/ISP
    control, `AX_ISP_RegisterSensor[Ext]`, `AX_ISP_IQ_*` image-quality
    tuning for AE/AWB/AF/gamma/LSC/3DNR/dehaze/etc.), `AX_VIN_*` (video
    input device/pipe/channel management), plus internal
    `ax_alg_lsc_*`/`aaas_alg_*`/`ainr_alg_*` 3A/AI-ISP algorithm
    internals. This is the library the GC2053/GC2083 sensor drivers and
    `mc20e_isp_reg_reset_value.bin` (already known) feed into.
  - `libax_ivps.so` (56 exported functions): `AX_IVPS_*` — crop/resize/
    OSD-draw/rotate/dewarp video post-processing (feeds `media`'s JPEG
    snapshot pipeline: `/tmp/snap_main.jpeg` etc.).
  - `libax_skel.so` (1.1 MB, not enumerated line-by-line here but present
    and large) and `libax_venc.so`/`libax_audio*.so`/`libax_ae/af/awb.so`
    round out the vision/encode/audio 3A stack; `libax_opal.so` (542 KB)
    and `libax_nt_ctrl.so`/`libax_nt_stream.so` are unidentified beyond
    their names (not decoded further — out of scope for this pass).
  - Standard third-party libs also present: OpenSSL 1.0.0
    (`libcrypto`/`libssl`), `libfdk-aac`, `libopus`, `libsamplerate`,
    `libtinyalsa`, `libnl` 1.1.4.

All `/soc` binaries checked are **32-bit little-endian ARM (`EM_ARM`)** —
consistent with, and independently corroborating, the live-observed
`armv7l` architecture (see the AX630C/chip_type open question above: the
binaries themselves are unambiguously 32-bit ARM, not the AArch64 that
AX630C's public specs describe).

## 7. What a modified `app.img` (or an added binary + `app_start.sh` edit) needs to boot

Derived directly from §3c/§4d — no guessing required, this is exactly what
the stock scripts check:

1. Must be a valid 64-byte U-Boot `mkimage` uImage header (`magic
   0x27051956`, `os=linux`, `type=filesystem`, `comp=none`) immediately
   followed by a valid **squashfs** image (any squashfs mount options the
   stock offset/format expects: little-endian, mounted with `-o offset=64`
   — i.e. the squashfs superblock must start exactly 64 bytes into the
   file). `dcrc`/`hcrc` are **not verified by the mount path itself**
   (`system_init.sh` only checks the `mount` exit code) — but they *are*
   checked by `fcrc` wherever a script calls it (`app_init.sh` for
   audio/alg, `update_audio.sh`, `update_uboot.sh`), so any header must
   still be internally consistent if it's ever going to survive an
   `fcrc`-gated code path or a future re-flash via the stock update
   scripts.
2. Placed at `/opt/app.img` (with `/opt/linkapp.img` symlinked to it —
   `system_init.sh` creates this automatically if missing, or an on-device
   tool can pre-create it).
3. A working `/bak/app.img` **should** exist too, purely for safety: if the
   modified image's squashfs superblock is invalid, `system_init.sh` will
   copy `/bak/app.img` over `/opt/app.img` and force a `reboot` — i.e. a
   bad image will not brick the device, it will self-heal back to
   whatever is in `/bak` (or fail closed if `/bak/app.img` is also
   missing/bad, in which case `/app` simply never mounts and
   `app_init.sh`/`app_start.sh` never run).
4. Inside the squashfs, `script/app_start.sh` is the actual entry point
   invoked by `app_init.sh` — adding a new daemon just means adding it to
   this list (each backgrounded with `&`, order matters only in that
   `ctrl`/`agora`/`cloud` are staggered by `sleep 1` — plausibly to let
   `watchdog`/`ble`/`media` claim shared resources like `/tmp/
   config.lock` or the `media_buffer.*` shared-memory segments first). A
   new process should also register itself with whatever populates
   `g_config->state.watchdog.*_pid`/`*_count` if it wants supervision —
   how that registration actually happens (shared-memory struct write vs.
   a message to `watchdog`) was not reverse-engineered in this pass (no
   disassembler on this box); treat as a follow-up for whoever writes the
   on-device daemon.
5. `LD_LIBRARY_PATH` at the point `app_start.sh` runs is
   `/soc/usr/lib:/soc/lib:/app/bin:/app/lib:/alg` (set by `app_init.sh`) —
   a new binary can dynamically link against anything in `/soc/lib`
   (the Axera SDK, §6) or `/app/lib` without extra setup.
6. None of this touches the encrypted kernel/uboot/rootfs A/B mechanism
   (§2/§4a-c) at all — modifying the app layer is completely decoupled
   from, and far lower-risk than, the raw-MTD kernel/uboot update path.

## 8. Open questions

- **uImage `arch=mips`** on `app/soc/alg/audio.img` despite an armv7l
  device (§1) — likely stale vendor build tooling, not a real mismatch,
  but not confirmed either way.
- **Kernel/uboot/spl/ddrinit encryption**: confirmed by entropy + magic-
  scan evidence (§2), but the exact cipher/mode/key-derivation and the
  960-byte header+trailer overhead's exact structure are unknown — would
  require either the vendor's signing toolchain or a hardware key-extraction
  path, both out of scope for this offline study.
- **`err:13` in `ota_info`** (§3b): no decoder for this error-code space
  was found in any readable string table. Unknown whether 13 means
  CRC/size/signature/timeout or something else.
- **`update_rootfs.sh` targeting `/dev/mtd3`** (§4c) appears to conflict
  with the real partition table (`mtd3`=`uboot`, not `rootfs`) — flagged
  as a likely-dead/legacy script or a genuine bug; **do not invoke it**
  against this hardware without first re-deriving the correct MTD number
  for the "rootfs" partition (`mtd10` per the live-mount facts) and
  understanding why the script disagrees.
- **AX630C vs. armv7l chip identity** (§6): `/soc` BSP scripts explicitly
  branch on `chip_type == "AX630C_CHIP"` (AX630C's public specs are
  Cortex-A53/aarch64), yet every ELF checked on this device — including
  `/soc/lib/*.so` — is 32-bit ARM, and the live kernel is `armv7l`
  (Cortex-A7 class). Not resolved: this could be a different Axera
  family member (e.g. AX620-series) sharing the same generic BSP, or the
  `chip_type` string on this unit could genuinely differ from what the
  script's `if` branch name suggests. Requires a live `/proc/ax_proc/
  chip_type` read (out of scope for this offline study) to settle.
- **`fcrc` binary location**: referenced by name in three scripts
  (`app_init.sh`, `update_audio.sh`, `update_uboot.sh`) but not present
  anywhere in the extracted `app`/`soc`/`alg`/`audio` trees — it must live
  in the base rootfs/busybox environment that is unreachable because the
  kernel/initramfs is encrypted. Its exact CRC algorithm (does it
  recompute `dcrc` the same way this study did in §1, or something else
  entirely?) is therefore unverified beyond "it agrees with our
  independent `zlib.crc32` recomputation for the uImage-format files in
  §1" (inferred, not directly observed).
- **`cmmpool` 160 MiB reservation vs. `mem=96M`** (§6): the two figures as
  read are hard to reconcile; not resolved here.
- **Watchdog's exact escalation thresholds** (timeout values, retry counts
  before kill vs. reboot): present as `ioctl`/runtime values, not string
  literals; would need a disassembler (`objdump`/`readelf`/Ghidra-class
  tool), none of which are available on this Unraid box. Treat §5's
  behavioral claims as string-literal-grounded facts about *what actions
  exist*, not as verified facts about *exactly when* each fires.
