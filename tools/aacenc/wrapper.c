/* aacenc: raw 16-bit little-endian mono 16 kHz PCM on stdin -> ADTS AAC-LC
 * bitstream on stdout. Spawned as a subprocess by kibbled (kibbled is a
 * fully static musl binary that cannot dlopen the device's libfdk-aac.so,
 * and the cross toolchain's glibc is too new to link against the device's
 * glibc 2.25 directly -- so this is a separate, statically-linked helper).
 *
 * The encoder parameter sequence below, and the expected frameLength /
 * AudioSpecificConfig sanity values, are pinned by on-device disassembly of
 * the vendor's own `media` binary -- see docs/23-audio-codec.md #2.1 in the
 * kibble-audio repo for the full derivation. The buffer-descriptor calling
 * convention follows fdk-aac's own upstream example, aac-enc.c:
 *   https://github.com/mstorsjo/fdk-aac/blob/master/aac-enc.c
 * (that example reads a WAV file; this one reads a raw stdin PCM stream,
 * everything about the aacEncEncode() call shape is otherwise the same).
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <fdk-aac/aacenc_lib.h>

#define SAMPLE_RATE 16000
#define FRAME_SAMPLES 1024               /* pinned: info.frameLength must equal this */
#define FRAME_BYTES (FRAME_SAMPLES * 2)  /* 16-bit mono PCM */
#define OUT_BUF_BYTES 20480              /* matches upstream aac-enc.c's outbuf size */

static int set_param(HANDLE_AACENCODER h, AACENC_PARAM param, UINT value, const char *name) {
    AACENC_ERROR err = aacEncoder_SetParam(h, param, value);
    if (err != AACENC_OK) {
        fprintf(stderr, "aacenc: SetParam(%s, %u) failed: %d\n", name, value, (int)err);
        return 1;
    }
    return 0;
}

/* Read up to `len` bytes from fd into buf, looping on short reads/EINTR.
 * Returns the number of bytes actually read; a return < len means EOF. */
static size_t read_full(int fd, unsigned char *buf, size_t len) {
    size_t got = 0;
    while (got < len) {
        ssize_t n = read(fd, buf + got, len - got);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            perror("aacenc: read(stdin)");
            exit(1);
        }
        if (n == 0)
            break; /* clean EOF */
        got += (size_t)n;
    }
    return got;
}

static void write_full(const void *buf, size_t len) {
    if (len == 0)
        return;
    if (fwrite(buf, 1, len, stdout) != len) {
        perror("aacenc: write(stdout)");
        exit(1);
    }
}

int main(void) {
    /* stdout is piped to a real-time reader on the other end; a full stdio
     * buffer would break pacing, so disable buffering entirely. */
    setvbuf(stdout, NULL, _IONBF, 0);

    HANDLE_AACENCODER handle;
    if (aacEncOpen(&handle, 0, 1) != AACENC_OK) {
        fprintf(stderr, "aacenc: aacEncOpen failed\n");
        return 1;
    }

    int failed = 0;
    failed |= set_param(handle, AACENC_AOT, 2, "AOT");                      /* AAC-LC */
    failed |= set_param(handle, AACENC_SAMPLERATE, SAMPLE_RATE, "SAMPLERATE");
    failed |= set_param(handle, AACENC_CHANNELMODE, MODE_1, "CHANNELMODE"); /* mono */
    failed |= set_param(handle, AACENC_CHANNELORDER, 1, "CHANNELORDER");
    failed |= set_param(handle, AACENC_BITRATEMODE, 3, "BITRATEMODE");      /* VBR mode 3 */
    failed |= set_param(handle, AACENC_TRANSMUX, TT_MP4_ADTS, "TRANSMUX");  /* ADTS framing */
    failed |= set_param(handle, AACENC_AFTERBURNER, 1, "AFTERBURNER");
    if (failed) {
        aacEncClose(&handle);
        return 1;
    }

    /* Mandatory post-SetParam init/flush call. */
    if (aacEncEncode(handle, NULL, NULL, NULL, NULL) != AACENC_OK) {
        fprintf(stderr, "aacenc: init aacEncEncode failed\n");
        aacEncClose(&handle);
        return 1;
    }

    AACENC_InfoStruct info;
    memset(&info, 0, sizeof(info));
    if (aacEncInfo(handle, &info) != AACENC_OK) {
        fprintf(stderr, "aacenc: aacEncInfo failed\n");
        aacEncClose(&handle);
        return 1;
    }
    fprintf(stderr, "aacenc: frameLength=%u confBuf=%02x %02x (confSize=%u)\n",
            (unsigned)info.frameLength, info.confBuf[0], info.confBuf[1],
            (unsigned)info.confSize);
    if (info.frameLength != FRAME_SAMPLES || info.confSize < 2 ||
        info.confBuf[0] != 0x14 || info.confBuf[1] != 0x08) {
        fprintf(stderr,
                "aacenc: encoder params regressed (expected frameLength=%d, confBuf=14 08)\n",
                FRAME_SAMPLES);
        aacEncClose(&handle);
        return 1;
    }

    INT_PCM pcm[FRAME_SAMPLES];
    unsigned char outbuf[OUT_BUF_BYTES];
    int flushing = 0;

    for (;;) {
        int num_in_samples;

        if (!flushing) {
            size_t got = read_full(STDIN_FILENO, (unsigned char *)pcm, FRAME_BYTES);
            if (got == 0) {
                /* Clean EOF right at a chunk boundary: nothing pending, start flushing. */
                flushing = 1;
                num_in_samples = -1;
            } else {
                if (got < FRAME_BYTES) /* EOF mid-chunk: zero-pad the remainder, encode anyway */
                    memset((unsigned char *)pcm + got, 0, FRAME_BYTES - got);
                num_in_samples = FRAME_SAMPLES;
            }
        } else {
            num_in_samples = -1;
        }

        void *in_ptr = pcm;
        int in_identifier = IN_AUDIO_DATA;
        int in_size = flushing ? 0 : FRAME_BYTES;
        int in_elem_size = sizeof(INT_PCM);
        AACENC_BufDesc in_buf;
        memset(&in_buf, 0, sizeof(in_buf));
        in_buf.numBufs = 1;
        in_buf.bufs = &in_ptr;
        in_buf.bufferIdentifiers = &in_identifier;
        in_buf.bufSizes = &in_size;
        in_buf.bufElSizes = &in_elem_size;

        AACENC_InArgs in_args;
        memset(&in_args, 0, sizeof(in_args));
        in_args.numInSamples = num_in_samples;

        void *out_ptr = outbuf;
        int out_identifier = OUT_BITSTREAM_DATA;
        int out_size = sizeof(outbuf);
        int out_elem_size = 1;
        AACENC_BufDesc out_buf;
        memset(&out_buf, 0, sizeof(out_buf));
        out_buf.numBufs = 1;
        out_buf.bufs = &out_ptr;
        out_buf.bufferIdentifiers = &out_identifier;
        out_buf.bufSizes = &out_size;
        out_buf.bufElSizes = &out_elem_size;

        AACENC_OutArgs out_args;
        memset(&out_args, 0, sizeof(out_args));

        AACENC_ERROR enc_err = aacEncEncode(handle, &in_buf, &out_buf, &in_args, &out_args);
        if (enc_err == AACENC_ENCODE_EOF)
            break;
        if (enc_err != AACENC_OK) {
            fprintf(stderr, "aacenc: aacEncEncode failed: %d\n", (int)enc_err);
            aacEncClose(&handle);
            return 1;
        }
        if (out_args.numOutBytes > 0)
            write_full(outbuf, (size_t)out_args.numOutBytes);
        /* numOutBytes == 0 is normal encoder lookahead/priming; write nothing, continue. */
    }

    aacEncClose(&handle);
    return 0;
}
