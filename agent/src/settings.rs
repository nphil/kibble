//! The device settings the Petkit app exposes (Localkit's `property/set` schema), mapped onto
//! fixed byte offsets in `/dev/shm/config_shm`.
//!
//! Offsets, widths and notify mechanisms come directly from `docs/15-settings-write.md`'s
//! disassembly of `ctrl`'s settings-write handler (`0x3c3b2`-`~0x40100`) — every offset below was
//! read off that field's own `str`/`str.w`/`strb.w` instruction, not inferred from adjacency
//! (adjacency is a cross-check where it lines up, not the evidence itself). Value domains
//! (bool vs. ranged int) come from `docs/appendix-localkit.md`'s harvested `Configuration.php`
//! schema, the phone app's own documented settings surface.
//!
//! Three keys from that same study are deliberately absent from this table:
//! - `foodWarnRange` — the array-store instructions use register+register addressing, so its
//!   base offset was never pinned (§3, §7.2).
//! - `capacity` — not a scalar write at all (a cloud storage-quota report field); no local
//!   config_shm write site exists (§3's own row says so explicitly).
//! - the bonus "device local timezone" float field — dispatch confirmed, offset not resolved.
//!
//! `writable` is a second, independent bit from "we know the offset": it is set only once
//! kibbled's own write for that specific key has been verified live against the device (read
//! back independently, `/opt/user.conf`'s MD5 re-validated afterward — see the deploy notes).
//! Getting one field's offset wrong would silently corrupt its neighbour, so "documented" and
//! "verified" are kept as two different bits; every other row ships read-only.

use crate::bus::Peer;
use crate::state::{Shm, SHM_LEN};

/// Storage width of one setting's value in `config_shm`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Width {
    U8,
    U32,
}

impl Width {
    pub const fn bytes(self) -> usize {
        match self {
            Width::U8 => 1,
            Width::U32 => 4,
        }
    }
}

/// How to interpret and, on write, range-check a setting's integer value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// Stored as 0 or 1; any nonzero input is rejected rather than silently coerced.
    Bool,
    /// A plain integer, checked against `[min, max]` inclusive on write.
    Int { min: u32, max: u32 },
}

impl Kind {
    /// Whether `value` is acceptable to write for this kind.
    pub fn accepts(self, value: u32) -> bool {
        match self {
            Kind::Bool => value == 0 || value == 1,
            Kind::Int { min, max } => (min..=max).contains(&value),
        }
    }
}

/// A follow-up bus message a write must send for the new value to take effect immediately,
/// beyond the `config_shm` write itself. `STUDY-settings-write.md` §2.1 traced every other
/// "notify" call in this handler to `ctrl`'s own inbox with an msg_id no registered handler
/// recognizes in this firmware build — confirmed dead code, safe to skip. `light` is the one
/// exception: msg_id 0x10 reaches `media`'s own registered handler table.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Notify {
    pub peer: Peer,
    pub msg_id: u16,
    /// The new value, truncated to this many low bytes little-endian, is the payload
    /// (`STUDY-settings-write.md` §3: "payload=1" for `light`, observed with the write itself
    /// having just set the field to 1).
    pub payload_len: usize,
}

#[allow(dead_code)] // `cjson_key`/`description` are citation-grade documentation, not consumed by code
pub struct Setting {
    /// snake_case key used by the HTTP JSON API (`GET /config`, `POST /config`).
    pub key: &'static str,
    /// The vendor's own `property/set` JSON key, where a distinct one exists. `None` for the one
    /// field discovered as an undocumented neighbour of `moveDetection` with no key of its own.
    pub cjson_key: Option<&'static str>,
    pub offset: usize,
    pub width: Width,
    pub kind: Kind,
    pub notify: Option<Notify>,
    pub writable: bool,
    pub description: &'static str,
}

impl Setting {
    /// Current value, widened to `u32` regardless of storage width.
    pub fn read(&self, shm: &Shm) -> u32 {
        match self.width {
            Width::U8 => shm.u8(self.offset) as u32,
            Width::U32 => shm.u32(self.offset),
        }
    }
}

/// Look up a setting by its HTTP API key.
pub fn find(key: &str) -> Option<&'static Setting> {
    SETTINGS.iter().find(|s| s.key == key)
}

/// Every setting's current value, flat `{"key": value, ...}` JSON — the body of `GET /config`.
pub fn to_json(shm: &Shm) -> String {
    let mut s = String::from("{");
    for (i, setting) in SETTINGS.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        s.push('"');
        s.push_str(setting.key);
        s.push_str("\":");
        s.push_str(&setting.read(shm).to_string());
    }
    s.push('}');
    s
}

const MINUTES_OF_DAY: Kind = Kind::Int { min: 0, max: 1440 };

pub const SETTINGS: &[Setting] = &[
    // --- Camera/AV block, config_shm 3064-3083 (contiguous, all u32) ---
    Setting {
        key: "night",
        cjson_key: Some("night"),
        offset: 3064,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: true, // verification pending this deploy cycle, see project report
        description: "Night vision (IR) enable",
    },
    Setting {
        key: "time_display",
        cjson_key: Some("timeDisplay"),
        offset: 3068,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Video timestamp overlay",
    },
    Setting {
        key: "light",
        cjson_key: Some("light"),
        offset: 3072,
        width: Width::U32,
        kind: Kind::Bool,
        // `0x10` is `ctrl`'s own `dispatch_handler_ledlight_mode_set` (its inbox is queue 2).
        // Named `Peer::Media` before the 2026-09-16 queue-id correction in `bus.rs`, which was
        // the same queue under the wrong name -- the wire target is unchanged.
        notify: Some(Notify {
            peer: Peer::Ctrl,
            msg_id: 0x10,
            payload_len: 1,
        }),
        writable: true, // verification pending this deploy cycle, see project report
        description: "Status LED enable",
    },
    Setting {
        key: "microphone",
        cjson_key: Some("microphone"),
        offset: 3076,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: true, // verification pending this deploy cycle, see project report
        description: "Microphone enable",
    },
    Setting {
        key: "camera",
        cjson_key: Some("camera"),
        offset: 3080,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Camera stream enable",
    },
    // --- Detection block, config_shm 3464-3699 ---
    Setting {
        key: "move_detection",
        cjson_key: Some("moveDetection"),
        offset: 3464,
        width: Width::U8,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Motion detection enable",
    },
    Setting {
        key: "move_track_enable",
        cjson_key: None,
        offset: 3465,
        width: Width::U8,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Motion tracking (undocumented neighbour of moveDetection, no cJSON key of its own)",
    },
    Setting {
        key: "move_sensitivity",
        cjson_key: Some("moveSensitivity"),
        offset: 3468,
        width: Width::U32,
        kind: Kind::Int { min: 1, max: 9 },
        notify: None,
        writable: false,
        description: "Motion detection sensitivity (1-9)",
    },
    Setting {
        key: "pet_detection",
        cjson_key: Some("petDetection"),
        offset: 3520,
        width: Width::U8,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Pet-visit (AI) detection enable",
    },
    Setting {
        key: "pet_sensitivity",
        cjson_key: Some("petSensitivity"),
        offset: 3524,
        width: Width::U32,
        kind: Kind::Int { min: 1, max: 9 },
        notify: None,
        writable: false,
        description: "Pet detection sensitivity (1-9)",
    },
    Setting {
        key: "eat_detection",
        cjson_key: Some("eatDetection"),
        offset: 3576,
        width: Width::U8,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Eating detection enable",
    },
    Setting {
        key: "eat_sensitivity",
        cjson_key: Some("eatSensitivity"),
        offset: 3580,
        width: Width::U32,
        kind: Kind::Int { min: 1, max: 9 },
        notify: None,
        writable: false,
        description: "Eating detection sensitivity (1-9)",
    },
    Setting {
        key: "vomit_detection",
        cjson_key: Some("vomitDetection"),
        offset: 3632,
        width: Width::U8,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Vomit detection enable",
    },
    Setting {
        key: "detect_interval",
        cjson_key: Some("detectInterval"),
        offset: 3688,
        width: Width::U32,
        kind: Kind::Int { min: 0, max: 300 },
        notify: None,
        writable: false,
        description: "Minimum seconds between detections (global)",
    },
    Setting {
        key: "detect_range_from",
        cjson_key: Some("detectMultiRange"),
        offset: 3692,
        width: Width::U32,
        kind: MINUTES_OF_DAY,
        notify: None,
        writable: false,
        description: "Detection active-hours schedule, first range start (minutes of day; array has more entries this study did not resolve the stride of)",
    },
    Setting {
        key: "detect_range_till",
        cjson_key: Some("detectMultiRange"),
        offset: 3696,
        width: Width::U32,
        kind: MINUTES_OF_DAY,
        notify: None,
        writable: false,
        description: "Detection active-hours schedule, first range end (minutes of day)",
    },
    // --- Feed/sound/calibration block, config_shm 3732-3771 (contiguous, all u32) ---
    Setting {
        key: "feed_picture",
        cjson_key: Some("feedPicture"),
        offset: 3732,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Capture photo on feed",
    },
    Setting {
        key: "eat_video",
        cjson_key: Some("eatVideo"),
        offset: 3736,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Record video clip on eat detection",
    },
    Setting {
        key: "sound_enable",
        cjson_key: Some("soundEnable"),
        offset: 3740,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Voice prompt on feed dispense",
    },
    Setting {
        key: "system_sound_enable",
        cjson_key: Some("systemSoundEnable"),
        offset: 3744,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "System guidance voice",
    },
    Setting {
        key: "feed_sound",
        cjson_key: Some("feedSound"),
        offset: 3748,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Sound on feed complete",
    },
    Setting {
        key: "volume",
        cjson_key: Some("volume"),
        offset: 3752,
        width: Width::U32,
        kind: Kind::Int { min: 0, max: 9 },
        notify: None,
        writable: true, // verification pending this deploy cycle, see project report
        description: "Speaker volume (0-9 app scale)",
    },
    Setting {
        key: "selected_sound",
        cjson_key: Some("selectedSound"),
        offset: 3756,
        width: Width::U32,
        kind: Kind::Int { min: 0, max: u32::MAX },
        notify: None,
        writable: false,
        description: "Selected notification sound ID (valid id range not recovered by this study)",
    },
    Setting {
        key: "factor1",
        cjson_key: Some("factor1"),
        offset: 3760,
        width: Width::U32,
        kind: Kind::Int { min: 1, max: 100 },
        notify: None,
        writable: false,
        description: "Hopper 1 calibration factor (cJSON also accepts bare \"factor\" as an alias for this same field)",
    },
    Setting {
        key: "factor2",
        cjson_key: Some("factor2"),
        offset: 3764,
        width: Width::U32,
        kind: Kind::Int { min: 1, max: 100 },
        notify: None,
        writable: false,
        description: "Hopper 2 calibration factor",
    },
    Setting {
        key: "food_warn",
        cjson_key: Some("foodWarn"),
        offset: 3768,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Low-food warning enable",
    },
    // --- Status LED schedule block, config_shm 3780-3791 (contiguous, all u32) ---
    Setting {
        key: "light_mode",
        cjson_key: Some("lightMode"),
        offset: 3780,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Status LED follows an active-hours schedule (distinct from `light`, the immediate on/off)",
    },
    Setting {
        key: "light_range_from",
        cjson_key: Some("lightMultiRange"),
        offset: 3784,
        width: Width::U32,
        kind: MINUTES_OF_DAY,
        notify: None,
        writable: false,
        description: "Status LED active-hours schedule, first range start (minutes of day)",
    },
    Setting {
        key: "light_range_till",
        cjson_key: Some("lightMultiRange"),
        offset: 3788,
        width: Width::U32,
        kind: MINUTES_OF_DAY,
        notify: None,
        writable: false,
        description: "Status LED active-hours schedule, first range end (minutes of day)",
    },
    // --- Do-not-disturb block, config_shm 3824-3835 (contiguous, all u32) ---
    Setting {
        key: "tone_mode",
        cjson_key: Some("toneMode"),
        offset: 3824,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Do-not-disturb (mute all sounds)",
    },
    Setting {
        key: "tone_range_from",
        cjson_key: Some("toneMultiRange"),
        offset: 3828,
        width: Width::U32,
        kind: MINUTES_OF_DAY,
        notify: None,
        writable: false,
        description: "Do-not-disturb hours, first range start (minutes of day)",
    },
    Setting {
        key: "tone_range_till",
        cjson_key: Some("toneMultiRange"),
        offset: 3832,
        width: Width::U32,
        kind: MINUTES_OF_DAY,
        notify: None,
        writable: false,
        description: "Do-not-disturb hours, first range end (minutes of day)",
    },
    // --- Child lock / schedule metadata / leftover-food block, config_shm 3872-3888 (contiguous) ---
    Setting {
        key: "manual_lock",
        cjson_key: Some("manualLock"),
        offset: 3872,
        width: Width::U32,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Child lock (disable physical buttons)",
    },
    Setting {
        key: "c_time",
        cjson_key: Some("CTime"),
        offset: 3876,
        width: Width::U32,
        kind: Kind::Int { min: 0, max: u32::MAX },
        notify: None,
        writable: false,
        description: "Schedule last-modified time (Unix timestamp; device-computed on every schedule write, folded into the schedule surface rather than getting its own HA entity)",
    },
    Setting {
        key: "surplus_control",
        cjson_key: Some("surplusControl"),
        offset: 3880,
        width: Width::U32,
        kind: Kind::Int { min: 0, max: u32::MAX },
        notify: None,
        writable: false,
        description: "Leftover-food detection threshold (Localkit's own app docs call this \
            read-only). docs/34-bowl-fill-surplus.md's disassembly of ble's 1Hz surplus ticker \
            (ble vaddr 0x13468/0x14e54) shows it is the live comparison operand against \
            BOWL_FILL_1 (`state == surplus_control > bowl_fill_1`, signed) and, exhaustively \
            cross-checked against every dispatch_send_msg call site in ctrl (93 total), no path \
            from writing it ever reaches feed/motor/OTA/reset -- confirmed safe to write. Shipped \
            non-writable anyway: live-tested twice this session (writable:true, deployed, \
            POST-then-restored both times) and neither run moved BOWL_FILL_1 -- once with it \
            invalid (predicted: the comparison is signed, so any realistic threshold reads as \
            greater than the invalid sentinel's -1, a no-op matching the ticker's already-settled \
            state) and once with a real value already in place, set to a threshold below it \
            (unpredicted: no effect observed over 42s, config_shm unchanged beyond the write \
            itself -- the persisted-ticker-byte model this session built from static analysis is \
            evidently incomplete). Re-flip writable only alongside a live test proving it helps.",
    },
    Setting {
        key: "surplus_standard",
        cjson_key: Some("surplusStandard"),
        offset: 3884,
        width: Width::U32,
        kind: Kind::Int { min: 0, max: 100 },
        notify: None,
        writable: false,
        description: "Leftover-food threshold",
    },
    Setting {
        key: "smart_frame",
        cjson_key: Some("smartFrame"),
        offset: 3888,
        width: Width::U8,
        kind: Kind::Bool,
        notify: None,
        writable: false,
        description: "Pet auto-tracking/framing in video",
    },
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_offset_and_width_fits_inside_config_shm() {
        for s in SETTINGS {
            assert!(
                s.offset + s.width.bytes() <= SHM_LEN,
                "{} at {}+{} runs past the {}-byte config_shm",
                s.key,
                s.offset,
                s.width.bytes(),
                SHM_LEN
            );
        }
    }

    /// The real bug class this table risks: ~40 hand-transcribed offsets, any two of which
    /// overlapping would mean writing one setting corrupts its neighbour's bytes.
    #[test]
    fn no_two_settings_overlap() {
        let mut spans: Vec<(usize, usize, &str)> = SETTINGS
            .iter()
            .map(|s| (s.offset, s.offset + s.width.bytes(), s.key))
            .collect();
        spans.sort_by_key(|&(start, ..)| start);
        for pair in spans.windows(2) {
            let (start_a, end_a, key_a) = pair[0];
            let (start_b, _end_b, key_b) = pair[1];
            assert!(
                end_a <= start_b,
                "{key_a} [{start_a},{end_a}) overlaps {key_b} at {start_b}"
            );
        }
    }

    #[test]
    fn keys_are_unique() {
        let mut keys: Vec<&str> = SETTINGS.iter().map(|s| s.key).collect();
        keys.sort_unstable();
        let mut deduped = keys.clone();
        deduped.dedup();
        assert_eq!(keys, deduped, "duplicate setting key");
    }

    #[test]
    fn find_resolves_a_known_key_and_rejects_unknown() {
        assert!(find("volume").is_some());
        assert!(find("not_a_real_setting").is_none());
    }

    #[test]
    fn only_light_carries_a_live_notify() {
        let notified: Vec<&str> = SETTINGS
            .iter()
            .filter(|s| s.notify.is_some())
            .map(|s| s.key)
            .collect();
        assert_eq!(notified, vec!["light"]);
    }

    #[test]
    fn bool_kind_accepts_only_zero_or_one() {
        assert!(Kind::Bool.accepts(0));
        assert!(Kind::Bool.accepts(1));
        assert!(!Kind::Bool.accepts(2));
        assert!(!Kind::Bool.accepts(u32::MAX));
    }

    #[test]
    fn int_kind_respects_its_bounds() {
        let k = Kind::Int { min: 1, max: 9 };
        assert!(!k.accepts(0));
        assert!(k.accepts(1));
        assert!(k.accepts(9));
        assert!(!k.accepts(10));
    }
}
