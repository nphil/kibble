# tools

`kibble-msg.c` — a minimal command-line sender for the feeder's internal message bus. This is the
program that proved the feed path before `kibbled` existed; it is kept because it is the smallest
possible way to poke one message at the device while debugging.

```sh
# cross-compile for the feeder (armv7, glibc 2.25 — static, so no libc dependency)
docker run --rm -v "$PWD":/w -w /w debian:stable-slim sh -c \
  'apt-get -qq update && apt-get -qq install -y gcc-arm-linux-gnueabihf && \
   arm-linux-gnueabihf-gcc -static -Os -march=armv7-a+fp -mfpu=neon-vfpv4 \
     -o kibble-msg kibble-msg.c -lrt'

# dispense one portion from hopper 1
./kibble-msg 8 6004 1 feed:1:0:test

# send an arbitrary payload
./kibble-msg <dst> <msg_id_hex> <src> hex:00AABB...
```

Careful: `feed:` dispenses real food.

## aacenc

`aacenc` — a PCM→AAC helper that `kibbled` spawns as a subprocess: reads raw 16-bit signed
little-endian mono 16 kHz PCM on stdin, writes ADTS-framed AAC-LC on stdout. It exists because
`kibbled` is a fully static musl binary that can't `dlopen` anything, the device's own
`libfdk-aac.so.2.0.1` isn't reachable from it directly, and the cross toolchain's glibc is too new
to dynamically link against the device's glibc 2.25 — so encoding happens in a second, separately
statically-linked process instead, mirroring `kibble-msg.c` above. The encoder parameter sequence
it drives is pinned by on-device disassembly of the vendor's own `media` binary; see
`docs/23-audio-codec.md` §2.1 for the full derivation.

```sh
# fetches fdk-aac v2.0.3 into tools/aacenc/.build/ (gitignored scratch) and builds it statically,
# once for armv7 (cross, the deploy target) and once for the native host (throwaway validation copy)
cd tools/aacenc
./build.sh

# build-arm/aacenc is the shipped deliverable — deploy alongside kibbled:
scp build-arm/aacenc <device>:/opt/kibble/aacenc

# build-native/aacenc-native is for local testing only, never deployed, e.g.:
ffmpeg -f lavfi -i "sine=frequency=440:duration=2:sample_rate=16000" \
  -f s16le -ac 1 - | build-native/aacenc-native > out.aac
```

`aacenc` takes no arguments and touches only stdin/stdout/stderr — no files, no network. On
startup it logs the encoder's reported `frameLength`/`confBuf` to stderr and exits non-zero if
they don't match the pinned `1024`/`14 08`, which would mean the encoder parameters regressed.
`kibble-embed.c` — the second-process NPU face-embedding extractor `agent/src/embed.rs` shells
out to (`docs/27-cat-id.md`, `docs/18-npu-confirmed.md`). Decodes a JPEG face crop (vendored
`third_party/stb_image.h`, JPEG-only, MIT/public-domain), resizes it to the confirmed model input
shape, runs the vendor's own frozen face-recognition model through the proven second-process
`AX_ENGINE_*` path, and writes the raw 512-float embedding plus the model's own `prob` scalar to
stdout as flat little-endian bytes. Unlike `kibble-msg.c`, this one is **dynamically linked**,
deliberately: `libax_engine.so`/`libax_sys.so` are the device's own glibc shared objects with no
static archive to link against, and `kibbled` itself is a static musl binary that cannot host
them directly (`embed.rs`'s module doc explains why this had to be a separate process).

```sh
# 1. Pull the device's own AX Engine libraries once, for link-time symbol resolution only --
#    their real code is never executed at build time; the on-device copies are what actually
#    run, resolved at runtime via the rpath/LD_LIBRARY_PATH below.
scp root@<feeder>:/soc/lib/libax_engine.so tools/
scp root@<feeder>:/soc/lib/libax_sys.so tools/

# 2. Cross-compile against an OLD glibc (the device runs 2.25; a current cross-toolchain's
#    glibc is too new -- docs/18-npu-confirmed.md Sec 5 hit exactly this and pins debian:11-slim
#    for that reason. debian:stable-slim, used for kibble-msg.c above, is NOT old enough here).
docker run --rm -v "$PWD":/w -w /w debian:11-slim sh -c \
  'apt-get -qq update && apt-get -qq install -y gcc-arm-linux-gnueabihf && \
   arm-linux-gnueabihf-gcc -Os -march=armv7-a+fp -mfpu=neon-vfpv4 \
     -o kibble-embed kibble-embed.c -L. -lax_engine -lax_sys \
     -Wl,-rpath,/soc/lib -Wl,--allow-shlib-undefined -lm'

# 3. Deploy alongside kibbled and drop the link-time-only .so copies (never shipped -- the
#    device's own /soc/lib copies are what the rpath above resolves against at runtime).
rm -f tools/libax_engine.so tools/libax_sys.so
# copy kibble-embed to /opt/kibble/kibble-embed, chmod +x

# run directly (kibbled invokes it the same way, with the same env var)
LD_LIBRARY_PATH=/soc/lib /opt/kibble/kibble-embed /alg/petkit_face_rec_mtl_s2_v5_sim.axmodel /opt/kibble/faces/pending/some-crop.jpg | xxd | head
```

`-Wl,--allow-shlib-undefined` is required because `libax_engine.so` itself needs
`libax_interpreter.so` (17 more `AX_NPU_*` symbols) — never linked directly, only resolved
transitively on-device at runtime, exactly as `docs/18-npu-confirmed.md` Sec 5 documents for the
original probe.

## kibble-food

`kibble-food.c` — the second-process bowl-fill inference helper `agent/src/foodlevel.rs` shells
out to (`docs/34-bowl-fill-surplus.md` Part 7). Decodes a JPEG camera frame (vendored
`third_party/stb_image.h`, same as `kibble-embed.c`), resizes it to the food model's own working
resolution, then `dlopen()`s the vendor's own `/alg/libalgo.so` and calls its real
`CPetkitAlgoFoodDetect` methods (`petkit_algo_model_init`/`_run`/`_deinit`) via `dlsym` — this is
the one tool in this directory that drives a *vendor* algorithm class directly rather than a bare
`.axmodel` through this project's own `AX_ENGINE_*` sequence, specifically to get byte-identical
pre/post-processing to what `media` itself runs (see the file's own module doc for the full
disassembly this is pinned from). Prints `score=<0.0-1.0 float>` on stdout, exit 0, on success.

Same glibc-version constraint as `kibble-embed.c` (the device runs 2.25; a current cross
toolchain's glibc is too new) — the `docker run ... debian:11-slim` recipe below is the normal
path. If Docker isn't available (it wasn't, the session this tool was first built in — the
sandbox had no `docker` binary or socket), the same glibc 2.31 userspace can be assembled by hand
from a Debian bullseye snapshot without Docker at all:

```sh
# 1. Pull the exact armhf-cross libc6 packages bullseye shipped (2.31-9cross4) from a snapshot
#    mirror, extract them, and patch the one GNU-ld *text* script (libc.so) that hardcodes
#    absolute /usr/arm-linux-gnueabihf/... paths back to itself -- Debian's cross-gcc packages
#    hardcode their own /usr/<triplet>/{include,lib} search path regardless of --sysroot, so
#    without this patch the linker silently pulls in the *system* (too-new) libc.so.6 instead.
mkdir -p /tmp/bullseye-sysroot
# ... apt-get download (isolated sources.list pointed at snapshot.debian.org) + dpkg-deb -x
#     libc6-armhf-cross_2.31-9cross4_all.deb and libc6-dev-armhf-cross_2.31-9cross4_all.deb into
#     /tmp/bullseye-sysroot ...
SR=/tmp/bullseye-sysroot/usr/arm-linux-gnueabihf/lib
cat > "$SR/libc.so" <<EOF
OUTPUT_FORMAT(elf32-littlearm)
GROUP ( $SR/libc.so.6 $SR/libc_nonshared.a  AS_NEEDED ( $SR/ld-linux-armhf.so.3 ) )
EOF

# 2. libax_engine.so/libax_sys.so link-time stubs: unlike kibble-embed.c, kibble-food.c never
#    calls AX_ENGINE_CreateHandle/GetIOInfo/RunSync/AX_SYS_MemAlloc/MemFree itself -- libalgo.so's
#    own dlsym'd methods do all of that internally -- so the *only* four symbols this binary's own
#    link step needs are AX_SYS_Init/Deinit and AX_ENGINE_Init/Deinit. A minimal hand-written stub
#    .so exporting just those four (bodies irrelevant -- never executed; the real
#    /soc/lib/libax_engine.so and libax_sys.so are what actually resolve at runtime, via the
#    rpath below) avoids pulling the real ones off the device at all:
mkdir -p /tmp/ax_stubs && cd /tmp/ax_stubs
cat > stub_ax_sys.c <<'EOF'
typedef int AX_S32;
AX_S32 AX_SYS_Init(void) { return 0; }
AX_S32 AX_SYS_Deinit(void) { return 0; }
EOF
cat > stub_ax_engine.c <<'EOF'
typedef int AX_S32; typedef unsigned int AX_U32;
typedef struct { int eHardMode; AX_U32 reserve[8]; } AX_ENGINE_NPU_ATTR_T;
AX_S32 AX_ENGINE_Init(AX_ENGINE_NPU_ATTR_T *attr) { (void)attr; return 0; }
AX_S32 AX_ENGINE_Deinit(void) { return 0; }
EOF
SR=/tmp/bullseye-sysroot/usr/arm-linux-gnueabihf
arm-linux-gnueabihf-gcc -B"$SR/lib" -L"$SR/lib" -I"$SR/include" -fPIC -shared \
  -Wl,-soname=libax_sys.so    -o libax_sys.so    stub_ax_sys.c
arm-linux-gnueabihf-gcc -B"$SR/lib" -L"$SR/lib" -I"$SR/include" -fPIC -shared \
  -Wl,-soname=libax_engine.so -o libax_engine.so stub_ax_engine.c

# 3. Build kibble-food.c itself against the patched sysroot + the stubs. -ldl is new relative to
#    kibble-embed.c (dlopen/dlsym/dlclose/dlerror) -- confirmed the resulting binary NEEDs
#    libdl.so.2 as a *separate* DT_NEEDED (glibc merged libdl into libc.so.6 only from 2.34; the
#    device is 2.25, so it must stay a separate shared object, exactly what the bullseye/2.31
#    sysroot naturally produces and a current/2.36+ toolchain would not).
cd /data/home/Homelabber/kibble/tools
arm-linux-gnueabihf-gcc -B"$SR/lib" -L"$SR/lib" -I"$SR/include" \
  -Os -march=armv7-a+fp -mfpu=neon-vfpv4 \
  -o kibble-food kibble-food.c -L/tmp/ax_stubs -lax_engine -lax_sys \
  -Wl,-rpath,/soc/lib -Wl,--allow-shlib-undefined -ldl -lm

# sanity check before deploying -- must show only GLIBC_2.4 (or lower), never 2.34+:
arm-linux-gnueabihf-objdump -T kibble-food | grep -oE 'GLIBC_[0-9.]+' | sort -u -V
```

Deploy alongside `kibbled`, same as `kibble-embed`: copy to `/opt/kibble/kibble-food`, `chmod +x`.
There is still no scp/tftp path from the sandbox to the feeder (`docs/34` Part 5's own note still
applies) — transfer via `feeder_shell`'s telnet session, gzip+base64, in line-bounded chunks (each
`feeder_shell` line is capped around 1 KB; chunks of ~60 base64-wrapped 76-char lines transfer
reliably). Calling `feeder_shell` *from inside `eval`* (`await tool.feeder_shell(...)` /
`tool.feeder_shell(...)` in Python/JS) rather than relaying chunks by hand through the
conversation is what actually made this reliable — hand-copying a fetched/generated base64 blob
back into a tool call silently truncated well before any documented size limit, repeatedly, before
this was tried; scripting the whole transfer loop in one `eval` cell and verifying the byte count
after every chunk caught the very first attempt that actually matched end to end (md5-verified).

```sh
LD_LIBRARY_PATH=/soc/lib /opt/kibble/kibble-food /alg/petkit_pp_fooddet_416_128_segreg_0509_u16.axmodel /opt/kibble/events/<ts>-visit.jpg
```
