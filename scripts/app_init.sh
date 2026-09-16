#!/bin/sh
# /opt/app_init.sh - Kibble boot hook. Stock init runs first, unconditionally.
# Recovery: telnet in (S50telnet starts independently, earlier) and rm this file.
#
# docs/23-audio-codec.md §19: the previous version of this script piped kibbled's own stdout
# and stderr straight to /dev/null and kept no record of restarts at all -- when kibbled started
# silently crash-looping mid-session (root cause of a real household talkback session getting
# cut off), that was undiagnosable from the device itself; the only evidence was timing
# correlation against unrelated kernel log lines. This version keeps every design goal of the
# original (dead simple, cannot itself hang or fail the boot path, does nothing if kibbled is
# disabled or missing) and adds two logs, split by destination on purpose:
#   - kibbled's own stdout/stderr, potentially high-volume and continuous, goes to /tmp (tmpfs,
#     RAM-backed) so a chatty process cannot wear or fill the flash /opt lives on. Unbounded here
#     is fine -- it cannot outlive a reboot and costs nothing this device needs elsewhere.
#   - the actual forensic record -- one short "start"/"exit" line per relaunch, with a
#     timestamp, uptime, iteration count, and exit code -- goes to /opt (flash) specifically
#     because it must survive a reboot; it is bounded on BOTH line count and byte size so even a
#     pathological fast crash loop cannot grow it without limit.
/app/script/app_init.sh &
[ -f /opt/kibble/disabled ] && exit 0
[ -x /opt/kibble/kibbled ] || exit 0

(
  sleep 20
  OUT=/tmp/kibbled.log
  RESTARTS=/opt/kibble/restarts.log
  MAX_LINES=2000
  KEEP_LINES=1000
  MAX_BYTES=65536
  i=0
  while :; do
    i=$((i + 1))
    echo "=== start #$i at $(date -u +%Y-%m-%dT%H:%M:%SZ), uptime $(cut -d' ' -f1 /proc/uptime) ===" >> "$RESTARTS"
    /opt/kibble/kibbled >> "$OUT" 2>&1
    rc=$?
    echo "=== exit  #$i at $(date -u +%Y-%m-%dT%H:%M:%SZ), uptime $(cut -d' ' -f1 /proc/uptime), rc=$rc ===" >> "$RESTARTS"
    # Bounded on lines AND bytes, checked once per relaunch (never per line), so neither a fast
    # crash loop (many short lines) nor a slow one that somehow writes long lines can grow this
    # file without limit.
    lines=$(wc -l < "$RESTARTS" 2>/dev/null || echo 0)
    bytes=$(wc -c < "$RESTARTS" 2>/dev/null || echo 0)
    if { [ "$lines" -gt "$MAX_LINES" ] || [ "$bytes" -gt "$MAX_BYTES" ]; } 2>/dev/null; then
      tail -n "$KEEP_LINES" "$RESTARTS" > "$RESTARTS.tmp" 2>/dev/null && mv "$RESTARTS.tmp" "$RESTARTS"
    fi
    sleep 5
  done
) &
exit 0
