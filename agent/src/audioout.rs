//! Speaker output: encodes PCM to the same AAC-LC/16kHz/mono ADTS format the mic uses (`adts.rs`,
//! `docs/23-audio-codec.md`) and paces it onto the ring's `CHAN_AUDIO_OUT` slot -- the same
//! physical protocol `agora` already uses for live app talkback, confirmed live this project
//! (every one of 548/548 captured chan=2 records decoded as valid ADTS AAC-LC/16kHz/mono).
//!
//! ## Why AAC, not raw PCM
//!
//! Two candidate speaker paths existed on paper: write AAC to the ring the way `agora` does, or
//! reach some more direct raw-PCM hand-off. The second is not available: the live capture above
//! settles it directly (the ring slot itself carries AAC, not PCM -- there is no separate PCM
//! entry point anywhere in `media`'s own strings, `docs/23-audio-codec.md`), independently
//! corroborated by static disassembly (every `AX_AO_SendFrame` call site in `media` sits directly
//! beside an `AX_ADEC` AAC-decode call, never a bare PCM hand-off) -- whatever reads
//! `CHAN_AUDIO_OUT` decodes AAC itself before the hardware ever sees PCM. So encoding is
//! mandatory, done via a small statically-linked helper subprocess (`tools/aacenc/`, built from
//! fdk-aac's own open source) because `kibbled` is a fully static musl binary that cannot
//! `dlopen` the device's own `libfdk-aac.so.2.0.1` (confirmed empirically: a minimal static test
//! binary built with this project's own `+crt-static` pipeline printed "Dynamic loading not
//! supported"), and the cross toolchain's glibc (2.41) is 16 releases newer than the device's
//! (2.25) -- too far apart to dynamically link a new glibc binary against the device's copy either.
//!
//! ## Writer-side ring synchronization -- explicit, not glossed over
//!
//! There is no known, safely-reverse-engineerable atomic claim primitive for this ring's write
//! side: every research session that looked for one (docs/11-media.md §4, docs/19-frame-ring.md
//! §1) explicitly flagged the registry's slot-0 "global header" semantics as unrecovered. What
//! *is* established: the ring is a genuine byte-continuous circular buffer, every reader already
//! tolerates a torn/garbled record by construction (sane length + known channel + global-sequence
//! continuity, rescan-on-failure -- `ring.rs`), and `agora` is proof a *second*, independent
//! process can already write into this exact ring without bringing the system down.
//!
//! This module's writer does a "locate the current tail, write immediately after it" append (see
//! `find_append_target`), racing `media`'s/`agora`'s own concurrent writes on the rare chance one
//! lands at the same instant. A collision has the same blast radius as any other torn write the
//! ring already tolerates by construction: the record involved is dropped/garbled and the very
//! next poll tick recovers via the existing resync path. It is not retried (there is no way to
//! detect a collision after the fact from the writer side) -- correctness here is instead verified
//! end-to-end by reading the ring back with the `rt5`/`ringtool2` probe (`docs/23-audio-codec.md`).
//!
//! ## Two producers, one speaker
//!
//! Two distinct risks share the name "two producers": (1) two *kibbled-internal* callers writing
//! at once (a live backchannel session and a `/speak` call, say) -- prevented outright by
//! [`SpeakerOwner`], a single in-process exclusive lock every writer (this module, `backchannel.rs`)
//! must hold for its whole session; (2) the *vendor's own* `agora` writing during a live app
//! talkback call at the same time `kibbled` tries to -- detected via [`call_active`], which reads
//! `/proc/ax_proc/aenc` (confirmed live: prints only its 3-line version banner when idle, and
//! `docs/23-audio-codec.md` independently flagged this exact file as the intended talk-session
//! signal) and treats anything other than that exact idle banner -- including a read error -- as
//! "a call might be active", refusing rather than risking corruption (a false positive only delays
//! an announcement; a false negative corrupts two streams at once). Checked once before a session
//! starts ([`SpeakerOwner::try_acquire`]) and again before every single frame during playback, so
//! a call that starts mid-clip aborts the clip rather than interleaving with it.

use std::fs::File;
use std::io::{self, Read, Write};
use std::ops::ControlFlow;
use std::os::raw::{c_int, c_long, c_void};
use std::os::unix::fs::FileExt;
use std::os::unix::io::AsRawFd;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, LazyLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::adts;
use crate::bus;
use crate::ring::{self, TailCursor};

/// Where `tools/aacenc/build.sh` deploys its output, alongside `kibbled` itself.
pub const AACENC_PATH: &str = "/opt/kibble/aacenc";

/// One AAC-LC access unit's sample count at 16kHz -- the encoder's own fixed frame length
/// (`docs/23-audio-codec.md` §2.1: `AACENC_GRANULE_LENGTH` is never overridden, so it stays at
/// FDK's 1024-sample default), and the ring's own real-time cadence unit.
pub const FRAME_SAMPLES: usize = 1024;
const FRAME_BYTES: usize = FRAME_SAMPLES * 2; // 16-bit mono

/// 1024 samples / 16000 Hz -- the real-time duration of one frame, and the ring write pacing
/// interval. Both the vendor's own encoder and the acceptance test (docs/23-audio-codec.md) treat
/// this as exact, not approximate.
pub const FRAME_DURATION: Duration = Duration::from_micros(1024 * 1_000_000 / 16_000);

/// RMS (on a signed 16-bit full scale of 32767) every `/speak`/clip PCM buffer is normalized to
/// before encoding, measured this project from `/audio/en/en_feed_start.aac` (one of the vendor's
/// own canned prompts) decoded off-device via ffmpeg -- matches a Kibble announcement's loudness
/// to the vendor's own prompts rather than whatever gain the uploader happened to record at.
const TARGET_RMS: f64 = 1532.0;
/// Never amplify by more than this, so a near-silent or empty clip doesn't get blown up into pure
/// noise chasing an unreachable target RMS.
const MAX_GAIN: f64 = 20.0;

const CALL_ACTIVE_PROC: &str = "/proc/ax_proc/aenc";
/// The exact idle-state line count of `/proc/ax_proc/aenc` (`docs/23-audio-codec.md` §2, and
/// independently re-confirmed live in this project): just the driver's own version banner, three
/// lines, when no `AX_AENC` channel is open. Petkit's talkback goes through a separate, custom
/// in-`media` encoder path that never touches `AX_AENC` (§2), so this specifically detects the
/// *transient uplink* channel a live app call opens -- not the mic's own always-on ring capture,
/// which never shows up here either way.
const IDLE_BANNER_LINES: usize = 3;

/// How far past a possibly-stale [`TailCursor`] snapshot to read looking for records the
/// background poller hasn't observed yet. Generous relative to the handful of records (typically
/// zero to two) that land in one `POLL_SLEEP` tick even under the ring's busiest observed load
/// (three simultaneous video channels plus mic audio) -- see the module doc for why this
/// "re-derive immediately before writing" step exists at all instead of trusting the snapshot
/// directly.
const CATCH_UP_WINDOW: usize = 128 * 1024;
/// Defensive bound on the catch-up walk's iteration count -- large enough that legitimate traffic
/// never comes close, small enough that a logic bug can't spin forever instead of erroring out.
const CATCH_UP_MAX_STEPS: usize = 4096;

/// `true` if the vendor's transient talk-session uplink channel looks active (or its state can't
/// be confirmed). Deliberately fails closed: a false positive only delays an announcement, a
/// false negative would interleave two writers on one ring slot.
pub fn call_active() -> bool {
    match std::fs::read_to_string(CALL_ACTIVE_PROC) {
        Ok(text) => {
            let lines: Vec<&str> = text.lines().collect();
            !(lines.len() == IDLE_BANNER_LINES
                && lines.first().is_some_and(|l| l.starts_with("--------")))
        }
        Err(_) => true,
    }
}

/// Exclusive in-process ownership of the speaker ring slot: `backchannel.rs`'s live session and
/// this module's `/speak`/clip-play both write `CHAN_AUDIO_OUT`, and must never do so at the same
/// time (see module doc, "Two producers, one speaker"). One `AtomicBool`, not a `Mutex`: ownership
/// is held across a whole playback session (seconds to minutes) typically by a different thread
/// than the one that acquired it -- [`OwnerGuard`] releases it on `Drop` regardless of which
/// thread drops it.
pub struct SpeakerOwner(AtomicBool);

impl SpeakerOwner {
    pub fn new() -> Arc<Self> {
        // Best-effort, non-fatal: clears a guard flag a previous kibbled crash may have left
        // stuck at `1` (docs/23-audio-codec.md §17.4 -- nothing else ever clears it, and
        // stopping when nothing is running is itself a harmless, logged no-op on the vendor
        // side). Runs exactly once, here, so `main.rs` doesn't need its own startup hook.
        AudioOutThread::clear_stale_guard_flag();
        Arc::new(Self(AtomicBool::new(false)))
    }

    /// Claims exclusive ownership, or returns the reason it couldn't: another kibbled-internal
    /// session already holds it, or the vendor's own uplink looks active.
    pub fn try_acquire(self: &Arc<Self>) -> Result<OwnerGuard, &'static str> {
        if call_active() {
            return Err("a live app talk session appears active (/proc/ax_proc/aenc)");
        }
        if self.0.swap(true, Ordering::AcqRel) {
            return Err("the speaker is already in use by another kibbled session");
        }
        Ok(OwnerGuard(Arc::clone(self)))
    }
}

pub struct OwnerGuard(Arc<SpeakerOwner>);

impl Drop for OwnerGuard {
    fn drop(&mut self) {
        self.0 .0.store(false, Ordering::Release);
    }
}

/// RAII guard for the vendor's `audio_out_thread` lifecycle (docs/23-audio-codec.md §17): sends
/// `speak_start` on acquisition and `speak_stop` on `Drop`, unconditionally, regardless of which
/// thread drops it or whether playback succeeded. `speak_stop` does not itself stop the vendor
/// thread -- it exits on its own idle timeout regardless, ~5s (§17.5) -- but is required to keep
/// its guard flag truthful for the *next* `speak_start`: kibbled's own, or (architecture-
/// consistent, not independently proven -- §17.6) a real Petkit-app pet-call talkback sharing the
/// same handler. Every construction path pairs with exactly one `Drop`; never sent standalone.
struct AudioOutThread;

/// `src` we stamp on `speak_start`/`speak_stop` -- neither handler reads it (disassembly-
/// confirmed, §17.1/§17.4), so this only matters for matching the rest of the project's existing
/// "we speak on ctrl's behalf" convention (`main.rs`'s own `SRC_AS_CTRL`).
const BUS_SRC: u16 = bus::Peer::Ctrl as u16;

impl AudioOutThread {
    /// Sends `speak_start` (msg `0xa`, empty payload -- §17.7). Idempotent server-side: §17.1
    /// disassembled both of `speak_start`'s "already started" return paths, both a bare `-1`
    /// with zero side effects, so calling this while the thread is already running is harmless.
    fn start() -> Result<Self, SpeakError> {
        send(bus::msg::SPEAK_START)?;
        Ok(Self)
    }

    fn clear_stale_guard_flag() {
        if let Err(e) = send(bus::msg::SPEAK_STOP) {
            eprintln!("kibbled: startup speak_stop: {e}");
        }
    }
}

impl Drop for AudioOutThread {
    fn drop(&mut self) {
        if let Err(e) = send(bus::msg::SPEAK_STOP) {
            eprintln!("kibbled: speak_stop on drop: {e}");
        }
    }
}

fn send(msg_id: u16) -> Result<(), SpeakError> {
    bus::Sender::open(bus::Peer::Media, BUS_SRC)
        .and_then(|s| s.send(msg_id, &[]))
        .map_err(|e| SpeakError::Bus(e.to_string()))
}

/// Errors from a speaker-write attempt, surfaced to HTTP callers via `main.rs`.
#[derive(Debug)]
pub enum SpeakError {
    Encoder(String),
    Ring(String),
    Bus(String),
}

impl std::fmt::Display for SpeakError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SpeakError::Encoder(e) => write!(f, "encoder: {e}"),
            SpeakError::Ring(e) => write!(f, "ring write: {e}"),
            SpeakError::Bus(e) => write!(f, "audio_out_thread: {e}"),
        }
    }
}

/// What a completed (or aborted) playback actually did -- logged by `main.rs` and reported by the
/// acceptance test (docs/23-audio-codec.md).
#[derive(Debug, Default, Clone, Copy)]
pub struct PlaybackStats {
    pub frames_written: u64,
    pub aborted_call_active: bool,
}

/// Scales `pcm` in place so its RMS matches [`TARGET_RMS`], clamping any sample that would clip
/// and capping the gain itself at [`MAX_GAIN`] (silence or near-silence has no sane gain to reach
/// the target, so it's left alone rather than amplified into noise).
pub fn normalize_rms(pcm: &mut [i16]) {
    if pcm.is_empty() {
        return;
    }
    let sum_sq: f64 = pcm.iter().map(|&s| f64::from(s) * f64::from(s)).sum();
    let rms = (sum_sq / pcm.len() as f64).sqrt();
    if rms < 1.0 {
        return; // effectively silent; nothing sane to normalize toward
    }
    let gain = (TARGET_RMS / rms).min(MAX_GAIN);
    for s in pcm.iter_mut() {
        *s = (f64::from(*s) * gain).clamp(f64::from(i16::MIN), f64::from(i16::MAX)) as i16;
    }
}

/// Normalizes and encodes `pcm` for storage (`PUT /clips/<name>`) or immediate playback
/// (`POST /speak`): concatenated ADTS access units, ready to write straight to a clip file or
/// pace onto the ring via [`play_encoded`]. Spawns and fully drains the `aacenc` helper; blocks
/// the calling thread for the encode's duration only (well under a second for any realistic
/// clip length -- `tools/aacenc/`'s own validation), not for real-time playback.
pub fn normalize_and_encode(pcm: &[i16]) -> Result<Vec<u8>, SpeakError> {
    let mut pcm = pcm.to_vec();
    normalize_rms(&mut pcm);
    let frames = encode(&pcm)?;
    Ok(frames.concat())
}

/// Plays back already-encoded ADTS bytes (a stored clip via `clips::load`, or the immediate
/// result of [`normalize_and_encode`]) by splitting them back into individual access units and
/// pacing them onto the ring at real-time cadence. Blocks the calling thread for the clip's full
/// real-time duration -- callers that must not block their own request thread (`main.rs`'s
/// `/speak` and `/clips/<name>/play` handlers) run this on a spawned thread and return once
/// [`SpeakerOwner::try_acquire`] alone has succeeded.
pub fn play_encoded(
    adts_bytes: &[u8],
    tail: &Arc<TailCursor>,
    _owner: &OwnerGuard,
) -> Result<PlaybackStats, SpeakError> {
    // Held until this function returns; `Drop` sends `speak_stop` regardless of how we exit.
    let _audio_thread = AudioOutThread::start()?;
    let mut frames = Vec::new();
    let mut off = 0;
    while let Some(header) = adts::parse(&adts_bytes[off..]) {
        let len = header.frame_length as usize;
        if len == 0 || off + len > adts_bytes.len() {
            break;
        }
        frames.push(&adts_bytes[off..off + len]);
        off += len;
    }
    let ring_file = open_ring_for_write()?;
    let start = Instant::now();
    let mut stats = PlaybackStats::default();
    for (i, frame) in frames.into_iter().enumerate() {
        match pace_one(start, i as u32, &ring_file, tail, frame, &mut stats)? {
            ControlFlow::Continue(()) => {}
            ControlFlow::Break(()) => break,
        }
    }
    Ok(stats)
}

/// A live, incrementally-fed speaker session for the RTSP backchannel (`backchannel.rs`): unlike
/// [`play_encoded`] (a short clip, fully known upfront), a backchannel call is open-ended and
/// arrives as a live RTP stream, so encoding and ring-writing happen concurrently with the caller
/// still feeding new PCM in. One background thread drains the encoder's stdout, paces, and writes
/// each frame as it becomes available; `feed` just writes PCM straight to the encoder's stdin --
/// `tools/aacenc/wrapper.c` itself accumulates arbitrary-sized writes up to its own fixed
/// 1024-sample frame internally, so callers don't need to align to that boundary themselves.
pub struct LiveSession {
    stdin: Option<ChildStdin>,
    child: Child,
    drainer: Option<thread::JoinHandle<Result<PlaybackStats, SpeakError>>>,
    _owner: OwnerGuard,
    // Held for the whole session; `Drop` sends `speak_stop` when this session ends, however.
    _audio_thread: AudioOutThread,
}

impl LiveSession {
    pub fn start(tail: Arc<TailCursor>, owner: OwnerGuard) -> Result<Self, SpeakError> {
        let audio_thread = AudioOutThread::start()?;
        let ring_file = open_ring_for_write()?;
        let mut child = spawn_encoder()?;
        let stdin = child.stdin.take().expect("piped stdin");
        let mut stdout = child.stdout.take().expect("piped stdout");
        let drainer = thread::spawn(move || -> Result<PlaybackStats, SpeakError> {
            let start = Instant::now();
            let mut stats = PlaybackStats::default();
            let mut buf = Vec::new();
            let mut chunk = [0u8; 8192];
            let mut frame_index: u32 = 0;
            loop {
                match next_adts_frame(&mut stdout, &mut buf, &mut chunk) {
                    Ok(None) => return Ok(stats),
                    Ok(Some(frame)) => {
                        match pace_one(start, frame_index, &ring_file, &tail, &frame, &mut stats)? {
                            ControlFlow::Continue(()) => frame_index += 1,
                            ControlFlow::Break(()) => return Ok(stats),
                        }
                    }
                    Err(e) => return Err(SpeakError::Encoder(e.to_string())),
                }
            }
        });
        Ok(Self {
            stdin: Some(stdin),
            child,
            drainer: Some(drainer),
            _owner: owner,
            _audio_thread: audio_thread,
        })
    }

    /// Feeds one arbitrary-length slice of 16kHz/mono PCM (`backchannel.rs` has already decoded
    /// and upsampled G.711 to this rate before calling in).
    pub fn feed(&mut self, pcm: &[i16]) -> io::Result<()> {
        let mut bytes = Vec::with_capacity(pcm.len() * 2);
        for s in pcm {
            bytes.extend_from_slice(&s.to_le_bytes());
        }
        self.stdin.as_mut().expect("session already finished").write_all(&bytes)
    }

    /// Closes the encoder's stdin (flushing its remaining lookahead) and waits for the drainer
    /// thread to finish pacing out whatever frames that produced. Consumes `self` -- a
    /// `LiveSession` is only good for one call to `finish` (or none, if dropped instead).
    pub fn finish(mut self) -> Result<PlaybackStats, SpeakError> {
        self.stdin.take(); // EOF
        let result = self
            .drainer
            .take()
            .expect("finish called once")
            .join()
            .unwrap_or_else(|_| Err(SpeakError::Encoder("drainer thread panicked".into())));
        let _ = self.child.wait();
        result
    }
}

impl Drop for LiveSession {
    fn drop(&mut self) {
        self.stdin.take(); // close if `finish` wasn't called
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(h) = self.drainer.take() {
            let _ = h.join();
        }
    }
}

/// Sleeps (if needed) until `frame_index`'s scheduled real-time slot relative to `start`, then
/// checks the vendor call-active signal and writes one frame if clear. Shared by both the
/// finite-clip pacing loop ([`play_encoded`]) and the live backchannel session's drainer
/// ([`LiveSession`]) so the two can't silently drift out of sync on what "paced" means.
fn pace_one(
    start: Instant,
    frame_index: u32,
    ring_file: &File,
    tail: &TailCursor,
    frame: &[u8],
    stats: &mut PlaybackStats,
) -> Result<ControlFlow<()>, SpeakError> {
    if call_active() {
        stats.aborted_call_active = true;
        return Ok(ControlFlow::Break(()));
    }
    let deadline = start + FRAME_DURATION * frame_index;
    let now = Instant::now();
    if deadline > now {
        thread::sleep(deadline - now);
    }
    match write_frame(ring_file, tail, frame) {
        Ok(true) => stats.frames_written += 1,
        Ok(false) => {} // dropped at the wrap seam; self-healing, see module doc
        Err(e) => return Err(SpeakError::Ring(e)),
    }
    Ok(ControlFlow::Continue(()))
}

fn spawn_encoder() -> Result<Child, SpeakError> {
    Command::new(AACENC_PATH)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| SpeakError::Encoder(format!("spawn {AACENC_PATH}: {e}")))
}

/// Runs `pcm` through the `aacenc` helper subprocess to completion, returning every ADTS access
/// unit it produced, in order.
fn encode(pcm: &[i16]) -> Result<Vec<Vec<u8>>, SpeakError> {
    let mut child = spawn_encoder()?;
    let mut stdin = child.stdin.take().expect("piped stdin");
    let mut stdout = child.stdout.take().expect("piped stdout");

    let chunks: Vec<[u8; FRAME_BYTES]> = pcm.chunks(FRAME_SAMPLES).map(pack_chunk).collect();
    let feeder = thread::spawn(move || -> io::Result<()> {
        for chunk in &chunks {
            stdin.write_all(chunk)?;
        }
        Ok(()) // stdin drops at closure end -> EOF, lets the encoder flush its lookahead
    });

    let mut frames = Vec::new();
    let mut buf = Vec::new();
    let mut chunk = [0u8; 8192];
    let read_result: io::Result<()> = (|| {
        while let Some(frame) = next_adts_frame(&mut stdout, &mut buf, &mut chunk)? {
            frames.push(frame);
        }
        Ok(())
    })();

    if let Err(e) = feeder.join().unwrap_or(Ok(())) {
        eprintln!("kibbled: aacenc stdin feed: {e}");
    }
    let status = child.wait();
    read_result.map_err(|e| SpeakError::Encoder(e.to_string()))?;
    match status {
        Ok(s) if s.success() => Ok(frames),
        Ok(s) => Err(SpeakError::Encoder(format!("aacenc exited: {s}"))),
        Err(e) => Err(SpeakError::Encoder(e.to_string())),
    }
}

fn pack_chunk(samples: &[i16]) -> [u8; FRAME_BYTES] {
    let mut buf = [0u8; FRAME_BYTES];
    for (i, s) in samples.iter().enumerate() {
        buf[i * 2..i * 2 + 2].copy_from_slice(&s.to_le_bytes());
    }
    buf
}

/// Pulls the next complete ADTS frame out of the encoder's stdout, accumulating partial reads in
/// `buf` across calls (`chunk` is scratch space, reused to avoid a fresh allocation per read).
/// `Ok(None)` means the encoder closed stdout with nothing left buffered -- a clean end of
/// stream, not an error.
fn next_adts_frame(
    stdout: &mut ChildStdout,
    buf: &mut Vec<u8>,
    chunk: &mut [u8],
) -> io::Result<Option<Vec<u8>>> {
    loop {
        if let Some(header) = adts::parse(buf) {
            let total = header.frame_length as usize;
            if total > 0 && buf.len() >= total {
                let frame: Vec<u8> = buf.drain(..total).collect();
                return Ok(Some(frame));
            }
        }
        let n = stdout.read(chunk)?;
        if n == 0 {
            if buf.is_empty() {
                return Ok(None);
            }
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                format!("aacenc closed stdout with {} unparsed trailing byte(s)", buf.len()),
            ));
        }
        buf.extend_from_slice(&chunk[..n]);
    }
}

fn open_ring_for_write() -> Result<File, SpeakError> {
    std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(ring::RING_PATH)
        .map_err(|e| SpeakError::Ring(format!("open {}: {e}", ring::RING_PATH)))
}

struct AppendTarget {
    global_seq: u32,
    chan2_seq: u32,
}

/// Derives `chan2_seq` -- the per-channel bookkeeping [`publish`] cannot get any other way, since
/// the vendor's own publish protocol only manages the *global* sequence (`docs/23-audio-codec.md`
/// §17.10.2) -- by seeding from `tail`'s last snapshot and walking forward over any records the
/// background poller hasn't observed yet, re-reading fresh bytes at every step rather than
/// trusting a single stale buffer. `global_seq` is also returned but is now a stale, provisional
/// value: `publish` overwrites it with the *real* `ring_base->0x18`-derived sequence under the
/// lock (see `publish`'s own doc comment) before anything is written.
fn find_append_target(ring_file: &File, tail: &TailCursor) -> Result<AppendTarget, String> {
    let Some((mut pos, mut global_seq, mut chan2_seq)) = tail.snapshot() else {
        return Err("ring poller has not seeded yet".into());
    };
    for _ in 0..CATCH_UP_MAX_STEPS {
        let window_len = CATCH_UP_WINDOW.min(ring::RING_LEN.saturating_sub(pos));
        if window_len < ring::HDR {
            // Within one header's width of the physical end: never split a record across the
            // wrap (the reader side's handling of that is unconfirmed) -- restart the walk from
            // the top, exactly like every writer's own wraparound already does.
            pos = ring::DATA_START;
            continue;
        }
        let mut window = vec![0u8; window_len];
        ring_file.read_exact_at(&mut window, pos as u64).map_err(|e| e.to_string())?;
        match ring::parse_header(&window, 0) {
            None => return Ok(AppendTarget { global_seq, chan2_seq }),
            Some(h) => {
                if h.chan == ring::CHAN_AUDIO_OUT {
                    chan2_seq = h.chan_seq;
                }
                global_seq = h.seq;
                pos += ring::HDR + h.length as usize;
            }
        }
    }
    Err("catch-up walk did not converge".into())
}

/// Assembles one ring record (56-byte header + `payload`) matching the exact byte layout observed
/// on `agora`'s own live chan=2 writes (`docs/23-audio-codec.md`), not the video/mic convention at
/// the two offsets (20, 28) whose meaning is otherwise unconfirmed -- replicated as observed
/// rather than guessed.
fn build_record(target: &AppendTarget, payload: &[u8]) -> Vec<u8> {
    let mut rec = vec![0u8; ring::HDR + payload.len()];
    rec[0..4].copy_from_slice(&(target.global_seq + 1).to_le_bytes());
    rec[4..8].copy_from_slice(&(payload.len() as u32).to_le_bytes());
    rec[8..12].copy_from_slice(&(target.chan2_seq + 1).to_le_bytes());
    let wall = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    rec[12..16].copy_from_slice(&(wall.as_secs() as u32).to_le_bytes());
    rec[16..20].copy_from_slice(&monotonic_us().to_le_bytes());
    // offset 20: observed 0 on every live chan=2 capture, vs the constant 5 video/mic records use
    // at this offset -- replicated as observed; already zero from the initializer.
    // offset 24: not captured for chan=2 specifically; left zero (video's own convention here too).
    rec[28..32].copy_from_slice(&2u32.to_le_bytes()); // observed constant on every chan=2 record
    rec[32] = 0; // frame_type: audio
    rec[33] = 4; // media_class: audio
    rec[34] = ring::CHAN_AUDIO_OUT;
    rec[46..48].copy_from_slice(&16u16.to_le_bytes()); // bit depth
    rec[48..50].copy_from_slice(&16000u16.to_le_bytes()); // sample rate
    rec[ring::HDR..].copy_from_slice(payload);
    rec
}

/// Finds the current per-channel bookkeeping state (`find_append_target`'s only remaining job in
/// this path) and publishes one record there via the vendor's own real protocol ([`publish`]).
/// `Ok(false)` (not an error) means the record was dropped because it would have straddled the
/// ring's physical wrap seam -- self-healing, same as any other torn write the ring tolerates.
fn write_frame(ring_file: &File, tail: &TailCursor, adts_frame: &[u8]) -> Result<bool, String> {
    let target = find_append_target(ring_file, tail)?;
    let mut record = build_record(&target, adts_frame);
    publish(&mut record)
}

/// True origin of the ring's cursor-relative addressing (`ring_base + VENDOR_DATA_ORIGIN +
/// cursor`), confirmed by exact arithmetic rather than new disassembly: `docs/23-audio-codec.md`
/// §17.2's disassembly-confirmed segment size (`0x800000` exactly) plus this value equals
/// `ring.rs`'s own live-measured `RING_LEN` (8_389_608) to the byte: `0x800000 + 0x3e8 ==
/// 8_389_608`. This is *not* the same as `ring::DATA_START` (1024): that value is merely a safe
/// scan-start seed for the read side's self-resyncing walker (harmless to be 24 bytes off, since
/// `find_next_header` corrects for it), but is 24 bytes off the vendor's own byte-exact writer
/// addressing -- `docs/23-audio-codec.md` §17.8 flags this discrepancy explicitly and leaves it
/// unreconciled for "any future attempt" at touching `ring_base->0x28` directly. This is that
/// attempt; the arithmetic above is the reconciliation.
const VENDOR_DATA_ORIGIN: usize = 0x3e8;
/// Wrap modulus for `ring_base->0x28`'s cursor value (`docs/23-audio-codec.md` §17.2/§17.10.2):
/// the segment's real 8 MiB data capacity, distinct from `ring::RING_LEN` (the *file's* total
/// size, registry prefix included).
const VENDOR_WRAP_MODULUS: u32 = 0x0080_0000;

/// Byte offsets of registry "slot 0" fields, relative to the mmap base (`docs/23-audio-codec.md`
/// §17.2/§17.10.2) -- the same coordinate system `VENDOR_DATA_ORIGIN` uses.
const OFF_MUTEX: usize = 0x00;
const OFF_GLOBAL_SEQ: usize = 0x18;
const OFF_SECOND_COUNTER: usize = 0x1c;
const OFF_RUNNING_TOTAL: usize = 0x20;
const OFF_WRITE_CURSOR: usize = 0x28;

/// How long a publish attempt waits for the vendor's shared ring mutex before giving up.
/// Deliberately short relative to the vendor's own 1s fallback (`docs/23-audio-codec.md`
/// §17.10.2 step 2): kibbled has no real-time deadline pressure the way the video/mic encoder
/// path does, so there is no reason to wait as long, and every extra millisecond here is a
/// millisecond a wedged or merely slow holder could be masked instead of surfaced as a normal,
/// retryable [`SpeakError`] (`docs/23-audio-codec.md` §17.11 step 2: "never indefinitely").
const LOCK_TIMEOUT: Duration = Duration::from_millis(200);

/// A *second*, `PROT_WRITE` mapping of the same shm segment `ring::Ring` maps read-only -- two
/// mappings of one file are ordinary, coherent POSIX behavior. Opened once and kept for the
/// process's lifetime (mirrors `ring::Ring`'s own "open at startup, never close" lifecycle), so
/// there is no per-publish mmap cost or failure mode in the hot path -- only the one-time setup
/// in [`writable_registry`] can fail.
struct WritableRegistry {
    base: *mut u8,
}

// Stable for the process's lifetime; every access is through the atomic/locked helpers below.
unsafe impl Send for WritableRegistry {}
unsafe impl Sync for WritableRegistry {}

impl WritableRegistry {
    /// A registry "slot 0" field (the mutex, or one of the `u32` counters) as an atomic
    /// reference, for the plain, non-private futex protocol in [`lock_ring_mutex`]/
    /// [`unlock_ring_mutex`] and the ordinary atomic loads/stores in [`publish`].
    fn field(&self, offset: usize) -> &AtomicU32 {
        unsafe { &*(self.base.add(offset) as *const AtomicU32) }
    }
}

fn writable_registry() -> Result<&'static WritableRegistry, SpeakError> {
    static REGISTRY: LazyLock<Result<WritableRegistry, String>> = LazyLock::new(|| {
        let f = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(ring::RING_PATH)
            .map_err(|e| format!("open {} for mmap: {e}", ring::RING_PATH))?;
        let len = f
            .metadata()
            .map_err(|e| format!("stat {}: {e}", ring::RING_PATH))?
            .len() as usize;
        if len < ring::RING_LEN {
            // Matches `ring::Ring::open`'s own pre-mmap size check exactly: never map more than
            // the file actually backs, so an access near the ring's own claimed capacity cannot
            // fault (SIGBUS) mid-lock -- the one failure mode this design cannot make safe by
            // construction, so it is refused here up front instead.
            return Err(format!(
                "{} is {len} bytes, expected at least {}",
                ring::RING_PATH,
                ring::RING_LEN
            ));
        }
        let p = unsafe {
            mmap(
                std::ptr::null_mut(),
                ring::RING_LEN,
                PROT_READ | PROT_WRITE,
                MAP_SHARED,
                f.as_raw_fd(),
                0,
            )
        };
        if p as isize == -1 {
            return Err(format!("mmap {}: {}", ring::RING_PATH, io::Error::last_os_error()));
        }
        Ok(WritableRegistry { base: p as *mut u8 })
    });
    REGISTRY.as_ref().map_err(|e| SpeakError::Ring(e.clone()))
}

/// Bounded-timeout acquire of the vendor's shared, `PTHREAD_PROCESS_SHARED`, non-robust mutex at
/// `ring_base+0x00` (`docs/23-audio-codec.md` §17.2/§17.10.1): the exact same 3-state (0 = free,
/// 1 = locked, 2 = locked-with-waiters) futex protocol glibc's own NPTL uses for this mutex kind
/// on this device (verified directly against glibc 2.25's `nptl`/`sysdeps/nptl` sources -- the
/// version this device runs -- not from memory). Deliberately **not** musl's own
/// `pthread_mutex_lock`/`_timedlock`: musl's mutex type/pshared detection reads a `_m_type`-style
/// field at a musl-specific struct offset that does not correspond to how glibc laid out this
/// already-initialized mutex, so calling musl's own function on it could silently decide the
/// mutex is process-*private* and take the private futex path -- no error, just zero actual
/// cross-process exclusion, which is exactly the corruption risk taking this lock exists to
/// prevent. Operating directly on the known-correct offset-0 word via raw, always-non-private
/// `futex(2)` calls (required for a `PTHREAD_PROCESS_SHARED` futex, and independent of either
/// libc's internal struct layout) sidesteps that ambiguity entirely. Never blocks past `timeout`.
fn lock_ring_mutex(word: &AtomicU32, timeout: Duration) -> Result<(), SpeakError> {
    if word.compare_exchange(0, 1, Ordering::Acquire, Ordering::Relaxed).is_ok() {
        return Ok(()); // uncontended fast path
    }
    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(SpeakError::Ring("ring mutex: timed out waiting for the lock".into()));
        }
        if word.swap(2, Ordering::Acquire) == 0 {
            return Ok(()); // acquired; word now reads 2, which is still correctly "locked"
        }
        let ts = Timespec {
            tv_sec: remaining.as_secs() as c_long,
            tv_nsec: remaining.subsec_nanos() as c_long,
        };
        // Sleep while the word is still 2. A real unlock's FUTEX_WAKE, a spurious wake, or our
        // own timeout all just fall through to re-check the loop's deadline/CAS above -- this
        // call's return value is deliberately never inspected.
        unsafe {
            syscall(
                SYS_FUTEX,
                word.as_ptr() as usize as c_long,
                FUTEX_WAIT,
                2,
                &ts as *const Timespec as usize as c_long,
                0,
            );
        }
    }
}

/// Releases the mutex [`lock_ring_mutex`] acquired, waking one waiter iff the word shows any
/// (mirrors glibc's own `__lll_unlock` exactly: unconditionally clear to `0`, wake only if the
/// prior value indicated a possible waiter).
fn unlock_ring_mutex(word: &AtomicU32) {
    if word.swap(0, Ordering::Release) > 1 {
        unsafe {
            syscall(SYS_FUTEX, word.as_ptr() as usize as c_long, FUTEX_WAKE, 1, 0, 0);
        }
    }
}

/// Publishes one already-built record (see [`build_record`]) into the ring using the vendor's own
/// protocol (`docs/23-audio-codec.md` §17.10.2 steps 4-9, byte-for-byte), so `audio_out_thread`
/// (and any other real reader) sees it exactly as it would a genuine `media`-internal write.
/// `record[0..4]` (the seq field [`build_record`] already stamped from a stale, scan-derived
/// guess) is overwritten with the freshly-bumped *real* global sequence inside the lock; nothing
/// else about `record`'s bytes changes here.
///
/// Steps 1-2 of `docs/23-audio-codec.md` §17.11 (bounds-check, encode, build everything) already
/// happened in the caller before this is reached. From [`lock_ring_mutex`] succeeding to
/// [`unlock_ring_mutex`]: no allocation, no logging, no panicking indexing, no `.unwrap()`/
/// `.expect()`, no syscalls beyond the lock/unlock's own futex calls and the plain memory
/// reads/writes below -- audited line by line, per the standing instruction.
///
/// Deliberately skips `docs/23-audio-codec.md` §17.10.2's step 3 (an advisory-only overflow/reset
/// check) and step 11 (the notify-semaphore loop): the real writer's own frequent video/mic
/// traffic already exercises step 3 whenever the threshold is actually reached, so occasional
/// kibbled writes not *also* performing that reset costs nothing but deferring it to the next
/// real write (guaranteed within tens of milliseconds); step 11 is real vendor behavior but not
/// required for `audio_out_thread`'s own poll-based consumption (§17.10.3), and every extra
/// `sem_open`/`sem_post` inside the lock is one more fallible thing to avoid per step 3 above.
/// Also skips step 10 (a keyframe-only side effect): this writer's `frame_type` is always `0`
/// (never `1`, see `build_record`), so that condition can never fire for a record built here.
fn publish(record: &mut [u8]) -> Result<bool, String> {
    if record.len() > VENDOR_WRAP_MODULUS as usize {
        // Mirrors §17.10.2 step 1's own pre-lock bounds check; unreachable for one audio frame
        // (a few hundred bytes) against an 8 MiB ring, kept as a direct, cheap mirror rather than
        // assumed away.
        return Err("record larger than the ring's own capacity".into());
    }
    let reg = writable_registry().map_err(|e| e.to_string())?;
    let mutex = reg.field(OFF_MUTEX);
    lock_ring_mutex(mutex, LOCK_TIMEOUT).map_err(|e| e.to_string())?;

    // ----- locked section: audited, see the doc comment above -----
    let global_seq = reg.field(OFF_GLOBAL_SEQ);
    let running_total = reg.field(OFF_RUNNING_TOTAL);
    let write_cursor = reg.field(OFF_WRITE_CURSOR);

    let cursor = write_cursor.load(Ordering::Relaxed);
    let advance = record.len() as u32;
    if u64::from(cursor) + u64::from(advance) > u64::from(VENDOR_WRAP_MODULUS) {
        // Would straddle the ring's physical wrap seam -- same conservative policy this writer
        // has always used (module doc, "Writer-side ring synchronization"): the vendor's own
        // wrap-splitting write is not something this project can safely replicate (unconfirmed
        // reader handling), so skip cleanly with zero side effects rather than guess at it.
        unlock_ring_mutex(mutex);
        return Ok(false);
    }

    // Step 4: bump the global counter first, and stamp the value it becomes into the record.
    let new_seq = global_seq.load(Ordering::Relaxed).wrapping_add(1);
    global_seq.store(new_seq, Ordering::Relaxed);
    record[0..4].copy_from_slice(&new_seq.to_le_bytes());

    // Step 5: bootstrap edge case, replicated exactly though never observed live on a ring
    // that's been under continuous video/mic traffic (running_total reads 0 only immediately
    // after a fresh ring init or a step-3 reset, neither of which this session ever triggers).
    if running_total.load(Ordering::Relaxed) == 0 {
        reg.field(OFF_SECOND_COUNTER).store(new_seq, Ordering::Relaxed);
    }

    // Steps 6-8: header+payload as one contiguous copy (already assembled into one buffer by
    // build_record) at the real vendor address -- equivalent to the vendor's own two separate
    // writes, since both happen under the same held lock either way and no reader can observe a
    // difference (§17.10.3: readers hold this same mutex across their own header+payload read).
    let dest = unsafe { reg.base.add(VENDOR_DATA_ORIGIN + cursor as usize) };
    unsafe { std::ptr::copy_nonoverlapping(record.as_ptr(), dest, record.len()) };
    write_cursor.store(cursor.wrapping_add(advance) % VENDOR_WRAP_MODULUS, Ordering::Relaxed);

    // Step 9: monotonic running total, deliberately never wrapped (matches the vendor exactly).
    running_total.store(running_total.load(Ordering::Relaxed).wrapping_add(advance), Ordering::Relaxed);

    unlock_ring_mutex(mutex);
    // ----- end locked section -----
    Ok(true)
}

#[repr(C)]
struct Timespec {
    tv_sec: c_long,
    tv_nsec: c_long,
}

extern "C" {
    fn clock_gettime(clk_id: c_int, tp: *mut Timespec) -> c_int;
    fn mmap(addr: *mut c_void, len: usize, prot: c_int, flags: c_int, fd: c_int, offset: i64) -> *mut c_void;
    fn syscall(number: c_long, a1: c_long, a2: c_long, a3: c_long, a4: c_long, a5: c_long) -> c_long;
}

const PROT_READ: c_int = 1;
const PROT_WRITE: c_int = 2;
const MAP_SHARED: c_int = 1;
/// ARM EABI syscall number for `futex` (`docs/23-audio-codec.md`: this device is armv7l) --
/// verified directly against the kernel's own `arch/arm/tools/syscall.tbl` (`240 common futex`),
/// not assumed from another architecture.
const SYS_FUTEX: c_long = 240;
const FUTEX_WAIT: c_long = 0;
/// Deliberately never OR'd with `FUTEX_PRIVATE_FLAG` (128) anywhere in this file: this word is
/// `PTHREAD_PROCESS_SHARED`, and using the private futex variant on a shared mutex would silently
/// stop cross-process wakeups from working, with no error to indicate it.
const FUTEX_WAKE: c_long = 1;

const CLOCK_MONOTONIC: c_int = 1;

/// The same monotonic clock domain `docs/19-frame-ring.md` documents the ring's own PTS field
/// using (`CLOCK_MONOTONIC` is a kernel-wide clock, identical across every process on the same
/// machine -- not per-process -- so this is directly comparable to whatever `media` stamps).
fn monotonic_us() -> u32 {
    let mut ts = Timespec { tv_sec: 0, tv_nsec: 0 };
    unsafe { clock_gettime(CLOCK_MONOTONIC, &mut ts) };
    let us = (ts.tv_sec as i64 as u64)
        .wrapping_mul(1_000_000)
        .wrapping_add((ts.tv_nsec as i64 as u64) / 1_000);
    (us & 0xFFFF_FFFF) as u32
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_rms_scales_a_quiet_signal_up_to_the_target() {
        let mut pcm = vec![100i16, -100, 100, -100];
        normalize_rms(&mut pcm);
        let rms = (pcm.iter().map(|&s| f64::from(s) * f64::from(s)).sum::<f64>() / pcm.len() as f64).sqrt();
        assert!((rms - TARGET_RMS).abs() < 1.0, "expected rms ~{TARGET_RMS}, got {rms}");
    }

    #[test]
    fn normalize_rms_leaves_silence_untouched() {
        let mut pcm = vec![0i16; 8];
        normalize_rms(&mut pcm);
        assert_eq!(pcm, vec![0i16; 8]);
    }

    #[test]
    fn normalize_rms_caps_gain_instead_of_amplifying_near_silence_into_noise() {
        let mut pcm = vec![1i16, -1, 1, -1];
        normalize_rms(&mut pcm);
        // Gain capped at MAX_GAIN (20x), not the ~1532x a naive target-RMS/measured-RMS would ask for.
        assert!(pcm.iter().all(|&s| s.unsigned_abs() as f64 <= MAX_GAIN + 1.0));
    }

    #[test]
    fn normalize_rms_clamps_loud_input_without_wrapping() {
        let mut pcm = vec![30000i16, -30000, 30000, -30000];
        normalize_rms(&mut pcm);
        // Already louder than the target: gain < 1, so this must only ever attenuate, never wrap.
        for s in pcm {
            assert!(s.unsigned_abs() <= 30000, "sample {s} must not exceed the original magnitude");
        }
    }

    #[test]
    fn call_active_fails_closed_when_the_proc_file_is_missing() {
        // /proc/ax_proc/aenc doesn't exist on this dev machine -- the read errors, and the
        // documented fail-closed behavior must treat that as "assume active".
        assert!(call_active());
    }

    #[test]
    fn pack_chunk_zero_pads_a_short_final_chunk() {
        let samples = [1i16, -1, 2];
        let packed = pack_chunk(&samples);
        assert_eq!(&packed[0..2], &1i16.to_le_bytes());
        assert_eq!(&packed[2..4], &(-1i16).to_le_bytes());
        assert_eq!(&packed[4..6], &2i16.to_le_bytes());
        assert!(packed[6..].iter().all(|&b| b == 0), "remainder must be zero-padded");
    }

    #[test]
    fn build_record_places_every_field_at_its_confirmed_offset() {
        let target = AppendTarget { global_seq: 41, chan2_seq: 9 };
        let payload = vec![0xAAu8; 20];
        let rec = build_record(&target, &payload);
        assert_eq!(rec.len(), ring::HDR + payload.len());
        assert_eq!(u32::from_le_bytes(rec[0..4].try_into().unwrap()), 42, "global seq = prev + 1");
        assert_eq!(u32::from_le_bytes(rec[4..8].try_into().unwrap()), 20, "length = payload len");
        assert_eq!(u32::from_le_bytes(rec[8..12].try_into().unwrap()), 10, "chan2 seq = prev + 1");
        assert_eq!(u32::from_le_bytes(rec[28..32].try_into().unwrap()), 2);
        assert_eq!(rec[32], 0, "frame_type: audio");
        assert_eq!(rec[33], 4, "media_class: audio");
        assert_eq!(rec[34], ring::CHAN_AUDIO_OUT);
        assert_eq!(u16::from_le_bytes(rec[46..48].try_into().unwrap()), 16);
        assert_eq!(u16::from_le_bytes(rec[48..50].try_into().unwrap()), 16000);
        assert_eq!(&rec[ring::HDR..], &payload[..]);
    }

    #[test]
    fn a_record_built_by_build_record_parses_back_via_ring_parse_header() {
        // End-to-end sanity: whatever this module writes must be exactly what ring.rs's own
        // reader (and by extension every real reader using the same protocol) accepts.
        let target = AppendTarget { global_seq: 5, chan2_seq: 0 };
        let payload = vec![0x12u8; 100];
        let rec = build_record(&target, &payload);
        let mut buf = vec![0u8; ring::DATA_START];
        buf.extend_from_slice(&rec);
        let h = ring::parse_header(&buf, ring::DATA_START).expect("must parse as a valid header");
        assert_eq!(h.chan, ring::CHAN_AUDIO_OUT);
        assert_eq!(h.length as usize, payload.len());
        assert_eq!(h.seq, 6);
    }

    #[test]
    fn next_adts_frame_returns_none_on_clean_eof_with_nothing_buffered() {
        // A closed pipe with zero bytes ever written reads as immediate EOF.
        let (mut read_end, write_end) = os_pipe();
        drop(write_end);
        let mut buf = Vec::new();
        let mut chunk = [0u8; 64];
        let result = next_adts_frame_from_read(&mut read_end, &mut buf, &mut chunk);
        assert!(matches!(result, Ok(None)));
    }

    #[test]
    fn next_adts_frame_errors_on_eof_with_unparsable_trailing_bytes() {
        let (mut read_end, mut write_end) = os_pipe();
        write_end.write_all(&[0xFFu8, 0xAA]).unwrap(); // too short to ever be a valid ADTS header
        drop(write_end);
        let mut buf = Vec::new();
        let mut chunk = [0u8; 64];
        let result = next_adts_frame_from_read(&mut read_end, &mut buf, &mut chunk);
        assert!(result.is_err(), "trailing garbage at EOF must be reported, not silently dropped");
    }

    // `next_adts_frame` takes a `&mut ChildStdout` specifically (so production code can't be
    // called with an arbitrary reader); these two tests exercise the identical logic through a
    // small generic shim over a real OS pipe, which is the only way to get a `Read` byte-for-byte
    // equivalent to a child's stdout without actually spawning `aacenc`.
    fn next_adts_frame_from_read(
        r: &mut impl Read,
        buf: &mut Vec<u8>,
        chunk: &mut [u8],
    ) -> io::Result<Option<Vec<u8>>> {
        loop {
            if let Some(header) = adts::parse(buf) {
                let total = header.frame_length as usize;
                if total > 0 && buf.len() >= total {
                    return Ok(Some(buf.drain(..total).collect()));
                }
            }
            let n = r.read(chunk)?;
            if n == 0 {
                if buf.is_empty() {
                    return Ok(None);
                }
                return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "trailing bytes"));
            }
            buf.extend_from_slice(&chunk[..n]);
        }
    }

    fn os_pipe() -> (std::fs::File, std::fs::File) {
        use std::os::unix::io::FromRawFd;
        let mut fds = [0i32; 2];
        extern "C" {
            fn pipe(fds: *mut i32) -> i32;
        }
        assert_eq!(unsafe { pipe(fds.as_mut_ptr()) }, 0);
        unsafe { (std::fs::File::from_raw_fd(fds[0]), std::fs::File::from_raw_fd(fds[1])) }
    }
}
