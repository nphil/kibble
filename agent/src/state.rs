//! Device state, read from the shared-memory config the vendor processes all map.
//!
//! `/dev/shm/config_shm` is 11952 bytes: a `usr` section (4664 B of user settings), then `dev`
//! (228 B of identity) and `state` (7060 B of live telemetry). Offsets below are the ones
//! confirmed either from `pktool`'s own field-name logging or by observing a value change on the
//! device; see STUDY-config.md and STUDY-feedtest.md.
//!
//! We map it read-only. Writes go through the owning process via the bus, never by poking bytes:
//! the vendor guards this file with a `flock` on `/tmp/config.lock` and caches values in-process.

use std::fs::File;
use std::io;
use std::os::raw::{c_int, c_void};
use std::os::unix::io::AsRawFd;

pub const SHM_PATH: &str = "/dev/shm/config_shm";
pub const SHM_LEN: usize = 11952;

pub mod off {
    /// usr section
    pub const DESICCANT_DAYS: usize = 4656;
    pub const VOLUME: usize = 4664;
    /// `usr.user_info.timezone_name`, IANA zone string (e.g. `"America/New_York"`) --
    /// `docs/07-config.md`/`appendix-config-layout.json`, confidence HIGH. Field capacity is
    /// 24 bytes; the confirmed live sample fills 16 of them.
    pub const TIMEZONE_NAME: usize = 4388;
    /// dev section
    pub const SERIAL: usize = 4768;
    pub const FIRMWARE: usize = 4860;
    pub const BLE_FIRMWARE: usize = 4876;
    /// state section
    pub const BOWL_FILL_1: usize = 9916; // u32; 0xffffffff while a feed is in flight
    /// NOT a second side of the bowl. `media`'s `eat_event_start_signal` copies `BOWL_FILL_1`
    /// here the moment a meal begins (0x1de6a-0x1de76), so it reads "bowl fill when the last
    /// meal started" -- the vision model produces exactly one number per run (docs/34 Part 10).
    /// Mapped for completeness; kibbled does not report it.
    pub const BOWL_FILL_AT_MEAL_START: usize = 9920; // u32
    /// Named "event counter" when first mapped; now known to be `ble`'s count of MCU feed-log
    /// records still awaiting `ctrl`'s `0x600f` ack (`ble` increments it at 0x146f2/0x14b66 on
    /// a FEED_LOG report, decrements it in `dispatch_handler_ble_res_feed_log` at 0x174b4).
    /// It reads 4 on a device where every feed works, so a non-zero value is not a fault.
    pub const EVENT_COUNTER: usize = 10184;
    /// Per-hopper food-level sensor, mirrored byte-for-byte from the T31 MCU's own UART status
    /// frame (frame byte +7 for hopper 1, +8 for hopper 2) by `ble`'s frame-receive handler
    /// (`ble` vaddr `0x18408`): an unconditional word-copy loop at `0x18644`-`0x1865e` writes
    /// frame[0..12) into `config_shm[10228..10240)`, which places frame+7/+8 at exactly these
    /// two offsets (cross-checked against two already-known neighbours copied by the same loop:
    /// frame+5 -> `config_shm[10233]`, frame+10 -> `off::FEEDING`). u8, three observed levels --
    /// 0 = empty, 1 = low, 2 = full/ok -- not a boolean despite the Table A debug-string name
    /// `state.ble.sta_data.food1_lack`/`food2_lack` (docs/07-config.md); `0xff` is a boot-time
    /// "never reported yet" sentinel (`ble` tests `== 0xff` at `0x184a4`/`0x186b6`; `ctrl`'s own
    /// reset-to-defaults path writes the literal `0xff` into `FOOD_2` at `0x88ac8`). The `< 2`
    /// threshold is disassembly-proven, not assumed: `ctrl`'s low-food tone-alarm gate (vaddr
    /// `0x8e208`-`0x8e21c`) fires whenever `food1 == 0 || food2 <= 1 || food1 == 1`, and `ble`'s
    /// own warning-flag setter/clearer agrees exactly (sets `config_shm[9976]` at `0x14a8e`-
    /// `0x14ab6` under the identical condition; clears it at `0x186bc`-`0x186dc` only when
    /// `food1 == 2 && food2 == 2`). Live-read 2026-09-17: both bytes = 2 (both hoppers stocked).
    /// See docs/07-config.md and docs/appendix-config-layout.json for the full evidence trail.
    pub const FOOD_1: usize = 10235; // u8; frame+7; 0/1/2, 0xff = unset
    pub const FOOD_2: usize = 10236; // u8; frame+8; 0/1/2, 0xff = unset
    /// Transient "a feed cycle is running" flag: 0 -> 1 -> 0 around a dispense.
    pub const FEEDING: usize = 10238;
    /// `media`'s own "a pet is eating right now" flag, u8: `eat_event_start_signal` stores 1
    /// (media 0x1df10, right after it writes `/tmp/fPre_eat.jpeg` and just before it sends
    /// ctrl the `E_EVT_RPT_TYPE_PET_EAT_START` 0x1002), `eat_event_over_signal` stores 0
    /// (0x1e110). ctrl reports it to the cloud as `"eating"`. This, not the JPEG, is what
    /// `ai.rs` derives the `"eat"` event from: the picture lives ~35 ms before ctrl consumes
    /// and removes it (docs/34 Part 9), the flag lives for the whole meal.
    pub const EATING: usize = 2960;
    /// Watchdog liveness counters, one u32 per supervised process, followed at `slot + 0x20`
    /// by that process's pid. Live series (2026-09-16, 45 samples at 2 s): each owned slot
    /// cycles 0/1/2 -- a small counter, not the 0<->1 toggle the first study guessed -- and
    /// the pid words name the owners: 10316=204 media, 10320=212 ctrl, 10332=271 cloud,
    /// 10336=203 ble, 10344=272 logUpload. Four of the five slots were mislabelled before.
    /// `ALIVE_AGORA` is by elimination (its pid word reads 0); 10292/10308 are the dead
    /// card/p2p slots. Stale for ~60 s -> the watchdog restarts (or, for `ctrl` past 30 min
    /// of uptime, reboots) -- study/WatchdogStudy.md.
    pub const ALIVE_MEDIA: usize = 10284;
    pub const ALIVE_CTRL: usize = 10288;
    pub const ALIVE_AGORA: usize = 10296;
    pub const ALIVE_CLOUD: usize = 10300;
    pub const ALIVE_BLE: usize = 10304;
    pub const ALIVE_LOGUPLOAD: usize = 10312;
    /// `g_config + 0x2880`: the block `media`'s `petkit_event_result_callback` fills with the
    /// vendor's own pet-identification result and `ctrl` reads to build its `pet_id`-carrying
    /// cloud event (study/EventStruct.md §2.3) -- see [`super::PetTrack`]. Confirmed live on
    /// 2026-09-16: `pet_id` here equalled the single enrolled `petId` in
    /// `/opt/pet_name_color.json` with a `start_time` 169 s after a `visit` crop.
    pub const PET_TRACK: usize = 10368;
    /// `ctrl`'s own cloud/IoT connection state word: `-1` never/failed, `2` connecting,
    /// `1` connected (writers at ctrl 0x27f6c/0x27e1e/0x28080, docs/35). Read by `media` as a
    /// gate on its "cloud" detectors: `media` 0x20750 invalidates `BOWL_FILL_1` and 0x2092c
    /// skips the food model unless this is 1 or 2; `check_algo_status` (0x2408e) needs `> 0`.
    /// Also `ctrl`'s own 180s Wi-Fi power-cycle gate fires only while it is `-1`. With the
    /// cloud blackholed ctrl leaves it at `-1` forever, which is why bowl fill never refreshed
    /// and the radio reset every 3 minutes; kibbled holds it at `2` instead
    /// (`cloud::hold_connecting_state`).
    pub const CLOUD_CONN_STATE: usize = 10120;
}

/// A hopper's raw food-level byte (`off::FOOD_1`/`off::FOOD_2`: 0/1/2, or `0xff` "never
/// reported since boot") collapsed to "is this a problem" -- pure so the tests can drive every
/// boundary without a live `config_shm`, the same reasoning as `ai.rs::track_key`. `< 2` is the
/// vendor's own threshold, not an assumption: see the doc comment on `off::FOOD_1` for the two
/// independent disassembled consumers (`ctrl`'s tone-alarm gate, `ble`'s own warning-flag
/// setter/clearer) that both use exactly this boundary.
fn hopper_empty_from_byte(raw: u8) -> Option<bool> {
    match raw {
        0xff => None,
        v => Some(v < 2),
    }
}

/// Bytes of the PetTrack block this module decodes: header (16) + 20 tracker entries of 24.
/// The vomit array that follows (`+0x1f0`) is not decoded -- nothing consumes it.
pub const PET_TRACK_LEN: usize = 0x1f0;
const TRACK_ENTRY_LEN: usize = 0x18;
const TRACK_MAX: usize = 20;

/// One entry of the PetTrack tracker array (`state.rs::off::PET_TRACK + 0x10 + 0x18*i`).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TrackEntry {
    /// The vendor's cloud pet id (`petId` in `/opt/pet_name_color.json`).
    pub pet_id: u32,
    /// Unix seconds, u64 LE at `+8`.
    pub start_time: u64,
    /// f32 at `+0x10`: the vendor's `total_score` (their own name -- the confirmation
    /// threshold setter is `petkit_modify_discern_total_score`). Study/TrackValue.md traced
    /// it in `libalgo`: the running SUM over every qualifying frame of the visit of that
    /// frame's best-candidate confidence (`vadd.f32` into `TrackData+0x18`, copied out
    /// unmodified). Not normalised, so larger = a longer visit and/or steadier matches; live
    /// values ran 276..2759.
    pub value: f32,
}

/// The vendor's on-device pet-identification result, decoded from `config_shm`.
#[derive(Debug, Clone, PartialEq)]
pub struct PetTrack {
    pub pet_id: u32,
    pub count: u32,
    pub area: u32,
    /// `tracker_count` entries, in the order the vendor keeps them.
    pub trackers: Vec<TrackEntry>,
}

impl PetTrack {
    /// Decodes [`PET_TRACK_LEN`] bytes. `tracker_count` is clamped to the array's capacity so a
    /// torn or garbage header can never index past the block.
    pub fn decode(b: &[u8]) -> Option<PetTrack> {
        if b.len() < PET_TRACK_LEN {
            return None;
        }
        let u32_at = |at: usize| u32::from_le_bytes(b[at..at + 4].try_into().unwrap());
        let n = (u32_at(4) as usize).min(TRACK_MAX);
        let trackers = (0..n)
            .map(|i| {
                let e = 0x10 + i * TRACK_ENTRY_LEN;
                TrackEntry {
                    pet_id: u32_at(e),
                    start_time: u64::from_le_bytes(b[e + 8..e + 16].try_into().unwrap()),
                    value: f32::from_le_bytes(b[e + 0x10..e + 0x14].try_into().unwrap()),
                }
            })
            .collect();
        Some(PetTrack { pet_id: u32_at(0), count: u32_at(8), area: u32_at(0xc), trackers })
    }

    /// The most recent tracker entry by `start_time`, if any.
    pub fn latest(&self) -> Option<&TrackEntry> {
        self.trackers.iter().max_by_key(|e| e.start_time)
    }
}

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
}

const PROT_READ: c_int = 1;
const MAP_SHARED: c_int = 1;
const MAP_FAILED: isize = -1;

/// Read-only view of the live config. Reads are plain loads: no syscall, no copy.
pub struct Shm {
    base: *const u8,
    len: usize,
}

// The mapping is read-only and the pointer is stable for the process lifetime.
unsafe impl Send for Shm {}
unsafe impl Sync for Shm {}

impl Shm {
    pub fn open() -> io::Result<Self> {
        let f = File::open(SHM_PATH)?;
        let len = f.metadata()?.len() as usize;
        if len < SHM_LEN {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("{SHM_PATH} is {len} bytes, expected at least {SHM_LEN}"),
            ));
        }
        let p = unsafe {
            mmap(
                std::ptr::null_mut(),
                len,
                PROT_READ,
                MAP_SHARED,
                f.as_raw_fd(),
                0,
            )
        };
        if p as isize == MAP_FAILED {
            return Err(io::Error::last_os_error());
        }
        Ok(Self {
            base: p as *const u8,
            len,
        })
    }

    #[inline]
    fn bytes(&self, at: usize, n: usize) -> &[u8] {
        assert!(at + n <= self.len, "read past end of config_shm");
        unsafe { std::slice::from_raw_parts(self.base.add(at), n) }
    }

    #[inline]
    pub fn u8(&self, at: usize) -> u8 {
        self.bytes(at, 1)[0]
    }

    #[inline]
    pub fn u32(&self, at: usize) -> u32 {
        u32::from_le_bytes(self.bytes(at, 4).try_into().unwrap())
    }

    /// NUL-terminated string field. Trailing garbage past the NUL is ignored.
    pub fn str(&self, at: usize, cap: usize) -> String {
        let b = self.bytes(at, cap);
        let end = b.iter().position(|&c| c == 0).unwrap_or(cap);
        String::from_utf8_lossy(&b[..end]).into_owned()
    }
    /// A bowl-fill reading, or `None` while the vendor has it invalidated mid-feed.
    pub fn bowl_fill(&self, at: usize) -> Option<u32> {
        match self.u32(at) {
            u32::MAX => None,
            v => Some(v),
        }
    }

    /// Whether `media`'s eat detector currently sees a pet eating -- see [`off::EATING`].
    pub fn eating(&self) -> bool {
        self.u8(off::EATING) != 0
    }

    /// A hopper's food level collapsed to "is this a problem" -- see
    /// [`hopper_empty_from_byte`] for the threshold evidence.
    pub fn hopper_empty(&self, at: usize) -> Option<bool> {
        hopper_empty_from_byte(self.u8(at))
    }

    /// A hopper's raw MCU food level: 0 empty, 1 low, 2 ok -- `None` for the `0xff` "never
    /// reported since boot" sentinel. The three-way reading the vendor's own alarm gate is built
    /// on (docs/07-config.md §10); `hopper_empty` is its collapsed form.
    pub fn hopper_level(&self, at: usize) -> Option<u8> {
        match self.u8(at) {
            0xff => None,
            v => Some(v.min(2)),
        }
    }

    /// `usr.user_info.timezone_name` -- the device's real, cloud-configured IANA zone, read live
    /// rather than assumed. Empty if `config_shm` hasn't been populated yet (before the vendor's
    /// own `loaded` flag goes up) or if the field is genuinely blank.
    pub fn timezone_name(&self) -> String {
        self.str(off::TIMEZONE_NAME, 24)
    }

    /// The vendor's live pet-identification block (see [`PetTrack`]). `media` writes it
    /// without any lock we share, so the bytes are copied twice and only accepted when both
    /// copies agree -- a write landing between the copies just means "try next tick".
    pub fn pet_track(&self) -> Option<PetTrack> {
        let first: [u8; PET_TRACK_LEN] = self.bytes(off::PET_TRACK, PET_TRACK_LEN).try_into().unwrap();
        let second = self.bytes(off::PET_TRACK, PET_TRACK_LEN);
        if first[..] != second[..] {
            return None;
        }
        PetTrack::decode(&first)
    }

    pub fn snapshot(&self) -> Snapshot {
        let timezone_name = self.timezone_name();
        let scheduler_tz_supported = crate::localtime::tz_for_iana_name(&timezone_name).is_some();
        let track = self.pet_track().and_then(|t| t.latest().copied());
        Snapshot {
            serial: self.str(off::SERIAL, 32),
            firmware: self.str(off::FIRMWARE, 16),
            ble_firmware: self.u32(off::BLE_FIRMWARE),
            volume: self.u8(off::VOLUME),
            desiccant_days: self.u8(off::DESICCANT_DAYS),
            feeding: self.u8(off::FEEDING) != 0,
            eating: self.eating(),
            bowl_fill: self.bowl_fill(off::BOWL_FILL_1),
            hopper_1_empty: self.hopper_empty(off::FOOD_1),
            hopper_2_empty: self.hopper_empty(off::FOOD_2),
            hopper_1_level: self.hopper_level(off::FOOD_1),
            hopper_2_level: self.hopper_level(off::FOOD_2),
            event_counter: self.u8(off::EVENT_COUNTER),
            timezone_name,
            scheduler_tz_supported,
            track,
        }
    }
}

impl Drop for Shm {
    fn drop(&mut self) {
        unsafe { munmap(self.base as *mut c_void, self.len) };
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Snapshot {
    pub serial: String,
    pub firmware: String,
    pub ble_firmware: u32,
    pub volume: u8,
    pub desiccant_days: u8,
    pub feeding: bool,
    /// `media`'s eat-in-progress flag -- see [`off::EATING`].
    pub eating: bool,
    /// The feeder's own vision estimate of bowl fullness, 0-100 -- `None` while invalid
    /// (`0xffffffff`: during a feed, or before `media`'s first run after boot).
    pub bowl_fill: Option<u32>,
    /// A hopper's food level collapsed to a problem flag -- see [`Shm::hopper_empty`].
    pub hopper_1_empty: Option<bool>,
    pub hopper_2_empty: Option<bool>,
    /// The raw three-level reading behind the flags -- see [`Shm::hopper_level`].
    pub hopper_1_level: Option<u8>,
    pub hopper_2_level: Option<u8>,
    pub event_counter: u8,
    /// `usr.user_info.timezone_name`, as read live from `config_shm` -- see
    /// `Shm::timezone_name`.
    pub timezone_name: String,
    /// Whether `localtime::tz_for_iana_name` recognizes [`Snapshot::timezone_name`] -- `false`
    /// means the scheduler (`scheduler.rs`) refuses to run even if enabled, per
    /// STUDY-schedule-encoding.md §11.1 item 2's "fail closed, never guess a DST rule" rule.
    pub scheduler_tz_supported: bool,
    /// The vendor's most recent pet identification ([`PetTrack::latest`]), or `None` when the
    /// block is empty or was caught mid-write.
    pub track: Option<TrackEntry>,
}

impl Snapshot {
    /// Hand-rolled so the agent carries no serialisation dependency.
    pub fn to_json(&self) -> String {
        fn opt(v: Option<u32>) -> String {
            v.map_or("null".into(), |n| n.to_string())
        }
        fn opt_bool(v: Option<bool>) -> &'static str {
            match v {
                None => "null",
                Some(true) => "true",
                Some(false) => "false",
            }
        }
        let track = match self.track {
            Some(t) => format!(
                r#"{{"pet_id":{},"start_unix":{},"value":{}}}"#,
                t.pet_id, t.start_time, t.value
            ),
            None => "null".into(),
        };
        format!(
            concat!(
                r#"{{"serial":"{}","firmware":"{}","ble_firmware":{},"volume":{},"#,
                r#""desiccant_days":{},"feeding":{},"eating":{},"bowl_fill":{},"hopper_empty":[{},{}],"hopper_level":[{},{}],"#,
                r#""event_counter":{},"timezone_name":"{}","scheduler_tz_supported":{},"track":{}}}"#
            ),
            self.serial.escape_debug(),
            self.firmware.escape_debug(),
            self.ble_firmware,
            self.volume,
            self.desiccant_days,
            self.feeding,
            self.eating,
            opt(self.bowl_fill),
            opt_bool(self.hopper_1_empty),
            opt_bool(self.hopper_2_empty),
            opt(self.hopper_1_level.map(u32::from)),
            opt(self.hopper_2_level.map(u32::from)),
            self.event_counter,
            self.timezone_name.escape_debug(),
            self.scheduler_tz_supported,
            track,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Snapshot {
        Snapshot {
            serial: "SN123".into(),
            firmware: "895".into(),
            ble_firmware: 159,
            volume: 6,
            desiccant_days: 30,
            feeding: false,
            eating: false,
            bowl_fill: Some(50),
            hopper_1_empty: Some(false),
            hopper_1_level: Some(2),
            hopper_2_level: None,
            hopper_2_empty: Some(false),
            event_counter: 3,
            timezone_name: "America/New_York".into(),
            scheduler_tz_supported: true,
            track: None,
        }
    }

    /// The first 88 bytes of `config_shm[10368..]` as read on the live feeder on 2026-09-16
    /// (study/a3watch.log), padded with zeros to the block length.
    fn live_block() -> Vec<u8> {
        let head = [
            0x08, 0x08, 0x0a, 0x06, 0x01, 0, 0, 0, 0x01, 0, 0, 0, 0x64, 0, 0, 0, // header
            0x08, 0x08, 0x0a, 0x06, 0, 0, 0, 0, 0x88, 0x0b, 0xaa, 0x6a, 0, 0, 0, 0, // entry 0
            0xae, 0xa0, 0x00, 0x45, 0, 0, 0, 0,
        ];
        let mut b = head.to_vec();
        b.resize(PET_TRACK_LEN, 0);
        b
    }

    #[test]
    fn decodes_the_live_identification_block() {
        let t = PetTrack::decode(&live_block()).unwrap();
        assert_eq!(t.pet_id, 101320712); // == petId in /opt/pet_name_color.json
        assert_eq!((t.count, t.area), (1, 100));
        assert_eq!(t.trackers.len(), 1);
        let e = t.latest().unwrap();
        assert_eq!((e.pet_id, e.start_time), (101320712, 1789528968));
        assert!((e.value - 2058.042).abs() < 0.01);
    }

    #[test]
    fn tracker_count_is_clamped_and_short_input_rejected() {
        let mut b = live_block();
        b[4..8].copy_from_slice(&u32::MAX.to_le_bytes());
        assert_eq!(PetTrack::decode(&b).unwrap().trackers.len(), TRACK_MAX);
        assert!(PetTrack::decode(&b[..PET_TRACK_LEN - 1]).is_none());
    }

    #[test]
    fn to_json_reports_track_or_null() {
        let mut s = sample();
        assert!(s.to_json().ends_with(r#""track":null}"#));
        s.track = Some(TrackEntry { pet_id: 101320712, start_time: 1789528968, value: 2058.042 });
        assert!(s.to_json().contains(r#""track":{"pet_id":101320712,"start_unix":1789528968,"value":2058.042}"#));
    }

    #[test]
    fn to_json_includes_timezone_fields() {
        let json = sample().to_json();
        assert!(json.contains(r#""timezone_name":"America/New_York""#));
        assert!(json.contains(r#""scheduler_tz_supported":true"#));
        assert!(json.contains(r#""bowl_fill":50"#));
    }

    #[test]
    fn to_json_reports_unsupported_zone_honestly() {
        let mut s = sample();
        s.timezone_name = "Europe/London".into();
        s.scheduler_tz_supported = false;
        let json = s.to_json();
        assert!(json.contains(r#""timezone_name":"Europe/London""#));
        assert!(json.contains(r#""scheduler_tz_supported":false"#));
    }

    #[test]
    fn hopper_empty_from_byte_thresholds_at_two_and_treats_0xff_as_unset() {
        // The vendor's own boundary (docs/07-config.md): both `ctrl`'s tone-alarm gate and
        // `ble`'s warning-flag setter/clearer treat 0 and 1 as a problem, 2 as fine.
        assert_eq!(hopper_empty_from_byte(0), Some(true));
        assert_eq!(hopper_empty_from_byte(1), Some(true));
        assert_eq!(hopper_empty_from_byte(2), Some(false));
        assert_eq!(hopper_empty_from_byte(3), Some(false));
        assert_eq!(hopper_empty_from_byte(0xff), None);
    }

    #[test]
    fn to_json_reports_hopper_empty() {
        let mut s = sample();
        s.hopper_1_empty = Some(true);
        s.hopper_2_empty = None;
        assert!(s.to_json().contains(r#""hopper_empty":[true,null]"#));
        assert!(s.to_json().contains(r#""hopper_level":[2,null]"#));
    }
}
