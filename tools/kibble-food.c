/* kibble-food.c -- second-process bowl-fill inference for Petkit D4SH2 (AX620Q), Kibble's own
 * on-device replacement for the vendor's cloud-gated "leftover food" score.
 *
 * docs/34-bowl-fill-surplus.md Part 6 disassembly-proved that `config_shm`'s BOWL_FILL_1 field is
 * not a physical sensor reading at all: it is `(int)(score * 100.0f)`, where `score` is a
 * 0.0-1.0 bowl-fullness estimate from a vision model
 * (`/alg/petkit_pp_fooddet_416_128_segreg_0509_u16.axmodel`) that only `media`'s own cloud-gated
 * pipeline was ever observed to run. Part 7 disassembled that model's real wrapper,
 * `/alg/libalgo.so`'s `CPetkitAlgoFoodDetect` class, closely enough to drive it directly:
 * `petkit_algo_model_run(buf, w, h, tag, cb)` expects a plain, tightly-packed, interleaved RGB24
 * buffer exactly [`FRAME_W`] columns by [`FRAME_H`] rows (it crops a hardcoded rows-182..360/
 * cols-0..576 band internally -- a camera-specific "where the bowl sits in frame" region, not the
 * model's own 416x128 input, which it resizes down to itself via OpenCV), applies the vendor's
 * own low-light gain and postprocessing (unmodified -- this helper calls the vendor's real
 * compiled code, never a reimplementation of it), and reports the result through the `cb(float)`
 * callback -- the same 0.0-1.0 score `media`'s own callback multiplies by 100 before landing it
 * in `config_shm`.
 *
 * Unlike kibble-embed.c (which drives a bare `.axmodel` through raw AX_ENGINE_* calls this
 * project owns end to end), this helper deliberately dlopen()s the vendor's own `/alg/libalgo.so`
 * and dlsym()s `CPetkitAlgoFoodDetect`'s real, mangled methods -- see docs/34-bowl-fill-surplus.md
 * Part 7 for the disassembly (constructor size, method signatures, crop rectangle, resize target,
 * the tag-dependent score formula) this call sequence is pinned from. Reimplementing that
 * post-processing ourselves would risk a plausible-looking but silently wrong score; calling the
 * vendor's own compiled function guarantees byte-identical behaviour to what `media` itself runs.
 *
 * `AX_SYS_Init`/`AX_ENGINE_Init`/`AX_ENGINE_Deinit`/`AX_SYS_Deinit` are NOT inside libalgo.so's
 * own init/deinit (confirmed by disassembly -- `petkit_algo_model_init` goes straight to
 * `AX_ENGINE_CreateHandle`; `media` calls the engine-level Init/Deinit itself, once, outside any
 * single algo class's own init): this helper calls them itself, directly-linked against
 * libax_engine.so/libax_sys.so exactly as kibble-embed.c already does (build/deploy:
 * tools/README.md).
 *
 * Usage: kibble-food <model.axmodel> <frame.jpg>
 * Prints exactly one line, `score=<0.0-1.0 float>`, and exits 0 on success. Any failure (bad
 * args, JPEG decode, dlopen/dlsym, AX_SYS/AX_ENGINE/libalgo error, non-finite score) prints a
 * message to stderr and exits nonzero -- never a crash. Unlike kibble-embed.c's own error paths
 * (which exit immediately on any AX_* failure, some after AX_ENGINE_Init has already succeeded),
 * every exit path here tears the NPU down first: this helper is meant to run unattended and
 * periodically from `kibbled`, where a leak-on-error would accumulate, not just once per manual
 * face-label click.
 */
#include <dlfcn.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_JPEG
/* See kibble-embed.c's identical comment: excludes the pow()-calling float-HDR path that would
 * otherwise pull in a too-new GLIBC symbol version from the cross toolchain's libm. */
#define STBI_NO_LINEAR
#include "third_party/stb_image.h"

/* ---- AX Engine ABI: only the engine-level lifecycle calls this helper drives itself; verbatim
 * from tools/kibble-embed.c / docs/18-npu-confirmed.md Sec 2. libalgo.so's own CreateHandle/
 * GetIOInfo/RunSync/MemAlloc calls are never touched directly here -- CPetkitAlgoFoodDetect's
 * real methods (dlsym'd below) do all of that internally. ---- */
typedef unsigned int AX_U32;
typedef int AX_S32;
typedef void AX_VOID;
typedef enum { AX_ENGINE_VIRTUAL_NPU_DISABLE = 0, AX_ENGINE_VIRTUAL_NPU_ENABLE = 1 } AX_ENGINE_NPU_MODE_T;
typedef struct { AX_ENGINE_NPU_MODE_T eHardMode; AX_U32 reserve[8]; } AX_ENGINE_NPU_ATTR_T;

extern AX_S32 AX_SYS_Init(AX_VOID);
extern AX_S32 AX_SYS_Deinit(AX_VOID);
extern AX_S32 AX_ENGINE_Init(AX_ENGINE_NPU_ATTR_T *attr);
extern AX_S32 AX_ENGINE_Deinit(AX_VOID);
/* ---- end AX Engine ABI block ---- */

#define LIBALGO_PATH "/alg/libalgo.so"

/* model_run's own hardcoded crop is rows [182,360) x cols [0,576) of whatever w x h buffer the
 * caller passes -- docs/34-bowl-fill-surplus.md Part 7 (`cv::Mat(src, Range(182,360),
 * Range(0,576))`, read directly out of the disassembled literal pool). w/h MUST be exactly these
 * values: anything smaller throws a C++ exception this plain-C caller cannot catch (libalgo.so is
 * compiled C++; an uncaught exception unwinding into a C frame calls std::terminate, exactly the
 * crash this helper must never produce), and there is no reason to pass anything larger and
 * effectively crop-select only part of the frame instead of the intended "resize the whole scene
 * down to the pipeline's own working resolution" transform -- 576x360 is exactly the vendor's own
 * 1152x720 sub-stream at a clean 0.5x (docs/34 Part 7's visual confirmation: the bowl sits in
 * roughly the bottom half of that frame, matching the crop rectangle exactly). */
#define FRAME_W 576
#define FRAME_H 360

/* `operator new(0xb8)` (184 bytes) is the exact, confirmed-live allocation size
 * `petkit_algo_create` uses immediately before constructing its own `CPetkitAlgoFoodDetect`
 * (docs/34-bowl-fill-surplus.md Part 7) -- not a guess or a margin-padded estimate. */
#define FOODDETECT_OBJ_SIZE 0xb8

/* The exact device-model tag `media` itself passes to `petkit_algo_food_detect_run` -- read live
 * out of `config_shm` offset 4832 on this device (`"D4SH"`, confirmed byte-for-byte) and matched
 * against the one literal string `petkit_food_detect_process` compares it against to select its
 * D4SH-tuned score coefficients (0.15/0.4/1.0) over the fallback set (0.15/0.3/0.6) -- see Part 7.
 * A hardcoded literal, not read from config_shm at runtime: this helper is already Petkit-D4SH2-
 * specific in a dozen other ways (the model path, the crop rectangle, the frame size). Not
 * `const`: the real function's own signature takes a plain (never observed written) `char*`. */
static char TAG[] = "D4SH";

/* libstdc++'s __cxx11::basic_string<char> ABI: {char* ptr; size_t len; union { size_t cap; char
 * buf[16]; };} = 24 bytes on a 32-bit target. Hand-built here, never via a real std::string
 * constructor call, so this helper never needs to dlopen libstdc++ itself: it only ever needs to
 * hand a `const std::string&` to one real libstdc++-ABI constructor (CPetkitAlgoFoodDetect's),
 * which copies the bytes out immediately -- this temporary's lifetime can end the moment that
 * call returns, exactly as it would for any C++ caller passing a temporary by const reference. */
typedef struct {
    char *ptr;
    uint32_t len;
    union {
        uint32_t cap;
        char buf[16];
    } u;
} cxx_string;
#define CXX_STRING_SSO_CAP 15

static void cxx_string_init(cxx_string *s, const char *text) {
    size_t n = strlen(text);
    if (n <= CXX_STRING_SSO_CAP) {
        s->ptr = s->u.buf;
        memcpy(s->u.buf, text, n + 1);
    } else {
        s->ptr = malloc(n + 1);
        if (!s->ptr) {
            fprintf(stderr, "kibble-food: out of memory (model path string)\n");
            exit(1);
        }
        memcpy(s->ptr, text, n + 1);
        s->u.cap = (uint32_t)n;
    }
    s->len = (uint32_t)n;
}

static void cxx_string_free(cxx_string *s) {
    if (s->ptr != s->u.buf) {
        free(s->ptr);
    }
}

/* CPetkitAlgoFoodDetect's real, mangled methods -- signatures pinned by disassembly,
 * docs/34-bowl-fill-surplus.md Part 7. All are plain AAPCS member-function calls (`self` occupies
 * the first argument slot exactly like any other pointer parameter); no C++ runtime of our own is
 * needed to call them, only to have built `self`'s storage and the string argument correctly. */
typedef void (*fooddetect_ctor_fn)(void *self, const cxx_string *model_path);
typedef AX_S32 (*model_init_fn)(void *self);
typedef AX_S32 (*model_run_fn)(void *self, unsigned char *buf, int w, int h, char *tag,
                                void (*cb)(float));
typedef void (*model_deinit_fn)(void *self);

static void *must_dlsym(void *lib, const char *sym) {
    dlerror();
    void *p = dlsym(lib, sym);
    const char *err = dlerror();
    if (err) {
        fprintf(stderr, "kibble-food: dlsym %s: %s\n", sym, err);
        dlclose(lib);
        exit(1);
    }
    return p;
}

/* Plain bilinear resize, interleaved RGB24 -- verbatim from tools/kibble-embed.c: this helper
 * needs the exact same "whatever the source frame's size, get to a fixed working size" transform,
 * just a different target size. */
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

static float g_score;
static int g_score_set;

/* AAPCS-VFP: a lone `float` argument arrives in `s0`, matching `media`'s own callback
 * (`petkit_food_detect_callback`, docs/34 Part 6) exactly -- this is not a coincidence, it is the
 * same function-pointer type `petkit_algo_model_run`'s signature requires. */
static void score_callback(float score) {
    g_score = score;
    g_score_set = 1;
}

static void die(const char *msg) {
    fprintf(stderr, "kibble-food: %s\n", msg);
    exit(1);
}

int main(int argc, char **argv) {
    if (argc != 3) {
        die("usage: kibble-food <model.axmodel> <frame.jpg>");
    }
    const char *model_path = argv[1];
    const char *jpeg_path = argv[2];

    /* 1. Decode + resize to exactly FRAME_W x FRAME_H interleaved RGB24 -- see the FRAME_W/H
     * comment above for why this exact size is required, not just "big enough". */
    int sw, sh, src_channels;
    unsigned char *decoded = stbi_load(jpeg_path, &sw, &sh, &src_channels, 3);
    if (!decoded) {
        fprintf(stderr, "kibble-food: decode %s: %s\n", jpeg_path, stbi_failure_reason());
        return 1;
    }
    unsigned char *frame = malloc((size_t)FRAME_W * FRAME_H * 3);
    if (!frame) {
        stbi_image_free(decoded);
        die("out of memory (frame buffer)");
    }
    resize_bilinear_rgb(decoded, sw, sh, frame, FRAME_W, FRAME_H);
    stbi_image_free(decoded);

    /* 2. dlopen the vendor's own algo library and resolve the exact methods this helper needs.
     * Loading it runs its file-scope C++ static initializers (iostream setup, OpenCV lookup
     * tables, per-class name-string globals) -- confirmed by disassembly, docs/34 Part 7, to be
     * inert CPU-only bookkeeping; nothing touches AX_SYS/AX_ENGINE until explicitly called below.
     *
     * RTLD_LAZY, not RTLD_NOW: confirmed live (readelf -sW) that libalgo.so carries at least one
     * genuinely unresolved import of its own -- `jas_image_writecmpt`, a JasPer/JPEG2000 symbol
     * with no corresponding DT_NEEDED entry at all, evidently expected to be satisfied by
     * whatever else happens to be loaded in `media`'s own process (this project's codepath never
     * touches OpenCV's JPEG2000 codec, so it is never actually called). RTLD_NOW eagerly resolves
     * every import at dlopen time and fails outright on that one; RTLD_LAZY defers each symbol to
     * its first real call, which this helper's own call sequence never reaches. */
    void *lib = dlopen(LIBALGO_PATH, RTLD_LAZY);
    if (!lib) {
        fprintf(stderr, "kibble-food: dlopen %s: %s\n", LIBALGO_PATH, dlerror());
        return 1;
    }
    fooddetect_ctor_fn ctor = (fooddetect_ctor_fn)must_dlsym(
        lib, "_ZN21CPetkitAlgoFoodDetectC1ERKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEE");
    model_init_fn model_init =
        (model_init_fn)must_dlsym(lib, "_ZN21CPetkitAlgoFoodDetect22petkit_algo_model_initEv");
    model_run_fn model_run = (model_run_fn)must_dlsym(
        lib, "_ZN21CPetkitAlgoFoodDetect21petkit_algo_model_runEPhiiPcPFvfE");
    model_deinit_fn model_deinit =
        (model_deinit_fn)must_dlsym(lib, "_ZN21CPetkitAlgoFoodDetect24petkit_algo_model_deinitEv");

    /* 3. Engine-level lifecycle -- this helper's own responsibility, not libalgo.so's (see the
     * module doc). Mirrors tools/kibble-embed.c's proven sequence exactly. */
    AX_S32 rc = AX_SYS_Init();
    if (rc != 0) {
        fprintf(stderr, "kibble-food: AX_SYS_Init failed (0x%08x)\n", (unsigned)rc);
        dlclose(lib);
        return 1;
    }
    AX_ENGINE_NPU_ATTR_T attr;
    memset(&attr, 0, sizeof(attr));
    attr.eHardMode = AX_ENGINE_VIRTUAL_NPU_DISABLE; /* matches live /proc/ax_proc/npu/vnpu=disable */
    rc = AX_ENGINE_Init(&attr);
    if (rc != 0) {
        fprintf(stderr, "kibble-food: AX_ENGINE_Init failed (0x%08x)\n", (unsigned)rc);
        AX_SYS_Deinit();
        dlclose(lib);
        return 1;
    }

    /* 4. Construct a CPetkitAlgoFoodDetect the exact same way libalgo.so's own petkit_algo_create
     * does: an operator-new-sized buffer plus the real mangled constructor (docs/34 Part 7). */
    void *obj = calloc(1, FOODDETECT_OBJ_SIZE);
    int exit_code = 0;
    if (!obj) {
        fprintf(stderr, "kibble-food: out of memory (algo object)\n");
        exit_code = 1;
    } else {
        cxx_string path_arg;
        cxx_string_init(&path_arg, model_path);
        ctor(obj, &path_arg);
        cxx_string_free(&path_arg);

        rc = model_init(obj);
        if (rc != 0) {
            fprintf(stderr,
                    "kibble-food: petkit_algo_model_init failed (rc=%d) -- is %s present and "
                    "readable?\n",
                    rc, model_path);
            exit_code = 1;
            /* Nothing to tear down: model_init's own internal error paths already unwind
             * whatever partial state (handle, IO buffers) they created before returning to us --
             * calling model_deinit on a never-fully-inited object risks reading garbage fields. */
        } else {
            g_score_set = 0;
            rc = model_run(obj, frame, FRAME_W, FRAME_H, TAG, score_callback);
            if (rc != 0) {
                fprintf(stderr,
                        "kibble-food: petkit_algo_model_run failed (rc=%d) -- NPU busy or model "
                        "error\n",
                        rc);
                exit_code = 1;
            } else if (!g_score_set) {
                fprintf(stderr, "kibble-food: model ran but the score callback never fired\n");
                exit_code = 1;
            } else if (!isfinite(g_score)) {
                fprintf(stderr, "kibble-food: model produced a non-finite score (%f)\n",
                        (double)g_score);
                exit_code = 1;
            }
            model_deinit(obj);
        }
        free(obj);
    }

    /* 5. Always tear down the engine-level state step 3 brought up, regardless of which step
     * above failed -- "never leave the NPU initialised" applies to every exit path, not just the
     * success one. */
    AX_ENGINE_Deinit();
    AX_SYS_Deinit();
    dlclose(lib);
    free(frame);

    if (exit_code != 0) {
        return exit_code;
    }
    printf("score=%.4f\n", (double)g_score);
    fflush(stdout);
    return 0;
}
