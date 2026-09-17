//! Before/after dish snapshots around a feed cycle.
//!
//! The Petkit app shows a "dish before"/"dish after" picture per feed. Kibble already has
//! everything needed to do this without spending any CPU on JPEG encode: `ring.rs`'s poller keeps
//! the sub channel's most recent H.264 keyframe (SPS+PPS+IDR access unit) cached in memory for
//! RTSP (`VideoFeed::latest_keyframe`), and `state.rs` already exposes the transient
//! "a feed cycle is running" flag (`config_shm` offset [`state::off::FEEDING`]). Capturing a
//! before/after pair is: grab the cached keyframe, dispense, watch the flag go `1` then back to
//! `0`, settle briefly, grab the cached keyframe again.
//!
//! This does **not** decode the H.264 into a JPEG on-device -- the whole point of using the
//! cached keyframe is that it costs nothing beyond a `Vec` clone. `GET /feeds/<name>` serves the
//! raw Annex-B bytes; Home Assistant (which already ships ffmpeg) decodes them to a displayable
//! image.
//!
//! ## Manual vs. scheduled feeds
//!
//! A manual `POST /feed` calls [`FeedCapture::note_manual_feed`] right after the bus send
//! succeeds, recording the id/amounts about to take effect. `scheduler.rs`'s `BusDispenser` does
//! the analogous thing for a feed it fires itself, via [`FeedCapture::note_scheduled_feed`] --
//! same timing (called immediately before its own bus send), but it only ever supplies amounts,
//! never an id or `manual: true` (see [`ScheduledNote`]'s own doc). The single background
//! watcher thread (spawned once, [`spawn`]) polls the feeding flag continuously; on every
//! `0` -> `1` transition it claims whatever manual-feed note is still pending (within
//! [`MANUAL_FEED_WINDOW`] of being recorded) as this cycle's metadata, else falls back to a
//! synthesised `scheduled-<ts>-<n>` id, claiming a fresh [`ScheduledNote`]'s amounts if
//! `scheduler.rs` left one, or leaving them `None` if nothing did -- a feed dispensed some other
//! way entirely (the vendor's own app, a stale/already-claimed note) genuinely has unknown
//! amounts, never guessed.
//!
//! ## Testing without dispensing
//!
//! [`FeedCapture::finish_cycle`] (grab the "after" frame, persist both frames, update the
//! in-memory record list, evict old ones) is exercised directly in this module's tests with
//! synthetic frame bytes and a fake id -- never by triggering a real dispense. The watcher loop
//! itself (which polls the *real* `config_shm` flag) is not unit tested for the same reason
//! `cloud.rs`'s live route mutations aren't: it needs the real device. See `docs/24-onboard-ai.md`
//! for how it was verified live (gated on the operator's go-ahead, not run automatically here).

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::ring::VideoFeed;
use crate::state::{off, Shm};

pub const FEEDS_DIR: &str = "/opt/kibble/feeds";
/// "cap at the last 20 feeds" -- 20 *events*, each up to a `.json` + two `.h264` files.
pub const MAX_FEEDS: usize = 20;
/// How long after the bus send that causes a feed -- a manual `POST /feed`'s own send, or
/// `scheduler.rs`'s `BusDispenser` firing a scheduled one -- a pending note ([`ManualNote`] or
/// [`ScheduledNote`]) stays claimable by the watcher thread's next observed flag transition. Feed
/// latency was measured at 24 ms (`docs/design-agent.md`); this is generous headroom, not a
/// tuned value.
const MANUAL_FEED_WINDOW: Duration = Duration::from_secs(15);
/// How often the watcher polls the feeding flag while idle, and while waiting for it to clear.
const POLL_INTERVAL: Duration = Duration::from_millis(200);
/// Settle time after the flag clears before grabbing the "after" frame -- bowl contents and the
/// camera's own auto-exposure both need a moment, per the assignment.
const SETTLE_AFTER_FEED: Duration = Duration::from_secs(3);
/// Give up waiting for the flag to ever go high after a manual send (something else failed) or
/// to ever clear again (should not happen) rather than watch forever.
const MAX_CYCLE_WAIT: Duration = Duration::from_secs(30);

fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

struct ManualNote {
    id: String,
    amount1: u8,
    amount2: u8,
    noted_at: Instant,
}

/// Amounts a `scheduler.rs` fire is *about* to cause, left just before the bus send so the next
/// observed 0->1 flag transition can pick them up -- same "leave a note, the watcher claims it"
/// pattern as [`ManualNote`], but only ever supplies the amounts: unlike a manual note it never
/// overrides the cycle's id (which stays the auto-generated `scheduled-<ts>-<n>` -- see
/// [`FeedCapture::start_cycle`]'s doc) or flips `manual` to `true` (a scheduler fire is not
/// user-initiated the way a `POST /feed` is).
struct ScheduledNote {
    amount1: u8,
    amount2: u8,
    noted_at: Instant,
}

/// One feed cycle's on-disk record, as reported by `GET /feeds`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FeedRecord {
    pub ts: u64,
    pub id: String,
    pub amount1: Option<u8>,
    pub amount2: Option<u8>,
    pub manual: bool,
    pub before: Option<String>,
    pub after: Option<String>,
}

impl FeedRecord {
    pub fn to_json(&self) -> String {
        fn opt_u8(v: Option<u8>) -> String {
            v.map_or("null".into(), |n| n.to_string())
        }
        fn opt_str(v: &Option<String>) -> String {
            v.as_ref().map_or("null".into(), |s| format!("\"{}\"", s.escape_debug()))
        }
        format!(
            r#"{{"ts":{},"id":"{}","amount1":{},"amount2":{},"manual":{},"before":{},"after":{}}}"#,
            self.ts,
            self.id.escape_debug(),
            opt_u8(self.amount1),
            opt_u8(self.amount2),
            self.manual,
            opt_str(&self.before),
            opt_str(&self.after),
        )
    }
}

pub struct FeedCapture {
    dir: PathBuf,
    sub_feed: Arc<VideoFeed>,
    pending: Mutex<Option<ManualNote>>,
    /// The scheduled-feed counterpart of `pending` -- see [`ScheduledNote`]'s own doc.
    pending_scheduled: Mutex<Option<ScheduledNote>>,
    /// Disambiguates two feeds landing in the same wall-clock second.
    seq: AtomicU64,
}

impl FeedCapture {
    pub fn new(sub_feed: Arc<VideoFeed>) -> Arc<FeedCapture> {
        Self::new_in(FEEDS_DIR, sub_feed)
    }

    fn new_in(dir: impl Into<PathBuf>, sub_feed: Arc<VideoFeed>) -> Arc<FeedCapture> {
        Arc::new(FeedCapture {
            dir: dir.into(),
            sub_feed,
            pending: Mutex::new(None),
            pending_scheduled: Mutex::new(None),
            seq: AtomicU64::new(0),
        })
    }

    /// Called by `POST /feed`'s handler right after the bus send succeeds. Not itself the
    /// capture -- just leaves a note the watcher thread can pick up on the flag transition this
    /// send is about to cause.
    pub fn note_manual_feed(&self, id: String, amount1: u8, amount2: u8) {
        *self.pending.lock().unwrap() = Some(ManualNote { id, amount1, amount2, noted_at: Instant::now() });
    }

    fn take_fresh_manual_note(&self) -> Option<(String, u8, u8)> {
        let mut guard = self.pending.lock().unwrap();
        match guard.take() {
            Some(note) if note.noted_at.elapsed() <= MANUAL_FEED_WINDOW => {
                Some((note.id, note.amount1, note.amount2))
            }
            _ => None,
        }
    }

    /// Called by `scheduler.rs`'s [`crate::scheduler::BusDispenser`] right before the bus send
    /// for a scheduler-fired feed -- the scheduled-feed counterpart of [`Self::note_manual_feed`].
    /// See [`ScheduledNote`]'s own doc for why this only ever supplies amounts, never an id or
    /// `manual: true`.
    pub fn note_scheduled_feed(&self, amount1: u8, amount2: u8) {
        *self.pending_scheduled.lock().unwrap() = Some(ScheduledNote { amount1, amount2, noted_at: Instant::now() });
    }

    fn take_fresh_scheduled_amounts(&self) -> Option<(u8, u8)> {
        let mut guard = self.pending_scheduled.lock().unwrap();
        match guard.take() {
            Some(note) if note.noted_at.elapsed() <= MANUAL_FEED_WINDOW => Some((note.amount1, note.amount2)),
            _ => None,
        }
    }

    fn keyframe_bytes(&self) -> Option<Vec<u8>> {
        self.sub_feed.latest_keyframe().map(|f| f.data)
    }

    /// Grab the "before" frame and record which feed (manual note, if a fresh one is pending, or
    /// a synthesised spontaneous id) this cycle belongs to. Called by the watcher the instant it
    /// observes the flag go high. A spontaneous cycle's amounts come from a fresh
    /// [`ScheduledNote`] when `scheduler.rs` left one via [`Self::note_scheduled_feed`], else stay
    /// `None` -- a feed dispensed some other way entirely (the vendor's own app, a stale or
    /// already-claimed note) genuinely has unknown amounts, never guessed.
    fn start_cycle(&self) -> (String, Option<u8>, Option<u8>, bool, Option<Vec<u8>>) {
        let before = self.keyframe_bytes();
        match self.take_fresh_manual_note() {
            Some((id, a1, a2)) => (id, Some(a1), Some(a2), true, before),
            None => {
                let seq = self.seq.fetch_add(1, Ordering::Relaxed);
                let id = format!("scheduled-{}-{seq}", now_unix());
                let (amount1, amount2) = match self.take_fresh_scheduled_amounts() {
                    Some((a1, a2)) => (Some(a1), Some(a2)),
                    None => (None, None),
                };
                (id, amount1, amount2, false, before)
            }
        }
    }

    /// Grab the "after" frame, persist both, append the record, evict over the cap. Pure enough
    /// (given already-captured bytes) to unit test without a device -- see the module doc.
    fn finish_cycle(
        &self,
        id: &str,
        amount1: Option<u8>,
        amount2: Option<u8>,
        manual: bool,
        before: Option<Vec<u8>>,
    ) -> io::Result<FeedRecord> {
        let after = self.keyframe_bytes();
        let ts = now_unix();
        let record = save_pair(&self.dir, ts, id, amount1, amount2, manual, before.as_deref(), after.as_deref())?;
        evict_oldest_if_over_cap(&self.dir, MAX_FEEDS)?;
        Ok(record)
    }

    /// `GET /feeds`: every still-on-disk record, oldest first.
    pub fn list_json(&self) -> io::Result<String> {
        let records = list_records(&self.dir)?;
        let items: Vec<String> = records.iter().map(FeedRecord::to_json).collect();
        Ok(format!("[{}]", items.join(",")))
    }

    /// `GET /feeds/<name>`: the raw Annex-B bytes of one `-before.h264`/`-after.h264` file.
    pub fn read_file(&self, name: &str) -> io::Result<Vec<u8>> {
        if !is_safe_feed_file_name(name) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid file name"));
        }
        fs::read(self.dir.join(name))
    }
}

fn is_safe_feed_file_name(name: &str) -> bool {
    !name.is_empty()
        && !name.contains('/')
        && !name.contains('\\')
        && name != "."
        && name != ".."
        && (name.ends_with("-before.h264") || name.ends_with("-after.h264"))
}

/// Writes `<dir>/<ts>-<id>-before.h264`, `...-after.h264` (whichever frames are `Some`) and a
/// `<ts>-<id>.json` sidecar carrying `amount1`/`amount2`/`manual` -- the two things `GET /feeds`
/// needs that don't fit in the filename convention the assignment specifies. A feed id can
/// contain almost anything (it's a free-form string on the wire), so metadata rides in its own
/// file rather than more filename fields that would need unambiguous re-parsing.
fn save_pair(
    dir: &Path,
    ts: u64,
    id: &str,
    amount1: Option<u8>,
    amount2: Option<u8>,
    manual: bool,
    before: Option<&[u8]>,
    after: Option<&[u8]>,
) -> io::Result<FeedRecord> {
    fs::create_dir_all(dir)?;
    let safe_id = sanitize_id_for_filename(id);
    let stem = format!("{ts}-{safe_id}");
    let before_name = match before {
        Some(bytes) => {
            let name = format!("{stem}-before.h264");
            fs::write(dir.join(&name), bytes)?;
            Some(name)
        }
        None => None,
    };
    let after_name = match after {
        Some(bytes) => {
            let name = format!("{stem}-after.h264");
            fs::write(dir.join(&name), bytes)?;
            Some(name)
        }
        None => None,
    };
    let meta = format!(
        r#"{{"ts":{ts},"id":"{}","amount1":{},"amount2":{},"manual":{manual}}}"#,
        id.escape_debug(),
        amount1.map_or("null".to_string(), |v| v.to_string()),
        amount2.map_or("null".to_string(), |v| v.to_string()),
    );
    fs::write(dir.join(format!("{stem}.json")), meta)?;
    Ok(FeedRecord { ts, id: id.to_string(), amount1, amount2, manual, before: before_name, after: after_name })
}

/// A feed id is a free-form wire string; strip path separators so it can't escape `dir` when
/// used inside a filename we construct and later join back onto `dir`.
fn sanitize_id_for_filename(id: &str) -> String {
    let cleaned: String = id.chars().map(|c| if c == '/' || c == '\\' { '_' } else { c }).collect();
    if cleaned.is_empty() {
        "feed".to_string()
    } else {
        cleaned
    }
}

/// Reconstructs every `<ts>-<id>.json` sidecar into a [`FeedRecord`], oldest first. A record
/// whose sidecar is missing or unreadable (shouldn't happen -- `save_pair` writes it last) is
/// skipped rather than failing the whole listing.
fn list_records(dir: &Path) -> io::Result<Vec<FeedRecord>> {
    let read = match fs::read_dir(dir) {
        Ok(r) => r,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    let mut records: Vec<FeedRecord> = Vec::new();
    for entry in read.filter_map(|e| e.ok()) {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else { continue };
        let Ok(text) = fs::read_to_string(&path) else { continue };
        let Some(record) = parse_record(&text, stem, dir) else { continue };
        records.push(record);
    }
    records.sort_by_key(|r| r.ts);
    Ok(records)
}

fn parse_record(json: &str, stem: &str, dir: &Path) -> Option<FeedRecord> {
    use crate::http::json_field;
    let ts: u64 = json_field(json, "ts")?.parse().ok()?;
    let id = json_field(json, "id")?.to_string();
    let amount1 = json_field(json, "amount1").and_then(|v| v.parse().ok());
    let amount2 = json_field(json, "amount2").and_then(|v| v.parse().ok());
    let manual = json_field(json, "manual").map(|v| v != "false").unwrap_or(false);
    let before_name = format!("{stem}-before.h264");
    let after_name = format!("{stem}-after.h264");
    let before = dir.join(&before_name).is_file().then_some(before_name);
    let after = dir.join(&after_name).is_file().then_some(after_name);
    Some(FeedRecord { ts, id, amount1, amount2, manual, before, after })
}

/// Deletes every file belonging to the oldest events once more than `cap` events exist, grouped
/// by their common `<ts>-<id>` stem (`.json` + both `.h264` siblings evicted together).
fn evict_oldest_if_over_cap(dir: &Path, cap: usize) -> io::Result<()> {
    let mut records = list_records(dir)?;
    if records.len() <= cap {
        return Ok(());
    }
    records.sort_by_key(|r| r.ts);
    for r in records.iter().take(records.len() - cap) {
        let stem = format!("{}-{}", r.ts, sanitize_id_for_filename(&r.id));
        for suffix in ["-before.h264", "-after.h264", ".json"] {
            let _ = fs::remove_file(dir.join(format!("{stem}{suffix}")));
        }
    }
    Ok(())
}

/// One iteration of the watcher: block until the flag rises, capture "before", block until it
/// falls, settle, capture "after", persist. Split out from [`spawn`]'s loop so it's callable
/// (with a real `Shm`) independent of the infinite loop, but still not part of the unit-tested
/// surface -- it always needs the real feeding flag.
fn run_one_cycle(shm: &Shm, capture: &FeedCapture) -> bool {
    if !wait_for_flag(shm, true, None) {
        return false;
    }
    // This watcher sees the flag at 200 ms; the 1 s state diff in ai.rs would also catch it,
    // but a dispense is the one thing worth telling HA about as fast as we know.
    crate::push::mark(crate::push::Field::State);
    let (id, amount1, amount2, manual, before) = capture.start_cycle();
    if !wait_for_flag(shm, false, Some(MAX_CYCLE_WAIT)) {
        eprintln!("kibbled: feed capture: flag for {id} never cleared, dropping this cycle");
        return true;
    }
    crate::push::mark(crate::push::Field::State);
    thread::sleep(SETTLE_AFTER_FEED);
    match capture.finish_cycle(&id, amount1, amount2, manual, before) {
        Ok(r) => {
            eprintln!(
                "kibbled: feed capture: saved {id} (manual={manual}, before={:?}, after={:?})",
                r.before, r.after
            );
            crate::push::mark(crate::push::Field::Feeds);
        }
        Err(e) => eprintln!("kibbled: feed capture: failed to save {id}: {e}"),
    }
    true
}

/// Poll until `off::FEEDING` reads `want` (as a bool), or `timeout` elapses (`None` = poll
/// forever -- used for the initial "wait for a feed to start" wait, which has no natural bound).
fn wait_for_flag(shm: &Shm, want: bool, timeout: Option<Duration>) -> bool {
    let start = Instant::now();
    loop {
        if (shm.u8(off::FEEDING) != 0) == want {
            return true;
        }
        if let Some(t) = timeout {
            if start.elapsed() >= t {
                return false;
            }
        }
        thread::sleep(POLL_INTERVAL);
    }
}

/// Start the persistent watcher thread and return the shared handle `main.rs` routes
/// `POST /feed`'s success path and `GET /feeds*` against.
pub fn spawn(shm: Arc<Shm>, sub_feed: Arc<VideoFeed>) -> Arc<FeedCapture> {
    let capture = FeedCapture::new(sub_feed);
    let handle = Arc::clone(&capture);
    thread::spawn(move || loop {
        run_one_cycle(&shm, &handle);
    });
    capture
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering as AtoOrdering};

    static COUNTER: AtomicU32 = AtomicU32::new(0);

    fn temp_dir(tag: &str) -> PathBuf {
        let n = COUNTER.fetch_add(1, AtoOrdering::Relaxed);
        let dir = std::env::temp_dir().join(format!("kibble-feedcap-test-{tag}-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    /// The exact scenario the assignment asks to verify without dispensing: call the capture
    /// function directly with a fake id and synthetic frame bytes.
    #[test]
    fn save_pair_writes_both_frames_and_a_readable_sidecar_for_a_fake_feed() {
        let dir = temp_dir("save");
        let record = save_pair(&dir, 1_700_000_000, "fake-feed-1", Some(3), Some(4), true, Some(b"BEFORE"), Some(b"AFTER"))
            .unwrap();
        assert_eq!(record.before.as_deref(), Some("1700000000-fake-feed-1-before.h264"));
        assert_eq!(record.after.as_deref(), Some("1700000000-fake-feed-1-after.h264"));
        assert_eq!(fs::read(dir.join(record.before.as_ref().unwrap())).unwrap(), b"BEFORE");
        assert_eq!(fs::read(dir.join(record.after.as_ref().unwrap())).unwrap(), b"AFTER");

        let listed = list_records(&dir).unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0], record);
    }

    #[test]
    fn save_pair_tolerates_a_missing_before_frame() {
        let dir = temp_dir("missing-before");
        let record = save_pair(&dir, 1, "x", None, None, false, None, Some(b"AFTER")).unwrap();
        assert_eq!(record.before, None);
        assert_eq!(record.after.as_deref(), Some("1-x-after.h264"));
    }

    #[test]
    fn feed_id_containing_slashes_is_sanitised_before_touching_the_filesystem() {
        let dir = temp_dir("slash-id");
        let record = save_pair(&dir, 1, "../../etc/passwd", None, None, false, Some(b"B"), Some(b"A")).unwrap();
        // the record still reports the caller's original id verbatim...
        assert_eq!(record.id, "../../etc/passwd");
        // ...but every file it actually wrote landed inside `dir`, nowhere else.
        let before_path = dir.join(record.before.as_ref().unwrap());
        assert!(before_path.starts_with(&dir));
        assert!(before_path.is_file());
    }

    #[test]
    fn eviction_keeps_the_newest_cap_events_and_removes_every_file_of_the_rest() {
        let dir = temp_dir("evict");
        for i in 0..5u64 {
            save_pair(&dir, 1_000 + i, &format!("f{i}"), Some(1), Some(1), true, Some(b"B"), Some(b"A")).unwrap();
        }
        evict_oldest_if_over_cap(&dir, 3).unwrap();
        let remaining = list_records(&dir).unwrap();
        assert_eq!(remaining.len(), 3);
        assert_eq!(remaining.iter().map(|r| r.id.clone()).collect::<Vec<_>>(), vec!["f2", "f3", "f4"]);
        // the evicted events' h264 files are gone too, not just their sidecars
        assert!(!dir.join("1000-f0-before.h264").exists());
        assert!(!dir.join("1000-f0-after.h264").exists());
        assert!(!dir.join("1000-f0.json").exists());
    }

    #[test]
    fn eviction_is_a_no_op_at_or_under_the_cap() {
        let dir = temp_dir("nocap");
        save_pair(&dir, 1, "only", None, None, false, Some(b"B"), Some(b"A")).unwrap();
        evict_oldest_if_over_cap(&dir, 20).unwrap();
        assert_eq!(list_records(&dir).unwrap().len(), 1);
    }

    #[test]
    fn finish_cycle_never_exceeds_max_feeds_across_many_manual_notes() {
        let dir = temp_dir("cap-loop");
        let sub_feed = VideoFeed::new();
        let capture = FeedCapture::new_in(&dir, sub_feed);
        for i in 0..(MAX_FEEDS + 7) {
            capture.note_manual_feed(format!("id-{i}"), 1, 2);
            let (id, a1, a2, manual, before) = capture.start_cycle();
            assert_eq!(id, format!("id-{i}"), "a fresh note must be claimed by the very next cycle");
            assert!(manual);
            capture.finish_cycle(&id, a1, a2, manual, before).unwrap();
        }
        assert_eq!(list_records(&dir).unwrap().len(), MAX_FEEDS);
    }

    #[test]
    fn start_cycle_falls_back_to_a_spontaneous_id_with_no_pending_manual_note() {
        let dir = temp_dir("spontaneous");
        let sub_feed = VideoFeed::new();
        let capture = FeedCapture::new_in(&dir, sub_feed);
        let (id, amount1, amount2, manual, _before) = capture.start_cycle();
        assert!(id.starts_with("scheduled-"), "got {id:?}");
        assert_eq!(amount1, None);
        assert_eq!(amount2, None);
        assert!(!manual);
    }

    #[test]
    fn stale_manual_notes_are_not_claimed_by_a_much_later_cycle() {
        let dir = temp_dir("stale");
        let sub_feed = VideoFeed::new();
        let capture = FeedCapture::new_in(&dir, sub_feed);
        capture.note_manual_feed("late".into(), 5, 5);
        // Simulate staleness directly rather than sleeping MANUAL_FEED_WINDOW in a test.
        capture.pending.lock().unwrap().as_mut().unwrap().noted_at =
            Instant::now() - MANUAL_FEED_WINDOW - Duration::from_secs(1);
        let (id, _, _, manual, _) = capture.start_cycle();
        assert!(!manual);
        assert!(id.starts_with("scheduled-"), "got {id:?}");
    }

    #[test]
    fn read_file_rejects_names_outside_the_before_after_convention() {
        let dir = temp_dir("read-guard");
        let sub_feed = VideoFeed::new();
        let capture = FeedCapture::new_in(&dir, sub_feed);
        assert!(capture.read_file("../../etc/passwd").is_err());
        assert!(capture.read_file("1-x.json").is_err(), "sidecar metadata is not a served file");
    }

    #[test]
    fn list_json_on_a_fresh_directory_is_an_empty_array() {
        let dir = temp_dir("empty");
        let sub_feed = VideoFeed::new();
        let capture = FeedCapture::new_in(&dir, sub_feed);
        assert_eq!(capture.list_json().unwrap(), "[]");
    }
}
