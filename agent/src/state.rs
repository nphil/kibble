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
    pub const BOWL_FILL_2: usize = 9920; // u32
    pub const EVENT_COUNTER: usize = 10184;
    /// Transient "a feed cycle is running" flag: 0 -> 1 -> 0 around a dispense.
    pub const FEEDING: usize = 10238;
    /// Watchdog liveness toggles, one byte per supervised process. Each owner flips its byte
    /// about every 2 s; if one goes stale the watchdog restarts, then kills, then reboots.
    pub const ALIVE_BLE: usize = 10284;
    pub const ALIVE_MEDIA: usize = 10288;
    pub const ALIVE_CTRL: usize = 10296;
    pub const ALIVE_AGORA: usize = 10300;
    pub const ALIVE_CLOUD: usize = 10304;
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

    /// `usr.user_info.timezone_name` -- the device's real, cloud-configured IANA zone, read live
    /// rather than assumed. Empty if `config_shm` hasn't been populated yet (before the vendor's
    /// own `loaded` flag goes up) or if the field is genuinely blank.
    pub fn timezone_name(&self) -> String {
        self.str(off::TIMEZONE_NAME, 24)
    }

    pub fn snapshot(&self) -> Snapshot {
        let timezone_name = self.timezone_name();
        let scheduler_tz_supported = crate::localtime::tz_for_iana_name(&timezone_name).is_some();
        Snapshot {
            serial: self.str(off::SERIAL, 32),
            firmware: self.str(off::FIRMWARE, 16),
            ble_firmware: self.u32(off::BLE_FIRMWARE),
            volume: self.u8(off::VOLUME),
            desiccant_days: self.u8(off::DESICCANT_DAYS),
            feeding: self.u8(off::FEEDING) != 0,
            bowl_fill_1: self.bowl_fill(off::BOWL_FILL_1),
            bowl_fill_2: self.bowl_fill(off::BOWL_FILL_2),
            event_counter: self.u8(off::EVENT_COUNTER),
            timezone_name,
            scheduler_tz_supported,
        }
    }
}

impl Drop for Shm {
    fn drop(&mut self) {
        unsafe { munmap(self.base as *mut c_void, self.len) };
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Snapshot {
    pub serial: String,
    pub firmware: String,
    pub ble_firmware: u32,
    pub volume: u8,
    pub desiccant_days: u8,
    pub feeding: bool,
    pub bowl_fill_1: Option<u32>,
    pub bowl_fill_2: Option<u32>,
    pub event_counter: u8,
    /// `usr.user_info.timezone_name`, as read live from `config_shm` -- see
    /// `Shm::timezone_name`.
    pub timezone_name: String,
    /// Whether `localtime::tz_for_iana_name` recognizes [`Snapshot::timezone_name`] -- `false`
    /// means the scheduler (`scheduler.rs`) refuses to run even if enabled, per
    /// STUDY-schedule-encoding.md §11.1 item 2's "fail closed, never guess a DST rule" rule.
    pub scheduler_tz_supported: bool,
}

impl Snapshot {
    /// Hand-rolled so the agent carries no serialisation dependency.
    pub fn to_json(&self) -> String {
        fn opt(v: Option<u32>) -> String {
            v.map_or("null".into(), |n| n.to_string())
        }
        format!(
            concat!(
                r#"{{"serial":"{}","firmware":"{}","ble_firmware":{},"volume":{},"#,
                r#""desiccant_days":{},"feeding":{},"bowl_fill":[{},{}],"event_counter":{},"#,
                r#""timezone_name":"{}","scheduler_tz_supported":{}}}"#
            ),
            self.serial.escape_debug(),
            self.firmware.escape_debug(),
            self.ble_firmware,
            self.volume,
            self.desiccant_days,
            self.feeding,
            opt(self.bowl_fill_1),
            opt(self.bowl_fill_2),
            self.event_counter,
            self.timezone_name.escape_debug(),
            self.scheduler_tz_supported,
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
            bowl_fill_1: Some(50),
            bowl_fill_2: None,
            event_counter: 3,
            timezone_name: "America/New_York".into(),
            scheduler_tz_supported: true,
        }
    }

    #[test]
    fn to_json_includes_timezone_fields() {
        let json = sample().to_json();
        assert!(json.contains(r#""timezone_name":"America/New_York""#));
        assert!(json.contains(r#""scheduler_tz_supported":true"#));
        assert!(json.contains(r#""bowl_fill":[50,null]"#));
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
}
