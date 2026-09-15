/* kibble-embed.c -- second-process NPU face-embedding extractor for Petkit D4SH2 (AX620Q).
 *
 * This IS the "second process" docs/18-npu-confirmed.md proved works: a small, independent,
 * dynamically-linked executable that opens the NPU itself (AX_SYS_Init -> AX_ENGINE_Init ->
 * CreateHandle -> GetIOInfo -> RunSync -> teardown) while the vendor's `media` keeps its own
 * eight engine handles open. `kibbled` (agent/src/embed.rs) shells out to this binary once per
 * face crop rather than linking libax_engine.so/libax_sys.so itself, because kibbled is a
 * statically-linked musl binary and those are glibc shared objects meant to be dynamically
 * linked -- two worlds that cannot share one binary. Build/deploy: tools/README.md.
 *
 * Pipeline: decode the JPEG crop (vendored stb_image.h, JPEG-only baseline/progressive decoder,
 * no external dependency), resize to the model's confirmed input shape with a plain bilinear
 * filter, run it through the vendor's own frozen face-recognition model, and write the raw
 * 512-float embedding plus the model's own `prob` scalar to stdout as flat little-endian bytes
 * (no text/JSON on this path -- kibbled's std-only Rust reader parses it directly, bit-exact).
 *
 * Usage: kibble-embed <model.axmodel> <crop.jpg>
 * Exit 0 with exactly 2052 bytes on stdout on success; nonzero exit + a message on stderr
 * otherwise. Never touches any vendor process or IPC object -- reads two files, talks to the
 * NPU driver directly. Whitelisted AX calls only, the identical set docs/18-npu-confirmed.md's
 * probe (tools/kibble-npu.c) used: SYS_Init/Deinit, SYS_MemAlloc/MemFree, ENGINE_Init/Deinit,
 * CreateHandle/DestroyHandle, GetIOInfo, RunSync. No reset/reconfigure calls, ever.
 */
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_JPEG
/* STBI_ONLY_JPEG alone does not exclude stbi__ldr_to_hdr (guarded by the separate
 * STBI_NO_LINEAR flag, for stbi_loadf's float-output path, orthogonal to which formats are
 * compiled in) -- that function's pow() call pulled in GLIBC_2.29 from the cross-toolchain's
 * libm, newer than the device's own glibc 2.25 (docs/18-npu-confirmed.md Sec 5's exact "too new
 * toolchain" failure mode, discovered here at link-symbol-version level rather than at runtime).
 * Never used anyway (only stbi_load, never stbi_loadf, is called below) -- excluding it outright
 * is correct, not just a workaround.
 */
#define STBI_NO_LINEAR
#include "third_party/stb_image.h"

/* ---- AX Engine ABI: verbatim from tools/kibble-npu.c / docs/18-npu-confirmed.md Sec 2 ---- */
typedef unsigned long long AX_U64; typedef unsigned int AX_U32; typedef unsigned char AX_U8;
typedef int AX_S32; typedef signed char AX_S8; typedef char AX_CHAR; typedef void AX_VOID;

typedef AX_VOID* AX_ENGINE_HANDLE;
typedef enum { AX_ENGINE_VIRTUAL_NPU_DISABLE = 0, AX_ENGINE_VIRTUAL_NPU_ENABLE = 1 } AX_ENGINE_NPU_MODE_T;
typedef struct { AX_ENGINE_NPU_MODE_T eHardMode; AX_U32 reserve[8]; } AX_ENGINE_NPU_ATTR_T;
typedef enum { AX_ENGINE_TL_UNKNOWN = 0, AX_ENGINE_TL_NHWC = 1, AX_ENGINE_TL_NCHW = 2 } AX_ENGINE_TENSOR_LAYOUT_T;
typedef enum { AX_ENGINE_MT_PHYSICAL = 0, AX_ENGINE_MT_VIRTUAL = 1, AX_ENGINE_MT_OCM = 2 } AX_ENGINE_MEMORY_TYPE_T;
typedef enum { AX_ENGINE_DT_UNKNOWN=0, AX_ENGINE_DT_UINT8=1, AX_ENGINE_DT_UINT16=2, AX_ENGINE_DT_FLOAT32=3,
    AX_ENGINE_DT_SINT16=4, AX_ENGINE_DT_SINT8=5, AX_ENGINE_DT_SINT32=6, AX_ENGINE_DT_UINT32=7,
    AX_ENGINE_DT_FLOAT64=8 } AX_ENGINE_DATA_TYPE_T;
typedef struct _AX_ENGINE_IOMETA_EX_T AX_ENGINE_IOMETA_EX_T; /* opaque: never dereferenced */

typedef struct _AX_ENGINE_IOMETA_T {
    AX_CHAR *pName; AX_S32 *pShape; AX_U8 nShapeSize;
    AX_ENGINE_TENSOR_LAYOUT_T eLayout; AX_ENGINE_MEMORY_TYPE_T eMemoryType; AX_ENGINE_DATA_TYPE_T eDataType;
    AX_ENGINE_IOMETA_EX_T *pExtraMeta; AX_U32 nSize; AX_U32 nQuantizationValue; AX_S32 *pStride;
#if defined(__aarch64__)
    AX_U64 u64Reserved[9];
#elif defined(__arm__)
    AX_U64 u64Reserved[11];
#endif
} AX_ENGINE_IOMETA_T;

typedef struct _AX_ENGINE_IO_INFO_T {
    AX_ENGINE_IOMETA_T *pInputs; AX_U32 nInputSize; AX_ENGINE_IOMETA_T *pOutputs; AX_U32 nOutputSize;
    AX_U32 nMaxBatchSize; AX_S32 bDynamicBatchSize;
#if defined(__aarch64__)
    AX_U64 u64Reserved[11];
#elif defined(__arm__)
    AX_U64 u64Reserved[13];
#endif
} AX_ENGINE_IO_INFO_T;

typedef struct _AX_ENGINE_IO_BUFFER_T {
    AX_U64 phyAddr; AX_VOID *pVirAddr; AX_U32 nSize; AX_S32 *pStride; AX_U8 nStrideSize;
#if defined(__aarch64__)
    AX_U64 u64Reserved[11];
#elif defined(__arm__)
    AX_U64 u64Reserved[13];
#endif
} AX_ENGINE_IO_BUFFER_T;

typedef struct _AX_ENGINE_IO_SETTING_T AX_ENGINE_IO_SETTING_T; /* opaque: we pass NULL */

typedef struct _AX_ENGINE_IO_T {
    AX_ENGINE_IO_BUFFER_T *pInputs; AX_U32 nInputSize; AX_ENGINE_IO_BUFFER_T *pOutputs; AX_U32 nOutputSize;
    AX_U32 nBatchSize; AX_ENGINE_IO_SETTING_T *pIoSetting;
#if defined(__aarch64__)
    AX_U64 u64Reserved[11];
#elif defined(__arm__)
    AX_U64 u64Reserved[13];
#endif
} AX_ENGINE_IO_T;

extern AX_S32 AX_SYS_Init(AX_VOID);
extern AX_S32 AX_SYS_Deinit(AX_VOID);
extern AX_S32 AX_SYS_MemAlloc(AX_U64 *phy, AX_VOID **vir, AX_U32 size, AX_U32 align, const AX_S8 *token);
extern AX_S32 AX_SYS_MemFree(AX_U64 phy, AX_VOID *vir);
extern AX_S32 AX_ENGINE_Init(AX_ENGINE_NPU_ATTR_T *attr);
extern AX_S32 AX_ENGINE_Deinit(AX_VOID);
extern AX_S32 AX_ENGINE_CreateHandle(AX_ENGINE_HANDLE *pH, const AX_VOID *pData, AX_U32 nSize);
extern AX_S32 AX_ENGINE_DestroyHandle(AX_ENGINE_HANDLE h);
extern AX_S32 AX_ENGINE_GetIOInfo(AX_ENGINE_HANDLE h, AX_ENGINE_IO_INFO_T **pIO);
extern AX_S32 AX_ENGINE_RunSync(AX_ENGINE_HANDLE h, AX_ENGINE_IO_T *pIO);
/* ---- end verbatim ABI block ---- */

/* Confirmed live via AX_ENGINE_GetIOInfo, not assumed: docs/18-npu-confirmed.md Sec 1. */
#define MODEL_W 224
#define MODEL_H 224
#define MODEL_C 3
#define OUT_FEAT_FLOATS 512

static void die(const char *msg) {
    fprintf(stderr, "kibble-embed: %s\n", msg);
    exit(1);
}

static void die_code(const char *what, AX_S32 rc) {
    fprintf(stderr, "kibble-embed: %s failed (0x%08x)\n", what, (unsigned)rc);
    exit(1);
}

/* Plain bilinear resize, interleaved RGB24, no external dependency. This classifier only needs
 * a stable, repeatable transform from "whatever size the vendor cropped" to the model's fixed
 * input -- not photographic resize quality -- so a from-scratch filter is appropriate here. */
static void resize_bilinear_rgb(const unsigned char *src, int sw, int sh,
                                 unsigned char *dst, int dw, int dh) {
    for (int y = 0; y < dh; y++) {
        float sy = (sh <= 1) ? 0.0f : ((float)y + 0.5f) * (float)sh / (float)dh - 0.5f;
        int y0 = (int)floorf(sy);
        float fy = sy - (float)y0;
        int y0c = y0 < 0 ? 0 : (y0 >= sh ? sh - 1 : y0);
        int y1c = (y0 + 1) < 0 ? 0 : ((y0 + 1) >= sh ? sh - 1 : (y0 + 1));
        for (int x = 0; x < dw; x++) {
            float sx = (sw <= 1) ? 0.0f : ((float)x + 0.5f) * (float)sw / (float)dw - 0.5f;
            int x0 = (int)floorf(sx);
            float fx = sx - (float)x0;
            int x0c = x0 < 0 ? 0 : (x0 >= sw ? sw - 1 : x0);
            int x1c = (x0 + 1) < 0 ? 0 : ((x0 + 1) >= sw ? sw - 1 : (x0 + 1));
            for (int c = 0; c < 3; c++) {
                float p00 = (float)src[(y0c * sw + x0c) * 3 + c];
                float p01 = (float)src[(y0c * sw + x1c) * 3 + c];
                float p10 = (float)src[(y1c * sw + x0c) * 3 + c];
                float p11 = (float)src[(y1c * sw + x1c) * 3 + c];
                float top = p00 + (p01 - p00) * fx;
                float bot = p10 + (p11 - p10) * fx;
                float v = top + (bot - top) * fy;
                dst[(y * dw + x) * 3 + c] = (unsigned char)(v + 0.5f);
            }
        }
    }
}

int main(int argc, char **argv) {
    if (argc != 3) {
        die("usage: kibble-embed <model.axmodel> <crop.jpg>");
    }
    const char *model_path = argv[1];
    const char *jpeg_path = argv[2];

    /* 1. Decode the crop. req_comp=3 forces interleaved RGB24 output even for a grayscale
     * source JPEG, so the resize/model-feed step below never needs to branch on channel count. */
    int sw, sh, src_channels;
    unsigned char *decoded = stbi_load(jpeg_path, &sw, &sh, &src_channels, 3);
    if (!decoded) {
        fprintf(stderr, "kibble-embed: decode %s: %s\n", jpeg_path, stbi_failure_reason());
        return 1;
    }

    /* 2. Resize to the confirmed model input shape. */
    unsigned char *input = malloc((size_t)MODEL_W * MODEL_H * MODEL_C);
    if (!input) {
        stbi_image_free(decoded);
        die("out of memory (input buffer)");
    }
    resize_bilinear_rgb(decoded, sw, sh, input, MODEL_W, MODEL_H);
    stbi_image_free(decoded);

    /* 3. Load the model file whole -- CreateHandle takes the raw container bytes. */
    FILE *f = fopen(model_path, "rb");
    if (!f) {
        fprintf(stderr, "kibble-embed: open %s: %s\n", model_path, strerror(errno));
        free(input);
        return 1;
    }
    fseek(f, 0, SEEK_END);
    long model_sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    unsigned char *model_buf = malloc((size_t)model_sz);
    if (!model_buf || fread(model_buf, 1, (size_t)model_sz, f) != (size_t)model_sz) {
        fclose(f);
        free(input);
        die("read model file");
    }
    fclose(f);

    /* 4. NPU sequence -- byte-for-byte the proven tools/kibble-npu.c call order. */
    AX_S32 rc;
    if ((rc = AX_SYS_Init()) != 0) die_code("AX_SYS_Init", rc);

    AX_ENGINE_NPU_ATTR_T attr;
    memset(&attr, 0, sizeof(attr));
    attr.eHardMode = AX_ENGINE_VIRTUAL_NPU_DISABLE; /* matches live /proc/ax_proc/npu/vnpu=disable */
    if ((rc = AX_ENGINE_Init(&attr)) != 0) die_code("AX_ENGINE_Init", rc);

    AX_ENGINE_HANDLE handle = NULL;
    if ((rc = AX_ENGINE_CreateHandle(&handle, model_buf, (AX_U32)model_sz)) != 0)
        die_code("AX_ENGINE_CreateHandle", rc);

    AX_ENGINE_IO_INFO_T *io_info = NULL;
    if ((rc = AX_ENGINE_GetIOInfo(handle, &io_info)) != 0) die_code("AX_ENGINE_GetIOInfo", rc);

    if (io_info->nInputSize != 1 || io_info->nOutputSize != 2) {
        fprintf(stderr, "kibble-embed: unexpected IO shape (in=%u out=%u) -- model changed?\n",
                (unsigned)io_info->nInputSize, (unsigned)io_info->nOutputSize);
        return 1;
    }
    if (io_info->pInputs[0].nSize != (AX_U32)(MODEL_W * MODEL_H * MODEL_C)) {
        fprintf(stderr, "kibble-embed: unexpected input size %u, expected %d -- model changed?\n",
                (unsigned)io_info->pInputs[0].nSize, MODEL_W * MODEL_H * MODEL_C);
        return 1;
    }

    AX_ENGINE_IO_BUFFER_T in_buf, out_bufs[2];
    memset(&in_buf, 0, sizeof(in_buf));
    memset(out_bufs, 0, sizeof(out_bufs));

    AX_U64 phy;
    AX_VOID *vir;
    if ((rc = AX_SYS_MemAlloc(&phy, &vir, io_info->pInputs[0].nSize, 16, (const AX_S8 *)"kibble_in")) != 0)
        die_code("AX_SYS_MemAlloc(input)", rc);
    memcpy(vir, input, io_info->pInputs[0].nSize);
    in_buf.phyAddr = phy;
    in_buf.pVirAddr = vir;
    in_buf.nSize = io_info->pInputs[0].nSize;

    for (AX_U32 i = 0; i < 2; i++) {
        if ((rc = AX_SYS_MemAlloc(&phy, &vir, io_info->pOutputs[i].nSize, 16, (const AX_S8 *)"kibble_out")) != 0)
            die_code("AX_SYS_MemAlloc(output)", rc);
        out_bufs[i].phyAddr = phy;
        out_bufs[i].pVirAddr = vir;
        out_bufs[i].nSize = io_info->pOutputs[i].nSize;
    }

    AX_ENGINE_IO_T io;
    memset(&io, 0, sizeof(io));
    io.pInputs = &in_buf;
    io.nInputSize = 1;
    io.pOutputs = out_bufs;
    io.nOutputSize = 2;
    if ((rc = AX_ENGINE_RunSync(handle, &io)) != 0) die_code("AX_ENGINE_RunSync", rc);

    /* 5. Identify feat (512 floats) vs prob (1 float) by size+dtype rather than assuming ordinal
     * position -- docs/18-npu-confirmed.md observed output[0]=feat/output[1]=prob live, but that
     * ordering is the model file's own metadata, not part of the ABI contract this project
     * pinned down, so this does not hardcode it. */
    int feat_idx = -1, prob_idx = -1;
    for (AX_U32 i = 0; i < 2; i++) {
        if (io_info->pOutputs[i].eDataType != AX_ENGINE_DT_FLOAT32) continue;
        if (io_info->pOutputs[i].nSize == OUT_FEAT_FLOATS * sizeof(float)) feat_idx = (int)i;
        else if (io_info->pOutputs[i].nSize == sizeof(float)) prob_idx = (int)i;
    }
    if (feat_idx < 0 || prob_idx < 0) {
        fprintf(stderr, "kibble-embed: could not identify feat/prob outputs by size+dtype\n");
        return 1;
    }

    float out[OUT_FEAT_FLOATS + 1];
    memcpy(out, out_bufs[feat_idx].pVirAddr, OUT_FEAT_FLOATS * sizeof(float));
    memcpy(out + OUT_FEAT_FLOATS, out_bufs[prob_idx].pVirAddr, sizeof(float));

    /* Raw little-endian bytes on stdout -- this device is little-endian ARM throughout, so a
     * native fwrite here is already the exact byte order agent/src/embed.rs's `from_le_bytes`
     * reader expects. No text/JSON on this path. */
    size_t written = fwrite(out, 1, sizeof(out), stdout);
    fflush(stdout);

    AX_SYS_MemFree(in_buf.phyAddr, in_buf.pVirAddr);
    AX_SYS_MemFree(out_bufs[0].phyAddr, out_bufs[0].pVirAddr);
    AX_SYS_MemFree(out_bufs[1].phyAddr, out_bufs[1].pVirAddr);
    AX_ENGINE_DestroyHandle(handle);
    AX_ENGINE_Deinit();
    AX_SYS_Deinit();
    free(model_buf);
    free(input);

    return written == sizeof(out) ? 0 : 1;
}
