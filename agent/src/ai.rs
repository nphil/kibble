//! Onboard AI: where the vendor's detection results actually go, and the event feed built from
//! what is genuinely observable without disrupting `ctrl`.
//!
//! ## The mechanism, with evidence (read this before changing anything below)
//!
//! `media` (dlopen'd `libalgo.so`, `docs/12-ai.md`) does **not** publish its detection results
//! through any shared-memory segment. Live, read-only checks this session ruled that out
//! directly: `/proc/<media pid>/maps` shows only the already-known `media_buffer_frame_buf`
//! (video ring) and `/dev/shm/config_shm`, plus exactly one small (4 KiB mapping, 16-byte live
//! file) anonymous `shm_open`+immediately-`shm_unlink`'d segment (`/dev/shm/gQDyHN (deleted)`,
//! readable read-only via `/proc/<pid>/map_files/<range>`) — decoded and it is a tiny
//! synchronization primitive (16 bytes, a counter/flag pair), not a result buffer. `ctrl`'s own
//! maps show only `config_shm`. So the only channel left is the bus, and that is what static
//! disassembly of the pulled `ctrl`/`media` binaries (md5-verified byte-identical to the live
//! device, `docs/24-onboard-ai.md`) confirms:
//!
//! - `ctrl`'s entire inbound (`/msg_dispatch_1`) handler table was recovered by symbolically
//!   executing its registration function (a sequence of GOT-indirected
//!   `dispatch_send_msg`-style `register(msg_id, handler_fn)` calls at the top of `.text`) and
//!   cross-checked against three msg_ids already proven in earlier sessions
//!   (`0x100a`=`recv_ble_data`, `0x100f`=`feed`, `0x101a`=`ble_get_schedule` — all three matched
//!   exactly). Two entries are AI-relevant:
//!     - `msg_id` [`msg::GET_PET_FACE_INFO_BY_NETWORK`] → `dispatch_handler_get_pet_face_info_by_network`:
//!       fires on a face-recognition result. Its own body only validates `payload[0] != 0` and
//!       hands off to further internal processing (a self-dispatch this study did not fully
//!       trace) — it does not itself unpack box/score fields.
//!     - `msg_id` [`msg::CTRL_EVENT_MSG`] → `dispatch_handler_ctrl_event_msg`: a **generic**
//!       event-report handler. It `memcpy`s a **168-byte payload** into a local struct and calls
//!       into a large switch (`pk_ctrl_event_msg_manage_proc`) keyed on an `event_type` field at
//!       **payload offset 0** (confirmed by disassembly: `ldr.w r8,[r4,#8]` where the local
//!       struct's payload copy starts at `local+8`, then a chain of `cmp.w r8,#N` cases).
//!       `event_type == 0x18` (24) is the pet-identification path — traced all the way to the
//!       packer that builds the cloud JSON literally containing
//!       `{"related_event":%s,"count":%d,"area":%d,"pet_id":%s,"tracker_info":%s,"vomit_info":%s}`
//!       (verbatim string in `ctrl`'s rodata). This is the event that carries `pet_id`.
//! - The wire envelope this study already documented (`bus.rs`'s module doc) was independently
//!   re-derived from scratch by disassembling `dispatch_send_msg` itself (byte-identical code in
//!   both `ctrl` @ `0x80b00` and `media` @ `0x338b4`): `u16 msg_id` at offset 0, `u16 src` at
//!   offset 2 (read from a **process-global**, never a caller argument), payload from offset 4,
//!   clamped to 540 (`0x21c`) bytes — exact match, byte for byte, with zero new assumptions.
//! - **Msg-id namespaces are per-destination, not global.** The same numeric id means a
//!   different handler depending which queue it is sent to — proven by decoding `media`'s *own*
//!   registration table the identical way: `media`'s inbox uses small sequential ids (`0x1`
//!   through `0x28`, e.g. `0x24` = `dispatch_handler_recv_pet_face_pic_info`), completely
//!   disjoint from `ctrl`'s `0x1002..0x101e` range. This is why the "prior pointer-table scan"
//!   noted in `docs/06-msgids.md` found no simple strided array: the table is built by a
//!   sequence of individual register-calls, and only makes sense scoped to one recipient.
//!
//! ## Why this module does not open `ctrl`'s queue
//!
//! `msg::CTRL_EVENT_MSG` and `msg::GET_PET_FACE_INFO_BY_NETWORK` are delivered to `ctrl`'s own
//! private inbox (`/msg_dispatch_1`). A POSIX message queue has exactly one reader; `ctrl` is
//! that reader. `kibbled` cannot attach a second reader to it the way it attaches to the frame
//! ring's shared memory — opening `/msg_dispatch_1` for receive and calling `mq_receive` would
//! **steal** the message from `ctrl` (it would never see it), which is a real behavioural change
//! to a process this project's own constraints require leaving alone. This is exactly the
//! documented fallback case: *"if the ONLY path is the mqueue to ctrl, ... the AI feed requires
//! Kibble to replace ctrl."* The struct above is fully documented for that day. Until then, this
//! module's real, working tap is the vendor's own JPEG side effects — see below.
//!
//! ## What this module actually taps: the vendor's own crop files
//!
//! Independent of the mqueue, `libalgo.so`/`media` write plain JPEG files to `/tmp` as a normal
//! part of the same detection pipeline (`docs/03-app.md` §10, re-confirmed present in the pulled
//! binaries this session): [`SAVE_FACE_JPG`] on a face match/enrollment, [`FPRE_PET_JPEG`] on a
//! generic pet/motion visit, [`FPRE_EAT_JPEG`] on an eat event. These are ordinary files under a
//! world-writable tmpfs — reading them is not touching any vendor IPC object at all, just
//! `stat`+`read` on a path, exactly as safe as the JPEG endpoints `03-app.md` already documents
//! `ctrl`/`cloud` themselves polling. A background thread here watches their mtimes and republishes
//! a [`Detection`] on change, giving Scrypted/HA a real, first-pass "something happened, here is
//! the picture" feed today — with `score`/`pet_id`/`box` honestly `null` (that data lives only in
//! the struct above, unreachable without replacing `ctrl`), not fabricated.
//!
//! ## Update: `cat` is real, unlike `score`/`box`
//!
//! Since `docs/27-cat-id.md`, a `"face"`-class detection's crop is also run through
//! [`crate::embed`]'s second-process NPU path and [`crate::faces::Gallery`]'s classifier before
//! being published. [`Detection::cat`] is a **real, first-party** identification: Kibble's own
//! frozen-embedding classifier's opinion, not the vendor's. It is `None` whenever the classifier
//! didn't confidently match an enrolled cat (including "no cats enrolled yet"), never a guess
//! dressed up as a fact.
//!
//! ## Update 2026-09-16: `pet_id` is real too, and it never needed `ctrl`'s queue
//!
//! The earlier sections were right that the `0x1002` *message* is private to `ctrl` and wrong
//! about where the data lives. `pet_id`, `count`, `area` and the per-visit tracker entries are
//! not in the 168-byte payload at all: `media` writes them into `config_shm` at
//! `state::off::PET_TRACK` (`g_config + 0x2880`, study/EventStruct.md §2.3) and `ctrl` reads them
//! back from there to build its cloud JSON. `kibbled` already maps that file read-only, so
//! [`poll_loop`] samples the block on every tick and publishes a `"track"`-class [`Detection`]
//! whenever the newest tracker entry changes -- `pet_id` is the vendor's cloud pet id
//! (`petId` in `/opt/pet_name_color.json`, confirmed equal on the live feeder), `ts` is the
//! vendor's own `start_time`. `score` stays `null` -- the vendor computes no similarity --
//! and `total_score` carries the vendor's own per-visit number under the vendor's own name:
//! the sum over the visit's qualifying frames of the best-candidate confidence
//! (`state::TrackEntry::value`), so bigger means longer and/or steadier, not "more likely".
//! A bounding box does not exist anywhere in the vendor chain, so `box` stays `null` for good.
//!
//! `/tmp/pet_face_pic.jpg` (named in the assignment) is the one exception worth flagging: it
//! exists only as a literal string inside **`ctrl`**, not `media`/`libalgo.so`. Disassembling its
//! one use site shows `ctrl` *opening it for reading* (existence check, `open`-then-`snprintf` of
//! what looks like an upload request), not writing it — so `ctrl` is a consumer, and this study
//! did not locate the writer (most likely `libalgo.so` builds that exact filename with a runtime
//! `snprintf` this static pass could not string-match, or a shell hand-off step exists outside
//! the three pulled binaries). Flagged as an open item rather than guessed.

use std::collections::VecDeque;
use std::fs;
use std::io;
use std::path::Path;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::catid;
use crate::faces;
use crate::state::Shm;

/// Bus message ids relevant to the AI pipeline. See the module doc for how each was recovered.
/// Both are scoped to `ctrl`'s own inbox (`/msg_dispatch_1`) -- meaningless on any other queue.
pub mod msg {
    /// `dispatch_handler_get_pet_face_info_by_network`, size 208 bytes at `ctrl` vaddr `0x52a98`.
    pub const GET_PET_FACE_INFO_BY_NETWORK: u16 = 0x101c;
    /// `dispatch_handler_pet_face_pic_used_end`, size 460 bytes at `ctrl` vaddr `0x528cc` -- the
    /// paired "done with this crop" companion, registered immediately before the above.
    pub const PET_FACE_PIC_USED_END: u16 = 0x101b;
    /// `dispatch_handler_ctrl_event_msg`, size 208 bytes at `ctrl` vaddr `0x3b890`. Generic event
    /// report; payload is [`CTRL_EVENT_MSG_LEN`] bytes, keyed by [`EVENT_TYPE_PET_TRACKING`] etc.
    pub const CTRL_EVENT_MSG: u16 = 0x1002;
}

/// Byte length of `dispatch_handler_ctrl_event_msg`'s payload -- confirmed by disassembly: the
/// handler's own `memcpy(local+8, payload, 0xa8)` call. 0xa8 = 168.
pub const CTRL_EVENT_MSG_LEN: usize = 0xa8;

/// `event_type` value (payload offset 0, u32 LE) that reaches the packer building
/// `{"related_event":%s,"count":%d,"area":%d,"pet_id":%s,"tracker_info":%s,"vomit_info":%s}` --
/// confirmed by tracing `pk_ctrl_event_msg_manage_proc`'s `cmp.w r8,#0x18` branch all the way to
/// the `bl` into that packer. This is the one event_type this study fully traced end to end;
/// the other observed values (0,1,2,3,4,5,6,7,8,0x33,0x34,0x35) reach other JSON shapes
/// (feed/motion/error) that were not each individually traced -- see `docs/24-onboard-ai.md`.
pub const EVENT_TYPE_PET_TRACKING: u32 = 0x18;

/// Field layout of `dispatch_handler_ctrl_event_msg`'s payload that this study actually
/// disassembly-confirmed. Only `event_type` has a nailed-down byte offset; the remaining fields
/// named in the cloud JSON format string (`related_event`, `count`, `area`, `pet_id`,
/// `tracker_info`, `vomit_info`) are known to exist, in that order, from the verbatim format
/// string, but this study did not trace the packer's `snprintf` argument list far enough to
/// recover their individual byte offsets within the 168 bytes -- left as an explicit open
/// question rather than guessed (matching `docs/12-ai.md`'s own confidence convention for the
/// same reason: exact offsets need either a smarter alignment pass or one live wire capture,
/// and this project's constraints forbid capturing `ctrl`'s own inbox to get one).
///
/// Decodes only what is proven: `event_type` at offset 0, LE u32.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CtrlEventMsgHeader {
    pub event_type: u32,
}

impl CtrlEventMsgHeader {
    /// Decode the confirmed header from a `ctrl_event_msg` payload. `None` if `payload` is
    /// shorter than a u32 -- a genuine payload is always [`CTRL_EVENT_MSG_LEN`] bytes, but the
    /// decoder itself only needs the first 4 to do its job.
    pub fn decode(payload: &[u8]) -> Option<Self> {
        let bytes: [u8; 4] = payload.get(0..4)?.try_into().ok()?;
        Some(Self { event_type: u32::from_le_bytes(bytes) })
    }

    pub fn is_pet_tracking(&self) -> bool {
        self.event_type == EVENT_TYPE_PET_TRACKING
    }
}

/// Vendor JPEG artifacts this module polls -- see the module doc's last section. Paths are the
/// literal strings recovered from the pulled binaries' rodata this session.
pub const SAVE_FACE_JPG: &str = "/tmp/saveFace.jpg";
pub const FPRE_PET_JPEG: &str = "/tmp/fPre_pet.jpeg";
pub const FPRE_EAT_JPEG: &str = "/tmp/fPre_eat.jpeg";

struct Watched {
    path: &'static str,
    class: &'static str,
    /// Whether a change to this file should also be captured into the face-enrolment pending
    /// directory (`faces.rs`) -- true only for the algo's own face-recognition crop, not the
    /// generic full-frame visit/eat previews.
    is_face_crop: bool,
}

const WATCHED: &[Watched] = &[
    Watched { path: SAVE_FACE_JPG, class: "face", is_face_crop: true },
    Watched { path: FPRE_PET_JPEG, class: "visit", is_face_crop: false },
    Watched { path: FPRE_EAT_JPEG, class: "eat", is_face_crop: false },
];

/// How often the poll loop re-`stat`s the watched files. `media` writes these on its own
/// detection cadence (seconds, not frames), so sub-second polling would buy nothing.
const POLL_INTERVAL: Duration = Duration::from_millis(1000);
/// How long `GET /events/stream` will block waiting for a new event past `?since=`.
pub const LONG_POLL_TIMEOUT: Duration = Duration::from_secs(25);
/// `GET /events` returns at most this many, newest last -- matches the assignment's own cap.
const MAX_EVENTS: usize = 50;
/// Where a watched crop is copied before the vendor's own pipeline can overwrite it in place
/// (all three watched files are fixed, reused filenames -- the vendor does not rotate them).
const EVENTS_DIR: &str = "/opt/kibble/events";

/// A name is safe to join onto `EVENTS_DIR` if it has no path separators and doesn't spell a
/// traversal -- every name this module itself generates already satisfies this (`poll_loop`'s
/// own `{ts}-{class}.jpg`), but `GET /events/<file>`'s `name` comes from an HTTP client. Mirrors
/// `faces.rs`'s identical check -- this project's established per-module convention (see also
/// `feed_capture.rs`'s `is_safe_feed_file_name`) rather than one shared utility.
fn is_safe_name(name: &str) -> bool {
    !name.is_empty() && !name.contains('/') && !name.contains('\\') && name != "." && name != ".."
}

#[derive(Debug)]
pub enum EventFileError {
    InvalidName,
    NotFound,
    Io(io::Error),
}

impl std::fmt::Display for EventFileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EventFileError::InvalidName => write!(f, "invalid file name"),
            EventFileError::NotFound => write!(f, "no such event file"),
            EventFileError::Io(e) => write!(f, "{e}"),
        }
    }
}

/// `GET /events/<file>`: the raw bytes of one detection crop `poll_loop` wrote to `EVENTS_DIR`
/// (`Detection::image` already names the exact file, for every class -- `face`/`visit`/`eat`
/// alike -- see the module doc's "what this module actually taps" section, and
/// `scrypted-plugin/README.md`'s "Agent-side TODO"). Same path-safety rules as
/// `faces::read_pending`: no traversal, no absolute paths (rejected by [`is_safe_name`]'s
/// no-separator check before ever reaching `join`), serves only from `EVENTS_DIR`.
pub fn read_event(name: &str) -> Result<Vec<u8>, EventFileError> {
    if !is_safe_name(name) {
        return Err(EventFileError::InvalidName);
    }
    match fs::read(Path::new(EVENTS_DIR).join(name)) {
        Ok(b) => Ok(b),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Err(EventFileError::NotFound),
        Err(e) => Err(EventFileError::Io(e)),
    }
}

pub fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

#[derive(Debug, Clone)]
pub struct Detection {
    pub seq: u64,
    pub ts: u64,
    pub class: &'static str,
    /// Always `None` today: the vendor never computes a similarity this pipeline can see (see
    /// "Update 2026-09-16" in the module doc). Kept so the JSON shape stays stable.
    pub score: Option<f32>,
    /// The vendor's cloud pet id, from `config_shm`'s PetTrack block -- `"track"` class only.
    pub pet_id: Option<u32>,
    /// Always `None`: no bounding box exists anywhere in the vendor's chain.
    pub b0x: Option<[f32; 4]>,
    /// Filename under `EVENTS_DIR` this detection's crop was saved to, if the copy succeeded.
    pub image: Option<String>,
    /// Kibble's own classifier's opinion, when confident -- see "Update: `cat` is real" above.
    pub cat: Option<String>,
    /// The vendor's `total_score` for the visit (`state::TrackEntry::value`), `"track"` class
    /// only: the sum of per-frame identification confidence over the tracked visit -- not a
    /// probability, so kept apart from `score`.
    pub total_score: Option<f32>,
}

impl Detection {
    pub fn to_json(&self) -> String {
        fn opt_num(v: Option<f32>) -> String {
            v.map_or("null".into(), |n| n.to_string())
        }
        fn opt_str(v: &Option<String>) -> String {
            v.as_ref().map_or("null".into(), |s| format!("\"{}\"", s.escape_debug()))
        }
        let box_json = match self.b0x {
            Some([x, y, w, h]) => format!("[{x},{y},{w},{h}]"),
            None => "null".into(),
        };
        let pet_id = self.pet_id.map_or("null".to_string(), |v| v.to_string());
        format!(
            r#"{{"seq":{},"ts":{},"class":"{}","score":{},"box":{},"pet_id":{},"image":{},"cat":{},"total_score":{}}}"#,
            self.seq,
            self.ts,
            self.class,
            opt_num(self.score),
            box_json,
            pet_id,
            opt_str(&self.image),
            opt_str(&self.cat),
            opt_num(self.total_score),
        )
    }
}

struct FeedInner {
    events: VecDeque<Detection>,
    next_seq: u64,
}

/// The last [`MAX_EVENTS`] detections, plus a condvar so `GET /events/stream` can long-poll
/// instead of hard-polling -- same shape as `ring.rs`'s `VideoFeed`/`Subscriber` pair, minus the
/// per-client queue (every caller just wants "everything past seq N", not its own private feed).
pub struct Feed {
    inner: Mutex<FeedInner>,
    changed: Condvar,
}

impl Feed {
    pub fn new() -> Arc<Feed> {
        let feed = Arc::new(Feed {
            inner: Mutex::new(FeedInner { events: VecDeque::with_capacity(MAX_EVENTS), next_seq: 1 }),
            changed: Condvar::new(),
        });
        feed.rehydrate_from_disk();
        feed
    }

    /// Rebuild the in-memory feed from the crops already on disk in [`EVENTS_DIR`].
    ///
    /// Without this, `GET /events` reported an empty list after every `kibbled` restart even
    /// though the images were sitting right there -- and `kibbled` restarts for ordinary reasons
    /// (a new binary, the supervisor's respawn loop). Filenames are the ones `poll_loop` writes,
    /// `<unix_ts>-<class>.jpg`, so the timestamp and class are recoverable exactly; `score`,
    /// `pet_id` and `box` stay `None` for the same honest reason they always are (that data only
    /// ever existed in `ctrl`'s private queue, never in the file).
    fn rehydrate_from_disk(&self) {
        let Ok(entries) = fs::read_dir(EVENTS_DIR) else { return };
        let mut found: Vec<(u64, &'static str, String)> = Vec::new();
        for e in entries.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            let Some((ts_str, rest)) = name.split_once('-') else { continue };
            let Ok(ts) = ts_str.parse::<u64>() else { continue };
            let class = match rest.strip_suffix(".jpg").or_else(|| rest.strip_suffix(".jpeg")) {
                Some("visit") => "visit",
                Some("eat") => "eat",
                Some("face") => "face",
                _ => continue,
            };
            found.push((ts, class, name));
        }
        if found.is_empty() {
            return;
        }
        found.sort_by_key(|(ts, _, _)| *ts);
        let skip = found.len().saturating_sub(MAX_EVENTS);
        let mut inner = self.inner.lock().unwrap();
        for (ts, class, name) in found.into_iter().skip(skip) {
            let seq = inner.next_seq;
            inner.next_seq += 1;
            inner.events.push_back(Detection {
                seq,
                ts,
                class,
                score: None,
                pet_id: None,
                b0x: None,
                image: Some(name),
                cat: None,
                total_score: None,
            });
        }
    }

    fn push(&self, class: &'static str, image: Option<String>, cat: Option<String>) {
        self.push_detection(now_unix(), class, image, cat, None, None);
    }

    /// The vendor identified a pet: publish it under the vendor's own `start_time`.
    fn push_track(&self, entry: &crate::state::TrackEntry) {
        self.push_detection(entry.start_time, "track", None, None, Some(entry.pet_id), Some(entry.value));
    }

    fn push_detection(
        &self,
        ts: u64,
        class: &'static str,
        image: Option<String>,
        cat: Option<String>,
        pet_id: Option<u32>,
        total_score: Option<f32>,
    ) {
        let mut inner = self.inner.lock().unwrap();
        let seq = inner.next_seq;
        inner.next_seq += 1;
        inner.events.push_back(Detection {
            seq,
            ts,
            class,
            score: None,
            pet_id,
            b0x: None,
            image,
            cat,
            total_score,
        });
        while inner.events.len() > MAX_EVENTS {
            inner.events.pop_front();
        }
        drop(inner);
        self.changed.notify_all();
        crate::push::mark(crate::push::Field::Events);
    }

    /// `GET /events`: the last [`MAX_EVENTS`], oldest first.
    pub fn snapshot_json(&self) -> String {
        let inner = self.inner.lock().unwrap();
        let items: Vec<String> = inner.events.iter().map(Detection::to_json).collect();
        format!("[{}]", items.join(","))
    }

    /// `GET /events/stream?since=N`: block up to [`LONG_POLL_TIMEOUT`] for at least one event
    /// with `seq > since`, then return whatever is available (possibly empty, on timeout).
    pub fn wait_since(&self, since: u64, timeout: Duration) -> String {
        let inner = self.inner.lock().unwrap();
        let ready = |i: &FeedInner| i.events.back().is_some_and(|e| e.seq > since);
        let inner = if ready(&inner) {
            inner
        } else {
            self.changed.wait_timeout_while(inner, timeout, |i| !ready(i)).unwrap().0
        };
        let items: Vec<String> =
            inner.events.iter().filter(|e| e.seq > since).map(Detection::to_json).collect();
        format!("[{}]", items.join(","))
    }
}

/// One pass over every watched path: if its mtime advanced since `last_seen`, copy it out and
/// publish a [`Detection`]. Returns the updated `last_seen` map entry for the caller to persist.
/// Pure enough to unit test the "did this file change" / naming logic without a real feeder.
fn check_one(w: &Watched, last_seen: Option<SystemTime>) -> (Option<SystemTime>, Option<(SystemTime, Vec<u8>)>) {
    let meta = match fs::metadata(w.path) {
        Ok(m) => m,
        Err(_) => return (last_seen, None), // vendor hasn't written this one (yet) -- not an error
    };
    let mtime = match meta.modified() {
        Ok(t) => t,
        Err(_) => return (last_seen, None),
    };
    if last_seen == Some(mtime) {
        return (last_seen, None);
    }
    match fs::read(w.path) {
        Ok(bytes) => (Some(mtime), Some((mtime, bytes))),
        Err(_) => (last_seen, None), // caught mid-write by the vendor; try again next tick
    }
}

/// Cap on crops kept in [`EVENTS_DIR`]. `/opt` is UBIFS on raw NAND with finite erase cycles and
/// ~56 MB free, and a detection crop lands here on every vendor detection -- unbounded, that is
/// a slow flash-filling leak. 200 is comfortably more than [`MAX_EVENTS`] (so `GET /events` can
/// always be rehydrated in full after a restart) while bounding the directory to a few MB.
const MAX_EVENT_FILES: usize = 200;

/// Delete the oldest crops once the directory exceeds [`MAX_EVENT_FILES`].
///
/// Called only right after a successful write, i.e. once per real detection -- never on a timer,
/// so an idle feeder performs no flash writes at all.
fn prune_events_dir() {
    let Ok(entries) = fs::read_dir(EVENTS_DIR) else { return };
    let mut names: Vec<String> =
        entries.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect();
    if names.len() <= MAX_EVENT_FILES {
        return;
    }
    // Filenames start with a fixed-width-ish unix timestamp, but sort numerically rather than
    // lexically so a digit-count rollover can't pick the wrong victim.
    names.sort_by_key(|n| n.split_once('-').and_then(|(ts, _)| ts.parse::<u64>().ok()).unwrap_or(0));
    for name in names.iter().take(names.len() - MAX_EVENT_FILES) {
        let _ = fs::remove_file(Path::new(EVENTS_DIR).join(name));
    }
}

/// Identity of a tracker entry for change detection: the vendor rewrites the block on every
/// visit, and a new `(pet_id, start_time)` pair is what "a new identification" means. Pure so
/// the tests can drive it without a live `config_shm`.
fn track_key(t: &crate::state::PetTrack) -> Option<(u32, u64)> {
    t.latest().map(|e| (e.pet_id, e.start_time))
}

fn poll_loop(feed: Arc<Feed>, gallery: Arc<faces::Gallery>, shm: Arc<Shm>) {
    let _ = fs::create_dir_all(EVENTS_DIR);
    let mut last_seen: Vec<Option<SystemTime>> = vec![None; WATCHED.len()];
    // Whatever the block holds at startup is history, not a new event: publishing it would
    // re-announce the same visit on every kibbled restart.
    let mut last_track = shm.pet_track().as_ref().and_then(track_key);
    // The vendor-state fields HA reads from `GET /state` (bowl fill, desiccant, feeding, the
    // identification block) have no producer of their own on our side -- media/ble write them
    // straight into config_shm -- so this loop's existing 1 s tick doubles as their change
    // detector: one `Snapshot` compare on memory it already reads, no extra timer.
    let mut last_state = shm.snapshot();
    loop {
        for (i, w) in WATCHED.iter().enumerate() {
            let (new_last, found) = check_one(w, last_seen[i]);
            last_seen[i] = new_last;
            if let Some((_, bytes)) = found {
                let ts = now_unix();
                let name = format!("{ts}-{}.jpg", w.class);
                let saved = fs::write(Path::new(EVENTS_DIR).join(&name), &bytes).is_ok();
                if saved {
                    prune_events_dir();
                }
                let mut cat = None;
                if w.is_face_crop {
                    if let Ok(pending_name) = faces::save_pending(&bytes, None) {
                        let pending_path = Path::new(faces::PENDING_DIR).join(&pending_name);
                        match faces::ensure_embedding(&pending_path) {
                            Ok(feat) => {
                                if let catid::Verdict::Known { cat: found_cat, .. } = gallery.identify(&feat) {
                                    cat = Some(found_cat);
                                }
                            }
                            Err(e) => {
                                eprintln!("kibbled: ai: embed {}: {e}", pending_path.display())
                            }
                        }
                    }
                }
                feed.push(w.class, saved.then_some(name), cat);
            }
        }
        // `None` here means a torn read (media was mid-write) -- keep the previous key and
        // look again next tick rather than treating it as "the block emptied".
        if let Some(track) = shm.pet_track() {
            let key = track_key(&track);
            if key.is_some() && key != last_track {
                feed.push_track(track.latest().unwrap());
            }
            last_track = key;
        }
        let state = shm.snapshot();
        if state != last_state {
            crate::push::mark(crate::push::Field::State);
            last_state = state;
        }
        thread::sleep(POLL_INTERVAL);
    }
}

/// Start the background poller and return the shared feed handle for `main.rs` to route
/// `GET /events`/`GET /events/stream` against.
pub fn spawn(gallery: Arc<faces::Gallery>, shm: Arc<Shm>) -> Arc<Feed> {
    let feed = Feed::new();
    let handle = Arc::clone(&feed);
    thread::spawn(move || poll_loop(handle, gallery, shm));
    feed
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Disassembly-derived, not a live wire capture: this project's own constraints forbid
    /// attaching to `ctrl`'s private inbox to get one (see the module doc), so this exercises
    /// the decoder against a payload constructed from the *confirmed* field layout
    /// (`event_type` LE u32 at offset 0) rather than fabricating a "captured" one.
    #[test]
    fn decodes_pet_tracking_event_type_from_a_disassembly_derived_payload() {
        let mut payload = [0u8; CTRL_EVENT_MSG_LEN];
        payload[0..4].copy_from_slice(&EVENT_TYPE_PET_TRACKING.to_le_bytes());
        let hdr = CtrlEventMsgHeader::decode(&payload).unwrap();
        assert_eq!(hdr.event_type, 0x18);
        assert!(hdr.is_pet_tracking());
    }

    #[test]
    fn other_event_types_are_not_misidentified_as_pet_tracking() {
        let mut payload = [0u8; CTRL_EVENT_MSG_LEN];
        payload[0..4].copy_from_slice(&7u32.to_le_bytes());
        let hdr = CtrlEventMsgHeader::decode(&payload).unwrap();
        assert!(!hdr.is_pet_tracking());
    }

    #[test]
    fn decode_rejects_a_too_short_payload() {
        assert!(CtrlEventMsgHeader::decode(&[1, 2, 3]).is_none());
    }

    #[test]
    fn feed_snapshot_is_empty_json_array_before_anything_is_pushed() {
        let feed = Feed::new();
        assert_eq!(feed.snapshot_json(), "[]");
    }

    #[test]
    fn push_assigns_increasing_seq_and_caps_at_max_events() {
        let feed = Feed::new();
        for _ in 0..(MAX_EVENTS + 10) {
            feed.push("visit", None, None);
        }
        let inner = feed.inner.lock().unwrap();
        assert_eq!(inner.events.len(), MAX_EVENTS);
        // oldest 10 were evicted -- first remaining seq is 11, last is MAX_EVENTS+10
        assert_eq!(inner.events.front().unwrap().seq, 11);
        assert_eq!(inner.events.back().unwrap().seq, (MAX_EVENTS + 10) as u64);
    }

    fn wait_since_returns_immediately_when_already_caught_up_to_a_past_seq() {
        let feed = Feed::new();
        feed.push("eat", None, None);
        feed.push("eat", None, None);
        let json = feed.wait_since(0, Duration::from_millis(50));
        assert!(json.contains("\"seq\":1"));
        assert!(json.contains("\"seq\":2"));
    }

    fn wait_since_filters_out_already_seen_events() {
        let feed = Feed::new();
        feed.push("eat", None, None);
        feed.push("eat", None, None);
        let json = feed.wait_since(1, Duration::from_millis(50));
        assert!(!json.contains("\"seq\":1"));
        assert!(json.contains("\"seq\":2"));
    }

    fn wait_since_times_out_to_an_empty_array_with_nothing_new() {
        let feed = Feed::new();
        feed.push("eat", None, None);
        let json = feed.wait_since(1, Duration::from_millis(30));
        assert_eq!(json, "[]");
    }

    fn detection_json_uses_null_for_the_fields_this_tap_cannot_fill() {
        let d = Detection {
            seq: 1,
            ts: 100,
            class: "face",
            score: None,
            pet_id: None,
            b0x: None,
            image: None,
            cat: None,
            total_score: None,
        };
        let json = d.to_json();
        assert!(json.contains(r#""score":null"#));
        assert!(json.contains(r#""pet_id":null"#));
        assert!(json.contains(r#""box":null"#));
        assert!(json.contains(r#""image":null"#));
        assert!(json.contains(r#""cat":null"#));
    }

    #[test]
    fn detection_json_includes_a_real_cat_when_the_classifier_is_confident() {
        let d = Detection {
            seq: 1,
            ts: 100,
            class: "face",
            score: None,
            pet_id: None,
            b0x: None,
            image: None,
            cat: Some("Rashy".to_string()),
            total_score: None,
        };
        assert!(d.to_json().contains(r#""cat":"Rashy""#));
    }

    #[test]
    fn push_carries_the_identified_cat_through_to_the_stored_detection() {
        let feed = Feed::new();
        feed.push("face", Some("1-Rashy.jpg".to_string()), Some("Rashy".to_string()));
        let inner = feed.inner.lock().unwrap();
        assert_eq!(inner.events.back().unwrap().cat.as_deref(), Some("Rashy"));
    }

    #[test]
    fn push_track_publishes_the_vendor_pet_id_under_its_own_start_time() {
        use crate::state::TrackEntry;
        let feed = Feed::new();
        feed.push_track(&TrackEntry { pet_id: 101320712, start_time: 1789528968, value: 2058.042 });
        let json = feed.snapshot_json();
        assert!(json.contains(r#""ts":1789528968,"class":"track""#), "{json}");
        assert!(json.contains(r#""pet_id":101320712"#), "{json}");
        assert!(json.contains(r#""score":null"#), "{json}");
        assert!(json.contains(r#""total_score":2058.042"#), "{json}");
    }

    #[test]
    fn track_key_changes_only_when_the_newest_entry_changes() {
        use crate::state::{PetTrack, TrackEntry};
        let e = |pet_id, start_time| TrackEntry { pet_id, start_time, value: 0.0 };
        let empty = PetTrack { pet_id: 0, count: 0, area: 0, trackers: vec![] };
        assert_eq!(track_key(&empty), None);
        let one = PetTrack { pet_id: 7, count: 1, area: 100, trackers: vec![e(7, 100)] };
        assert_eq!(track_key(&one), Some((7, 100)));
        // A second, older entry appended behind the newest does not count as a new visit.
        let two = PetTrack { trackers: vec![e(7, 100), e(7, 50)], ..one.clone() };
        assert_eq!(track_key(&two), track_key(&one));
        let newer = PetTrack { trackers: vec![e(7, 100), e(9, 200)], ..one };
        assert_eq!(track_key(&newer), Some((9, 200)));
    }

    // --- GET /events/<file> path safety -------------------------------------------------------

    #[test]
    fn read_event_rejects_path_traversal() {
        let err = read_event("../../etc/passwd").unwrap_err();
        assert!(matches!(err, EventFileError::InvalidName));
    }

    #[test]
    fn read_event_rejects_absolute_paths() {
        let err = read_event("/etc/passwd").unwrap_err();
        assert!(matches!(err, EventFileError::InvalidName));
    }

    #[test]
    fn read_event_reports_not_found_for_a_missing_file() {
        let err = read_event("2026-01-01-nope.jpg").unwrap_err();
        assert!(matches!(err, EventFileError::NotFound | EventFileError::Io(_)));
    }

    #[test]
    fn read_event_serves_a_real_file_written_by_the_poller() {
        // Exercises the exact naming convention `poll_loop` uses, end to end, without needing
        // the real device -- the file this module actually watches (`SAVE_FACE_JPG` etc.) is
        // vendor-only, but the copy step (`fs::write` into `EVENTS_DIR`) is pure filesystem
        // logic this test can drive directly.
        let _ = fs::create_dir_all(EVENTS_DIR);
        let name = format!("{}-visit-readtest.jpg", now_unix());
        fs::write(Path::new(EVENTS_DIR).join(&name), b"fake-jpeg-bytes").unwrap();
        assert_eq!(read_event(&name).unwrap(), b"fake-jpeg-bytes");
        let _ = fs::remove_file(Path::new(EVENTS_DIR).join(&name));
    }

    #[test]
    fn is_safe_name_rejects_empty_dot_and_dotdot() {
        assert!(!is_safe_name(""));
        assert!(!is_safe_name("."));
        assert!(!is_safe_name(".."));
        assert!(is_safe_name("1700000000-face.jpg"));
    }
}
