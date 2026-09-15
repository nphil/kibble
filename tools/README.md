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
