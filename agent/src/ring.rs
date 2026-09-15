//! Read-only access to the vendor `media` process's video/audio frame ring.
//!
//! `/dev/shm/media_buffer_frame_buf` is an 8,389,608-byte POSIX shm segment: the first
//! `DATA_START` (1024) bytes are a reader-registration table we don't touch (byte density jumps
//! from ~20-32% nonzero to ~99-100% exactly at that offset -- registration fields versus
//! compressed bitstream), then a true byte-continuous circular buffer of records -- a 56-byte
//! header immediately followed by `length` bytes of H.264 Annex-B payload, back-to-back with zero
//! padding. A keyframe record is one access unit bundling SPS+PPS+IDR; an interframe record
//! carries one P-slice. `chan` 8 is the 1152x720@25fps "sub" stream this module serves.
//!
//! There is no new-frame signal (the vendor creates `sem.media_buffer_reader_6` but never posts
//! to it), so this is a poller: a background thread walks the ring, validates each record (sane
//! length, known channel, global sequence exactly previous+1), and republishes new sub-channel
//! records into a `VideoFeed` the RTSP server reads from. On any validation failure it resyncs by
//! scanning forward for the next Annex-B start code.
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

use std::fs::File;
use std::io;
use std::os::raw::{c_int, c_void};
use std::os::unix::io::AsRawFd;
use std::slice;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

pub const RING_PATH: &str = "/dev/shm/media_buffer_frame_buf";
const RING_LEN: usize = 8_389_608;
const DATA_START: usize = 1024;
const HDR: usize = 56;

/// Channel field values (record header offset 34).
const CHAN_MAIN: u8 = 4;
pub const CHAN_SUB: u8 = 8;
const CHAN_THUMB: u8 = 16;
const CHAN_AUDIO: u8 = 1;

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
/// continuity with whatever a caller read before it -- `Walker` tracks that.
struct Header {
    seq: u32,
    length: u32,
    pts_us: u32,
    frame_type: u8,
    chan: u8,
}

/// Parse and structurally validate a record header from `buf[off..]`: in-bounds header, sane
/// payload length, known channel, and the whole record (header plus payload) fitting inside
/// `buf` without needing bytes at or past `buf.len()`. Pure function of a byte slice so it's
/// testable without a real mapping.
fn parse_header(buf: &[u8], off: usize) -> Option<Header> {
    if off.checked_add(HDR)? > buf.len() {
        return None;
    }
    let b = &buf[off..off + HDR];
    let seq = u32::from_le_bytes(b[0..4].try_into().unwrap());
    let length = u32::from_le_bytes(b[4..8].try_into().unwrap());
    let pts_us = u32::from_le_bytes(b[16..20].try_into().unwrap());
    let frame_type = b[32];
    let chan = b[34];
    if !(1..=2_000_000).contains(&length) {
        return None;
    }
    if !matches!(chan, CHAN_AUDIO | CHAN_MAIN | CHAN_SUB | CHAN_THUMB) {
        return None;
    }
    if off + HDR + length as usize > buf.len() {
        return None;
    }
    Some(Header { seq, length, pts_us, frame_type, chan })
}

/// Scan forward from `from` for the next byte offset that starts with an Annex-B start code
/// (`00 00 00 01`) *and* whose implied header (`HDR` bytes back) passes `parse_header`. Wraps
/// once -- `[from, buf.len())` then `[DATA_START, from)` -- so it always terminates within one
/// lap of the ring.
fn find_next_header(buf: &[u8], from: usize) -> Option<usize> {
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

/// One access unit from the sub channel: exactly the bytes stored in the record's payload
/// (Annex-B, start-code prefixed), copied out of the mmap so it can outlive the poller's next
/// step.
#[derive(Clone)]
pub struct Frame {
    pub pts_us: u32,
    pub keyframe: bool,
    pub data: Vec<u8>,
}

struct FeedInner {
    generation: u64,
    latest: Option<Frame>,
    latest_keyframe: Option<Frame>,
}

/// Hand-off point between the ring-poller thread and the RTSP server thread. Deliberately
/// "latest frame wins": if the RTSP thread is busy writing to a slow client when two new frames
/// arrive, it sees only the second on its next check. For a live view that's the right trade --
/// bounded memory, never blocks the poller, at worst skips a P-frame under backpressure.
pub struct VideoFeed {
    inner: Mutex<FeedInner>,
    changed: Condvar,
}

impl VideoFeed {
    pub fn new() -> Arc<VideoFeed> {
        Arc::new(VideoFeed {
            inner: Mutex::new(FeedInner { generation: 0, latest: None, latest_keyframe: None }),
            changed: Condvar::new(),
        })
    }

    fn publish(&self, frame: Frame) {
        let mut inner = self.inner.lock().unwrap();
        if frame.keyframe {
            inner.latest_keyframe = Some(frame.clone());
        }
        inner.latest = Some(frame);
        inner.generation = inner.generation.wrapping_add(1);
        self.changed.notify_all();
    }

    /// The most recently seen keyframe, if any, plus the generation it was read at (so a caller
    /// can seed `wait_next`'s `last_seen` without racing a frame published between two separate
    /// calls). Used to start a newly-connected RTSP client immediately instead of making it wait
    /// out the ~4 s GOP for the next one.
    pub fn latest_keyframe(&self) -> (u64, Option<Frame>) {
        let inner = self.inner.lock().unwrap();
        (inner.generation, inner.latest_keyframe.clone())
    }

    /// Block up to `timeout` for a frame newer than `last_seen`, updating `last_seen` and
    /// returning it if one arrives. `None` on timeout with nothing new -- callers use that to go
    /// check their socket for incoming client requests (GET_PARAMETER, TEARDOWN) without a
    /// dedicated thread per connection.
    pub fn wait_next(&self, last_seen: &mut u64, timeout: Duration) -> Option<Frame> {
        let inner = self.inner.lock().unwrap();
        let (inner, _) = self
            .changed
            .wait_timeout_while(inner, timeout, |i| i.generation == *last_seen)
            .unwrap();
        if inner.generation != *last_seen {
            *last_seen = inner.generation;
            inner.latest.clone()
        } else {
            None
        }
    }
}

fn poll_loop(ring: Ring, feed: Arc<VideoFeed>) {
    let mut w = Walker::new();
    w.seed(ring.as_bytes());
    loop {
        let mut made_progress = false;
        while let Some((h, payload_off)) = w.step(ring.as_bytes()) {
            made_progress = true;
            if h.chan == CHAN_SUB {
                let data = ring.as_bytes()[payload_off..payload_off + h.length as usize].to_vec();
                feed.publish(Frame { pts_us: h.pts_us, keyframe: h.frame_type == FRAME_KEYFRAME, data });
            }
        }
        w.maybe_advise(&ring);
        if !made_progress {
            thread::sleep(POLL_SLEEP);
        }
    }
}

/// Open the ring and spawn the background poller thread that feeds `feed`. The only fallible
/// step is the initial open (bad path, too-small file, mmap failure); the poll loop itself never
/// stops on its own.
pub fn spawn(feed: Arc<VideoFeed>) -> io::Result<thread::JoinHandle<()>> {
    let ring = Ring::open()?;
    Ok(thread::spawn(move || poll_loop(ring, feed)))
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
        let h = parse_header(&buf, DATA_START).expect("valid header");
        assert_eq!(h.seq, 7);
        assert_eq!(h.chan, CHAN_SUB);
        assert_eq!(h.frame_type, 1);
        assert_eq!(h.length as usize, b"\x00\x00\x00\x01payload".len());
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
}
