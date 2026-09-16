//! The feed schedule.
//!
//! Persisted cache of every entry, its enabled state, and (via `scheduler.rs`) the duplicate-
//! fire bookkeeping that makes a restart safe -- see that module's doc for the actual firing
//! logic. This module owns: the HTTP-facing CRUD (`replace`/`add`/`remove`/`set_enabled`), the
//! on-disk format (`/opt/kibble/schedule.json`), and the vendor-facing wire encoding.
//!
//! ## Who actually fires a feed
//!
//! STUDY-schedule-encoding.md §6-§8 settled a question this module's own doc used to leave open:
//! the MCU is **not** an autonomous timer. `ctrl` zeroes any usable positive countdown before it
//! reaches the wire (§6), and `ctrl` itself re-fetches "what's next" after every feed (§8) --
//! textbook host-side-scheduler behaviour, not "delegate to the MCU and forget". So `kibbled`
//! (`scheduler.rs`) is the real scheduler: it owns the clock and calls the already-proven
//! `feed_ctrl` dispense path directly, at the appointed local time. The wire write this module
//! still does on every mutation (`push`, below) is retained as a **cosmetic bookkeeping sync**
//! only -- it keeps the device's own record consistent with what a real vendor write would look
//! like (`time` always `0`, matching confirmed real-device behaviour), in case the app is ever
//! used to read schedule status. It is not, and must never become, the trigger.
//!
//! Write semantics, mirrored from `ctrl` (STUDY-schedule.md §3.2): a write **replaces the whole
//! table** — there is no single-entry add/delete on the wire — and every write re-sends the RTC
//! (msg [`crate::bus::msg::BLE_SET_RTC`]) immediately before the schedule
//! (msg [`crate::bus::msg::BLE_SET_SCHEDULE`]). The cache is written to disk *before* either send,
//! so a crash mid-send still leaves the intent recorded rather than losing it.
//!
//! An entry disabled in the cache is simply omitted from the wire table: `enable` is not a wire
//! field (the 22-byte struct is fully accounted for without one), so "disabled" has no on-device
//! representation to send — STUDY-schedule.md's own struct recovery found no room for it. The
//! scheduler (`scheduler.rs`) independently skips a disabled entry too, so this stays consistent
//! either way.

use std::collections::HashMap;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::bus::{msg, Sender};
use crate::http::{json_field, query_field};
use crate::localtime::{self, Tz};

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

/// Finds `"key":` followed by `open` (`'['` or `'{'`) and returns the raw text strictly up to
/// its matching close, respecting nested braces/brackets/strings so a comma inside a value never
/// splits early. Shared by [`json_array_body`] (arrays) and [`json_object_body`] (objects).
fn balanced_body<'a>(body: &'a str, key: &str, open: char) -> Option<&'a str> {
    let pat = format!("\"{key}\"");
    let after_key = &body[body.find(&pat)? + pat.len()..];
    let after_colon = after_key.trim_start().strip_prefix(':')?.trim_start();
    let inner = after_colon.strip_prefix(open)?;
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

/// Finds `"key": [ ... ]` and returns the raw text strictly between the brackets.
fn json_array_body<'a>(body: &'a str, key: &str) -> Option<&'a str> {
    balanced_body(body, key, '[')
}

/// Finds `"key": { ... }` and returns the raw text strictly between the braces -- the
/// object-shaped counterpart of [`json_array_body`], used for `fired` (keyed by arbitrary
/// schedule entry ids, not a fixed field name).
fn json_object_body<'a>(body: &'a str, key: &str) -> Option<&'a str> {
    balanced_body(body, key, '{')
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

/// Splits an object's inner content (as returned by [`json_object_body`]) into `(key, raw
/// value)` pairs, for objects whose keys are arbitrary data (not fixed field names) -- e.g. the
/// `fired` map, keyed by schedule entry id. Reuses [`split_top_level`]'s nesting/string-aware
/// comma splitting, then peels the leading quoted key off each piece. Matches this file's (and
/// the rest of the agent's) accepted hand-rolled-JSON limitation of not unescaping backslash
/// sequences -- ids are trusted plain strings, exactly like every other id round-tripped through
/// this project's own JSON writers.
fn split_object_pairs(inner: &str) -> Vec<(&str, &str)> {
    split_top_level(inner)
        .into_iter()
        .filter_map(|pair| {
            let rest = pair.strip_prefix('"')?;
            let (key, after_quote) = rest.split_once('"')?;
            let value = after_quote.trim_start().strip_prefix(':')?.trim();
            Some((key, value))
        })
        .collect()
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

/// What happened, or didn't, the one time the scheduler (`scheduler.rs`) evaluated one entry's
/// one calendar-day occurrence. Persisted per entry in [`Cache::fired`] so a restart can never
/// re-evaluate (and so never re-fire, nor retry) an occurrence already resolved either way --
/// STUDY-schedule-encoding.md §11.1 items 3-4.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Dispensed,
    Missed,
}

impl Outcome {
    fn as_str(&self) -> &'static str {
        match self {
            Outcome::Dispensed => "dispensed",
            Outcome::Missed => "missed",
        }
    }

    fn parse(s: &str) -> Option<Outcome> {
        match s {
            "dispensed" => Some(Outcome::Dispensed),
            "missed" => Some(Outcome::Missed),
            _ => None,
        }
    }
}

/// One entry's most recently resolved occurrence (see [`Outcome`]). Only the latest is kept --
/// `scheduler.rs` never looks back more than one calendar day, so once an occurrence is resolved
/// it is never revisited and older history has no bearing on correctness.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FiredRecord {
    /// The local calendar date (`YYYY-MM-DD`) the resolved occurrence was due on.
    pub date: String,
    pub outcome: Outcome,
    /// Unix time the resolution was recorded -- for observability only, never re-derived.
    pub at_utc: u64,
}

/// The persisted cache: every entry kibbled knows about (enabled or not), when it last changed,
/// and (`fired`) the scheduler's own per-entry duplicate-fire tracking -- all three share one
/// file (`/opt/kibble/schedule.json`) and one lock ([`Schedule`]'s internal mutex) so an HTTP
/// mutation and a scheduler tick can never race each other into a lost update.
#[derive(Debug, Clone, PartialEq)]
struct Cache {
    entries: Vec<Entry>,
    last_modified: u64,
    fired: HashMap<String, FiredRecord>,
}

impl Cache {
    fn empty() -> Self {
        Cache { entries: Vec::new(), last_modified: 0, fired: HashMap::new() }
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
    /// `fsync`s the temp file's data *and* the containing directory before returning: `/opt` is
    /// flash (a UBIFS-on-UBI volume, `docs/design-agent.md`), and the boot script's respawn
    /// loop can relaunch a crashed `kibbled` within 5 seconds (`docs/design-agent.md`'s own
    /// `app_init.sh`) -- with nothing forcing this write past the page cache, a crash in that
    /// window could lose a `scheduler.rs::claim_fire` record that had already returned `Ok`,
    /// which is exactly the double-feed this project's whole design exists to prevent
    /// (STUDY-schedule-encoding.md §11.1 item 4: record before dispense is only a real guarantee
    /// if the record actually reaches durable storage before the dispense call fires).
    fn save(&self, path: &Path) -> io::Result<()> {
        let tmp = path.with_file_name(format!(
            "{}.tmp",
            path.file_name().and_then(|n| n.to_str()).unwrap_or("schedule.json")
        ));
        let dir = path.parent().map(Path::to_path_buf).unwrap_or_else(|| PathBuf::from("."));
        fs::create_dir_all(&dir)?;
        {
            let mut f = fs::File::create(&tmp)?;
            f.write_all(self.to_json().as_bytes())?;
            f.sync_all()?; // the data itself must be durable before the rename makes it visible
        }
        fs::rename(&tmp, path)?;
        // Best-effort: makes the *rename* durable too (so a crash right after this call can
        // never resurrect the pre-rename file), but the content itself is already safely on
        // flash via the `sync_all()` above regardless of whether this succeeds.
        if let Ok(dir_handle) = fs::File::open(&dir) {
            let _ = dir_handle.sync_all();
        }
        Ok(())
    }

    fn to_json(&self) -> String {
        let entries: Vec<String> = self.entries.iter().map(Entry::to_json).collect();
        let mut fired: Vec<(&String, &FiredRecord)> = self.fired.iter().collect();
        fired.sort_by(|a, b| a.0.cmp(b.0)); // stable file contents; HashMap iteration order isn't
        let fired_json: Vec<String> = fired
            .iter()
            .map(|(id, r)| {
                format!(
                    r#""{}":{{"date":"{}","outcome":"{}","at_utc":{}}}"#,
                    id.escape_debug(),
                    r.date.escape_debug(),
                    r.outcome.as_str(),
                    r.at_utc,
                )
            })
            .collect();
        format!(
            r#"{{"entries":[{}],"last_modified":{},"fired":{{{}}}}}"#,
            entries.join(","),
            self.last_modified,
            fired_json.join(","),
        )
    }

    fn parse(s: &str) -> Option<Self> {
        let entries = parse_entries(s).ok()?;
        let last_modified = json_field(s, "last_modified").and_then(|v| v.parse().ok()).unwrap_or(0);
        let fired = json_object_body(s, "fired").map(parse_fired_map).unwrap_or_default();
        Some(Cache { entries, last_modified, fired })
    }
}

/// Parses the inside of a `"fired": { ... }` object (see [`json_object_body`]) into entry-id ->
/// [`FiredRecord`] pairs. A pair that doesn't parse cleanly is dropped rather than failing the
/// whole cache load -- matches this file's existing "skip the piece, load has the rest" style.
fn parse_fired_map(inner: &str) -> HashMap<String, FiredRecord> {
    split_object_pairs(inner)
        .into_iter()
        .filter_map(|(id, value)| {
            let date = json_field(value, "date")?.to_string();
            let outcome = Outcome::parse(json_field(value, "outcome")?)?;
            let at_utc = json_field(value, "at_utc")?.parse().ok()?;
            Some((id.to_string(), FiredRecord { date, outcome, at_utc }))
        })
        .collect()
}

/// Errors from a schedule mutation: `Invalid` is the caller's fault (bad input, unknown id, over
/// the cap) and maps to `400`; `Internal` is ours (cache I/O, bus send) and maps to `500`.
pub enum Error {
    Invalid(String),
    Internal(String),
}

/// Owns the cache and the bus handle used to push it. One instance, shared (via `Arc`) between
/// the HTTP handler and `scheduler.rs`'s background tick thread -- the internal mutex is what
/// makes `claim_fire` the single atomic gate STUDY-schedule-encoding.md §11.1 items 3-4 need.
pub struct Schedule {
    path: PathBuf,
    cache: Mutex<Cache>,
}

impl Schedule {
    pub fn load(path: PathBuf) -> io::Result<Self> {
        let cache = Cache::load(&path)?;
        Ok(Self { path, cache: Mutex::new(cache) })
    }

    /// Test-only: seeds a schedule with `entries` and no bus dependency, for `scheduler.rs`'s
    /// tests, which need a `Schedule` to exist without ever touching the bus (matching this
    /// project's convention of not exercising real hardware from unit tests).
    #[cfg(test)]
    pub(crate) fn seed_for_test(path: PathBuf, entries: Vec<Entry>) -> Self {
        let cache = Cache { entries, last_modified: 0, fired: HashMap::new() };
        cache.save(&path).expect("seed_for_test: write schedule cache");
        Schedule { path, cache: Mutex::new(cache) }
    }

    /// Body for `GET /schedule`: the cache -- see the module docs for why that is authoritative
    /// -- plus, per entry, when it will next fire and what happened the last time the scheduler
    /// (`scheduler.rs`) resolved it.
    pub fn snapshot_json(&self, tz: &Tz, now_utc: i64, scheduler_enabled: bool) -> String {
        let guard = self.cache.lock().unwrap();
        let entries: Vec<String> = guard
            .entries
            .iter()
            .map(|e| entry_status_json(e, guard.fired.get(&e.id), tz, now_utc))
            .collect();
        format!(
            r#"{{"entries":[{}],"count":{},"last_modified":{},"scheduler_enabled":{},"source":"agent_cache","note":"the MCU has no schedule read-back (STUDY-schedule.md \u00a74); entries are kibbled's own record, and (STUDY-schedule-encoding.md \u00a711) kibbled itself -- not the MCU -- is what actually fires each one at its local time"}}"#,
            entries.join(","),
            guard.entries.len(),
            guard.last_modified,
            scheduler_enabled,
        )
    }

    /// Every entry, cloned out -- so a caller (the scheduler tick loop) can iterate without
    /// holding the lock across a subsequent bus call or dispense.
    pub fn entries_snapshot(&self) -> Vec<Entry> {
        self.cache.lock().unwrap().entries.clone()
    }

    /// The last occurrence resolved for `entry_id`, if any.
    pub fn fired_record(&self, entry_id: &str) -> Option<FiredRecord> {
        self.cache.lock().unwrap().fired.get(entry_id).cloned()
    }

    /// Atomically checks whether `entry_id`'s occurrence on `date` (or any *later* date) has
    /// already been resolved and, if not, durably records `outcome` for it before returning --
    /// see `scheduler.rs`'s module doc for why this exact ordering (record before dispense) is
    /// the safety-critical property here. Only the single most recent resolved date is kept per
    /// entry (`fired` is keyed by entry id alone), so this compares lexicographically against
    /// `date` (ISO `YYYY-MM-DD` sorts correctly as plain strings) rather than for exact equality
    /// -- `scheduler.rs` evaluates *both* yesterday and today's occurrence every tick, in that
    /// order, and an exact-equality check would let resolving today's occurrence "forget" that
    /// yesterday's was already independently resolved (and vice versa on the next tick),
    /// re-triggering it. Monotonic dates make "already resolved" mean "at or before the latest
    /// date this entry has ever resolved", which is exactly what "never look back" requires.
    /// `Ok(true)` means this call is the one that newly claimed the occurrence (the caller
    /// should act on `outcome` only in that case, and only once); `Ok(false)` means an earlier
    /// tick, an earlier process, or a racing thread already resolved this date or a later one.
    pub fn claim_fire(&self, entry_id: &str, date: &str, outcome: Outcome, at_utc: u64) -> io::Result<bool> {
        let mut guard = self.cache.lock().unwrap();
        if guard.fired.get(entry_id).is_some_and(|r| r.date.as_str() >= date) {
            return Ok(false);
        }
        let mut next = guard.clone();
        next.fired.insert(entry_id.to_string(), FiredRecord { date: date.to_string(), outcome, at_utc });
        next.save(&self.path)?;
        *guard = next;
        Ok(true)
    }

    /// `PUT /schedule` — replaces the whole table.
    pub fn replace(&self, entries: Vec<Entry>, ble: &Sender, now: u64) -> Result<(), Error> {
        if entries.len() > MAX_ENTRIES {
            return Err(Error::Invalid(cap_error(entries.len())));
        }
        self.persist_then_push(entries, ble, now)
    }

    /// `POST /schedule/entry` — adds one entry, then resends the whole table.
    pub fn add(&self, entry: Entry, ble: &Sender, now: u64) -> Result<(), Error> {
        let entries = {
            let guard = self.cache.lock().unwrap();
            if guard.entries.iter().any(|e| e.id == entry.id) {
                return Err(Error::Invalid(format!("entry id {:?} already exists", entry.id)));
            }
            if guard.entries.len() >= MAX_ENTRIES {
                return Err(Error::Invalid(cap_error(guard.entries.len() + 1)));
            }
            let mut entries = guard.entries.clone();
            entries.push(entry);
            entries
        };
        self.persist_then_push(entries, ble, now)
    }

    /// `DELETE /schedule/entry?id=` — removes one entry, then resends the whole table.
    pub fn remove(&self, id: &str, ble: &Sender, now: u64) -> Result<(), Error> {
        let entries = {
            let guard = self.cache.lock().unwrap();
            if !guard.entries.iter().any(|e| e.id == id) {
                return Err(Error::Invalid(format!("no schedule entry with id {id:?}")));
            }
            guard.entries.iter().filter(|e| e.id != id).cloned().collect()
        };
        self.persist_then_push(entries, ble, now)
    }

    /// `POST /schedule/entry/enabled` — flips one entry's enabled flag, then resends the whole
    /// table (a disabled entry is simply omitted from the wire array).
    pub fn set_enabled(&self, id: &str, enabled: bool, ble: &Sender, now: u64) -> Result<(), Error> {
        let entries = {
            let guard = self.cache.lock().unwrap();
            if !guard.entries.iter().any(|e| e.id == id) {
                return Err(Error::Invalid(format!("no schedule entry with id {id:?}")));
            }
            let mut entries = guard.entries.clone();
            for e in entries.iter_mut() {
                if e.id == id {
                    e.enabled = enabled;
                }
            }
            entries
        };
        self.persist_then_push(entries, ble, now)
    }

    /// Writes the cache first -- so a crash mid-send still leaves the intent recorded -- then
    /// pushes it to the device. Re-reads `fired` fresh under the same lock that commits
    /// `entries` (rather than accepting a pre-built `Cache`), so a concurrent `scheduler.rs`
    /// `claim_fire` landing between an entries-mutation's validation step and this call can never
    /// be lost.
    fn persist_then_push(&self, entries: Vec<Entry>, ble: &Sender, now: u64) -> Result<(), Error> {
        {
            let mut guard = self.cache.lock().unwrap();
            let cache = Cache { entries, last_modified: now, fired: guard.fired.clone() };
            cache.save(&self.path).map_err(|e| Error::Internal(format!("save schedule cache: {e}")))?;
            *guard = cache;
        }
        self.push(ble, now)
    }

    /// Re-sends the RTC then the full (enabled-only) wire table, mirroring `ctrl`
    /// (STUDY-schedule.md §3.2). The wire `time` field is always `0` now: STUDY-schedule-
    /// encoding.md §6/§11 confirms the real device zeroes any non-negative `time` before it ever
    /// reaches the MCU, so this write's only remaining purpose is cosmetic bookkeeping sync with
    /// vendor behaviour -- `scheduler.rs`, not this table, is what actually fires a feed.
    fn push(&self, ble: &Sender, now: u64) -> Result<(), Error> {
        ble.send(msg::BLE_SET_RTC, &rtc_payload(now))
            .map_err(|e| Error::Internal(format!("RTC send failed: {e}")))?;
        let entries = self.entries_snapshot();
        let wire: Vec<WireEntry> = entries
            .iter()
            .filter(|e| e.enabled)
            .map(|e| WireEntry { id: e.id.clone(), amount_l: e.amount_l, amount_r: e.amount_r, time: 0 })
            .collect();
        let payload = encode_table(&wire).map_err(Error::Invalid)?;
        ble.send(msg::BLE_SET_SCHEDULE, &payload)
            .map_err(|e| Error::Internal(format!("schedule send failed: {e}")))
    }
}

/// One entry's `GET /schedule` JSON, including scheduler-derived fields (`next_fire_utc`,
/// `last_fired_*`) -- kept separate from [`Entry::to_json`] (the on-disk persistence shape,
/// which has no business knowing about timezones or "now").
fn entry_status_json(e: &Entry, fired: Option<&FiredRecord>, tz: &Tz, now_utc: i64) -> String {
    let next_fire_json = if e.enabled {
        localtime::next_occurrence_utc(tz, e.minute_of_day, now_utc).to_string()
    } else {
        "null".to_string()
    };
    let (last_date_json, last_outcome_json) = match fired {
        Some(r) => (format!("\"{}\"", r.date.escape_debug()), format!("\"{}\"", r.outcome.as_str())),
        None => ("null".to_string(), "null".to_string()),
    };
    format!(
        r#"{{"id":"{}","time":"{}","amount_l":{},"amount_r":{},"enabled":{},"next_fire_utc":{next_fire_json},"last_fired_date":{last_date_json},"last_fired_outcome":{last_outcome_json}}}"#,
        e.id.escape_debug(),
        format_time_of_day(e.minute_of_day),
        e.amount_l,
        e.amount_r,
        e.enabled,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

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
    fn format_and_parse_time_of_day_round_trip() {
        // Pure HH:MM <-> minute-of-day conversion, independent of "now" or any timezone -- the
        // wire `time` field itself is now always 0 (STUDY-schedule-encoding.md §11); the real
        // local-time math lives in `scheduler.rs`/`localtime.rs`.
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
            fired: HashMap::new(),
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
            fired: HashMap::new(),
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

    // --- json_object_body / split_object_pairs ------------------------------------------------

    #[test]
    fn json_object_body_extracts_nested_object_text() {
        let body = r#"{"entries":[],"fired":{"a":{"date":"2026-09-16","outcome":"dispensed","at_utc":1},"b":{"date":"2026-09-15","outcome":"missed","at_utc":2}},"last_modified":0}"#;
        let inner = json_object_body(body, "fired").unwrap();
        assert_eq!(
            inner,
            r#""a":{"date":"2026-09-16","outcome":"dispensed","at_utc":1},"b":{"date":"2026-09-15","outcome":"missed","at_utc":2}"#
        );
    }

    #[test]
    fn json_object_body_missing_key_is_none() {
        assert_eq!(json_object_body(r#"{"entries":[]}"#, "fired"), None);
    }

    #[test]
    fn split_object_pairs_splits_arbitrary_keys() {
        let inner = r#""a":{"x":1},"b":{"y":2}"#;
        assert_eq!(split_object_pairs(inner), vec![("a", r#"{"x":1}"#), ("b", r#"{"y":2}"#)]);
    }

    #[test]
    fn split_object_pairs_on_empty_object_is_empty() {
        assert_eq!(split_object_pairs(""), Vec::<(&str, &str)>::new());
    }

    // --- Outcome / FiredRecord persistence ----------------------------------------------------

    #[test]
    fn outcome_round_trips_through_its_string_form() {
        assert_eq!(Outcome::parse(Outcome::Dispensed.as_str()), Some(Outcome::Dispensed));
        assert_eq!(Outcome::parse(Outcome::Missed.as_str()), Some(Outcome::Missed));
        assert_eq!(Outcome::parse("bogus"), None);
    }

    #[test]
    fn cache_with_fired_records_round_trips_through_json() {
        let mut fired = HashMap::new();
        fired.insert(
            "breakfast".to_string(),
            FiredRecord { date: "2026-09-16".into(), outcome: Outcome::Dispensed, at_utc: 1_789_594_000 },
        );
        fired.insert(
            "dinner".to_string(),
            FiredRecord { date: "2026-09-15".into(), outcome: Outcome::Missed, at_utc: 1_789_500_000 },
        );
        let cache = Cache { entries: Vec::new(), last_modified: 5, fired };
        let parsed = Cache::parse(&cache.to_json()).unwrap();
        assert_eq!(parsed, cache);
    }

    #[test]
    fn cache_with_no_fired_key_at_all_parses_as_empty() {
        // Forward compatibility with a cache file written before this field existed.
        let parsed = Cache::parse(r#"{"entries":[],"last_modified":0}"#).unwrap();
        assert_eq!(parsed.fired, HashMap::new());
    }

    // --- claim_fire: the atomic record-before-dispense gate ---------------------------------

    #[test]
    fn claim_fire_first_call_for_an_occurrence_claims_it() {
        let path = std::env::temp_dir().join(format!("kibble-sched-test-claim-{}.json", std::process::id()));
        let _ = fs::remove_file(&path);
        let schedule = Schedule::seed_for_test(path.clone(), Vec::new());
        let claimed = schedule.claim_fire("breakfast", "2026-09-16", Outcome::Dispensed, 1_789_594_000).unwrap();
        assert!(claimed, "the first call for a fresh occurrence must claim it");
        let record = schedule.fired_record("breakfast").unwrap();
        assert_eq!(record.date, "2026-09-16");
        assert_eq!(record.outcome, Outcome::Dispensed);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn claim_fire_is_durably_recorded_before_returning() {
        // Proves the record survives even if the process is dropped immediately after -- i.e.
        // the disk write, not just an in-memory flag, completes before `claim_fire` returns.
        let path = std::env::temp_dir().join(format!("kibble-sched-test-durable-{}.json", std::process::id()));
        let _ = fs::remove_file(&path);
        {
            let schedule = Schedule::seed_for_test(path.clone(), Vec::new());
            assert!(schedule.claim_fire("breakfast", "2026-09-16", Outcome::Dispensed, 1).unwrap());
        } // `schedule` dropped here -- nothing further flushes anything.
        let reloaded = Schedule::load(path.clone()).unwrap();
        let record = reloaded.fired_record("breakfast").unwrap();
        assert_eq!(record.date, "2026-09-16");
        assert_eq!(record.outcome, Outcome::Dispensed);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn claim_fire_same_occurrence_twice_only_claims_once() {
        let path = std::env::temp_dir().join(format!("kibble-sched-test-dup-{}.json", std::process::id()));
        let _ = fs::remove_file(&path);
        let schedule = Schedule::seed_for_test(path.clone(), Vec::new());
        assert!(schedule.claim_fire("breakfast", "2026-09-16", Outcome::Dispensed, 1).unwrap());
        // Same entry, same date -- a second tick, or a restart re-evaluating the same day, must
        // never re-claim it (STUDY-schedule-encoding.md §11.1 item 3).
        assert!(!schedule.claim_fire("breakfast", "2026-09-16", Outcome::Missed, 2).unwrap());
        // And the original outcome is untouched by the rejected second call.
        assert_eq!(schedule.fired_record("breakfast").unwrap().outcome, Outcome::Dispensed);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn claim_fire_a_later_date_for_the_same_entry_claims_fresh() {
        let path = std::env::temp_dir().join(format!("kibble-sched-test-nextday-{}.json", std::process::id()));
        let _ = fs::remove_file(&path);
        let schedule = Schedule::seed_for_test(path.clone(), Vec::new());
        assert!(schedule.claim_fire("breakfast", "2026-09-16", Outcome::Dispensed, 1).unwrap());
        assert!(schedule.claim_fire("breakfast", "2026-09-17", Outcome::Dispensed, 2).unwrap());
        assert_eq!(schedule.fired_record("breakfast").unwrap().date, "2026-09-17");
        let _ = fs::remove_file(&path);
    }

    /// Regression: `scheduler.rs`'s `tick()` resolves *both* yesterday's and today's occurrence
    /// every cycle, in that order, for the same entry id. An exact-date-equality check here
    /// once let resolving today "forget" that yesterday was already independently resolved
    /// (`fired` keeps only the one latest record per entry), letting a later tick re-claim --
    /// and re-dispense -- an occurrence already settled. The fix compares dates monotonically.
    #[test]
    fn claim_fire_does_not_allow_reclaiming_an_earlier_date_once_a_later_one_is_resolved() {
        let path = std::env::temp_dir().join(format!("kibble-sched-test-monotonic-{}.json", std::process::id()));
        let _ = fs::remove_file(&path);
        let schedule = Schedule::seed_for_test(path.clone(), Vec::new());
        assert!(schedule.claim_fire("breakfast", "2026-01-14", Outcome::Missed, 1).unwrap());
        assert!(schedule.claim_fire("breakfast", "2026-01-15", Outcome::Dispensed, 2).unwrap());
        // Re-checking the *earlier* date (exactly what a later tick's "yesterday" candidate
        // does) must not be treated as fresh just because the record has since moved on.
        assert!(!schedule.claim_fire("breakfast", "2026-01-14", Outcome::Dispensed, 3).unwrap());
        let record = schedule.fired_record("breakfast").unwrap();
        assert_eq!(record.date, "2026-01-15", "the later resolution must survive untouched");
        assert_eq!(record.outcome, Outcome::Dispensed);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn claim_fire_two_threads_racing_the_same_occurrence_only_one_wins() {
        let path = std::env::temp_dir().join(format!("kibble-sched-test-race-{}.json", std::process::id()));
        let _ = fs::remove_file(&path);
        let schedule = Arc::new(Schedule::seed_for_test(path.clone(), Vec::new()));
        let handles: Vec<_> = (0..8u64)
            .map(|i| {
                let schedule = Arc::clone(&schedule);
                std::thread::spawn(move || {
                    schedule.claim_fire("breakfast", "2026-09-16", Outcome::Dispensed, 1_000 + i).unwrap()
                })
            })
            .collect();
        let claimed_count = handles.into_iter().map(|h| h.join().unwrap()).filter(|&claimed| claimed).count();
        assert_eq!(claimed_count, 1, "exactly one of the racing threads must have claimed the occurrence");
        let _ = fs::remove_file(&path);
    }

    // --- entries_snapshot / snapshot_json -----------------------------------------------------

    #[test]
    fn entries_snapshot_reflects_current_entries() {
        let path = std::env::temp_dir().join(format!("kibble-sched-test-snapshot-{}.json", std::process::id()));
        let _ = fs::remove_file(&path);
        let entries = vec![Entry { id: "a".into(), minute_of_day: 90, amount_l: 1, amount_r: 1, enabled: true }];
        let schedule = Schedule::seed_for_test(path.clone(), entries.clone());
        assert_eq!(schedule.entries_snapshot(), entries);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn snapshot_json_includes_next_fire_and_scheduler_fields() {
        let path = std::env::temp_dir().join(format!("kibble-sched-test-snapjson-{}.json", std::process::id()));
        let _ = fs::remove_file(&path);
        let entries = vec![
            Entry { id: "morning".into(), minute_of_day: 7 * 60, amount_l: 1, amount_r: 1, enabled: true },
            Entry { id: "off".into(), minute_of_day: 8 * 60, amount_l: 1, amount_r: 1, enabled: false },
        ];
        let schedule = Schedule::seed_for_test(path.clone(), entries);
        let tz = localtime::DEVICE_TZ;
        let now = tz.local_to_utc(localtime::Civil { year: 2026, month: 1, day: 15 }, 6 * 3600);
        let json = schedule.snapshot_json(&tz, now, false);
        assert!(json.contains(r#""scheduler_enabled":false"#));
        assert!(json.contains(r#""id":"morning""#));
        assert!(json.contains("\"next_fire_utc\":"), "enabled entry must carry a next-fire value");
        assert!(json.contains(r#""id":"off""#));
        assert!(json.contains(r#""next_fire_utc":null"#), "a disabled entry never fires");
        let _ = fs::remove_file(&path);
    }
}
