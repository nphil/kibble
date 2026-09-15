//! The feeder's internal message bus.
//!
//! Every vendor process (`ctrl`, `ble`, `media`, `cloud`, `agora`, `watchdog`, `logUpload`) owns one
//! POSIX message queue named `/msg_dispatch_<id>` and receives work on it. The wire format was
//! recovered from `ctrl!dispatch_send_msg` (vaddr 0x80b00):
//!
//! ```text
//!     u16 msg_id | u16 src | payload[len]        mq_send(mqd, buf, 4 + len, 0)
//! ```
//!
//! `dst` never travels in the message; it only selects which queue to open. A payload longer than
//! 540 bytes is silently clamped by the vendor, so we reject it instead.

use std::ffi::CString;
use std::io;
use std::os::raw::{c_char, c_int, c_uint};

pub const MAX_PAYLOAD: usize = 0x21c; // 540; queue msgsize is 544 = 4 + this

/// Queue ids, i.e. the `dst` of a message.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(u32)]
pub enum Peer {
    Ctrl = 1,
    Media = 2,
    Cloud = 4,
    Watchdog = 5,
    Agora = 7,
    Ble = 8,
    LogUpload = 10,
}

/// Message ids we send or expect. Recovered from sender immediates and handler-name tables;
/// see STUDY-msgids.md and STUDY-feedtest.md.
pub mod msg {
    /// ctrl -> ble: dispense or cancel. Payload is [`super::FeedCtrl`]. Proven by dispensing.
    pub const BLE_FEED_CTRL: u16 = 0x6004;
    /// -> ctrl: an incoming feed request (what the cloud path delivers).
    pub const FEED: u16 = 0x100f;
    /// ble -> ctrl: bytes received from a BLE peer.
    pub const RECV_BLE_DATA: u16 = 0x100a;
    /// ctrl's OWN dst=1 inbox, registered handler `dispatch_handler_ble_get_schedule`. Despite
    /// the name this is NOT a message to `ble`, and it is NOT a working read: STUDY-schedule.md
    /// §1.2 fully disassembled the handler and it is a dead stub (every path returns 0, no
    /// `dispatch_send_msg` call at all). Kept only as a documented, confirmed dead end -- see
    /// [`super::schedule`] for why the schedule cache, not a device read, is the source of truth.
    pub const BLE_GET_SCHEDULE: u16 = 0x101a;
    /// ctrl -> ble: replaces the whole schedule table. Payload is [`super::schedule::WireEntry`]
    /// entries behind a 2-byte `{count, reserved}` header. STUDY-schedule.md §3: pure pass-
    /// through to UART CMD 0x04, no ble-side struct of its own.
    pub const BLE_SET_SCHEDULE: u16 = 0x6005;
    /// ctrl -> ble: 4-byte little-endian Unix timestamp. `ctrl` sends this immediately before
    /// every schedule-set (STUDY-schedule.md §3.2); we mirror that ordering.
    pub const BLE_SET_RTC: u16 = 0x6007;
    /// ctrl -> ble: enable/disable BLE advertising, `dispatch_handler_ble_set_adv`. Payload
    /// byte 0 is `1`=on/`0`=off, rest padding to the 4-byte shape every sender (`pktool`'s
    /// `bleadv 0|1`, `ctrl`'s own pairing flow, us) uses. `ble` relays it to the T31 MCU as
    /// UART CMD `0x09`, subaddr `2` -- see docs/26-ble-advertising.md for the full disassembly
    /// trace and why nothing here implies a timeout: that lives entirely in [`super::advertise`].
    pub const BLE_SET_ADV: u16 = 0x6001;
}

type MqdT = c_int;

extern "C" {
    fn mq_open(name: *const c_char, oflag: c_int, ...) -> MqdT;
    fn mq_send(mqdes: MqdT, msg_ptr: *const c_char, msg_len: usize, msg_prio: c_uint) -> c_int;
    fn mq_close(mqdes: MqdT) -> c_int;
}

const O_WRONLY: c_int = 1;

/// A send handle for one peer's queue. Cheap to keep open; the vendor does the same.
pub struct Sender {
    mqd: MqdT,
    /// Value we put in the `src` field. The vendor reads its own process id from a global.
    src: u16,
}

impl Sender {
    pub fn open(peer: Peer, src: u16) -> io::Result<Self> {
        let name = CString::new(format!("/msg_dispatch_{}", peer as u32)).unwrap();
        let mqd = unsafe { mq_open(name.as_ptr(), O_WRONLY) };
        if mqd < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self { mqd, src })
    }

    pub fn send(&self, msg_id: u16, payload: &[u8]) -> io::Result<()> {
        if payload.len() > MAX_PAYLOAD {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload exceeds 540 bytes",
            ));
        }
        let mut buf = [0u8; 4 + MAX_PAYLOAD];
        buf[0..2].copy_from_slice(&msg_id.to_le_bytes());
        buf[2..4].copy_from_slice(&self.src.to_le_bytes());
        buf[4..4 + payload.len()].copy_from_slice(payload);
        let n = 4 + payload.len();
        let rc = unsafe { mq_send(self.mqd, buf.as_ptr() as *const c_char, n, 0) };
        if rc < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    /// The raw descriptor, for a context that cannot use [`Sender::send`] safely -- namely a
    /// signal handler, which must not allocate or take a lock the interrupted code might
    /// already hold. Nothing here is unsafe by itself; [`send_raw`] is the signal-safe sender
    /// that actually uses it. See [`super::advertise`]'s shutdown handler, the one caller.
    pub fn raw(&self) -> MqdT {
        self.mqd
    }
}

impl Drop for Sender {
    fn drop(&mut self) {
        unsafe { mq_close(self.mqd) };
    }
}

/// Signal-handler-safe send: fixed-size stack buffer, no allocation, no lock -- unlike
/// [`Sender::send`], which is safe everywhere else but must not be called from a signal handler
/// (its `Sender` would need to be reached through a lock or reopened through `CString`, both of
/// which can deadlock if the interrupted code was already inside the allocator). `mqd` is a
/// [`Sender::raw`] descriptor kept in a plain `AtomicI32` for exactly this purpose.
pub fn send_raw(mqd: MqdT, msg_id: u16, src: u16, payload: &[u8; 4]) {
    let mut buf = [0u8; 8];
    buf[0..2].copy_from_slice(&msg_id.to_le_bytes());
    buf[2..4].copy_from_slice(&src.to_le_bytes());
    buf[4..8].copy_from_slice(payload);
    unsafe {
        mq_send(mqd, buf.as_ptr() as *const c_char, 8, 0);
    }
}

/// Payload of [`msg::BLE_FEED_CTRL`], 67 bytes. Layout from the two builders in `ctrl`
/// (`dispatch_handler_feed` @0x44f98 forwards it verbatim; @0x44020 builds it from the cloud's
/// `feed_realtime` JSON: `id`, `amount1`, `amount2`, and `feed_realtime_cancel` sets byte 0).
pub struct FeedCtrl {
    pub cancel: bool,
    /// Feed-record id. The vendor uses the cloud's record id; ours just has to be unique.
    pub id: String,
    pub amount1: u8,
    pub amount2: u8,
}

impl FeedCtrl {
    pub const LEN: usize = 67;

    pub fn encode(&self) -> [u8; Self::LEN] {
        let mut b = [0u8; Self::LEN];
        b[0] = self.cancel as u8;
        let id = self.id.as_bytes();
        let n = id.len().min(63); // id[64], always NUL-terminated
        b[1..1 + n].copy_from_slice(&id[..n]);
        b[65] = self.amount1;
        b[66] = self.amount2;
        b
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The exact bytes that dispensed food on 2026-09-15, minus the 4-byte header.
    #[test]
    fn feed_payload_matches_proven_frame() {
        let got = FeedCtrl {
            cancel: false,
            id: "kibbletest1".into(),
            amount1: 1,
            amount2: 0,
        }
        .encode();
        assert_eq!(got[0], 0x00);
        assert_eq!(&got[1..12], b"kibbletest1");
        assert!(got[12..65].iter().all(|&b| b == 0));
        assert_eq!(got[65], 1);
        assert_eq!(got[66], 0);
    }

    /// An over-long id must not run past the 64-byte field into the amounts.
    #[test]
    fn oversized_id_is_truncated_and_terminated() {
        let got = FeedCtrl {
            cancel: false,
            id: "x".repeat(200),
            amount1: 7,
            amount2: 9,
        }
        .encode();
        assert_eq!(got[63], b'x');
        assert_eq!(got[64], 0, "id field must stay NUL-terminated");
        assert_eq!(got[65], 7);
        assert_eq!(got[66], 9);
    }
}
