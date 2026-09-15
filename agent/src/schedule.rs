//! The feed schedule.
//!
//! The MCU owns firing; kibbled owns the only readable copy. STUDY-schedule.md §4 found three
//! independent, mutually-reinforcing dead ends for reading a schedule back (`ctrl`'s own
//! `dispatch_handler_ble_get_schedule` is a stub that always returns 0 and never touches the bus;
//! `ble`'s 30-entry dispatch table has no "get schedule" counterpart; the MCU's CMD 0x04 ack
//! carries only a one-byte result code, never content) — so this module's persisted cache, not a
//! device read, is the source of truth for every `GET`.
//!
//! Write semantics, mirrored from `ctrl` (STUDY-schedule.md §3.2): a write **replaces the whole
//! table** — there is no single-entry add/delete on the wire — and every write re-sends the RTC
//! (msg [`crate::bus::msg::BLE_SET_RTC`]) immediately before the schedule
//! (msg [`crate::bus::msg::BLE_SET_SCHEDULE`]). The cache is written to disk *before* either send,
//! so a crash mid-send still leaves the intent recorded rather than losing it.
//!
//! An entry disabled in the cache is simply omitted from the wire table: `enable` is not a wire
//! field (the 22-byte struct is fully accounted for without one), so "disabled" has no on-device
//! representation to send — STUDY-schedule.md's own struct recovery found no room for it.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::bus::{msg, Sender};
use crate::http::{json_field, query_field};

/// Vendor's hard payload clamp (`bus::MAX_PAYLOAD` = 540 bytes) leaves room for exactly this many
/// 22-byte entries after the 2-byte header (24*22 + 2 = 530 <= 540; a 25th entry would silently
/// overrun mid-struct on the wire instead of failing cleanly) — STUDY-schedule.md TL;DR "Entry
/// capacity". We reject at this boundary ourselves rather than ever relying on that clamp.
pub const MAX_ENTRIES: usize = 24;

/// Byte length of one wire entry (STUDY-schedule.md §3.3: `id[16]` + `amount_l` + `amount_r` +
/// `time` s32).
pub const WIRE_ENTRY_LEN: usize = 22;

/// Default persisted-cache location.
pub const CACHE_PATH: &str = "/opt/kibble/schedule.json";

/// Current Unix time, or 0 if the clock is somehow before the epoch.
pub fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Parses `"HH:MM"` (24-hour) into minutes since local midnight, `0..=1439`.
pub fn parse_time_of_day(s: &str) -> Option<u16> {
    let (h, m) = s.split_once(':')?;
    if h.len() != 2 || m.len() != 2 {
        return None; // reject "7:3", "007:30", etc. -- HH:MM only, matches the app's own field
    }
    let h: u16 = h.parse().ok()?;
    let m: u16 = m.parse().ok()?;
    if h > 23 || m > 59 {
        return None;
    }
    Some(h * 60 + m)
}

/// The inverse of [`parse_time_of_day`].
pub fn format_time_of_day(minute_of_day: u16) -> String {
    format!("{:02}:{:02}", minute_of_day / 60, minute_of_day % 60)
}

/// Converts one entry's local time-of-day into the wire `time` field sent to the MCU.
///
/// **Pending confirmation — see the project report.** STUDY-schedule.md §3.4 (this repo's own
/// disassembly of `ctrl`'s schedule-entry builder) found that `ctrl` discards ("zeroes") any
/// non-negative JSON `t` before it reaches the wire and only preserves a *negative* `t` verbatim
/// — which does not match a plain minute-of-day passthrough for the ordinary case. Independently,
/// `dwyschka/localkit`'s `Time::calculateLatest()` (a production Petkit-cloud reimplementation
/// tuned against real D4SH units — the same model as this device) computes `t` as a positive
/// count of seconds from "now" until this item's next occurrence, floored at 1, with its own
/// comments describing empirically tuning that value to single-second accuracy against real
/// observed firing behaviour. These two static sources disagree on whether a positive `t` is
/// preserved (Localkit's model, implemented here) or discarded (this repo's disassembly) for the
/// ordinary case. Do not use this in a live send with a non-empty table until that conflict is
/// resolved and confirmed — a 0-entry table (no entries, hence no `time` field at all) is
/// unaffected and safe regardless.
pub fn wire_time_seconds_until(minute_of_day: u16, now_unix: u64) -> i32 {
    const DAY_SECS: i64 = 86_400;
    let now_second_of_day = (now_unix % DAY_SECS as u64) as i64;
    let target_second_of_day = minute_of_day as i64 * 60;
    let mut delta = target_second_of_day - now_second_of_day;
    if delta <= 0 {
        delta += DAY_SECS; // today's occurrence already passed (or is this instant) -> tomorrow
    }
    delta.clamp(1, i32::MAX as i64) as i32
}

/// The 4-byte RTC payload `ctrl` sends immediately before every schedule write (STUDY-schedule.md
/// §3.2: `bl 0x80b00(msg_id=0x6007, ...)`, payload "most plausibly a `time(NULL)`-shaped current
/// Unix time helper" — MEDIUM confidence, not independently disassembled, but low-risk: a wrong
/// RTC value miscalibrates the MCU's clock, it does not misfire a feed).
fn rtc_payload(now_unix: u64) -> [u8; 4] {
    (now_unix as u32).to_le_bytes()
}

/// One 22-byte wire entry, ready to encode. Field names and offsets are taken verbatim from
/// `ctrl`'s own embedded log format string (STUDY-schedule.md §3.3):
/// `"id = %16s,amount_l=%d,amount_r=%d,time=%d"`.
pub struct WireEntry {
    pub id: String,
    pub amount_l: u8,
    pub amount_r: u8,
    pub time: i32,
}

impl WireEntry {
    pub fn encode(&self) -> [u8; WIRE_ENTRY_LEN] {
        let mut b = [0u8; WIRE_ENTRY_LEN];
        let id = self.id.as_bytes();
        let n = id.len().min(15); // id[16], always NUL-terminated (mirrors FeedCtrl's id[64])
        b[0..n].copy_from_slice(&id[..n]);
        b[16] = self.amount_l;
        b[17] = self.amount_r;
        b[18..22].copy_from_slice(&self.time.to_le_bytes());
        b
    }

    pub fn decode(b: &[u8; WIRE_ENTRY_LEN]) -> Self {
        let end = b[0..16].iter().position(|&c| c == 0).unwrap_or(16);
        WireEntry {
            id: String::from_utf8_lossy(&b[0..end]).into_owned(),
            amount_l: b[16],
            amount_r: b[17],
            time: i32::from_le_bytes(b[18..22].try_into().unwrap()),
        }
    }
}

/// Encodes the `{count, reserved}` header plus every entry, in order. Rejects (rather than
/// silently truncating) past [`MAX_ENTRIES`], since the vendor's own 540-byte bus clamp
/// (`bus::MAX_PAYLOAD`) would otherwise cut a 25th entry off mid-struct.
pub fn encode_table(entries: &[WireEntry]) -> Result<Vec<u8>, String> {
    if entries.len() > MAX_ENTRIES {
        return Err(cap_error(entries.len()));
    }
    let mut buf = Vec::with_capacity(2 + entries.len() * WIRE_ENTRY_LEN);
    buf.push(entries.len() as u8); // count
    buf.push(0); // reserved
    for e in entries {
        buf.extend_from_slice(&e.encode());
    }
    Ok(buf)
}

fn cap_error(attempted: usize) -> String {
    format!(
        "schedule would have {attempted} entries; the wire format allows at most {MAX_ENTRIES} \
         (a {}-byte bus payload clamp would otherwise silently truncate the table mid-entry)",
        crate::bus::MAX_PAYLOAD
    )
}

/// One schedule entry as kibbled caches it. This -- not a device read -- is the source of truth;
/// see the module docs.
#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    pub id: String,
    /// Minutes since local midnight, `0..=1439`.
    pub minute_of_day: u16,
    pub amount_l: u8,
    pub amount_r: u8,
    /// A disabled entry stays in the cache but is omitted from the wire table.
    pub enabled: bool,
}

impl Entry {
    fn to_json(&self) -> String {
        format!(
            r#"{{"id":"{}","time":"{}","amount_l":{},"amount_r":{},"enabled":{}}}"#,
            self.id.escape_debug(),
            format_time_of_day(self.minute_of_day),
            self.amount_l,
            self.amount_r,
            self.enabled,
        )
    }

    /// Parses one `{"id":..,"time":"HH:MM","amount_l":N,"amount_r":N,"enabled":bool}` object.
    /// `id` and `enabled` are optional; a missing `id` gets a fresh generated one, a missing
    /// `enabled` defaults to `true`.
    fn parse_one(obj: &str) -> Result<Entry, String> {
        let time = json_field(obj, "time").ok_or("entry missing \"time\"")?;
        let minute_of_day = parse_time_of_day(time)
            .ok_or_else(|| format!("invalid \"time\" {time:?}, expected \"HH:MM\" 00:00..=23:59"))?;
        let amount_l = parse_amount(obj, "amount_l")?;
        let amount_r = parse_amount(obj, "amount_r")?;
        let enabled = json_field(obj, "enabled").map(|v| v != "false").unwrap_or(true);
        let id = json_field(obj, "id")
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| format!("sched-{}", now_unix()));
        Ok(Entry { id, minute_of_day, amount_l, amount_r, enabled })
    }
}

fn parse_amount(obj: &str, key: &str) -> Result<u8, String> {
    let v = json_field(obj, key).ok_or_else(|| format!("entry missing \"{key}\""))?;
    let n: i64 = v
        .parse()
        .map_err(|_| format!("\"{key}\" must be an integer 0..=50, got {v:?}"))?;
    if !(0..=50).contains(&n) {
        return Err(format!("\"{key}\" must be 0..=50, got {n}"));
    }
    Ok(n as u8)
}

/// Finds `"key": [ ... ]` and returns the raw text strictly between the brackets, respecting
/// nested braces/brackets/strings so a comma inside a value never splits early.
fn json_array_body<'a>(body: &'a str, key: &str) -> Option<&'a str> {
    let pat = format!("\"{key}\"");
    let after_key = &body[body.find(&pat)? + pat.len()..];
    let after_colon = after_key.trim_start().strip_prefix(':')?.trim_start();
    let inner = after_colon.strip_prefix('[')?;
    let mut depth = 1i32;
    let mut in_str = false;
    let mut escape = false;
    for (i, c) in inner.char_indices() {
        if escape {
            escape = false;
            continue;
        }
        match c {
            '\\' if in_str => escape = true,
            '"' => in_str = !in_str,
            '[' | '{' if !in_str => depth += 1,
            ']' | '}' if !in_str => {
                depth -= 1;
                if depth == 0 {
                    return Some(&inner[..i]);
                }
            }
            _ => {}
        }
    }
    None
}

/// Splits the inside of a JSON array (as returned by [`json_array_body`]) into its top-level
/// element substrings, respecting nested braces/brackets/strings.
fn split_top_level(inner: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut depth = 0i32;
    let mut in_str = false;
    let mut escape = false;
    let mut start = 0usize;
    for (i, c) in inner.char_indices() {
        if escape {
            escape = false;
            continue;
        }
        match c {
            '\\' if in_str => escape = true,
            '"' => in_str = !in_str,
            '{' | '[' if !in_str => depth += 1,
            '}' | ']' if !in_str => depth -= 1,
            ',' if !in_str && depth == 0 => {
                let piece = inner[start..i].trim();
                if !piece.is_empty() {
                    out.push(piece);
                }
                start = i + c.len_utf8();
            }
            _ => {}
        }
    }
    let last = inner[start..].trim();
    if !last.is_empty() {
        out.push(last);
    }
    out
}

/// Parses a top-level `{"entries":[...]}` body (shared by `PUT /schedule` and the on-disk cache).
pub fn parse_entries(body: &str) -> Result<Vec<Entry>, String> {
    let inner = json_array_body(body, "entries").ok_or("missing \"entries\" array")?;
    split_top_level(inner).into_iter().map(Entry::parse_one).collect()
}

/// Parses a single `{"time":"HH:MM","amount_l":N,"amount_r":N,"id":"...","enabled":bool}` body,
/// as `POST /schedule/entry` receives it directly (not wrapped in an `"entries"` array).
pub fn parse_entry(body: &str) -> Result<Entry, String> {
    Entry::parse_one(body)
}

/// Reads `id` off a `DELETE /schedule/entry?id=...` query string.
pub fn entry_id_from_query(query: &str) -> Option<&str> {
    query_field(query, "id").filter(|s| !s.is_empty())
}

/// The persisted cache: every entry kibbled knows about (enabled or not) plus when it last
/// changed.
#[derive(Debug, Clone, PartialEq)]
struct Cache {
    entries: Vec<Entry>,
    last_modified: u64,
}

impl Cache {
    fn empty() -> Self {
        Cache { entries: Vec::new(), last_modified: 0 }
    }

    fn load(path: &Path) -> io::Result<Self> {
        match fs::read_to_string(path) {
            Ok(s) => Self::parse(&s)
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "corrupt schedule cache")),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(Self::empty()),
            Err(e) => Err(e),
        }
    }

    /// Writes via a temp file + rename so a crash mid-write can never leave a half-written,
    /// corrupt cache behind -- the last-known-good file survives until the new one is complete.
    fn save(&self, path: &Path) -> io::Result<()> {
        let tmp = path.with_file_name(format!(
            "{}.tmp",
            path.file_name().and_then(|n| n.to_str()).unwrap_or("schedule.json")
        ));
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir)?;
        }
        fs::write(&tmp, self.to_json())?;
        fs::rename(&tmp, path)
    }

    fn to_json(&self) -> String {
        let entries: Vec<String> = self.entries.iter().map(Entry::to_json).collect();
        format!(
            r#"{{"entries":[{}],"last_modified":{}}}"#,
            entries.join(","),
            self.last_modified,
        )
    }

    fn parse(s: &str) -> Option<Self> {
        let entries = parse_entries(s).ok()?;
        let last_modified = json_field(s, "last_modified").and_then(|v| v.parse().ok()).unwrap_or(0);
        Some(Cache { entries, last_modified })
    }
}

/// Errors from a schedule mutation: `Invalid` is the caller's fault (bad input, unknown id, over
/// the cap) and maps to `400`; `Internal` is ours (cache I/O, bus send) and maps to `500`.
pub enum Error {
    Invalid(String),
    Internal(String),
}

/// Owns the cache and the bus handle used to push it. One instance per running agent.
pub struct Schedule {
    path: PathBuf,
    cache: Cache,
}

impl Schedule {
    pub fn load(path: PathBuf) -> io::Result<Self> {
        let cache = Cache::load(&path)?;
        Ok(Self { path, cache })
    }

    /// Body for `GET /schedule`. Always the cache -- see the module docs for why that is
    /// authoritative -- with an explicit flag so a client never mistakes it for a live device
    /// read.
    pub fn snapshot_json(&self) -> String {
        let entries: Vec<String> = self.cache.entries.iter().map(Entry::to_json).collect();
        format!(
            r#"{{"entries":[{}],"count":{},"last_modified":{},"source":"agent_cache","note":"the MCU has no schedule read-back (STUDY-schedule.md \u00a74); this is kibbled's own record of the last table it sent"}}"#,
            entries.join(","),
            self.cache.entries.len(),
            self.cache.last_modified,
        )
    }

    /// `PUT /schedule` — replaces the whole table.
    pub fn replace(&mut self, entries: Vec<Entry>, ble: &Sender, now: u64) -> Result<(), Error> {
        if entries.len() > MAX_ENTRIES {
            return Err(Error::Invalid(cap_error(entries.len())));
        }
        self.persist_then_push(Cache { entries, last_modified: now }, ble, now)
    }

    /// `POST /schedule/entry` — adds one entry, then resends the whole table.
    pub fn add(&mut self, entry: Entry, ble: &Sender, now: u64) -> Result<(), Error> {
        if self.cache.entries.iter().any(|e| e.id == entry.id) {
            return Err(Error::Invalid(format!("entry id {:?} already exists", entry.id)));
        }
        if self.cache.entries.len() >= MAX_ENTRIES {
            return Err(Error::Invalid(cap_error(self.cache.entries.len() + 1)));
        }
        let mut entries = self.cache.entries.clone();
        entries.push(entry);
        self.persist_then_push(Cache { entries, last_modified: now }, ble, now)
    }

    /// `DELETE /schedule/entry?id=` — removes one entry, then resends the whole table.
    pub fn remove(&mut self, id: &str, ble: &Sender, now: u64) -> Result<(), Error> {
        if !self.cache.entries.iter().any(|e| e.id == id) {
            return Err(Error::Invalid(format!("no schedule entry with id {id:?}")));
        }
        let entries: Vec<Entry> = self.cache.entries.iter().filter(|e| e.id != id).cloned().collect();
        self.persist_then_push(Cache { entries, last_modified: now }, ble, now)
    }

    /// `POST /schedule/entry/enabled` — flips one entry's enabled flag, then resends the whole
    /// table (a disabled entry is simply omitted from the wire array).
    pub fn set_enabled(&mut self, id: &str, enabled: bool, ble: &Sender, now: u64) -> Result<(), Error> {
        if !self.cache.entries.iter().any(|e| e.id == id) {
            return Err(Error::Invalid(format!("no schedule entry with id {id:?}")));
        }
        let mut entries = self.cache.entries.clone();
        for e in entries.iter_mut() {
            if e.id == id {
                e.enabled = enabled;
            }
        }
        self.persist_then_push(Cache { entries, last_modified: now }, ble, now)
    }

    /// Writes the cache first -- so a crash mid-send still leaves the intent recorded -- then
    /// pushes it to the device.
    fn persist_then_push(&mut self, cache: Cache, ble: &Sender, now: u64) -> Result<(), Error> {
        cache
            .save(&self.path)
            .map_err(|e| Error::Internal(format!("save schedule cache: {e}")))?;
        self.cache = cache;
        self.push(ble, now)
    }

    /// Re-sends the RTC then the full (enabled-only) wire table, mirroring `ctrl`
    /// (STUDY-schedule.md §3.2).
    fn push(&self, ble: &Sender, now: u64) -> Result<(), Error> {
        ble.send(msg::BLE_SET_RTC, &rtc_payload(now))
            .map_err(|e| Error::Internal(format!("RTC send failed: {e}")))?;
        let wire: Vec<WireEntry> = self
            .cache
            .entries
            .iter()
            .filter(|e| e.enabled)
            .map(|e| WireEntry {
                id: e.id.clone(),
                amount_l: e.amount_l,
                amount_r: e.amount_r,
                time: wire_time_seconds_until(e.minute_of_day, now),
            })
            .collect();
        let payload = encode_table(&wire).map_err(Error::Invalid)?;
        ble.send(msg::BLE_SET_SCHEDULE, &payload)
            .map_err(|e| Error::Internal(format!("schedule send failed: {e}")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- entry encode/decode round-trip -------------------------------------------------------

    #[test]
    fn wire_entry_round_trips() {
        let e = WireEntry { id: "morning".into(), amount_l: 12, amount_r: 8, time: 27_000 };
        let decoded = WireEntry::decode(&e.encode());
        assert_eq!(decoded.id, "morning");
        assert_eq!(decoded.amount_l, 12);
        assert_eq!(decoded.amount_r, 8);
        assert_eq!(decoded.time, 27_000);
    }

    #[test]
    fn wire_entry_round_trips_negative_time() {
        // Only a negative `time` survives verbatim on the real wire (STUDY-schedule.md §3.4) --
        // whatever the final encoding, the byte-packing itself must not clip or misread the sign.
        let e = WireEntry { id: "x".into(), amount_l: 1, amount_r: 1, time: -1 };
        assert_eq!(WireEntry::decode(&e.encode()).time, -1);
    }

    /// A 16-char id fills the entire `id[16]` field; the encoder must still leave it
    /// NUL-terminated rather than running the 16th character into `amount_l`.
    #[test]
    fn sixteen_char_id_stays_nul_terminated_and_does_not_reach_amount_l() {
        let id = "0123456789abcdef"; // exactly 16 chars
        assert_eq!(id.len(), 16);
        let e = WireEntry { id: id.into(), amount_l: 42, amount_r: 43, time: 1 };
        let bytes = e.encode();
        assert_eq!(&bytes[0..15], b"0123456789abcde");
        assert_eq!(bytes[15], 0, "byte 15 must be NUL, not the 16th source character");
        assert_eq!(bytes[16], 42, "amount_l must be untouched by the id field");
        assert_eq!(bytes[17], 43, "amount_r must be untouched by the id field");
        // Decoding must recover a clean (truncated, NUL-terminated) id, not run into amount_l.
        assert_eq!(WireEntry::decode(&bytes).id, "0123456789abcde");
    }

    #[test]
    fn oversized_id_is_truncated_and_terminated() {
        let e = WireEntry { id: "x".repeat(200), amount_l: 7, amount_r: 9, time: 5 };
        let bytes = e.encode();
        assert_eq!(bytes[14], b'x');
        assert_eq!(bytes[15], 0, "id field must stay NUL-terminated");
        assert_eq!(bytes[16], 7);
        assert_eq!(bytes[17], 9);
    }

    // --- header + cap --------------------------------------------------------------------------

    #[test]
    fn encode_table_header_is_count_then_reserved_zero() {
        let entries = vec![
            WireEntry { id: "a".into(), amount_l: 1, amount_r: 1, time: 1 },
            WireEntry { id: "b".into(), amount_l: 2, amount_r: 2, time: 2 },
        ];
        let buf = encode_table(&entries).unwrap();
        assert_eq!(buf.len(), 2 + 2 * WIRE_ENTRY_LEN);
        assert_eq!(buf[0], 2, "count");
        assert_eq!(buf[1], 0, "reserved");
    }

    #[test]
    fn empty_table_is_just_the_header() {
        let buf = encode_table(&[]).unwrap();
        assert_eq!(buf, vec![0, 0]);
    }

    #[test]
    fn twenty_four_entries_is_accepted() {
        let entries: Vec<WireEntry> = (0..24)
            .map(|i| WireEntry { id: format!("e{i}"), amount_l: 1, amount_r: 1, time: 1 })
            .collect();
        let buf = encode_table(&entries).expect("24 entries must fit");
        assert_eq!(buf[0], 24);
        assert_eq!(buf.len(), 2 + 24 * WIRE_ENTRY_LEN);
    }

    /// The 25th entry must be rejected outright, never silently dropped or truncated -- a
    /// truncated wire send is a corrupted table, not a smaller valid one.
    #[test]
    fn twenty_fifth_entry_is_rejected_not_truncated() {
        let entries: Vec<WireEntry> = (0..25)
            .map(|i| WireEntry { id: format!("e{i}"), amount_l: 1, amount_r: 1, time: 1 })
            .collect();
        let err = encode_table(&entries).expect_err("25 entries must be rejected");
        assert!(err.contains("25"), "error should mention the attempted count: {err}");
        assert!(err.contains("24"), "error should mention the cap: {err}");
    }

    #[test]
    fn max_entries_payload_fits_within_bus_max_payload() {
        // Ties MAX_ENTRIES to bus::MAX_PAYLOAD so the two constants can never silently drift
        // apart (that drift is exactly the "vendor clamp corrupts the table" hazard this cap
        // exists to prevent).
        assert!(2 + MAX_ENTRIES * WIRE_ENTRY_LEN <= crate::bus::MAX_PAYLOAD);
        assert!(2 + (MAX_ENTRIES + 1) * WIRE_ENTRY_LEN > crate::bus::MAX_PAYLOAD);
    }

    // --- time-of-day parsing: boundaries + rejection --------------------------------------------

    #[test]
    fn time_of_day_boundaries() {
        assert_eq!(parse_time_of_day("00:00"), Some(0));
        assert_eq!(parse_time_of_day("23:59"), Some(1439));
        assert_eq!(format_time_of_day(0), "00:00");
        assert_eq!(format_time_of_day(1439), "23:59");
    }

    #[test]
    fn time_of_day_rejects_out_of_range() {
        assert_eq!(parse_time_of_day("24:00"), None, "hour 24 does not exist");
        assert_eq!(parse_time_of_day("12:60"), None, "minute 60 does not exist");
        assert_eq!(parse_time_of_day("23:60"), None);
        assert_eq!(parse_time_of_day("99:99"), None);
    }

    #[test]
    fn time_of_day_rejects_malformed_input() {
        assert_eq!(parse_time_of_day(""), None);
        assert_eq!(parse_time_of_day("7:30"), None, "hour must be zero-padded HH");
        assert_eq!(parse_time_of_day("07:3"), None, "minute must be zero-padded MM");
        assert_eq!(parse_time_of_day("07-30"), None, "must be colon-separated");
        assert_eq!(parse_time_of_day("ab:cd"), None);
        assert_eq!(parse_time_of_day("12:30:00"), None);
    }

    #[test]
    fn wire_time_round_trips_to_the_same_time_of_day() {
        // Whatever the eventual encoding, format_time_of_day(minute_of_day) must be independent
        // of "now" -- only the wire value (tested separately, pending live confirmation) varies.
        for minute in [0u16, 1, 60, 719, 720, 1439] {
            assert_eq!(parse_time_of_day(&format_time_of_day(minute)), Some(minute));
        }
    }

    // --- amount validation -----------------------------------------------------------------------

    #[test]
    fn amount_accepts_boundaries_and_rejects_out_of_range() {
        let ok = r#"{"amount_l":0,"amount_r":50}"#;
        assert_eq!(parse_amount(ok, "amount_l"), Ok(0));
        assert_eq!(parse_amount(ok, "amount_r"), Ok(50));
        let bad = r#"{"amount_l":51}"#;
        assert!(parse_amount(bad, "amount_l").is_err());
        let neg = r#"{"amount_l":-1}"#;
        assert!(parse_amount(neg, "amount_l").is_err());
    }

    // --- entry JSON parsing ------------------------------------------------------------------

    #[test]
    fn parses_one_entry_with_defaults() {
        let e = Entry::parse_one(r#"{"time":"07:30","amount_l":10,"amount_r":5}"#).unwrap();
        assert_eq!(e.minute_of_day, 7 * 60 + 30);
        assert_eq!(e.amount_l, 10);
        assert_eq!(e.amount_r, 5);
        assert!(e.enabled, "enabled defaults to true");
        assert!(!e.id.is_empty(), "a missing id gets a generated one");
    }

    #[test]
    fn parses_one_entry_explicit_fields() {
        let e = Entry::parse_one(
            r#"{"id":"morning","time":"06:15","amount_l":1,"amount_r":2,"enabled":false}"#,
        )
        .unwrap();
        assert_eq!(e.id, "morning");
        assert_eq!(e.minute_of_day, 6 * 60 + 15);
        assert!(!e.enabled);
    }

    #[test]
    fn rejects_entry_missing_time() {
        assert!(Entry::parse_one(r#"{"amount_l":1,"amount_r":1}"#).is_err());
    }

    #[test]
    fn parses_entries_array() {
        let body = r#"{"entries":[{"time":"06:00","amount_l":1,"amount_r":1},{"time":"18:30","amount_l":2,"amount_r":2}]}"#;
        let entries = parse_entries(body).unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].minute_of_day, 6 * 60);
        assert_eq!(entries[1].minute_of_day, 18 * 60 + 30);
    }

    #[test]
    fn empty_entries_array_parses_to_no_entries() {
        assert_eq!(parse_entries(r#"{"entries":[]}"#).unwrap(), vec![]);
    }

    #[test]
    fn entry_round_trips_through_json() {
        let e = Entry {
            id: "a".into(),
            minute_of_day: 450,
            amount_l: 10,
            amount_r: 10,
            enabled: true,
        };
        let body = format!(r#"{{"entries":[{}]}}"#, e.to_json());
        let parsed = parse_entries(&body).unwrap();
        assert_eq!(parsed, vec![e]);
    }

    // --- cache persistence ------------------------------------------------------------------

    #[test]
    fn cache_round_trips_through_json() {
        let cache = Cache {
            entries: vec![Entry {
                id: "a".into(),
                minute_of_day: 90,
                amount_l: 3,
                amount_r: 4,
                enabled: false,
            }],
            last_modified: 1_700_000_000,
        };
        let parsed = Cache::parse(&cache.to_json()).unwrap();
        assert_eq!(parsed, cache);
    }

    #[test]
    fn missing_cache_file_loads_as_empty() {
        let path = std::env::temp_dir().join(format!("kibble-sched-test-missing-{}", std::process::id()));
        let _ = fs::remove_file(&path);
        let cache = Cache::load(&path).unwrap();
        assert_eq!(cache, Cache::empty());
    }

    #[test]
    fn save_then_load_round_trips_on_disk() {
        let path = std::env::temp_dir().join(format!("kibble-sched-test-{}.json", std::process::id()));
        let _ = fs::remove_file(&path);
        let cache = Cache {
            entries: vec![Entry {
                id: "disk".into(),
                minute_of_day: 5,
                amount_l: 1,
                amount_r: 1,
                enabled: true,
            }],
            last_modified: 42,
        };
        cache.save(&path).unwrap();
        assert_eq!(Cache::load(&path).unwrap(), cache);
        let _ = fs::remove_file(&path);
    }

    // --- query string ------------------------------------------------------------------------

    #[test]
    fn reads_id_from_query_string() {
        assert_eq!(entry_id_from_query("id=abc123"), Some("abc123"));
        assert_eq!(entry_id_from_query("foo=bar&id=xyz"), Some("xyz"));
        assert_eq!(entry_id_from_query(""), None);
        assert_eq!(entry_id_from_query("id="), None);
    }

    // --- wire time (candidate encoding; see doc comment on wire_time_seconds_until) -----------

    #[test]
    fn wire_time_counts_seconds_to_the_next_occurrence_today() {
        // now = 08:00:00 exactly, target 09:00 -> 3600s away.
        let now = 8 * 3600;
        assert_eq!(wire_time_seconds_until(9 * 60, now), 3600);
    }

    #[test]
    fn wire_time_rolls_to_tomorrow_once_today_has_passed() {
        // now = 08:00:00, target 07:00 (already passed today) -> 23h away, not negative.
        let now = 8 * 3600;
        assert_eq!(wire_time_seconds_until(7 * 60, now), 23 * 3600);
    }

    #[test]
    fn wire_time_is_floored_at_one_not_zero() {
        // now exactly on the target minute -> due right now, still must not encode as 0/negative.
        let now = 8 * 3600; // 08:00:00
        let v = wire_time_seconds_until(8 * 60, now);
        assert!(v >= 1, "must never be 0 or negative for a non-negative delta: got {v}");
    }
}
