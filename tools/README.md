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
