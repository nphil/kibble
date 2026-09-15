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
