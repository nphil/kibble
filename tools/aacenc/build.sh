#!/usr/bin/env bash
# Builds the aacenc PCM->ADTS-AAC helper twice:
#
#   build-arm/aacenc            static armv7 binary -- the shipped deliverable,
#                                deployed to the device at /opt/kibble/aacenc.
#   build-native/aacenc-native  static x86_64 binary -- local validation only,
#                                never deployed.
#
# fdk-aac is fetched from source into ./.build (gitignored scratch space) and
# built statically (BUILD_SHARED_LIBS=OFF) once per target. Safe to re-run:
# the clone is skipped if already present, and each fdk-aac library build is
# skipped if its static archive already exists. The two aacenc binaries
# themselves are always relinked so wrapper.c edits take effect.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/.build"
FDK_TAG="v2.0.3"
FDK_SRC="$BUILD_DIR/fdk-aac"
JOBS="$(nproc 2>/dev/null || echo 2)"
ARM_ARCH_FLAGS="-march=armv7-a+fp -mfpu=neon-vfpv4 -Os"

log() { echo "[build.sh] $*" >&2; }

# ---- fetch fdk-aac ---------------------------------------------------------
mkdir -p "$BUILD_DIR"
if [ ! -d "$FDK_SRC/.git" ]; then
  log "cloning fdk-aac $FDK_TAG"
  git clone --depth 1 --branch "$FDK_TAG" https://github.com/mstorsjo/fdk-aac "$FDK_SRC"
else
  log "fdk-aac already cloned at $FDK_SRC ($(git -C "$FDK_SRC" describe --tags 2>/dev/null || echo unknown))"
fi

# ---- build fdk-aac statically for armv7 (cross) ----------------------------
ARM_TOOLCHAIN="$BUILD_DIR/arm-toolchain.cmake"
ARM_FDK_BUILD="$BUILD_DIR/fdk-arm-build"
ARM_FDK_INSTALL="$BUILD_DIR/fdk-arm-install"

# No CMAKE_BUILD_TYPE here on purpose: a Release config would append its own
# -O3 *after* our forced -Os, and the last -O flag on the command line wins.
# Forcing CMAKE_C_FLAGS/CXX_FLAGS directly keeps -Os authoritative.
cat > "$ARM_TOOLCHAIN" <<EOF
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)
set(CMAKE_C_COMPILER arm-linux-gnueabihf-gcc)
set(CMAKE_CXX_COMPILER arm-linux-gnueabihf-g++)
set(CMAKE_C_FLAGS "$ARM_ARCH_FLAGS" CACHE STRING "" FORCE)
set(CMAKE_CXX_FLAGS "$ARM_ARCH_FLAGS" CACHE STRING "" FORCE)
EOF

if [ -f "$ARM_FDK_INSTALL/lib/libfdk-aac.a" ]; then
  log "fdk-aac (arm) already built at $ARM_FDK_INSTALL/lib/libfdk-aac.a"
else
  log "configuring fdk-aac (arm cross, $ARM_ARCH_FLAGS)"
  cmake -S "$FDK_SRC" -B "$ARM_FDK_BUILD" \
    -DCMAKE_TOOLCHAIN_FILE="$ARM_TOOLCHAIN" \
    -DBUILD_SHARED_LIBS=OFF \
    -DCMAKE_INSTALL_PREFIX="$ARM_FDK_INSTALL" \
    -DCMAKE_INSTALL_LIBDIR=lib
  log "building fdk-aac (arm cross)"
  cmake --build "$ARM_FDK_BUILD" -j"$JOBS"
  cmake --install "$ARM_FDK_BUILD"
fi

log "linking build-arm/aacenc"
mkdir -p "$SCRIPT_DIR/build-arm"
arm-linux-gnueabihf-g++ -static $ARM_ARCH_FLAGS \
  -I"$ARM_FDK_INSTALL/include" \
  -o "$SCRIPT_DIR/build-arm/aacenc" \
  "$SCRIPT_DIR/wrapper.c" \
  "$ARM_FDK_INSTALL/lib/libfdk-aac.a" -lm

# ---- build fdk-aac statically for the native host, for validation only ----
NATIVE_FDK_BUILD="$BUILD_DIR/fdk-native-build"
NATIVE_FDK_INSTALL="$BUILD_DIR/fdk-native-install"

if [ -f "$NATIVE_FDK_INSTALL/lib/libfdk-aac.a" ]; then
  log "fdk-aac (native) already built at $NATIVE_FDK_INSTALL/lib/libfdk-aac.a"
else
  log "configuring fdk-aac (native, plain cmake && make)"
  mkdir -p "$NATIVE_FDK_BUILD"
  ( cd "$NATIVE_FDK_BUILD" && cmake "$FDK_SRC" \
      -DBUILD_SHARED_LIBS=OFF \
      -DCMAKE_INSTALL_PREFIX="$NATIVE_FDK_INSTALL" \
      -DCMAKE_INSTALL_LIBDIR=lib )
  log "building fdk-aac (native)"
  ( cd "$NATIVE_FDK_BUILD" && make -j"$JOBS" )
  ( cd "$NATIVE_FDK_BUILD" && make install )
fi

log "linking build-native/aacenc-native"
mkdir -p "$SCRIPT_DIR/build-native"
g++ -static -O2 \
  -I"$NATIVE_FDK_INSTALL/include" \
  -o "$SCRIPT_DIR/build-native/aacenc-native" \
  "$SCRIPT_DIR/wrapper.c" \
  "$NATIVE_FDK_INSTALL/lib/libfdk-aac.a" -lm

log "done:"
log "  $SCRIPT_DIR/build-arm/aacenc            (deploy to device as /opt/kibble/aacenc)"
log "  $SCRIPT_DIR/build-native/aacenc-native   (local validation only, never deployed)"
