//! Read-only access to the vendor `media` process's video/audio frame ring, plus a write path
//! into the same ring's `auido-out` talkback slot (`audioout.rs` owns the writer; this module
//! only maps the ring and defines the record header shape both directions agree on).
//!
//! `/dev/shm/media_buffer_frame_buf` is an 8,389,608-byte POSIX shm segment: the first
//! `DATA_START` (1024) bytes are a reader-registration table we don't touch (byte density jumps
//! from ~20-32% nonzero to ~99-100% exactly at that offset -- registration fields versus
//! compressed bitstream), then a true byte-continuous circular buffer of records -- a 56-byte
//! header immediately followed by `length` bytes of payload, back-to-back with zero padding.
//! Video payloads are H.264 Annex-B; a keyframe record is one access unit bundling SPS+PPS+IDR,
//! an interframe record carries one P-slice. `chan` 4 is the 1728x1080 "main" stream, `chan` 8
//! is the 1152x720@25fps "sub" stream; both are served over RTSP (see `rtsp.rs`). `chan` 1 is
//! the microphone: MPEG-4 AAC-LC/16kHz/mono, one complete 1024-sample access unit per record,
//! ADTS-framed (`docs/23-audio-codec.md`) -- also served over RTSP, as a third track both `/main`
//! and `/sub` sessions subscribe to (there's only one microphone regardless of which video mount
//! a client picked).
//!
//! There is no new-frame signal (the vendor creates `sem.media_buffer_reader_6` but never posts
//! to it), so this is a poller: a background thread walks the ring, validates each record (sane
//! length, known channel, global sequence exactly previous+1), and republishes new main/sub
//! records into that channel's `VideoFeed`. On any validation failure it resyncs by scanning
//! forward for the next Annex-B start code.
//!
//! Whether the vendor ever splits a single record's bytes across the ring's physical end
//! (`len`) is not confirmed either way by anything we've read from the device or its binaries.
//! Rather than guess, every offset computation that would need bytes at or past `len` is treated
//! as "not a valid record here" and handled by the same resync path used for a torn read -- at
//! worst this costs one dropped record right at the wrap seam, roughly once per lap (measured
//! 12-20+ s of buffered video), which is inaudible/invisible on a live camera feed.
//!
//! We map the ring read-only and never write it -- we're a passive third reader alongside
//! `agora` and `cloud`, using the same plain POSIX shm+mmap protocol they do.
//!
//! ## Fan-out to multiple RTSP clients
//!
//! `VideoFeed` used to be a single-slot "latest wins" hand-off, correct only because exactly one
//! RTSP client was ever connected at a time. Now that several can be (Scrypted's own prebuffer
//! plus a human debugging with `ffprobe`, per stream), it's a small pub/sub hub instead: one
//! `publish()` call per ring record (the poller never polls more than once per record, regardless
//! of how many clients are attached) fans out to every attached [`Subscriber`]'s own bounded
//! queue. See [`Subscriber::push`] for the per-client backpressure policy, and
//! [`VideoFeed::subscribe`] for the session-count cap that keeps a client count fixed to a small
//! ceiling instead of unbounded.

use std::collections::VecDeque;
use std::fs::File;
use std::io;
use std::os::raw::{c_int, c_void};
use std::os::unix::io::AsRawFd;
use std::slice;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

pub const RING_PATH: &str = "/dev/shm/media_buffer_frame_buf";
pub(crate) const RING_LEN: usize = 8_389_608;
pub(crate) const DATA_START: usize = 1024;
pub(crate) const HDR: usize = 56;

/// Channel field values (record header offset 34).
pub const CHAN_MAIN: u8 = 4;
pub const CHAN_SUB: u8 = 8;
const CHAN_THUMB: u8 = 16;
const CHAN_AUDIO: u8 = 1;
/// The talkback/speaker-bound tag -- confirmed live (not `docs/23-audio-codec.md`'s original
/// inference): every record `agora` wrote during a real app talkback session decoded as valid
/// AAC-LC/16kHz/mono ADTS under this `chan`, active for exactly the wall-clock span
/// `/proc/ax_proc/ao`'s `SndFrm` counter also moved for. `audioout.rs` writes it; recognized
/// here too so the video/mic-audio poller's seq-continuity walk doesn't resync-hiccup every
/// time a talkback record shows up interleaved with ordinary video -- it's simply not
/// dispatched to any feed (see `poll_loop`), just accepted as a normal, known record.
pub(crate) const CHAN_AUDIO_OUT: u8 = 2;

/// Frame type values (record header offset 32).
const FRAME_KEYFRAME: u8 = 1;

/// How long `Walker::step` will keep returning "nothing new" at the same cursor position --
/// normal while polling faster than the ~40 ms sub-channel frame interval -- before deciding the
/// cursor itself is wrong (startup seed, or a wrap we didn't handle cleanly) and resyncing from
/// scratch via an Annex-B scan.
const STALL_RESYNC_AFTER: Duration = Duration::from_millis(400);
/// How much of the ring to let accumulate in our own resident set before advising it back out.
const ADVISE_CHUNK: usize = 512 * 1024;
/// Poll cadence when the walker has drained everything currently available.
const POLL_SLEEP: Duration = Duration::from_millis(10);
/// Averaging window for each stream's observed fps (`GET /streams`, see `FpsWindow`).
const FPS_WINDOW: Duration = Duration::from_secs(2);

extern "C" {
    fn mmap(
        addr: *mut c_void,
        len: usize,
        prot: c_int,
        flags: c_int,
        fd: c_int,
        offset: i64,
    ) -> *mut c_void;
    fn munmap(addr: *mut c_void, len: usize) -> c_int;
    fn madvise(addr: *mut c_void, len: usize, advice: c_int) -> c_int;
}

const PROT_READ: c_int = 1;
const MAP_SHARED: c_int = 1;
const MAP_FAILED: isize = -1;
const MADV_DONTNEED: c_int = 4;
const PAGE_SIZE: usize = 4096;

/// Read-only mapping of the ring. Reads are plain loads; nothing here ever writes it.
struct Ring {
    base: *const u8,
    len: usize,
}

// The mapping is read-only and the pointer is stable for the process lifetime.
unsafe impl Send for Ring {}
unsafe impl Sync for Ring {}

impl Ring {
    fn open() -> io::Result<Self> {
        let f = File::open(RING_PATH)?;
        let len = f.metadata()?.len() as usize;
        if len < RING_LEN {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("{RING_PATH} is {len} bytes, expected at least {RING_LEN}"),
            ));
        }
        let p = unsafe {
            mmap(std::ptr::null_mut(), len, PROT_READ, MAP_SHARED, f.as_raw_fd(), 0)
        };
        if p as isize == MAP_FAILED {
            return Err(io::Error::last_os_error());
        }
        Ok(Self { base: p as *const u8, len })
    }

    #[inline]
    fn as_bytes(&self) -> &[u8] {
        unsafe { slice::from_raw_parts(self.base, self.len) }
    }

    /// Drop the process's resident pages for `[from, to)` (rounded to whole pages) so a
    /// continuously-walking poller doesn't accumulate the whole 8 MiB ring into its own RSS --
    /// the pages stay in the (tmpfs-backed, always-resident) shared page cache and re-fault
    /// cheaply if touched again; this only affects our own mapping's accounting.
    fn advise_dontneed(&self, from: usize, to: usize) {
        let from = (from + PAGE_SIZE - 1) & !(PAGE_SIZE - 1);
        let to = to & !(PAGE_SIZE - 1);
        if to > from {
            unsafe {
                madvise(self.base.add(from) as *mut c_void, to - from, MADV_DONTNEED);
            }
        }
    }
}

impl Drop for Ring {
    fn drop(&mut self) {
        unsafe { munmap(self.base as *mut c_void, self.len) };
    }
}

/// One record's header, parsed and structurally sanity-checked. Does not imply sequence
/// continuity with whatever a caller read before it -- `Walker` tracks that. `pub(crate)` (and
/// so is `parse_header`/`find_next_header` below): `audioout.rs` reuses these exact primitives
/// for its own writer-side "where's the current tail" walk, rather than a second reimplementation
/// of the same record-header format.
pub(crate) struct Header {
    pub(crate) seq: u32,
    /// Offset 8: a separate monotonic counter per channel (main/sub/thumb/mic/audio-out each
    /// increment their own copy by exactly 1 per record of that type). `audioout.rs` tracks this
    /// for `CHAN_AUDIO_OUT` so its own writes continue whatever numbering `agora`'s already did,
    /// rather than restarting at an arbitrary value a real reader might reject.
    pub(crate) chan_seq: u32,
    pub(crate) length: u32,
    pub(crate) pts_us: u32,
    pub(crate) frame_type: u8,
    pub(crate) chan: u8,
    pub(crate) width: u16,
    pub(crate) height: u16,
}

/// Parse and structurally validate a record header from `buf[off..]`: in-bounds header, sane
/// payload length, known channel, and the whole record (header plus payload) fitting inside
/// `buf` without needing bytes at or past `buf.len()`. Pure function of a byte slice so it's
/// testable without a real mapping.
pub(crate) fn parse_header(buf: &[u8], off: usize) -> Option<Header> {
    if off.checked_add(HDR)? > buf.len() {
        return None;
    }
    let b = &buf[off..off + HDR];
    let seq = u32::from_le_bytes(b[0..4].try_into().unwrap());
    let length = u32::from_le_bytes(b[4..8].try_into().unwrap());
    let chan_seq = u32::from_le_bytes(b[8..12].try_into().unwrap());
    let pts_us = u32::from_le_bytes(b[16..20].try_into().unwrap());
    let frame_type = b[32];
    let chan = b[34];
    // Video width/height (offset 46/48); repurposed as bits-per-sample/sample-rate on audio
    // records, but nothing here reads those two fields for audio, so no harm in always parsing
    // them the same way (see docs/19-frame-ring.md's per-record header table).
    let width = u16::from_le_bytes(b[46..48].try_into().unwrap());
    let height = u16::from_le_bytes(b[48..50].try_into().unwrap());
    if !(1..=2_000_000).contains(&length) {
        return None;
    }
    if !matches!(chan, CHAN_AUDIO | CHAN_MAIN | CHAN_SUB | CHAN_THUMB | CHAN_AUDIO_OUT) {
        return None;
    }
    if off + HDR + length as usize > buf.len() {
        return None;
    }
    Some(Header { seq, chan_seq, length, pts_us, frame_type, chan, width, height })
}

/// Scan forward from `from` for the next byte offset that starts with an Annex-B start code
/// (`00 00 00 01`) *and* whose implied header (`HDR` bytes back) passes `parse_header`. Wraps
/// once -- `[from, buf.len())` then `[DATA_START, from)` -- so it always terminates within one
/// lap of the ring.
pub(crate) fn find_next_header(buf: &[u8], from: usize) -> Option<usize> {
    let start = from.clamp(DATA_START, buf.len());
    scan_range(buf, start, buf.len()).or_else(|| scan_range(buf, DATA_START, start))
}

fn scan_range(buf: &[u8], from: usize, to: usize) -> Option<usize> {
    if from + 4 > to || to > buf.len() {
        return None;
    }
    let hay = &buf[from..to];
    for i in 0..=hay.len() - 4 {
        if hay[i] == 0 && hay[i + 1] == 0 && hay[i + 2] == 0 && hay[i + 3] == 1 {
            let hit = from + i;
            if hit >= HDR {
                let hdr_off = hit - HDR;
                if parse_header(buf, hdr_off).is_some() {
                    return Some(hdr_off);
                }
            }
        }
    }
    None
}

/// Decide whether `next_pos` has advanced far enough past `advised_upto` to be worth an
/// `madvise(MADV_DONTNEED)` call, returning the updated `advised_upto` and the range to advise
/// (if any). Pure so the RSS-bounding policy is testable without a real mapping.
fn advise_plan(advised_upto: usize, next_pos: usize, ring_len: usize) -> (usize, Option<(usize, usize)>) {
    if next_pos >= advised_upto {
        if next_pos - advised_upto >= ADVISE_CHUNK {
            (next_pos, Some((advised_upto, next_pos)))
        } else {
            (advised_upto, None)
        }
    } else {
        // Wrapped since the last advise: flush the tail and restart the count from the top.
        (DATA_START, Some((advised_upto, ring_len)))
    }
}

/// Cursor and validation state for one continuous walk through the ring.
struct Walker {
    next_pos: usize,
    prev_seq: Option<u32>,
    advised_upto: usize,
    stalled_since: Option<Instant>,
}

impl Walker {
    fn new() -> Self {
        Walker { next_pos: DATA_START, prev_seq: None, advised_upto: DATA_START, stalled_since: None }
    }

    /// Fast-forward from the start of the data region to the writer's current position, with no
    /// resync along the way. A single no-resync pass from `DATA_START` always reaches exactly the
    /// writer's position: every byte gets overwritten in one strictly-forward sweep per lap, so
    /// `[DATA_START, writer)` is one seq-continuous chain and the writer's own not-yet-overwritten
    /// (stale, previous-lap) byte is exactly where that chain breaks. Leaves the walker ready to
    /// poll from "now" instead of replaying however much history the ring happens to hold.
    fn seed(&mut self, buf: &[u8]) {
        let mut pos = find_next_header(buf, DATA_START).unwrap_or(DATA_START);
        let mut prev_seq = None;
        while let Some(h) = parse_header(buf, pos) {
            if !prev_seq.map_or(true, |s| h.seq == s + 1) {
                break;
            }
            prev_seq = Some(h.seq);
            pos += HDR + h.length as usize;
        }
        self.next_pos = pos;
        self.prev_seq = prev_seq;
        self.advised_upto = DATA_START;
    }

    /// Try to consume exactly one more record. `None` means "nothing new right now", which is
    /// the normal state between frames, not an error; after `STALL_RESYNC_AFTER` of that in a
    /// row (tracked in wall-clock time across calls, whether or not the caller sleeps between
    /// them) it resyncs on its own.
    fn step(&mut self, buf: &[u8]) -> Option<(Header, usize)> {
        if self.next_pos + HDR > buf.len() {
            self.next_pos = DATA_START;
        }
        if let Some(h) = parse_header(buf, self.next_pos) {
            if self.prev_seq.map_or(true, |s| h.seq == s + 1) {
                let payload_off = self.next_pos + HDR;
                self.prev_seq = Some(h.seq);
                self.next_pos += HDR + h.length as usize;
                self.stalled_since = None;
                return Some((h, payload_off));
            }
        }
        let now = Instant::now();
        let stalled_since = *self.stalled_since.get_or_insert(now);
        if now.duration_since(stalled_since) >= STALL_RESYNC_AFTER {
            if let Some(p) = find_next_header(buf, self.next_pos) {
                self.next_pos = p;
                self.prev_seq = None;
            }
            self.stalled_since = None;
        }
        None
    }

    fn maybe_advise(&mut self, ring: &Ring) {
        let (new_upto, range) = advise_plan(self.advised_upto, self.next_pos, ring.len);
        if let Some((from, to)) = range {
            ring.advise_dontneed(from, to);
        }
        self.advised_upto = new_upto;
    }
}

/// Shared, lock-free snapshot of the poller's current position in the ring: the offset
/// immediately after the last record it validated, that record's global sequence number, and
/// (separately) the last per-channel sequence number seen on `CHAN_AUDIO_OUT`. `audioout.rs`
/// seeds its own append point from this rather than repeating a full ring scan on every write --
/// the poller is already walking continuously (up to 100 Hz, `POLL_SLEEP`), so this is never more
/// than one poll tick stale. That staleness is exactly why `audioout.rs` still does a short, fresh
/// catch-up walk immediately before every actual write instead of trusting this snapshot as the
/// literal write target: several records can land in even one poll tick, and writing at a
/// position real data has already moved past would guarantee, not just risk, a collision. This
/// cursor only narrows that catch-up walk from "scan the whole ring" to "check the last couple of
/// records" -- see `audioout.rs`'s module doc for the full writer-side synchronization rationale
/// (there is no known, safely-reverse-engineerable atomic claim primitive for this ring's write
/// side; every prior research session that looked -- docs/11-media.md §4, docs/19-frame-ring.md
/// §1 -- explicitly flagged slot 0's exact semantics as unrecovered).
pub struct TailCursor {
    ready: AtomicBool,
    next_pos: AtomicU32,
    global_seq: AtomicU32,
    chan2_seq: AtomicU32,
}

impl TailCursor {
    pub(crate) fn new() -> Arc<Self> {
        Arc::new(Self {
            ready: AtomicBool::new(false),
            next_pos: AtomicU32::new(DATA_START as u32),
            global_seq: AtomicU32::new(0),
            chan2_seq: AtomicU32::new(0),
        })
    }

    fn update(&self, global_seq: u32, next_pos: usize, chan2_seq: Option<u32>) {
        self.next_pos.store(next_pos as u32, Ordering::Release);
        self.global_seq.store(global_seq, Ordering::Release);
        if let Some(s) = chan2_seq {
            self.chan2_seq.store(s, Ordering::Release);
        }
        self.ready.store(true, Ordering::Release);
    }

    /// `(next_pos, last_global_seq, last_chan2_seq)`, or `None` before the poller has validated
    /// its first record (startup only -- normally seeded within milliseconds).
    pub(crate) fn snapshot(&self) -> Option<(usize, u32, u32)> {
        if !self.ready.load(Ordering::Acquire) {
            return None;
        }
        Some((
            self.next_pos.load(Ordering::Acquire) as usize,
            self.global_seq.load(Ordering::Acquire),
            self.chan2_seq.load(Ordering::Acquire),
        ))
    }
}

/// One access unit from a video channel: exactly the bytes stored in the record's payload
/// (Annex-B, start-code prefixed), copied out of the mmap so it can outlive the poller's next
/// step.
#[derive(Clone)]
pub struct Frame {
    pub pts_us: u32,
    pub keyframe: bool,
    pub data: Vec<u8>,
}

/// Tracks a stream's observed frame rate as a simple windowed average: count every `tick()`,
/// and once `FPS_WINDOW` of wall-clock time has actually elapsed, turn that count into a rate and
/// start counting again. Takes `now` as a parameter rather than calling `Instant::now()` itself so
/// it's testable with synthetic timestamps.
struct FpsWindow {
    count: u32,
    window_start: Instant,
    fps: f64,
}

impl FpsWindow {
    fn new(now: Instant) -> Self {
        FpsWindow { count: 0, window_start: now, fps: 0.0 }
    }

    fn tick(&mut self, now: Instant) {
        self.count += 1;
        let elapsed = now.saturating_duration_since(self.window_start);
        if elapsed >= FPS_WINDOW {
            self.fps = self.count as f64 / elapsed.as_secs_f64();
            self.count = 0;
            self.window_start = now;
        }
    }
}

/// A single client's inbox: fed by [`VideoFeed::publish`] from the one poller thread, drained by
/// that client's own RTSP session thread via [`Subscription::recv`]. Bounded so a stalled client
/// can never grow without limit or block the poller.
struct Subscriber {
    id: u64,
    /// The client's `ip:port`, exactly as `TcpStream::peer_addr` reported it -- carried here
    /// purely for `GET /streams` diagnostics (`VideoFeed::snapshot`), not used for any control
    /// decision.
    peer: String,
    queue: Mutex<VecDeque<Frame>>,
    changed: Condvar,
    cap: usize,
}

impl Subscriber {
    /// Enqueue `frame`, applying the eviction policy if already at `cap`. The invariant this
    /// maintains: never let an interframe sit in the queue without the keyframe it decodes
    /// against actually still being there to be delivered first -- either both survive, or
    /// neither does.
    ///
    /// - If `frame` is itself a keyframe: it is a complete, self-contained resync point
    ///   (SPS+PPS+IDR) that makes everything buffered before it moot -- an interframe still
    ///   queued at this point references a keyframe the client hasn't been sent yet, so keeping
    ///   it while dropping that keyframe would hand the client an undecodable frame. Clear the
    ///   whole queue and start clean from this keyframe.
    /// - Otherwise (another interframe arrived while already full): keep any keyframe already
    ///   queued -- it is the client's only resync point -- and drop the oldest *interframe*
    ///   instead. An interframe after a dropped predecessor is already useless to a decoder, so
    ///   dropping older ones costs nothing beyond what the stall already cost.
    ///
    /// This runs on the poller thread (via `publish`), so it must never block on anything but
    /// this one subscriber's own short-held mutex.
    fn push(&self, frame: Frame) {
        let mut q = self.queue.lock().unwrap();
        if q.len() >= self.cap {
            if frame.keyframe {
                q.clear();
            } else {
                match q.iter().position(|f| !f.keyframe) {
                    Some(i) => {
                        q.remove(i);
                    }
                    None => {
                        // Every queued frame is a keyframe (degenerate cap==1 case) -- nothing
                        // else to drop.
                        q.pop_front();
                    }
                }
            }
        }
        q.push_back(frame);
        self.changed.notify_one();
    }

    fn recv(&self, timeout: Duration) -> Option<Frame> {
        let q = self.queue.lock().unwrap();
        let (mut q, _) = self.changed.wait_timeout_while(q, timeout, |q| q.is_empty()).unwrap();
        q.pop_front()
    }
}

struct FeedInner {
    latest_keyframe: Option<Frame>,
    next_id: u64,
    subscribers: Vec<Arc<Subscriber>>,
    width: u16,
    height: u16,
    fps: FpsWindow,
}

/// Snapshot of one stream's state for `GET /streams`: what the ring is actually producing (so a
/// resolution/fps mismatch is visible) and exactly who is attached right now.
pub struct StreamSnapshot {
    pub width: u16,
    pub height: u16,
    pub fps: f64,
    pub sessions: Vec<String>,
}

/// Fan-out point between the ring-poller thread and however many RTSP sessions are currently
/// playing this stream. One poll of the ring (see `poll_loop`) feeds every subscriber; each
/// subscriber has its own small bounded queue (see `Subscriber::push`) so a slow client only ever
/// drops its own frames and never blocks the poller or any other client.
pub struct VideoFeed {
    inner: Mutex<FeedInner>,
}

/// A live subscription to a [`VideoFeed`], held for the lifetime of one RTSP PLAY session and
/// counting against that feed's session cap until dropped.
pub struct Subscription {
    feed: Arc<VideoFeed>,
    sub: Arc<Subscriber>,
}

impl Subscription {
    /// Block up to `timeout` for this client's next queued frame. `None` on timeout with nothing
    /// queued -- callers use that to go check their socket for incoming client requests
    /// (GET_PARAMETER, TEARDOWN) without a dedicated thread per connection.
    pub fn recv(&self, timeout: Duration) -> Option<Frame> {
        self.sub.recv(timeout)
    }
}

impl Drop for Subscription {
    fn drop(&mut self) {
        let mut inner = self.feed.inner.lock().unwrap();
        inner.subscribers.retain(|s| s.id != self.sub.id);
    }
}

impl VideoFeed {
    pub fn new() -> Arc<VideoFeed> {
        Arc::new(VideoFeed {
            inner: Mutex::new(FeedInner {
                latest_keyframe: None,
                next_id: 0,
                subscribers: Vec::new(),
                width: 0,
                height: 0,
                fps: FpsWindow::new(Instant::now()),
            }),
        })
    }

    fn publish(&self, frame: Frame, width: u16, height: u16) {
        let mut inner = self.inner.lock().unwrap();
        inner.width = width;
        inner.height = height;
        inner.fps.tick(Instant::now());
        if frame.keyframe {
            inner.latest_keyframe = Some(frame.clone());
        }
        for sub in &inner.subscribers {
            sub.push(frame.clone());
        }
    }

    /// The most recently seen keyframe, if any. Used by DESCRIBE to build SDP without consuming a
    /// session slot.
    pub fn latest_keyframe(&self) -> Option<Frame> {
        self.inner.lock().unwrap().latest_keyframe.clone()
    }

    /// Register a new session, seeded with the current keyframe (if any) so it can start decoding
    /// immediately instead of waiting out the ~4s GOP for the next one -- unless `max_sessions`
    /// are already active, in which case this returns `None` and the caller must refuse the
    /// client (RTSP `453 Not Enough Bandwidth`) rather than let it hang. On success, also returns
    /// the number of sessions now active (including this one) so the caller can tell whether this
    /// was the spare slot beyond the expected one client.
    pub fn subscribe(
        self: &Arc<Self>,
        peer: String,
        max_sessions: usize,
        queue_cap: usize,
    ) -> Option<(Subscription, usize)> {
        let mut inner = self.inner.lock().unwrap();
        if inner.subscribers.len() >= max_sessions {
            return None;
        }
        let id = inner.next_id;
        inner.next_id += 1;
        let mut queue = VecDeque::with_capacity(queue_cap);
        if let Some(kf) = &inner.latest_keyframe {
            queue.push_back(kf.clone());
        }
        let sub = Arc::new(Subscriber { id, peer, queue: Mutex::new(queue), changed: Condvar::new(), cap: queue_cap });
        inner.subscribers.push(Arc::clone(&sub));
        let active = inner.subscribers.len();
        Some((Subscription { feed: Arc::clone(self), sub }, active))
    }

    /// Current state for `GET /streams`: see [`StreamSnapshot`].
    pub fn snapshot(&self) -> StreamSnapshot {
        let inner = self.inner.lock().unwrap();
        StreamSnapshot {
            width: inner.width,
            height: inner.height,
            fps: inner.fps.fps,
            sessions: inner.subscribers.iter().map(|s| s.peer.clone()).collect(),
        }
    }
}

/// One AAC access unit from the ring's mic-audio channel: the raw ring payload, ADTS-framed,
/// exactly as `media`'s encoder wrote it (`docs/23-audio-codec.md` -- one complete 1024-sample
/// AAC-LC access unit per record, 7-byte ADTS header, no CRC). `rtsp.rs` strips the ADTS header
/// and RFC-3640-wraps what's left; nothing here re-encodes or re-frames it.
#[derive(Clone)]
pub struct AudioFrame {
    pub pts_us: u32,
    pub data: Vec<u8>,
}

/// One client's audio inbox -- the audio equivalent of `Subscriber`, minus the keyframe
/// eviction policy: every AAC access unit decodes independently (no GOP dependency), so once a
/// slow client's queue is full the only sane policy is "drop the oldest queued frame".
struct AudioSubscriber {
    id: u64,
    queue: Mutex<VecDeque<AudioFrame>>,
    changed: Condvar,
    cap: usize,
}

impl AudioSubscriber {
    fn push(&self, frame: AudioFrame) {
        let mut q = self.queue.lock().unwrap();
        if q.len() >= self.cap {
            q.pop_front();
        }
        q.push_back(frame);
        self.changed.notify_one();
    }

    fn recv(&self, timeout: Duration) -> Option<AudioFrame> {
        let q = self.queue.lock().unwrap();
        let (mut q, _) = self.changed.wait_timeout_while(q, timeout, |q| q.is_empty()).unwrap();
        q.pop_front()
    }
}

struct AudioFeedInner {
    latest: Option<AudioFrame>,
    next_id: u64,
    subscribers: Vec<Arc<AudioSubscriber>>,
}

/// Fan-out point for the ring's one mic-audio channel, mirroring `VideoFeed` but shared by
/// *both* RTSP mounts: `/main` and `/sub` each get their own video feed (different ring
/// channels), but there is only one microphone, so `main.rs` owns a single `Arc<AudioFeed>` that
/// every session on either mount subscribes to.
pub struct AudioFeed {
    inner: Mutex<AudioFeedInner>,
}

/// A live subscription to an [`AudioFeed`], held for the lifetime of one RTSP PLAY session.
pub struct AudioSubscription {
    feed: Arc<AudioFeed>,
    sub: Arc<AudioSubscriber>,
}

impl AudioSubscription {
    pub fn recv(&self, timeout: Duration) -> Option<AudioFrame> {
        self.sub.recv(timeout)
    }
}

impl Drop for AudioSubscription {
    fn drop(&mut self) {
        let mut inner = self.feed.inner.lock().unwrap();
        inner.subscribers.retain(|s| s.id != self.sub.id);
    }
}

impl AudioFeed {
    pub fn new() -> Arc<AudioFeed> {
        Arc::new(AudioFeed { inner: Mutex::new(AudioFeedInner { latest: None, next_id: 0, subscribers: Vec::new() }) })
    }

    fn publish(&self, frame: AudioFrame) {
        let mut inner = self.inner.lock().unwrap();
        inner.latest = Some(frame.clone());
        for sub in &inner.subscribers {
            sub.push(frame.clone());
        }
    }

    /// Register a new session, seeded with the most recently published frame (if any) so PLAY
    /// doesn't have to wait out a full ~64ms AAC frame period for its first packet. Unlike
    /// `VideoFeed::subscribe`, there is no session cap here: a client only ever reaches this
    /// after `rtsp.rs` has already cleared `VideoFeed`'s cap for whichever mount it SETUP, so a
    /// second independent limit here would just double-count the same ceiling under a different
    /// name.
    pub fn subscribe(self: &Arc<Self>, queue_cap: usize) -> AudioSubscription {
        let mut inner = self.inner.lock().unwrap();
        let id = inner.next_id;
        inner.next_id += 1;
        let mut queue = VecDeque::with_capacity(queue_cap);
        if let Some(f) = &inner.latest {
            queue.push_back(f.clone());
        }
        let sub = Arc::new(AudioSubscriber { id, queue: Mutex::new(queue), changed: Condvar::new(), cap: queue_cap });
        inner.subscribers.push(Arc::clone(&sub));
        AudioSubscription { feed: Arc::clone(self), sub }
    }
}

fn poll_loop(
    ring: Ring,
    main_feed: Arc<VideoFeed>,
    sub_feed: Arc<VideoFeed>,
    audio_feed: Arc<AudioFeed>,
    tail: Arc<TailCursor>,
) {
    let mut w = Walker::new();
    w.seed(ring.as_bytes());
    loop {
        let mut made_progress = false;
        while let Some((h, payload_off)) = w.step(ring.as_bytes()) {
            made_progress = true;
            match h.chan {
                CHAN_MAIN | CHAN_SUB => {
                    let feed = if h.chan == CHAN_MAIN { &main_feed } else { &sub_feed };
                    let data = ring.as_bytes()[payload_off..payload_off + h.length as usize].to_vec();
                    feed.publish(
                        Frame { pts_us: h.pts_us, keyframe: h.frame_type == FRAME_KEYFRAME, data },
                        h.width,
                        h.height,
                    );
                }
                CHAN_AUDIO => {
                    let data = ring.as_bytes()[payload_off..payload_off + h.length as usize].to_vec();
                    audio_feed.publish(AudioFrame { pts_us: h.pts_us, data });
                }
                _ => {}
            }
            let next_pos = payload_off + h.length as usize;
            let chan2_seq = (h.chan == CHAN_AUDIO_OUT).then_some(h.chan_seq);
            tail.update(h.seq, next_pos, chan2_seq);
        }
        w.maybe_advise(&ring);
        if !made_progress {
            thread::sleep(POLL_SLEEP);
        }
    }
}

/// Open the ring and spawn the background poller thread that feeds `main_feed` (chan
/// `CHAN_MAIN`), `sub_feed` (chan `CHAN_SUB`) and `audio_feed` (chan `CHAN_AUDIO`) from a single
/// walk through the ring -- one poll, three writers, regardless of how many RTSP clients any of
/// them ends up fanning out to. The only fallible step is the initial open (bad path, too-small
/// file, mmap failure); the poll loop itself never stops on its own. Also returns a
/// [`TailCursor`] the same walk keeps fresh, for `audioout.rs`'s writer to seed its own append
/// point from (see `TailCursor`'s doc comment).
pub fn spawn(
    main_feed: Arc<VideoFeed>,
    sub_feed: Arc<VideoFeed>,
    audio_feed: Arc<AudioFeed>,
) -> io::Result<(thread::JoinHandle<()>, Arc<TailCursor>)> {
    let ring = Ring::open()?;
    let tail = TailCursor::new();
    let tail_for_poller = Arc::clone(&tail);
    Ok((
        thread::spawn(move || poll_loop(ring, main_feed, sub_feed, audio_feed, tail_for_poller)),
        tail,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_record(seq: u32, chan: u8, frame_type: u8, payload: &[u8]) -> Vec<u8> {
        let mut r = vec![0u8; HDR + payload.len()];
        r[0..4].copy_from_slice(&seq.to_le_bytes());
        r[4..8].copy_from_slice(&(payload.len() as u32).to_le_bytes());
        r[32] = frame_type;
        r[34] = chan;
        r[HDR..].copy_from_slice(payload);
        r
    }

    fn ring_prefix() -> Vec<u8> {
        vec![0u8; DATA_START]
    }

    #[test]
    fn parse_header_reads_known_fields() {
        let mut buf = ring_prefix();
        buf.extend(make_record(7, CHAN_SUB, 1, b"\x00\x00\x00\x01payload"));
        buf[DATA_START + 46..DATA_START + 48].copy_from_slice(&1152u16.to_le_bytes());
        buf[DATA_START + 48..DATA_START + 50].copy_from_slice(&720u16.to_le_bytes());
        let h = parse_header(&buf, DATA_START).expect("valid header");
        assert_eq!(h.seq, 7);
        assert_eq!(h.chan, CHAN_SUB);
        assert_eq!(h.frame_type, 1);
        assert_eq!(h.length as usize, b"\x00\x00\x00\x01payload".len());
        assert_eq!(h.width, 1152);
        assert_eq!(h.height, 720);
    }

    #[test]
    fn parse_header_rejects_record_that_would_run_past_the_mapping() {
        let mut buf = ring_prefix();
        buf.extend(vec![0u8; HDR]);
        buf[DATA_START + 4..DATA_START + 8].copy_from_slice(&1000u32.to_le_bytes());
        buf[DATA_START + 34] = CHAN_SUB;
        assert!(parse_header(&buf, DATA_START).is_none());
    }

    #[test]
    fn parse_header_rejects_unknown_channel() {
        let mut buf = ring_prefix();
        buf.extend(make_record(1, 99, 1, b"x"));
        assert!(parse_header(&buf, DATA_START).is_none());
    }

    #[test]
    fn parse_header_rejects_zero_length() {
        let mut buf = ring_prefix();
        buf.extend(make_record(1, CHAN_SUB, 1, b""));
        assert!(parse_header(&buf, DATA_START).is_none());
    }

    #[test]
    fn find_next_header_skips_false_positive_start_codes() {
        let mut buf = ring_prefix();
        // A start-code-looking run too close to the data start to have a real header behind it.
        buf.extend_from_slice(&[0, 0, 0, 1, 0xAA]);
        buf.extend(make_record(3, CHAN_SUB, 2, &[0, 0, 0, 1, 0x41, 0x42]));
        let found = find_next_header(&buf, DATA_START).expect("should find the real header");
        let h = parse_header(&buf, found).unwrap();
        assert_eq!(h.seq, 3);
    }

    #[test]
    fn find_next_header_wraps_when_nothing_matches_before_the_end() {
        let mut buf = ring_prefix();
        let real_at = buf.len();
        buf.extend(make_record(9, CHAN_MAIN, 2, &[0, 0, 0, 1, 5])); // real record right after DATA_START
        let scan_start = buf.len();
        buf.extend(vec![0xAAu8; 64]); // junk tail with no start code at all -> first pass finds nothing
        let found = find_next_header(&buf, scan_start).expect("wraps around to find the earlier record");
        assert_eq!(found, real_at);
    }

    #[test]
    fn walker_steps_through_a_contiguous_chain_in_order() {
        let mut buf = ring_prefix();
        buf.extend(make_record(1, CHAN_SUB, 1, b"a"));
        buf.extend(make_record(2, CHAN_MAIN, 2, b"bb"));
        buf.extend(make_record(3, CHAN_SUB, 2, b"c"));
        // No seed() here: seed() fast-forwards past all contiguous backlog by design (see its
        // doc comment), which would consume all three records before this test ever calls
        // step(). A fresh Walker::new() already starts at DATA_START with no prior sequence.
        let mut w = Walker::new();
        let (h1, _) = w.step(&buf).expect("first record");
        assert_eq!(h1.seq, 1);
        let (h2, _) = w.step(&buf).expect("second record");
        assert_eq!(h2.seq, 2);
        let (h3, _) = w.step(&buf).expect("third record");
        assert_eq!(h3.seq, 3);
        assert!(w.step(&buf).is_none(), "caught up to the writer, nothing more to read");
    }

    #[test]
    fn walker_wraps_next_pos_back_to_data_start_at_the_physical_end() {
        // All-zero buffer: no record anywhere, so a wrapped read cleanly fails validation
        // (length 0) rather than incidentally landing on real data placed right at DATA_START.
        let ring_len = DATA_START + 200;
        let buf = vec![0u8; ring_len];
        let mut w = Walker::new();
        w.next_pos = ring_len - 2; // too close to the end to hold a full header
        assert!(w.step(&buf).is_none());
        assert_eq!(w.next_pos, DATA_START);
    }

    #[test]
    fn walker_stalls_on_seq_gap_then_resyncs_after_the_threshold() {
        let mut buf = ring_prefix();
        buf.extend(make_record(1, CHAN_SUB, 1, &[0, 0, 0, 1, 0x41]));
        let gap_start = buf.len();
        // seq jumps 1 -> 5, not a torn record; a real Annex-B start code in its payload so the
        // resync scan below has something legitimate to find (the previous record's header
        // fields are chosen so they don't false-positive-match one either -- length=5 and
        // length=6 both avoid a trailing 0x01 byte at the seq/length field boundary).
        buf.extend(make_record(5, CHAN_SUB, 2, &[0, 0, 0, 1, 0x42, 0x43]));
        let mut w = Walker::new();
        let (h1, _) = w.step(&buf).expect("first record");
        assert_eq!(h1.seq, 1);
        assert!(w.step(&buf).is_none(), "seq 5 != prev+1, must not be accepted as-is");
        // Force the stall clock past the threshold instead of sleeping 400ms in a test.
        w.stalled_since = Some(Instant::now() - STALL_RESYNC_AFTER);
        assert!(w.step(&buf).is_none(), "this call performs the resync itself");
        assert_eq!(w.next_pos, gap_start);
        let (h2, _) = w.step(&buf).expect("resynced record");
        assert_eq!(h2.seq, 5);
    }

    #[test]
    fn advise_plan_waits_until_a_full_chunk_has_accumulated() {
        let (upto, range) = advise_plan(DATA_START, DATA_START + ADVISE_CHUNK - 1, RING_LEN);
        assert_eq!(upto, DATA_START);
        assert_eq!(range, None);
    }

    #[test]
    fn advise_plan_fires_once_a_chunk_is_crossed() {
        let next = DATA_START + ADVISE_CHUNK;
        let (upto, range) = advise_plan(DATA_START, next, RING_LEN);
        assert_eq!(upto, next);
        assert_eq!(range, Some((DATA_START, next)));
    }

    #[test]
    fn advise_plan_flushes_the_tail_on_wraparound() {
        let (upto, range) = advise_plan(RING_LEN - 100, DATA_START + 5, RING_LEN);
        assert_eq!(upto, DATA_START);
        assert_eq!(range, Some((RING_LEN - 100, RING_LEN)));
    }

    fn frame(pts_us: u32, keyframe: bool) -> Frame {
        Frame { pts_us, keyframe, data: vec![] }
    }

    fn new_subscriber(cap: usize) -> Subscriber {
        Subscriber { id: 0, peer: "test:0".into(), queue: Mutex::new(VecDeque::new()), changed: Condvar::new(), cap }
    }

    fn queue_pts(sub: &Subscriber) -> Vec<u32> {
        sub.queue.lock().unwrap().iter().map(|f| f.pts_us).collect()
    }

    #[test]
    fn subscriber_queue_drops_oldest_interframe_and_keeps_the_keyframe_when_full() {
        let sub = new_subscriber(3);
        sub.push(frame(1, true));
        sub.push(frame(2, false));
        sub.push(frame(3, false)); // full: [kf1, i2, i3]
        sub.push(frame(4, false)); // must drop the oldest *interframe* (i2), not the keyframe
        assert_eq!(queue_pts(&sub), vec![1, 3, 4]);
        assert!(sub.queue.lock().unwrap().front().unwrap().keyframe);
    }

    #[test]
    fn subscriber_queue_drops_true_oldest_when_no_keyframe_is_buffered() {
        let sub = new_subscriber(2);
        sub.push(frame(1, false));
        sub.push(frame(2, false)); // full: [1, 2]
        sub.push(frame(3, false)); // no keyframe to protect -> drop true oldest
        assert_eq!(queue_pts(&sub), vec![2, 3]);
    }

    #[test]
    fn subscriber_queue_clears_and_restarts_from_a_fresh_keyframe_under_pressure() {
        let sub = new_subscriber(2);
        sub.push(frame(1, true));
        sub.push(frame(2, false)); // full: [kf1, i2]
        // A fresh keyframe arriving while full must not leave i2 (which decodes against kf1)
        // queued behind a dropped kf1 -- that would hand the client an undecodable frame. The
        // whole queue is cleared and restarted from this keyframe instead.
        sub.push(frame(3, true));
        assert_eq!(queue_pts(&sub), vec![3]);
        assert!(sub.queue.lock().unwrap().front().unwrap().keyframe);
    }

    #[test]
    fn subscriber_queue_does_not_evict_anything_while_under_capacity() {
        let sub = new_subscriber(3);
        sub.push(frame(1, true));
        sub.push(frame(2, false));
        sub.push(frame(3, true)); // reaches cap exactly via ordinary pushes, nothing to evict
        assert_eq!(queue_pts(&sub), vec![1, 2, 3], "room was available; nothing needed to be dropped");
    }

    #[test]
    fn subscriber_recv_returns_none_on_timeout_with_an_empty_queue() {
        let sub = new_subscriber(3);
        assert!(sub.recv(Duration::from_millis(5)).is_none());
    }

    #[test]
    fn subscriber_recv_drains_in_fifo_order() {
        let sub = new_subscriber(3);
        sub.push(frame(1, true));
        sub.push(frame(2, false));
        assert_eq!(sub.recv(Duration::from_millis(10)).unwrap().pts_us, 1);
        assert_eq!(sub.recv(Duration::from_millis(10)).unwrap().pts_us, 2);
        assert!(sub.recv(Duration::from_millis(5)).is_none());
    }

    #[test]
    fn feed_subscribe_refuses_past_the_session_cap() {
        let feed = VideoFeed::new();
        let (a, n1) = feed.subscribe("peer-a:1".into(), 2, 3).expect("first session admitted");
        assert_eq!(n1, 1);
        let (b, n2) = feed.subscribe("peer-b:2".into(), 2, 3).expect("second session admitted");
        assert_eq!(n2, 2, "second session is the spare slot");
        assert!(feed.subscribe("peer-c:3".into(), 2, 3).is_none(), "third session must be refused, not hang");
        drop(a);
        let (_c, n3) = feed.subscribe("peer-c:3".into(), 2, 3).expect("dropping a session frees its slot");
        assert_eq!(n3, 2);
        drop(b);
    }

    #[test]
    fn feed_subscribe_seeds_the_new_session_with_the_latest_keyframe() {
        let feed = VideoFeed::new();
        feed.publish(frame(1, false), 1152, 720); // no keyframe yet
        feed.publish(frame(2, true), 1152, 720);
        let (sub, _) = feed.subscribe("peer:1".into(), 2, 3).expect("admitted");
        let first = sub.recv(Duration::from_millis(10)).expect("seeded frame");
        assert!(first.keyframe);
        assert_eq!(first.pts_us, 2);
    }

    #[test]
    fn feed_publish_fans_out_to_every_subscriber_independently() {
        let feed = VideoFeed::new();
        let (a, _) = feed.subscribe("peer-a:1".into(), 2, 3).unwrap();
        let (b, _) = feed.subscribe("peer-b:2".into(), 2, 3).unwrap();
        feed.publish(frame(5, true), 1728, 1080);
        assert_eq!(a.recv(Duration::from_millis(10)).unwrap().pts_us, 5);
        assert_eq!(b.recv(Duration::from_millis(10)).unwrap().pts_us, 5);
    }

    #[test]
    fn feed_snapshot_reports_resolution_and_connected_peers() {
        let feed = VideoFeed::new();
        feed.publish(frame(1, true), 1152, 720);
        let (_a, _) = feed.subscribe("10.0.0.5:4001".into(), 2, 3).unwrap();
        let (_b, _) = feed.subscribe("10.0.0.9:55000".into(), 2, 3).unwrap();
        let snap = feed.snapshot();
        assert_eq!((snap.width, snap.height), (1152, 720));
        assert_eq!(snap.sessions, vec!["10.0.0.5:4001".to_string(), "10.0.0.9:55000".to_string()]);
    }

    #[test]
    fn feed_snapshot_drops_peers_whose_session_ended() {
        let feed = VideoFeed::new();
        let (a, _) = feed.subscribe("gone:1".into(), 2, 3).unwrap();
        drop(a);
        assert!(feed.snapshot().sessions.is_empty());
    }

    #[test]
    fn fps_window_reports_nothing_until_a_full_window_elapses() {
        let t0 = Instant::now();
        let mut w = FpsWindow::new(t0);
        for i in 1..=10u32 {
            w.tick(t0 + Duration::from_millis(40 * i as u64)); // 25fps cadence, 400ms total
        }
        assert_eq!(w.fps, 0.0, "the 2s window hasn't elapsed yet");
    }

    #[test]
    fn fps_window_computes_rate_once_the_window_elapses() {
        let t0 = Instant::now();
        let mut w = FpsWindow::new(t0);
        // 25fps cadence (40ms/frame) for 2.0s -> 50 ticks, the 50th lands exactly on the window.
        for i in 1..=50u32 {
            w.tick(t0 + Duration::from_millis(40 * i as u64));
        }
        assert!((w.fps - 25.0).abs() < 0.5, "expected ~25fps, got {}", w.fps);
    }
}
