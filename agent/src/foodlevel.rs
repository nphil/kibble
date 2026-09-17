//! On-device bowl-fill inference: runs the vendor's own food-detection model through a second,
//! independent process (`tools/kibble-food.c`, mirroring `embed.rs`/`tools/kibble-embed.c`'s
//! established pattern) against the freshest available camera frame, entirely locally -- no
//! cloud round trip.
//!
//! ## Why this exists
//!
//! `docs/34-bowl-fill-surplus.md` Part 6 disassembly-proved that `config_shm`'s `BOWL_FILL_1`
//! (`state::Snapshot::bowl_fill_1`, the vendor's own reading) is `(int)(score * 100.0f)`, where
//! `score` is a 0.0-1.0 bowl-fullness estimate from a vision model that only `media`'s own
//! cloud-gated pipeline was ever observed to run -- with the cloud blackholed (this project's
//! whole point), that field just stays invalid forever. Part 7 disassembled the model's real
//! wrapper (`/alg/libalgo.so`'s `CPetkitAlgoFoodDetect` class) closely enough to drive it
//! directly from a second process, exactly as `embed.rs` already does for face embeddings --
//! see `tools/kibble-food.c`'s own module doc for the full call sequence and disassembly
//! citations. This module is the `kibbled`-side half: decide when a fresh-enough frame is worth
//! spending an inference on, run the helper, cache the result, expose it via `GET /state`.
//!
//! ## Frame source
//!
//! The model's own hardcoded crop rectangle (`tools/kibble-food.c`'s `FRAME_W`/`FRAME_H`
//! comment) expects a full-scene camera frame, not a face crop. `kibbled` has no on-device H.264
//! decoder (`feed_capture.rs`'s before/after keyframes are raw Annex-B, served to HA for it to
//! decode, never decoded here), so the cheapest correct source is what `ai.rs` already taps: the
//! vendor's own `"visit"`/`"eat"` JPEG snapshots (`ai::Feed::latest_scene_image`) -- full 1152x720
//! frames, written on the vendor's own detection cadence, not a fixed timer. That means a frame
//! is not always available (no `poll_loop` tick, or plainly a bit stale after some time without
//! cat traffic); this module accepts that honestly rather than reusing an old frame indefinitely
//! -- see [`MAX_FRAME_AGE`].
//!
//! ## Scheduling: the Contract's two triggers
//!
//! `GET /state`'s `bowl_fill_local` updates (a) once right after a feed cycle completes (bypasses
//! the periodic cooldown entirely -- a feed is inherently rare enough not to need rate-limiting
//! against itself) and (b) otherwise at most once every [`RATE_LIMIT`], and never while
//! `state::off::FEEDING` is set. The rate limit is charged on every real attempt (spawning the
//! helper), not only successful ones -- a failed run (NPU busy, helper crash) still counts, so a
//! misbehaving helper can never turn into a hot loop hammering the NPU. [`should_attempt`] is the
//! pure decision function this logic reduces to; [`poll_loop`] is the real, unit-untestable
//! wiring around it (needs a real `Shm`/`ai::Feed`/subprocess, same reasoning `feed_capture.rs`'s
//! own module doc gives for not unit-testing its watcher loop).
//!
//! The "never during a feed" rule is enforced twice: [`should_attempt`] refuses to even start
//! while `FEEDING` is set, and [`run_helper`] polls the same flag while the child is running and
//! kills it the instant a feed starts mid-inference -- an inference that outlives its own start
//! check is exactly as unsafe as one that never checked at all.
//!
//! ## What `GET /state` shows
//!
//! A new field, `bowl_fill_local: [percent, computed_unix]` (both `null` until the first
//! successful reading), alongside the existing, untouched `bowl_fill` (the vendor's own
//! `config_shm` value, hopper 1/hopper 2). Deliberately a new field rather than repurposing
//! `bowl_fill`'s own second slot: that slot is hopper 2's *own* reading (never populated by the
//! vendor on this device, per `docs/34` Part "Empirical confirmation"), not a spare timestamp
//! slot -- writing Kibble's own hopper-1 estimate there would misrepresent hopper 2 on any
//! two-hopper device. Both fields stay independently readable so an operator (or a future HA
//! integration update) can compare Kibble's own estimate against the vendor's, when the vendor's
//! happens to be available too.

use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::ai;
use crate::state::{off, Shm};

/// Real device path of the vendor's food-detection model -- read-only input, never modified.
/// Confirmed present, this exact size (992,633 bytes), live: docs/34-bowl-fill-surplus.md Part 7.
pub const MODEL_PATH: &str = "/alg/petkit_pp_fooddet_416_128_segreg_0509_u16.axmodel";
/// Where the cross-compiled second-process helper (`tools/kibble-food.c`) is deployed --
/// alongside `kibbled` itself, the same directory `embed.rs`'s helper already lives in.
pub const HELPER_PATH: &str = "/opt/kibble/kibble-food";

/// "After a feed" is its own trigger with no cooldown; otherwise, at most one real attempt per
/// this long -- the Contract's own "at most once every 10 minutes" bound.
const RATE_LIMIT: Duration = Duration::from_secs(600);
/// How old the freshest `"visit"`/`"eat"` frame may be and still be worth an inference. Matches
/// [`RATE_LIMIT`]: there is no point holding a frame "fresh" for longer than this module would
/// ever go looking for one anyway, and a frame older than one full cycle is plausibly showing a
/// bowl state that has already changed.
const MAX_FRAME_AGE: Duration = Duration::from_secs(600);
/// Settle time after `FEEDING` clears before looking for a frame -- mirrors
/// `feed_capture.rs::SETTLE_AFTER_FEED` exactly (same reasoning: the scene needs a moment, and so
/// does the vendor's own detection pipeline if a cat is right there).
const SETTLE_AFTER_FEED: Duration = Duration::from_secs(3);
/// How often the background loop wakes to check `FEEDING` and the rate limit.
const POLL_INTERVAL: Duration = Duration::from_millis(1000);
/// How often [`run_helper`] polls the child for exit / the feeding flag while it runs.
const HELPER_POLL_INTERVAL: Duration = Duration::from_millis(50);
/// Hard bound on one helper invocation. Observed live: ~1.5s cold (model file not yet page
/// cached), ~0.6s warm (docs/34 Part 7) -- 15s is generous headroom for a busy NPU, not a tuned
/// value, while still bounding a genuinely stuck helper's worst case.
const HELPER_TIMEOUT: Duration = Duration::from_secs(15);

/// One cached reading, ready for `GET /state`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Reading {
    /// `(score * 100.0).round()`, clamped to `0..=100` -- the same units and rounding convention
    /// as the vendor's own `BOWL_FILL_1` (`docs/34` Part 6: `(int)(score * 100.0f)`), so the two
    /// are directly comparable.
    pub pct: u8,
    /// Unix time this reading was computed (not the source frame's own timestamp).
    pub computed_unix: u64,
}

struct Inner {
    last: Option<Reading>,
    last_attempt_at: Option<Instant>,
}

pub struct FoodLevel {
    inner: Mutex<Inner>,
}

impl FoodLevel {
    fn new() -> Arc<FoodLevel> {
        Arc::new(FoodLevel { inner: Mutex::new(Inner { last: None, last_attempt_at: None }) })
    }

    /// `GET /state`'s `bowl_fill_local` -- `None` until the first successful reading lands.
    pub fn snapshot(&self) -> Option<Reading> {
        self.inner.lock().unwrap().last
    }

    fn last_attempt_at(&self) -> Option<Instant> {
        self.inner.lock().unwrap().last_attempt_at
    }

    fn record_attempt(&self, now: Instant) {
        self.inner.lock().unwrap().last_attempt_at = Some(now);
    }

    fn record_reading(&self, reading: Reading) {
        self.inner.lock().unwrap().last = Some(reading);
    }
}

/// Whether [`poll_loop`] should spend a real attempt (spawn the helper) right now. Pure: the
/// entire "never during a feed / after-feed bypass / otherwise rate-limited" policy in one
/// directly unit-testable function, independent of any real clock, process, or device.
fn should_attempt(
    feeding_now: bool,
    just_finished_feed: bool,
    last_attempt_at: Option<Instant>,
    now: Instant,
    rate_limit: Duration,
) -> bool {
    if feeding_now {
        return false;
    }
    if just_finished_feed {
        return true;
    }
    match last_attempt_at {
        None => true,
        Some(t) => now.saturating_duration_since(t) >= rate_limit,
    }
}

/// Decodes `tools/kibble-food.c`'s stdout (`"score=<float>\n"`) into a validated 0.0-1.0 score.
/// Pure and independent of the process spawn, so it's directly unit-testable without a real
/// helper binary or NPU -- mirrors `embed::parse_output`'s own doc/reasoning.
fn parse_score(stdout: &[u8]) -> Option<f32> {
    let text = std::str::from_utf8(stdout).ok()?;
    let first_line = text.lines().next()?;
    let rest = first_line.strip_prefix("score=")?;
    let value: f32 = rest.trim().parse().ok()?;
    // The helper itself already rejects non-finite scores (isfinite check before printing) and
    // the vendor's own formula only clamps the *upper* bound to 1.0 (docs/34 Part 7) -- re-verify
    // both here rather than trust a single layer, and allow only a hair of float-rounding
    // headroom rather than silently clamping a value that would indicate something is genuinely
    // wrong (a helper version mismatch, a model change) into a plausible-looking number.
    if !value.is_finite() || !(-0.001..=1.001).contains(&value) {
        return None;
    }
    Some(value.clamp(0.0, 1.0))
}

#[derive(Debug)]
enum HelperError {
    /// Couldn't even start the helper process (missing binary, permissions, ...).
    Spawn(std::io::Error),
    /// A feed started while the helper was still running -- killed immediately, not a real
    /// failure of the helper itself, but never a value to trust either.
    AbortedForFeed,
    /// The helper didn't exit within [`HELPER_TIMEOUT`] -- killed.
    Timeout,
    /// The helper ran but exited nonzero; `stderr` is whatever it printed, truncated is fine --
    /// this is for a log line, not machine parsing.
    ExitStatus { code: Option<i32>, stderr: String },
    /// The helper exited 0 but stdout wasn't a valid `score=<float>` line.
    BadOutput,
}

impl std::fmt::Display for HelperError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HelperError::Spawn(e) => write!(f, "spawn {HELPER_PATH}: {e}"),
            HelperError::AbortedForFeed => write!(f, "aborted: a feed started mid-inference"),
            HelperError::Timeout => write!(f, "{HELPER_PATH} did not exit within {HELPER_TIMEOUT:?}"),
            HelperError::ExitStatus { code, stderr } => {
                write!(f, "{HELPER_PATH} exited {code:?}: {}", stderr.trim())
            }
            HelperError::BadOutput => write!(f, "{HELPER_PATH} exited 0 but printed no valid score"),
        }
    }
}

/// Runs the helper once against `image`, polling for exit *and* for `state::off::FEEDING` on
/// every tick -- an inference that started legally but outlives a feed that begins mid-run is
/// exactly as unsafe as one that skipped the pre-flight check, so this is the second (and final)
/// enforcement point for "never while a feed is in progress" (`should_attempt` is the first).
fn run_helper(helper: &Path, model: &Path, image: &Path, timeout: Duration, shm: &Shm) -> Result<f32, HelperError> {
    let mut child = Command::new(helper)
        .arg(model)
        .arg(image)
        // Belt and suspenders, matching `embed::extract_with`: the helper's own `-Wl,-rpath,
        // /soc/lib` link-time flag should already cover this.
        .env("LD_LIBRARY_PATH", "/soc/lib")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(HelperError::Spawn)?;

    let start = Instant::now();
    let status = loop {
        if shm.u8(off::FEEDING) != 0 {
            let _ = child.kill();
            let _ = child.wait();
            return Err(HelperError::AbortedForFeed);
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {
                if start.elapsed() >= timeout {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(HelperError::Timeout);
                }
                thread::sleep(HELPER_POLL_INTERVAL);
            }
            Err(e) => return Err(HelperError::Spawn(e)),
        }
    };

    let mut stdout = Vec::new();
    if let Some(mut s) = child.stdout.take() {
        let _ = s.read_to_end(&mut stdout);
    }
    if !status.success() {
        let mut stderr = String::new();
        if let Some(mut s) = child.stderr.take() {
            let _ = s.read_to_string(&mut stderr);
        }
        return Err(HelperError::ExitStatus { code: status.code(), stderr });
    }
    parse_score(&stdout).ok_or(HelperError::BadOutput)
}

/// One attempt: find a fresh-enough frame, run the helper, cache the result. No-op (silently) if
/// no frame is fresh enough -- per the module doc, that is an expected, non-error outcome.
fn try_compute(foodlevel: &FoodLevel, shm: &Shm, feed: &ai::Feed, now: Instant) {
    let Some((ts, filename)) = feed.latest_scene_image() else { return };
    let age = ai::now_unix().saturating_sub(ts);
    if age > MAX_FRAME_AGE.as_secs() {
        return;
    }
    let image_path = Path::new(ai::EVENTS_DIR).join(&filename);

    foodlevel.record_attempt(now);
    match run_helper(Path::new(HELPER_PATH), Path::new(MODEL_PATH), &image_path, HELPER_TIMEOUT, shm) {
        Ok(score) => {
            let pct = (score * 100.0).round().clamp(0.0, 100.0) as u8;
            foodlevel.record_reading(Reading { pct, computed_unix: ai::now_unix() });
        }
        Err(e) => eprintln!("kibbled: foodlevel: {e}"),
    }
}

fn poll_loop(foodlevel: Arc<FoodLevel>, shm: Arc<Shm>, feed: Arc<ai::Feed>) {
    let mut was_feeding = shm.u8(off::FEEDING) != 0;
    loop {
        thread::sleep(POLL_INTERVAL);
        let feeding = shm.u8(off::FEEDING) != 0;
        let just_finished = was_feeding && !feeding;
        was_feeding = feeding;

        if !just_finished {
            let now = Instant::now();
            if should_attempt(feeding, false, foodlevel.last_attempt_at(), now, RATE_LIMIT) {
                try_compute(&foodlevel, &shm, &feed, now);
            }
            continue;
        }

        // A feed cycle just completed: give the scene (and the vendor's own detection pipeline,
        // if a cat is right there) a moment, then attempt once, bypassing the periodic cooldown
        // -- unless a new feed has *already* started again in that brief window.
        thread::sleep(SETTLE_AFTER_FEED);
        let feeding_after_settle = shm.u8(off::FEEDING) != 0;
        let now = Instant::now();
        if !feeding_after_settle
            && should_attempt(false, true, foodlevel.last_attempt_at(), now, RATE_LIMIT)
        {
            try_compute(&foodlevel, &shm, &feed, now);
        }
        // Re-sync so a feed that started during the settle sleep is observed as a fresh 0->1
        // transition next tick rather than silently lost.
        was_feeding = shm.u8(off::FEEDING) != 0;
    }
}

/// Start the background loop and return the shared handle `main.rs` routes `GET /state` against.
pub fn spawn(shm: Arc<Shm>, feed: Arc<ai::Feed>) -> Arc<FoodLevel> {
    let foodlevel = FoodLevel::new();
    let loop_handle = Arc::clone(&foodlevel);
    thread::spawn(move || poll_loop(loop_handle, shm, feed));
    foodlevel
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(secs: u64) -> Instant {
        // A fixed, arbitrary base plus an offset -- Instant has no public constructor, so tests
        // anchor everything to one `Instant::now()` call and reason only about relative offsets.
        Instant::now() + Duration::from_secs(secs)
    }

    #[test]
    fn never_attempts_while_feeding() {
        assert!(!should_attempt(true, false, None, t(1000), RATE_LIMIT));
        assert!(!should_attempt(true, true, None, t(1000), RATE_LIMIT));
        assert!(!should_attempt(true, false, Some(t(0)), t(1000), RATE_LIMIT));
    }

    #[test]
    fn just_finished_feed_bypasses_the_cooldown() {
        let now = t(1000);
        // Even an attempt recorded this very instant does not block the post-feed attempt.
        assert!(should_attempt(false, true, Some(now), now, RATE_LIMIT));
    }

    #[test]
    fn first_ever_attempt_is_allowed() {
        assert!(should_attempt(false, false, None, t(0), RATE_LIMIT));
    }

    #[test]
    fn periodic_attempt_is_rate_limited() {
        let last = t(0);
        let still_cooling = last + RATE_LIMIT - Duration::from_secs(1);
        let cooled_down = last + RATE_LIMIT;
        assert!(!should_attempt(false, false, Some(last), still_cooling, RATE_LIMIT));
        assert!(should_attempt(false, false, Some(last), cooled_down, RATE_LIMIT));
    }

    #[test]
    fn parses_a_well_formed_score() {
        assert_eq!(parse_score(b"score=0.4400\n"), Some(0.44));
        assert_eq!(parse_score(b"score=0.0000\n"), Some(0.0));
        assert_eq!(parse_score(b"score=1.0000\n"), Some(1.0));
        // No trailing newline is still one valid line.
        assert_eq!(parse_score(b"score=0.5"), Some(0.5));
    }

    #[test]
    fn clamps_tiny_float_overshoot_past_one() {
        assert_eq!(parse_score(b"score=1.0004\n"), Some(1.0));
    }

    #[test]
    fn rejects_malformed_or_out_of_range_output() {
        assert_eq!(parse_score(b""), None);
        assert_eq!(parse_score(b"garbage\n"), None);
        assert_eq!(parse_score(b"score=nan\n"), None);
        assert_eq!(parse_score(b"score=inf\n"), None);
        assert_eq!(parse_score(b"score=2.5000\n"), None);
        assert_eq!(parse_score(b"score=-1.0000\n"), None);
        // Wrong prefix entirely -- e.g. a stray log line that leaked onto stdout.
        assert_eq!(parse_score(b"kibble-food: dlopen failed\n"), None);
    }

    #[test]
    fn ignores_anything_after_the_first_line() {
        // The helper never does this (stdout is exactly one line on success), but the parser
        // itself must not be tricked by trailing noise either way.
        assert_eq!(parse_score(b"score=0.3000\nsecond line\n"), Some(0.3));
    }
}
