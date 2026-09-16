//! Local wall-clock time, computed independently of the OS's own timezone database.
//!
//! ## Why this exists instead of `libc::localtime_r`
//!
//! `docs/02-boot.md` (the boot script disassembly) shows the vendor's own `system_init.sh`
//! running `rm -rf /etc/localtime` on every boot -- there is no populated zoneinfo file on this
//! device, deliberately. Calling the OS's own `localtime_r`/`mktime` (even via a raw `extern "C"`
//! declaration, the pattern this project already uses for `mq_open`/`flock` rather than pulling in
//! the `libc` crate) would silently compute everything as UTC, not the device's real, configured
//! zone -- wrong, and wrong in a way a unit test running on a developer machine (which usually
//! *does* have `/etc/localtime`) would never catch. So this module hardcodes the one DST rule this
//! device actually needs and computes it in pure, dependency-free, fully unit-testable Rust.
//!
//! **The zone:** `docs/07-config.md` and `appendix-config-layout.json` both independently confirm
//! `config_shm`'s own `usr.user_info.timezone_name` reads `"America/New_York"` (and
//! `usr.user_info.timezone` reads the matching `-4.0` float, EDT, in the same live capture) --
//! this is Nitin's real, configured zone, not a guess. `docs/28-schedule-encoding.md` §11.1 item 2
//! requires DST-safe local-time arithmetic; [`DEVICE_TZ`] plus [`Tz::local_to_utc`] is that
//! arithmetic, re-derived from the current wall clock on every call rather than ever caching a
//! fixed UTC instant.
//!
//! **The rule:** DST runs from 02:00 local standard time on the second Sunday of March to 02:00
//! local daylight time on the first Sunday of November (the U.S. Energy Policy Act of 2005,
//! in effect every year since 2007 -- no exceptions or year-dependent variation to account for).
//! If this device is ever reconfigured to a different zone, [`DEVICE_TZ`] is the one constant to
//! change; nothing else in this module is US-specific (`Tz` itself is a plain fixed-offset-plus-
//! one-DST-rule type, not hardcoded to any particular zone).
//!
//! ## The two edge cases every DST rule has, resolved explicitly (not left to chance)
//!
//! - **The repeated hour** (fall back, November): a local wall-clock reading in the last hour of
//!   daylight time occurs twice -- once while still on daylight time, again an hour later once
//!   the clock has fallen back to standard time. [`Tz::local_to_utc`] resolves this to the
//!   *earlier* (still-daylight) instant.
//! - **The skipped hour** (spring forward, March): a local wall-clock reading in that hour never
//!   occurs at all -- the clock jumps straight over it. [`Tz::local_to_utc`] resolves this to the
//!   instant the real clock would read that time *after* the jump (i.e. the later of the two
//!   candidate instants) -- equivalently, "fire once the clock has caught up," never before the
//!   jump has actually happened.
//!
//! Both are exercised directly in this module's tests with concrete, independently-verified
//! (Python `zoneinfo`-cross-checked) timestamps -- see the `dst_edge_cases` test group.

use std::time::{SystemTime, UNIX_EPOCH};

/// A proleptic-Gregorian calendar date, independent of any timezone.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Civil {
    pub year: i64,
    pub month: u32,
    pub day: u32,
}

impl Civil {
    /// Days since the Unix epoch (1970-01-01) to a civil date. Howard Hinnant's well-known
    /// `days_from_civil` algorithm (public domain, `howardhinnant.github.io/date_algorithms.html`)
    /// -- correct for the entire proleptic Gregorian calendar, not just post-1970 dates.
    pub fn epoch_day(&self) -> i64 {
        let y = if self.month <= 2 { self.year - 1 } else { self.year };
        let era = if y >= 0 { y } else { y - 399 } / 400;
        let yoe = y - era * 400; // [0, 399]
        let mp = (self.month as i64 + 9) % 12; // [0, 11], Mar=0 .. Feb=11
        let doy = (153 * mp + 2) / 5 + self.day as i64 - 1; // [0, 365]
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
        era * 146097 + doe - 719468
    }

    /// Inverse of [`Civil::epoch_day`].
    pub fn from_epoch_day(z: i64) -> Civil {
        let z = z + 719468;
        let era = if z >= 0 { z } else { z - 146096 } / 146097;
        let doe = z - era * 146097; // [0, 146096]
        let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
        let y = yoe + era * 400;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
        let mp = (5 * doy + 2) / 153; // [0, 11]
        let day = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
        let month = (if mp < 10 { mp + 3 } else { mp - 9 }) as u32; // [1, 12]
        let year = if month <= 2 { y + 1 } else { y };
        Civil { year, month, day }
    }

    pub fn pred(&self) -> Civil {
        Civil::from_epoch_day(self.epoch_day() - 1)
    }

    pub fn succ(&self) -> Civil {
        Civil::from_epoch_day(self.epoch_day() + 1)
    }

    /// 0=Sunday .. 6=Saturday. 1970-01-01 (epoch day 0) was a Thursday.
    pub fn weekday(&self) -> u32 {
        (self.epoch_day() + 4).rem_euclid(7) as u32
    }

    /// The `n`th (1-based) occurrence of `weekday` (0=Sunday..6=Saturday) in `year`/`month`.
    fn nth_weekday(year: i64, month: u32, weekday: u32, n: i64) -> Civil {
        let first = Civil { year, month, day: 1 };
        let delta = (weekday + 7 - first.weekday()) % 7;
        Civil::from_epoch_day(first.epoch_day() + delta as i64 + (n - 1) * 7)
    }

    pub fn to_iso(&self) -> String {
        format!("{:04}-{:02}-{:02}", self.year, self.month, self.day)
    }
}

/// A fixed standard-time offset plus (optionally) one DST rule. The only rule this module
/// implements is the U.S. one (see the module doc); a zone that never observes DST just sets
/// `dst_offset_secs == std_offset_secs`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Tz {
    pub std_offset_secs: i64,
    pub dst_offset_secs: i64,
}

/// This device's real, confirmed zone -- see the module doc for the evidence.
pub const DEVICE_TZ: Tz = Tz { std_offset_secs: -5 * 3600, dst_offset_secs: -4 * 3600 };

impl Tz {
    fn observes_dst(&self) -> bool {
        self.dst_offset_secs != self.std_offset_secs
    }

    /// UTC instants (start, end) of the DST period within `year`, per the U.S. rule: 2nd Sunday
    /// of March at 02:00 local standard time, to 1st Sunday of November at 02:00 local daylight
    /// time.
    fn dst_range_utc(&self, year: i64) -> (i64, i64) {
        let start = Civil::nth_weekday(year, 3, 0, 2);
        let end = Civil::nth_weekday(year, 11, 0, 1);
        let start_utc = start.epoch_day() * 86_400 + 2 * 3600 - self.std_offset_secs;
        let end_utc = end.epoch_day() * 86_400 + 2 * 3600 - self.dst_offset_secs;
        (start_utc, end_utc)
    }

    /// Whether DST is in effect at UTC instant `utc`.
    pub fn is_dst(&self, utc: i64) -> bool {
        if !self.observes_dst() {
            return false;
        }
        // Approximate the local *standard-time* year to know which year's transition pair to
        // use. Safe because the U.S. DST window sits well inside the calendar year on both ends
        // (March-to-November) -- it is never active across a New Year's boundary, so a
        // standard-time-shifted day count can never land in the wrong year here.
        let approx_year = Civil::from_epoch_day((utc + self.std_offset_secs).div_euclid(86_400)).year;
        let (start, end) = self.dst_range_utc(approx_year);
        utc >= start && utc < end
    }

    pub fn offset_at(&self, utc: i64) -> i64 {
        if self.is_dst(utc) {
            self.dst_offset_secs
        } else {
            self.std_offset_secs
        }
    }

    /// UTC instant -> local (calendar date, second-of-day).
    pub fn to_local(&self, utc: i64) -> (Civil, i64) {
        let local = utc + self.offset_at(utc);
        let day = local.div_euclid(86_400);
        let sod = local.rem_euclid(86_400);
        (Civil::from_epoch_day(day), sod)
    }

    /// Inverse of [`Tz::to_local`]: the UTC instant at which the local wall clock reads
    /// `second_of_day` on `date`. See the module doc for how the repeated/skipped hour at each
    /// DST transition are resolved.
    pub fn local_to_utc(&self, date: Civil, second_of_day: i64) -> i64 {
        let naive = date.epoch_day() * 86_400 + second_of_day;
        let as_std = naive - self.std_offset_secs;
        if !self.observes_dst() {
            return as_std;
        }
        let as_dst = naive - self.dst_offset_secs;
        let std_consistent = !self.is_dst(as_std);
        let dst_consistent = self.is_dst(as_dst);
        match (std_consistent, dst_consistent) {
            (true, false) => as_std,
            (false, true) => as_dst,
            // Repeated hour (fall back): both interpretations are internally consistent --
            // resolve to the earlier (still-daylight, first) instant.
            (true, true) => as_std.min(as_dst),
            // Skipped hour (spring forward): neither interpretation is consistent -- resolve to
            // the later instant, i.e. "once the clock has actually caught up to this reading".
            (false, false) => as_std.max(as_dst),
        }
    }
}

/// The next UTC instant, strictly after `now_utc`'s current occurrence (or at/after `now_utc`
/// if today's occurrence hasn't happened yet), that the local wall clock reads `minute_of_day`.
/// Pure "what's the next scheduled run" query -- independent of whether any past occurrence was
/// ever actually resolved (see `scheduler.rs` for that bookkeeping).
pub fn next_occurrence_utc(tz: &Tz, minute_of_day: u16, now_utc: i64) -> i64 {
    let (today, _) = tz.to_local(now_utc);
    let today_due = tz.local_to_utc(today, minute_of_day as i64 * 60);
    if today_due >= now_utc {
        today_due
    } else {
        tz.local_to_utc(today.succ(), minute_of_day as i64 * 60)
    }
}

/// Current Unix time, or 0 if the clock is somehow before the epoch (mirrors `schedule::now_unix`
/// exactly; kept as a separate copy so this module has zero dependency on `schedule.rs`).
pub fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- civil date <-> epoch day ----------------------------------------------------------

    #[test]
    fn epoch_day_known_values() {
        assert_eq!(Civil { year: 1970, month: 1, day: 1 }.epoch_day(), 0);
        assert_eq!(Civil { year: 1969, month: 12, day: 31 }.epoch_day(), -1);
        assert_eq!(Civil { year: 2026, month: 1, day: 15 }.epoch_day(), 20468);
        assert_eq!(Civil { year: 2026, month: 9, day: 16 }.epoch_day(), 20712);
        // Leap day: 2000 is a leap year (divisible by 400).
        assert_eq!(Civil { year: 2000, month: 2, day: 28 }.epoch_day(), 11015);
        assert_eq!(Civil { year: 2000, month: 2, day: 29 }.epoch_day(), 11016);
        assert_eq!(Civil { year: 2000, month: 3, day: 1 }.epoch_day(), 11017);
    }

    #[test]
    fn epoch_day_round_trips_across_a_wide_range() {
        // Every day across ~30 years, both directions -- would catch any off-by-one in either
        // the era/century leap-year handling or the inverse.
        for z in -5000..15000 {
            let c = Civil::from_epoch_day(z);
            assert_eq!(c.epoch_day(), z, "round trip failed for epoch day {z} -> {c:?}");
        }
    }

    #[test]
    fn weekday_matches_known_days() {
        assert_eq!(Civil { year: 1970, month: 1, day: 1 }.weekday(), 4); // Thursday
        assert_eq!(Civil { year: 2026, month: 3, day: 8 }.weekday(), 0); // Sunday (verified below)
        assert_eq!(Civil { year: 2026, month: 11, day: 1 }.weekday(), 0); // Sunday
    }

    #[test]
    fn nth_weekday_finds_the_real_2026_dst_transition_dates() {
        // Independently verified against Python's `calendar`/`zoneinfo`.
        assert_eq!(Civil::nth_weekday(2026, 3, 0, 2), Civil { year: 2026, month: 3, day: 8 });
        assert_eq!(Civil::nth_weekday(2026, 11, 0, 1), Civil { year: 2026, month: 11, day: 1 });
    }

    // --- local_to_utc: ordinary days, cross-checked against Python zoneinfo -----------------

    #[test]
    fn matches_the_studys_own_worked_example() {
        // STUDY-schedule-encoding.md's own specimen: 2026-09-16 17:25:00 America/New_York, EDT.
        // Independently verified: `datetime(2026,9,16,17,25,tzinfo=ZoneInfo("America/New_York"))
        // .timestamp()` == 1789593900.
        let date = Civil { year: 2026, month: 9, day: 16 };
        assert_eq!(DEVICE_TZ.local_to_utc(date, 17 * 3600 + 25 * 60), 1_789_593_900);
        assert!(DEVICE_TZ.is_dst(1_789_593_900), "September must be DST");
    }

    #[test]
    fn plain_winter_day_is_standard_time() {
        let date = Civil { year: 2026, month: 1, day: 15 };
        assert_eq!(DEVICE_TZ.local_to_utc(date, 7 * 3600 + 30 * 60), 1_768_480_200);
        assert!(!DEVICE_TZ.is_dst(1_768_480_200));
    }

    #[test]
    fn to_local_is_the_exact_inverse_of_local_to_utc_on_ordinary_days() {
        let date = Civil { year: 2026, month: 9, day: 16 };
        let sod = 17 * 3600 + 25 * 60;
        let utc = DEVICE_TZ.local_to_utc(date, sod);
        assert_eq!(DEVICE_TZ.to_local(utc), (date, sod));
    }

    // --- DST-safety: no drift across the transition (STUDY-schedule-encoding.md \u00a711.1 item 2) --

    #[test]
    fn same_local_time_of_day_stays_the_same_local_time_across_the_spring_transition() {
        // 2026-03-07 (before "spring forward") and 2026-03-09 (after) both at 17:25 local --
        // independently verified against Python zoneinfo: 1772922300 and 1773091500.
        let before = DEVICE_TZ.local_to_utc(Civil { year: 2026, month: 3, day: 7 }, 17 * 3600 + 25 * 60);
        let after = DEVICE_TZ.local_to_utc(Civil { year: 2026, month: 3, day: 9 }, 17 * 3600 + 25 * 60);
        assert_eq!(before, 1_772_922_300);
        assert_eq!(after, 1_773_091_500);
        // Two calendar days apart, minus the one hour "spring forward" loses -- proves this is
        // real timezone-aware arithmetic, not a cached UTC instant plus a flat 2*86400.
        assert_eq!(after - before, 2 * 86_400 - 3600);
        // And both, converted back, really do read 17:25 local -- the whole point of item 2.
        assert_eq!(DEVICE_TZ.to_local(before).1, 17 * 3600 + 25 * 60);
        assert_eq!(DEVICE_TZ.to_local(after).1, 17 * 3600 + 25 * 60);
    }

    #[test]
    fn same_local_time_of_day_stays_the_same_local_time_across_the_fall_transition() {
        let before = DEVICE_TZ.local_to_utc(Civil { year: 2026, month: 10, day: 30 }, 17 * 3600 + 25 * 60);
        let after = DEVICE_TZ.local_to_utc(Civil { year: 2026, month: 11, day: 2 }, 17 * 3600 + 25 * 60);
        // Three calendar days apart, plus the one hour "fall back" gains.
        assert_eq!(after - before, 3 * 86_400 + 3600);
        assert_eq!(DEVICE_TZ.to_local(before).1, 17 * 3600 + 25 * 60);
        assert_eq!(DEVICE_TZ.to_local(after).1, 17 * 3600 + 25 * 60);
    }

    // --- DST edge cases: the skipped and repeated hour, resolved explicitly ----------------

    #[test]
    fn dst_edge_cases_repeated_fall_back_hour_resolves_to_the_earlier_instant() {
        // 2026-11-01 01:30 local occurs twice. Independently verified against Python zoneinfo:
        // fold=0 (still-EDT, first) == 1793511000; fold=1 (already-EST, second) == 1793514600.
        let date = Civil { year: 2026, month: 11, day: 1 };
        let resolved = DEVICE_TZ.local_to_utc(date, 1 * 3600 + 30 * 60);
        assert_eq!(resolved, 1_793_511_000, "must resolve to the earlier (still-daylight) instant");
        assert!(DEVICE_TZ.is_dst(resolved), "the earlier instant is still on daylight time");
    }

    #[test]
    fn dst_edge_cases_skipped_spring_forward_hour_resolves_to_the_later_instant() {
        // 2026-03-08 02:30 local never happens (clocks jump 02:00 -> 03:00). Independently
        // computed by hand-replicating this exact algorithm in Python and cross-checked against
        // the real transition instant (1_772_953_200, i.e. 02:00:00 EST becomes 03:00:00 EDT).
        let date = Civil { year: 2026, month: 3, day: 8 };
        let resolved = DEVICE_TZ.local_to_utc(date, 2 * 3600 + 30 * 60);
        assert_eq!(resolved, 1_772_955_000);
        assert!(resolved > 1_772_953_200, "must resolve to after the real transition instant, never before it");
    }

    // --- next_occurrence_utc -----------------------------------------------------------------

    #[test]
    fn next_occurrence_is_today_if_still_ahead_else_tomorrow() {
        // "now" = 2026-01-15 08:00:00 local.
        let now = DEVICE_TZ.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 8 * 3600);
        assert_eq!(now, 1_768_482_000);
        // An 07:00 entry already passed today -> next occurrence is tomorrow's 07:00.
        assert_eq!(next_occurrence_utc(&DEVICE_TZ, 7 * 60, now), 1_768_564_800);
        // A 09:00 entry hasn't happened yet today -> next occurrence is today's 09:00.
        assert_eq!(next_occurrence_utc(&DEVICE_TZ, 9 * 60, now), 1_768_485_600);
    }

    #[test]
    fn next_occurrence_right_at_the_boundary_is_still_today() {
        let now = DEVICE_TZ.local_to_utc(Civil { year: 2026, month: 1, day: 15 }, 8 * 3600);
        // An entry due at exactly "now" has not yet strictly passed -> still today (a caller
        // evaluating "is it due" at this exact instant should see it as due-now, not tomorrow).
        assert_eq!(next_occurrence_utc(&DEVICE_TZ, 8 * 60, now), now);
    }

    // --- a zone with no DST rule at all behaves as plain fixed offset -----------------------

    #[test]
    fn zone_without_dst_never_reports_dst_and_never_shifts() {
        let tz = Tz { std_offset_secs: -6 * 3600, dst_offset_secs: -6 * 3600 };
        let utc = tz.local_to_utc(Civil { year: 2026, month: 7, day: 4 }, 12 * 3600);
        assert!(!tz.is_dst(utc));
        assert_eq!(tz.to_local(utc), (Civil { year: 2026, month: 7, day: 4 }, 12 * 3600));
    }
}
