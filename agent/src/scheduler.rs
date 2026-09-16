//! kibbled owns the schedule's firing, not the MCU (STUDY-schedule-encoding.md §6-§8): `ctrl`
//! zeroes any usable positive countdown before it reaches the wire, and `ctrl` itself re-fetches
//! "what's next" after every feed -- host-side-scheduler behaviour, not "delegate to the MCU and
//! forget". This module is that scheduler: a background thread that, once per [`TICK_INTERVAL`],
//! asks "is any enabled entry due, in local wall-clock time, right now?" and if so calls the
//! already-proven `feed_ctrl` dispense path directly (`bus::FeedCtrl`/`msg::BLE_FEED_CTRL`, the
//! same bus message `POST /feed` sends). `schedule.rs`'s own wire write to the MCU
//! (`msg::BLE_SET_SCHEDULE`) is now purely cosmetic bookkeeping sync, not a trigger.
//!
//! ## Safety-critical failure modes (STUDY-schedule-encoding.md §11.1) -- all four handled here
//!
//! 1. **Bounded missed-fire catch-up.** [`candidate_dates`] only ever considers *today* and
//!    *yesterday*'s occurrence for each entry (never further back), and [`evaluate`] only
//!    actually dispenses when the due instant is within [`GRACE_WINDOW_SECS`] of now. Once an
//!    occurrence's date has been resolved (either way) via [`schedule::Schedule::claim_fire`] it
//!    is never revisited, and dates further back than yesterday are never even considered -- at
//!    most one late feed per entry per restart, never a pile-up, no matter how long the process
//!    was down.
//! 2. **DST/clock-change safe.** Every cycle recomputes "is this due" from the current wall
//!    clock via [`localtime::Tz::local_to_utc`] (never a cached UTC instant plus 86400) -- see
//!    that module's doc for why this device's DST rule is hardcoded rather than read from the OS
//!    (`/etc/localtime` is deleted at boot, `docs/02-boot.md`). A manual clock change is handled
//!    identically: there is no cached "next deadline" anywhere in this module, only "now".
//! 3. **Per-occurrence duplicate-fire tracking survives restarts.**
//!    [`schedule::Schedule::claim_fire`] persists, per entry id, the local calendar date of the
//!    last occurrence it resolved (dispensed or missed) to `/opt/kibble/schedule.json` -- the
//!    same file the schedule cache already lived in. A restart reloads that file before this
//!    module's thread ever runs a tick, so an occurrence already resolved before the crash is
//!    never reconsidered.
//! 4. **Record before dispense.** [`schedule::Schedule::claim_fire`] durably writes the
//!    occurrence's outcome to disk *before* returning; [`evaluate`] only calls
//!    [`Dispenser::dispense`] after `claim_fire` has returned `Ok(true)`, and never retries
//!    regardless of what dispense returns. A crash or dispense failure after that point can
//!    under-feed (bounded, safe -- item 1 recovers on the next scheduled day) but can never
//!    double-feed (the occurrence is already permanently claimed) -- STUDY-schedule-encoding.md
//!    §11.1 item 4 states this exact tradeoff as correct: "a possible extra late feed is bounded
//!    and recoverable... an unrecorded double-feed is not bounded at all."
//!
//! ## Disabled by default
//!
//! Set [`ENV_ENABLED`] to `1` or `true` to turn this on; anything else (including unset, the
//! default) leaves the whole thread unspawned -- `main.rs` never even opens the extra bus sender
//! dispensing needs. **No automated test in this project ever dispenses real food** -- every
//! test below injects a [`Dispenser`] stub; see that trait's doc for why.

use std::sync::Arc;
use std::thread;
use std::time::Duration;

use crate::bus::{msg, FeedCtrl, Sender};
use crate::localtime::{self, Civil, Tz};
use crate::schedule::{Entry, Outcome, Schedule};

/// Set to `1` or `true` (case-insensitive) to enable the scheduler. Unset, or any other value,
/// leaves it off -- see the module doc's safety section.
pub const ENV_ENABLED: &str = "KIBBLE_SCHEDULER_ENABLED";

pub fn enabled_from_env() -> bool {
    matches!(std::env::var(ENV_ENABLED).as_deref(), Ok("1") | Ok("true") | Ok("TRUE") | Ok("True"))
}

/// How late a missed fire may still be caught up (STUDY-schedule-encoding.md §11.1 item 1:
/// "recommend a few hours" -- a product decision for Nitin, not load-bearing to correctness; only
/// the *existence* of a bound is). Tune freely; nothing else in this module assumes this value.
pub const GRACE_WINDOW_SECS: i64 = 2 * 60 * 60;

/// How often the background thread re-evaluates every entry. Short enough that ordinary polling
/// latency is never mistaken for a "missed" fire (a real fire lands within one tick of due, far
/// inside [`GRACE_WINDOW_SECS`]).
pub const TICK_INTERVAL: Duration = Duration::from_secs(15);

/// Abstraction over "actually dispense food," so every decision above can be unit-tested without
/// ever touching the bus or a real device -- this project's own instruction: never dispense real
/// food during development or testing. The production impl ([`BusDispenser`]) is a thin wrapper
/// around the exact call `POST /feed` already makes (`main::feed`/`send_feed`).
pub trait Dispenser {
    fn dispense(&self, id: &str, amount_l: u8, amount_r: u8) -> Result<(), String>;
}

/// Production dispenser: `bus::FeedCtrl` over `msg::BLE_FEED_CTRL`, the same already-proven,
/// already-verified-by-dispensing bus message `POST /feed` sends.
pub struct BusDispenser {
    pub ble: Sender,
}

impl Dispenser for BusDispenser {
    fn dispense(&self, id: &str, amount_l: u8, amount_r: u8) -> Result<(), String> {
        self.ble
            .send(
                msg::BLE_FEED_CTRL,
                &FeedCtrl { cancel: false, id: id.to_string(), amount1: amount_l, amount2: amount_r }.encode(),
            )
            .map_err(|e| format!("bus send failed: {e}"))
    }
}

/// Spawns the background tick thread. Only ever called when [`enabled_from_env`] is true --
/// `main.rs` does not even open the bus sender otherwise.
pub fn spawn(schedule: Arc<Schedule>, ble: Sender) {
    thread::spawn(move || {
        let dispenser = BusDispenser { ble };
        loop {
            tick(&schedule, &dispenser, &localtime::DEVICE_TZ, now_utc());
            thread::sleep(TICK_INTERVAL);
        }
    });
}

fn now_utc() -> i64 {
    localtime::now_unix() as i64
}

/// One full pass: every enabled entry, checked against both today's and yesterday's local
/// calendar date (see the module doc's item 1 -- this bounds catch-up without ever looking back
/// further, no matter how long the process was down).
pub fn tick(schedule: &Schedule, dispenser: &dyn Dispenser, tz: &Tz, now: i64) {
    for entry in schedule.entries_snapshot().into_iter().filter(|e| e.enabled) {
        for date in candidate_dates(tz, now) {
            evaluate(schedule, dispenser, &entry, tz, date, now);
        }
    }
}

/// Yesterday and today's local calendar date, in that order -- the only two occurrences any tick
/// ever considers for one entry (see the module doc's item 1). Yesterday matters for the case
/// where a restart happens shortly after midnight but before an entry due late the previous
/// night has been caught up.
fn candidate_dates(tz: &Tz, now: i64) -> [Civil; 2] {
    let (today, _) = tz.to_local(now);
    [today.pred(), today]
}

/// Resolves exactly one (entry, calendar date) occurrence:
/// - not due yet (the local wall clock hasn't reached `entry.minute_of_day` on `date`) -> no-op;
/// - already resolved (by an earlier tick, an earlier process, or a racing thread --
///   [`schedule::Schedule::claim_fire`] is the single atomic gate) -> no-op;
/// - due, within [`GRACE_WINDOW_SECS`] -> claim as dispensed, then dispense;
/// - due, past the grace window -> claim as missed, never dispense.
fn evaluate(schedule: &Schedule, dispenser: &dyn Dispenser, entry: &Entry, tz: &Tz, date: Civil, now: i64) {
    let due_utc = tz.local_to_utc(date, entry.minute_of_day as i64 * 60);
    if now < due_utc {
        return;
    }
    let date_str = date.to_iso();
    let late_by = now - due_utc;
    let outcome = if late_by <= GRACE_WINDOW_SECS { Outcome::Dispensed } else { Outcome::Missed };
    match schedule.claim_fire(&entry.id, &date_str, outcome, now.max(0) as u64) {
        Ok(true) if outcome == Outcome::Dispensed => {
            let id = format!("kibble-sched-{}-{date_str}", entry.id);
            if let Err(e) = dispenser.dispense(&id, entry.amount_l, entry.amount_r) {
                eprintln!(
                    "kibbled: scheduler: entry {} occurrence {date_str} recorded but dispense \
                     failed: {e} -- not retried (a possible missed feed is the safe tradeoff, a \
                     double-feed is not -- STUDY-schedule-encoding.md \u{a7}11.1 item 4)",
                    entry.id
                );
            }
        }
        Ok(true) => eprintln!(
            "kibbled: scheduler: entry {} occurrence {date_str} missed ({late_by}s late, beyond \
             the {GRACE_WINDOW_SECS}s grace window) -- skipped, not dispensing",
            entry.id
        ),
        Ok(false) => {} // already resolved -- nothing to do
        Err(e) => eprintln!(
            "kibbled: scheduler: entry {} occurrence {date_str}: could not persist the fire \
             record ({e}) -- refusing to dispense",
            entry.id
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schedule::Schedule;
    use std::fs;
    use std::path::PathBuf;
    use std::sync::Mutex as StdMutex;

    fn tmp_path(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!("kibble-scheduler-test-{tag}-{}.json", std::process::id()))
    }

    fn entry(id: &str, minute_of_day: u16) -> Entry {
        Entry { id: id.into(), minute_of_day, amount_l: 1, amount_r: 1, enabled: true }
    }

    /// Records every dispense call; never touches the bus. This is what makes every test below
    /// safe to run anywhere, including CI -- no test in this module can ever cause a real feed.
    struct StubDispenser {
        calls: StdMutex<Vec<(String, u8, u8)>>,
    }

    impl StubDispenser {
        fn new() -> Self {
            StubDispenser { calls: StdMutex::new(Vec::new()) }
        }
        fn calls(&self) -> Vec<(String, u8, u8)> {
            self.calls.lock().unwrap().clone()
        }
    }

    impl Dispenser for StubDispenser {
        fn dispense(&self, id: &str, amount_l: u8, amount_r: u8) -> Result<(), String> {
            self.calls.lock().unwrap().push((id.to_string(), amount_l, amount_r));
            Ok(())
        }
    }

    // --- basic due/not-due decisions ---------------------------------------------------------

    #[test]
    fn entry_not_yet_due_today_does_not_fire() {
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("notdue");
        let schedule = Schedule::seed_for_test(path.clone(), vec![entry("dinner", 18 * 60)]);
        // Pre-resolve yesterday's occurrence so this test isolates *today's* not-yet-due
        // decision from the separate (and separately tested) missed-fire bookkeeping.
        schedule.claim_fire("dinner", "2026-01-14", Outcome::Missed, 0).unwrap();
        let dispenser = StubDispenser::new();
        let now = tz.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 17 * 3600); // 1h early
        tick(&schedule, &dispenser, &tz, now);
        assert_eq!(dispenser.calls().len(), 0);
        assert_eq!(
            schedule.fired_record("dinner").unwrap().date,
            "2026-01-14",
            "today must remain unresolved -- it is not due yet"
        );
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn disabled_entry_never_fires_even_when_overdue() {
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("disabled");
        let mut e = entry("off", 7 * 60);
        e.enabled = false;
        let schedule = Schedule::seed_for_test(path.clone(), vec![e]);
        let dispenser = StubDispenser::new();
        let due = tz.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 7 * 3600);
        tick(&schedule, &dispenser, &tz, due + 3600);
        assert_eq!(dispenser.calls().len(), 0);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn exactly_due_now_fires() {
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("exact");
        let schedule = Schedule::seed_for_test(path.clone(), vec![entry("breakfast", 7 * 60)]);
        let dispenser = StubDispenser::new();
        let due = tz.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 7 * 3600);
        tick(&schedule, &dispenser, &tz, due);
        assert_eq!(dispenser.calls(), vec![("kibble-sched-breakfast-2026-01-15".to_string(), 1, 1)]);
        let _ = fs::remove_file(&path);
    }

    // --- requirement 1: bounded missed-fire catch-up ("10 seconds late" vs "10 hours late") --

    #[test]
    fn missed_by_10_seconds_still_dispenses() {
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("missed10s");
        let schedule = Schedule::seed_for_test(path.clone(), vec![entry("breakfast", 7 * 60)]);
        let dispenser = StubDispenser::new();
        let due = tz.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 7 * 3600);
        tick(&schedule, &dispenser, &tz, due + 10);
        assert_eq!(dispenser.calls().len(), 1, "10s late is well inside the grace window");
        assert_eq!(schedule.fired_record("breakfast").unwrap().outcome, Outcome::Dispensed);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn missed_by_10_hours_is_skipped_not_dispensed() {
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("missed10h");
        let schedule = Schedule::seed_for_test(path.clone(), vec![entry("breakfast", 7 * 60)]);
        let dispenser = StubDispenser::new();
        let due = tz.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 7 * 3600);
        tick(&schedule, &dispenser, &tz, due + 10 * 3600);
        assert_eq!(dispenser.calls().len(), 0, "10h late is well beyond the grace window");
        assert_eq!(schedule.fired_record("breakfast").unwrap().outcome, Outcome::Missed);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn missed_occurrence_is_never_retried_on_a_later_tick_the_same_day() {
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("missedlatch");
        let schedule = Schedule::seed_for_test(path.clone(), vec![entry("breakfast", 7 * 60)]);
        let dispenser = StubDispenser::new();
        let due = tz.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 7 * 3600);
        tick(&schedule, &dispenser, &tz, due + 10 * 3600); // missed, recorded
        tick(&schedule, &dispenser, &tz, due + 11 * 3600); // a later tick, same day
        assert_eq!(dispenser.calls().len(), 0, "a missed occurrence must stay missed, never dispensed late");
        let _ = fs::remove_file(&path);
    }

    // --- requirement 2: DST-safe ---------------------------------------------------------------

    #[test]
    fn dst_transition_uses_the_correct_local_instant_not_a_flat_86400_step() {
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("dst");
        let schedule = Schedule::seed_for_test(path.clone(), vec![entry("evening", 17 * 60 + 25)]);
        let dispenser = StubDispenser::new();

        let before = tz.local_to_utc(Civil { year: 2026, month: 3, day: 7 }, 17 * 3600 + 25 * 60);
        tick(&schedule, &dispenser, &tz, before);
        assert_eq!(dispenser.calls().len(), 1, "the pre-transition occurrence must fire");

        let correct_next = tz.local_to_utc(Civil { year: 2026, month: 3, day: 9 }, 17 * 3600 + 25 * 60);
        let naive_next = before + 2 * 86_400; // what flat day-arithmetic would (wrongly) expect
        assert_eq!(naive_next - correct_next, 3600, "sanity: DST must shift these by exactly one hour");

        tick(&schedule, &dispenser, &tz, correct_next - 1);
        assert_eq!(dispenser.calls().len(), 1, "must not fire the next occurrence a second early");

        tick(&schedule, &dispenser, &tz, correct_next);
        assert_eq!(dispenser.calls().len(), 2, "must fire exactly at the true, DST-adjusted local 17:25");
        let _ = fs::remove_file(&path);
    }

    // --- requirement 3+4: duplicate suppression across restart, record-before-dispense -------

    #[test]
    fn record_is_durably_written_before_dispense_is_called() {
        struct AssertRecordedFirst<'a> {
            schedule: &'a Schedule,
            calls: StdMutex<usize>,
        }
        impl<'a> Dispenser for AssertRecordedFirst<'a> {
            fn dispense(&self, _id: &str, _l: u8, _r: u8) -> Result<(), String> {
                let record = self
                    .schedule
                    .fired_record("breakfast")
                    .expect("the fire record must already be persisted when dispense is called");
                assert_eq!(record.date, "2026-01-15");
                assert_eq!(record.outcome, Outcome::Dispensed);
                *self.calls.lock().unwrap() += 1;
                Ok(())
            }
        }
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("recordfirst");
        let schedule = Schedule::seed_for_test(path.clone(), vec![entry("breakfast", 7 * 60)]);
        let due = tz.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 7 * 3600);
        let dispenser = AssertRecordedFirst { schedule: &schedule, calls: StdMutex::new(0) };
        tick(&schedule, &dispenser, &tz, due);
        assert_eq!(*dispenser.calls.lock().unwrap(), 1, "dispense must actually have run, not been skipped");
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn restart_after_a_crash_between_record_and_dispense_never_double_feeds() {
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("restartmid");
        let due = tz.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 7 * 3600);
        {
            // First "process": claims the occurrence, then simulates a crash by never actually
            // calling dispense (that call is exactly what a crash right after the record write
            // would prevent).
            let schedule = Schedule::seed_for_test(path.clone(), vec![entry("breakfast", 7 * 60)]);
            assert!(schedule.claim_fire("breakfast", "2026-01-15", Outcome::Dispensed, due as u64).unwrap());
        }
        // Second "process": fresh `Schedule` reloaded from the same file, ticking again.
        let schedule = Schedule::load(path.clone()).unwrap();
        let dispenser = StubDispenser::new();
        tick(&schedule, &dispenser, &tz, due + 5);
        assert_eq!(
            dispenser.calls().len(),
            0,
            "an occurrence already recorded before the crash must never be retried after restart"
        );
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn duplicate_fire_is_suppressed_across_a_full_restart() {
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("duprestart");
        let due = tz.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 7 * 3600);
        {
            let schedule = Schedule::seed_for_test(path.clone(), vec![entry("breakfast", 7 * 60)]);
            let dispenser = StubDispenser::new();
            tick(&schedule, &dispenser, &tz, due);
            assert_eq!(dispenser.calls().len(), 1);
        }
        let schedule = Schedule::load(path.clone()).unwrap();
        let dispenser = StubDispenser::new();
        tick(&schedule, &dispenser, &tz, due + 300);
        assert_eq!(dispenser.calls().len(), 0, "restart must not re-fire an occurrence already dispensed");
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn dispense_failure_is_not_retried_the_occurrence_stays_claimed() {
        struct FailingDispenser {
            calls: StdMutex<usize>,
        }
        impl Dispenser for FailingDispenser {
            fn dispense(&self, _id: &str, _l: u8, _r: u8) -> Result<(), String> {
                *self.calls.lock().unwrap() += 1;
                Err("simulated bus failure".into())
            }
        }
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("dispensefail");
        let schedule = Schedule::seed_for_test(path.clone(), vec![entry("breakfast", 7 * 60)]);
        let dispenser = FailingDispenser { calls: StdMutex::new(0) };
        let due = tz.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 7 * 3600);
        tick(&schedule, &dispenser, &tz, due);
        assert_eq!(*dispenser.calls.lock().unwrap(), 1, "dispense must have been attempted exactly once");
        tick(&schedule, &dispenser, &tz, due + 5);
        assert_eq!(*dispenser.calls.lock().unwrap(), 1, "a failed dispense must never be retried");
        assert_eq!(schedule.fired_record("breakfast").unwrap().outcome, Outcome::Dispensed);
        let _ = fs::remove_file(&path);
    }

    // --- two fires racing ----------------------------------------------------------------------

    #[test]
    fn two_concurrent_ticks_racing_the_same_due_entry_dispense_exactly_once() {
        let path = tmp_path("tickrace");
        let schedule = Arc::new(Schedule::seed_for_test(path.clone(), vec![entry("breakfast", 7 * 60)]));
        let dispenser = Arc::new(StubDispenser::new());
        let due = localtime::DEVICE_TZ.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 7 * 3600);
        let handles: Vec<_> = (0..8)
            .map(|_| {
                let schedule = Arc::clone(&schedule);
                let dispenser = Arc::clone(&dispenser);
                std::thread::spawn(move || tick(&schedule, dispenser.as_ref(), &localtime::DEVICE_TZ, due))
            })
            .collect();
        for h in handles {
            h.join().unwrap();
        }
        assert_eq!(dispenser.calls().len(), 1, "exactly one of the racing ticks must have actually dispensed");
        let _ = fs::remove_file(&path);
    }

    // --- "next occurrence" actually advances (never refires the same day from a later day) ----

    #[test]
    fn scheduler_advances_to_the_next_days_occurrence_without_refiring_the_prior_one() {
        let tz = localtime::DEVICE_TZ;
        let path = tmp_path("advance");
        let schedule = Schedule::seed_for_test(path.clone(), vec![entry("breakfast", 7 * 60)]);
        let dispenser = StubDispenser::new();
        let day1_due = tz.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 7 * 3600);
        tick(&schedule, &dispenser, &tz, day1_due);
        assert_eq!(dispenser.calls().len(), 1, "day 1's occurrence must fire");

        // Re-ticking later the same day must never fire again.
        tick(&schedule, &dispenser, &tz, day1_due + 3600);
        assert_eq!(dispenser.calls().len(), 1, "must not refire the same day's already-resolved occurrence");

        // Fast-forward (no real waiting) to the next day's same local wall-clock time and confirm
        // the scheduler recomputes and fires it -- "next occurrence" genuinely advances instead
        // of getting stuck refusing day 1 forever, or somehow refiring day 1.
        let day2_due = localtime::next_occurrence_utc(&tz, 7 * 60, day1_due + 1);
        assert_eq!(day2_due - day1_due, 86_400, "sanity: no DST transition between these two dates");
        tick(&schedule, &dispenser, &tz, day2_due);
        assert_eq!(dispenser.calls().len(), 2, "day 2's occurrence must fire as a fresh, distinct occurrence");
        assert_eq!(schedule.fired_record("breakfast").unwrap().date, "2026-01-16");
        let _ = fs::remove_file(&path);
    }

    // --- config flag ----------------------------------------------------------------------------

    #[test]
    fn enabled_from_env_reads_the_documented_flag() {
        // Only this test touches KIBBLE_SCHEDULER_ENABLED, so it's safe under parallel test
        // execution -- nothing else in this crate reads or writes this specific variable.
        unsafe {
            std::env::remove_var(ENV_ENABLED);
        }
        assert!(!enabled_from_env(), "unset must default to disabled");
        unsafe {
            std::env::set_var(ENV_ENABLED, "0");
        }
        assert!(!enabled_from_env(), "any value other than the documented ones stays disabled");
        unsafe {
            std::env::set_var(ENV_ENABLED, "1");
        }
        assert!(enabled_from_env());
        unsafe {
            std::env::set_var(ENV_ENABLED, "true");
        }
        assert!(enabled_from_env());
        unsafe {
            std::env::remove_var(ENV_ENABLED);
        }
    }
}
