//! Speaker output: two ways into `media`'s own decoder/AO chain, both over the vendor's
//! `play_aac_file` bus message (`bus::msg::PLAY_AAC_FILE`, `docs/23-audio-codec.md` §18.2/§20):
//!
//! 1. **Clip playback** ([`play_bytes`]/[`play_path`]) -- `/speak` and `/clips/<name>/play`. Hands
//!    `media` an ADTS AAC-LC/16 kHz/mono *file* and it plays it exactly like its own canned
//!    prompts. Proven audible live (§20.1: a 23-frame file moved `/proc/ax_proc/ao`'s `SndFrm`
//!    by exactly +23).
//! 2. **Live talkback** ([`LiveSession`], the RTSP backchannel in `backchannel.rs`/`rtsp.rs`).
//!    Same message, but the "file" is a **named pipe** (`TALK_FIFO`): one `play_aac_file` per
//!    session, encoder frames streamed through the pipe as the client's audio arrives, EOF when
//!    the session ends. `media`'s worker `fopen`/`fread`s it like any file, the decoder and the
//!    AO pace it in real time, and there are no file boundaries to click at. §20.6's listening
//!    tests fixed the two rules this module enforces: (a) queued *files* gap at every boundary
//!    (so no chunking), and (b) the pipe must never run dry -- a feeder slower than real time
//!    pops on every frame, a feeder ahead of real time is clean -- hence the pre-roll and the
//!    silence fill below.
//!
//! The earlier ring-publish path (writing `CHAN_AUDIO_OUT` records the way `agora` does) is
//! gone: `media` never consumed a single record of it across four sessions of byte-level work
//! (§13-§18), and §20.2 showed the vendor's own consumer needs a start position kibbled has no
//! way to supply. Everything learned about that ring stays in docs/23.
//!
//! ## Why AAC, not raw PCM
//!
//! `media` decodes AAC itself before the hardware ever sees PCM -- every `AX_AO_SendFrame` call
//! site sits beside an `AX_ADEC` decode, and the only file player it has checks for an ADTS sync
//! word (§18.2). So encoding is mandatory, done via a small statically-linked helper subprocess
//! (`tools/aacenc/`, built from fdk-aac's own open source) because `kibbled` is a fully static
//! musl binary that cannot `dlopen` the device's own `libfdk-aac.so` (confirmed empirically), and
//! the cross toolchain's glibc is 16 releases newer than the device's -- too far apart to
//! dynamically link against the device's copy either.
//!
//! ## Two producers, one speaker
//!
//! (1) Two *kibbled-internal* callers at once (a live backchannel and a `/speak`) -- prevented
//! outright by [`SpeakerOwner`], one in-process exclusive lock every speaker path must hold for
//! its whole session. (2) The *vendor's own* live app talkback running at the same time --
//! detected two independent ways, both failing closed, because the one time this project
//! collided with a real household talkback it cut that talkback short (§19):
//! [`talkback_active`] reads `audio_out_thread`'s guard flag straight out of `media`'s memory
//! (`1` for exactly the duration of an app press-and-hold, §19.1/§20.2), and [`call_active`]
//! reads `/proc/ax_proc/aenc`. Both are checked before a session starts
//! ([`SpeakerOwner::try_acquire`]); a live session re-checks them once a second and aborts.

use std::fs::File;
use std::io::{self, Read, Write};
use std::os::raw::c_int;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{FileExt, OpenOptionsExt};
use std::path::Path;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::adts;
use crate::bus;

/// Where `tools/aacenc/build.sh` deploys its output, alongside `kibbled` itself.
pub const AACENC_PATH: &str = "/opt/kibble/aacenc";

/// Off-by-default safety gate (docs/23-audio-codec.md §19): presence of this file is the *only*
/// thing that allows any code in this module to touch `media`'s bus queue or the ring's
/// `CHAN_AUDIO_OUT` slot. Absent by default on a fresh `/opt/kibble` -- audio stays fully inert
/// until a human explicitly turns it on via `POST /audio {"enabled": true}`. This exists because
/// an earlier, flag-less build sent an unconditional `speak_stop` from every process start
/// (`SpeakerOwner::new()`, now deleted below) and silently ended a real household talkback
/// session mid-call the one time `kibbled` happened to restart during one -- see §19 for the
/// full incident. Checked at the single chokepoint every audio-output path shares
/// ([`SpeakerOwner::try_acquire`]), not scattered across every caller, so there is exactly one
/// place this guarantee can be gotten wrong.
const AUDIO_ENABLED_PATH: &str = "/opt/kibble/audio_enabled";

/// `true` iff a human has explicitly turned audio on (see [`AUDIO_ENABLED_PATH`]'s doc comment).
/// A plain existence check, not a parsed value -- there is nothing to parse wrong.
pub fn enabled() -> bool {
    std::path::Path::new(AUDIO_ENABLED_PATH).exists()
}

/// Flips the flag [`enabled`] reads. Creating the file is the only "on" state; removing it (or
/// it never having existed) is "off" -- `remove_file`'s `NotFound` is not an error here, since
/// "already off" is a completely normal request to make.
pub fn set_enabled(on: bool) -> io::Result<()> {
    if on {
        std::fs::write(AUDIO_ENABLED_PATH, b"")
    } else {
        match std::fs::remove_file(AUDIO_ENABLED_PATH) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e),
        }
    }
}


/// One AAC-LC access unit's sample count at 16kHz -- the encoder's own fixed frame length
/// (`docs/23-audio-codec.md` §2.1: `AACENC_GRANULE_LENGTH` is never overridden, so it stays at
/// FDK's 1024-sample default), and the ring's own real-time cadence unit.
pub const FRAME_SAMPLES: usize = 1024;
const FRAME_BYTES: usize = FRAME_SAMPLES * 2; // 16-bit mono

/// 1024 samples / 16000 Hz -- the real-time duration of one frame. Both the vendor's own
/// encoder and the acceptance tests (docs/23-audio-codec.md) treat this as exact.
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

/// `true` if the vendor's transient talk-session uplink channel looks active (or its state can't
/// be confirmed). Deliberately fails closed: a false positive only delays an announcement, a
/// false negative would play over a real session.
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

/// Absolute virtual address of `audio_out_thread`'s guard flag inside `/app/bin/media`
/// (`docs/23-audio-codec.md` §17.1/§17.4: set to `1` by `speak_start`, cleared by `speak_stop`,
/// never touched by the thread itself). `media` is a fixed-address (non-PIE) executable --
/// `/proc/<pid>/maps` puts its text at `0x10000` on every boot this project has looked at -- so
/// the link-time address *is* the runtime address, readable as a plain 4-byte `pread` of
/// `/proc/<pid>/mem` with no `ptrace`, no signal, and no pause of the target (the same read-only
/// technique §18.4 used to verify the dispatch table live).
const MEDIA_GUARD_FLAG_ADDR: u64 = 0x767f0;

/// `true` if a real app talkback is in progress right now, or that can't be determined. Reads
/// `media`'s `speak_start` guard flag (see [`MEDIA_GUARD_FLAG_ADDR`]): `docs/23-audio-codec.md`
/// §19.1 traced it `0 -> 1` at the instant of an app press-and-hold and `1 -> 0` at release, so a
/// `1` here means the vendor's `audio_out_thread` is (or is about to be) feeding the speaker and
/// `kibbled` must not. Fails closed on any error -- no `media` process, unreadable `/proc`, short
/// read -- for the same reason [`call_active`] does.
pub fn talkback_active() -> bool {
    let Some(pid) = vendor_pid("media") else { return true };
    let mut word = [0u8; 4];
    match File::open(format!("/proc/{pid}/mem"))
        .and_then(|f| f.read_exact_at(&mut word, MEDIA_GUARD_FLAG_ADDR))
    {
        Ok(()) => u32::from_le_bytes(word) != 0,
        Err(_) => true,
    }
}

/// PID of the vendor process whose `comm` is exactly `name`, by a plain `/proc` scan.
fn vendor_pid(name: &str) -> Option<u32> {
    std::fs::read_dir("/proc").ok()?.flatten().find_map(|entry| {
        let pid: u32 = entry.file_name().to_str()?.parse().ok()?;
        let comm = std::fs::read_to_string(entry.path().join("comm")).ok()?;
        (comm.trim_end() == name).then_some(pid)
    })
}

/// Exclusive in-process ownership of the speaker: `backchannel.rs`'s live session and this
/// module's `/speak`/clip-play must never run at the same time (see module doc, "Two producers,
/// one speaker"). One `AtomicBool`, not a `Mutex`: ownership is held across a whole playback
/// session (seconds to minutes) typically by a different thread than the one that acquired it --
/// [`OwnerGuard`] releases it on `Drop` regardless of which thread drops it.
pub struct SpeakerOwner(AtomicBool);

impl SpeakerOwner {
    pub fn new() -> Arc<Self> {
        // No vendor bus message and no ring access happens here, deliberately: a process start
        // must never be able to send a command that could end a session it does not own.
        // docs/23-audio-codec.md §19 -- an earlier version of this function sent an
        // unconditional `speak_stop` here to clear a crash-stuck guard flag, and the one time
        // `kibbled` happened to restart during a real household talkback, that autonomous send
        // ended the vendor's own live session mid-call. If a stale-flag cleanup is ever needed
        // again it must be an explicit, audio-flag-gated action a human asks for -- see
        // `try_acquire` below for the one gate every audio-output path shares -- never something
        // that fires on process start, a crash-loop, or any other non-request-triggered path.
        Arc::new(Self(AtomicBool::new(false)))
    }

    /// Claims exclusive ownership, or returns the reason it couldn't: audio is off, a live app
    /// talk session looks active, or another kibbled-internal session already holds it. This is
    /// the single chokepoint every audio-output path shares (`/speak`, `/clips/<name>/play`, the
    /// RTSP backchannel) -- gating it here, once, is what makes "nothing touches `media` or the
    /// ring unless `enabled()` is true" a real guarantee instead of a convention callers could
    /// forget (docs/23-audio-codec.md §19).
    pub fn try_acquire(self: &Arc<Self>) -> Result<OwnerGuard, &'static str> {
        if !enabled() {
            return Err("audio is disabled (POST /audio {\"enabled\":true} to enable)");
        }
        if talkback_active() {
            return Err("a live app talkback is active (media's speak_start guard flag is set)");
        }
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

/// `src` we stamp on bus messages to `media` -- no handler reads it (§17.1/§18.2), so this only
/// matters for matching the rest of the project's "we speak on ctrl's behalf" convention.
const BUS_SRC: u16 = bus::Peer::Ctrl as u16;

/// Errors from a speaker session, surfaced to HTTP callers via `main.rs`.
#[derive(Debug)]
pub enum SpeakError {
    Encoder(String),
    Bus(String),
    File(String),
}

impl std::fmt::Display for SpeakError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SpeakError::Encoder(e) => write!(f, "encoder: {e}"),
            SpeakError::Bus(e) => write!(f, "media bus: {e}"),
            SpeakError::File(e) => write!(f, "clip file: {e}"),
        }
    }
}

/// What a completed (or aborted) playback actually did -- logged by `main.rs` and reported by the
/// acceptance test (docs/23-audio-codec.md).
#[derive(Debug, Default, Clone, Copy)]
pub struct PlaybackStats {
    /// Frames handed to `media`: written to the ring (ring path) or contained in the file it was
    /// told to play (file path).
    pub frames_written: u64,
    /// Frames the audio-output driver actually emitted during this playback, measured as the
    /// delta of `/proc/ax_proc/ao`'s `SndFrm` counter ([`ao_frames_sent`]) -- the only ground
    /// truth this project has for "it made sound". File path only; the ring path has never
    /// moved this counter (`docs/23-audio-codec.md` §17.12.4) and does not attempt to read it.
    pub frames_played: u64,
    /// Live sessions only: frames of synthesized silence the filler had to insert because the
    /// client's audio fell behind wall-clock time (network/encoder stalls). Each one is a gap the
    /// listener heard; zero is a clean session.
    pub silence_frames: u64,
    /// Live sessions only: the largest deficit (in frames) between wall-clock time and the
    /// client's delivered audio seen during the session -- how close it came to needing silence.
    pub max_lag_frames: u64,
    pub aborted_call_active: bool,
}

/// The most recent completed playback or live session, for `GET /audio` -- the only place these
/// numbers can be seen while the supervisor still discards `kibbled`'s stderr (docs/23 §19.5).
pub static LAST_STATS: Mutex<Option<PlaybackStats>> = Mutex::new(None);

pub fn record_last(stats: PlaybackStats) {
    *LAST_STATS.lock().unwrap_or_else(|p| p.into_inner()) = Some(stats);
}

impl PlaybackStats {
    pub fn to_json(&self) -> String {
        format!(
            r#"{{"frames_written":{},"frames_played":{},"silence_frames":{},"max_lag_frames":{},"aborted_call_active":{}}}"#,
            self.frames_written, self.frames_played, self.silence_frames, self.max_lag_frames, self.aborted_call_active
        )
    }
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
/// (`POST /speak`): concatenated ADTS access units, ready to write straight to a clip file for
/// [`play_path`] / [`play_bytes`]. Spawns and fully drains the `aacenc` helper; blocks the
/// calling thread for the encode's duration only (well under a second for any realistic clip
/// length -- `tools/aacenc/`'s own validation), not for real-time playback.
pub fn normalize_and_encode(pcm: &[i16]) -> Result<Vec<u8>, SpeakError> {
    let mut pcm = pcm.to_vec();
    normalize_rms(&mut pcm);
    let frames = encode(&pcm)?;
    Ok(frames.concat())
}

/// Number of complete ADTS access units in `adts_bytes` -- what `media` will play from a file of
/// these bytes, and therefore the `SndFrm` delta a successful [`play_path`] is expected to show.
/// Stops at the first frame whose declared length overruns the buffer (a truncated tail is not
/// a frame `media` will play either; its own reader checks the same sync word and length).
pub fn count_frames(adts_bytes: &[u8]) -> u64 {
    let mut frames = 0;
    let mut off = 0;
    while let Some(header) = adts::parse(&adts_bytes[off..]) {
        let len = header.frame_length as usize;
        if len == 0 || off + len > adts_bytes.len() {
            break;
        }
        frames += 1;
        off += len;
    }
    frames
}

/// Where [`play_bytes`] stages a one-shot clip for `media` to `fopen`: tmpfs, never flash
/// (`/tmp` is RAM on this device; `/opt` is UBIFS and every write there wears it). One fixed
/// path rather than a fresh temp name per call: [`SpeakerOwner`] already serializes callers and
/// [`play_path`] does not return until `media` has played the file (or the bounded wait ran
/// out), so no caller can overwrite a file still being read. Written to a `.part` sibling and
/// `rename`d into place so `media` can never `fopen` a half-written file.
const SPEAK_FILE: &str = "/tmp/kibble-speak.aac";

/// `/proc/ax_proc/ao`: the audio-output driver's own status, including the cumulative `SndFrm`
/// frame counter every live speaker test in `docs/23-audio-codec.md` used as its ground truth.
const AO_PROC: &str = "/proc/ax_proc/ao";

/// How often [`play_path`] re-reads `SndFrm` while waiting for `media` to finish, and how long
/// past the clip's nominal real-time length it keeps waiting before giving up on the counter:
/// covers bus dispatch latency (`media` started playing ~0.3 s after the send in §20's trace)
/// plus the decoder's few-frame tail, with margin. Nominal length + grace is the worst case a
/// caller blocks; a clip that plays normally returns the moment the counter shows every frame
/// out.
const PLAY_POLL: Duration = Duration::from_millis(100);
const PLAY_GRACE: Duration = Duration::from_secs(3);

/// The audio-output driver's cumulative sent-frame count (`SndFrm`), or `None` if the proc file
/// is unreadable or its layout isn't the one this project has observed on every read (a
/// `AoCardId ...` header row naming the column, then one data row). Monotonic across a boot; the
/// delta over a playback is the number of AAC frames the speaker actually received.
pub fn ao_frames_sent() -> Option<u64> {
    parse_snd_frm(&std::fs::read_to_string(AO_PROC).ok()?)
}

/// Pulls `SndFrm` out of `/proc/ax_proc/ao`'s text by its column header rather than a fixed
/// position: the file is a series of `-------- SECTION ---` banners each followed by a header
/// row and a data row, and `SndFrm` is a column of the last one (`AO DEV STATUS`).
fn parse_snd_frm(text: &str) -> Option<u64> {
    let mut lines = text.lines();
    while let Some(line) = lines.next() {
        let Some(col) = line.split_whitespace().position(|c| c == "SndFrm") else { continue };
        return lines.next()?.split_whitespace().nth(col)?.parse().ok();
    }
    None
}

/// Plays already-encoded ADTS bytes (the immediate result of [`normalize_and_encode`]) by staging
/// them at [`SPEAK_FILE`] and handing that path to `media` via [`play_path`]. Blocks for the
/// clip's real-time duration -- callers that must not block their own request thread (`main.rs`'s
/// `/speak` handler) run this on a spawned thread and return once [`SpeakerOwner::try_acquire`]
/// alone has succeeded. The staged file is removed afterwards to give the RAM back.
pub fn play_bytes(adts_bytes: &[u8], owner: &OwnerGuard) -> Result<PlaybackStats, SpeakError> {
    let part = format!("{SPEAK_FILE}.part");
    std::fs::write(&part, adts_bytes)
        .and_then(|()| std::fs::rename(&part, SPEAK_FILE))
        .map_err(|e| SpeakError::File(format!("{SPEAK_FILE}: {e}")))?;
    let result = play_path(Path::new(SPEAK_FILE), count_frames(adts_bytes), owner);
    let _ = std::fs::remove_file(SPEAK_FILE);
    result
}

/// Plays one ADTS AAC-LC/16kHz/mono file already on disk (a stored clip under `clips::CLIPS_DIR`,
/// or [`play_bytes`]'s staged upload) through `media`'s own canned-prompt engine:
/// `bus::msg::PLAY_AAC_FILE` with the path as payload, exactly what `ctrl` sends for
/// `/audio/en/*.aac` (`docs/23-audio-codec.md` §18.2, proven audible §20). `media` decodes and
/// paces the file itself on its own worker thread, so this only has to wait: it polls
/// [`ao_frames_sent`] until the driver has emitted `frames` more frames than before the send,
/// or the clip's nominal length plus [`PLAY_GRACE`] has elapsed, whichever comes first, and
/// reports the observed delta as `frames_played`. Holding `owner` for that whole wait is what
/// keeps a second clip from being sent while `media` is still reading this one.
///
/// This is the only function in `kibbled` that sends `PLAY_AAC_FILE`, and it is reachable only
/// through an [`OwnerGuard`] -- i.e. only after [`SpeakerOwner::try_acquire`]'s `enabled()` and
/// talkback checks passed for an explicit `/speak` or `/clips/<name>/play` request
/// (`docs/23-audio-codec.md` §19.4's standing requirement).
pub fn play_path(path: &Path, frames: u64, _owner: &OwnerGuard) -> Result<PlaybackStats, SpeakError> {
    let before = ao_frames_sent();
    send_play(path)?;
    Ok(PlaybackStats { frames_written: frames, frames_played: wait_for_ao(before, frames), ..PlaybackStats::default() })
}

/// The named pipe a live talk session streams through (tmpfs). One fixed path: [`SpeakerOwner`]
/// serializes sessions, and each session recreates it.
const TALK_FIFO: &str = "/tmp/kibble-talk.aac";
/// Frames held back before the first byte reaches `media`, so the decoder/AO start with a
/// cushion against network and encoder jitter instead of on an empty buffer (§20.6: starting
/// with 4 frames still popped once at the start). 8 frames = 512 ms; also the floor of the
/// session's end-to-end latency, on top of the encoder's own ~1 frame.
const PREROLL_FRAMES: usize = 8;
/// Silence fill: the session keeps its own 16 kHz sample clock, and whenever the client's PCM
/// falls this far behind wall-clock time, zero samples are encoded in its place so the pipe never
/// runs dry (§20.6: any starvation is an audible pop). Real audio that then arrives late is
/// queued behind the silence -- a bounded, one-off latency cost per gap, not a drop.
const SILENCE_SLACK_SAMPLES: u64 = (PREROLL_FRAMES as u64 - 2) * FRAME_SAMPLES as u64;
/// Encoded silence appended after the client's last audio so it drains through the decoder and
/// AO before the pipe's EOF, instead of the tail being cut/popped (§20.6).
const TAIL_SILENCE_FRAMES: usize = 4;
/// How long a pipe write may stay blocked (pipe full, `media` not reading) before the session
/// concludes `media` never picked the file up and aborts rather than hanging a thread forever.
const FIFO_WRITE_TIMEOUT: Duration = Duration::from_secs(3);
const FIFO_RETRY: Duration = Duration::from_millis(5);
/// How often a live session re-checks the vendor-talkback gates.
const GATE_RECHECK_FRAMES: u64 = 16;

/// A live, incrementally-fed speaker session for the RTSP backchannel (`backchannel.rs`): unlike
/// [`play_path`] (a short clip, fully known upfront), a backchannel call is open-ended and
/// arrives as a live RTP stream. PCM goes into the `aacenc` helper's stdin (`feed`, plus the
/// silence filler); a drainer thread pulls ADTS frames off its stdout and writes them into
/// [`TALK_FIFO`], which `media` is reading as the "file" of one `PLAY_AAC_FILE` (module doc,
/// path 2). `tools/aacenc/wrapper.c` accumulates arbitrary-sized writes up to its own fixed
/// 1024-sample frame internally, so callers don't need to align to that boundary themselves.
pub struct LiveSession {
    encoder: Arc<Mutex<EncoderIn>>,
    child: Child,
    stop: Arc<AtomicBool>,
    filler: Option<thread::JoinHandle<()>>,
    drainer: Option<thread::JoinHandle<Result<PlaybackStats, SpeakError>>>,
    ao_before: Option<u64>,
    _owner: OwnerGuard,
}

/// The encoder's stdin plus the session's sample clock, shared by `feed` (caller thread) and the
/// silence filler thread.
struct EncoderIn {
    stdin: Option<ChildStdin>,
    /// Set by the first real `feed`: the session's sample clock starts with the client's first
    /// audio, not with `start()`, so the ~1 s a client takes to open its own source (Scrypted's
    /// ffmpeg spawn, HomeKit's negotiation) isn't filled with silence that then delays everything.
    started: Option<Instant>,
    samples_fed: u64,
    silence_frames: u64,
    max_lag_samples: u64,
}

impl EncoderIn {
    fn write_pcm(&mut self, pcm: &[i16]) -> io::Result<()> {
        let Some(stdin) = self.stdin.as_mut() else { return Ok(()) };
        self.started.get_or_insert_with(Instant::now);
        let mut bytes = Vec::with_capacity(pcm.len() * 2);
        for s in pcm {
            bytes.extend_from_slice(&s.to_le_bytes());
        }
        stdin.write_all(&bytes)?;
        self.samples_fed += pcm.len() as u64;
        Ok(())
    }
}

impl LiveSession {
    pub fn start(owner: OwnerGuard) -> Result<Self, SpeakError> {
        let fifo = open_talk_fifo()?;
        let mut child = spawn_encoder()?;
        let stdin = child.stdin.take().expect("piped stdin");
        let mut stdout = child.stdout.take().expect("piped stdout");
        let ao_before = ao_frames_sent();
        send_play(Path::new(TALK_FIFO))?;

        let drainer = thread::spawn(move || -> Result<PlaybackStats, SpeakError> {
            let mut fifo = fifo;
            let mut stats = PlaybackStats::default();
            let mut buf = Vec::new();
            let mut chunk = [0u8; 8192];
            let mut preroll: Vec<Vec<u8>> = Vec::with_capacity(PREROLL_FRAMES);
            loop {
                let frame = match next_adts_frame(&mut stdout, &mut buf, &mut chunk) {
                    Ok(Some(f)) => f,
                    Ok(None) => break,
                    Err(e) => return Err(SpeakError::Encoder(e.to_string())),
                };
                if stats.frames_written % GATE_RECHECK_FRAMES == 0 && (talkback_active() || call_active()) {
                    stats.aborted_call_active = true;
                    break;
                }
                if preroll.len() < PREROLL_FRAMES {
                    preroll.push(frame);
                    if preroll.len() == PREROLL_FRAMES {
                        for f in preroll.drain(..) {
                            write_fifo(&mut fifo, &f)?;
                            stats.frames_written += 1;
                        }
                    }
                    continue;
                }
                write_fifo(&mut fifo, &frame)?;
                stats.frames_written += 1;
            }
            // A session shorter than the pre-roll still plays what it had.
            for f in preroll.drain(..) {
                write_fifo(&mut fifo, &f)?;
                stats.frames_written += 1;
            }
            drop(fifo); // last writer gone -> `media` reads EOF and its worker exits
            Ok(stats)
        });

        let encoder = Arc::new(Mutex::new(EncoderIn { stdin: Some(stdin), started: None, samples_fed: 0, silence_frames: 0, max_lag_samples: 0 }));
        let stop = Arc::new(AtomicBool::new(false));
        let filler = {
            let encoder = Arc::clone(&encoder);
            let stop = Arc::clone(&stop);
            thread::spawn(move || {
                let zeros = [0i16; FRAME_SAMPLES];
                while !stop.load(Ordering::Acquire) {
                    thread::sleep(FRAME_DURATION / 2);
                    let mut enc = encoder.lock().unwrap_or_else(|p| p.into_inner());
                    let Some(started) = enc.started else { continue };
                    let expected = started.elapsed().as_micros() as u64 * 16_000 / 1_000_000;
                    enc.max_lag_samples = enc.max_lag_samples.max(expected.saturating_sub(enc.samples_fed));
                    while enc.samples_fed + SILENCE_SLACK_SAMPLES < expected {
                        if enc.write_pcm(&zeros).is_err() {
                            return;
                        }
                        enc.silence_frames += 1;
                    }
                }
            })
        };

        Ok(Self { encoder, child, stop, filler: Some(filler), drainer: Some(drainer), ao_before, _owner: owner })
    }

    /// Feeds one arbitrary-length slice of 16kHz/mono PCM (`backchannel.rs` has already decoded
    /// and upsampled G.711 to this rate before calling in).
    pub fn feed(&mut self, pcm: &[i16]) -> io::Result<()> {
        self.encoder.lock().unwrap_or_else(|p| p.into_inner()).write_pcm(pcm)
    }

    /// Appends the tail silence, closes the encoder's stdin (flushing its lookahead), waits for
    /// the drainer to push the last frames through the pipe and close it, then reads how many
    /// frames the AO actually emitted. Consumes `self` -- one `finish` (or none, if dropped).
    pub fn finish(mut self) -> Result<PlaybackStats, SpeakError> {
        self.stop.store(true, Ordering::Release);
        if let Some(h) = self.filler.take() {
            let _ = h.join();
        }
        let (silence_frames, max_lag_frames) = {
            let mut enc = self.encoder.lock().unwrap_or_else(|p| p.into_inner());
            let zeros = [0i16; FRAME_SAMPLES];
            for _ in 0..TAIL_SILENCE_FRAMES {
                let _ = enc.write_pcm(&zeros);
            }
            enc.stdin.take(); // EOF
            (enc.silence_frames, enc.max_lag_samples / FRAME_SAMPLES as u64)
        };
        let mut result = self
            .drainer
            .take()
            .expect("finish called once")
            .join()
            .unwrap_or_else(|_| Err(SpeakError::Encoder("drainer thread panicked".into())));
        let _ = self.child.wait();
        if let Ok(stats) = result.as_mut() {
            stats.frames_played = wait_for_ao(self.ao_before, stats.frames_written);
            stats.silence_frames = silence_frames;
            stats.max_lag_frames = max_lag_frames;
        }
        let _ = std::fs::remove_file(TALK_FIFO);
        result
    }
}

impl Drop for LiveSession {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(h) = self.filler.take() {
            let _ = h.join();
        }
        self.encoder.lock().unwrap_or_else(|p| p.into_inner()).stdin.take();
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(h) = self.drainer.take() {
            let _ = h.join();
        }
        let _ = std::fs::remove_file(TALK_FIFO);
    }
}

/// (Re)creates [`TALK_FIFO`] and opens it `O_RDWR | O_NONBLOCK`: read-write so the open never
/// blocks waiting for `media` and so our fd counts as the writer `media`'s `fopen("rb")` needs;
/// non-blocking so a pipe `media` isn't draining surfaces as `WouldBlock` for [`write_fifo`]'s
/// bounded retry instead of parking the drainer forever.
fn open_talk_fifo() -> Result<File, SpeakError> {
    let _ = std::fs::remove_file(TALK_FIFO);
    let c_path = std::ffi::CString::new(TALK_FIFO).expect("no NUL");
    if unsafe { mkfifo(c_path.as_ptr(), 0o600) } != 0 {
        return Err(SpeakError::File(format!("mkfifo {TALK_FIFO}: {}", io::Error::last_os_error())));
    }
    std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(O_NONBLOCK)
        .open(TALK_FIFO)
        .map_err(|e| SpeakError::File(format!("open {TALK_FIFO}: {e}")))
}

/// Writes one whole ADTS frame into the pipe, retrying `WouldBlock` (pipe full) for up to
/// [`FIFO_WRITE_TIMEOUT`]. Frames are far below `PIPE_BUF`, so each write lands atomically.
fn write_fifo(fifo: &mut File, frame: &[u8]) -> Result<(), SpeakError> {
    let deadline = Instant::now() + FIFO_WRITE_TIMEOUT;
    loop {
        match fifo.write_all(frame) {
            Ok(()) => return Ok(()),
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    return Err(SpeakError::File(format!("{TALK_FIFO}: media stopped reading")));
                }
                thread::sleep(FIFO_RETRY);
            }
            Err(e) => return Err(SpeakError::File(format!("{TALK_FIFO}: {e}"))),
        }
    }
}

fn send_play(path: &Path) -> Result<(), SpeakError> {
    let mut payload = Vec::with_capacity(path.as_os_str().len() + 1);
    payload.extend_from_slice(path.as_os_str().as_bytes());
    payload.push(0);
    bus::Sender::open(bus::Peer::Media, BUS_SRC)
        .and_then(|s| s.send(bus::msg::PLAY_AAC_FILE, &payload))
        .map_err(|e| SpeakError::Bus(format!("play_aac_file: {e}")))
}

/// Polls [`ao_frames_sent`] until the driver has emitted `frames` more than `before`, or
/// [`PLAY_GRACE`] passes with no further movement; returns the observed delta.
fn wait_for_ao(before: Option<u64>, frames: u64) -> u64 {
    let Some(before) = before else { return 0 };
    let mut played = 0;
    let mut last_move = Instant::now();
    loop {
        thread::sleep(PLAY_POLL);
        if let Some(now) = ao_frames_sent() {
            let p = now.saturating_sub(before);
            if p != played {
                played = p;
                last_move = Instant::now();
            }
            if played >= frames {
                return played;
            }
        }
        if last_move.elapsed() >= PLAY_GRACE {
            return played;
        }
    }
}

extern "C" {
    fn mkfifo(path: *const std::os::raw::c_char, mode: u32) -> c_int;
}
const O_NONBLOCK: c_int = 0o4000;

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
    fn talkback_active_fails_closed_without_a_media_process() {
        // No vendor `media` process on this dev machine: the gate must refuse, not wave through.
        assert!(talkback_active());
    }

    /// Verbatim `/proc/ax_proc/ao` from the device (2026-09-16, docs/23-audio-codec.md §20),
    /// trailing spaces and all -- `SndFrm` sits in the *last* section, after five earlier
    /// `AoCardId`-headed tables that must not be mistaken for it.
    const REAL_AO_PROC: &str = "-------- AO VERSION ------------------------\n\
[Axera version]: ax_audio V3.0.0_20250707110135 Jul  7 2025 11:43:31 JK\n\
\n\
-------- AO DEV ATTR ------------------------\n\
AoCardId        AoDevId         ChnCnt          Samplerate      PeriodSize      PeriodCount     LinkMode        InsertSilence   AoDepth         enBitwidth      \n\
0               1               2               16000           160             8               0               0               30              16bit           \n\
-------- AO DEV VOLCTL ATTR ------------------------\n\
AoCardId        AoDevId         VqeVolume       CurrVqeVolume   MuteEnable      Fade            FadeInRate      FadeOutRate     \n\
0               1               0.700000        0.700000        0               0               0               0               \n\
-------- AO DEV STATUS ------------------------\n\
AoCardId        AoDevId         SndFrm          GetFrm          Writei          \n\
0               1               161             161             161             \n";

    #[test]
    fn parse_snd_frm_reads_the_status_table_by_column_name() {
        assert_eq!(parse_snd_frm(REAL_AO_PROC), Some(161));
    }

    #[test]
    fn parse_snd_frm_is_none_when_the_status_table_is_absent_or_truncated() {
        assert_eq!(parse_snd_frm("-------- AO VERSION ---\n[Axera version]: x\n"), None);
        let header_only = REAL_AO_PROC.rsplit_once('\n').unwrap().0.rsplit_once('\n').unwrap().0;
        assert_eq!(parse_snd_frm(header_only), None);
    }

    #[test]
    fn count_frames_stops_at_a_truncated_trailing_frame() {
        // Two real 266-byte frames (docs/23 §3 header) then a third whose declared length
        // overruns the buffer: only the two complete ones count, matching what `media`'s own
        // reader will play.
        let hdr = [0xffu8, 0xf1, 0x60, 0x40, 0x21, 0x5f, 0xfc];
        let mut buf = Vec::new();
        for _ in 0..2 {
            buf.extend_from_slice(&hdr);
            buf.extend(std::iter::repeat_n(0u8, 266 - 7));
        }
        buf.extend_from_slice(&hdr);
        buf.extend(std::iter::repeat_n(0u8, 100));
        assert_eq!(count_frames(&buf), 2);
        assert_eq!(count_frames(&[]), 0);
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
