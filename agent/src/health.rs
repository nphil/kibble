//! Process-restart visibility (docs/23-audio-codec.md §19): a silently crash-looping `kibbled`
//! turned out to be the more serious bug a wedged-audio investigation surfaced -- the boot
//! script's own supervisor (`>/dev/null 2>&1`, no logging at all) made "is it restarting?" an
//! unanswerable question for hours. [`record_start`] reads, increments, and persists a start
//! counter plus this instance's own start time on every process start, and [`last_exit_code`]
//! reads the boot script's own durable forensic log (`scripts/app_init.sh`, deployed as
//! `/opt/app_init.sh`) for the most recent exit code, so `GET /state` (and, via it, a Home
//! Assistant diagnostic sensor) can show a human that the agent keeps restarting -- and roughly
//! why -- instead of that only being discoverable by disassembling a symptom.

use std::fs;
use std::time::{SystemTime, UNIX_EPOCH};

// Both paths are tmpfs, deliberately: nothing about restart bookkeeping is written to the
// feeder's flash (Nitin, 2026-09-15). `/opt` is UBIFS on raw NAND with finite erase cycles, and a
// counter rewritten on every process start is exactly the kind of small, frequent write that
// wears it. The cost is that the count resets on reboot -- which is fine, because the question it
// answers is "is kibbled crash-looping *right now*", and the durable history lives in remote
// syslog on the Unraid box (see `scripts/app_init.sh`), not on the device.
const PATH: &str = "/tmp/kibble-health.json";
/// The boot script's own record (`scripts/app_init.sh`). kibbled never writes this file itself,
/// only reads it, so a read failure (missing on a system not yet running the new boot script) is
/// just "unknown", not an error worth surfacing.
const RESTARTS_LOG: &str = "/tmp/kibbled-restarts.log";

#[derive(Debug, Clone, Copy)]
pub struct Health {
    pub start_count: u64,
    pub last_start_unix: u64,
}

/// Reads the previous count (0 if the file is missing or doesn't parse -- the first boot ever,
/// or a fresh `/opt/kibble`), increments it, writes the new count and this instant's wall-clock
/// start time back out, and returns the result. Best-effort: a write failure here must never
/// stop `kibbled` from starting, so it is logged and swallowed rather than propagated.
pub fn record_start() -> Health {
    let prev_count = fs::read_to_string(PATH).ok().and_then(|s| parse_count(&s)).unwrap_or(0);
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let count = prev_count + 1;
    let json = format!(r#"{{"start_count":{count},"last_start_unix":{now}}}"#);
    if let Err(e) = fs::write(PATH, json) {
        eprintln!("kibbled: could not persist {PATH}: {e} (start_count will read as 0 on next boot)");
    }
    Health { start_count: count, last_start_unix: now }
}

/// The exit code from the most recent `=== exit  #N ... rc=<code> ===` line in
/// `scripts/app_init.sh`'s durable restart log, or `None` if the log doesn't exist yet (a system
/// not yet running the updated boot script) or has no exit line at all (kibbled has never yet
/// exited since that log started, i.e. this is still its first, still-running start).
pub fn last_exit_code() -> Option<i32> {
    let text = fs::read_to_string(RESTARTS_LOG).ok()?;
    let last_exit_line = text.lines().rev().find(|l| l.contains("=== exit"))?;
    let key = "rc=";
    let start = last_exit_line.find(key)? + key.len();
    let rest = &last_exit_line[start..];
    let end = rest.find(|c: char| !c.is_ascii_digit() && c != '-').unwrap_or(rest.len());
    rest[..end].parse().ok()
}

/// Pulls `"start_count":<digits>` out of the small JSON object [`record_start`] itself wrote,
/// tolerant of field order. Hand-rolled, matching this project's no-serialisation-crate
/// convention (`state.rs`'s `Snapshot::to_json`, `bus.rs`'s wire format, etc.).
fn parse_count(s: &str) -> Option<u64> {
    let key = "\"start_count\":";
    let start = s.find(key)? + key.len();
    let rest = &s[start..];
    let end = rest.find(|c: char| !c.is_ascii_digit()).unwrap_or(rest.len());
    rest[..end].parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_count_reads_the_field_regardless_of_field_order() {
        assert_eq!(parse_count(r#"{"start_count":42,"last_start_unix":100}"#), Some(42));
        assert_eq!(parse_count(r#"{"last_start_unix":100,"start_count":7}"#), Some(7));
    }

    #[test]
    fn parse_count_handles_missing_or_garbled_input() {
        assert_eq!(parse_count(""), None);
        assert_eq!(parse_count("not json"), None);
        assert_eq!(parse_count(r#"{"other":1}"#), None);
    }

    fn last_exit_code_from_text(text: &str) -> Option<i32> {
        let last_exit_line = text.lines().rev().find(|l| l.contains("=== exit"))?;
        let key = "rc=";
        let start = last_exit_line.find(key)? + key.len();
        let rest = &last_exit_line[start..];
        let end = rest.find(|c: char| !c.is_ascii_digit() && c != '-').unwrap_or(rest.len());
        rest[..end].parse().ok()
    }

    #[test]
    fn last_exit_code_reads_the_most_recent_exit_line_not_the_first() {
        let log = "=== start #1 at ... ===\n\
                    === exit  #1 at ..., rc=0 ===\n\
                    === start #2 at ... ===\n\
                    === exit  #2 at ..., rc=134 ===\n";
        assert_eq!(last_exit_code_from_text(log), Some(134));
    }

    #[test]
    fn last_exit_code_is_none_when_the_process_has_never_exited_yet() {
        assert_eq!(last_exit_code_from_text("=== start #1 at ... ===\n"), None);
        assert_eq!(last_exit_code_from_text(""), None);
    }
}
