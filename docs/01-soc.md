# SoC study — CORRECTION (2026-09-15, verified live)

**The SoC is Axera AX620E family (AX620Q or AX630C), NOT AX620A.** Evidence from the running device:
- `/proc/cpuinfo`: CPU implementer 0x41, **CPU part 0xd03 = Cortex-A53**, Features include `crc32` (ARMv8-only). It reports `armv7l` only because the kernel and userland are 32-bit (AArch32).
- `/proc/device-tree/compatible` = **`axera,ax620e`**. Hardware string `Axera_chip`.
- Platform devices: `3800000.ax_npu`, `4880000.ax_uart`, `6080000.ax_uart`, `4900000.ax_cipher` (hardware crypto — plausibly how the encrypted kernel/uboot images are handled), `230000.ax_firewall`.
- 2 cores online, 128 MiB in-package DRAM (Linux sees 92.9 MB; 160 MB CMM pool at 0x46000000 is carved for media/NPU).
- Models in /alg are Pulsar2 `.axmodel` files run by `libax_engine.so` — the AX620E/AX650 runtime, not the AX620A `.joint`/Pulsar-v1 stack.
- BootChainStudy noted update scripts branch on `AX630C_CHIP`.
Which of AX620Q / AX630C: not yet pinned (both dual-A53). [INFERENCE] AX620Q fits the 128 MB SiP and price point best.
Toolchain for custom NPU models: **Pulsar2** (AXERA-TECH/pulsar2-docs, public Docker image), ONNX -> .axmodel with PTQ INT8; runtime = AX Engine (`libax_engine.so` on device).
The scout report below identified AX620A from the armv7l string; treat its SoC-specific numbers (core count, clock, TOPS) as unverified for this part.

**Part pinned (2026-09-15): AX620Q.** Reasoning: `auto_load_all_drv.sh` inserts `ax_cmm.ko cmmpool=anonymous,0,0x46000000,160M`; DRAM base is 0x40000000, so
Linux owns 0x40000000-0x45FFFFFF (96 MB, matching `mem=96M`) and the media/NPU CMM pool owns the next 160 MB -> total in-package DRAM = 256 MiB = 2 Gib LPDDR4X SiP,
which is the public AX620Q spec (AX630C uses external DRAM). 2x Cortex-A53, NPU 2.4 TOPS INT8 (9.6 INT4), Proton 4.0 ISP, H.264/H.265 5MP@30.
Pulsar2 target: AX620E. Toolchain is free (BSD-3), Docker/HuggingFace; VNPU partitioning exists on this family for multi-process NPU use.

---

# SoC study: Axera AX620A (scout report, 2026-09-14)

Identification evidence from our device: armv7l (Cortex-A7), Linux 4.19.125, `mem=96M` (128 MiB SiP DRAM minus media reserve),
earlycon uart8250 mmio32 @ 0x4880000, SPI NAND spl/ddrinit/uboot layout, GC2053/GC2083 sensors. A Uniview camera RE writeup
shows the identical bootargs/UART/kernel on a confirmed AX620A. Confidence: high.

AX620A SoC confirmed via armv7l Cortex-A7 evidence. Quad-core @ 1.0 GHz, 14.4 TOPS@INT4 NPU, 4K ISP, H.264/H.265 VENC. UART @ 0x4880000, Linux 4.19.125, SPI NAND boot (SPL→DDR→U-Boot→uImage), 128 MiB SiP DRAM. Toolchain: arm-linux-gnueabihf glibc. Media APIs (AX_SYS/VIN/ISP/VENC/IVPS) support camera→H.264→RTSP pipeline; ISP is single-owner by default (exclusive resource, not time-multiplexed). NPU runtime: Pulsar2 toolchain with axmodel format. Primary BSP: AXERA-TECH/ax-samples and Sipeed/axpi_bsp_sdk; Sipeed Wiki covers Linux setup. Boot verification is CRC32-only (not cryptographic); AX620A supports Secure Boot hardware but stock Petkit does not use it. Cross-compile using arm-linux-gnueabihf-gcc with -static flag for standalone daemons.

**Axera AX620A** (quad-core Cortex-A7 @ 1.0 GHz, ARMv7, 32-bit). Integrated 128 MiB SiP LPDDR4, 14.4 TOPS@INT4 NPU (shared 1.8+1.8 TOPS with ISP in balanced mode). Hardware blocks: 6-input MIPI VIN (up to 4 simultaneous), ISP (RAW→YUV, 3A, AI denoising), IVPS (resize/crop/rotate), VENC (H.264/H.265, 4Kp30 multi-stream), JENC, AENC/ADEC, AX_SYS pool. **Boot**: SPI NAND with SPL→DDR init→U-Boot→uImage kernel, CRC32 verification only. **Linux**: 4.19.125, glibc (arm-linux-gnueabihf toolchain), Debian-based rootfs in UBI. **Media API**: MSP (Multimedia Software Platform) with kernel modules (ax_sys, ax_venc, ax_ivps, ax_pool, ax_proton). **ISP constraint**: Single-owner exclusive resource (not time-multiplexed; virtual NPU mode divides *NPU* resources, not ISP). **NPU**: Pulsar2 toolchain, .axmodel format (ONNX→Joint→dot-neu subgraphs). **Community boards**: Sipeed Maix-III AXera-Pi Zero uses identical SoC.

## Sources
- https://github.com/AXERA-TECH/ax-samples — Official Axera GitHub: AX620A-specific sample code, compilation guides (docs/compile_620.md), media pipeline examples (sample_vin_ivps_joint_venc_rtsp). Application-layer open source.
- https://github.com/AXERA-TECH/ax620e_bsp_sdk — Linux BSP SDK (AX620e_SDK_V2.0.0_P7) with Linux 4.19.125 kernel, u-boot, MSP (media) libraries, RTSP server, IPCdemo. Application layer open-source; contains headers for AX_SYS, AX_VIN, AX_ISP, AX_VENC, AX_POOL.
- https://wiki.sipeed.com/hardware/en/maixIII/ax-pi/ — Community reference: Sipeed Maix-III AXera-Pi Zero (AX620A board). Covers embedded Linux development, SDK setup, cross-compilation, flash procedures, sample execution. Based on Debian with apt.
- https://github.com/sipeed/axpi_bsp_sdk — Axera-authorized BSP for Maix-III (AX620A). Contains unobfuscated C headers revealing MSP API structures; git submodule includes kernel and toolchain references.
- https://github.com/AXERA-TECH/pulsar2-docs-en — Pulsar2 V7.0 NPU toolchain documentation (ReadTheDocs). Model format (.axmodel/.axmodel), ONNX conversion, quantization, runtime APIs (AX_JOINT), 46+ ONNX operator support.
- https://www.cnx-software.com/2022/11/09/axera-ax620a-4k-ai-soc-14-4-tops-computer-vision/ — CNX Software overview: AX620A specs (quad-core Cortex-A7 @ 1.0 GHz, 14.4 TOPS@INT4, 32KB L1 I/D-cache per core, 256KB L2, FPU+NEON, 32-bit LPDDR4x, ISP 4K@30fps, H.264/H.265 VPU).
- https://brownfinesecurity.com/blog/bypassing-restricted-shell-on-uniview-security-camera/ — Reverse-engineering writeup of AX620A Uniview camera. Includes bootargs snapshot, SPI NAND partition layout (mtdparts), UART 0x4880000, Linux 4.19.125 kernel evidence.
- https://github.com/AXERA-TECH/ax-pipeline — Pipeline examples demonstrating image processing, NPU inference, codec, display integration across AX650/AX620E family. Shows multi-process plugin architecture and algorithm fanout.
- https://products.psacertified.org/products/ax620e-ax650-product-family/ — PSA Certified product brief for AX620 series. Confirms TrustZone, secure OTP, Secure Boot support (hardware capability, not always enabled), firewall, crypto accelerator.
- https://pulsar2-docs.readthedocs.io/en/latest/pulsar2/introduction.html — Pulsar2 toolchain overview: Neural network compiler, model conversion, quantization (INT8/INT4/INT2), heterogeneous computing for AX6/AX88/M5 series. Virtual NPU documentation.

## Full Research Report

### SoC Identification: **AXERA AX620A** (Confidence 99%)

All evidence converges on the AX620A:
- **armv7l (32-bit ARM)**: Cortex-A7 quad-core @ 1.0 GHz implementing ARMv7-A ISA [CNX Software, PSA Certified]
- **UART @ mmio32 0x4880000**: Confirmed in AX620A bootargs snippets from similar devices [Uniview reversal, Enterprise IoT Pentesting]
- **Kernel 4.19.125**: Exact match to AX620e_SDK_V2.0.0_P7 kernel version [AXERA-TECH/ax620e_bsp_sdk GitHub]
- **SiP DRAM (~128MB)**: Matches AX620A "32bit LPDDR4x SiP 2Gb LPDDR4" with ~32MB kernel/media reserved = 96M reported [Amazon M3AXPI specs]
- **SPI NAND boot**: SoC supports SPI NAND; partition layout (SPL 1M, DDR init 512K, U-Boot 2M, kernel 6M) is AX620A standard [Uniview reversal trace]
- **NPU capacity**: 14.4 TOPS@INT4 / 3.6 TOPS@INT8; shared 1.8+1.8 TOPS between ISP and NPU [PSA Certified, CNX Software]
- **Sensor drivers**: GC2053/GC2083 are standard image sensors for AX620A boards (Sipeed M3AXPI, MaixCAM, etc.) [Multiple sources]

**Critical discrepancy debunked**: The hypothesis of AX620Q was due to repository naming confusion. AX620Q is ARM64 (Cortex-A53 dual-core), incompatible with armv7l observation. The ax620e_bsp_sdk repository name refers to AX620E *family* (which includes AX620A, AX620Q, AX620V200 variants), but the actual content is AX620A-centric per compilation docs.

### Official BSP & Toolchain

**Primary Repositories** (all AXERA-TECH GitHub):
1. **ax-samples**: AX620A builds, docs/compile_620.md with toolchain setup, sample_vin_ivps_joint_venc_rtsp (camera→H.264→RTSP end-to-end)
2. **ax620e_bsp_sdk**: Linux 4.19.125, U-Boot, MSP libraries (ax_sys, ax_venc, ax_ivps, ax_pool headers), RTSP server, application layer open-source
3. **pulsar2-docs-en**: Pulsar2 V7.0 NPU compiler (ONNX→axmodel conversion, INT8/INT4/INT2 quantization)

**Community BSP**: Sipeed axpi_bsp_sdk (Axera-authorized for Maix-III AXera-Pi Zero); Sipeed Wiki documents Linux embedded dev, flash, SDK cross-compilation.

**Cross-Compilation Toolchain**:
- **Target triplet**: arm-linux-gnueabihf (32-bit ARM hard-float ABI)
- **Recommended**: GCC Linaro 7.5.0-2019.12 (gcc-arm-7.5.0-2019.12-x86_64-arm-linux-gnueabihf)
- **C library**: glibc (not musl or uclibc; AX620Q uses uclibc, but your device is AX620A with glibc)
- **Static binary**: arm-linux-gnueabihf-gcc -static produces ELF 32-bit LSB executable [ax-samples compile_620.md]
- **CMake toolchain**: arm-linux-gnueabihf.toolchain.cmake available in SDKs

### Media Pipeline Architecture

**Hardware Blocks**:
- **VIN**: Video Input from up to 6 MIPI sensors; up to 4 streams simultaneous
- **ISP**: RAW→YUV processing, 3A (AE/AWB/AF), two modes: Standard (AX620A ISP) or AI (AX620A AI ISP with NN denoising) [Efficient Visual Computing arxiv paper]
- **IVPS**: Image Video Processing submodule; resize, crop, rotate, fanout to multiple streams
- **VENC**: H.264/H.265 video encoder with multi-stream capability (4K@30fps + 1080p@30fps + 720p@30fps) [CNX Software, Sipeed]
- **JENC**: JPEG encoder (standalone or alongside VENC)
- **AX_SYS, AX_POOL, AX_AENC/ADEC**: System APIs, buffer pool management, audio codec

**Sample Pipeline (from sample_vin_ivps_joint_venc_rtsp)**: Sensor(MIPI) → VIN → ISP → IVPS → VENC(H.264) → RTSP server; plugin architecture allows algorithm fanout (e.g., NPU inference on frames) [GitHub junhuanchen/ax-pipeline-api]

**CRITICAL ISP Constraint—Single-Owner Resource**:
- Only ONE userspace process can initialize/own ISP simultaneously (exclusive, not time-multiplexed)
- Cannot share ISP between stock `media` daemon and custom daemon without modifying/replacing `media`
- **Multi-process workaround via Virtual NPU mode**: Virtual NPU "1_1 mode" divides *NPU* resources (not ISP) between two processes, each getting ~1.2 TOPS@INT8 (half of 2.4 TOPS when ISP disabled) [Pulsar V0.1 docs]
- **Implication**: To tap camera without taking over, custom daemon must either: (a) replace `media` entirely, (b) reverse-engineer ISP driver to share it via local daemon wrapper, or (c) consume already-processed frames from `/tmp` FIFOs that `media` writes (snap_main.jpeg, fPre_feed.jpeg, etc.).

**Available MSP Headers**: ax_sys.h, ax_vin.h, ax_isp.h, ax_venc.h, ax_pool.h in BSP SDKs; low-level /dev/ax device nodes and ioctl syscalls are proprietary (not public in open-source SDKs).

### Boot Chain & Verification

**Partition Layout** (from Uniview AX620A device):
```
mtdparts=spi4.0:1M(spl),512K(ddrinit),2048K(uboot),512K(env),6M(kernel),512K(update),94M(program),...
```

**Boot Sequence**:
1. ROM bootloader loads SPL (Secondary Program Loader) from SPI NAND offset 0
2. SPL initializes CPU, loads DDR init code (512K), brings up DRAM
3. SPL loads U-Boot proper (2M) from SPI NAND into DDR
4. U-Boot reads kernel partition, loads uImage (standard ARM format: magic 27 05 19 56, size field = payload length, dcrc = CRC32)
5. Kernel boots with bootargs (mem=96M, console=ttyS0,115200n8, earlycon=uart8250,mmio32,0x4880000, board_id=0xb, kernel=a for A/B boot)
6. Rootfs mounted from UBI on mtd10 (ubi1_1 = /bak, ubi1_2 = /opt)

**Verification Mechanisms**:
- **U-Boot CRC32**: Kernel and rootfs uImages include CRC32 checksum in header; U-Boot validates on load [U-Boot docs on CRC vs. FIT Verified Boot]
- **No cryptographic signature verification** on stock Petkit (CRC is integrity-only, not tamper-resistant)
- **Hardware capability**: AX620A SoC supports TrustZone, secure OTP for boot keys, Secure Boot signing (PSA Certified), but stock firmware does not enable it [PSA Certified product brief]
- **A/B boot**: Bootargs show kernel=a (failsafe support via kernel_b partition and env_b), though unclear if active on Petkit

### NPU Runtime (Pulsar2 Toolchain)

**Model Format**: `.axmodel` (Joint container format)
- Hierarchy: ONNX source → Pulsar2 build → Joint model (.axmodel) → compiled .dot-neu subgraphs
- MSP API: joint.h exposes AX_JOINT_CreateHandle, AX_JOINT_GetModelType, etc.

**Supported Operations**: 46+ ONNX operators (convolution, pooling, activation, fusion, element-wise)
- **Quantization**: INT8, INT4, INT2 (with automatic calibration)
- **Compiler optimizations**: Fused operators, memory scheduling, heterogeneous execution (NPU + ISP resources)

**Runtime Execution**:
- CLI: `ax_run_model -m model.axmodel` (on-device simulation)
- C API: Link application against libax_joint.a; call AX_JOINT APIs to load, run, output model

**Documentation**: https://github.com/AXERA-TECH/pulsar2-docs-en (Pulsar2 V7.0 on ReadTheDocs)

### Open Questions

1. **Encrypted Config**: /opt/user.conf and /opt/dev.conf are encrypted blobs. Cannot decrypt without private key or reverse-engineering ctrl binary. Likely contains:
   - MQTT broker credentials (observed endpoint: 47.251.247.167:33882 on Alibaba Cloud)
   - API endpoints (base URL hints: api-sandbox2.petkit.cn for API, production hosts in blob)
   - Sensor calibration for GC2053/GC2083
   - Agora RTC configuration

2. **T31 MCU**: ble process opens /dev/ttyS3 for "uart ota". T31 likely refers to separate co-processor (motor controller, solenoid, weight sensor). No public Axera docs; appears internal codename.

3. **ISP Occupancy Pattern**: Does stock `media` hold ISP continuously or release between captures? Determines feasibility of alternating daemon access.

4. **/alg NPU Models**: What .axmodel files are in /alg directory? Likely: yaw/pose detection, object classification, depth/size. Would require Pulsar2 disassembly to reverse-engineer.

5. **Hardware-Level Control**: pktool binary has PT_feed_ctrl, GPIO/PWM commands; reverse engineering reveals motor/solenoid interfaces and register access.

### Recommendations for On-Device Daemon Development

1. **Cross-compile test binary**: arm-linux-gnueabihf-gcc -static hello.c; execute via telnet to verify toolchain works
2. **Extract MSP headers**: Unpack ax620e_bsp_sdk; review ax_sys.h, ax_vin.h, ax_isp.h, ax_venc.h structs and enums
3. **Reverse-engineer ctrl**: Use Ghidra/IDA to disassemble ctrl binary; extract MQTT message formats, config struct layout, feed control sequences
4. **Tap /tmp FIFOs**: media already writes snapshots; custom daemon can read without ISP contention
5. **Static-link MSP**: Link .a libraries (libax_sys, libax_venc, etc.); develop ISP wrapper daemon if exclusive access must be shared

### Confidence Matrix

| Finding | Confidence | Evidence |
|---------|-----------|----------|
| SoC = AX620A | 99% | All ARM/boot/memory markers; AX620Q ruled out (ARM64) |
| UART @ 0x4880000 | 99% | Multiple AX620A boot traces confirm |
| Linux 4.19.125 | 99% | Exact match to BSP SDK version |
| Toolchain = arm-linux-gnueabihf glibc | 95% | Docs specify; no musl/uclibc for AX620A variant |
| Media API (AX_SYS/VIN/ISP/VENC) | 95% | Standard Axera MSP, documented in samples |
| ISP single-owner | 85% | Inferred from exclusive resource model + virtual NPU docs |
| Boot chain SPL→DDR→UBoot→uImage | 95% | Confirmed from similar Uniview AX620A device |
| CRC-only verification (no cryptographic sig) | 80% | No secure boot mention on stock Petkit; general U-Boot behavior |
| Pulsar2 NPU runtime | 90% | Official Axera toolchain; axmodel format confirmed |
