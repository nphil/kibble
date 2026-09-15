
/* kibble-msg: send one message on the Petkit internal dispatch bus.
 * Wire format recovered from ctrl dispatch_send_msg @0x80b00:
 *   u16 msg_id | u16 src | payload[len]      (mq_send size = 4+len, prio 0, max total 544)
 * Feed payload (67 B) from ctrl dispatch_handler_feed/@0x44020:
 *   u8 cancel | char id[64] | u8 amount1 | u8 amount2
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <mqueue.h>
#include <errno.h>

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s <dst> <msg_id_hex> <src> [feed:a1:a2:id | hex:AABB..]\n", argv[0]);
        return 2;
    }
    unsigned dst = strtoul(argv[1], 0, 0);
    unsigned msg_id = strtoul(argv[2], 0, 16);
    unsigned src = strtoul(argv[3], 0, 0);
    unsigned char buf[544]; memset(buf, 0, sizeof buf);
    buf[0] = msg_id & 0xff; buf[1] = (msg_id >> 8) & 0xff;
    buf[2] = src & 0xff;    buf[3] = (src >> 8) & 0xff;
    size_t plen = 0;
    if (argc > 4 && !strncmp(argv[4], "feed:", 5)) {
        char *s = argv[4] + 5;
        unsigned a1 = strtoul(strtok(s, ":"), 0, 0);
        char *t = strtok(0, ":"); unsigned a2 = t ? strtoul(t, 0, 0) : 0;
        char *id = strtok(0, ":");
        plen = 67;
        buf[4] = 0;                                   /* 0 = dispense, 1 = cancel */
        if (id) strncpy((char *)buf + 5, id, 63);     /* id[64] at payload+1 */
        buf[4 + 65] = a1 & 0xff;
        buf[4 + 66] = a2 & 0xff;
    } else if (argc > 4 && !strncmp(argv[4], "hex:", 4)) {
        char *h = argv[4] + 4;
        for (; h[0] && h[1] && plen < 540; h += 2, plen++)
            { char o[3] = {h[0], h[1], 0}; buf[4 + plen] = (unsigned char)strtoul(o, 0, 16); }
    }
    char q[64]; snprintf(q, sizeof q, "/msg_dispatch_%u", dst);
    mqd_t mq = mq_open(q, O_WRONLY);
    if (mq == (mqd_t)-1) { fprintf(stderr, "mq_open(%s): %s\n", q, strerror(errno)); return 1; }
    printf("send %s msg_id=0x%04x src=%u payload=%zu total=%zu:", q, msg_id, src, plen, plen + 4);
    for (size_t i = 0; i < plen + 4; i++) printf("%s%02x", (i == 4 ? " | " : " "), buf[i]);
    printf("\n");
    if (mq_send(mq, (char *)buf, plen + 4, 0) < 0) { fprintf(stderr, "mq_send: %s\n", strerror(errno)); return 1; }
    printf("ok\n"); mq_close(mq); return 0;
}

