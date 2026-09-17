#!/bin/sh
# /opt/app_init.sh - Kibble boot hook. Stock init runs first, unconditionally.
# Recovery: telnet in (S50telnet starts independently, earlier) and rm this file.
#
# Logging policy (Nitin, 2026-09-15): NOTHING is logged to the feeder's own flash.
# /opt is UBIFS on raw NAND with finite erase cycles, so:
#   - kibbled's own stdout/stderr -> /tmp/kibbled.log (tmpfs, volatile, free)
#   - one start line and one exit line per run -> remote syslog on the Unraid box
#     (rsyslogd, UDP 514, verified listening), so crash history survives a reboot
#     without a single flash write.
#
# Why the start/exit lines matter: before this, the supervisor discarded kibbled's
# output entirely and kept no record of restarts, which made "did it crash?" an
# unanswerable question -- and an autonomous restart silently killing the vendor's
# talkback session cost a whole debugging session to find. The exit code is the
# evidence (rc=134 is SIGABRT, i.e. a Rust panic under panic=abort).
#
# Deliberately NOT piping kibbled's stdout into nc: if the nc process died, the
# next write would raise SIGPIPE and kill kibbled. The log sink must never be able
# to take down the thing it is logging.
/app/script/app_init.sh &
[ -f /opt/kibble/disabled ] && exit 0
[ -x /opt/kibble/kibbled ] || exit 0

SYSLOG_HOST=192.168.1.69
SYSLOG_PORT=514

# <14> = facility 1 (user) severity 6 (info), per RFC 3164's PRI encoding.
notify() {
  [ -x /usr/bin/nc ] || return 0
  echo "<14>kibbled: $1" | nc -u -w 1 "$SYSLOG_HOST" "$SYSLOG_PORT" 2>/dev/null
}

# /tmp is a 46 MB tmpfs backed by the device's 92 MB of RAM: cap kibbled's own log so a
# chatty failure can never eat memory. Truncating a file the process still writes with `>`
# leaves a hole at the old offset, which tmpfs does not allocate -- so this is safe to do
# while kibbled runs, and costs nothing when the log is small.
(
  while :; do
    sleep 3600
    [ "$(wc -c < /tmp/kibbled.log 2>/dev/null || echo 0)" -gt 4194304 ] && : > /tmp/kibbled.log
  done
) &

(
  sleep 20
  i=0
  while :; do
    i=$((i + 1))
    notify "start #$i uptime=$(cut -d' ' -f1 /proc/uptime) md5=$(md5sum /opt/kibble/kibbled 2>/dev/null | cut -c1-8)"
    /opt/kibble/kibbled >/tmp/kibbled.log 2>&1
    rc=$?
    notify "exit #$i rc=$rc uptime=$(cut -d' ' -f1 /proc/uptime) tail=$(tail -c 200 /tmp/kibbled.log 2>/dev/null | tr '\n' ' ')"
    sleep 5
  done
) &
exit 0
