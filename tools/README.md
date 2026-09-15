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
